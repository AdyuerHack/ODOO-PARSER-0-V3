from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)

class ImportJob(models.Model):
    _name = 'import.job'
    _description = 'Import Job Chunk'
    _order = 'create_date asc'

    session_id = fields.Many2one('import.session', string='Session', required=True, ondelete='cascade')
    chunk_index = fields.Integer(string='Chunk Index', required=True)
    row_from = fields.Integer(string='Row From', required=True)
    row_to = fields.Integer(string='Row To', required=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error')
    ], string='Status', default='pending', required=True)
    attempts = fields.Integer(string='Attempts', default=0)
    error_message = fields.Text(string='Error Message')
    row_error_ids = fields.One2many('import.row.error', 'job_id', string='Row Errors')
    
    def process_chunk(self):
        """Method executed by queue_job to process this chunk of rows."""
        self.ensure_one()
        self.state = 'processing'
        self.attempts += 1
        
        try:
            from ..services.file_reader import FileReader
            file_info = FileReader.read(self.session_id.file, self.session_id.filename)
            df = file_info['df']
            
            # Slice the df for this chunk
            chunk_df = df.iloc[self.row_from:self.row_to]
            
            # Get validated mappings from session
            mappings = self.session_id.column_mapping_ids.filtered(lambda m: not m.is_ignored and m.target_field_id)
            mapping_rules = {m.source_header: (m.target_field_id.name, m.cleansing_rule_ids) for m in mappings}
            
            from ..services.cleansing_engine import CleansingEngine
            from ..services.deduplication_engine import DeduplicationEngine
            import pandas as pd
            
            created_count = 0
            updated_count = 0
            skipped_count = 0
            fail_count = 0
            dups_odoo_count = 0
            
            policy = self.session_id.duplicate_policy or 'update'
            criterion = self.session_id.duplicate_criterion or 'vat'
            
            for index, row in chunk_df.iterrows():
                row_dict = {}
                for header, (target_field, rules) in mapping_rules.items():
                    val = row.get(header)
                    if pd.isna(val):
                        continue
                    if rules:
                        val = CleansingEngine.apply_rules(val, rules)
                    row_dict[target_field] = val
                    
                if not row_dict:
                    continue
                    
                # Use savepoint to isolate record failures
                try:
                    with self.env.cr.savepoint():
                        partner = DeduplicationEngine.find_existing_partner(self.env, row_dict, criterion=criterion)
                        if partner:
                            dups_odoo_count += 1
                            if policy == 'update':
                                partner.write(row_dict)
                                updated_count += 1
                            elif policy == 'skip':
                                skipped_count += 1
                            elif policy == 'create':
                                self.env['res.partner'].create(row_dict)
                                created_count += 1
                        else:
                            self.env['res.partner'].create(row_dict)
                            created_count += 1
                except Exception as e:
                    fail_count += 1
                    _logger.warning("Error importing row %s in job %s: %s", index, self.id, str(e))
                    self.env['import.row.error'].create({
                        'job_id': self.id,
                        'row_number': int(index) + 1, # +1 for 1-based indexing in reporting
                        'raw_data': str(row.to_dict()),
                        'error_type': type(e).__name__,
                        'error_message': str(e)
                    })

            # Update session progress counters
            self.session_id.sudo().write({
                'processed_rows': self.session_id.processed_rows + created_count + updated_count + skipped_count,
                'updated_rows': self.session_id.updated_rows + updated_count,
                'skipped_rows': self.session_id.skipped_rows + skipped_count,
                'failed_rows': self.session_id.failed_rows + fail_count,
                'duplicate_count_odoo': self.session_id.duplicate_count_odoo + dups_odoo_count,
            })
                    
            self.state = 'done'
        except Exception as e:
            self.state = 'error'
            self.error_message = str(e)
            
        self._check_session_completion()

    def _check_session_completion(self):
        """Check if all jobs are done and update session state."""
        pending_jobs = self.env['import.job'].search_count([
            ('session_id', '=', self.session_id.id),
            ('state', 'in', ['pending', 'processing'])
        ])
        if pending_jobs == 0:
            self.session_id.state = 'done'
