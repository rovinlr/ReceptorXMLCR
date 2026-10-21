from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import SupplierXmlCommon, factura_xml, linea_detalle


@tagged("post_install", "-at_install")
class TestSupplierXmlImport(SupplierXmlCommon):
    # ------------------------------------------------------------------ #
    # Parsing / helpers
    # ------------------------------------------------------------------ #
    def test_move_type_from_root_tag(self):
        self.assertEqual(self.move_model._get_move_type_from_xml("FacturaElectronica"), "in_invoice")
        self.assertEqual(self.move_model._get_move_type_from_xml("NotaCreditoElectronica"), "in_refund")
        with self.assertRaises(UserError):
            self.move_model._get_move_type_from_xml("MensajeReceptor")

    def test_normalize_identification(self):
        self.assertEqual(self.move_model._normalize_identification("3-101-123456"), "3101123456")
        self.assertEqual(self.move_model._normalize_identification(" cr3101 "), "CR3101")
        self.assertEqual(self.move_model._normalize_identification(None), "")

    def test_currency_code_defaults_to_crc(self):
        root = self.move_model._parse_supplier_xml_root(factura_xml(moneda="USD"))
        self.assertEqual(self.move_model._currency_code_from_xml(root), "USD")
        # When ResumenFactura has no currency node, fallback is CRC.
        bare = (
            '<FacturaElectronica xmlns="urn:x"><ResumenFactura>'
            "<TotalComprobante>1</TotalComprobante></ResumenFactura></FacturaElectronica>"
        ).encode()
        root_bare = self.move_model._parse_supplier_xml_root(bare)
        self.assertEqual(self.move_model._currency_code_from_xml(root_bare), "CRC")

    def test_line_net_price_unit_uses_subtotal(self):
        root = self.move_model._parse_supplier_xml_root(factura_xml())
        line_node = root.xpath("//*[local-name()='LineaDetalle']")[0]
        # SubTotal 2000 / Cantidad 2 = 1000 (net of VAT).
        self.assertEqual(self.move_model._line_net_price_unit(line_node, 2.0), 1000.0)

    # ------------------------------------------------------------------ #
    # Payload sanitizing (mangled BOM / junk before the XML declaration)
    # ------------------------------------------------------------------ #
    def test_import_with_mangled_bom_prefix(self):
        # Real-world case: some providers prepend EF BF BD sequences (a UTF-8
        # BOM corrupted by a bad encode/decode cycle) before the declaration.
        payload = b"\xef\xbf\xbd" * 3 + factura_xml(clave="BOM-1")
        self.assertTrue(self.move_model._is_supported_supplier_xml_payload(payload))
        move = self.move_model.create_from_supplier_xml(
            xml_content=payload,
            company_id=self.env.company.id,
        )
        self.assertEqual(move.supplier_xml_key, "BOM-1")

    def test_import_with_real_utf8_bom(self):
        payload = b"\xef\xbb\xbf" + factura_xml(clave="BOM-2")
        move = self.move_model.create_from_supplier_xml(
            xml_content=payload,
            company_id=self.env.company.id,
        )
        self.assertEqual(move.supplier_xml_key, "BOM-2")

    def test_sanitize_keeps_clean_payload(self):
        payload = factura_xml(clave="BOM-3")
        self.assertEqual(self.move_model._sanitize_supplier_xml_payload(payload), payload)

    # ------------------------------------------------------------------ #
    # Security: XXE hardening
    # ------------------------------------------------------------------ #
    def test_xxe_entities_not_expanded(self):
        payload = (
            b'<?xml version="1.0"?>'
            b"<!DOCTYPE FacturaElectronica [ <!ENTITY xxe \"SECRETO\"> ]>"
            b"<FacturaElectronica><Emisor><Nombre>&xxe;</Nombre></Emisor></FacturaElectronica>"
        )
        # Note: whether the DEFAULT parser expands the entity depends on the
        # libxml2 version (2.13+ no longer does), so we only assert on ours.
        # Hardened parser must neutralize it (no expansion) or reject the doc.
        try:
            safe = self.move_model._parse_supplier_xml_root(payload)
        except UserError:
            return
        self.assertNotEqual(safe.xpath("//*[local-name()='Nombre']")[0].text, "SECRETO")

    # ------------------------------------------------------------------ #
    # Receiver validation
    # ------------------------------------------------------------------ #
    def test_receiver_mismatch_raises(self):
        with self.assertRaises(UserError):
            self.move_model.create_from_supplier_xml(
                xml_content=factura_xml(receptor_numero="3101000000"),
                company_id=self.env.company.id,
            )

    # ------------------------------------------------------------------ #
    # Full import
    # ------------------------------------------------------------------ #
    def test_import_invoice_creates_move_partner_and_tax(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.partner_id.vat, "3101123456")
        self.assertEqual(move.partner_id.name, "Proveedor Demo SA")
        self.assertEqual(move.currency_id, self.crc)
        self.assertEqual(move.ref, "00100001010000000001")
        self.assertEqual(len(move.invoice_line_ids), 1)

        line = move.invoice_line_ids
        self.assertEqual(line.price_unit, 1000.0)
        self.assertEqual(line.quantity, 2.0)
        self.assertIn(self.tax_iva_13, line.tax_ids)
        # 2000 net + 13% = 2260, matches TotalComprobante (no warning expected).
        self.assertEqual(move.amount_total, 2260.0)
        self.assertFalse(
            any("Avisos al importar" in (m.body or "") for m in move.message_ids),
            "No debería haber avisos cuando el total coincide.",
        )

    def test_import_credit_note(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(root_tag="NotaCreditoElectronica"),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.move_type, "in_refund")

    def test_duplicate_clave_returns_existing_move(self):
        xml = factura_xml(clave="UNIQUE-CLAVE-1")
        first = self.move_model.create_from_supplier_xml(xml_content=xml, company_id=self.env.company.id)
        second = self.move_model.create_from_supplier_xml(xml_content=xml, company_id=self.env.company.id)
        self.assertEqual(first, second)
        self.assertTrue(second.env.context.get("supplier_xml_duplicate"))
        self.assertEqual(
            self.move_model.search_count([("supplier_xml_key", "=", "UNIQUE-CLAVE-1")]),
            1,
        )

    def test_duplicate_clave_from_other_supplier_creates_new_move(self):
        # A different emisor reusing an existing Clave must NOT be merged into
        # the other supplier's bill: new document + warning.
        first = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="DUP-CLAVE-2"),
            company_id=self.env.company.id,
        )
        second = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(
                clave="DUP-CLAVE-2",
                emisor_numero="3101777777",
                emisor_nombre="Otro Proveedor SA",
            ),
            company_id=self.env.company.id,
        )
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.partner_id, second.partner_id)
        self.assertEqual(
            self.move_model.search_count([("supplier_xml_key", "=", "DUP-CLAVE-2")]),
            2,
        )
        self.assertTrue(
            any("misma clave" in (m.body or "") for m in second.message_ids),
            "Debe advertir que otra factura de otro proveedor tiene la misma clave.",
        )

    # ------------------------------------------------------------------ #
    # Consistency checks (totals / unmapped taxes)
    # ------------------------------------------------------------------ #
    def test_total_mismatch_posts_warning(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(total_comprobante="9999.00"),
            company_id=self.env.company.id,
        )
        self.assertTrue(
            any("no coincide con el total del XML" in (m.body or "") for m in move.message_ids),
            "Debe avisar cuando el total importado no coincide con el XML.",
        )

    def test_unmapped_tax_posts_warning_and_skips_tax(self):
        # Tarifa 99 has no matching purchase tax -> line imported without tax + warning.
        line = linea_detalle(tarifa="99.00", codigo_tarifa="99", monto_impuesto="0.00")
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(lineas=[line], total_comprobante="2000.00"),
            company_id=self.env.company.id,
        )
        self.assertFalse(move.invoice_line_ids.tax_ids)
        self.assertTrue(
            any("No se encontró un impuesto de compra" in (m.body or "") for m in move.message_ids),
            "Debe avisar cuando un impuesto del XML no se pudo mapear.",
        )

    # ------------------------------------------------------------------ #
    # Supplier creation: contact data / location from the Emisor node
    # ------------------------------------------------------------------ #
    def test_new_supplier_gets_contact_data_from_xml(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="CONTACT-1"),
            company_id=self.env.company.id,
        )
        partner = move.partner_id
        self.assertEqual(partner.email, "proveedor@demo.cr")
        # phone_validation (si está instalado) puede reformatear el número.
        self.assertIn("22223333", (partner.phone or "").replace(" ", "").replace("-", ""))
        self.assertEqual(partner.street, "100m norte del parque")
        self.assertEqual(partner.country_id.code, "CR")
        self.assertEqual(partner.company_type, "company")

    def test_new_supplier_person_for_cedula_fisica(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(
                clave="CONTACT-2", emisor_numero="101110111", emisor_tipo="01"
            ),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.partner_id.company_type, "person")

    def test_existing_supplier_is_not_modified(self):
        existing = self.env["res.partner"].create(
            {"name": "Proveedor Ya Existente", "vat": "3101123456", "email": "original@x.cr"}
        )
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="CONTACT-3"),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.partner_id, existing)
        self.assertEqual(existing.email, "original@x.cr")
        self.assertEqual(existing.name, "Proveedor Ya Existente")

    def test_location_from_xml_with_einvoice_catalogs(self):
        if "fp.province" not in self.env or "fp_province_id" not in self.env["res.partner"]._fields:
            self.skipTest("l10n_cr_einvoice no está instalado en esta base.")
        province = self.env["fp.province"].search([("code", "=", "1")], limit=1)
        if not province:
            province = self.env["fp.province"].create({"code": "1", "name": "San José"})
        canton = self.env["fp.canton"].search(
            [("province_id", "=", province.id), ("code", "=", "08")], limit=1
        )
        if not canton:
            canton = self.env["fp.canton"].create(
                {"code": "08", "name": "Goicoechea", "province_id": province.id}
            )
        district = self.env["fp.district"].search(
            [("canton_id", "=", canton.id), ("code", "=", "02")], limit=1
        )
        if not district:
            district = self.env["fp.district"].create(
                {"code": "02", "name": "San Francisco", "canton_id": canton.id}
            )

        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="LOC-1"),
            company_id=self.env.company.id,
        )
        partner = move.partner_id
        self.assertEqual(partner.fp_province_id, province)
        self.assertEqual(partner.fp_canton_id, canton)
        self.assertEqual(partner.fp_district_id, district)
        self.assertEqual(partner.city, canton.name)

    # ------------------------------------------------------------------ #
    # Accounting date follows the XML FechaEmision
    # ------------------------------------------------------------------ #
    def test_accounting_date_matches_invoice_date(self):
        # Odoo pushes vendor-bill dates to end-of-month/today for sequences;
        # the import must force date == FechaEmision instead.
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="DATE-1"),
            company_id=self.env.company.id,
        )
        self.assertEqual(str(move.invoice_date), "2026-06-20")
        self.assertEqual(move.date, move.invoice_date)

    def test_accounting_date_locked_period_adjusts_and_warns(self):
        try:
            self.env.company.fiscalyear_lock_date = "2026-06-30"
        except Exception:
            self.skipTest("No se pudo establecer la fecha de bloqueo en esta base de test.")
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="DATE-LOCK-1"),
            company_id=self.env.company.id,
        )
        self.assertEqual(str(move.invoice_date), "2026-06-20")
        self.assertGreater(move.date, fields.Date.to_date("2026-06-30"))
        self.assertTrue(
            any("período bloqueado" in (m.body or "") for m in move.message_ids),
            "Debe avisar en el chatter cuando la fecha de emisión cae en período bloqueado.",
        )

    # ------------------------------------------------------------------ #
    # Account from supplier history / Enterprise prediction
    # ------------------------------------------------------------------ #
    def _create_posted_bill(self, partner, account, labels):
        bill = self.move_model.create(
            {
                "move_type": "in_invoice",
                "partner_id": partner.id,
                "invoice_date": "2026-05-15",
                "invoice_line_ids": [
                    (0, 0, {"name": label, "quantity": 1, "price_unit": 100, "account_id": account.id})
                    for label in labels
                ],
            }
        )
        bill.action_post()
        return bill

    def _history_expense_account(self):
        return self.env["account.account"].create(
            {
                "code": "699998",
                "name": "Gasto historial (test)",
                "account_type": "expense",
            }
        )

    def test_account_fallback_without_history(self):
        # A brand-new partner has no history -> the default expense account stays.
        default = self.move_model._default_expense_account(self.env.company)
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="HIST-0"),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.invoice_line_ids.account_id, default)

    def test_account_from_history_same_detail(self):
        partner = self.env["res.partner"].create({"name": "Proveedor Demo SA", "vat": "3101123456"})
        history_account = self._history_expense_account()
        # Same detail text as the XML line ("Servicio de prueba").
        self._create_posted_bill(partner, history_account, ["Servicio de prueba"])

        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="HIST-1"),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.partner_id, partner)
        self.assertEqual(move.invoice_line_ids.account_id, history_account)

    def test_account_from_history_most_used(self):
        partner = self.env["res.partner"].create({"name": "Proveedor Demo SA", "vat": "3101123456"})
        history_account = self._history_expense_account()
        # Different labels: no exact-detail match, so the most used account wins.
        self._create_posted_bill(partner, history_account, ["Alquiler oficina", "Alquiler parqueo"])

        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="HIST-2"),
            company_id=self.env.company.id,
        )
        self.assertEqual(move.invoice_line_ids.account_id, history_account)

    def test_enterprise_prediction_is_used_when_available(self):
        default = self.move_model._default_expense_account(self.env.company)
        other = self.env["account.account"].search(
            [("account_type", "=", "expense"), ("id", "!=", default.id)], limit=1
        )
        if not other:
            self.skipTest("Se necesita más de una cuenta de gasto para probar la predicción.")

        # Enterprise's _predict_account takes no arguments and runs on the line.
        line_model_cls = type(self.env["account.move.line"])
        with patch.object(line_model_cls, "_predict_account", create=True, return_value=other.id):
            move = self.move_model.create_from_supplier_xml(
                xml_content=factura_xml(clave="PREDICT-1"),
                company_id=self.env.company.id,
            )
        self.assertEqual(move.invoice_line_ids.account_id, other)

    # ------------------------------------------------------------------ #
    # Credit note linking via InformacionReferencia
    # ------------------------------------------------------------------ #
    def test_credit_note_links_to_existing_invoice(self):
        invoice = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="NCLINK-ORIG-1"),
            company_id=self.env.company.id,
        )
        credit_note = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(
                root_tag="NotaCreditoElectronica",
                clave="NCLINK-NC-1",
                referencia="NCLINK-ORIG-1",
            ),
            company_id=self.env.company.id,
        )
        self.assertEqual(credit_note.supplier_xml_reference_key, "NCLINK-ORIG-1")
        self.assertEqual(credit_note.reversed_entry_id, invoice)
        self.assertTrue(
            any("ligada automáticamente" in (m.body or "").lower() for m in invoice.message_ids),
            "La factura debe tener la nota del enlace en el chatter.",
        )

    def test_invoice_adopts_orphan_credit_note(self):
        # The NC arrives BEFORE the invoice (emails out of order).
        credit_note = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(
                root_tag="NotaCreditoElectronica",
                clave="NCLINK-NC-2",
                referencia="NCLINK-ORIG-2",
            ),
            company_id=self.env.company.id,
        )
        self.assertFalse(credit_note.reversed_entry_id)
        self.assertTrue(
            any("no está importado" in (m.body or "") for m in credit_note.message_ids),
            "Debe avisar que la factura referenciada no existe todavía.",
        )

        invoice = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="NCLINK-ORIG-2"),
            company_id=self.env.company.id,
        )
        self.assertEqual(credit_note.reversed_entry_id, invoice)

    def test_credit_note_reference_from_other_supplier_not_linked(self):
        invoice = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="NCLINK-ORIG-3"),
            company_id=self.env.company.id,
        )
        credit_note = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(
                root_tag="NotaCreditoElectronica",
                clave="NCLINK-NC-3",
                referencia="NCLINK-ORIG-3",
                emisor_numero="3101777777",
                emisor_nombre="Otro Proveedor SA",
            ),
            company_id=self.env.company.id,
        )
        self.assertNotEqual(credit_note.partner_id, invoice.partner_id)
        self.assertFalse(credit_note.reversed_entry_id, "No debe ligarse a la factura de otro proveedor.")
        self.assertTrue(
            any("OTRO proveedor" in (m.body or "") for m in credit_note.message_ids),
        )

    # ------------------------------------------------------------------ #
    # Posting guard for Hacienda-rejected documents
    # ------------------------------------------------------------------ #
    def test_post_rejected_opens_confirmation_wizard(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="REJ-POST-1"),
            company_id=self.env.company.id,
        )
        move.write({"supplier_xml_hacienda_state": "rechazado"})

        action = move.action_post()
        self.assertIsInstance(action, dict, "Debe devolver la acción del wizard, no postear.")
        self.assertEqual(action.get("res_model"), "supplier.xml.rejected.wizard")
        self.assertEqual(move.state, "draft", "La factura NO debe confirmarse sin pasar por el wizard.")

        wizard = self.env["supplier.xml.rejected.wizard"].create({"move_ids": [(6, 0, move.ids)]})
        wizard.action_confirm_anyway()
        self.assertEqual(move.state, "posted", "El wizard debe permitir confirmar de todos modos.")

    def test_post_accepted_posts_normally(self):
        move = self.move_model.create_from_supplier_xml(
            xml_content=factura_xml(clave="REJ-POST-2"),
            company_id=self.env.company.id,
        )
        move.write({"supplier_xml_hacienda_state": "aceptado"})
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_invoice_without_lines_raises(self):
        empty = (
            '<FacturaElectronica xmlns="urn:x">'
            "<Clave>X</Clave>"
            "<Emisor><Identificacion><Numero>3101123456</Numero></Identificacion></Emisor>"
            "<Receptor><Identificacion><Numero>3101999999</Numero></Identificacion></Receptor>"
            "</FacturaElectronica>"
        ).encode()
        with self.assertRaises(UserError):
            self.move_model.create_from_supplier_xml(xml_content=empty, company_id=self.env.company.id)
