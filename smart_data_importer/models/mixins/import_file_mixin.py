from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

class ImportFileMixin(models.AbstractModel):
    _name = 'import.file.mixin'
    _description = 'Import File Management Mixin'

    file = fields.Binary(string='File', required=True, attachment=True)
    filename = fields.Char(string='Filename', required=True)
    
    encoding = fields.Char(string='Encoding', readonly=True)
    delimiter = fields.Char(string='Delimiter', readonly=True)
    header_row_index = fields.Integer(string='Header Row Index', readonly=True)
    total_rows = fields.Integer(string='Total Rows', readonly=True)

    @api.constrains('filename', 'file')
    def _check_file_validity(self):
        max_size_mb = float(self.env['ir.config_parameter'].sudo().get_param('smart_data_importer.max_file_size_mb', default='50'))
        max_size_bytes = max_size_mb * 1024 * 1024
        
        for record in self:
            if not record.filename:
                continue
            
            # Check extension
            valid_extensions = ['.csv', '.xls', '.xlsx']
            if not any(record.filename.lower().endswith(ext) for ext in valid_extensions):
                raise ValidationError(_("Only CSV and Excel files (.csv, .xls, .xlsx) are supported."))
            
            # Check size
            if record.file:
                # Odoo stores binaries in base64. Length of base64 string * 3/4 is roughly size in bytes.
                file_size = len(record.file) * 3 / 4
                if file_size > max_size_bytes:
                    raise ValidationError(_("File size exceeds the maximum limit of %s MB.") % max_size_mb)
