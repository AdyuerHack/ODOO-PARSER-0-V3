import json
import logging
import re
import requests

_logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


class LLMMapper:
    """
    Universal AI & LLM Service for Smart Data Importer.
    Supports DeepSeek, Anthropic (Claude), Google Gemini, OpenAI, and any Custom/OpenAI-compatible endpoint (Ollama, Groq, etc.).
    Provides:
      1. Fallback column-to-field semantic mapping (Level 3 cascade).
      2. Full-file template analysis, structuring, rule recommendation, and Odoo template generation.
      3. Live connection testing.
    """

    @classmethod
    def test_connection(cls, env):
        """
        Tests the configured AI connection with a lightweight prompt.
        :return: tuple (success: bool, message: str)
        """
        config = env['ir.config_parameter'].sudo()
        if config.get_param('smart_data_importer.llm_enabled', 'False') != 'True':
            return False, "El asistente de IA no está habilitado en Configuración."

        provider = config.get_param('smart_data_importer.llm_provider', 'openai')
        model = config.get_param('smart_data_importer.llm_model', '')
        api_key = config.get_param('smart_data_importer.llm_api_key', '')
        base_url = config.get_param('smart_data_importer.llm_base_url', '')

        if not api_key and provider != 'custom':
            return False, f"Falta la clave API (API Key) para el proveedor '{provider}'."

        model = model or cls._get_default_model(provider)

        test_prompt = "Responde únicamente con un JSON válido exactamente así: {\"status\": \"ok\", \"provider\": \"" + provider + "\"}"

        try:
            raw_response = cls._call_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                prompt=test_prompt,
                base_url=base_url,
                timeout=15,
                max_tokens=100
            )
            parsed = cls._parse_json_object(raw_response)
            if parsed and parsed.get('status') == 'ok':
                return True, f"¡Conexión exitosa con {provider.upper()} (Modelo: {model})!"
            return True, f"Respuesta recibida de {provider.upper()} ({model}): {raw_response[:100]}"
        except Exception as e:
            _logger.exception("Error al probar la conexión con el LLM:")
            return False, f"Error al conectar con {provider.upper()}: {str(e)}"

    @classmethod
    def map(cls, env, headers, sample_values_map, model_name='res.partner'):
        """
        Semantic mapper: Level 3 of the mapping cascade.
        Used only for headers that Hash (Level 1) and Fuzzy (Level 2) could not resolve.
        """
        if not headers:
            return {}

        config = env['ir.config_parameter'].sudo()
        if config.get_param('smart_data_importer.llm_enabled', 'False') != 'True':
            return {}

        provider = config.get_param('smart_data_importer.llm_provider', 'openai')
        model = config.get_param('smart_data_importer.llm_model') or cls._get_default_model(provider)
        api_key = config.get_param('smart_data_importer.llm_api_key', '')
        base_url = config.get_param('smart_data_importer.llm_base_url', '')

        if not api_key and provider != 'custom':
            _logger.warning("LLM mapping is enabled but api_key is missing. Skipping LLM step.")
            return {}

        fields_records = env['ir.model.fields'].search([
            ('model_id.model', '=', model_name),
            ('store', '=', True),
        ])
        field_options = {
            f.name: f.field_description
            for f in fields_records
            if f.name not in ('id', 'display_name')
        }
        name_to_id = {f.name: f.id for f in fields_records}

        prompt = cls._build_mapping_prompt(headers, sample_values_map, field_options)

        try:
            raw_response = cls._call_llm(provider, model, api_key, prompt, base_url=base_url)
            suggestions = cls._parse_json_array(raw_response)
        except Exception as e:
            _logger.warning("LLM mapping call failed, skipping LLM step: %s", str(e))
            return {}

        results = {}
        for item in suggestions:
            header = item.get('header')
            field_name = item.get('field_name')
            confidence = item.get('confidence')
            justification = item.get('justification', '')

            if header not in headers or not field_name or field_name not in name_to_id:
                continue

            try:
                confidence_val = max(0.0, min(1.0, float(confidence) / 100.0))
            except (TypeError, ValueError):
                confidence_val = 0.7

            results[header] = {
                'target_field_id': name_to_id[field_name],
                'confidence': confidence_val,
                'source': 'llm',
                'llm_justification': justification,
            }

        return results

    @classmethod
    def analyze_and_structure_template(cls, env, headers, sample_values_map, total_rows=0, filename="", model_name='res.partner'):
        """
        Deep semantic analysis:
        Analyzes the full spreadsheet/CSV, predicts fields, recommends cleansing rules,
        suggests duplicate criteria and policy, and prepares template metadata.
        :return: dict with structured template specifications
        """
        config = env['ir.config_parameter'].sudo()
        if config.get_param('smart_data_importer.llm_enabled', 'False') != 'True':
            raise ValueError("El servicio de IA no está habilitado en Configuración.")

        provider = config.get_param('smart_data_importer.llm_provider', 'openai')
        model = config.get_param('smart_data_importer.llm_model') or cls._get_default_model(provider)
        api_key = config.get_param('smart_data_importer.llm_api_key', '')
        base_url = config.get_param('smart_data_importer.llm_base_url', '')

        if not api_key and provider != 'custom':
            raise ValueError(f"Falta configurar la API Key para el proveedor '{provider}'.")

        fields_records = env['ir.model.fields'].search([
            ('model_id.model', '=', model_name),
            ('store', '=', True),
        ])
        field_options = {
            f.name: f.field_description or f.name
            for f in fields_records
            if f.name not in ('id', 'display_name')
        }
        name_to_id = {f.name: f.id for f in fields_records}

        # Available cleansing rule actions
        rules_records = env['import.cleansing.rule'].search([('active', '=', True)])
        available_rules = {r.action_type: r.name for r in rules_records}

        prompt = cls._build_full_template_prompt(
            headers=headers,
            sample_values_map=sample_values_map,
            field_options=field_options,
            available_rules=available_rules,
            total_rows=total_rows,
            filename=filename
        )

        raw_response = cls._call_llm(provider, model, api_key, prompt, base_url=base_url, timeout=45, max_tokens=2500)
        parsed = cls._parse_json_object(raw_response)
        if not parsed:
            raise ValueError("La IA no devolvió una respuesta con formato JSON válido.")

        # Process and validate mappings
        validated_mappings = []
        raw_mappings = parsed.get('mappings', [])

        for item in raw_mappings:
            header = item.get('header')
            field_name = item.get('field_name')
            confidence = item.get('confidence', 80)
            justification = item.get('justification', '')
            is_ignored = bool(item.get('is_ignored', False))
            suggested_rules = item.get('cleansing_rules', [])

            target_id = name_to_id.get(field_name) if field_name in name_to_id else False

            try:
                confidence_val = max(0.0, min(1.0, float(confidence) / 100.0))
            except (TypeError, ValueError):
                confidence_val = 0.8

            # Match cleansing rule action_types to rule IDs
            rule_ids = []
            if suggested_rules and isinstance(suggested_rules, list):
                rule_recs = env['import.cleansing.rule'].search([
                    ('action_type', 'in', suggested_rules),
                    ('active', '=', True)
                ])
                rule_ids = rule_recs.ids

            validated_mappings.append({
                'header': header,
                'target_field_id': target_id,
                'target_field_name': field_name if target_id else False,
                'confidence': confidence_val,
                'justification': justification,
                'is_ignored': is_ignored or (not target_id),
                'rule_ids': rule_ids,
            })

        dup_criterion = parsed.get('duplicate_criterion', 'vat')
        if dup_criterion not in ('vat', 'email', 'name'):
            dup_criterion = 'vat'

        dup_policy = parsed.get('duplicate_policy', 'update')
        if dup_policy not in ('ignore', 'update', 'block'):
            dup_policy = 'update'

        template_name = parsed.get('template_name') or f"Plantilla IA - {filename or 'res.partner'}"
        summary = parsed.get('analysis_summary', 'Análisis completado satisfactoriamente por IA.')

        return {
            'template_name': template_name,
            'duplicate_criterion': dup_criterion,
            'duplicate_policy': dup_policy,
            'mappings': validated_mappings,
            'summary': summary,
            'provider': provider,
            'model': model
        }

    # -------------------------------------------------------------------------
    # Prompts
    # -------------------------------------------------------------------------

    @classmethod
    def _build_mapping_prompt(cls, headers, sample_values_map, field_options):
        fields_list = "\n".join(f"- {name}: {desc}" for name, desc in sorted(field_options.items()))
        columns_list = "\n".join(
            f'- Column "{h}", sample values: {sample_values_map.get(h, [])}' for h in headers
        )
        return (
            "You are an expert Odoo data engineer mapping spreadsheet column headers to Odoo res.partner fields.\n\n"
            f"Available target fields:\n{fields_list}\n\n"
            f"Unmapped columns:\n{columns_list}\n\n"
            "For each column, determine the single best matching Odoo field from the list above based on "
            "the header name and sample values. If no field is a reasonable match, omit that column.\n\n"
            "Respond ONLY with a valid JSON array, without markdown formatting or commentary. Each item must have:\n"
            "[\n"
            "  {\n"
            '    "header": "original column name",\n'
            '    "field_name": "technical_field_name",\n'
            '    "confidence": integer between 0 and 100,\n'
            '    "justification": "short justification in Spanish"\n'
            "  }\n"
            "]"
        )

    @classmethod
    def _build_full_template_prompt(cls, headers, sample_values_map, field_options, available_rules, total_rows, filename):
        fields_list = "\n".join(f"- {name}: {desc}" for name, desc in sorted(field_options.items()))
        columns_list = "\n".join(
            f'- Column "{h}", sample values: {sample_values_map.get(h, [])[:5]}' for h in headers
        )
        rules_list = "\n".join(f"- {action}: {name}" for action, name in available_rules.items())

        return (
            "Eres un Arquitecto de Datos y Especialista Senior de Odoo.\n"
            f"Se ha cargado un archivo para importar contactos a res.partner ('{filename}', {total_rows} registros aproximados).\n\n"
            f"CAMPOS DISPONIBLES EN ODOO (res.partner):\n{fields_list}\n\n"
            f"REGLAS DE LIMPIEZA DISPONIBLES:\n{rules_list}\n\n"
            f"COLUMNAS DEL ARCHIVO Y VALORES DE MUESTRA:\n{columns_list}\n\n"
            "INSTRUCCIONES:\n"
            "1. Analiza cada columna del archivo e identifica a qué campo de res.partner corresponde.\n"
            "2. Asigna las mejores reglas de limpieza para cada columna (ejemplo: 'email' para correos, 'phone' para telefonos, 'vat_document' para identificación/NIT/RUT, 'strip' para textos, 'titlecase' para nombres de personas).\n"
            "3. Determina si alguna columna debe ignorarse (is_ignored=true si son datos de relleno o notas irrelevantes).\n"
            "4. Define el criterio de deduplicación recomendado ('vat' si hay identificación tributaria, 'email' si hay correo único, o 'name').\n"
            "5. Define la política de duplicados recomendada ('update' para actualizar registros existentes, 'ignore' o 'block').\n"
            "6. Asigna un nombre descriptivo a la plantilla (ej: 'Plantilla Contactos B2B - [Nombre Empresa]').\n"
            "7. Escribe un resumen ejecutivo de calidad de datos en español (analysis_summary) con observaciones, alertas de formatos y recomendaciones.\n\n"
            "RESPONDE ÚNICAMENTE CON UN OBJETO JSON VÁLIDO (sin bloques de código markdown, sin explicaciones fuera del JSON) con la siguiente estructura exacta:\n"
            "{\n"
            '  "template_name": "Nombre sugerido de la plantilla",\n'
            '  "duplicate_criterion": "vat" | "email" | "name",\n'
            '  "duplicate_policy": "update" | "ignore" | "block",\n'
            '  "analysis_summary": "Resumen ejecutivo en español sobre la calidad, consistencia y formato del archivo...",\n'
            '  "mappings": [\n'
            "    {\n"
            '      "header": "nombre de la columna del archivo",\n'
            '      "field_name": "campo_tecnico_odoo",\n'
            '      "confidence": 95,\n'
            '      "is_ignored": false,\n'
            '      "cleansing_rules": ["strip", "email"],\n'
            '      "justification": "Explicación breve de por qué se mapeó a este campo"\n'
            "    }\n"
            "  ]\n"
            "}"
        )

    # -------------------------------------------------------------------------
    # Provider Calling & Communication
    # -------------------------------------------------------------------------

    @classmethod
    def _call_llm(cls, provider, model, api_key, prompt, base_url="", timeout=30, max_tokens=1500):
        provider = (provider or '').lower()

        if provider == 'anthropic':
            url = base_url.strip() if base_url else ANTHROPIC_API_URL
            headers = {
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
            payload = {
                'model': model or 'claude-3-5-haiku-20241022',
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return "".join(
                block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text'
            )

        elif provider in ('openai', 'deepseek', 'custom'):
            if provider == 'openai':
                url = base_url.strip() if base_url else OPENAI_API_URL
            elif provider == 'deepseek':
                url = base_url.strip() if base_url else DEEPSEEK_API_URL
            else:  # custom
                url = base_url.strip()
                if not url:
                    raise ValueError("El proveedor personalizado requiere especificar la URL base (Base URL).")

            headers = {
                'Authorization': f'Bearer {api_key}',
                'content-type': 'application/json',
            }
            payload = {
                'model': model or cls._get_default_model(provider),
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': max_tokens,
                'temperature': 0.1,
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content']

        elif provider == 'gemini':
            # Google Gemini native generateContent API
            gemini_model = model or 'gemini-1.5-flash'
            if base_url:
                # If custom base URL provided, use OpenAI-compatible proxy for Gemini
                url = base_url.strip()
                headers = {'Authorization': f'Bearer {api_key}', 'content-type': 'application/json'}
                payload = {
                    'model': gemini_model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'max_tokens': max_tokens,
                    'temperature': 0.1,
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                return data['choices'][0]['message']['content']
            else:
                url = GEMINI_API_URL_TEMPLATE.format(model=gemini_model, key=api_key)
                headers = {'content-type': 'application/json'}
                payload = {
                    'contents': [{
                        'parts': [{'text': prompt}]
                    }],
                    'generationConfig': {
                        'temperature': 0.1,
                        'maxOutputTokens': max_tokens
                    }
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get('candidates', [])
                if not candidates:
                    raise ValueError("Gemini no generó candidatos de respuesta.")
                parts = candidates[0].get('content', {}).get('parts', [])
                return "".join(p.get('text', '') for p in parts)

        else:
            raise ValueError(f"Proveedor de IA no soportado: {provider}")

    @classmethod
    def _get_default_model(cls, provider):
        defaults = {
            'openai': 'gpt-4o-mini',
            'anthropic': 'claude-3-5-haiku-20241022',
            'deepseek': 'deepseek-chat',
            'gemini': 'gemini-1.5-flash',
            'custom': 'llama3',
        }
        return defaults.get(provider, 'gpt-4o-mini')

    # -------------------------------------------------------------------------
    # Response Parsing Utilities
    # -------------------------------------------------------------------------

    @classmethod
    def _clean_json_text(cls, raw_text):
        if not raw_text:
            return ""
        text = raw_text.strip()
        # Remove markdown code fences if present
        if "```" in text:
            match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.IGNORECASE)
            if match:
                text = match.group(1).strip()
            else:
                text = text.replace('```json', '').replace('```', '').strip()
        return text

    @classmethod
    def _parse_json_array(cls, raw_text):
        cleaned = cls._clean_json_text(raw_text)
        if not cleaned:
            return []
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            # Fallback: find first '[' and last ']'
            start = cleaned.find('[')
            end = cleaned.rfind(']')
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    pass
            _logger.warning("No se pudo parsear el array JSON del LLM: %s", cleaned[:300])
            return []

    @classmethod
    def _parse_json_object(cls, raw_text):
        cleaned = cls._clean_json_text(raw_text)
        if not cleaned:
            return {}
        try:
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            # Fallback: find first '{' and last '}'
            start = cleaned.find('{')
            end = cleaned.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    pass
            _logger.warning("No se pudo parsear el objeto JSON del LLM: %s", cleaned[:300])
            return {}