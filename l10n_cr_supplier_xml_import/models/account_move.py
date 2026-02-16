import base64
import binascii
import io
import zipfile
from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = "account.move"

    supplier_xml_filename = fields.Char(readonly=True, copy=False)
    supplier_xml_key = fields.Char(readonly=True, copy=False)
    supplier_xml_gateway_id = fields.Many2one("supplier.xml.gateway", readonly=True, copy=False)

    def init(self):
        """Backward-compatible safety for databases where module wasn't upgraded yet."""
        self.env.cr.execute(
            """
            ALTER TABLE account_move
            ADD COLUMN IF NOT EXISTS supplier_xml_gateway_id integer
            """
        )

    @api.model
    def create_from_supplier_xml(
        self,
        xml_content,
        journal_id=None,
        company_id=None,
        filename=None,
        supplier_xml_gateway_id=None,
    ):
        """Create a vendor bill or vendor credit note from Costa Rica supplier XML."""
        vals = self._parse_supplier_xml(xml_content, journal_id=journal_id, company_id=company_id)
        if filename:
            vals["supplier_xml_filename"] = filename
        if supplier_xml_gateway_id:
            vals["supplier_xml_gateway_id"] = supplier_xml_gateway_id
        return self.with_context(default_move_type=vals["move_type"]).create(vals)

    @api.model
    def _parse_supplier_xml(self, xml_content, journal_id=None, company_id=None):
        try:
            root = etree.fromstring(xml_content)
        except Exception as error:
            raise UserError(_("No se pudo leer el XML adjunto: %s") % error) from error

        local_name = etree.QName(root).localname
        move_type = self._get_move_type_from_xml(local_name)

        company = self.env["res.company"].browse(company_id) if company_id else self.env.company
        self._validate_receiver(root, company)

        emisor_name = self._xml_text(root, ["Emisor", "Nombre"])
        emisor_vat = self._normalize_identification(self._xml_text(root, ["Emisor", "Identificacion", "Numero"]))
        if not emisor_vat:
            raise UserError(_("El XML no contiene la identificación del emisor."))

        partner = self._find_or_create_supplier(emisor_name, emisor_vat)
        journal = self._get_purchase_journal(journal_id=journal_id, company=company)

        lines = self._build_invoice_lines(root, company)
        if not lines:
            raise UserError(_("El XML no tiene líneas de detalle para importar."))

        return {
            "move_type": move_type,
            "company_id": company.id,
            "journal_id": journal.id,
            "partner_id": partner.id,
            "ref": self._xml_text(root, ["NumeroConsecutivo"]) or self._xml_text(root, ["Clave"]),
            "invoice_date": self._parse_invoice_date(self._xml_text(root, ["FechaEmision"])),
            "supplier_xml_key": self._xml_text(root, ["Clave"]),
            "invoice_line_ids": lines,
        }

    @api.model
    def _get_move_type_from_xml(self, local_name):
        if local_name == "FacturaElectronica":
            return "in_invoice"
        if local_name == "NotaCreditoElectronica":
            return "in_refund"
        raise UserError(_("Tipo de XML no soportado: %s") % local_name)

    @api.model
    def _normalize_identification(self, value):
        return "".join(ch for ch in (value or "") if ch.isalnum()).upper()

    @api.model
    def _validate_receiver(self, root, company):
        receptor_number = self._normalize_identification(self._xml_text(root, ["Receptor", "Identificacion", "Numero"]))
        company_vat = self._normalize_identification(company.vat)
        if receptor_number and company_vat and receptor_number != company_vat:
            raise UserError(_("La cédula del receptor no coincide con la del sistema que recibe."))

    @api.model
    def _find_or_create_supplier(self, name, vat):
        partner = self.env["res.partner"].search([("vat", "=", vat)], limit=1)
        if partner:
            return partner
        return self.env["res.partner"].create(
            {
                "name": name or vat,
                "vat": vat,
                "supplier_rank": 1,
                "company_type": "company",
            }
        )

    @api.model
    def _get_purchase_journal(self, journal_id=None, company=None):
        journal = self.env["account.journal"]
        if journal_id:
            journal = self.env["account.journal"].browse(journal_id)
        if not journal:
            configured_journal_id = self.env["ir.config_parameter"].sudo().get_param(
                "l10n_cr_supplier_xml_import.default_purchase_journal_id"
            )
            if configured_journal_id and configured_journal_id.isdigit():
                journal = self.env["account.journal"].browse(int(configured_journal_id))
                if journal.company_id != company or journal.type != "purchase":
                    journal = self.env["account.journal"]
        if not journal:
            journal = self.env["account.journal"].search(
                [("type", "=", "purchase"), ("company_id", "=", company.id)], limit=1
            )
        if not journal:
            raise UserError(_("No hay diario de compras configurado para la compañía."))
        return journal

    @api.model
    def _parse_invoice_date(self, date_str):
        if not date_str:
            return fields.Date.context_today(self)
        value = date_str.split("T")[0]
        return fields.Date.from_string(value)

    @api.model
    def _default_expense_account(self, company):
        company_domain = self._company_domain(self.env["account.account"], company)
        account = self.env["account.account"].search(company_domain + [("account_type", "=", "expense")], limit=1)
        if not account:
            raise UserError(_("No se encontró una cuenta de gasto para crear las líneas de factura."))
        return account

    @api.model
    def _company_domain(self, model, company):
        if "company_id" in model._fields:
            return [("company_id", "=", company.id)]
        if "company_ids" in model._fields:
            return [("company_ids", "in", [company.id])]
        return []

    @api.model
    def _build_invoice_lines(self, root, company):
        default_account = self._default_expense_account(company)
        line_cmds = []
        for line_node in root.xpath("//*[local-name()='LineaDetalle']"):
            detail = self._xml_text(line_node, ["Detalle"])
            quantity = self._xml_float(line_node, ["Cantidad"], default=1.0)
            price_unit = self._xml_float(line_node, ["PrecioUnitario"], default=0.0)

            tax_ids = self._tax_ids_from_line(line_node, company)

            line_vals = {
                "name": detail or _("Línea importada desde XML"),
                "quantity": quantity,
                "price_unit": price_unit,
                "account_id": default_account.id,
            }
            if tax_ids:
                line_vals["tax_ids"] = [(6, 0, tax_ids)]
            line_cmds.append((0, 0, line_vals))
        return line_cmds

    @api.model
    def _tax_ids_from_line(self, line_node, company):
        tax_model = self.env["account.tax"]
        tax_ids = []
        for tax_node in line_node.xpath("./*[local-name()='Impuesto']"):
            code = self._xml_text(tax_node, ["CodigoTarifaIVA"])
            if not code:
                continue
            tax = False
            for candidate_field in ("fr_tax_rate_code_iva", "l10n_cr_edi_code", "tax_code", "code"):
                if candidate_field in tax_model._fields:
                    tax = tax_model.search(
                        [
                            (candidate_field, "=", code),
                            ("type_tax_use", "=", "purchase"),
                            *self._company_domain(tax_model, company),
                        ],
                        limit=1,
                    )
                    if tax:
                        break
            if tax and tax.id not in tax_ids:
                tax_ids.append(tax.id)
        return tax_ids

    @api.model
    def _xml_text(self, node, path):
        query = "./" + "/".join("*[local-name()='%s']" % part for part in path)
        result = node.xpath(query)
        if not result:
            return False
        return (result[0].text or "").strip()

    @api.model
    def _xml_float(self, node, path, default=0.0):
        value = self._xml_text(node, path)
        if not value:
            return default
        try:
            return float(value)
        except ValueError:
            return default


    @api.model
    def _attachment_raw_payload(self, attachment):
        if "raw" in attachment._fields and attachment.raw:
            return attachment.raw if isinstance(attachment.raw, bytes) else attachment.raw.encode()

        datas = attachment.datas
        if not datas and "db_datas" in attachment._fields:
            datas = attachment.db_datas
        if not datas:
            return b""
        if isinstance(datas, str):
            datas = datas.encode()
        return base64.b64decode(datas)

    @api.model
    def _is_supported_supplier_xml_payload(self, payload):
        if not payload:
            return False
        try:
            xml_root = etree.fromstring(payload)
        except Exception:
            return False
        return etree.QName(xml_root).localname in {"FacturaElectronica", "NotaCreditoElectronica"}

    @api.model
    def _normalize_attachment_payload(self, payload):
        if payload is None:
            return b""
        if isinstance(payload, bytes):
            return payload
        if isinstance(payload, str):
            return payload.encode()
        return bytes(payload)

    @api.model
    def _base64_decoded_payload_if_xml(self, payload):
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return b""
        return decoded if self._is_supported_supplier_xml_payload(decoded) else b""

    @api.model
    def _extract_supported_xml_payloads(self, payload, filename=False):
        normalized_payload = self._normalize_attachment_payload(payload)
        if not normalized_payload:
            return []

        xml_payloads = []
        if self._is_supported_supplier_xml_payload(normalized_payload):
            xml_payloads.append((filename, normalized_payload))

        decoded_payload = self._base64_decoded_payload_if_xml(normalized_payload)
        if decoded_payload:
            xml_payloads.append((filename, decoded_payload))

        is_zip_candidate = (filename or "").lower().endswith(".zip") or normalized_payload.startswith(b"PK")
        if not is_zip_candidate:
            return xml_payloads

        try:
            with zipfile.ZipFile(io.BytesIO(normalized_payload)) as zip_file:
                for xml_name in zip_file.namelist():
                    if not xml_name.lower().endswith(".xml"):
                        continue
                    xml_payload = zip_file.read(xml_name)
                    if self._is_supported_supplier_xml_payload(xml_payload):
                        xml_payloads.append((xml_name, xml_payload))
        except (zipfile.BadZipFile, RuntimeError, ValueError):
            return xml_payloads

        return xml_payloads

    def _message_and_move_attachments_for_xml_import(self):
        self.ensure_one()
        move_attachments = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "account.move"),
                ("res_id", "=", self.id),
                ("type", "=", "binary"),
            ]
        )
        message_attachments = self.env["ir.attachment"].search(
            [
                ("res_model", "=", "mail.message"),
                ("res_id", "in", self.message_ids.ids),
                ("type", "=", "binary"),
            ]
        )
        return (move_attachments | message_attachments).sorted(key=lambda a: a.id, reverse=True)

    def action_read_supplier_xml_attachment(self):
        self.ensure_one()
        if self.move_type not in ("in_invoice", "in_refund"):
            raise UserError(_("Esta acción solo aplica para facturas o notas de crédito de proveedor."))
        if self.state != "draft":
            raise UserError(_("Solo se puede leer XML manualmente cuando el documento está en borrador."))

        attachments = self._message_and_move_attachments_for_xml_import()
        if not attachments:
            raise UserError(_("No hay adjuntos en este documento o en su chatter."))

        for attachment in attachments:
            payload = self._attachment_raw_payload(attachment)
            xml_payloads = self._extract_supported_xml_payloads(payload, filename=attachment.name)
            for extracted_name, extracted_payload in xml_payloads:
                try:
                    vals = self._parse_supplier_xml(
                        extracted_payload,
                        journal_id=self.journal_id.id or None,
                        company_id=self.company_id.id,
                    )
                except UserError:
                    continue

                self.write(
                    {
                        "partner_id": vals["partner_id"],
                        "company_id": vals["company_id"],
                        "journal_id": vals["journal_id"],
                        "ref": vals["ref"],
                        "invoice_date": vals["invoice_date"],
                        "supplier_xml_key": vals["supplier_xml_key"],
                        "supplier_xml_filename": extracted_name or attachment.name,
                        "invoice_line_ids": [(5, 0, 0)] + vals["invoice_line_ids"],
                    }
                )
                self.message_post(body=_("XML leído manualmente desde el adjunto: %s") % (extracted_name or ""))
                return True

        raise UserError(_("No se encontró un XML válido en los adjuntos del documento o del chatter."))

    @api.model
    def _extract_xml_attachments_from_message(self, msg_dict):
        xml_candidates = []
        for attachment in msg_dict.get("attachments", []):
            if len(attachment) < 2:
                continue
            filename, payload = attachment[0], attachment[1]
            mimetype = attachment[2] if len(attachment) > 2 else False
            is_xml_name = bool(filename and filename.lower().endswith(".xml"))
            is_xml_mimetype = mimetype in {"text/xml", "application/xml"}
            is_zip_name = bool(filename and filename.lower().endswith(".zip"))
            is_zip_mimetype = mimetype in {"application/zip", "application/x-zip-compressed"}
            if not is_xml_name and not is_xml_mimetype and not is_zip_name and not is_zip_mimetype:
                continue
            xml_candidates.extend(self._extract_supported_xml_payloads(payload, filename=filename))
        return xml_candidates

    def _import_xml_from_message_attachments(self, msg_dict):
        self.ensure_one()
        if self.move_type not in ("in_invoice", "in_refund"):
            return

        xml_attachments = self._extract_xml_attachments_from_message(msg_dict)
        if not xml_attachments:
            for attachment in self._message_and_move_attachments_for_xml_import():
                payload = self._attachment_raw_payload(attachment)
                xml_attachments.extend(self._extract_supported_xml_payloads(payload, filename=attachment.name))

        for filename, payload in xml_attachments:
            if not payload:
                continue
            try:
                vals = self._parse_supplier_xml(
                    payload,
                    journal_id=self.journal_id.id or None,
                    company_id=self.company_id.id,
                )
            except UserError:
                continue

            write_vals = {
                "partner_id": vals["partner_id"],
                "company_id": vals["company_id"],
                "journal_id": vals["journal_id"],
                "ref": vals["ref"],
                "invoice_date": vals["invoice_date"],
                "supplier_xml_key": vals["supplier_xml_key"],
                "supplier_xml_filename": filename,
                "invoice_line_ids": [(5, 0, 0)] + vals["invoice_line_ids"],
            }
            self.write(write_vals)
            self.message_post(body=_("XML de proveedor leído automáticamente desde los adjuntos del correo."))
            return

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        move = super().message_new(msg_dict, custom_values=custom_values)
        move._import_xml_from_message_attachments(msg_dict)
        return move

    def message_update(self, msg_dict, update_vals=None):
        result = super().message_update(msg_dict, update_vals=update_vals)
        for move in self:
            move._import_xml_from_message_attachments(msg_dict)
        return result
