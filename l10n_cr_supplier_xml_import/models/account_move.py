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
            for candidate_field in ("l10n_cr_edi_code", "tax_code", "code"):
                if candidate_field in tax_model._fields:
                    tax = tax_model.search(
                        [
                            (candidate_field, "=", code),
                            ("type_tax_use", "in", ["purchase", "none"]),
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
