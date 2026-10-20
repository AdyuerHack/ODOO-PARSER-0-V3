from odoo import models, fields, api

REQUIRED_LEVELS = [
    ('required', 'Required'),
    ('conditional', 'Conditional'),
    ('optional', 'Optional'),
]

# Field name -> cleansing rule suggested when the user picks it manually
DEFAULT_RULES_BY_FIELD = {
    'email': 'email',
    'email_edi': 'email',
    'phone': 'phone',
    'mobile': 'phone',
    'vat': 'vat_document',
}


class ImportColumnMapping(models.Model):
    _name = 'import.column.mapping'
    _description = 'Import Column Mapping'

    session_id = fields.Many2one('import.session', string='Session', required=True, ondelete='cascade')
    source_header = fields.Char(string='Original Header', required=True)
    source_header_normalized = fields.Char(string='Normalized Header', required=True)
    sample_values = fields.Char(string='Sample Values')

    # The domain is set in the view from the session's target model
    target_field_id = fields.Many2one('ir.model.fields', string='Target Field', ondelete='cascade')
    target_field_name = fields.Char(related='target_field_id.name', string='Target Field Name', store=True, readonly=True)
    target_subfield = fields.Char(
        string='Sub-field',
        help="'id' means the cells hold external identifiers (e.g. base.state_co_01) instead of names.")

    confidence = fields.Float(string='Confidence Score', help="From 0.0 to 1.0")
    llm_justification = fields.Text(string='AI Justification', help="Explanation provided by the LLM for this suggested mapping.")
    source = fields.Selection([
        ('template', 'Template'),
        ('hash', 'Hash'),
        ('fuzzy', 'Fuzzy'),
        ('llm', 'LLM'),
        ('manual', 'Manual')
    ], string='Source')

    required_level = fields.Selection(REQUIRED_LEVELS, string='Required',
                                      help="Read from the header colour of the base template.")
    empty_count = fields.Integer(string='Empty cells')

    is_ignored = fields.Boolean(string='Ignore', default=False)
    is_conflict = fields.Boolean(string='Conflict', help="Target field is assigned to multiple columns", default=False)
    cleansing_rule_ids = fields.Many2many('import.cleansing.rule', string='Cleansing Rules')

    @api.onchange('target_field_id')
    def _onchange_target_field_id(self):
        if not self.target_field_id:
            return
        self.source = 'manual'
        Rule = self.env['import.cleansing.rule']
        rules = Rule
        if self.target_field_id.ttype in ('char', 'text', 'many2one', 'many2many', 'selection'):
            rules |= Rule.search([('action_type', '=', 'strip')], limit=1)
        special = DEFAULT_RULES_BY_FIELD.get(self.target_field_id.name)
        if special:
            rules |= Rule.search([('action_type', '=', special)], limit=1)
        self.cleansing_rule_ids = [(6, 0, rules.ids)]
