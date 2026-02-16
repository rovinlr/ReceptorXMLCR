# ReceptorXMLCR

Módulo para Odoo 19 que importa XML de facturas de proveedor de Costa Rica.

## Funcionalidades
- En Odoo 19 funciona con alias de correo entrante de `mail` (sin dependencia de `fetchmail`).
- Detecta automáticamente si el XML es:
  - `FacturaElectronica` → factura de proveedor (`in_invoice`).
  - `NotaCreditoElectronica` → nota de crédito de proveedor (`in_refund`).
- Valida que la cédula del receptor (`Receptor/Identificacion/Numero`) coincida con el VAT de la compañía en Odoo.
- Busca proveedor por identificación (`Emisor/Identificacion/Numero`) y lo crea si no existe.
- Carga los datos principales:
  - Referencia (`ref`) desde `NumeroConsecutivo`.
  - Fecha de factura (`invoice_date`) desde `FechaEmision`.
  - Líneas (`LineaDetalle`) con:
    - `name` desde `Detalle`.
    - `quantity` desde `Cantidad`.
    - `price_unit` desde `PrecioUnitario`.
- Intenta mapear impuestos por `CodigoTarifaIVA`; si no encuentra coincidencia, no importa ese impuesto.

## Uso
1. Instalar el módulo `l10n_cr_supplier_xml_import`.
2. En una factura de proveedor o nota de crédito de proveedor, usar el botón **Importar XML proveedor**.
3. Cargar el archivo XML y confirmar.

Si la cédula del receptor no coincide, se muestra este mensaje:

> La cédula del receptor no coincide con la del sistema que recibe.
