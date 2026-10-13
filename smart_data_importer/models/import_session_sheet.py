from odoo import models, fields, api


class ImportSessionSheet(models.Model):
    _name = 'import.session.sheet'
    _description = 'Sheet found in an uploaded workbook'
    _order = 'sequence, id'

    session_id = fields.Many2one('import.session', string='Session', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Sheet', required=True)
    kind = fields.Selection([
        ('data', 'Data'),
        ('instructions', 'Instructions'),
        ('values', 'Possible values (lookup lists)'),
        ('empty', 'Empty'),
    ], string='Type', required=True, default='data')
    visible = fields.Boolean(string='Visible', default=True)
    row_count = fields.Integer(string='Rows')
    column_count = fields.Integer(string='Columns')
    dropped_rows = fields.Integer(string='Discarded empty rows',
                                  help="Rows without real data (empty, only template defaults or flag rows).")
    header_list = fields.Text(string='Headers')

    @api.depends('name', 'kind', 'row_count')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = "%s (%s filas)" % (rec.name, rec.row_count) if rec.kind == 'data' else rec.name
