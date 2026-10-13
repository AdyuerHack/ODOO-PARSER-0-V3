import logging

import pandas as pd

from .cleansing_engine import CleansingEngine

_logger = logging.getLogger(__name__)

PARTNER_CRITERIA = ('vat', 'email', 'vat_or_email')
# Fields tried, in order, to recognise an existing record of any other model
GENERIC_KEY_FIELDS = ('code', 'default_code', 'code_prefix_start', 'name')


class DeduplicationEngine:
    """Detects duplicates inside the file and against existing Odoo records."""

    # ------------------------------------------------------------------
    # Inside the file
    # ------------------------------------------------------------------

    @classmethod
    def analyze_file_duplicates(cls, df, session):
        """Count rows of the file that repeat the duplicate key of an earlier row."""
        if df is None or df.empty:
            return 0

        model_name = session.target_model_id.model if 'target_model_id' in session._fields else 'res.partner'
        criterion = session.duplicate_criterion or 'auto'
        mappings = session.column_mapping_ids.filtered(lambda m: not m.is_ignored and m.target_field_id)
        by_field = {}
        for m in mappings:
            by_field.setdefault(m.target_field_id.name, m)

        def cleaned(mapping):
            rules = mapping.cleansing_rule_ids
            return df[mapping.source_header].apply(
                lambda x: CleansingEngine.apply_rules(x, rules) if pd.notna(x) else ''
            )

        if model_name == 'res.partner' and criterion in PARTNER_CRITERIA:
            counts = []
            if criterion in ('vat', 'vat_or_email') and 'vat' in by_field:
                counts.append(cls._count_dups(cleaned(by_field['vat'])))
            if criterion in ('email', 'vat_or_email') and 'email' in by_field:
                counts.append(cls._count_dups(cleaned(by_field['email']).str.lower()))
            return max(counts) if counts else 0

        key_field = cls._generic_key(by_field)
        if not key_field:
            return 0
        return cls._count_dups(cleaned(by_field[key_field]))

    @staticmethod
    def _count_dups(series):
        non_empty = series[series != '']
        return int(non_empty.duplicated().sum())

    @staticmethod
    def _generic_key(available_fields):
        return next((f for f in GENERIC_KEY_FIELDS if f in available_fields), None)

    # ------------------------------------------------------------------
    # Against Odoo
    # ------------------------------------------------------------------

    @classmethod
    def find_existing(cls, env, model_name, row_dict, criterion='auto'):
        """Return the existing record matching row_dict (or an empty recordset)."""
        Model = env[model_name]
        if model_name == 'res.partner' and criterion in PARTNER_CRITERIA:
            return cls.find_existing_partner(env, row_dict, criterion)

        key_field = cls._generic_key({k for k in row_dict if row_dict.get(k)})
        if not key_field or key_field not in Model._fields:
            return Model.browse()
        value = row_dict[key_field]
        operator = '=ilike' if isinstance(value, str) and key_field == 'name' else '='
        return Model.search([(key_field, operator, value)], limit=1)

    @classmethod
    def find_existing_partner(cls, env, row_dict, criterion='vat'):
        # Contacts of a company usually repeat the company's NIT. Matching them by
        # NIT would overwrite the company, so they are matched by parent + name.
        parent_id = row_dict.get('parent_id')
        name = (row_dict.get('name') or '').strip() if isinstance(row_dict.get('name'), str) else ''
        if parent_id and name:
            return env['res.partner'].search(
                [('parent_id', '=', parent_id), ('name', '=ilike', name)], limit=1)

        vat = (row_dict.get('vat') or '').strip()
        email = (row_dict.get('email') or '').strip()

        domain = []
        if criterion == 'vat' and vat:
            domain = [('vat', '=', vat)]
        elif criterion == 'email' and email:
            domain = [('email', '=ilike', email)]
        elif criterion == 'vat_or_email':
            if vat and email:
                domain = ['|', ('vat', '=', vat), ('email', '=ilike', email)]
            elif vat:
                domain = [('vat', '=', vat)]
            elif email:
                domain = [('email', '=ilike', email)]

        if not domain:
            return env['res.partner']
        return env['res.partner'].search(domain, limit=1)
