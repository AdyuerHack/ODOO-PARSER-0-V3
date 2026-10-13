from rapidfuzz import process, fuzz

from .field_catalog import FieldCatalog, split_header
from .file_reader import FileReader


class FuzzyMapper:
    """Approximate mapper using Levenshtein-based similarity."""

    @classmethod
    def map(cls, env, headers, model_name='res.partner', threshold=85):
        """
        Returns a dictionary for headers that matched above the threshold.
        Only importable fields are candidates, so technical fields such as
        create_uid or message_ids are never suggested.
        """
        fields_records = FieldCatalog.importable_fields(env, model_name)

        field_choices = {}
        for f in fields_records:
            for label in (f.field_description, f.name):
                norm = FileReader.normalize_header(label)
                if norm and norm not in field_choices:
                    field_choices[norm] = f.id

        choices_list = list(field_choices.keys())
        if not choices_list:
            return {}

        results = {}
        for header in headers:
            norm_header = FileReader.normalize_header(header)
            if not norm_header:
                continue
            # Technical headers (type_regime_id) of fields that don't exist here must not
            # partially match a short field name (type), so they use a full-string ratio.
            base, _sub = split_header(header)
            scorer = fuzz.ratio if base and '_' in base else fuzz.WRatio
            match = process.extractOne(norm_header, choices_list, scorer=scorer)
            if match:
                best_string, score, _idx = match
                if score >= threshold:
                    results[header] = {
                        'target_field_id': field_choices[best_string],
                        'target_subfield': False,
                        'confidence': round(score / 100.0, 2),
                        'source': 'fuzzy',
                    }
        return results
