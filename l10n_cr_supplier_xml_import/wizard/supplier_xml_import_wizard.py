import base64

from odoo import _, fields, models
from odoo.exceptions import UserError


class SupplierXMLImportWizard(models.TransientModel):
    _name = "supplier.xml.import.wizard"
    _description = "Importar XML de proveedor"

    xml_file = fields.Binary(required=True)
    xml_filename = fields.Char(required=True)
    journal_id = fields.Many2one("account.journal", domain="[('type', '=', 'purchase')]")

    def action_import_xml(self):
        self.ensure_one()
        if not self.xml_file:
            raise UserError(_("Debe seleccionar un archivo XML."))

        xml_content = base64.b64decode(self.xml_file)
        move = self.env["account.move"].create_from_supplier_xml(
            xml_content=xml_content,
            journal_id=self.journal_id.id or None,
            company_id=self.env.company.id,
            filename=self.xml_filename,
        )

        return {
            "type": "ir.actions.act_window",
            "name": _("Factura importada"),
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
            "target": "current",
        }
