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
from odoo.tools import config

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    supplier_xml_filename = fields.Char(readonly=True, copy=False)
    supplier_xml_key = fields.Char(readonly=True, copy=False)
    supplier_xml_gateway_id = fields.Many2one("supplier.xml.gateway", readonly=True, copy=False)
    supplier_xml_message_id = fields.Char(readonly=True, copy=False, index=True)
    supplier_xml_reference_key = fields.Char(
        string="Clave referenciada",
        readonly=True,
        copy=False,
        index=True,
        help="Clave del comprobante original declarada en InformacionReferencia "
        "de una nota de crédito/débito electrónica.",
    )
    supplier_xml_hacienda_state = fields.Selection(
        [("aceptado", "Aceptado"), ("rechazado", "Rechazado")],
        string="Estado en Hacienda",
        readonly=True,
        copy=False,
        help="Estado del comprobante del proveedor según el XML de respuesta de "
        "Hacienda (MensajeHacienda) adjunto al correo. Vacío = sin respuesta recibida.",
    )

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
    def _sanitize_supplier_xml_payload(self, payload):
        """Remove junk bytes before the XML content.

        Some providers emit files with a mangled UTF-8 BOM (EF BF BD sequences)
        or other garbage before the XML declaration, which libxml2 rejects with
        "Start tag expected". UTF-16 payloads are re-encoded to UTF-8 first so
        the search for '<' is meaningful.
        """
        if not payload or not isinstance(payload, bytes):
            return payload
        if payload[:2] in (b"\xff\xfe", b"\xfe\xff"):
            try:
                text = payload.decode("utf-16")
            except UnicodeDecodeError:
                return payload
            # Drop the XML declaration: it says UTF-16 but the bytes are now UTF-8.
            if text.lstrip().startswith("<?xml"):
                text = text.split("?>", 1)[-1]
            payload = text.encode("utf-8")
        index = payload.find(b"<")
        if index > 0:
            payload = payload[index:]
        return payload

    @api.model
    def _parse_supplier_xml_root(self, xml_content):
        xml_content = self._sanitize_supplier_xml_payload(xml_content)
        try:
            return etree.fromstring(xml_content, parser=self._get_safe_xml_parser())
        except Exception as error:
            raise UserError(_("No se pudo leer el XML adjunto: %s") % error) from error

    def _finalize_supplier_xml_move(self, move, root, warnings):
        """Post-import steps shared by every entry point (wizard, manual, email)."""
        self._align_accounting_date(move)
        self._apply_predicted_accounts(move)
        self._link_credit_note_references(move, warnings)
        self._post_supplier_xml_checks(move, root, warnings)

    def _link_credit_note_references(self, move, warnings):
        """Tie credit notes to their original bill via the referenced Clave.

        Both directions are covered (emails do not always arrive in order):
        an imported NC looks up its referenced invoice, and an imported invoice
        adopts orphan NCs that were already referencing its Clave. The link is
        Odoo's native ``reversed_entry_id`` and is only made between documents
        of the SAME supplier; it never overwrites an existing link.
        """
        move.ensure_one()
        if move.move_type == "in_refund" and move.supplier_xml_reference_key:
            original = self._find_existing_supplier_move_by_key(
                supplier_xml_key=move.supplier_xml_reference_key,
                company_id=move.company_id.id,
            )
            if not original or original == move:
                warnings.append(
                    _(
                        "La nota de crédito referencia la clave %(clave)s, pero ese comprobante "
                        "no está importado; el vínculo se hará automáticamente si la factura "
                        "llega después.",
                        clave=move.supplier_xml_reference_key,
                    )
                )
            elif original.partner_id != move.partner_id:
                warnings.append(
                    _(
                        "La clave referenciada %(clave)s corresponde a %(move)s de OTRO proveedor "
                        "(%(partner)s); no se ligó. Verifique el XML.",
                        clave=move.supplier_xml_reference_key,
                        move=original.display_name,
                        partner=original.partner_id.display_name,
                    )
                )
            elif not move.reversed_entry_id:
                move.reversed_entry_id = original.id
                original.message_post(
                    body=_(
                        "Nota de crédito %(move)s ligada automáticamente a esta factura "
                        "(clave referenciada en el XML).",
                        move=move.display_name,
                    )
                )
                move.message_post(
                    body=_(
                        "Ligada automáticamente a la factura %(move)s (clave referenciada "
                        "%(clave)s).",
                        move=original.display_name,
                        clave=move.supplier_xml_reference_key,
                    )
                )
        elif move.move_type == "in_invoice" and move.supplier_xml_key:
            orphan_credit_notes = self.search(
                [
                    ("move_type", "=", "in_refund"),
                    ("supplier_xml_reference_key", "=", move.supplier_xml_key),
                    ("company_id", "=", move.company_id.id),
                    ("partner_id", "=", move.partner_id.id),
                    ("reversed_entry_id", "=", False),
                    ("id", "!=", move.id),
                ]
            )
            if orphan_credit_notes:
                orphan_credit_notes.write({"reversed_entry_id": move.id})
                for credit_note in orphan_credit_notes:
                    credit_note.message_post(
                        body=_(
                            "Ligada automáticamente a la factura %(move)s, importada después "
                            "de esta nota de crédito.",
                            move=move.display_name,
                        )
                    )
                move.message_post(
                    body=_(
                        "Se ligaron automáticamente %(count)s nota(s) de crédito que ya "
                        "referenciaban la clave de esta factura: %(names)s",
                        count=len(orphan_credit_notes),
                        names=", ".join(orphan_credit_notes.mapped("display_name")),
                    )
                )

    @api.model
    def _extract_hacienda_response_payloads(self, payload, filename=False):
        """(clave, state, detail) for every MensajeHacienda found in the payload.

        Suppliers attach Hacienda's response XML next to the invoice (same email
        or a later one). Handles plain XML and ZIP attachments; Mensaje codes:
        1 = aceptado, 3 = rechazado (others are ignored).
        """
        normalized_payload = self._normalize_attachment_payload(payload)
        if not normalized_payload:
            return []

        candidates = [normalized_payload]
        is_zip_candidate = (filename or "").lower().endswith(".zip") or normalized_payload.startswith(b"PK")
        if is_zip_candidate:
            try:
                with zipfile.ZipFile(io.BytesIO(normalized_payload)) as zip_file:
                    for member_name in zip_file.namelist():
                        if member_name.lower().endswith(".xml"):
                            candidates.append(zip_file.read(member_name))
            except (zipfile.BadZipFile, RuntimeError, ValueError):
                pass

        responses = []
        for candidate in candidates:
            try:
                root = etree.fromstring(
                    self._sanitize_supplier_xml_payload(candidate), parser=self._get_safe_xml_parser()
                )
            except Exception:
                continue
            if etree.QName(root).localname != "MensajeHacienda":
                continue
            clave = (self._xml_text(root, ["Clave"]) or "").strip()
            state = {"1": "aceptado", "3": "rechazado"}.get((self._xml_text(root, ["Mensaje"]) or "").strip())
            if clave and state:
                responses.append((clave, state, self._xml_text(root, ["DetalleMensaje"]) or ""))
        return responses

    @api.model
    def _hacienda_responses_from_message(self, msg_dict):
        """Collect Hacienda responses from every attachment of an email msg_dict."""
        responses = []
        for attachment in msg_dict.get("attachments", []):
            filename = False
            payload = b""
            if isinstance(attachment, dict):
                filename = attachment.get("filename") or attachment.get("fname")
                payload = attachment.get("content") or attachment.get("payload") or b""
            elif isinstance(attachment, (list, tuple)) and len(attachment) >= 2:
                filename, payload = attachment[0], attachment[1]
            responses.extend(self._extract_hacienda_response_payloads(payload, filename=filename))
        return responses

    @api.model
    def _apply_hacienda_responses(self, responses, company=None):
        """Write the Hacienda state onto the moves matching each response's Clave.

        Idempotent (no rewrite/chatter when the state is unchanged). Returns the
        moves that were updated.
        """
        updated_moves = self.env["account.move"]
        for clave, state, detail in responses:
            move = self._find_existing_supplier_move_by_key(
                supplier_xml_key=clave,
                company_id=company.id if company else None,
            )
            if not move or move.supplier_xml_hacienda_state == state:
                continue
            move.write({"supplier_xml_hacienda_state": state})
            if state == "aceptado":
                body = _("Hacienda ACEPTÓ el comprobante del proveedor (clave %(clave)s).", clave=clave)
            else:
                body = _(
                    "⚠️ Hacienda RECHAZÓ el comprobante del proveedor (clave %(clave)s). "
                    "Verifique con el proveedor antes de confirmar la factura.",
                    clave=clave,
                )
            if detail:
                body = Markup("<p>%s</p><p><em>%s</em></p>") % (body, detail)
            move.message_post(body=body)
            updated_moves |= move
        return updated_moves

    def action_post(self):
        """Warn before posting a vendor document that Hacienda rejected."""
        if not self.env.context.get("supplier_xml_skip_rejected_check"):
            rejected_moves = self.filtered(
                lambda move: move.move_type in ("in_invoice", "in_refund")
                and move.supplier_xml_hacienda_state == "rechazado"
            )
            if rejected_moves:
                return {
                    "type": "ir.actions.act_window",
                    "name": _("Documento rechazado por Hacienda"),
                    "res_model": "supplier.xml.rejected.wizard",
                    "view_mode": "form",
                    "target": "new",
                    "context": {
                        "default_move_ids": [(6, 0, self.ids)],
                        "default_rejected_names": ", ".join(rejected_moves.mapped("display_name")),
                    },
                }
        return super().action_post()

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
            if existing_move.partner_id.id == vals["partner_id"]:
                # Same Clave and same supplier: it is the same document. The
                # context flag lets callers (e.g. the mail gateway) know they
                # should attach to it instead of announcing a new invoice.
                return existing_move.with_context(supplier_xml_duplicate=True)
            # Same Clave but a DIFFERENT supplier: never merge documents of
            # different partners; import separately and leave a loud warning.
            warnings.append(self._duplicate_clave_warning(existing_move, vals.get("supplier_xml_key")))
        if filename:
            vals["supplier_xml_filename"] = filename
        if supplier_xml_gateway_id:
            vals["supplier_xml_gateway_id"] = supplier_xml_gateway_id
        move = self.with_context(default_move_type=vals["move_type"]).create(vals)
        self._finalize_supplier_xml_move(move, root, warnings)
        return move

    @api.model
    def _duplicate_clave_warning(self, existing_move, clave):
        return _(
            "Ya existe otro documento (%(move)s) con la misma clave %(clave)s pero de otro "
            "proveedor (%(partner)s). Verifique una posible clave errónea del emisor antes de validar.",
            move=existing_move.display_name,
            clave=clave or _("desconocida"),
            partner=existing_move.partner_id.display_name,
        )

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

        lines = self._build_invoice_lines(root, company, warnings=warnings)
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
            "supplier_xml_reference_key": self._reference_key_from_xml(root) if move_type == "in_refund" else False,
            "invoice_line_ids": lines,
        }

    @api.model
    def _reference_key_from_xml(self, root):
        """Clave of the referenced document from InformacionReferencia.

        A credit note may carry several reference nodes; prefer the first whose
        Numero looks like a Hacienda clave (50 digits), else the first non-empty
        Numero. Tag names are namespace-agnostic, so both v4.4 (TipoDocIR) and
        older layouts are covered — we only read Numero.
        """
        first_number = False
        for reference_node in root.xpath("./*[local-name()='InformacionReferencia']"):
            number = (self._xml_text(reference_node, ["Numero"]) or "").strip()
            if not number:
                continue
            if len(number) == 50 and number.isdigit():
                return number
            if not first_number:
                first_number = number
        return first_number

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
        # Never hit the network during tests: the thread flag is not set in every
        # test runner (e.g. post-install), so also check the test_enable config.
        if config["test_enable"] or getattr(threading.current_thread(), "testing", False):
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
                # sudo for the metadata check: the configured journal is a GLOBAL
                # parameter and may belong to another company — reading it with
                # the current user would raise AccessError in multi-company DBs.
                journal_meta = journal.sudo()
                if not journal_meta.exists() or journal_meta.company_id != company or journal_meta.type != "purchase":
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
    def _align_accounting_date(self, move):
        """Force the accounting date to the XML FechaEmision (invoice_date).

        Odoo's own compute pushes vendor-bill accounting dates forward (end of
        the emission month, or today) to keep sequences increasing, so we cannot
        delegate to ``_get_accounting_date`` unconditionally: Costa Rican
        practice books the bill on its emission date. Only when the emission
        date falls in a locked period do we keep Odoo's adjusted date, leaving a
        note in the chatter.
        """
        if not move.invoice_date or move.state != "draft":
            return
        target = move.invoice_date
        locked = False
        if hasattr(move, "_get_violated_lock_dates"):
            try:
                has_tax = move._affect_tax_report()
                if move._get_violated_lock_dates(move.invoice_date, has_tax):
                    locked = True
                    target = move._get_accounting_date(move.invoice_date, has_tax) or move.invoice_date
            except Exception:  # noqa: BLE001 - date alignment must never abort the import
                locked = False
                target = move.invoice_date
        if move.date != target:
            move.date = target
        if locked and target != move.invoice_date:
            move.message_post(
                body=_(
                    "La fecha de emisión %(emision)s cae en un período bloqueado; "
                    "se usó %(fecha)s como fecha contable.",
                    emision=move.invoice_date,
                    fecha=target,
                )
            )

    def _apply_predicted_accounts(self, move):
        """Set each imported line's account from the supplier's history.

        Mirrors l10n_it_edi's import: first Enterprise's predictive-bills method
        (``line._predict_account()``, no args, only exists with account_accountant
        installed), then a deterministic history lookup that also works on
        Community, otherwise the default expense account already set on the line.
        """
        if move.state != "draft":
            return
        for line in move.invoice_line_ids:
            if line.display_type != "product" or line.product_id:
                continue
            predicted = self._predict_line_account(line)
            if not predicted:
                predicted = self._account_from_partner_history(line.name, move.partner_id, move.company_id)
            if predicted and predicted != line.account_id:
                line.account_id = predicted

    @api.model
    def _predict_line_account(self, line):
        """Enterprise account prediction on an already-created draft line."""
        if not hasattr(line, "_predict_account"):
            return self.env["account.account"]
        try:
            predicted = line._predict_account()
        except Exception:  # noqa: BLE001 - prediction must never abort the import
            _logger.warning("Fallo la prediccion de cuenta para la linea %s.", line.id, exc_info=True)
            return self.env["account.account"]
        if isinstance(predicted, int):
            predicted = self.env["account.account"].browse(predicted) if predicted else self.env["account.account"]
        if predicted and predicted.exists() and self._account_is_usable(predicted):
            return predicted
        return self.env["account.account"]

    @api.model
    def _account_is_usable(self, account):
        """True when the account is not archived/deprecated.

        account.account uses ``active`` in Odoo 19 and ``deprecated`` in older
        versions; check whichever field exists.
        """
        if "deprecated" in account._fields:
            return not account.deprecated
        if "active" in account._fields:
            return account.active
        return True

    @api.model
    def _account_from_partner_history(self, label, partner, company):
        """Account used on this supplier's posted bills: same detail first, then
        the most used one. Works on Community (no Enterprise dependency)."""
        if not partner:
            return self.env["account.account"]
        line_model = self.env["account.move.line"]
        domain = [
            ("company_id", "=", company.id),
            ("partner_id", "=", partner.id),
            ("move_id.move_type", "in", ("in_invoice", "in_refund")),
            ("parent_state", "=", "posted"),
            ("display_type", "=", "product"),
            (
                "account_id.account_type",
                "not in",
                ("asset_receivable", "liability_payable", "liquidity", "asset_cash", "equity", "off_balance"),
            ),
        ]
        account_fields = self.env["account.account"]._fields
        if "deprecated" in account_fields:
            domain.append(("account_id.deprecated", "=", False))
        elif "active" in account_fields:
            domain.append(("account_id.active", "=", True))
        if label:
            escaped_label = label.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            line = line_model.search(domain + [("name", "=ilike", escaped_label)], order="id desc", limit=1)
            if line:
                return line.account_id
        groups = line_model._read_group(domain, groupby=["account_id"], aggregates=["__count"])
        if groups:
            account, _count = max(groups, key=lambda group: group[1])
            return account
        return self.env["account.account"]

    @api.model
    def _company_domain(self, model, company):
        if "company_id" in model._fields:
            return [("company_id", "=", company.id)]
        if "company_ids" in model._fields:
            return [("company_ids", "in", [company.id])]
        return []

    @api.model
    def _build_invoice_lines(self, root, company, warnings=None):
        default_account = self._default_expense_account(company)
        line_cmds = []
        other_charges_tax_ids = self._tax_ids_for_other_charges(company)
        seen_other_charge_nodes = set()
        for line_node in root.xpath("//*[local-name()='LineaDetalle']"):
            detail = self._xml_text(line_node, ["Detalle"])
            quantity = self._xml_float(line_node, ["Cantidad"], default=1.0)
            price_unit = self._line_net_price_unit(line_node=line_node, quantity=quantity)

            tax_ids = self._tax_ids_from_line(line_node, company, warnings=warnings, line_label=detail)

            line_vals = {
                "name": detail or _("Línea importada desde XML"),
                "quantity": quantity,
                "price_unit": price_unit,
                "account_id": default_account.id,
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
            "supplier_xml_reference_key": vals.get("supplier_xml_reference_key") or False,
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
            xml_root = etree.fromstring(
                self._sanitize_supplier_xml_payload(payload), parser=self._get_safe_xml_parser()
            )
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

        # First pass: apply any Hacienda response XML (MensajeHacienda) found in
        # the attachments — it may refer to this move or to another one.
        hacienda_responses = []
        for attachment in attachments:
            hacienda_responses.extend(
                self._extract_hacienda_response_payloads(
                    self._attachment_raw_payload(attachment), filename=attachment.name
                )
            )
        responded_moves = self._apply_hacienda_responses(hacienda_responses, company=self.company_id)

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

                duplicate_move = self._find_existing_supplier_move_by_key(
                    supplier_xml_key=vals.get("supplier_xml_key"),
                    company_id=vals.get("company_id"),
                )
                if duplicate_move and duplicate_move != self:
                    if duplicate_move.partner_id.id == vals["partner_id"]:
                        raise UserError(
                            _(
                                "Este XML (clave %(clave)s) ya está importado en %(move)s.",
                                clave=vals.get("supplier_xml_key"),
                                move=duplicate_move.display_name,
                            )
                        )
                    warnings.append(self._duplicate_clave_warning(duplicate_move, vals.get("supplier_xml_key")))

                self.write(self._supplier_xml_move_write_vals(vals, filename=extracted_name or attachment.name))
                self.message_post(body=_("XML leído manualmente desde el adjunto: %s") % (extracted_name or ""))
                self._finalize_supplier_xml_move(self, root, warnings)
                return True

        if responded_moves:
            # No invoice XML, but a Hacienda response was applied: that is a
            # valid outcome — raising here would roll the state write back.
            self.message_post(
                body=_("Respuesta de Hacienda aplicada a: %s.")
                % ", ".join(responded_moves.mapped("display_name"))
            )
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
        # Hacienda responses apply regardless of the draft guards below — the
        # email may only carry the response for an already-imported bill.
        hacienda_responses = self._hacienda_responses_from_message(msg_dict)
        self._apply_hacienda_responses(hacienda_responses, company=self.company_id)
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

            duplicate_move = self._find_existing_supplier_move_by_key(
                supplier_xml_key=vals.get("supplier_xml_key"),
                company_id=vals.get("company_id"),
            )
            if duplicate_move and duplicate_move != self:
                if duplicate_move.partner_id.id == vals["partner_id"]:
                    # Same document already imported: keep this draft empty (the
                    # email attachments stay on it) and point to the existing bill.
                    self.message_post(
                        body=_(
                            "El XML adjunto (clave %(clave)s) ya está importado en %(move)s. "
                            "Este borrador no se completó para evitar un duplicado.",
                            clave=vals.get("supplier_xml_key"),
                            move=duplicate_move.display_name,
                        )
                    )
                    return
                warnings.append(self._duplicate_clave_warning(duplicate_move, vals.get("supplier_xml_key")))

            gateway = self._gateway_from_email_message(msg_dict)
            write_vals = self._supplier_xml_move_write_vals(vals, filename=filename)
            if gateway:
                write_vals["supplier_xml_gateway_id"] = gateway.id
            message_id = self.env["supplier.xml.gateway"]._extract_message_id_from_message(msg_dict)
            if message_id and not self.supplier_xml_message_id:
                write_vals["supplier_xml_message_id"] = message_id
            self.write(write_vals)
            self.message_post(body=_("XML de proveedor leído automáticamente desde los adjuntos del correo."))
            self._finalize_supplier_xml_move(self, root, warnings)
            # Re-apply: a response travelling WITH the invoice matches only now
            # that this move carries its supplier_xml_key.
            self._apply_hacienda_responses(hacienda_responses, company=self.company_id)
            return

    @api.model
    def _find_duplicate_move_for_message(self, msg_dict):
        """Existing move matching an XML attachment's Clave AND its emisor.

        Used to route a resent invoice email to the already-imported bill
        instead of creating a new empty draft. Only matches when the XML's
        emisor VAT equals the existing bill's partner VAT — a reused Clave from
        a different supplier must NOT be merged.
        """
        for _filename, payload in self._extract_xml_attachments_from_message(msg_dict):
            if not payload:
                continue
            try:
                root = self._parse_supplier_xml_root(payload)
            except UserError:
                continue
            clave = self._xml_text(root, ["Clave"])
            if not clave:
                continue
            existing_move = self._find_existing_supplier_move_by_key(supplier_xml_key=clave)
            if not existing_move:
                continue
            emisor_vat = self._normalize_identification(
                self._xml_text(root, ["Emisor", "Identificacion", "Numero"])
            )
            partner_vat = self._normalize_identification(existing_move.partner_id.vat)
            if emisor_vat and partner_vat and emisor_vat == partner_vat:
                return existing_move
        return self.env["account.move"]

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        # A resent invoice (XML with an already-imported Clave, same supplier)
        # must not create a new empty draft: return the existing bill so the
        # mail gateway posts the message and its attachments onto it instead.
        duplicate_move = self._find_duplicate_move_for_message(msg_dict)
        if duplicate_move:
            duplicate_move.message_post(
                body=_(
                    "Correo recibido con un XML ya importado (clave %(clave)s). El mensaje y "
                    "sus adjuntos se agregaron a esta factura en lugar de crear un borrador "
                    "duplicado.",
                    clave=duplicate_move.supplier_xml_key,
                )
            )
            return duplicate_move
        move = super().message_new(msg_dict, custom_values=custom_values)
        move._import_xml_from_message_attachments(msg_dict)
        return move

    def message_update(self, msg_dict, update_vals=None):
        result = super().message_update(msg_dict, update_vals=update_vals)
        for move in self:
            move._import_xml_from_message_attachments(msg_dict)
        return result
