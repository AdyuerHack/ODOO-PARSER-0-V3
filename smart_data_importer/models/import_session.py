from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import base64

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
    
    column_mapping_ids = fields.One2many('import.column.mapping', 'session_id', string='Column Mappings')
    job_ids = fields.One2many('import.job', 'session_id', string='Jobs')
    template_id = fields.Many2one('import.template', string='Applied Template')

    @api.onchange('template_id')
    def _onchange_template_id(self):
        if self.template_id and self.column_mapping_ids:
            template = self.template_id
            template_lines_by_header = {line.source_header.strip().lower(): line for line in template.line_ids}
            for col in self.column_mapping_ids:
                norm_header = col.source_header.strip().lower()
                if norm_header in template_lines_by_header:
                    t_line = template_lines_by_header[norm_header]
                    col.target_field_id = t_line.target_field_id.id if t_line.target_field_id else False
                    col.cleansing_rule_ids = [(6, 0, t_line.cleansing_rule_ids.ids)]
                    col.is_ignored = t_line.is_ignored
                    col.source = 'template'
                    col.confidence = 1.0
                    col.is_conflict = False

            if template.duplicate_criterion:
                self.duplicate_criterion = template.duplicate_criterion
            if template.duplicate_policy:
                self.duplicate_policy = template.duplicate_policy

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('import.session') or _('New')
        return super().create(vals_list)

    def action_analyze(self):
        self.ensure_one()
        from ..services.orchestrator import PipelineOrchestrator
        PipelineOrchestrator.run_premapping(self)
        self._compute_file_duplicates()
        return True

    def action_save_as_template(self):
        self.ensure_one()
        if not self.column_mapping_ids:
            raise ValidationError(_("No column mappings available to save as template."))

        headers = [m.source_header for m in self.column_mapping_ids]
        signature = self.env['import.template'].compute_header_signature(headers)
        
        # Build lines
        lines_vals = []
        for m in self.column_mapping_ids:
            lines_vals.append((0, 0, {
                'source_header': m.source_header,
                'target_field_id': m.target_field_id.id if m.target_field_id else False,
                'cleansing_rule_ids': [(6, 0, m.cleansing_rule_ids.ids)],
                'is_ignored': m.is_ignored,
            }))

        template_name = _("Template for %s") % (self.filename or self.name)
        
        template = self.env['import.template'].create({
            'name': template_name,
            'header_signature': signature,
            'header_list': ', '.join(headers),
            'duplicate_criterion': self.duplicate_criterion,
            'duplicate_policy': self.duplicate_policy,
            'line_ids': lines_vals,
        })
        
        self.template_id = template.id
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Template Saved'),
                'message': _("Mapping successfully saved as template '%s'.") % template.name,
                'type': 'success',
                'sticky': False,
            }
        }

    def action_apply_template(self):
        self.ensure_one()
        if not self.template_id:
            raise ValidationError(_("Please select a template to apply."))
        
        template = self.template_id
        template_lines_by_header = {line.source_header.strip().lower(): line for line in template.line_ids}
        
        for col in self.column_mapping_ids:
            norm_header = col.source_header.strip().lower()
            if norm_header in template_lines_by_header:
                t_line = template_lines_by_header[norm_header]
                col.write({
                    'target_field_id': t_line.target_field_id.id if t_line.target_field_id else False,
                    'cleansing_rule_ids': [(6, 0, t_line.cleansing_rule_ids.ids)],
                    'is_ignored': t_line.is_ignored,
                    'source': 'template',
                    'confidence': 1.0,
                    'is_conflict': False
                })

        if template.duplicate_criterion:
            self.duplicate_criterion = template.duplicate_criterion
        if template.duplicate_policy:
            self.duplicate_policy = template.duplicate_policy

        self._compute_file_duplicates()
        return True

    def action_confirm_mapping(self):
        self.ensure_one()
        if self.state != 'mapped':
            raise ValidationError(_("Cannot confirm mapping in current state."))
            
        chunk_size = int(self.env['ir.config_parameter'].sudo().get_param('smart_data_importer.chunk_size', default='500'))
        
        self.start_time = fields.Datetime.now()
        self.state = 'queued'
        
        job_vals = []
        # Calculate chunks
        for i in range(0, self.total_rows, chunk_size):
            job_vals.append({
                'session_id': self.id,
                'chunk_index': i // chunk_size,
                'row_from': i,
                'row_to': min(i + chunk_size, self.total_rows),
                'state': 'pending',
            })
            
        if job_vals:
            jobs = self.env['import.job'].create(job_vals)
            for job in jobs:
                job.with_delay(description=f"Import Session {self.name} - Chunk {job.chunk_index}").process_chunk()
        else:
            self.state = 'done'
            self.end_time = fields.Datetime.now()
        return True

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
        if not self.file:
            return {
                'total_rows': 0,
                'columns': [],
                'raw_rows': [],
                'transformed_rows': [],
                'metrics': {}
            }

        from ..services.file_reader import FileReader
        from ..services.cleansing_engine import CleansingEngine
        import pandas as pd

        try:
            file_info = FileReader.read(self.file, self.filename)
            df = file_info['df'].fillna('')
            sample_df = df.head(10)

            # Build column metadata
            columns_meta = []
            rules_map = {}
            for col in self.column_mapping_ids:
                rules = col.cleansing_rule_ids
                rules_map[col.source_header] = rules
                columns_meta.append({
                    'id': col.id,
                    'header': col.source_header,
                    'target_field': col.target_field_id.field_description or col.target_field_id.name if col.target_field_id else False,
                    'target_field_name': col.target_field_id.name if col.target_field_id else False,
                    'source': col.source or 'unresolved',
                    'confidence': round((col.confidence or 0.0) * 100, 1),
                    'is_ignored': col.is_ignored,
                    'rules_count': len(rules),
                    'rule_names': [r.name for r in rules]
                })

            raw_rows = sample_df.to_dict('records')

            # Generate Transformed Preview
            transformed_rows = []
            for _, row in sample_df.iterrows():
                trans_row = {}
                for header in sample_df.columns:
                    val = row.get(header, '')
                    rules = rules_map.get(header, [])
                    if rules and val != '':
                        clean_val = CleansingEngine.apply_rules(val, rules)
                        trans_row[header] = {
                            'value': clean_val,
                            'is_modified': str(clean_val) != str(val)
                        }
                    else:
                        trans_row[header] = {
                            'value': val,
                            'is_modified': False
                        }
                transformed_rows.append(trans_row)

            # Metrics
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
                    'duplicate_criterion': self.duplicate_criterion
                }
            }
        except Exception as e:
            return {
                'error': str(e),
                'columns': [],
                'raw_rows': [],
                'transformed_rows': [],
                'metrics': {}
            }


