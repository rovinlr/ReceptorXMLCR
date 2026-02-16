from odoo import fields, models


class FetchmailServer(models.Model):
    _inherit = "fetchmail.server"

    process_emails_from_date = fields.Date(
        string="Procesar correos desde",
        help="Fecha mínima global para procesar correos de facturas de proveedor.",
    )
