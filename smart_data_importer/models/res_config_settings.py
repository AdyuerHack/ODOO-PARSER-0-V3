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
        string="Enable AI-Assisted Mapping & Templates",
        config_parameter='smart_data_importer.llm_enabled',
        default=False,
        help="When enabled, AI will analyze spreadsheets, auto-generate templates, suggest mappings and cleansing rules."
    )
    smart_data_importer_llm_provider = fields.Selection([
        ('deepseek', 'DeepSeek (DeepSeek-V3 / R1)'),
        ('anthropic', 'Anthropic (Claude 3.5 Sonnet / Haiku)'),
        ('gemini', 'Google Gemini (Gemini 2.0 Flash / 1.5 Pro)'),
        ('openai', 'OpenAI (GPT-4o / GPT-4o-mini)'),
        ('custom', 'Custom / Local (Ollama, Groq, OpenRouter)'),
    ], string="AI Provider",
        config_parameter='smart_data_importer.llm_provider',
        default='deepseek'
    )
    smart_data_importer_llm_model = fields.Char(
        string="Model Name",
        config_parameter='smart_data_importer.llm_model',
        help="e.g. deepseek-chat, claude-3-5-haiku-20241022, gemini-1.5-flash, gpt-4o-mini"
    )
    smart_data_importer_llm_api_key = fields.Char(
        string="API Key",
        config_parameter='smart_data_importer.llm_api_key'
    )
    smart_data_importer_llm_base_url = fields.Char(
        string="Custom Base URL / Endpoint",
        config_parameter='smart_data_importer.llm_base_url',
        help="Custom OpenAI-compatible URL e.g. http://localhost:11434/v1 or https://api.groq.com/openai/v1"
    )

    def action_test_llm_connection(self):
        """Tests the LLM connection and returns a notification to the user."""
        self.ensure_one()
        # Save current settings to ir.config_parameter first so the test runs against them
        config = self.env['ir.config_parameter'].sudo()
        config.set_param('smart_data_importer.llm_enabled', 'True' if self.smart_data_importer_llm_enabled else 'False')
        config.set_param('smart_data_importer.llm_provider', self.smart_data_importer_llm_provider or 'deepseek')
        config.set_param('smart_data_importer.llm_model', self.smart_data_importer_llm_model or '')
        config.set_param('smart_data_importer.llm_api_key', self.smart_data_importer_llm_api_key or '')
        config.set_param('smart_data_importer.llm_base_url', self.smart_data_importer_llm_base_url or '')

        from ..services.mapper_llm import LLMMapper
        success, message = LLMMapper.test_connection(self.env)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Prueba de Conexión IA' if success else 'Fallo de Conexión IA',
                'message': message,
                'type': 'success' if success else 'danger',
                'sticky': not success,
            }
        }

