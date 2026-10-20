import base64
import io
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError, UserError


def _make_template_workbook():
    """Small workbook shaped like the base templates the consultants use:
    an instructions sheet with the colour legend, a hidden lookup sheet and a
    data sheet with technical headers, phantom rows and a flag row of 1s."""
    import openpyxl
    from openpyxl.styles import PatternFill

    red = PatternFill('solid', fgColor='FFFF0000')
    blue = PatternFill('solid', fgColor='FF0070C0')
    green = PatternFill('solid', fgColor='FF00B050')

    wb = openpyxl.Workbook()
    ins = wb.active
    ins.title = 'Instrucciones'
    ins['A1'] = 'INSTRUCCIONES'
    ins['A5'], ins['A6'], ins['A7'] = 'Obligatorio', 'Condicional', 'Opcional'
    ins['A5'].fill, ins['A6'].fill, ins['A7'].fill = red, blue, green

    values = wb.create_sheet('Posibles Valores')
    values['A1'] = 'Posibles Valores a Seleccionar'
    values['A4'] = 'Tipos de Identificación'
    values['A5'] = 'NIT'

    data = wb.create_sheet('PROVEEDORES')
    headers = ['name', 'is_company', 'parent_id', 'vat', 'phone', 'zip', 'email']
    fills = [red, red, blue, red, green, green, green]
    for col, (header, fill) in enumerate(zip(headers, fills), start=1):
        cell = data.cell(row=1, column=col, value=header)
        cell.fill = fill
    rows = [
        ['Empresa Prueba SAS', 1, None, '901705206-5', 3113792545, '050001', 'info@prueba.co'],
        ['Contacto  Prueba ', 0, 'Empresa Prueba SAS', '901705206-5', None, '050001', 'contacto@prueba.co'],
        ['Persona Rara', 2, None, None, None, None, None],
    ]
    for r, row in enumerate(rows, start=2):
        for c, value in enumerate(row, start=1):
            data.cell(row=r, column=c, value=value)
    # phantom rows: only the template default is_company = 0
    for r in range(5, 40):
        data.cell(row=r, column=2, value=0)
    # isolated flag row
    for c in range(1, 8):
        data.cell(row=60, column=c, value=1)

    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue())


@tagged('post_install', '-at_install')
class TestImportSession(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env['import.session']
        cls.Config = cls.env['ir.config_parameter']

        cls.Config.sudo().set_param('smart_data_importer.max_file_size_mb', '50')
        cls.Config.sudo().set_param('smart_data_importer.fuzzy_threshold', '85')
        cls.Config.sudo().set_param('smart_data_importer.llm_enabled', 'False')

        # latin-1 encoded CSV with accents and a leading-zero postal code
        cls.csv_content = "NIT,Correo electronico,Teléfono,Nombre,CP\n0123456,test@example.com,3001234567,Test Company,050001".encode('latin-1')
        cls.csv_b64 = base64.b64encode(cls.csv_content)
        cls.xlsx_b64 = _make_template_workbook()

    # ------------------------------------------------------------------
    # Reader
    # ------------------------------------------------------------------

    def test_01_upload_and_validation(self):
        """Test file extension validation."""
        with self.assertRaises(ValidationError):
            self.Session.create({'filename': 'test.pdf', 'file': base64.b64encode(b"dummy")})

        session = self.Session.create({'filename': 'test.csv', 'file': self.csv_b64})
        self.assertEqual(session.state, 'draft')
        self.assertEqual(len(session.sheet_ids), 1)

    def test_02_analyze_and_mapping(self):
        """Hash mapping via synonyms, and text values keep their leading zeros."""
        session = self.Session.create({'filename': 'test.csv', 'file': self.csv_b64})
        session.action_analyze()

        self.assertEqual(session.state, 'mapped')
        self.assertEqual(session.total_rows, 1)
        self.assertEqual(len(session.column_mapping_ids), 5)

        nit_map = session.column_mapping_ids.filtered(lambda m: m.source_header == 'NIT')
        self.assertEqual(nit_map.source, 'hash')
        self.assertEqual(nit_map.target_field_name, 'vat')
        self.assertIn('0123456', nit_map.sample_values)

        email_map = session.column_mapping_ids.filtered(lambda m: m.source_header == 'Correo electronico')
        self.assertEqual(email_map.source, 'hash')
        self.assertEqual(email_map.target_field_name, 'email')

    def test_03_reader_multisheet_template(self):
        """Instructions/values sheets are skipped, phantom and flag rows dropped, colours read."""
        from odoo.addons.smart_data_importer.services.file_reader import FileReader

        sheets = {s['name']: s for s in FileReader.list_sheets(self.xlsx_b64, 'contactos.xlsx')}
        self.assertEqual(sheets['Instrucciones']['kind'], 'instructions')
        self.assertEqual(sheets['Posibles Valores']['kind'], 'values')
        self.assertEqual(sheets['PROVEEDORES']['kind'], 'data')

        info = FileReader.read(self.xlsx_b64, 'contactos.xlsx')
        self.assertEqual(info['sheet_name'], 'PROVEEDORES')
        df = info['df']
        self.assertEqual(len(df), 3, "Only the 3 real rows must remain")
        self.assertEqual(list(df.index), [2, 3, 4], "Index keeps the real spreadsheet row numbers")
        self.assertEqual(df.loc[2, 'phone'], '3113792545', "Numbers must not become floats")
        self.assertEqual(df.loc[2, 'zip'], '050001', "Leading zeros must be kept")
        self.assertEqual(info['column_meta']['name']['required_level'], 'required')
        self.assertEqual(info['column_meta']['parent_id']['required_level'], 'conditional')
        self.assertEqual(info['column_meta']['email']['required_level'], 'optional')

    def test_04_session_detects_sheet_and_model(self):
        session = self.Session.create({'filename': 'contactos.xlsx', 'file': self.xlsx_b64})
        self.assertEqual(session.sheet_id.name, 'PROVEEDORES')
        self.assertEqual(session.target_model_id.model, 'res.partner')
        session.action_analyze()
        vat = session.column_mapping_ids.filtered(lambda m: m.source_header == 'vat')
        self.assertEqual(vat.target_field_name, 'vat')
        self.assertEqual(vat.required_level, 'required')

    # ------------------------------------------------------------------
    # AI
    # ------------------------------------------------------------------

    def test_05_ai_json_parsing(self):
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper

        raw_array = "```json\n[{\"header\": \"NIT\", \"field_name\": \"vat\", \"confidence\": 95}]\n```"
        parsed_array = LLMMapper._parse_json_array(raw_array)
        self.assertEqual(parsed_array[0]['field_name'], 'vat')

        raw_obj = "```json\n{\"template_name\": \"Test\", \"duplicate_criterion\": \"vat\"}\n```"
        self.assertEqual(LLMMapper._parse_json_object(raw_obj).get('template_name'), 'Test')

        self.assertEqual(LLMMapper._normalize_confidence(95, 0.5), 0.95)
        self.assertEqual(LLMMapper._normalize_confidence(0.95, 0.5), 0.95)
        self.assertEqual(LLMMapper._normalize_confidence('x', 0.5), 0.5)

    def _enable_ai(self):
        self.Config.sudo().set_param('smart_data_importer.llm_enabled', 'True')
        self.Config.sudo().set_param('smart_data_importer.llm_provider', 'deepseek')
        self.Config.sudo().set_param('smart_data_importer.llm_api_key', 'dummy-key')
        self.Config.sudo().set_param('smart_data_importer.llm_model', 'deepseek-chat')

    def test_06_ai_prepare_and_upload_template(self):
        """AI analysis builds the template; omitted columns are kept as ignored."""
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper
        self._enable_ai()
        session = self.Session.create({'filename': 'clientes_2026.csv', 'file': self.csv_b64})

        mock_ai_response = (
            '{"template_name": "Plantilla Clientes B2B - Auto AI", "duplicate_criterion": "vat",'
            ' "duplicate_policy": "update", "analysis_summary": "Estructura limpia.",'
            ' "mappings": ['
            '  {"header": "NIT", "field_name": "vat", "confidence": 98, "justification": "Identificación fiscal", "cleansing_rules": ["vat_document"]},'
            '  {"header": "Correo electronico", "field_name": "email", "confidence": 0.95, "justification": "Correo", "cleansing_rules": ["email"]},'
            '  {"header": "Teléfono", "field_name": "phone", "confidence": 90, "justification": "Teléfono", "cleansing_rules": ["phone"]},'
            '  {"header": "Nombre", "field_name": "name", "confidence": 99, "justification": "Razón social", "cleansing_rules": ["strip"]}'
            ']}'
        )
        with patch.object(LLMMapper, '_call_llm', return_value=mock_ai_response):
            session.action_ai_prepare_and_upload_template()

        self.assertEqual(session.state, 'mapped')
        self.assertEqual(session.template_id.name, "Plantilla Clientes B2B - Auto AI")
        self.assertEqual(len(session.template_id.line_ids), 5)
        self.assertEqual(len(session.column_mapping_ids), 5)

        vat_col = session.column_mapping_ids.filtered(lambda c: c.source_header == 'NIT')
        self.assertEqual(vat_col.source, 'llm')
        self.assertAlmostEqual(vat_col.confidence, 0.98)
        self.assertTrue(vat_col.cleansing_rule_ids)

        email_col = session.column_mapping_ids.filtered(lambda c: c.source_header == 'Correo electronico')
        self.assertAlmostEqual(email_col.confidence, 0.95, msg="0-1 confidences must not be divided again")

        cp_col = session.column_mapping_ids.filtered(lambda c: c.source_header == 'CP')
        self.assertTrue(cp_col.is_ignored, "A column the AI forgot must stay visible as ignored")

        # Running it again updates the same template instead of creating another one
        with patch.object(LLMMapper, '_call_llm', return_value=mock_ai_response):
            session.write({'state': 'draft'})
            session.action_ai_prepare_and_upload_template()
        templates = self.env['import.template'].search([('header_signature', '=', session.template_id.header_signature)])
        self.assertEqual(len(templates), 1)

    def test_07_ai_invalid_policy_values_are_normalised(self):
        """The AI may answer 'ignore'/'block'/'name', which are not valid selection values."""
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper
        self._enable_ai()
        session = self.Session.create({'filename': 'c.csv', 'file': self.csv_b64})
        response = ('{"template_name": "T", "duplicate_criterion": "name", "duplicate_policy": "block",'
                    ' "analysis_summary": "ok", "mappings": []}')
        with patch.object(LLMMapper, '_call_llm', return_value=response):
            session.action_ai_prepare_and_upload_template()
        self.assertEqual(session.duplicate_criterion, 'vat')
        self.assertEqual(session.duplicate_policy, 'skip')

    def test_08_ai_provider_error_is_readable(self):
        from odoo.addons.smart_data_importer.services.mapper_llm import LLMMapper, LLMError
        self._enable_ai()
        session = self.Session.create({'filename': 'c.csv', 'file': self.csv_b64})
        with patch.object(LLMMapper, '_call_llm', side_effect=LLMError("HTTP 401 (API key inválida)")):
            with self.assertRaises(UserError):
                session.action_ai_prepare_and_upload_template()

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def test_09_value_converter(self):
        from odoo.addons.smart_data_importer.services.value_converter import ValueConverter, ConversionError
        conv = ValueConverter(self.env, 'res.partner')
        self.assertIs(conv.convert('is_company', '1'), True)
        self.assertIs(conv.convert('is_company', 'No'), False)
        with self.assertRaises(ConversionError):
            conv.convert('is_company', '2')
        self.assertEqual(conv.convert('type', 'contact'), 'contact')
        co = self.env.ref('base.co')
        self.assertEqual(conv.convert('country_id', co.name), co.id)
        self.assertEqual(conv.convert('country_id', 'base.co', subfield='id'), co.id)
        with self.assertRaises(ConversionError):
            conv.convert('country_id', 'País que no existe')

    def test_10_end_to_end_import(self):
        """Full import of the template sheet: real rows created, bad row reported with its row number."""
        session = self.Session.create({'filename': 'contactos.xlsx', 'file': self.xlsx_b64})
        session.action_analyze()
        session.duplicate_criterion = 'vat'
        session.action_confirm_mapping()

        self.assertEqual(session.state, 'done')
        self.assertTrue(session.end_time)
        self.assertEqual(session.processed_rows, 3)
        self.assertEqual(session.created_rows, 2)
        self.assertEqual(session.failed_rows, 1)
        self.assertEqual(session.progress_percent, 100.0)

        company = self.env['res.partner'].search([('name', '=', 'Empresa Prueba SAS')])
        self.assertTrue(company.is_company)
        self.assertEqual(company.zip, '050001')
        self.assertEqual(company.phone, '3113792545')

        contact = self.env['res.partner'].search([('name', '=', 'Contacto Prueba')])
        self.assertEqual(contact.parent_id, company, "Parent resolved by name; double spaces collapsed")
        self.assertFalse(company.email == 'contacto@prueba.co', "The contact must not overwrite its company")

        error = session.row_error_ids
        self.assertEqual(error.row_number, 4)
        self.assertIn("'2'", error.error_message)
