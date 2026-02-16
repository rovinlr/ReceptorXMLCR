from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    supplier_xml_process_emails_from_date = fields.Date(
        string="Procesar correos desde",
        config_parameter="l10n_cr_supplier_xml_import.process_emails_from_date",
        help="Ignora correos anteriores a esta fecha al procesar XML de facturas por correo.",
    )
    supplier_xml_mail_server_id = fields.Many2one(
        "ir.mail_server",
        string="Servidor de correo",
        config_parameter="l10n_cr_supplier_xml_import.mail_server_id",
        help="Servidor de correo asociado a la búsqueda manual de correos.",
    )

    @api.model
    def _get_supplier_xml_mail_server(self):
        server_id = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_cr_supplier_xml_import.mail_server_id"
        )
        return self.env["ir.mail_server"].browse(int(server_id)) if server_id else self.env["ir.mail_server"]

    def action_supplier_xml_search_emails(self):
        self.ensure_one()

        server = self._get_supplier_xml_mail_server()
        if not server:
            raise UserError(_("Seleccione un servidor de correo en la configuración general."))

        fetchmail_model = self.env.registry.get("fetchmail.server")
        if fetchmail_model:
            fetchmail_server = self.env["fetchmail.server"].search([("name", "=", server.name)], limit=1)
            if fetchmail_server:
                if hasattr(fetchmail_server, "fetch_mail"):
                    fetchmail_server.fetch_mail()
                else:
                    fetchmail_server._fetch_mails()
                return {
                    "type": "ir.actions.client",
                    "tag": "display_notification",
                    "params": {
                        "title": _("Búsqueda completada"),
                        "message": _("Se ejecutó la búsqueda manual de correos en el servidor %s.")
                        % server.name,
                        "type": "success",
                        "sticky": False,
                    },
                }

        server_action = self.env["ir.actions.server"].search(
            [
                ("state", "=", "code"),
                ("code", "ilike", "_fetch_mails()"),
            ],
            limit=1,
        )
        if server_action:
            server_action.with_context(active_model="fetchmail.server", active_ids=[], active_id=False).run()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Búsqueda completada"),
                    "message": _(
                        "Se ejecutó la acción de servidor para revisar correos entrantes."
                    ),
                    "type": "success",
                    "sticky": False,
                },
            }

        raise UserError(
            _(
                "No se encontró un mecanismo para revisar correos entrantes. "
                "Verifique que exista el modelo fetchmail o una acción de servidor con model._fetch_mails()."
            )
        )
