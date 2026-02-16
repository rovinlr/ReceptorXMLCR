import base64

from lxml import etree

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
    def _extract_localname_from_xml(self, payload):
        try:
            xml_root = etree.fromstring(payload)
            return etree.QName(xml_root).localname
        except Exception:
            return False

    @api.model
    def _get_invoice_xml_attachments(self, attachments):
        supported_localnames = {"FacturaElectronica", "NotaCreditoElectronica"}
        xml_candidates = []

        for attachment in attachments or []:
            filename, payload = attachment[0], attachment[1]
            if not filename or not filename.lower().endswith(".xml"):
                continue
            local_name = self._extract_localname_from_xml(payload)
            if local_name in supported_localnames:
                xml_candidates.append((filename, payload))

        return xml_candidates

    @api.model
    def _attachment_datas(self, payload):
        if isinstance(payload, bytes):
            return base64.b64encode(payload)
        if isinstance(payload, str):
            return payload.encode()
        return payload

    def _keep_mail_attachments_on_move(self, move, msg_dict):
        attachment_ids = []
        for attachment in msg_dict.get("attachments", []):
            filename, payload = attachment[0], attachment[1]
            if not filename or payload is None:
                continue
            ir_attachment = self.env["ir.attachment"].create(
                {
                    "name": filename,
                    "datas": self._attachment_datas(payload),
                    "res_model": "account.move",
                    "res_id": move.id,
                    "type": "binary",
                }
            )
            attachment_ids.append(ir_attachment.id)

        if attachment_ids:
            move.message_post(
                body=_("Adjuntos del correo original guardados en la factura."),
                attachment_ids=attachment_ids,
            )

    def _process_supplier_email(self, msg_dict):
        self.ensure_one()

        process_from_date = self.process_emails_from_date
        if process_from_date and msg_dict.get("date"):
            email_datetime = fields.Datetime.to_datetime(msg_dict.get("date"))
            if email_datetime and email_datetime.date() < process_from_date:
                self.message_post(
                    body=_("Correo ignorado por fecha (%s). Solo se procesan correos desde %s.")
                    % (fields.Datetime.to_string(email_datetime), fields.Date.to_string(process_from_date))
                )
                return

        xml_attachments = self._get_invoice_xml_attachments(msg_dict.get("attachments", []))
        if not xml_attachments:
            raise UserError(_("El correo no contiene XML de factura o nota de crédito para procesar."))

        move = False
        for filename, payload in xml_attachments:
            try:
                move = self.env["account.move"].create_from_supplier_xml(
                    xml_content=payload,
                    journal_id=self.journal_id.id or None,
                    company_id=self.company_id.id,
                    filename=filename,
                    supplier_xml_gateway_id=self.id,
                )
                break
            except UserError:
                continue

        if not move:
            raise UserError(_("No se encontró un XML de factura o nota de crédito válido en el correo."))

        self._keep_mail_attachments_on_move(move, msg_dict)
        move.message_post(body=_("Factura creada automáticamente desde correo: %s") % (msg_dict.get("subject") or ""))

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        values = custom_values or {}
        values.setdefault("name", msg_dict.get("subject") or _("Correo XML proveedor"))
        record = super().message_new(msg_dict, custom_values=values)
        record._process_supplier_email(msg_dict)
        return record

    def message_update(self, msg_dict, update_vals=None):
        result = super().message_update(msg_dict, update_vals=update_vals)
        for gateway in self:
            gateway._process_supplier_email(msg_dict)
        return result

    def action_process_incoming_emails(self):
        self.ensure_one()
        mail_thread_model = self.env["mail.thread"].with_context(active_test=False)
        fetch_method = getattr(mail_thread_model, "_fetch_mails", False)
        if not fetch_method:
            raise UserError(
                _("No hay un recolector de correos entrantes disponible. Verifique la configuración de correo entrante.")
            )

        fetch_method()

        self.message_post(
            body=_("Se ejecutó la revisión manual de correos entrantes. Fecha de referencia: %s")
            % (fields.Date.to_string(self.process_emails_from_date) or _("sin límite"))
        )
        return True

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
        values.update(
            {
                "alias_model_id": self.env["ir.model"]._get_id("supplier.xml.gateway"),
                "alias_force_thread_id": self.id,
            }
        )
        return values
