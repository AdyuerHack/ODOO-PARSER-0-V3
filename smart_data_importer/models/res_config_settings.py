from odoo import models, fields

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    smart_data_importer_max_file_size_mb = fields.Float(
        string="Max File Size (MB)", 
        config_parameter='smart_data_importer.max_file_size_mb',
        default=50.0
    )
    smart_data_importer_fuzzy_threshold = fields.Integer(
        string="Fuzzy Threshold", 
        config_parameter='smart_data_importer.fuzzy_threshold',
        default=85
    )
    smart_data_importer_chunk_size = fields.Integer(
        string="Chunk Size",
        config_parameter='smart_data_importer.chunk_size',
        default=500
    )
