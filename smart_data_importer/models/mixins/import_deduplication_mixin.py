from odoo import models, fields, api, _

class ImportDeduplicationMixin(models.AbstractModel):
    _name = 'import.deduplication.mixin'
    _description = 'Import Deduplication & Policy Mixin'

    duplicate_criterion = fields.Selection([
        ('vat', 'Tax ID / Document (vat)'),
        ('email', 'Email Address'),
        ('vat_or_email', 'Tax ID or Email')
    ], string='Duplicate Criterion', default='vat', required=True)
    
    duplicate_policy = fields.Selection([
        ('update', 'Update Existing Records'),
        ('skip', 'Skip Duplicates'),
        ('create', 'Create Duplicate Records')
    ], string='Duplicate Policy', default='update', required=True)

    duplicate_count_file = fields.Integer(string='Duplicates in File', readonly=True, default=0)
    duplicate_count_odoo = fields.Integer(string='Matched in Odoo', readonly=True, default=0)

    def _compute_file_duplicates(self):
        """Analyzes internal duplicates in file using DeduplicationEngine."""
        for record in self:
            if not getattr(record, 'file', None):
                record.duplicate_count_file = 0
                continue
            from ...services.file_reader import FileReader
            from ...services.deduplication_engine import DeduplicationEngine
            try:
                file_info = FileReader.read(record.file, record.filename)
                df = file_info['df']
                dups = DeduplicationEngine.analyze_file_duplicates(df, record)
                record.duplicate_count_file = dups
            except Exception:
                record.duplicate_count_file = 0
