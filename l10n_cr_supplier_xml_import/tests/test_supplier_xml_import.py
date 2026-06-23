from lxml import etree

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
    # Security: XXE hardening
    # ------------------------------------------------------------------ #
    def test_xxe_entities_not_expanded(self):
        payload = (
            b'<?xml version="1.0"?>'
            b"<!DOCTYPE FacturaElectronica [ <!ENTITY xxe \"SECRETO\"> ]>"
            b"<FacturaElectronica><Emisor><Nombre>&xxe;</Nombre></Emisor></FacturaElectronica>"
        )
        # Default parser would expand the entity (proves the attack is real).
        unsafe = etree.fromstring(payload)
        self.assertEqual(unsafe.xpath("//*[local-name()='Nombre']")[0].text, "SECRETO")

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
        self.assertEqual(
            self.move_model.search_count([("supplier_xml_key", "=", "UNIQUE-CLAVE-1")]),
            1,
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
