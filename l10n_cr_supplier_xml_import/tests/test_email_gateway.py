from odoo.tests import tagged

from .common import SupplierXmlCommon, factura_xml


@tagged("post_install", "-at_install")
class TestSupplierEmailGateway(SupplierXmlCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.gateway = cls.env["supplier.xml.gateway"].create(
            {
                "name": "Buzón Test",
                "company_id": cls.env.company.id,
            }
        )

    def _bodies(self, record):
        return " ".join(m.body or "" for m in record.message_ids)

    # ------------------------------------------------------------------ #
    # Date parsing (regression for tz-aware vs naive TypeError)
    # ------------------------------------------------------------------ #
    def test_parse_email_datetime_is_naive(self):
        parsed = self.gateway._parse_email_datetime({"date": "Mon, 23 Jun 2026 10:00:00 +0000"})
        self.assertTrue(parsed)
        self.assertIsNone(parsed.tzinfo, "La fecha debe normalizarse a naive UTC.")
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2026, 6, 23))

    def test_date_filter_does_not_crash_with_tzaware_email(self):
        # Regression: comparing a tz-aware email date against a naive config
        # datetime used to raise TypeError and abort the email.
        self.env["ir.config_parameter"].sudo().set_param(
            "l10n_cr_supplier_xml_import.process_emails_from_date", "2030-01-01 00:00:00"
        )
        msg_dict = {
            "subject": "Vieja",
            "date": "Mon, 23 Jun 2026 10:00:00 +0000",
            "attachments": [("f.xml", factura_xml(), "application/xml")],
        }
        # Must not raise; email is ignored by date.
        self.gateway._process_supplier_email(msg_dict)
        self.assertIn("ignorado por fecha", self._bodies(self.gateway))
        self.assertFalse(self.gateway.move_ids)

    # ------------------------------------------------------------------ #
    # No XML in the email (e.g. a plain PDF that is not an e-invoice)
    # ------------------------------------------------------------------ #
    def test_email_without_xml_is_graceful(self):
        msg_dict = {
            "subject": "Solo PDF",
            "attachments": [("factura.pdf", b"%PDF-1.4 contenido falso", "application/pdf")],
        }
        # Must NOT raise and must NOT create a move.
        self.gateway._process_supplier_email(msg_dict)
        self.assertIn("sin XML de factura electrónica", self._bodies(self.gateway))
        self.assertFalse(self.gateway.move_ids)

    # ------------------------------------------------------------------ #
    # XML present but invalid -> surface the real reason, do not lose mail
    # ------------------------------------------------------------------ #
    def test_email_with_invalid_xml_surfaces_reason(self):
        msg_dict = {
            "subject": "Receptor equivocado",
            "attachments": [("f.xml", factura_xml(receptor_numero="3101000000"), "application/xml")],
        }
        self.gateway._process_supplier_email(msg_dict)
        bodies = self._bodies(self.gateway)
        self.assertIn("No se pudo crear la factura", bodies)
        self.assertIn("receptor no coincide", bodies)
        self.assertFalse(self.gateway.move_ids)

    # ------------------------------------------------------------------ #
    # Happy path + Message-ID dedup
    # ------------------------------------------------------------------ #
    def test_email_creates_move_and_sets_message_id(self):
        msg_dict = {
            "subject": "Factura proveedor",
            "message_id": "<msg-1@test>",
            "date": "Mon, 23 Jun 2026 10:00:00 +0000",
            "attachments": [("f.xml", factura_xml(clave="MAIL-CLAVE-1"), "application/xml")],
        }
        self.gateway._process_supplier_email(msg_dict)
        self.assertEqual(len(self.gateway.move_ids), 1)
        move = self.gateway.move_ids
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.supplier_xml_message_id, "<msg-1@test>")

        # Re-delivery of the same Message-ID must be skipped (no duplicate move).
        self.gateway._process_supplier_email(msg_dict)
        self.assertEqual(len(self.gateway.move_ids), 1)
        self.assertIn("ya procesado previamente", self._bodies(self.gateway))

    def test_resent_email_same_clave_attaches_to_existing_move(self):
        # The supplier re-sends the invoice in a NEW email (different
        # Message-ID): no new move; the email attachments are added to the
        # existing bill and the chatter explains it.
        first_msg = {
            "subject": "Factura original",
            "message_id": "<resend-1@test>",
            "attachments": [("f.xml", factura_xml(clave="MAIL-DUP-1"), "application/xml")],
        }
        self.gateway._process_supplier_email(first_msg)
        self.assertEqual(len(self.gateway.move_ids), 1)
        move = self.gateway.move_ids
        attachments_before = self.env["ir.attachment"].search_count(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id)]
        )

        second_msg = {
            "subject": "Reenvío factura",
            "message_id": "<resend-2@test>",
            "attachments": [("f.xml", factura_xml(clave="MAIL-DUP-1"), "application/xml")],
        }
        self.gateway._process_supplier_email(second_msg)

        self.assertEqual(len(self.gateway.move_ids), 1, "No debe crearse una segunda factura.")
        attachments_after = self.env["ir.attachment"].search_count(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id)]
        )
        self.assertGreater(
            attachments_after, attachments_before, "Los adjuntos del reenvío deben quedar en la factura."
        )
        self.assertTrue(
            any("ya importado anteriormente" in (m.body or "") for m in move.message_ids),
            "El chatter debe indicar que el XML ya estaba importado.",
        )
        # The original Message-ID must be preserved (not overwritten by the resend).
        self.assertEqual(move.supplier_xml_message_id, "<resend-1@test>")
