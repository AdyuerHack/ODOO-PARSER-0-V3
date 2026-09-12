import json
import logging
import requests

_logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


class LLMMapper:
    """
    Semantic mapper: Level 3 of the mapping cascade.
    Used only for headers that Hash (Level 1) and Fuzzy (Level 2) could not resolve.
    Sends the header name + sample values + available Odoo fields to the configured
    LLM provider and asks it to infer the best matching field, with a justification.
    """

    @classmethod
    def map(cls, env, headers, sample_values_map, model_name='res.partner'):
        """
        :param env: Odoo environment
        :param headers: list of unresolved source headers (strings)
        :param sample_values_map: dict {header: [sample value, ...]}
        :param model_name: target Odoo model, e.g. 'res.partner'
        :return: dict {header: {'target_field_id', 'confidence', 'source': 'llm', 'llm_justification'}}
        """
        if not headers:
            return {}

        config = env['ir.config_parameter'].sudo()
        if config.get_param('smart_data_importer.llm_enabled', 'False') != 'True':
            return {}

        provider = config.get_param('smart_data_importer.llm_provider', 'anthropic')
        model = config.get_param('smart_data_importer.llm_model')
        api_key = config.get_param('smart_data_importer.llm_api_key')

        if not api_key or not model:
            _logger.warning(
                "LLM mapping is enabled but provider/model/api_key is not fully configured. Skipping LLM step."
            )
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

        prompt = cls._build_prompt(headers, sample_values_map, field_options)

        try:
            raw_response = cls._call_llm(provider, model, api_key, prompt)
            suggestions = cls._parse_response(raw_response)
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
                confidence_val = 0.5

            results[header] = {
                'target_field_id': name_to_id[field_name],
                'confidence': confidence_val,
                'source': 'llm',
                'llm_justification': justification,
            }

        return results

    @classmethod
    def _build_prompt(cls, headers, sample_values_map, field_options):
        fields_list = "\n".join(f"- {name}: {desc}" for name, desc in sorted(field_options.items()))
        columns_list = "\n".join(
            f'- Column "{h}", sample values: {sample_values_map.get(h, [])}' for h in headers
        )
        return (
            "You are mapping spreadsheet column headers to Odoo res.partner fields.\n\n"
            f"Available target fields:\n{fields_list}\n\n"
            f"Unmapped columns:\n{columns_list}\n\n"
            "For each column, decide the single best matching field from the list above based on "
            "the header name and sample values. If no field is a reasonable match, omit that column "
            "from the response.\n\n"
            "Respond ONLY with a JSON array, no prose, no markdown fences. Each item must have exactly "
            "these keys: \"header\" (the original column name), \"field_name\" (the technical field "
            "name from the list), \"confidence\" (integer 0-100), \"justification\" (one short sentence)."
        )

    @classmethod
    def _call_llm(cls, provider, model, api_key, prompt):
        if provider == 'anthropic':
            headers = {
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            }
            payload = {
                'model': model,
                'max_tokens': 1024,
                'messages': [{'role': 'user', 'content': prompt}],
            }
            resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return "".join(
                block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text'
            )

        elif provider == 'openai':
            headers = {
                'Authorization': f'Bearer {api_key}',
                'content-type': 'application/json',
            }
            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'max_tokens': 1024,
            }
            resp = requests.post(OPENAI_API_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return data['choices'][0]['message']['content']

        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

    @classmethod
    def _parse_response(cls, raw_text):
        if not raw_text:
            return []
        text = raw_text.strip()
        if text.startswith('```'):
            text = text.strip('`')
            if text.startswith('json'):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            _logger.warning("LLM response was not valid JSON: %s", text[:300])
            return []
        return parsed if isinstance(parsed, list) else []