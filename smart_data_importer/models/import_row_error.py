from odoo import models, fields

class ImportRowError(models.Model):
    _name = 'import.row.error'
    _description = 'Import Row Error'
    _order = 'create_date asc'

    job_id = fields.Many2one('import.job', string='Job', required=True, ondelete='cascade')
    session_id = fields.Many2one('import.session', related='job_id.session_id', store=True, string='Session')
    row_number = fields.Integer(string='Row Number')
    raw_data = fields.Text(string='Raw Data')
    error_type = fields.Char(string='Error Type')
    error_message = fields.Text(string='Error Message')
