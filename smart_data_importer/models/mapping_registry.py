from odoo import models, fields

class MappingRegistry(models.Model):
    _name = 'mapping.registry'
    _description = 'Mapping Registry'
    _order = 'priority desc, id'

    synonym = fields.Char(string='Synonym', required=True)
    model_id = fields.Many2one('ir.model', string='Model', required=True, ondelete='cascade')
    field_id = fields.Many2one('ir.model.fields', string='Field', required=True, domain="[('model_id', '=', model_id)]", ondelete='cascade')
    language = fields.Selection([
        ('es', 'Spanish'),
        ('en', 'English')
    ], string='Language', default='es', required=True)
    priority = fields.Integer(string='Priority', default=10, help="Higher priority synonyms are matched first.")

    # Odoo 19 syntax (replaces the old _sql_constraints list)
    _synonym_model_uniq = models.Constraint(
        'unique(synonym, model_id, language)',
        'Synonym must be unique per model and language!',
    )
