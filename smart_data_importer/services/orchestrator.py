from odoo.addons.smart_data_importer.services.field_catalog import split_header
from odoo.addons.smart_data_importer.services.file_reader import FileReader
from odoo.addons.smart_data_importer.services.mapper_hash import HashMapper
from odoo.addons.smart_data_importer.services.mapper_fuzzy import FuzzyMapper
from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper
from odoo.exceptions import UserError
from odoo import _

# Field name -> cleansing rule action applied by default
DEFAULT_RULES_BY_FIELD = {
    'email': 'email',
    'email_edi': 'email',
    'phone': 'phone',
    'mobile': 'phone',
    'vat': 'vat_document',
}


class PipelineOrchestrator:

    @classmethod
    def run_premapping(cls, session):
        """
        Coordinates reading -> template match -> hash -> fuzzy -> llm.
        Creates import.column.mapping records for the session's sheet and target model.
        """
        try:
            file_info = session._read_file()
        except UserError:
            raise
        except Exception as e:
            raise UserError(_("Error reading file: %s") % str(e))

        df = file_info['df']
        model_name = session.target_model_id.model
        session.write({
            'encoding': file_info['encoding'],
            'delimiter': file_info['delimiter'],
            'header_row_index': file_info['header_row_index'],
            'total_rows': file_info['total_rows'],
            'state': 'analyzing',
        })
        session.column_mapping_ids.unlink()

        headers = list(df.columns)
        if not headers:
            return

        sample_values_map = cls.sample_values(df)

        # 1. Reusable template with the same header signature (US-26)
        matching_template = session.env['import.template'].find_matching_template(headers, model_name=model_name)
        if matching_template:
            session.write({
                'template_id': matching_template.id,
                'duplicate_criterion': matching_template.duplicate_criterion,
                'duplicate_policy': matching_template.duplicate_policy,
            })
            template_lines = {FileReader.normalize_header(l.source_header): l for l in matching_template.line_ids}
            resolved = {}
            for header in headers:
                t_line = template_lines.get(FileReader.normalize_header(header))
                if not t_line:
                    continue
                resolved[header] = {
                    'target_field_id': t_line.target_field_id.id or False,
                    'target_subfield': t_line.target_subfield or False,
                    'is_ignored': t_line.is_ignored,
                    'cleansing_rule_ids': [(6, 0, t_line.cleansing_rule_ids.ids)],
                    'source': 'template',
                    'confidence': 1.0,
                }
            cls.create_mappings(session, df, file_info['column_meta'], sample_values_map, resolved, auto_rules=False)
            session.state = 'mapped'
            return

        # 2. Headers pointing to sub-fields of lines (bank_ids/acc_number, seller_ids/price...)
        resolved = {}
        for header in headers:
            base, sub = split_header(header)
            if sub and sub != 'id':
                resolved[header] = {
                    'is_ignored': True,
                    'source': 'manual',
                    'llm_justification': _(
                        "Sub-field of related lines (%s). Not supported yet: import those lines separately."
                    ) % header,
                }
        pending = [h for h in headers if h not in resolved]

        # 3. Hash (technical names + synonyms)
        hash_results = HashMapper.map(session.env, pending, model_name=model_name)
        resolved.update(hash_results)
        pending = [h for h in pending if h not in hash_results]

        # 4. Fuzzy
        config = session.env['ir.config_parameter'].sudo()
        threshold = int(config.get_param('smart_data_importer.fuzzy_threshold', default='85'))
        fuzzy_results = FuzzyMapper.map(session.env, pending, model_name=model_name, threshold=threshold)
        resolved.update(fuzzy_results)
        pending = [h for h in pending if h not in fuzzy_results]

        # 5. LLM (only what is still unresolved)
        llm_results = LLMMapper.map(session.env, pending, sample_values_map, model_name=model_name)
        resolved.update(llm_results)

        cls.create_mappings(session, df, file_info['column_meta'], sample_values_map, resolved)
        session.state = 'mapped'

    # ------------------------------------------------------------------
    # Shared helpers (also used by the AI template flow)
    # ------------------------------------------------------------------

    @classmethod
    def sample_values(cls, df, limit=5):
        return {
            header: [str(x) for x in df[header].dropna().head(limit).tolist()]
            for header in df.columns
        }

    @classmethod
    def create_mappings(cls, session, df, column_meta, sample_values_map, resolved, auto_rules=True):
        """Create one import.column.mapping per column of the sheet.

        :param resolved: {header: vals} with the mapping found for each header;
            headers missing from it are created unmapped so the user can fix them.
        """
        env = session.env
        rules_cache = {}

        def rule(action):
            if action not in rules_cache:
                rules_cache[action] = env['import.cleansing.rule'].search(
                    [('action_type', '=', action), ('active', '=', True)], limit=1)
            return rules_cache[action]

        mapping_vals = []
        assigned_fields = {}
        for header in df.columns:
            column = df[header]
            meta = column_meta.get(header) or {}
            val = {
                'session_id': session.id,
                'source_header': header,
                'source_header_normalized': FileReader.normalize_header(header),
                'sample_values': ', '.join(sample_values_map.get(header, [])[:3]),
                'required_level': meta.get('required_level') or False,
                'empty_count': int(column.isna().sum()),
            }
            val.update(resolved.get(header, {}))

            target_id = val.get('target_field_id')
            if target_id and not val.get('is_ignored'):
                assigned_fields.setdefault(target_id, []).append(header)

                if auto_rules and 'cleansing_rule_ids' not in val:
                    field_rec = env['ir.model.fields'].browse(target_id)
                    rule_ids = []
                    if field_rec.ttype in ('char', 'text', 'many2one', 'many2many', 'selection'):
                        rule_ids += rule('strip').ids
                    special = DEFAULT_RULES_BY_FIELD.get(field_rec.name)
                    if special:
                        rule_ids += rule(special).ids
                    if rule_ids:
                        val['cleansing_rule_ids'] = [(6, 0, rule_ids)]
            mapping_vals.append(val)

        for val in mapping_vals:
            target_id = val.get('target_field_id')
            if target_id and not val.get('is_ignored') and len(assigned_fields.get(target_id, [])) > 1:
                val['is_conflict'] = True

        return env['import.column.mapping'].create(mapping_vals)
