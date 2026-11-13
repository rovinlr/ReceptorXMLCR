{
    "name": "Costa Rica Supplier XML Import",
    "version": "20.0.1.0.0",
    "summary": "Import supplier XML invoices and credit notes into vendor bills",
    "author": "FenixCR Solutions",
    "depends": ["account", "mail"],
    "data": [
        "wizard/supplier_xml_import_wizard_views.xml",
        "wizard/supplier_xml_rejected_wizard_views.xml",
        "views/account_move_views.xml",
        "views/res_config_settings_views.xml",
        "views/supplier_xml_gateway_views.xml",
        'security/ir.access.csv',
    ],
    "license": "OPL-1",
    "images": ["static/description/xml_import_banner.svg"],
    "application": False,
}
