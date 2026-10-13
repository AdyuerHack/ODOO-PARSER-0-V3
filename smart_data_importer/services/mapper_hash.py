from .field_catalog import FieldCatalog, split_header
from .file_reader import FileReader


class HashMapper:
    """Deterministic mapper with O(1) resolution.

    Level 0: the header is already the technical field name (e.g. 'vat',
             'state_id/id'), which is how the base templates are written.
    Level 1: the header is a known synonym in mapping.registry (e.g. 'NIT').
    """

    @classmethod
    def map(cls, env, headers, model_name='res.partner'):
        """
        Returns {original_header: {'target_field_id', 'target_subfield', 'confidence', 'source'}}
        """
        results = cls.map_technical(env, headers, model_name=model_name)

        # Level 1 - synonym registry
        registry_records = env['mapping.registry'].search(
            [('model_id.model', '=', model_name)], order='priority desc, id'
        )
        lookup = {}
        for reg in registry_records:
            norm_syn = FileReader.normalize_header(reg.synonym)
            if norm_syn not in lookup:  # first one wins = highest priority
                lookup[norm_syn] = reg.field_id.id

        for header in headers:
            if header in results:
                continue
            norm_header = FileReader.normalize_header(header)
            if norm_header in lookup:
                results[header] = {
                    'target_field_id': lookup[norm_header],
                    'target_subfield': False,
                    'confidence': 1.0,
                    'source': 'hash',
                }

        return results

    @classmethod
    def map_technical(cls, env, headers, model_name='res.partner'):
        """Level 0 only: headers that are exactly a field name of the model."""
        importable = FieldCatalog.importable_fields(env, model_name)
        by_name = {f.name: f.id for f in importable}
        results = {}
        for header in headers:
            base, sub = split_header(header)
            if base in by_name and sub in (None, 'id'):
                results[header] = {
                    'target_field_id': by_name[base],
                    'target_subfield': sub or False,
                    'confidence': 1.0,
                    'source': 'hash',
                }
        return results
