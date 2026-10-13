from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)


class ImportJob(models.Model):
    _name = 'import.job'
    _description = 'Import Job Chunk'
    _order = 'create_date asc, chunk_index asc'

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
        """Import the rows [row_from, row_to) of the session sheet into the target model."""
        self.ensure_one()
        session = self.session_id
        self.write({'state': 'processing', 'attempts': self.attempts + 1})

        from ..services.cleansing_engine import CleansingEngine
        from ..services.deduplication_engine import DeduplicationEngine
        from ..services.value_converter import ValueConverter, ConversionError
        import pandas as pd

        counters = {'created': 0, 'updated': 0, 'skipped': 0, 'failed': 0, 'dups': 0}
        try:
            model_name = session.target_model_id.model
            Model = self.env[model_name]
            converter = ValueConverter(self.env, model_name)

            df = session._read_file()['df']
            chunk_df = df.iloc[self.row_from:self.row_to]

            mappings = session.column_mapping_ids.filtered(lambda m: not m.is_ignored and m.target_field_id)
            plan = [(m.source_header, m.target_field_id.name, m.target_subfield or None, m.cleansing_rule_ids)
                    for m in mappings]
            policy = session.duplicate_policy or 'update'
            criterion = session.duplicate_criterion or 'auto'

            for row_number, row in chunk_df.iterrows():
                row_dict, errors = {}, []
                for header, field_name, subfield, rules in plan:
                    val = row.get(header)
                    if val is None or pd.isna(val):
                        continue
                    if rules:
                        val = CleansingEngine.apply_rules(val, rules)
                    try:
                        converted = converter.convert(field_name, val, subfield=subfield)
                    except ConversionError as e:
                        errors.append(str(e))
                        continue
                    if converted is not False or self.env[model_name]._fields[field_name].type == 'boolean':
                        row_dict[field_name] = converted

                if not row_dict and not errors:
                    counters['skipped'] += 1
                    continue
                if errors:
                    counters['failed'] += 1
                    self._log_row_error(row_number, row, 'ConversionError', '\n'.join(errors))
                    continue

                try:
                    with self.env.cr.savepoint():
                        existing = DeduplicationEngine.find_existing(self.env, model_name, row_dict, criterion)
                        if existing:
                            counters['dups'] += 1
                            if policy == 'update':
                                existing.write(row_dict)
                                counters['updated'] += 1
                            elif policy == 'skip':
                                counters['skipped'] += 1
                            else:  # create
                                Model.create(row_dict)
                                counters['created'] += 1
                        else:
                            Model.create(row_dict)
                            counters['created'] += 1
                except Exception as e:
                    counters['failed'] += 1
                    _logger.warning("Error importing row %s in job %s: %s", row_number, self.id, e)
                    self._log_row_error(row_number, row, type(e).__name__, str(e))

            self.state = 'done'
        except Exception as e:
            _logger.exception("Import job %s failed", self.id)
            self.write({'state': 'error', 'error_message': str(e)})

        handled = counters['created'] + counters['updated'] + counters['skipped'] + counters['failed']
        session.sudo().write({
            'processed_rows': session.processed_rows + handled,
            'created_rows': session.created_rows + counters['created'],
            'updated_rows': session.updated_rows + counters['updated'],
            'skipped_rows': session.skipped_rows + counters['skipped'],
            'failed_rows': session.failed_rows + counters['failed'],
            'duplicate_count_odoo': session.duplicate_count_odoo + counters['dups'],
        })
        self._check_session_completion()

    def _log_row_error(self, row_number, row, error_type, message):
        raw = {k: v for k, v in row.to_dict().items() if v is not None and str(v) != 'nan'}
        self.env['import.row.error'].create({
            'job_id': self.id,
            'row_number': int(row_number),  # real spreadsheet row (index kept by FileReader)
            'raw_data': str(raw),
            'error_type': error_type,
            'error_message': message,
        })

    def _check_session_completion(self):
        """When no job is pending, close the session and stamp the end time."""
        session = self.session_id
        pending_jobs = self.search_count([
            ('session_id', '=', session.id),
            ('state', 'in', ['pending', 'processing'])
        ])
        if pending_jobs == 0:
            all_failed = all(j.state == 'error' for j in session.job_ids)
            session.sudo().write({
                'state': 'error' if all_failed else 'done',
                'end_time': fields.Datetime.now(),
            })
