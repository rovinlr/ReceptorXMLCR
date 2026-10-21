from odoo import fields, models


class SupplierXMLRejectedWizard(models.TransientModel):
    _name = "supplier.xml.rejected.wizard"
    _description = "Confirmar documento rechazado por Hacienda"

    move_ids = fields.Many2many("account.move", string="Documentos")
    rejected_names = fields.Char(readonly=True)

    def action_confirm_anyway(self):
        self.ensure_one()
        return self.move_ids.with_context(supplier_xml_skip_rejected_check=True).action_post()
