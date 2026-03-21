import inspect

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    supplier_xml_journal_id = fields.Many2one(
        "account.journal",
        string="Diario para XML de proveedor",
        domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
        config_parameter="l10n_cr_supplier_xml_import.default_purchase_journal_id",
        help="Diario por defecto usado al crear facturas desde XML de proveedor.",
    )

    supplier_xml_process_emails_from_date = fields.Datetime(
        string="Procesar correos desde",
        config_parameter="l10n_cr_supplier_xml_import.process_emails_from_date",
        help="Ignora correos anteriores a esta fecha al procesar XML de facturas por correo.",
    )
    supplier_xml_process_emails_to_date = fields.Datetime(
        string="Procesar correos hasta",
        config_parameter="l10n_cr_supplier_xml_import.process_emails_to_date",
        help="Ignora correos posteriores a esta fecha al procesar XML de facturas por correo.",
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
            process_from_datetime = self.supplier_xml_process_emails_from_date
            process_to_datetime = self.supplier_xml_process_emails_to_date
            fetch_context = self._fetchmail_search_all_hints()
            fetch_context.update(
                {
                    "supplier_xml_process_emails_from_date": process_from_datetime,
                    "supplier_xml_process_emails_to_date": process_to_datetime,
                }
            )
            if hasattr(server, "fetch_mail"):
                self._call_fetchmail_method(
                    server.with_context(**fetch_context),
                    "fetch_mail",
                    process_from_datetime,
                    process_to_datetime,
                )
            else:
                self._call_fetchmail_method(
                    server.with_context(**fetch_context),
                    "_fetch_mails",
                    process_from_datetime,
                    process_to_datetime,
                )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Búsqueda completada"),
                    "message": _(
                        "Se ejecutó la búsqueda manual de correos en el servidor %s "
                        "(incluye correos leídos)."
                    )
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

    @api.model
    def _call_fetchmail_method(self, server, method_name, process_from_datetime=False, process_to_datetime=False):
        """Execute fetchmail methods with optional filters when supported.

        This keeps backward compatibility with varying method signatures across
        deployments while allowing integrations to receive explicit date/search
        hints.
        """
        method = getattr(server, method_name)
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            method()
            return

        accepted_kwargs = {}
        parameter_names = set(signature.parameters)

        search_all_hints = self._fetchmail_search_all_hints()
        for param_name, param_value in search_all_hints.items():
            if param_name in parameter_names:
                accepted_kwargs[param_name] = param_value
        if "process_from_datetime" in parameter_names and process_from_datetime:
            accepted_kwargs["process_from_datetime"] = process_from_datetime
        if "process_to_datetime" in parameter_names and process_to_datetime:
            accepted_kwargs["process_to_datetime"] = process_to_datetime
        if "from_date" in parameter_names and process_from_datetime:
            accepted_kwargs["from_date"] = process_from_datetime
        if "to_date" in parameter_names and process_to_datetime:
            accepted_kwargs["to_date"] = process_to_datetime

        method_ctx = server.with_context(**search_all_hints)
        getattr(method_ctx, method_name)(**accepted_kwargs)

    @api.model
    def _fetchmail_search_all_hints(self):
        """Hints reused across signature/context styles to include read emails.

        Distintos forks de fetchmail usan nombres de parámetros diferentes para
        limitar a mensajes no leídos. Este mapa centralizado permite forzar
        búsqueda completa (ALL) sin acoplarse a una sola implementación.
        """
        return {
            "supplier_xml_fetch_search_mode": "ALL",
            "search_mode": "ALL",
            "imap_search": "ALL",
            "fetchmail_search_criterion": "ALL",
            "search_criterion": "ALL",
            "criteria": "ALL",
            "only_unread": False,
            "unread_only": False,
            "seen": True,
            "include_seen": True,
        }
