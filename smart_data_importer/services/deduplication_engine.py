import pandas as pd
import logging
from .cleansing_engine import CleansingEngine

_logger = logging.getLogger(__name__)

class DeduplicationEngine:
    """Service responsible for detecting and resolving duplicates in import sessions."""

    @classmethod
    def analyze_file_duplicates(cls, df, session):
        """
        Analyzes the DataFrame to count internal duplicate rows based on the selected criterion.
        :param df: pandas DataFrame of the uploaded file
        :param session: import.session record
        :return: int (number of duplicated rows in file)
        """
        if df is None or df.empty:
            return 0

        criterion = session.duplicate_criterion or 'vat'
        mappings = session.column_mapping_ids.filtered(lambda m: not m.is_ignored and m.target_field_id)
        
        # Find which columns map to 'vat' and 'email'
        vat_headers = [m for m in mappings if m.target_field_id.name == 'vat']
        email_headers = [m for m in mappings if m.target_field_id.name == 'email']

        cols_to_check = []
        df_copy = pd.DataFrame()

        if criterion in ('vat', 'vat_or_email') and vat_headers:
            h = vat_headers[0].source_header
            rules = vat_headers[0].cleansing_rule_ids
            # Clean column values for accurate matching
            df_copy['vat_clean'] = df[h].apply(lambda x: CleansingEngine.apply_rules(x, rules) if pd.notna(x) else '')
            cols_to_check.append('vat_clean')

        if criterion in ('email', 'vat_or_email') and email_headers:
            h = email_headers[0].source_header
            rules = email_headers[0].cleansing_rule_ids
            df_copy['email_clean'] = df[h].apply(lambda x: CleansingEngine.apply_rules(x, rules) if pd.notna(x) else '')
            cols_to_check.append('email_clean')

        if not cols_to_check:
            return 0

        if criterion == 'vat_or_email' and 'vat_clean' in df_copy and 'email_clean' in df_copy:
            # Filter non-empty values
            vat_dups = df_copy[df_copy['vat_clean'] != '']['vat_clean'].duplicated().sum()
            email_dups = df_copy[df_copy['email_clean'] != '']['email_clean'].duplicated().sum()
            return int(max(vat_dups, email_dups))
        else:
            primary_col = cols_to_check[0]
            non_empty = df_copy[df_copy[primary_col] != '']
            return int(non_empty[primary_col].duplicated().sum())

    @classmethod
    def find_existing_partner(cls, env, row_dict, criterion='vat'):
        """
        Searches for an existing res.partner in Odoo matching row_dict based on criterion.
        :param env: Odoo environment
        :param row_dict: dictionary of mapped values for res.partner
        :param criterion: 'vat', 'email', or 'vat_or_email'
        :return: res.partner recordset (single record or empty)
        """
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

        partner = env['res.partner'].search(domain, limit=1)
        return partner
