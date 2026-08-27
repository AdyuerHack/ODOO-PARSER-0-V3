from odoo import models, fields, api

class ImportColumnMapping(models.Model):
    _name = 'import.column.mapping'
    _description = 'Import Column Mapping'

    session_id = fields.Many2one('import.session', string='Session', required=True, ondelete='cascade')
    source_header = fields.Char(string='Original Header', required=True)
    source_header_normalized = fields.Char(string='Normalized Header', required=True)
    sample_values = fields.Char(string='Sample Values')
    
    target_field_id = fields.Many2one('ir.model.fields', string='Target Field', domain="[('model_id.model', '=', 'res.partner')]")
    
    confidence = fields.Float(string='Confidence Score', help="From 0.0 to 1.0")
    source = fields.Selection([
        ('template', 'Template'),
        ('hash', 'Hash'),
        ('fuzzy', 'Fuzzy'),
        ('llm', 'LLM'),
        ('manual', 'Manual')
    ], string='Source')
    
    is_ignored = fields.Boolean(string='Ignore', default=False)
    is_conflict = fields.Boolean(string='Conflict', help="Target field is assigned to multiple columns", default=False)
    cleansing_rule_ids = fields.Many2many('import.cleansing.rule', string='Cleansing Rules')

    @api.onchange('target_field_id')
    def _onchange_target_field_id(self):
        if not self.target_field_id:
            return
        field_name = self.target_field_id.name
        rules = self.env['import.cleansing.rule']
        if field_name in ('email',):
            rules |= self.env['import.cleansing.rule'].search([('action_type', '=', 'email')], limit=1)
        elif field_name in ('phone', 'mobile'):
            rules |= self.env['import.cleansing.rule'].search([('action_type', '=', 'phone')], limit=1)
        elif field_name in ('vat',):
            rules |= self.env['import.cleansing.rule'].search([('action_type', '=', 'vat_document')], limit=1)
        elif field_name in ('name', 'street', 'street2', 'city'):
            rules |= self.env['import.cleansing.rule'].search([('action_type', '=', 'strip')], limit=1)
        
        if rules:
            self.cleansing_rule_ids = [(6, 0, rules.ids)]
