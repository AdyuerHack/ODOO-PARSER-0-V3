from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)

class ImportCleansingRule(models.Model):
    _name = 'import.cleansing.rule'
    _description = 'Data Cleansing & Normalization Rule'
    _order = 'sequence, id'

    name = fields.Char(string='Rule Name', required=True, translate=True)
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    description = fields.Text(string='Description')

    category = fields.Selection([
        ('text', 'Text & Formatting'),
        ('contact', 'Contacts & Tax IDs'),
        ('number', 'Numbers & Currency'),
        ('date', 'Dates & Times'),
        ('custom', 'Custom / Advanced')
    ], string='Category', default='text', required=True)

    action_type = fields.Selection([
        # Text & Formatting
        ('strip', 'Trim Whitespace'),
        ('lowercase', 'Lowercase'),
        ('uppercase', 'Uppercase'),
        ('titlecase', 'Capitalize Words (Title Case)'),
        ('remove_accents', 'Remove Accents / Diacritics'),
        ('digits_only', 'Keep Digits Only'),
        ('find_replace', 'Find & Replace Text'),
        ('prefix_suffix', 'Add/Remove Prefix & Suffix'),
        ('value_mapping', 'Value Mapping (Dictionary)'),
        ('default_value', 'Default Value Fallback'),
        
        # Contacts & Tax IDs
        ('email', 'Normalize Email'),
        ('phone', 'Normalize Phone Number'),
        ('vat_document', 'Normalize Tax ID / Document'),
        
        # Numbers & Dates
        ('number_clean', 'Clean Currency / Number'),
        ('date_format', 'Normalize Date Format'),
        
        # Custom / Advanced
        ('regex', 'Custom Regex Replacement'),
        ('python_code', 'Python Code (Safe Eval)')
    ], string='Action Type', required=True, default='strip')

    # Specific Configuration Fields
    # Find & Replace
    find_text = fields.Char(string='Find Text')
    replace_text = fields.Char(string='Replace With', default='')
    case_sensitive = fields.Boolean(string='Case Sensitive', default=False)

    # Prefix & Suffix
    prefix_text = fields.Char(string='Prefix')
    suffix_text = fields.Char(string='Suffix')
    remove_prefix = fields.Boolean(string='Remove Prefix if exists', default=False)
    remove_suffix = fields.Boolean(string='Remove Suffix if exists', default=False)

    # Value Mapping
    line_ids = fields.One2many('import.cleansing.rule.line', 'rule_id', string='Value Mapping Table')

    # Default Value
    default_value = fields.Char(string='Default Value if Empty')

    # Number / Currency Clean
    decimal_separator = fields.Selection([
        ('dot', 'Dot as Decimal (e.g. 1,250.50 -> 1250.50)'),
        ('comma', 'Comma as Decimal (e.g. 1.250,50 -> 1250.50)')
    ], string='Decimal Notation', default='comma')

    # Date Format
    date_input_format = fields.Selection([
        ('auto', 'Auto-detect Format'),
        ('%d/%m/%Y', 'DD/MM/YYYY (e.g. 31/12/2025)'),
        ('%m/%d/%Y', 'MM/DD/YYYY (e.g. 12/31/2025)'),
        ('%Y-%m-%d', 'YYYY-MM-DD (e.g. 2025-12-31)'),
        ('%Y/%m/%d', 'YYYY/MM/DD (e.g. 2025/12/31)'),
        ('%d-%m-%Y', 'DD-MM-YYYY (e.g. 31-12-2025)'),
        ('%Y%m%d', 'YYYYMMDD (e.g. 20251231)')
    ], string='Input Date Format', default='auto')

    # Regex
    regex_pattern = fields.Char(string='Regex Pattern', help='Regular expression pattern to match.')
    regex_replacement = fields.Char(string='Regex Replacement', default='', help='Text to replace matches with.')

    # Python Code
    python_code = fields.Text(
        string='Python Code',
        default="# Available variables: 'value' (input string), 'rule' (rule record)\n# Store output in 'result'\nresult = value.strip()"
    )

    # Live Sandbox / Interactive Tester
    test_input = fields.Char(string='Sample Input', default='  Example Text 123  ')
    test_output = fields.Char(string='Transformed Output', compute='_compute_test_output', readonly=True)

    @api.depends('test_input', 'action_type', 'find_text', 'replace_text', 'case_sensitive',
                 'prefix_text', 'suffix_text', 'remove_prefix', 'remove_suffix',
                 'default_value', 'decimal_separator', 'date_input_format',
                 'regex_pattern', 'regex_replacement', 'python_code', 'line_ids.source_value', 'line_ids.target_value')
    def _compute_test_output(self):
        for rec in self:
            if rec.test_input is not False and rec.test_input is not None:
                from ..services.cleansing_engine import CleansingEngine
                rec.test_output = CleansingEngine.apply_rules(rec.test_input, [rec])
            else:
                rec.test_output = ''

    @api.onchange('test_input', 'action_type', 'find_text', 'replace_text', 'case_sensitive',
                  'prefix_text', 'suffix_text', 'remove_prefix', 'remove_suffix',
                  'default_value', 'decimal_separator', 'date_input_format',
                  'regex_pattern', 'regex_replacement', 'python_code', 'line_ids')
    def _onchange_sandbox_preview(self):
        if self.test_input is not False and self.test_input is not None:
            from ..services.cleansing_engine import CleansingEngine
            self.test_output = CleansingEngine.apply_rules(self.test_input, [self])
        else:
            self.test_output = ''

    def action_test_rule(self):
        """Manual test trigger for UI feedback"""
        self.ensure_one()
        self._compute_test_output()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Rule Tested Successfully'),
                'message': _("Input: '%s' -> Output: '%s'") % (self.test_input or '', self.test_output or ''),
                'type': 'success',
                'sticky': False,
            }
        }


class ImportCleansingRuleLine(models.Model):
    _name = 'import.cleansing.rule.line'
    _description = 'Value Mapping Line'
    _order = 'sequence, id'

    rule_id = fields.Many2one('import.cleansing.rule', string='Rule', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)
    source_value = fields.Char(string='Original Value (From)', required=True)
    target_value = fields.Char(string='Mapped Value (To)', required=True)
