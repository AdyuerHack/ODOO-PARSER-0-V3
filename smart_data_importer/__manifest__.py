{
    'name': 'Odoo Multi-Format Data Importer',
    'version': '19.0.1.1.0',
    'summary': 'Import Odoo base templates (contacts, accounts, taxes, products...) from CSV/Excel with AI-assisted mapping.',
    'category': 'Productivity',
    'author': 'Team',
    'depends': ['base', 'web'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/mapping_registry_data.xml',
        'data/cleansing_rules_data.xml',
        'data/import_config_data.xml',
        'views/import_cleansing_rule_views.xml',
        'views/import_template_views.xml',
        'views/import_session_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'smart_data_importer/static/src/xml/preview_table.xml',
            'smart_data_importer/static/src/js/preview_table.js',
        ],
    },
    'external_dependencies': {
        'python': ['pandas', 'openpyxl', 'chardet', 'rapidfuzz', 'requests'],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
