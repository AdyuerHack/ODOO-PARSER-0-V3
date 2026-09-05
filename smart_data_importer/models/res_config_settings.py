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

    smart_data_importer_llm_enabled = fields.Boolean(
        string="Enable AI-Assisted Mapping",
        config_parameter='smart_data_importer.llm_enabled',
        default=False,
        help="When enabled, columns not resolved by Hash or Fuzzy matching are sent to the configured LLM for semantic mapping suggestions."
    )
    smart_data_importer_llm_provider = fields.Selection([
        ('anthropic', 'Anthropic (Claude)'),
        ('openai', 'OpenAI (GPT)'),
    ], string="LLM Provider",
        config_parameter='smart_data_importer.llm_provider',
        default='anthropic'
    )
    smart_data_importer_llm_model = fields.Char(
        string="Model Name",
        config_parameter='smart_data_importer.llm_model',
        help="e.g. claude-3-5-haiku-20241022, gpt-4o-mini"
    )
    smart_data_importer_llm_api_key = fields.Char(
        string="API Key",
        config_parameter='smart_data_importer.llm_api_key'
    )