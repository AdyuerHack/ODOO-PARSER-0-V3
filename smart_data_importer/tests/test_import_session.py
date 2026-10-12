from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError
import base64

@tagged('post_install', '-at_install')
class TestImportSession(TransactionCase):
    
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env['import.session']
        cls.Config = cls.env['ir.config_parameter']
        
        # Ensure config parameters
        cls.Config.sudo().set_param('smart_data_importer.max_file_size_mb', '50')
        cls.Config.sudo().set_param('smart_data_importer.fuzzy_threshold', '85')
        
        # Test CSV content
        cls.csv_content = b"NIT,Correo electronico,Tel\xf3fno,Nombre\n123456,test@example.com,5551234,Test Company"
        cls.csv_b64 = base64.b64encode(cls.csv_content)
        
    def test_01_upload_and_validation(self):
        """Test file extension validation."""
        with self.assertRaises(ValidationError):
            self.Session.create({
                'filename': 'test.pdf',
                'file': base64.b64encode(b"dummy")
            })
            
        session = self.Session.create({
            'filename': 'test.csv',
            'file': self.csv_b64
        })
        self.assertEqual(session.state, 'draft')
        
    def test_02_analyze_and_mapping(self):
        """Test the file reader and mappers."""
        session = self.Session.create({
            'filename': 'test.csv',
            'file': self.csv_b64
        })
        session.action_analyze()
        
        self.assertEqual(session.state, 'mapped')
        self.assertEqual(session.total_rows, 1)
        
        mappings = session.column_mapping_ids
        self.assertEqual(len(mappings), 4)
        
        # 'NIT' should be Hash mapped to 'vat'
        nit_map = mappings.filtered(lambda m: m.source_header == 'NIT')
        self.assertEqual(nit_map.source, 'hash')
        self.assertEqual(nit_map.target_field_name, 'vat')
        
        # 'Correo electronico' should be Hash mapped to 'email'
        email_map = mappings.filtered(lambda m: m.source_header == 'Correo electronico')
        self.assertEqual(email_map.source, 'hash')
        self.assertEqual(email_map.target_field_name, 'email')

    def test_03_ai_json_parsing_and_cleaners(self):
        """Test robust JSON array and object parsing with markdown wrappers."""
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper

        # Markdown wrapped JSON array
        raw_array = "```json\n[{\"header\": \"NIT\", \"field_name\": \"vat\", \"confidence\": 95}]\n```"
        parsed_array = LLMMapper._parse_json_array(raw_array)
        self.assertEqual(len(parsed_array), 1)
        self.assertEqual(parsed_array[0]['field_name'], 'vat')

        # Markdown wrapped JSON object
        raw_obj = "```json\n{\"template_name\": \"Test Template\", \"duplicate_criterion\": \"vat\", \"duplicate_policy\": \"update\"}\n```"
        parsed_obj = LLMMapper._parse_json_object(raw_obj)
        self.assertEqual(parsed_obj.get('template_name'), 'Test Template')
        self.assertEqual(parsed_obj.get('duplicate_criterion'), 'vat')

    def test_04_ai_prepare_and_upload_template(self):
        """Test AI-assisted full analysis, template auto-generation and upload to Odoo."""
        from unittest.mock import patch
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper

        # Enable AI
        self.Config.sudo().set_param('smart_data_importer.llm_enabled', 'True')
        self.Config.sudo().set_param('smart_data_importer.llm_provider', 'deepseek')
        self.Config.sudo().set_param('smart_data_importer.llm_api_key', 'dummy-key')
        self.Config.sudo().set_param('smart_data_importer.llm_model', 'deepseek-chat')

        session = self.Session.create({
            'filename': 'clientes_2026.csv',
            'file': self.csv_b64
        })

        mock_ai_response = (
            '{\n'
            '  "template_name": "Plantilla Clientes B2B - Auto AI",\n'
            '  "duplicate_criterion": "vat",\n'
            '  "duplicate_policy": "update",\n'
            '  "analysis_summary": "Estructura limpia con 4 columnas estándar.",\n'
            '  "mappings": [\n'
            '    {"header": "NIT", "field_name": "vat", "confidence": 98, "justification": "Identificación fiscal", "cleansing_rules": ["vat_document"]},\n'
            '    {"header": "Correo electronico", "field_name": "email", "confidence": 95, "justification": "Correo principal", "cleansing_rules": ["email"]},\n'
            '    {"header": "Teléfono", "field_name": "phone", "confidence": 90, "justification": "Teléfono de contacto", "cleansing_rules": ["phone"]},\n'
            '    {"header": "Nombre", "field_name": "name", "confidence": 99, "justification": "Razón social", "cleansing_rules": ["strip"]}\n'
            '  ]\n'
            '}'
        )

        with patch.object(LLMMapper, '_call_llm', return_value=mock_ai_response):
            session.action_ai_prepare_and_upload_template()

        # Verify state is mapped
        self.assertEqual(session.state, 'mapped')

        # Verify template was created in Odoo and linked
        self.assertTrue(session.template_id)
        self.assertEqual(session.template_id.name, "Plantilla Clientes B2B - Auto AI")
        self.assertEqual(session.template_id.duplicate_criterion, "vat")
        self.assertEqual(session.template_id.duplicate_policy, "update")
        self.assertEqual(len(session.template_id.line_ids), 4)

        # Verify session mappings
        self.assertEqual(len(session.column_mapping_ids), 4)
        vat_col = session.column_mapping_ids.filtered(lambda c: c.source_header == 'NIT')
        self.assertEqual(vat_col.source, 'llm')
        self.assertEqual(vat_col.confidence, 0.98)
        self.assertTrue(bool(vat_col.cleansing_rule_ids))
        self.assertEqual(session.ai_analysis_summary, "Estructura limpia con 4 columnas estándar.")

