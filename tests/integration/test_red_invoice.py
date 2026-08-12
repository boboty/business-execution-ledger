from decimal import Decimal

from bel.application.import_invoices import import_invoices
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.models import InvoiceModel


def test_negative_gross_amount_invoice_imports_normally(db_session, invoice_workbook_factory):
    """Red invoices (红票) carry negative amounts. They must be saved as
    a normal negative-amount Invoice fact, never dropped or rejected.
    See spec section 8/30."""
    rows = [
        ["Tmpl", " ", "数电票（普通发票）", "2026-07-15", None, "DIGITAL-RED-001", "Seller B",
         "Refund Item", None, "件", -1, "1052.48", -1052.48, 1.0, -10.52, -1063.00, "正常", -1052.48, -10.52, -1063.00],
    ]
    path = invoice_workbook_factory(rows)

    result = import_invoices(db_session, path, InvoiceDirection.PURCHASE)

    assert result.invoices_created == 1
    invoice = db_session.query(InvoiceModel).one()
    assert invoice.gross_amount == Decimal("-1063.00")
    assert invoice.net_amount == Decimal("-1052.48")
    assert invoice.tax_amount == Decimal("-10.52")
    assert invoice.seller == "Seller B"
