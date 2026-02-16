{
    "name": "Costa Rica Supplier XML Import",
    "version": "19.0.1.0.0",
    "summary": "Import supplier XML invoices and credit notes into vendor bills",
    "depends": ["account", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "wizard/supplier_xml_import_wizard_views.xml",
        "views/account_move_views.xml",
        "views/res_config_settings_views.xml",
        "views/supplier_xml_gateway_views.xml",
    ],
    "license": "LGPL-3",
    "application": False,
}
