from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SupplierXMLGateway(models.Model):
    _name = "supplier.xml.gateway"
    _description = "Buzón de XML de proveedor"
    _inherit = ["mail.thread", "mail.alias.mixin"]

    name = fields.Char(required=True, default="Buzón XML Proveedor")
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    journal_id = fields.Many2one(
        "account.journal",
        domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
        help="Diario usado para las facturas importadas automáticamente desde correo.",
    )
    process_emails_from_date = fields.Date(
        string="Procesar correos desde",
        help="Ignora correos con fecha anterior a este valor.",
    )

    move_ids = fields.One2many("account.move", "supplier_xml_gateway_id", string="Facturas recibidas")
    move_count = fields.Integer(compute="_compute_move_count", string="Facturas recibidas")

    @api.depends("move_ids")
    def _compute_move_count(self):
        for record in self:
            record.move_count = len(record.move_ids)


    @api.model
    def message_new(self, msg_dict, custom_values=None):
        values = custom_values or {}
        values.setdefault("name", msg_dict.get("subject") or _("Correo XML proveedor"))
        record = super().message_new(msg_dict, custom_values=values)

        if record.process_emails_from_date and msg_dict.get("date"):
            email_datetime = fields.Datetime.to_datetime(msg_dict.get("date"))
            if email_datetime and email_datetime.date() < record.process_emails_from_date:
                record.message_post(
                    body=_(
                        "Correo ignorado por fecha (%s). Solo se procesan correos desde %s."
                    )
                    % (
                        fields.Datetime.to_string(email_datetime),
                        fields.Date.to_string(record.process_emails_from_date),
                    )
                )
                return record

        xml_attachments = []
        for attachment in msg_dict.get("attachments", []):
            filename, payload = attachment[0], attachment[1]
            if filename and filename.lower().endswith(".xml"):
                xml_attachments.append((filename, payload))

        if not xml_attachments:
            raise UserError(_("El correo no contiene archivos XML para procesar."))

        move = False
        for filename, payload in xml_attachments:
            try:
                move = self.env["account.move"].create_from_supplier_xml(
                    xml_content=payload,
                    journal_id=record.journal_id.id or None,
                    company_id=record.company_id.id,
                    filename=filename,
                    supplier_xml_gateway_id=record.id,
                )
                break
            except UserError:
                continue

        if not move:
            raise UserError(_("No se encontró un XML de factura o nota de crédito válido en el correo."))

        move.message_post(body=_("Factura creada automáticamente desde correo: %s") % (msg_dict.get("subject") or ""))
        return record

    def action_view_received_moves(self):
        self.ensure_one()
        return {
            "name": _("Facturas recibidas"),
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("supplier_xml_gateway_id", "=", self.id)],
            "context": {
                "default_move_type": "in_invoice",
            },
        }

    def _alias_get_creation_values(self):
        values = super()._alias_get_creation_values()
        values.update({"alias_model_id": self.env["ir.model"]._get_id("supplier.xml.gateway")})
        return values
