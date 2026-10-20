from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)

class ImportDeduplicationMixin(models.AbstractModel):
    _name = 'import.deduplication.mixin'
    _description = 'Import Deduplication & Policy Mixin'

    duplicate_criterion = fields.Selection([
        ('vat', 'Tax ID / Document (vat)'),
        ('email', 'Email Address'),
        ('vat_or_email', 'Tax ID or Email'),
        ('auto', 'Automatic (code, internal reference or name)'),
    ], string='Duplicate Criterion', default='vat', required=True,
        help="Contacts use Tax ID / Email. Other models (accounts, taxes, products...) "
             "use the first mapped key among code, internal reference or name.")
    
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
            from odoo.addons.smart_data_importer.services.deduplication_engine import DeduplicationEngine
            try:
                file_info = record._read_file()
                df = file_info['df']
                dups = DeduplicationEngine.analyze_file_duplicates(df, record)
                record.duplicate_count_file = dups
            except Exception:
                _logger.exception("Could not count duplicates in file")
                record.duplicate_count_file = 0
