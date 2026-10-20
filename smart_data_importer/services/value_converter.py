import datetime
import logging
import re

from .file_reader import FileReader

_logger = logging.getLogger(__name__)

TRUE_VALUES = {'1', 'true', 'verdadero', 'si', 'yes', 'y', 's', 'x', 'activo', 'active'}
FALSE_VALUES = {'0', 'false', 'falso', 'no', 'n', 'inactivo', 'inactive'}

DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%m/%d/%Y', '%d/%m/%y', '%Y%m%d')
DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M')


class ConversionError(ValueError):
    """A cell value cannot be converted to the target field type."""


class ValueConverter:
    """Turns cleaned text values into values Odoo can write.

    One instance per import run: it caches relational lookups so the same
    city or account is searched only once.
    """

    def __init__(self, env, model_name):
        self.env = env
        self.model_name = model_name
        self.model = env[model_name]
        self._m2o_cache = {}
        self._selection_cache = {}

    # ------------------------------------------------------------------
    def convert(self, field_name, value, subfield=None):
        """Return the Odoo value for ``value`` or raise ConversionError."""
        field = self.model._fields.get(field_name)
        if field is None:
            raise ConversionError("El campo '%s' no existe en %s." % (field_name, self.model_name))

        if value is None:
            return False
        text = str(value).strip()
        if text == '':
            return False

        ftype = field.type
        if ftype in ('char', 'text', 'html'):
            return text
        if ftype == 'boolean':
            return self._to_bool(text, field)
        if ftype == 'integer':
            return int(self._to_number(text, field))
        if ftype in ('float', 'monetary'):
            return self._to_number(text, field)
        if ftype == 'date':
            return self._to_date(text, field)
        if ftype == 'datetime':
            return self._to_datetime(text, field)
        if ftype == 'selection':
            return self._to_selection(text, field)
        if ftype == 'many2one':
            return self._to_many2one(text, field, subfield)
        if ftype == 'many2many':
            ids = [self._to_many2one(part.strip(), field, subfield)
                   for part in re.split(r'[,;]', text) if part.strip()]
            return [(6, 0, ids)]
        raise ConversionError("El tipo de campo '%s' (%s) todavía no se puede importar." % (ftype, field_name))

    # ------------------------------------------------------------------
    def _label(self, field):
        return "%s (%s)" % (field.string, field.name)

    def _to_bool(self, text, field):
        norm = FileReader.normalize_header(text)
        if norm in TRUE_VALUES:
            return True
        if norm in FALSE_VALUES:
            return False
        raise ConversionError(
            "Valor '%s' no válido para %s: se espera 1/0, Sí/No o Verdadero/Falso." % (text, self._label(field))
        )

    def _to_number(self, text, field):
        clean = text.replace(' ', '').replace('$', '')
        if clean.endswith('%'):
            clean = clean[:-1]
        if ',' in clean and '.' in clean:
            # 1.250,50 -> 1250.50 ; 1,250.50 -> 1250.50
            if clean.rfind(',') > clean.rfind('.'):
                clean = clean.replace('.', '').replace(',', '.')
            else:
                clean = clean.replace(',', '')
        elif ',' in clean:
            clean = clean.replace(',', '.')
        try:
            return float(clean)
        except ValueError:
            raise ConversionError("Valor '%s' no es un número válido para %s." % (text, self._label(field)))

    def _to_date(self, text, field):
        for fmt in DATE_FORMATS:
            try:
                return datetime.datetime.strptime(text[:10] if fmt == '%Y-%m-%d' else text, fmt).date()
            except ValueError:
                continue
        raise ConversionError("Fecha '%s' no reconocida para %s (use AAAA-MM-DD)." % (text, self._label(field)))

    def _to_datetime(self, text, field):
        for fmt in DATETIME_FORMATS:
            try:
                return datetime.datetime.strptime(text, fmt)
            except ValueError:
                continue
        return datetime.datetime.combine(self._to_date(text, field), datetime.time(0, 0))

    # ------------------------------------------------------------------
    def _selection_options(self, field):
        if field.name in self._selection_cache:
            return self._selection_cache[field.name]
        options = {}
        langs = [code for code, _name in self.env['res.lang'].get_installed()] or ['en_US']
        for lang in set(langs + ['en_US']):
            model = self.model.with_context(lang=lang)
            try:
                selection = model._fields[field.name]._description_selection(model.env)
            except Exception:
                selection = field.selection if isinstance(field.selection, list) else []
            for key, label in selection or []:
                options[FileReader.normalize_header(key)] = key
                options[FileReader.normalize_header(label)] = key
        self._selection_cache[field.name] = options
        return options

    def _to_selection(self, text, field):
        options = self._selection_options(field)
        key = options.get(FileReader.normalize_header(text))
        if key is not None:
            return key
        labels = sorted({k for k in options.values()})
        raise ConversionError(
            "Valor '%s' no es una opción válida para %s. Opciones: %s"
            % (text, self._label(field), ', '.join(map(str, labels)))
        )

    # ------------------------------------------------------------------
    def _to_many2one(self, text, field, subfield=None):
        comodel_name = field.comodel_name
        cache_key = (comodel_name, subfield, text.lower())
        if cache_key in self._m2o_cache:
            return self._m2o_cache[cache_key]

        if subfield == 'id':
            record = self.env.ref(text, raise_if_not_found=False)
            if not record or record._name != comodel_name:
                raise ConversionError("No existe el identificador externo '%s' en %s." % (text, comodel_name))
            self._m2o_cache[cache_key] = record.id
            return record.id

        comodel = self.env[comodel_name].with_context(active_test=False)
        record = self._find_record(comodel, text)
        if not record:
            raise ConversionError(
                "No se encontró '%s' en %s para el campo %s." % (text, comodel._description, self._label(field))
            )
        if len(record) > 1:
            raise ConversionError(
                "'%s' es ambiguo en %s (%s coincidencias) para %s."
                % (text, comodel._description, len(record), self._label(field))
            )
        self._m2o_cache[cache_key] = record.id
        return record.id

    def _find_record(self, comodel, text):
        fields = comodel._fields
        rec_name = comodel._rec_name or 'name'
        searches = []
        if rec_name in fields:
            searches.append([(rec_name, '=ilike', text)])
        if 'code' in fields:
            searches.append([('code', '=', text)])
            # "240810 IVA Descontable" -> code 240810
            first = text.split()[0]
            if first != text and first.isdigit():
                searches.append([('code', '=', first)])
        if 'default_code' in fields:
            searches.append([('default_code', '=', text)])
        for domain in searches:
            try:
                found = comodel.search(domain, limit=2)
            except Exception:
                continue
            if found:
                return found
        try:
            pairs = comodel.name_search(text, operator='=ilike', limit=2)
        except Exception:
            pairs = []
        if pairs:
            return comodel.browse([p[0] for p in pairs])
        return comodel.browse()
