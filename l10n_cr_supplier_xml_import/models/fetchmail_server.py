"""Compatibilidad para Odoo 19.

En Odoo 19 el modelo ``fetchmail.server`` sigue existiendo, pero ahora vive
dentro del addon ``mail`` (antes era un addon ``fetchmail`` independiente). Este
módulo ya no lo hereda ni lo extiende: la recepción de correos se basa en el
alias de correo entrante (``mail.alias.mixin``) de ``supplier.xml.gateway``.

El archivo se conserva como punto de extensión y para evitar errores en
despliegues que aún lo referencien.
"""
