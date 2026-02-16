from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    supplier_xml_process_emails_from_date = fields.Datetime(
        string="Procesar correos desde",
        config_parameter="l10n_cr_supplier_xml_import.process_emails_from_date",
        help="Ignora correos anteriores a esta fecha al procesar XML de facturas por correo.",
    )
    supplier_xml_mail_server_ref = fields.Reference(
        selection="_selection_supplier_xml_mail_servers",
        string="Servidor de correo",
        help="Servidor entrante (fetchmail) o saliente asociado a la búsqueda manual de correos.",
    )

    @api.model
    def _selection_supplier_xml_mail_servers(self):
        models = []
        if self.env.registry.get("fetchmail.server"):
            models.append(("fetchmail.server", _("Servidor entrante")))
        models.append(("ir.mail_server", _("Servidor saliente")))
        return models

    @api.model
    def get_values(self):
        values = super().get_values()
        server_ref = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_cr_supplier_xml_import.mail_server_ref"
        )
        if not server_ref:
            legacy_id = self.env["ir.config_parameter"].sudo().get_param(
                "l10n_cr_supplier_xml_import.mail_server_id"
            )
            if legacy_id and legacy_id.isdigit():
                server_ref = f"ir.mail_server,{int(legacy_id)}"

        if server_ref and "," in server_ref:
            model_name, rec_id = server_ref.split(",", 1)
            if rec_id.isdigit() and self.env.registry.get(model_name):
                values["supplier_xml_mail_server_ref"] = f"{model_name},{int(rec_id)}"
        return values

    def set_values(self):
        super().set_values()
        server_ref = self.supplier_xml_mail_server_ref
        value = f"{server_ref._name},{server_ref.id}" if server_ref else ""
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("l10n_cr_supplier_xml_import.mail_server_ref", value)
        if server_ref and server_ref._name == "ir.mail_server":
            icp.set_param("l10n_cr_supplier_xml_import.mail_server_id", server_ref.id)

    @api.model
    def _get_supplier_xml_mail_server(self):
        server_ref = self.env["ir.config_parameter"].sudo().get_param(
            "l10n_cr_supplier_xml_import.mail_server_ref"
        )
        if not server_ref:
            legacy_id = self.env["ir.config_parameter"].sudo().get_param(
                "l10n_cr_supplier_xml_import.mail_server_id"
            )
            if legacy_id and legacy_id.isdigit():
                server_ref = f"ir.mail_server,{int(legacy_id)}"

        if not server_ref or "," not in server_ref:
            return self.env["ir.mail_server"]

        model_name, rec_id = server_ref.split(",", 1)
        if not rec_id.isdigit() or not self.env.registry.get(model_name):
            return self.env["ir.mail_server"]
        return self.env[model_name].browse(int(rec_id))

    def action_supplier_xml_search_emails(self):
        self.ensure_one()

        server = self._get_supplier_xml_mail_server()
        if not server:
            raise UserError(_("Seleccione un servidor de correo en la configuración general."))

        if server._name == "fetchmail.server":
            if hasattr(server, "fetch_mail"):
                server.fetch_mail()
            else:
                server._fetch_mails()
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Búsqueda completada"),
                    "message": _("Se ejecutó la búsqueda manual de correos en el servidor %s.")
                    % server.display_name,
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
