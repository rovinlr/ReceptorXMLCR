import base64
import binascii
import io
import logging
import threading
import zipfile
from email import policy
from email.parser import BytesParser
from lxml import etree
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    supplier_xml_filename = fields.Char(readonly=True, copy=False)
    supplier_xml_key = fields.Char(readonly=True, copy=False)
    supplier_xml_gateway_id = fields.Many2one("supplier.xml.gateway", readonly=True, copy=False)
    supplier_xml_message_id = fields.Char(readonly=True, copy=False, index=True)

    @api.model
    def _get_safe_xml_parser(self):
        """Hardened lxml parser for untrusted supplier XML (arrives via email).

        Disables external entity resolution, network access and oversized trees
        to prevent XXE / SSRF / billion-laughs attacks.
        """
        return etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            load_dtd=False,
            dtd_validation=False,
        )

    @api.model
    def _parse_supplier_xml_root(self, xml_content):
        try:
            return etree.fromstring(xml_content, parser=self._get_safe_xml_parser())
        except Exception as error:
            raise UserError(_("No se pudo leer el XML adjunto: %s") % error) from error

    def _post_supplier_xml_checks(self, move, root, warnings):
        """Post a chatter note when the imported totals/taxes do not match the XML.

        Surfaces silent inconsistencies (unmapped taxes, total mismatch) instead
        of leaving a vendor bill that quietly differs from the legal document.
        """
        move.ensure_one()
        messages = list(warnings or [])

        xml_total = self._xml_float(root, ["ResumenFactura", "TotalComprobante"], default=False)
        if xml_total is not False and move.currency_id and move.currency_id.compare_amounts(
            move.amount_total, xml_total
        ) != 0:
            messages.append(
                _(
                    "El total registrado (%(odoo)s) no coincide con el total del XML "
                    "(%(xml)s). Revise impuestos o líneas antes de validar.",
                    odoo=move.amount_total,
                    xml=xml_total,
                )
            )

        if not messages:
            return
        body = Markup("<p>%s</p><ul>%s</ul>") % (
            _("Avisos al importar el XML de proveedor:"),
            Markup("").join(Markup("<li>%s</li>") % message for message in messages),
        )
        move.message_post(body=body)

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
        root = self._parse_supplier_xml_root(xml_content)
        warnings = []
        vals = self._parse_supplier_xml(
            xml_content, journal_id=journal_id, company_id=company_id, root=root, warnings=warnings
        )
        existing_move = self._find_existing_supplier_move_by_key(
            supplier_xml_key=vals.get("supplier_xml_key"),
            company_id=vals.get("company_id"),
        )
        if existing_move:
            return existing_move
        if filename:
            vals["supplier_xml_filename"] = filename
        if supplier_xml_gateway_id:
            vals["supplier_xml_gateway_id"] = supplier_xml_gateway_id
        move = self.with_context(default_move_type=vals["move_type"]).create(vals)
        self._post_supplier_xml_checks(move, root, warnings)
        return move

    @api.model
    def _find_existing_supplier_move_by_key(self, supplier_xml_key, company_id=None):
        if not supplier_xml_key:
            return self.env["account.move"]
        domain = [
            ("supplier_xml_key", "=", supplier_xml_key),
            ("move_type", "in", ["in_invoice", "in_refund"]),
        ]
        if company_id:
            domain.append(("company_id", "=", company_id))
        return self.search(domain, limit=1)

    @api.model
    def _parse_supplier_xml(self, xml_content, journal_id=None, company_id=None, root=None, warnings=None):
        if root is None:
            root = self._parse_supplier_xml_root(xml_content)

        local_name = etree.QName(root).localname
        move_type = self._get_move_type_from_xml(local_name)

        company = self.env["res.company"].browse(company_id) if company_id else self.env.company
        self._validate_receiver(root, company)
        currency = self._currency_from_xml(root, company)

        emisor_name = self._xml_text(root, ["Emisor", "Nombre"])
        emisor_vat = self._normalize_identification(self._xml_text(root, ["Emisor", "Identificacion", "Numero"]))
        if not emisor_vat:
            raise UserError(_("El XML no contiene la identificación del emisor."))

        partner = self._find_or_create_supplier(emisor_name, emisor_vat, root=root)
        journal = self._get_purchase_journal(journal_id=journal_id, company=company)

        lines = self._build_invoice_lines(root, company, partner=partner, warnings=warnings)
        if not lines:
            raise UserError(_("El XML no tiene líneas de detalle para importar."))

        return {
            "move_type": move_type,
            "company_id": company.id,
            "journal_id": journal.id,
            "partner_id": partner.id,
            "currency_id": currency.id,
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
    def _find_or_create_supplier(self, name, vat, root=None):
        partner = self.env["res.partner"].search([("vat", "=", vat)], limit=1)
        if partner:
            return partner
        vals = {
            "name": name or vat,
            "vat": vat,
            "supplier_rank": 1,
            "company_type": "company",
        }
        if root is not None:
            vals.update(self._supplier_vals_from_xml(root))
        partner = self.env["res.partner"].create(vals)
        self._enrich_supplier_from_hacienda(partner)
        return partner

    @api.model
    def _supplier_vals_from_xml(self, root):
        """Extra partner values taken from the Emisor node of the XML."""
        vals = {}
        partner_model = self.env["res.partner"]

        email = (self._xml_text(root, ["Emisor", "CorreoElectronico"]) or "").strip()
        if email and "@" in email:
            vals["email"] = email

        phone_number = self._xml_text(root, ["Emisor", "Telefono", "NumTelefono"])
        if phone_number:
            country_code = self._xml_text(root, ["Emisor", "Telefono", "CodigoPais"])
            vals["phone"] = "+%s %s" % (country_code, phone_number) if country_code else phone_number

        # l10n_cr_einvoice generates Ubicacion/OtrasSenas from partner.street,
        # so importing it into street keeps both modules symmetric.
        other_address = self._xml_text(root, ["Emisor", "Ubicacion", "OtrasSenas"])
        if other_address:
            vals["street"] = other_address

        neighborhood = self._xml_text(root, ["Emisor", "Ubicacion", "Barrio"])
        if neighborhood and "fp_neighborhood_code" in partner_model._fields:
            vals["fp_neighborhood_code"] = neighborhood[:64]

        country = self.env.ref("base.cr", raise_if_not_found=False)
        if country:
            vals["country_id"] = country.id

        id_type = (self._xml_text(root, ["Emisor", "Identificacion", "Tipo"]) or "").strip()
        if id_type:
            id_type = id_type.zfill(2)
            if id_type == "01":
                vals["company_type"] = "person"
            id_type_field = partner_model._fields.get("fp_identification_type")
            if id_type_field is not None and isinstance(id_type_field.selection, list):
                if id_type in dict(id_type_field.selection):
                    vals["fp_identification_type"] = id_type

        vals.update(self._supplier_location_vals_from_xml(root))
        return vals

    @api.model
    def _supplier_location_vals_from_xml(self, root):
        """Province/canton/district from the XML Ubicacion codes.

        Uses the l10n_cr_einvoice catalogs (fp.province/fp.canton/fp.district)
        when that module is installed: records are only selected by code, never
        created. Returns {} when the module or the codes are absent.
        """
        partner_model = self.env["res.partner"]
        if "fp_province_id" not in partner_model._fields or "fp.province" not in self.env:
            return {}

        province_code = (self._xml_text(root, ["Emisor", "Ubicacion", "Provincia"]) or "").strip()
        if not province_code:
            return {}
        province = self.env["fp.province"].search([("code", "=", province_code)], limit=1)
        if not province:
            return {}
        vals = {"fp_province_id": province.id, "fp_province_code": province.code}

        canton_code = (self._xml_text(root, ["Emisor", "Ubicacion", "Canton"]) or "").strip()
        if not canton_code:
            return vals
        canton = self.env["fp.canton"].search(
            [
                ("province_id", "=", province.id),
                ("code", "in", [canton_code, canton_code.zfill(2)]),
            ],
            limit=1,
        )
        if not canton:
            return vals
        vals.update({"fp_canton_id": canton.id, "fp_canton_code": canton.code, "city": canton.name})

        district_code = (self._xml_text(root, ["Emisor", "Ubicacion", "Distrito"]) or "").strip()
        if not district_code:
            return vals
        district = self.env["fp.district"].search(
            [
                ("canton_id", "=", canton.id),
                ("code", "in", [district_code, district_code.zfill(2)]),
            ],
            limit=1,
        )
        if district:
            vals.update({"fp_district_id": district.id, "fp_district_code": district.code})
        return vals

    @api.model
    def _enrich_supplier_from_hacienda(self, partner):
        """Complement a newly created supplier with official Hacienda data.

        Delegates to l10n_cr_einvoice's VAT lookup when installed (it fills the
        official name, identification type and economic activity, and only sets
        the email when empty, so XML data is preserved). Skipped during tests
        and never allowed to break the import on network errors.
        """
        if not hasattr(partner, "_fp_query_hacienda") or not hasattr(partner, "_fp_apply_hacienda_payload"):
            return
        if getattr(threading.current_thread(), "testing", False):
            return
        try:
            data = partner._fp_query_hacienda(partner.vat)
            if data:
                partner._fp_apply_hacienda_payload(data)
        except Exception:  # noqa: BLE001 - enrichment must never abort the import
            _logger.warning(
                "No se pudo enriquecer el proveedor %s con datos de Hacienda.",
                partner.vat,
                exc_info=True,
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
    @api.model
    def _predicted_expense_account(self, label, partner, company, default_account):
        """Reuse Odoo's account prediction (per supplier + line label).

        Odoo predicts the account only through the Label onchange in the UI, which
        never fires on programmatic import. We call it explicitly so imported bills
        land on the account historically used for that supplier/detail, and fall
        back to the default expense account when there is no history or the API is
        unavailable. Never let prediction break the import.
        """
        if not label or not partner:
            return default_account
        line_model = self.env["account.move.line"].with_company(company)
        if not hasattr(line_model, "_predict_account"):
            return default_account
        try:
            predicted = line_model._predict_account(label, partner)
        except Exception:  # noqa: BLE001 - prediction must never abort the import
            return default_account
        if isinstance(predicted, int):
            predicted = self.env["account.account"].browse(predicted) if predicted else self.env["account.account"]
        if predicted and predicted.exists() and not getattr(predicted, "deprecated", False):
            return predicted
        return default_account

    @api.model
    def _company_domain(self, model, company):
        if "company_id" in model._fields:
            return [("company_id", "=", company.id)]
        if "company_ids" in model._fields:
            return [("company_ids", "in", [company.id])]
        return []

    @api.model
    def _build_invoice_lines(self, root, company, partner=None, warnings=None):
        default_account = self._default_expense_account(company)
        line_cmds = []
        other_charges_tax_ids = self._tax_ids_for_other_charges(company)
        seen_other_charge_nodes = set()
        for line_node in root.xpath("//*[local-name()='LineaDetalle']"):
            detail = self._xml_text(line_node, ["Detalle"])
            quantity = self._xml_float(line_node, ["Cantidad"], default=1.0)
            price_unit = self._line_net_price_unit(line_node=line_node, quantity=quantity)

            tax_ids = self._tax_ids_from_line(line_node, company, warnings=warnings, line_label=detail)
            account = self._predicted_expense_account(detail, partner, company, default_account)

            line_vals = {
                "name": detail or _("Línea importada desde XML"),
                "quantity": quantity,
                "price_unit": price_unit,
                "account_id": account.id,
            }
            if tax_ids:
                line_vals["tax_ids"] = [(6, 0, tax_ids)]
            line_cmds.append((0, 0, line_vals))
            line_cmds.extend(
                self._build_other_charge_lines(
                    parent_node=line_node,
                    default_account=default_account,
                    tax_ids=other_charges_tax_ids,
                    seen_nodes=seen_other_charge_nodes,
                )
            )
        line_cmds.extend(
            self._build_other_charge_lines(
                parent_node=root,
                default_account=default_account,
                tax_ids=other_charges_tax_ids,
                xpath="./*[local-name()='OtrosCargos'] | ./*[local-name()='DetalleServicio']/*[local-name()='OtrosCargos']",
                seen_nodes=seen_other_charge_nodes,
            )
        )
        return line_cmds

    @api.model
    def _line_net_price_unit(self, line_node, quantity):
        """Use net taxable base from XML to avoid duplicating VAT in Odoo totals."""
        subtotal = self._xml_float(line_node, ["SubTotal"], default=False)
        total_line = self._xml_float(line_node, ["MontoTotalLinea"], default=False)
        if quantity and quantity > 0:
            if subtotal is not False:
                return subtotal / quantity
            # Fallback only when SubTotal is missing; MontoTotalLinea can include VAT.
            if total_line is not False:
                return total_line / quantity
        return self._xml_float(line_node, ["PrecioUnitario"], default=0.0)

    @api.model
    def _currency_from_xml(self, root, company):
        currency_code = self._currency_code_from_xml(root)
        if not currency_code:
            return company.currency_id

        currency = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", currency_code)],
            limit=1,
        )
        if not currency:
            raise UserError(
                _(
                    "El XML indica la moneda %(currency_code)s, pero esa moneda no existe en Odoo. "
                    "Debe crearla o corregir la configuración antes de importar el XML."
                )
                % {"currency_code": currency_code}
            )
        if "active" in currency._fields and not currency.active:
            raise UserError(
                _(
                    "El XML indica la moneda %(currency_code)s, pero esa moneda está inactiva en Odoo. "
                    "Actívela en Contabilidad > Configuración > Monedas antes de importar el XML."
                )
                % {"currency_code": currency_code}
            )
        return currency

    @api.model
    def _currency_code_from_xml(self, root):
        currency_code = self._xml_text(root, ["ResumenFactura", "CodigoTipoMoneda", "CodigoMoneda"])
        if not currency_code:
            currency_code = self._xml_text(root, ["ResumenFactura", "CodigoMoneda"])
        return (currency_code or "CRC").strip().upper()

    @api.model
    def _supplier_xml_move_write_vals(self, vals, filename=False):
        write_vals = {
            "move_type": vals["move_type"],
            "partner_id": vals["partner_id"],
            "company_id": vals["company_id"],
            "journal_id": vals["journal_id"],
            "currency_id": vals["currency_id"],
            "ref": vals["ref"],
            "invoice_date": vals["invoice_date"],
            "supplier_xml_key": vals["supplier_xml_key"],
            "invoice_line_ids": [(5, 0, 0)] + vals["invoice_line_ids"],
        }
        if filename:
            write_vals["supplier_xml_filename"] = filename
        return write_vals

    @api.model
    def _tax_ids_from_line(self, line_node, company, warnings=None, line_label=False):
        tax_ids = []
        for tax_node in line_node.xpath("./*[local-name()='Impuesto']"):
            code = self._xml_text(tax_node, ["CodigoTarifaIVA"])
            rate = self._xml_float(tax_node, ["Tarifa"], default=False)
            tax = self._find_exonerated_purchase_tax(company=company, tax_node=tax_node, code=code)
            if not tax:
                tax = self._find_purchase_tax_by_code_or_rate(company=company, code=code, rate=rate)
            if tax and tax.id not in tax_ids:
                tax_ids.append(tax.id)
            elif not tax and warnings is not None and (code or rate is not False):
                warnings.append(
                    _(
                        "No se encontró un impuesto de compra para la línea «%(line)s» "
                        "(código tarifa %(code)s, tarifa %(rate)s%%). Se importó sin ese impuesto.",
                        line=line_label or _("(sin detalle)"),
                        code=code or _("n/d"),
                        rate=rate if rate is not False else _("n/d"),
                    )
                )
        return tax_ids

    @api.model
    def _find_exonerated_purchase_tax(self, company, tax_node, code):
        exoneration_node = tax_node.xpath("./*[local-name()='Exoneracion']")
        if not exoneration_node:
            return self.env["account.tax"]

        tax_amount = self._xml_float(tax_node, ["Monto"], default=0.0)
        exonerated_amount = self._xml_float(tax_node, ["Exoneracion", "MontoExoneracion"], default=0.0)
        is_fully_exonerated = tax_amount <= 0.0 or abs(tax_amount - exonerated_amount) < 0.0001
        if not is_fully_exonerated:
            return self.env["account.tax"]

        tax = self._find_purchase_tax_by_code_and_amount(company=company, code=code, amount=0.0)
        if tax:
            return tax
        return self._find_purchase_tax_by_code_and_amount(company=company, code="10", amount=0.0)

    @api.model
    def _find_purchase_tax_by_code_and_amount(self, company, code, amount):
        tax_model = self.env["account.tax"]
        company_domain = self._company_domain(tax_model, company)
        rounded_amount = round(amount, 4)
        for candidate_field in ("fp_tax_rate_code_iva", "fr_tax_rate_code_iva", "l10n_cr_edi_code", "tax_code", "code"):
            if candidate_field not in tax_model._fields:
                continue
            tax = tax_model.search(
                [
                    (candidate_field, "=", code),
                    ("type_tax_use", "=", "purchase"),
                    ("amount", "=", rounded_amount),
                    *company_domain,
                ],
                limit=1,
            )
            if tax:
                return tax
        return tax_model

    @api.model
    def _tax_ids_for_other_charges(self, company):
        tax = self._find_purchase_tax_by_code_or_rate(company=company, code="10")
        return [tax.id] if tax else []

    @api.model
    def _build_other_charge_lines(
        self,
        parent_node,
        default_account,
        tax_ids=None,
        xpath="./*[local-name()='OtrosCargos']",
        seen_nodes=None,
    ):
        line_cmds = []
        tax_ids = tax_ids or []
        seen_nodes = seen_nodes if seen_nodes is not None else set()
        for charge_node in parent_node.xpath(xpath):
            node_identifier = id(charge_node)
            if node_identifier in seen_nodes:
                continue
            seen_nodes.add(node_identifier)
            amount = self._xml_float(charge_node, ["MontoCargo"], default=0.0)
            if amount <= 0:
                continue
            charge_detail = self._xml_text(charge_node, ["Detalle"])
            charge_document = (
                self._xml_text(charge_node, ["TipoDocumentoOC"])
                or self._xml_text(charge_node, ["TipoDocumento"])
                or self._xml_text(charge_node, ["TipoDocumentoOTROS"])
            )
            line_name = charge_detail or _("Otros cargos")
            if charge_document:
                line_name = _("%(line_name)s (Doc: %(doc)s)", line_name=line_name, doc=charge_document)
            charge_line_vals = {
                "name": line_name,
                "quantity": 1.0,
                "price_unit": amount,
                "account_id": default_account.id,
            }
            if tax_ids:
                charge_line_vals["tax_ids"] = [(6, 0, tax_ids)]
            line_cmds.append((0, 0, charge_line_vals))
        return line_cmds

    @api.model
    def _find_purchase_tax_by_code_or_rate(self, company, code=False, rate=False):
        tax_model = self.env["account.tax"]
        company_domain = self._company_domain(tax_model, company)

        if code:
            for candidate_field in ("fp_tax_rate_code_iva", "fr_tax_rate_code_iva", "l10n_cr_edi_code", "tax_code", "code"):
                if candidate_field in tax_model._fields:
                    tax = tax_model.search(
                        [
                            (candidate_field, "=", code),
                            ("type_tax_use", "=", "purchase"),
                            *company_domain,
                        ],
                        limit=1,
                    )
                    if tax:
                        return tax

        if rate is not False:
            rounded_rate = round(rate, 4)
            tax = tax_model.search(
                [
                    ("amount_type", "=", "percent"),
                    ("amount", "=", rounded_rate),
                    ("type_tax_use", "=", "purchase"),
                    *company_domain,
                ],
                limit=1,
            )
            if tax:
                return tax

        return tax_model

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
            xml_root = etree.fromstring(payload, parser=self._get_safe_xml_parser())
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
    def _extract_supported_xml_payloads(self, payload, filename=False, allow_email_container=True):
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
        if is_zip_candidate:
            try:
                with zipfile.ZipFile(io.BytesIO(normalized_payload)) as zip_file:
                    for xml_name in zip_file.namelist():
                        if not xml_name.lower().endswith(".xml"):
                            continue
                        xml_payload = zip_file.read(xml_name)
                        if self._is_supported_supplier_xml_payload(xml_payload):
                            xml_payloads.append((xml_name, xml_payload))
            except (zipfile.BadZipFile, RuntimeError, ValueError):
                pass

        if allow_email_container and self._looks_like_email_container(normalized_payload, filename=filename):
            xml_payloads.extend(
                self._extract_xml_payloads_from_email_container(normalized_payload, filename=filename)
            )

        return xml_payloads

    @api.model
    def _looks_like_email_container(self, payload, filename=False):
        lower_name = (filename or "").lower()
        if lower_name.endswith(".eml") or lower_name.endswith(".msg"):
            return True
        return payload.lstrip().startswith(b"Return-Path:") or payload.lstrip().startswith(b"Received:")

    @api.model
    def _extract_xml_payloads_from_email_container(self, payload, filename=False):
        try:
            email_message = BytesParser(policy=policy.default).parsebytes(payload)
        except Exception:
            return []

        xml_payloads = []
        for part in email_message.walk():
            if part.is_multipart():
                continue
            part_payload = part.get_payload(decode=True)
            if not part_payload:
                continue
            part_filename = part.get_filename() or filename
            xml_payloads.extend(
                self._extract_supported_xml_payloads(
                    part_payload,
                    filename=part_filename,
                    allow_email_container=False,
                )
            )
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
                    root = self._parse_supplier_xml_root(extracted_payload)
                    warnings = []
                    vals = self._parse_supplier_xml(
                        extracted_payload,
                        journal_id=self.journal_id.id or None,
                        company_id=self.company_id.id,
                        root=root,
                        warnings=warnings,
                    )
                except UserError:
                    continue

                self.write(self._supplier_xml_move_write_vals(vals, filename=extracted_name or attachment.name))
                self.message_post(body=_("XML leído manualmente desde el adjunto: %s") % (extracted_name or ""))
                self._post_supplier_xml_checks(self, root, warnings)
                return True

        raise UserError(_("No se encontró un XML válido en los adjuntos del documento o del chatter."))

    @api.model
    def _extract_xml_attachments_from_message(self, msg_dict):
        xml_candidates = []
        for attachment in msg_dict.get("attachments", []):
            filename = False
            payload = b""
            mimetype = False

            if isinstance(attachment, dict):
                filename = attachment.get("filename") or attachment.get("fname")
                payload = attachment.get("content") or attachment.get("payload") or b""
                mimetype = (
                    attachment.get("mimetype")
                    or attachment.get("content_type")
                    or attachment.get("type")
                )
            elif isinstance(attachment, (list, tuple)):
                if len(attachment) < 2:
                    continue
                filename, payload = attachment[0], attachment[1]
                mimetype = attachment[2] if len(attachment) > 2 else False

            if isinstance(mimetype, dict):
                mimetype = (
                    mimetype.get("mimetype")
                    or mimetype.get("content_type")
                    or mimetype.get("type")
                    or False
                )

            is_xml_name = bool(filename and filename.lower().endswith(".xml"))
            is_xml_mimetype = mimetype in {"text/xml", "application/xml"}
            is_zip_name = bool(filename and filename.lower().endswith(".zip"))
            is_zip_mimetype = mimetype in {"application/zip", "application/x-zip-compressed"}
            if not is_xml_name and not is_xml_mimetype and not is_zip_name and not is_zip_mimetype:
                continue
            xml_candidates.extend(self._extract_supported_xml_payloads(payload, filename=filename))
        return xml_candidates

    @api.model
    def _gateway_from_email_message(self, msg_dict):
        return self.env["supplier.xml.gateway"]._gateway_from_email_message(msg_dict)

    def _import_xml_from_message_attachments(self, msg_dict):
        self.ensure_one()
        if self.move_type not in ("in_invoice", "in_refund"):
            return
        # Avoid wiping a bill that was already imported or manually edited: only
        # auto-import on a still-draft document that has not been filled from XML yet.
        if self.state != "draft" or self.supplier_xml_key:
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
                root = self._parse_supplier_xml_root(payload)
                warnings = []
                vals = self._parse_supplier_xml(
                    payload,
                    journal_id=self.journal_id.id or None,
                    company_id=self.company_id.id,
                    root=root,
                    warnings=warnings,
                )
            except UserError:
                continue

            gateway = self._gateway_from_email_message(msg_dict)
            write_vals = self._supplier_xml_move_write_vals(vals, filename=filename)
            if gateway:
                write_vals["supplier_xml_gateway_id"] = gateway.id
            message_id = self.env["supplier.xml.gateway"]._extract_message_id_from_message(msg_dict)
            if message_id and not self.supplier_xml_message_id:
                write_vals["supplier_xml_message_id"] = message_id
            self.write(write_vals)
            self.message_post(body=_("XML de proveedor leído automáticamente desde los adjuntos del correo."))
            self._post_supplier_xml_checks(self, root, warnings)
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
