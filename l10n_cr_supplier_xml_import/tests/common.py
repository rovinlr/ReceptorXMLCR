from odoo.addons.account.tests.common import AccountTestInvoicingCommon


CR_NS = "https://cdn.comprobanteselectronicos.go.cr/xml-schemas/v4.4/facturaElectronica"


def factura_xml(
    root_tag="FacturaElectronica",
    clave="50601012600310112345600100001010000000001199999999",
    consecutivo="00100001010000000001",
    fecha="2026-06-20T10:00:00-06:00",
    emisor_nombre="Proveedor Demo SA",
    emisor_numero="3101123456",
    receptor_numero="3101999999",
    moneda="CRC",
    lineas=None,
    total_comprobante="2260.00",
):
    """Build a Hacienda-like supplier XML (namespaced) for tests."""
    if lineas is None:
        lineas = [linea_detalle()]
    lineas_xml = "".join(lineas)
    return (
        f'<{root_tag} xmlns="{CR_NS}">'
        f"<Clave>{clave}</Clave>"
        f"<NumeroConsecutivo>{consecutivo}</NumeroConsecutivo>"
        f"<FechaEmision>{fecha}</FechaEmision>"
        f"<Emisor><Nombre>{emisor_nombre}</Nombre>"
        f"<Identificacion><Tipo>02</Tipo><Numero>{emisor_numero}</Numero></Identificacion></Emisor>"
        f"<Receptor><Nombre>Mi Empresa</Nombre>"
        f"<Identificacion><Tipo>02</Tipo><Numero>{receptor_numero}</Numero></Identificacion></Receptor>"
        f"<DetalleServicio>{lineas_xml}</DetalleServicio>"
        f"<ResumenFactura>"
        f"<CodigoTipoMoneda><CodigoMoneda>{moneda}</CodigoMoneda></CodigoTipoMoneda>"
        f"<TotalComprobante>{total_comprobante}</TotalComprobante>"
        f"</ResumenFactura>"
        f"</{root_tag}>"
    ).encode("utf-8")


def linea_detalle(
    numero=1,
    detalle="Servicio de prueba",
    cantidad="2",
    precio_unitario="1000.00",
    monto_total="2000.00",
    subtotal="2000.00",
    tarifa="13.00",
    codigo_tarifa="08",
    monto_impuesto="260.00",
    monto_total_linea="2260.00",
    impuesto=True,
):
    impuesto_xml = ""
    if impuesto:
        impuesto_xml = (
            f"<Impuesto>"
            f"<Codigo>01</Codigo>"
            f"<CodigoTarifaIVA>{codigo_tarifa}</CodigoTarifaIVA>"
            f"<Tarifa>{tarifa}</Tarifa>"
            f"<Monto>{monto_impuesto}</Monto>"
            f"</Impuesto>"
        )
    return (
        f"<LineaDetalle>"
        f"<NumeroLinea>{numero}</NumeroLinea>"
        f"<Detalle>{detalle}</Detalle>"
        f"<Cantidad>{cantidad}</Cantidad>"
        f"<PrecioUnitario>{precio_unitario}</PrecioUnitario>"
        f"<MontoTotal>{monto_total}</MontoTotal>"
        f"<SubTotal>{subtotal}</SubTotal>"
        f"{impuesto_xml}"
        f"<MontoTotalLinea>{monto_total_linea}</MontoTotalLinea>"
        f"</LineaDetalle>"
    )


class SupplierXmlCommon(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.move_model = cls.env["account.move"]

        # Receiver VAT must match the XML's Receptor number (normalized).
        cls.env.company.vat = "3101999999"

        # Make sure CRC exists and is active (the parser rejects inactive ones).
        crc = cls.env.ref("base.CRC")
        crc.active = True
        cls.crc = crc

        # A 13% purchase tax so rate-based mapping (Tarifa 13) finds a match.
        cls.tax_iva_13 = cls.env["account.tax"].create(
            {
                "name": "IVA 13% Compra (test)",
                "amount": 13.0,
                "amount_type": "percent",
                "type_tax_use": "purchase",
                "company_id": cls.env.company.id,
            }
        )
