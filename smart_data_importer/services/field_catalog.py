import re

# Fields that never make sense as an import target
MAGIC_FIELDS = {
    'id', 'display_name', 'create_uid', 'create_date', 'write_uid', 'write_date',
    '__last_update',
}
TECHNICAL_PREFIXES = ('message_', 'activity_', 'website_message', 'rating_', '__')
UNSUPPORTED_TYPES = {'one2many', 'binary', 'reference', 'many2one_reference', 'properties', 'properties_definition', 'json'}

_IDENTIFIER = re.compile(r'^[a-z_][a-z0-9_]*$')


def split_header(header):
    """Split an Odoo-style import header into (field_name, subfield).

    'state_id/id' -> ('state_id', 'id')
    'bank_ids/acc_number' -> ('bank_ids', 'acc_number')
    'Name' -> ('name', None)
    Headers that are not technical identifiers return (None, None).
    """
    if header is None:
        return None, None
    text = str(header).strip()
    base, _sep, sub = text.partition('/')
    base = base.strip().lower()
    if not _IDENTIFIER.match(base):
        return None, None
    sub = sub.strip() or None
    return base, sub


class FieldCatalog:
    """Helpers to know which fields of a model can receive imported values."""

    @classmethod
    def importable_fields(cls, env, model_name):
        """ir.model.fields recordset of fields that can be written by an import."""
        if model_name not in env:
            return env['ir.model.fields']
        model_fields = env[model_name]._fields
        records = env['ir.model.fields'].search([('model_id.model', '=', model_name)])

        def keep(rec):
            if rec.name in MAGIC_FIELDS or rec.name.startswith(TECHNICAL_PREFIXES):
                return False
            if rec.ttype in UNSUPPORTED_TYPES:
                return False
            field = model_fields.get(rec.name)
            if field is None:
                return False
            # Computed fields are only writable when they have an inverse
            if field.compute and not field.inverse and field.readonly:
                return False
            if field.related and field.readonly:
                return False
            # Stored fields, or company-dependent ones (property_* in Odoo 19 are not stored as columns)
            return field.store or getattr(field, 'company_dependent', False) or bool(field.inverse)

        return records.filtered(keep)

    @classmethod
    def detect_model(cls, env, headers):
        """Guess the target model from technical column headers.

        Returns (model_name, score) where score is the share of technical
        headers that exist as fields on that model, or (False, 0.0).
        """
        names = []
        for header in headers:
            base, _sub = split_header(header)
            if base and base not in names:
                names.append(base)
        if len(names) < 2:
            return False, 0.0

        rows = env['ir.model.fields'].sudo().search_read(
            [('name', 'in', names), ('model_id.transient', '=', False)],
            ['name', 'model'],
        )
        hits = {}
        for row in rows:
            hits.setdefault(row['model'], set()).add(row['name'])

        candidates = []
        for model_name, matched in hits.items():
            if model_name not in env or env[model_name]._abstract:
                continue
            if model_name.startswith(('ir.', 'base.', 'bus.', 'mail.', 'res.config')):
                continue
            # The first column of a template is normally the record name: weigh it more
            weight = len(matched) + (1.5 if names[0] in matched else 0)
            # Fewer total fields = more specific model, used to break ties
            # (e.g. res.partner vs res.users, which inherits all partner fields)
            size = len(env[model_name]._fields)
            candidates.append((weight, -size, len(matched), model_name))

        if not candidates:
            return False, 0.0
        candidates.sort(reverse=True)
        _weight, _size, count, model_name = candidates[0]
        score = count / len(names)
        if count < 2 or score < 0.3:
            return False, score
        return model_name, round(score, 2)
