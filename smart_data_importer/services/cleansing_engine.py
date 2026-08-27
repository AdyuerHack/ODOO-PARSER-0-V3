import re
import unicodedata
from datetime import datetime
import pandas as pd
import logging
from odoo.tools.safe_eval import safe_eval

_logger = logging.getLogger(__name__)

class CleansingEngine:
    """
    Extensible Data Cleansing & Normalization Engine.
    Uses a dynamic Handler Registry pattern (@register_handler).
    """

    _HANDLERS = {}

    @classmethod
    def register_handler(cls, action_type):
        """Decorator to register a transformation handler for an action_type."""
        def decorator(func):
            cls._HANDLERS[action_type] = func
            return func
        return decorator

    @classmethod
    def apply_rules(cls, value, rules):
        """
        Applies a sequence of import.cleansing.rule records to a single value.
        :param value: raw value (str, int, float, etc.)
        :param rules: recordset or iterable of import.cleansing.rule
        :return: transformed value
        """
        if value is None or (isinstance(value, float) and pd.isna(value)):
            # Check if any rule handles default value for empty inputs
            current_val = ''
        else:
            current_val = str(value)

        for rule in rules:
            handler = cls._HANDLERS.get(rule.action_type)
            if not handler:
                _logger.warning("No handler registered for cleansing action '%s'", rule.action_type)
                continue

            try:
                current_val = handler(current_val, rule)
            except Exception as e:
                _logger.warning("Error applying rule '%s' (%s) on value '%s': %s",
                               rule.name, rule.action_type, current_val, str(e))

        return current_val

    # =========================================================================
    # HANDLERS - Text & Formatting
    # =========================================================================

    @classmethod
    def _handle_strip(cls, val, rule):
        return val.strip() if val else val

    @classmethod
    def _handle_lowercase(cls, val, rule):
        return val.lower() if val else val

    @classmethod
    def _handle_uppercase(cls, val, rule):
        return val.upper() if val else val

    @classmethod
    def _handle_titlecase(cls, val, rule):
        return val.title() if val else val

    @classmethod
    def _handle_remove_accents(cls, val, rule):
        if not val:
            return val
        return ''.join(c for c in unicodedata.normalize('NFD', val) if unicodedata.category(c) != 'Mn')

    @classmethod
    def _handle_digits_only(cls, val, rule):
        if not val:
            return val
        return re.sub(r'\D', '', val)

    @classmethod
    def _handle_find_replace(cls, val, rule):
        if not val or not rule.find_text:
            return val
        find_txt = rule.find_text
        replace_txt = rule.replace_text or ''
        if rule.case_sensitive:
            return val.replace(find_txt, replace_txt)
        else:
            pattern = re.compile(re.escape(find_txt), re.IGNORECASE)
            return pattern.sub(replace_txt, val)

    @classmethod
    def _handle_prefix_suffix(cls, val, rule):
        if not val:
            return val
        res = val
        # Prefix handling
        if rule.prefix_text:
            p_text = rule.prefix_text
            if rule.remove_prefix:
                if res.startswith(p_text):
                    res = res[len(p_text):]
            else:
                if not res.startswith(p_text):
                    res = f"{p_text}{res}"

        # Suffix handling
        if rule.suffix_text:
            s_text = rule.suffix_text
            if rule.remove_suffix:
                if res.endswith(s_text):
                    res = res[:-len(s_text)]
            else:
                if not res.endswith(s_text):
                    res = f"{res}{s_text}"
        return res

    @classmethod
    def _handle_value_mapping(cls, val, rule):
        if not val or not rule.line_ids:
            return val
        clean_val = val.strip()
        for line in rule.line_ids:
            if clean_val.lower() == (line.source_value or '').strip().lower():
                return line.target_value
        return val

    @classmethod
    def _handle_default_value(cls, val, rule):
        if not val or val.strip() == '':
            return rule.default_value or ''
        return val

    # =========================================================================
    # HANDLERS - Contacts & Tax IDs
    # =========================================================================

    @classmethod
    def _handle_email(cls, val, rule):
        if not val:
            return val
        email_str = val.strip().lower()
        email_str = re.sub(r'^[<"\'\s]+|[>"\'\s]+$', '', email_str)
        match = re.search(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', email_str)
        return match.group(0) if match else email_str

    @classmethod
    def _handle_phone(cls, val, rule):
        if not val:
            return val
        phone_str = val.strip()
        has_plus = phone_str.startswith('+')
        digits = re.sub(r'\D', '', phone_str)
        if not digits:
            return ''
        return f"+{digits}" if has_plus else digits

    @classmethod
    def _handle_vat_document(cls, val, rule):
        if not val:
            return val
        vat_str = val.strip().upper()
        # Remove internal spaces and dots (NIT/CC standard)
        return re.sub(r'[\s.]', '', vat_str)

    # =========================================================================
    # HANDLERS - Numbers, Currency & Dates
    # =========================================================================

    @classmethod
    def _handle_number_clean(cls, val, rule):
        if not val:
            return val
        clean_str = val.strip()
        # Remove currency symbols ($ € £ COP USD etc.)
        clean_str = re.sub(r'[$€£]|COP|USD|EUR', '', clean_str, flags=re.IGNORECASE).strip()
        
        if rule.decimal_separator == 'comma':
            # e.g. 1.250.000,50 -> remove dots, replace comma with dot
            clean_str = clean_str.replace(' ', '').replace('.', '')
            clean_str = clean_str.replace(',', '.')
        else:
            # e.g. 1,250,000.50 -> remove commas and spaces
            clean_str = clean_str.replace(' ', '').replace(',', '')

        # Keep only numbers, negative sign, and decimal dot
        clean_str = re.sub(r'[^\d.-]', '', clean_str)
        return clean_str

    @classmethod
    def _handle_date_format(cls, val, rule):
        if not val:
            return val
        date_str = val.strip()
        # Common date formats
        formats_to_try = []
        if rule.date_input_format and rule.date_input_format != 'auto':
            formats_to_try.append(rule.date_input_format)
        else:
            formats_to_try = [
                '%d/%m/%Y', '%Y-%m-%d', '%m/%d/%Y', '%d-%m-%Y',
                '%Y/%m/%d', '%Y%m%d', '%d/%m/%y', '%m/%d/%y'
            ]

        for fmt in formats_to_try:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime('%Y-%m-%d')
            except ValueError:
                continue

        return date_str

    # =========================================================================
    # HANDLERS - Advanced / Custom
    # =========================================================================

    @classmethod
    def _handle_regex(cls, val, rule):
        if not val or not rule.regex_pattern:
            return val
        replacement = rule.regex_replacement or ''
        return re.sub(rule.regex_pattern, replacement, val)

    @classmethod
    def _handle_python_code(cls, val, rule):
        if not rule.python_code:
            return val
        local_dict = {
            'value': val,
            'rule': rule,
            're': re,
            'result': val
        }
        try:
            safe_eval(rule.python_code, local_dict, mode="exec", nocopy=True)
            res = local_dict.get('result', val)
            return str(res) if res is not None else ''
        except Exception as e:
            _logger.warning("Error executing custom Python code in rule '%s': %s", rule.name, str(e))
            return val


# Register default handlers dynamically
CleansingEngine.register_handler('strip')(CleansingEngine._handle_strip)
CleansingEngine.register_handler('lowercase')(CleansingEngine._handle_lowercase)
CleansingEngine.register_handler('uppercase')(CleansingEngine._handle_uppercase)
CleansingEngine.register_handler('titlecase')(CleansingEngine._handle_titlecase)
CleansingEngine.register_handler('remove_accents')(CleansingEngine._handle_remove_accents)
CleansingEngine.register_handler('digits_only')(CleansingEngine._handle_digits_only)
CleansingEngine.register_handler('find_replace')(CleansingEngine._handle_find_replace)
CleansingEngine.register_handler('prefix_suffix')(CleansingEngine._handle_prefix_suffix)
CleansingEngine.register_handler('value_mapping')(CleansingEngine._handle_value_mapping)
CleansingEngine.register_handler('default_value')(CleansingEngine._handle_default_value)
CleansingEngine.register_handler('email')(CleansingEngine._handle_email)
CleansingEngine.register_handler('phone')(CleansingEngine._handle_phone)
CleansingEngine.register_handler('vat_document')(CleansingEngine._handle_vat_document)
CleansingEngine.register_handler('number_clean')(CleansingEngine._handle_number_clean)
CleansingEngine.register_handler('date_format')(CleansingEngine._handle_date_format)
CleansingEngine.register_handler('regex')(CleansingEngine._handle_regex)
CleansingEngine.register_handler('python_code')(CleansingEngine._handle_python_code)
