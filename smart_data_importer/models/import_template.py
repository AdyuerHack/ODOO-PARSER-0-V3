from odoo import models, fields, api, _


class ImportTemplate(models.Model):
    _name = 'import.template'
    _inherit = [
        'import.deduplication.mixin',
        'import.signature.mixin'
    ]
    _description = 'Reusable Import Template'
    _order = 'create_date desc'

    name = fields.Char(string='Template Name', required=True)
    active = fields.Boolean(string='Active', default=True)
    description = fields.Text(string='Description / Source Notes')

    model_id = fields.Many2one(
        'ir.model',
        string='Target Model',
        required=True,
        ondelete='cascade',
        default=lambda self: self.env['ir.model'].search([('model', '=', 'res.partner')], limit=1)
    )

    line_ids = fields.One2many('import.template.line', 'template_id', string='Template Columns', copy=True)
    line_count = fields.Integer(string='Columns Count', compute='_compute_line_count')

    @api.depends('line_ids')
    def _compute_line_count(self):
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.model
    def find_matching_template(self, headers, model_name='res.partner'):
        """Searches for an active template matching the provided column headers."""
        signature = self.compute_header_signature(headers)
        if not signature:
            return self.browse()
        return self.search([
            ('active', '=', True),
            ('model_id.model', '=', model_name),
            ('header_signature', '=', signature)
        ], limit=1)

    @api.model
    def upsert_for_headers(self, headers, model, name, lines_vals, duplicate_criterion=None,
                           duplicate_policy=None, description=None):
        """Create the template for these headers, or update the existing one.

        Re-running the AI or saving the same mapping twice no longer creates
        duplicated templates.
        """
        signature = self.compute_header_signature(headers)
        vals = {
            'header_signature': signature,
            'header_list': self.format_header_list(headers),
            'line_ids': [(5, 0, 0)] + [(0, 0, dict(v, sequence=i)) for i, v in enumerate(lines_vals)],
        }
        if duplicate_criterion:
            vals['duplicate_criterion'] = duplicate_criterion
        if duplicate_policy:
            vals['duplicate_policy'] = duplicate_policy
        if description:
            vals['description'] = description

        template = self.find_matching_template(headers, model_name=model.model)
        if template:
            template.write(vals)
        else:
            vals.update({'name': name, 'model_id': model.id})
            template = self.create(vals)
        return template


class ImportTemplateLine(models.Model):
    _name = 'import.template.line'
    _description = 'Import Template Column Line'
    _order = 'sequence, id'

    template_id = fields.Many2one('import.template', string='Template', required=True, ondelete='cascade')
    sequence = fields.Integer(string='Sequence', default=10)
    source_header = fields.Char(string='Original File Header', required=True)
    # The domain is set in the view from the template's model
    target_field_id = fields.Many2one('ir.model.fields', string='Target Field', ondelete='cascade')
    target_subfield = fields.Char(string='Sub-field')
    cleansing_rule_ids = fields.Many2many('import.cleansing.rule', string='Cleansing Rules')
    is_ignored = fields.Boolean(string='Ignore Column', default=False)
