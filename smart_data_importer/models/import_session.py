from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
import logging

_logger = logging.getLogger(__name__)


def _default_target_model(self):
    return self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)


class ImportSession(models.Model):
    _name = 'import.session'
    _inherit = [
        'import.file.mixin',
        'import.deduplication.mixin',
        'import.progress.mixin',
        'import.signature.mixin'
    ]
    _description = 'Import Session'
    _order = 'create_date desc'

    name = fields.Char(string='Name', required=True, copy=False, readonly=True, default=lambda self: _('New'))
    state = fields.Selection([
        ('draft', 'Draft'),
        ('analyzing', 'Analyzing'),
        ('mapped', 'Mapped'),
        ('queued', 'Queued'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error')
    ], string='Status', default='draft', required=True, copy=False)
    user_id = fields.Many2one('res.users', string='User', default=lambda self: self.env.user, required=True)

    # Workbook / target
    sheet_ids = fields.One2many('import.session.sheet', 'session_id', string='Sheets', readonly=True)
    sheet_id = fields.Many2one(
        'import.session.sheet', string='Sheet to import',
        domain="[('session_id', '=', id), ('kind', '=', 'data')]",
        help="Excel workbooks can contain several sheets. Instructions and 'possible values' sheets are skipped.")
    target_model_id = fields.Many2one(
        'ir.model', string='Target Model', required=True, ondelete='cascade',
        default=_default_target_model, domain=[('transient', '=', False)],
        help="Odoo model that will receive the rows. Detected from the column headers when possible.")
    target_model_name = fields.Char(related='target_model_id.model', string='Target Model Name')
    model_detection_score = fields.Float(string='Model detection match', readonly=True,
                                         help="Share of technical column headers that exist on the detected model.")
    model_detection_note = fields.Char(string='Model detection note', readonly=True)
    dropped_rows = fields.Integer(string='Discarded empty rows', readonly=True)

    column_mapping_ids = fields.One2many('import.column.mapping', 'session_id', string='Column Mappings')
    job_ids = fields.One2many('import.job', 'session_id', string='Jobs')
    row_error_ids = fields.One2many('import.row.error', 'session_id', string='Row Errors')
    template_id = fields.Many2one('import.template', string='Applied Template')

    ai_analysis_summary = fields.Text(string='AI Analysis Summary', readonly=True)
    ai_template_name = fields.Char(string='AI Suggested Template Name', readonly=True)

    required_missing_count = fields.Integer(
        string='Empty required cells', compute='_compute_required_missing',
        help="Cells left empty in columns that the template marks as required.")

    @api.depends('column_mapping_ids.empty_count', 'column_mapping_ids.required_level', 'column_mapping_ids.is_ignored')
    def _compute_required_missing(self):
        for rec in self:
            rec.required_missing_count = sum(
                m.empty_count for m in rec.column_mapping_ids
                if m.required_level == 'required' and not m.is_ignored
            )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('import.session') or _('New')
        records = super().create(vals_list)
        for rec in records.filtered('file'):
            rec._sync_sheets()
        return records

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get('smart_importer_skip_sync'):
            return res
        if 'file' in vals or 'filename' in vals:
            for rec in self.filtered('file'):
                rec._sync_sheets()
        elif 'sheet_id' in vals and 'target_model_id' not in vals:
            for rec in self:
                rec._detect_target_model()
        return res

    # ------------------------------------------------------------------
    # Workbook helpers
    # ------------------------------------------------------------------

    def _read_file(self):
        """Read the selected sheet of the uploaded file."""
        self.ensure_one()
        from ..services.file_reader import FileReader
        return FileReader.read(self.file, self.filename, sheet_name=self.sheet_id.name or None)

    def _sync_sheets(self):
        """List the sheets of the uploaded workbook and pick a default data sheet."""
        self.ensure_one()
        from ..services.file_reader import FileReader
        summaries = FileReader.list_sheets(self.file, self.filename)

        ctx_self = self.with_context(smart_importer_skip_sync=True)
        ctx_self.sheet_id = False
        self.sheet_ids.unlink()
        sheets = self.env['import.session.sheet'].create([{
            'session_id': self.id,
            'sequence': i,
            'name': s['name'],
            'kind': s['kind'],
            'visible': s['visible'],
            'row_count': s['row_count'],
            'column_count': s['column_count'],
            'dropped_rows': s['dropped_rows'],
            'header_list': ', '.join(s['headers']),
        } for i, s in enumerate(summaries)])

        data_sheets = sheets.filtered(lambda s: s.kind == 'data')
        default = (data_sheets.filtered('visible') or data_sheets)[:1]
        ctx_self.sheet_id = default.id or False
        self._detect_target_model()

    def _detect_target_model(self):
        """Pick the target model whose fields best match the sheet headers."""
        self.ensure_one()
        if not self.sheet_id or not self.sheet_id.header_list:
            return
        from ..services.field_catalog import FieldCatalog
        headers = [h.strip() for h in self.sheet_id.header_list.split(',')]
        model_name, score = FieldCatalog.detect_model(self.env, headers)
        vals = {'model_detection_score': score, 'model_detection_note': False}
        model = model_name and self.env['ir.model'].search([('model', '=', model_name)], limit=1)
        if model:
            vals['target_model_id'] = model.id
            vals['duplicate_criterion'] = 'vat' if model_name == 'res.partner' else 'auto'
        else:
            vals['model_detection_note'] = _(
                "The target model could not be detected from the headers of sheet '%s'. "
                "Choose it manually. If the data belongs to an app that is not installed "
                "(e.g. fixed assets need Accounting Assets), install it first."
            ) % self.sheet_id.name
        self.with_context(smart_importer_skip_sync=True).write(vals)

    @api.onchange('target_model_id')
    def _onchange_target_model_id(self):
        if self.target_model_id:
            self.duplicate_criterion = 'vat' if self.target_model_id.model == 'res.partner' else 'auto'

    # ------------------------------------------------------------------
    # Templates
    # ------------------------------------------------------------------

    @api.onchange('template_id')
    def _onchange_template_id(self):
        if self.template_id and self.column_mapping_ids:
            self._apply_template_lines(self.template_id, use_write=False)

    def _apply_template_lines(self, template, use_write=True):
        from ..services.file_reader import FileReader
        lines = {FileReader.normalize_header(l.source_header): l for l in template.line_ids}
        for col in self.column_mapping_ids:
            t_line = lines.get(FileReader.normalize_header(col.source_header))
            if not t_line:
                continue
            vals = {
                'target_field_id': t_line.target_field_id.id or False,
                'target_subfield': t_line.target_subfield or False,
                'cleansing_rule_ids': [(6, 0, t_line.cleansing_rule_ids.ids)],
                'is_ignored': t_line.is_ignored,
                'source': 'template',
                'confidence': 1.0,
                'is_conflict': False,
            }
            if use_write:
                col.write(vals)
            else:
                col.update(vals)
        if template.duplicate_criterion:
            self.duplicate_criterion = template.duplicate_criterion
        if template.duplicate_policy:
            self.duplicate_policy = template.duplicate_policy

    def action_apply_template(self):
        self.ensure_one()
        if not self.template_id:
            raise ValidationError(_("Please select a template to apply."))
        self._apply_template_lines(self.template_id)
        self._compute_file_duplicates()
        return True

    def action_save_as_template(self):
        self.ensure_one()
        if not self.column_mapping_ids:
            raise ValidationError(_("No column mappings available to save as template."))
        lines_vals = [{
            'source_header': m.source_header,
            'target_field_id': m.target_field_id.id or False,
            'target_subfield': m.target_subfield or False,
            'cleansing_rule_ids': [(6, 0, m.cleansing_rule_ids.ids)],
            'is_ignored': m.is_ignored,
        } for m in self.column_mapping_ids]
        name = _("Template for %s") % (self.sheet_id.name or self.filename or self.name)
        template = self.env['import.template'].upsert_for_headers(
            headers=[m.source_header for m in self.column_mapping_ids],
            model=self.target_model_id,
            name=name,
            lines_vals=lines_vals,
            duplicate_criterion=self.duplicate_criterion,
            duplicate_policy=self.duplicate_policy,
        )
        self.template_id = template.id
        return self._notify(_('Template Saved'), _("Mapping saved as template '%s'.") % template.name)

    # ------------------------------------------------------------------
    # Pipeline actions
    # ------------------------------------------------------------------

    def _check_ready(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Please upload a file (CSV or Excel) first."))
        if not self.sheet_id and self.sheet_ids:
            raise UserError(_("This workbook has no data sheet to import. Sheets: %s")
                            % ', '.join(self.sheet_ids.mapped('name')))
        if not self.target_model_id:
            raise UserError(_("Choose the target model before analysing."))

    def action_analyze(self):
        self.ensure_one()
        self._check_ready()
        from ..services.orchestrator import PipelineOrchestrator
        PipelineOrchestrator.run_premapping(self)
        self.dropped_rows = self.sheet_id.dropped_rows
        self._compute_file_duplicates()
        return True

    def action_ai_prepare_and_upload_template(self):
        """
        AI Intelligent Preparation & Upload:
        Reads the selected sheet, asks the configured AI (DeepSeek, Claude, Gemini,
        OpenAI or a custom endpoint) to analyse it, then creates or updates the
        import.template and the session column mappings.
        """
        self.ensure_one()
        self._check_ready()

        config = self.env['ir.config_parameter'].sudo()
        if config.get_param('smart_data_importer.llm_enabled', 'False') != 'True':
            raise UserError(_("The AI assistant is disabled. Enable it in Smart Importer > Configuration > Settings and enter the API key."))

        from ..services.field_catalog import split_header
        from ..services.mapper_hash import HashMapper
        from ..services.mapper_llm import LLMMapper, LLMError
        from ..services.orchestrator import PipelineOrchestrator

        file_info = self._read_file()
        df = file_info['df']
        headers = list(df.columns)
        if not headers:
            raise UserError(_("The selected sheet has no recognisable columns."))
        model = self.target_model_id

        self.write({
            'encoding': file_info['encoding'],
            'delimiter': file_info['delimiter'],
            'header_row_index': file_info['header_row_index'],
            'total_rows': file_info['total_rows'],
            'dropped_rows': file_info['dropped_rows'],
        })

        sample_values_map = PipelineOrchestrator.sample_values(df)
        try:
            ai_data = LLMMapper.analyze_and_structure_template(
                env=self.env,
                headers=headers,
                sample_values_map=sample_values_map,
                total_rows=self.total_rows,
                filename="%s / %s" % (self.filename or self.name, self.sheet_id.name or ''),
                model_name=model.model,
            )
        except (LLMError, ValueError) as e:
            raise UserError(_("The AI analysis failed: %s") % e)

        # Technical headers are certain: they override whatever the AI said
        exact = HashMapper.map_technical(self.env, headers, model_name=model.model)

        resolved = {}
        for m in ai_data['mappings']:
            header = m['header']
            vals = {
                'target_field_id': m['target_field_id'] or False,
                'target_subfield': False,
                'source': 'llm',
                'confidence': m['confidence'],
                'llm_justification': m['justification'],
                'is_ignored': m['is_ignored'],
                'cleansing_rule_ids': [(6, 0, m['rule_ids'])],
            }
            if header in exact:
                vals.update(exact[header])
                vals['is_ignored'] = False
            base, sub = split_header(header)
            if sub and sub != 'id':
                vals.update({'is_ignored': True, 'target_field_id': False,
                             'llm_justification': _("Sub-field of related lines: not supported yet.")})
            resolved[header] = vals

        self.column_mapping_ids.unlink()
        mappings = PipelineOrchestrator.create_mappings(
            self, df, file_info['column_meta'], sample_values_map, resolved, auto_rules=False)

        template_name = ai_data.get('template_name') or _("AI Template - %s") % (self.sheet_id.name or self.filename)
        provider_name = (ai_data.get('provider') or 'AI').upper()
        criterion = ai_data.get('duplicate_criterion') if model.model == 'res.partner' else 'auto'
        template = self.env['import.template'].upsert_for_headers(
            headers=headers,
            model=model,
            name=template_name,
            lines_vals=[{
                'source_header': m.source_header,
                'target_field_id': m.target_field_id.id or False,
                'target_subfield': m.target_subfield or False,
                'cleansing_rule_ids': [(6, 0, m.cleansing_rule_ids.ids)],
                'is_ignored': m.is_ignored,
            } for m in mappings],
            duplicate_criterion=criterion,
            duplicate_policy=ai_data.get('duplicate_policy'),
            description=_("Generated by AI (%s - %s):\n\n%s") % (provider_name, ai_data.get('model', ''), ai_data.get('summary', '')),
        )

        self.write({
            'template_id': template.id,
            'duplicate_criterion': template.duplicate_criterion,
            'duplicate_policy': template.duplicate_policy,
            'ai_analysis_summary': ai_data.get('summary', ''),
            'ai_template_name': template.name,
            'state': 'mapped',
        })
        self._compute_file_duplicates()

        mapped_count = len(mappings.filtered(lambda m: m.target_field_id and not m.is_ignored))
        return self._notify(
            _('Template ready'),
            _("The AI (%s) analysed the sheet and saved template '%s' with %s mapped columns out of %s.")
            % (provider_name, template.name, mapped_count, len(mappings)),
        )

    def action_confirm_mapping(self):
        self.ensure_one()
        if self.state != 'mapped':
            raise ValidationError(_("Cannot confirm mapping in current state."))
        if not self.column_mapping_ids.filtered(lambda m: m.target_field_id and not m.is_ignored):
            raise ValidationError(_("No column is mapped to a field. Map at least one column before importing."))

        chunk_size = int(self.env['ir.config_parameter'].sudo().get_param('smart_data_importer.chunk_size', default='500')) or 500

        self.write({
            'start_time': fields.Datetime.now(),
            'end_time': False,
            'state': 'queued',
            'processed_rows': 0,
            'created_rows': 0,
            'updated_rows': 0,
            'skipped_rows': 0,
            'failed_rows': 0,
            'duplicate_count_odoo': 0,
        })
        self.job_ids.unlink()

        job_vals = [{
            'session_id': self.id,
            'chunk_index': i // chunk_size,
            'row_from': i,
            'row_to': min(i + chunk_size, self.total_rows),
            'state': 'pending',
        } for i in range(0, self.total_rows, chunk_size)]

        if not job_vals:
            self.write({'state': 'done', 'end_time': fields.Datetime.now()})
            return True

        self.state = 'processing'
        jobs = self.env['import.job'].create(job_vals)
        for job in jobs:
            if hasattr(job, 'with_delay'):
                job.with_delay(description=f"Import Session {self.name} - Chunk {job.chunk_index}").process_chunk()
            else:
                job.process_chunk()
        return True

    def _notify(self, title, message, ntype='success'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': ntype, 'sticky': False,
                       # reload the form so the new mappings are visible
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    # ------------------------------------------------------------------
    # OWL dashboard
    # ------------------------------------------------------------------

    preview_data = fields.Char(string='Preview', compute='_compute_preview_data', store=False)

    def _compute_preview_data(self):
        for rec in self:
            rec.preview_data = 'preview'

    def get_dashboard_summary(self):
        """
        Returns rich data structure for the OWL Human-in-the-Loop Dashboard.
        Includes raw rows, transformed rows (with cleansing rules applied), column metadata, and quality metrics.
        """
        self.ensure_one()
        empty = {'total_rows': 0, 'columns': [], 'raw_rows': [], 'transformed_rows': [], 'metrics': {}}
        if not self.file:
            return empty

        from ..services.cleansing_engine import CleansingEngine

        try:
            file_info = self._read_file()
            df = file_info['df'].astype(object).where(file_info['df'].notna(), '')
            sample_df = df.head(10)

            columns_meta = []
            rules_map = {}
            for col in self.column_mapping_ids:
                rules = col.cleansing_rule_ids
                rules_map[col.source_header] = rules
                target = col.target_field_id
                columns_meta.append({
                    'id': col.id,
                    'header': col.source_header,
                    'target_field': (target.field_description or target.name) if target else False,
                    'target_field_name': target.name if target else False,
                    'target_subfield': col.target_subfield or False,
                    'source': col.source or 'unresolved',
                    'confidence': round((col.confidence or 0.0) * 100, 1),
                    'is_ignored': col.is_ignored,
                    'required_level': col.required_level or False,
                    'empty_count': col.empty_count,
                    'rules_count': len(rules),
                    'rule_names': [r.name for r in rules],
                })

            raw_rows = sample_df.to_dict('records')
            transformed_rows = []
            for _idx, row in sample_df.iterrows():
                trans_row = {}
                for header in sample_df.columns:
                    val = row.get(header, '')
                    rules = rules_map.get(header, [])
                    if rules and val != '':
                        clean_val = CleansingEngine.apply_rules(val, rules)
                        trans_row[header] = {'value': clean_val, 'is_modified': str(clean_val) != str(val)}
                    else:
                        trans_row[header] = {'value': val, 'is_modified': False}
                transformed_rows.append(trans_row)

            mapped_cols = [c for c in columns_meta if c['target_field'] and not c['is_ignored']]
            confidences = [c['confidence'] for c in mapped_cols]
            avg_conf = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

            return {
                'total_rows': self.total_rows or len(df),
                'columns': columns_meta,
                'raw_headers': list(sample_df.columns),
                'raw_rows': raw_rows,
                'transformed_rows': transformed_rows,
                'metrics': {
                    'total_columns': len(columns_meta),
                    'mapped_columns': len(mapped_cols),
                    'unmapped_columns': len(columns_meta) - len(mapped_cols),
                    'duplicate_file': self.duplicate_count_file,
                    'duplicate_odoo': self.duplicate_count_odoo,
                    'avg_confidence': avg_conf,
                    'duplicate_policy': self.duplicate_policy,
                    'duplicate_criterion': self.duplicate_criterion,
                    'required_missing': self.required_missing_count,
                    'dropped_rows': file_info['dropped_rows'],
                    'sheet_name': file_info['sheet_name'],
                    'target_model': self.target_model_id.model,
                }
            }
        except Exception as e:
            _logger.exception("Error building import dashboard")
            return dict(empty, error=str(e))
