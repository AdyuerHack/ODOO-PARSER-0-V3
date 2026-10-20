import json
import logging
import re

import requests

from .field_catalog import FieldCatalog
from .file_reader import FileReader

_logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Default model per provider, used when the "Model Name" setting is empty.
# Checked October 2026 against each provider's deprecation page:
# - claude-3-5-haiku-20241022 was retired on 2026-02-19 (claude-haiku-5-5 is active until at least 2027-10)
# - gemini-1.5-flash is no longer available for new projects
# - gpt-4o-mini is scheduled for shutdown on 2027-01-14
DEFAULT_MODELS = {
    'openai': 'gpt-4o-mini',
    'anthropic': 'claude-haiku-5-5',
    'deepseek': 'deepseek-chat',
    'gemini': 'gemini-3.5-flash-lite',
    'custom': 'llama3',
}

# Values accepted by import.deduplication.mixin
DUPLICATE_CRITERIA = ('vat', 'email', 'vat_or_email', 'auto')
DUPLICATE_POLICIES = ('update', 'skip', 'create')
# Words an LLM tends to use instead of our keys
POLICY_ALIASES = {'ignore': 'skip', 'omit': 'skip', 'block': 'skip', 'insert': 'create', 'overwrite': 'update'}


class LLMError(Exception):
    """Readable error raised when the AI provider cannot be used."""


class LLMMapper:
    """
    Universal AI service for Smart Data Importer.
    Supports DeepSeek, Anthropic (Claude), Google Gemini, OpenAI and any
    OpenAI-compatible endpoint (Ollama, Groq, OpenRouter...).
    Provides:
      1. Fallback column-to-field semantic mapping (Level 3 cascade).
      2. Full-sheet analysis that builds a reusable import template.
      3. Live connection testing.
    """

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @classmethod
    def _get_config(cls, env):
        config = env['ir.config_parameter'].sudo()
        provider = (config.get_param('smart_data_importer.llm_provider') or 'deepseek').lower()
        return {
            'enabled': config.get_param('smart_data_importer.llm_enabled', 'False') == 'True',
            'provider': provider,
            'model': (config.get_param('smart_data_importer.llm_model') or '').strip() or cls._get_default_model(provider),
            'api_key': (config.get_param('smart_data_importer.llm_api_key') or '').strip(),
            'base_url': (config.get_param('smart_data_importer.llm_base_url') or '').strip(),
            'send_samples': config.get_param('smart_data_importer.llm_send_samples', 'True') != 'False',
        }

    @classmethod
    def _get_default_model(cls, provider):
        return DEFAULT_MODELS.get(provider, DEFAULT_MODELS['openai'])

    @classmethod
    def test_connection(cls, env):
        """
        Tests the configured AI connection with a lightweight prompt.
        :return: tuple (success: bool, message: str)
        """
        cfg = cls._get_config(env)
        if not cfg['enabled']:
            return False, "El asistente de IA no está habilitado en Configuración."
        if not cfg['api_key'] and cfg['provider'] != 'custom':
            return False, f"Falta la clave API (API Key) para el proveedor '{cfg['provider']}'."

        prompt = 'Responde únicamente con este JSON: {"status": "ok"}'
        try:
            raw = cls._call_llm(cfg['provider'], cfg['model'], cfg['api_key'], prompt,
                                base_url=cfg['base_url'], timeout=20, max_tokens=50)
        except LLMError as e:
            return False, f"Error al conectar con {cfg['provider'].upper()} ({cfg['model']}): {e}"
        except Exception as e:
            _logger.exception("Unexpected error testing the LLM connection")
            return False, f"Error inesperado con {cfg['provider'].upper()}: {e}"

        parsed = cls._parse_json_object(raw)
        if parsed.get('status') == 'ok':
            return True, f"¡Conexión exitosa con {cfg['provider'].upper()} (modelo: {cfg['model']})!"
        return True, f"Respuesta recibida de {cfg['provider'].upper()} ({cfg['model']}): {raw[:100]}"

    # ------------------------------------------------------------------
    # Level 3 mapping
    # ------------------------------------------------------------------

    @classmethod
    def map(cls, env, headers, sample_values_map, model_name='res.partner'):
        """
        Semantic mapper: Level 3 of the mapping cascade.
        Used only for headers that Hash (Level 1) and Fuzzy (Level 2) could not resolve.
        Never raises: on any problem it logs and returns {} so the import can continue.
        """
        if not headers:
            return {}
        cfg = cls._get_config(env)
        if not cfg['enabled']:
            return {}
        if not cfg['api_key'] and cfg['provider'] != 'custom':
            _logger.warning("LLM mapping is enabled but the API key is missing. Skipping LLM step.")
            return {}

        fields_records = FieldCatalog.importable_fields(env, model_name)
        name_to_id = {f.name: f.id for f in fields_records}
        samples = sample_values_map if cfg['send_samples'] else {}
        prompt = cls._build_mapping_prompt(headers, samples, fields_records, model_name)

        try:
            raw = cls._call_llm(cfg['provider'], cfg['model'], cfg['api_key'], prompt, base_url=cfg['base_url'])
            suggestions = cls._parse_json_array(raw)
        except Exception as e:
            _logger.warning("LLM mapping call failed, skipping LLM step: %s", e)
            return {}

        by_norm = {FileReader.normalize_header(h): h for h in headers}
        results = {}
        for item in suggestions:
            if not isinstance(item, dict):
                continue
            header = by_norm.get(FileReader.normalize_header(item.get('header')))
            field_name = item.get('field_name')
            if not header or header in results or field_name not in name_to_id:
                continue
            results[header] = {
                'target_field_id': name_to_id[field_name],
                'target_subfield': False,
                'confidence': cls._normalize_confidence(item.get('confidence'), default=0.7),
                'source': 'llm',
                'llm_justification': item.get('justification', ''),
            }
        return results

    # ------------------------------------------------------------------
    # Full sheet analysis
    # ------------------------------------------------------------------

    @classmethod
    def analyze_and_structure_template(cls, env, headers, sample_values_map, total_rows=0, filename="", model_name='res.partner'):
        """
        Deep semantic analysis of a sheet: predicts fields, recommends cleansing
        rules, duplicate criteria/policy and a template name.

        Every header of the sheet is present in the returned mappings: columns
        the AI did not mention come back as ignored, so nothing silently vanishes.
        :raises LLMError, ValueError: when the AI cannot be used or answers badly
        """
        cfg = cls._get_config(env)
        if not cfg['enabled']:
            raise ValueError("El servicio de IA no está habilitado en Configuración.")
        if not cfg['api_key'] and cfg['provider'] != 'custom':
            raise ValueError(f"Falta configurar la API Key para el proveedor '{cfg['provider']}'.")

        fields_records = FieldCatalog.importable_fields(env, model_name)
        name_to_id = {f.name: f.id for f in fields_records}

        rules_records = env['import.cleansing.rule'].search([('active', '=', True)])
        available_rules = {r.action_type: r.name for r in rules_records}

        samples = sample_values_map if cfg['send_samples'] else {}
        prompt = cls._build_full_template_prompt(
            headers=headers,
            sample_values_map=samples,
            fields_records=fields_records,
            available_rules=available_rules,
            total_rows=total_rows,
            filename=filename,
            model_name=model_name,
            model_description=env[model_name]._description if model_name in env else model_name,
        )

        raw = cls._call_llm(cfg['provider'], cfg['model'], cfg['api_key'], prompt,
                            base_url=cfg['base_url'], timeout=120, max_tokens=8192)
        parsed = cls._parse_json_object(raw)
        if not parsed:
            snippet = (raw or "").strip()[:200]
            if snippet:
                raise ValueError(f"La IA no devolvió una respuesta con formato JSON válido. Respuesta recibida: {snippet}...")
            raise ValueError("La IA no devolvió una respuesta con formato JSON válido (respuesta vacía del proveedor).")

        by_norm = {FileReader.normalize_header(h): h for h in headers}
        ai_by_header = {}
        for item in parsed.get('mappings') or []:
            if not isinstance(item, dict):
                continue
            header = by_norm.get(FileReader.normalize_header(item.get('header')))
            if header and header not in ai_by_header:
                ai_by_header[header] = item

        rule_ids_by_action = {r.action_type: r.id for r in rules_records}
        validated_mappings = []
        for header in headers:
            item = ai_by_header.get(header)
            if item is None:
                validated_mappings.append({
                    'header': header, 'target_field_id': False, 'target_field_name': False,
                    'confidence': 0.0, 'is_ignored': True, 'rule_ids': [],
                    'justification': "La IA no sugirió un campo para esta columna; revísala manualmente.",
                })
                continue

            field_name = item.get('field_name')
            target_id = name_to_id.get(field_name, False)
            suggested_rules = item.get('cleansing_rules') or []
            if not isinstance(suggested_rules, list):
                suggested_rules = [suggested_rules]
            rule_ids = [rule_ids_by_action[a] for a in suggested_rules if a in rule_ids_by_action]
            justification = item.get('justification', '')
            if field_name and not target_id:
                justification = (f"La IA sugirió '{field_name}', que no existe o no es importable en {model_name}. "
                                 + justification)

            validated_mappings.append({
                'header': header,
                'target_field_id': target_id,
                'target_field_name': field_name if target_id else False,
                'confidence': cls._normalize_confidence(item.get('confidence'), default=0.8),
                'justification': justification,
                'is_ignored': bool(item.get('is_ignored', False)) or not target_id,
                'rule_ids': rule_ids,
            })

        criterion = parsed.get('duplicate_criterion')
        if model_name != 'res.partner' or criterion not in DUPLICATE_CRITERIA:
            criterion = 'vat' if model_name == 'res.partner' else 'auto'

        policy = str(parsed.get('duplicate_policy') or 'update').lower()
        policy = POLICY_ALIASES.get(policy, policy)
        if policy not in DUPLICATE_POLICIES:
            policy = 'update'

        return {
            'template_name': parsed.get('template_name') or f"Plantilla IA - {filename or model_name}",
            'duplicate_criterion': criterion,
            'duplicate_policy': policy,
            'mappings': validated_mappings,
            'summary': parsed.get('analysis_summary') or 'Análisis completado por IA.',
            'provider': cfg['provider'],
            'model': cfg['model'],
        }

    @staticmethod
    def _normalize_confidence(value, default):
        """Accept 0-100 or 0-1 and always return 0.0-1.0."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if number > 1.0:
            number = number / 100.0
        return max(0.0, min(1.0, number))

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    @staticmethod
    def _fields_block(fields_records):
        lines = []
        records = fields_records.sorted('name') if hasattr(fields_records, 'sorted') else sorted(fields_records, key=lambda f: getattr(f, 'name', ''))
        for f in records:
            extra = f" -> {f.relation}" if getattr(f, 'relation', False) else ""
            required = " [obligatorio]" if getattr(f, 'required', False) else ""
            lines.append(f"- {f.name} ({getattr(f, 'ttype', 'char')}{extra}): {getattr(f, 'field_description', f.name)}{required}")
        return "\n".join(lines)

    @classmethod
    def _build_mapping_prompt(cls, headers, sample_values_map, fields_records, model_name):
        columns_list = "\n".join(
            f'- Column "{h}", sample values: {sample_values_map.get(h, [])[:5]}' for h in headers
        )
        return (
            f"You are an expert Odoo data engineer mapping spreadsheet column headers to fields of the Odoo model {model_name}.\n\n"
            f"Available target fields (technical name (type -> related model): label):\n{cls._fields_block(fields_records)}\n\n"
            f"Unmapped columns:\n{columns_list}\n\n"
            "For each column, choose the single best matching field from the list above based on the header "
            "and the sample values. If no field is a reasonable match, omit that column.\n\n"
            "Respond ONLY with a valid JSON array, without markdown or commentary. Each item:\n"
            '[{"header": "original column name", "field_name": "technical_field_name", '
            '"confidence": integer 0-100, "justification": "short justification in Spanish"}]'
        )

    @classmethod
    def _build_full_template_prompt(cls, headers, sample_values_map, fields_records, available_rules,
                                    total_rows, filename, model_name, model_description):
        columns_list = "\n".join(
            f'- Columna "{h}", valores de muestra: {sample_values_map.get(h, [])[:5]}' for h in headers
        )
        rules_list = "\n".join(f"- {action}: {name}" for action, name in available_rules.items())
        partner_rules = ""
        if model_name == 'res.partner':
            partner_rules = (
                "4. Define el criterio de deduplicación: 'vat' si hay identificación tributaria, "
                "'email' si hay correo único, o 'vat_or_email'.\n"
            )
        else:
            partner_rules = "4. Usa 'auto' como criterio de deduplicación (se busca por código o nombre).\n"

        return (
            "Eres un Arquitecto de Datos y Especialista Senior de Odoo 19.\n"
            f"Se cargó la hoja '{filename}' ({total_rows} filas) para importar en el modelo "
            f"{model_name} ({model_description}).\n"
            "Muchas plantillas ya usan el nombre técnico del campo como encabezado (ej. 'vat', 'city_id', 'state_id/id'); "
            "en ese caso mapea a ese mismo campo.\n\n"
            f"CAMPOS DISPONIBLES (nombre técnico (tipo -> modelo relacionado): etiqueta):\n{cls._fields_block(fields_records)}\n\n"
            f"REGLAS DE LIMPIEZA DISPONIBLES:\n{rules_list}\n\n"
            f"COLUMNAS DE LA HOJA Y VALORES DE MUESTRA:\n{columns_list}\n\n"
            "INSTRUCCIONES:\n"
            "1. Incluye TODAS las columnas en 'mappings', incluso las que no correspondan a ningún campo (con is_ignored=true).\n"
            "2. Asigna reglas de limpieza útiles (ej: 'email' para correos, 'phone' para teléfonos, "
            "'vat_document' para NIT/cédula, 'strip' para textos).\n"
            "3. Marca is_ignored=true para columnas auxiliares, fórmulas de apoyo o notas.\n"
            f"{partner_rules}"
            "5. Política de duplicados: 'update' (actualizar existentes), 'skip' (omitir) o 'create' (crear igual).\n"
            "6. Asigna un nombre descriptivo a la plantilla.\n"
            "7. En analysis_summary escribe en español un diagnóstico de calidad: columnas vacías, formatos "
            "inconsistentes, valores que parecen errores y recomendaciones concretas.\n"
            "8. Mantén la justificación de cada columna concisa (1 oración breve) para optimizar la velocidad y el procesamiento.\n\n"
            "RESPONDE ÚNICAMENTE CON UN OBJETO JSON VÁLIDO (sin markdown) con esta estructura:\n"
            "{\n"
            '  "template_name": "Nombre sugerido",\n'
            '  "duplicate_criterion": "vat" | "email" | "vat_or_email" | "auto",\n'
            '  "duplicate_policy": "update" | "skip" | "create",\n'
            '  "analysis_summary": "Diagnóstico en español...",\n'
            '  "mappings": [\n'
            '    {"header": "columna", "field_name": "campo_tecnico", "confidence": 95, "is_ignored": false,\n'
            '     "cleansing_rules": ["strip"], "justification": "por qué"}\n'
            "  ]\n"
            "}"
        )

    # ------------------------------------------------------------------
    # Provider calls
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint(base_url, default, suffix):
        """Accept either a base URL (https://host/v1) or the full endpoint."""
        if not base_url:
            return default
        url = base_url.rstrip('/')
        return url if url.endswith(suffix) else url + suffix

    @classmethod
    def _post(cls, url, headers, payload, timeout, secret=''):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout:
            raise LLMError(f"el proveedor no respondió en {timeout} segundos.")
        except requests.RequestException as e:
            raise LLMError(cls._redact(f"no se pudo conectar ({e.__class__.__name__}): {e}", secret))

        if resp.status_code >= 400:
            detail = resp.text[:400]
            try:
                body = resp.json()
                err = body.get('error') if isinstance(body, dict) else None
                if isinstance(err, dict):
                    detail = err.get('message') or detail
                elif isinstance(err, str):
                    detail = err
            except ValueError:
                pass
            hints = {401: " (API key inválida)", 403: " (sin permiso para este modelo)",
                     404: " (modelo o URL no encontrados)", 429: " (límite de uso o saldo agotado)"}
            raise LLMError(cls._redact(f"HTTP {resp.status_code}{hints.get(resp.status_code, '')}: {detail}", secret))
        try:
            return resp.json()
        except ValueError:
            raise LLMError("la respuesta del proveedor no es JSON.")

    @staticmethod
    def _redact(text, secret):
        return text.replace(secret, '***') if secret else text

    @classmethod
    def _call_llm(cls, provider, model, api_key, prompt, base_url="", timeout=30, max_tokens=1500):
        provider = (provider or '').lower()
        model = model or cls._get_default_model(provider)

        if provider == 'anthropic':
            url = cls._endpoint(base_url, ANTHROPIC_API_URL, '/messages')
            if base_url and not base_url.rstrip('/').endswith(('/v1', '/messages')):
                url = base_url.rstrip('/') + '/v1/messages'
            body = {
                'model': model,
                'max_tokens': max_tokens,
                'messages': [{'role': 'user', 'content': prompt}],
                # Prevent thinking models (e.g. claude-haiku-5-5, claude-3-7-sonnet) from consuming
                # thousands of output tokens on internal monologue, which truncates the JSON response.
                'thinking': {'type': 'disabled'},
            }
            try:
                data = cls._post(url, {
                    'x-api-key': api_key,
                    'anthropic-version': '2023-06-01',
                    'content-type': 'application/json',
                }, body, timeout, api_key)
            except LLMError as e:
                # If an older Anthropic model or proxy rejects the 'thinking' parameter, retry without it
                if 'thinking' in str(e).lower():
                    body.pop('thinking', None)
                    data = cls._post(url, {
                        'x-api-key': api_key,
                        'anthropic-version': '2023-06-01',
                        'content-type': 'application/json',
                    }, body, timeout, api_key)
                else:
                    raise
            return "".join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')

        if provider in ('openai', 'deepseek', 'custom') or (provider == 'gemini' and base_url):
            if provider == 'custom' and not base_url:
                raise LLMError("el proveedor personalizado requiere la URL base (ej. http://localhost:11434/v1).")
            default = {'openai': OPENAI_API_URL, 'deepseek': DEEPSEEK_API_URL}.get(provider, '')
            url = cls._endpoint(base_url, default, '/chat/completions')
            payload = {'model': model, 'messages': [{'role': 'user', 'content': prompt}]}
            if provider == 'openai':
                payload['max_completion_tokens'] = max_tokens
                payload['response_format'] = {'type': 'json_object'}
            else:
                payload['max_tokens'] = max_tokens
                payload['temperature'] = 0.1
                if provider == 'deepseek':
                    payload['response_format'] = {'type': 'json_object'}
            headers = {'content-type': 'application/json'}
            if api_key:
                headers['Authorization'] = f'Bearer {api_key}'
            data = cls._post(url, headers, payload, timeout, api_key)
            try:
                return data['choices'][0]['message']['content'] or ''
            except (KeyError, IndexError, TypeError):
                raise LLMError("respuesta sin contenido (formato inesperado).")

        if provider == 'gemini':
            # The key goes in a header, never in the URL, so it cannot leak into logs or error messages
            url = GEMINI_API_URL_TEMPLATE.format(model=model)
            data = cls._post(url, {'content-type': 'application/json', 'x-goog-api-key': api_key}, {
                'contents': [{'parts': [{'text': prompt}]}],
                'generationConfig': {
                    'temperature': 0.1,
                    'maxOutputTokens': max_tokens,
                    'responseMimeType': 'application/json',
                },
            }, timeout, api_key)
            candidates = data.get('candidates', [])
            if not candidates:
                raise LLMError("Gemini no generó respuesta (posible bloqueo de seguridad o cuota).")
            parts = candidates[0].get('content', {}).get('parts', [])
            return "".join(p.get('text', '') for p in parts)

        raise LLMError(f"proveedor de IA no soportado: {provider}")

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @classmethod
    def _clean_json_text(cls, raw_text):
        if not raw_text:
            return ""
        text = raw_text.strip()
        # Strip reasoning / thinking tags (e.g. DeepSeek-R1, Qwen)
        text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE).strip()
        # Extract markdown code blocks
        if "```" in text:
            match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.IGNORECASE)
            if match:
                text = match.group(1).strip()
            else:
                text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE).strip()
                text = re.sub(r'```$', '', text).strip()
        return text

    @classmethod
    def _remove_trailing_commas(cls, text):
        return re.sub(r',\s*([\]}])', r'\1', text)

    @classmethod
    def _parse_json_array(cls, raw_text):
        cleaned = cls._clean_json_text(raw_text)
        if not cleaned:
            return []
        try:
            parsed = json.loads(cleaned, strict=False)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        cleaned_no_commas = cls._remove_trailing_commas(cleaned)
        try:
            parsed = json.loads(cleaned_no_commas, strict=False)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

        start, end = cleaned_no_commas.find('['), cleaned_no_commas.rfind(']')
        if start != -1 and end > start:
            try:
                parsed = json.loads(cleaned_no_commas[start:end + 1], strict=False)
                if isinstance(parsed, list):
                    return parsed
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
            parsed = json.loads(cleaned, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        cleaned_no_commas = cls._remove_trailing_commas(cleaned)
        try:
            parsed = json.loads(cleaned_no_commas, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        start, end = cleaned_no_commas.find('{'), cleaned_no_commas.rfind('}')
        if start != -1 and end > start:
            try:
                parsed = json.loads(cleaned_no_commas[start:end + 1], strict=False)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        # If truncated inside 'mappings', attempt graceful recovery of completed items
        if start != -1 and '"mappings"' in cleaned_no_commas:
            last_brace = cleaned_no_commas.rfind('}')
            if last_brace > start:
                candidate = cleaned_no_commas[start:last_brace + 1].rstrip().rstrip(',') + '\n  ]\n}'
                candidate = cls._remove_trailing_commas(candidate)
                try:
                    parsed = json.loads(candidate, strict=False)
                    if isinstance(parsed, dict) and 'mappings' in parsed:
                        _logger.info("Recuperado JSON truncado del LLM con %d columnas mapeadas.", len(parsed.get('mappings', [])))
                        return parsed
                except json.JSONDecodeError:
                    pass

        _logger.warning("No se pudo parsear el objeto JSON del LLM: %s", cleaned[:300])
        return {}
