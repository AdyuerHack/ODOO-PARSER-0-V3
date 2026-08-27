import hashlib
from odoo import models, fields, api, _
from ...services.file_reader import FileReader

class ImportSignatureMixin(models.AbstractModel):
    _name = 'import.signature.mixin'
    _description = 'Import Header Signature & Structure Mixin'

    header_signature = fields.Char(string='Header Signature (Hash)', readonly=True, index=True)
    header_list = fields.Text(string='Header Columns', readonly=True)

    @api.model
    def compute_header_signature(self, headers):
        """
        Generates a deterministic SHA256 signature from a list of raw column headers.
        Normalizes and sorts headers so column order in file doesn't break matching.
        """
        if not headers:
            return False
        normalized = sorted([FileReader.normalize_header(h) for h in headers if h])
        sig_str = '|'.join(normalized)
        return hashlib.sha256(sig_str.encode('utf-8')).hexdigest()

    @api.model
    def format_header_list(self, headers):
        """Returns clean comma-separated list of headers."""
        if not headers:
            return ''
        return ', '.join([str(h).strip() for h in headers if h])
