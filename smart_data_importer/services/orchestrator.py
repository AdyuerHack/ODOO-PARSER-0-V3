from .file_reader import FileReader
from .mapper_hash import HashMapper
from .mapper_fuzzy import FuzzyMapper
from odoo.exceptions import UserError
from odoo import _

class PipelineOrchestrator:
    
    @classmethod
    def run_premapping(cls, session):
        """
        Coordinates reading -> header detection -> hash -> fuzzy.
        Creates import.column.mapping records.
        """
        # 1. Read file and extract headers/metadata
        try:
            file_info = FileReader.read(session.file, session.filename)
        except Exception as e:
            raise UserError(_("Error reading file: %s") % str(e))
            
        df = file_info['df']
        
        # Save metadata to session
        session.write({
            'encoding': file_info['encoding'],
            'delimiter': file_info['delimiter'],
            'header_row_index': file_info['header_row_index'],
            'total_rows': file_info['total_rows'],
            'state': 'analyzing'
        })
        
        # Clear existing mappings
        session.column_mapping_ids.unlink()
        
        # Ensure we have columns
        headers = list(df.columns)
        if not headers:
            return
            
        # 1.5. Check for Matching Reusable Template (US-26)
        matching_template = session.env['import.template'].find_matching_template(headers, model_name='res.partner')
        if matching_template:
            session.write({
                'template_id': matching_template.id,
                'duplicate_criterion': matching_template.duplicate_criterion,
                'duplicate_policy': matching_template.duplicate_policy,
            })
            template_lines = {l.source_header.strip().lower(): l for l in matching_template.line_ids}
            mapping_vals = []
            for header in headers:
                norm_h = header.strip().lower()
                val = {
                    'session_id': session.id,
                    'source_header': header,
                    'source_header_normalized': FileReader.normalize_header(header),
                    'sample_values': ', '.join([str(x) for x in df[header].dropna().head(3).tolist()]),
                    'source': 'template',
                    'confidence': 1.0,
                }
                if norm_h in template_lines:
                    t_line = template_lines[norm_h]
                    val['target_field_id'] = t_line.target_field_id.id if t_line.target_field_id else False
                    val['is_ignored'] = t_line.is_ignored
                    if t_line.cleansing_rule_ids:
                        val['cleansing_rule_ids'] = [(6, 0, t_line.cleansing_rule_ids.ids)]
                mapping_vals.append(val)
            
            session.env['import.column.mapping'].create(mapping_vals)
            session.state = 'mapped'
            return

        # 2. Hash Mapping
        hash_results = HashMapper.map(session.env, headers, model_name='res.partner')
        
        # Find unresolved headers
        unresolved_headers = [h for h in headers if h not in hash_results]
        
        # 3. Fuzzy Mapping
        # Get threshold from config
        threshold = int(session.env['ir.config_parameter'].sudo().get_param('smart_data_importer.fuzzy_threshold', default='85'))
        fuzzy_results = FuzzyMapper.map(session.env, unresolved_headers, model_name='res.partner', threshold=threshold)
        
        # 4. Build mapping records
        mapping_vals = []
        assigned_fields = {} # target_field_id -> list of headers
        
        for header in headers:
            val = {
                'session_id': session.id,
                'source_header': header,
                'source_header_normalized': FileReader.normalize_header(header),
                # sample_values: grab first 3 non-null values
                'sample_values': ', '.join([str(x) for x in df[header].dropna().head(3).tolist()])
            }
            
            if header in hash_results:
                val.update(hash_results[header])
            elif header in fuzzy_results:
                val.update(fuzzy_results[header])
                
            target_id = val.get('target_field_id')
            if target_id:
                if target_id not in assigned_fields:
                    assigned_fields[target_id] = []
                assigned_fields[target_id].append(header)
                
                # Auto-assign sensible cleansing rules
                target_field_rec = session.env['ir.model.fields'].browse(target_id)
                f_name = target_field_rec.name
                rule_action = None
                if f_name in ('email',):
                    rule_action = 'email'
                elif f_name in ('phone', 'mobile'):
                    rule_action = 'phone'
                elif f_name in ('vat',):
                    rule_action = 'vat_document'
                elif f_name in ('name', 'street', 'street2', 'city'):
                    rule_action = 'strip'
                
                if rule_action:
                    rule_rec = session.env['import.cleansing.rule'].search([('action_type', '=', rule_action)], limit=1)
                    if rule_rec:
                        val['cleansing_rule_ids'] = [(6, 0, rule_rec.ids)]
                
            mapping_vals.append(val)
            
        # 5. Detect conflicts
        for val in mapping_vals:
            target_id = val.get('target_field_id')
            if target_id and len(assigned_fields[target_id]) > 1:
                val['is_conflict'] = True
                
        # 6. Create records and update state
        session.env['import.column.mapping'].create(mapping_vals)
        session.state = 'mapped'
