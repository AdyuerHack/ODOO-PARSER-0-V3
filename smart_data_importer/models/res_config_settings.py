from odoo import models, fields, api

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
        ('deepseek', 'DeepSeek'),
        ('anthropic', 'Anthropic (Claude)'),
        ('gemini', 'Google Gemini'),
        ('openai', 'OpenAI'),
        ('custom', 'Custom / Local (Ollama, Groq, OpenRouter)'),
    ], string="AI Provider",
        config_parameter='smart_data_importer.llm_provider',
        default='deepseek'
    )
    smart_data_importer_llm_model = fields.Char(
        string="Model Name",
        config_parameter='smart_data_importer.llm_model',
        help="Leave empty to use the provider default: deepseek-chat, claude-haiku-5-5, "
             "gemini-3.5-flash-lite, gpt-4o-mini, llama3."
    )
    smart_data_importer_llm_api_key = fields.Char(
        string="API Key",
        config_parameter='smart_data_importer.llm_api_key'
    )
    smart_data_importer_llm_base_url = fields.Char(
        string="Custom Base URL / Endpoint",
        config_parameter='smart_data_importer.llm_base_url',
        help="Base URL of an OpenAI-compatible server, e.g. http://localhost:11434/v1 or "
             "https://api.groq.com/openai/v1. '/chat/completions' is added automatically."
    )
    smart_data_importer_llm_send_samples = fields.Boolean(
        string="Send sample values to the AI",
        default=True,
        help="When disabled, only column headers are sent to the AI provider, never cell values "
             "(useful when files contain personal data)."
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        param = self.env['ir.config_parameter'].sudo().get_param('smart_data_importer.llm_send_samples', 'True')
        res['smart_data_importer_llm_send_samples'] = param != 'False'
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'smart_data_importer.llm_send_samples',
            'True' if self.smart_data_importer_llm_send_samples else 'False')

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
        config.set_param('smart_data_importer.llm_send_samples', 'True' if self.smart_data_importer_llm_send_samples else 'False')

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

