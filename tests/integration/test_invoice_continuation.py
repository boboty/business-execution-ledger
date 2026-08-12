from decimal import Decimal

from bel.application.import_invoices import import_invoices
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.models import EvidenceFragmentModel, InvoiceItemModel, InvoiceModel


def test_one_invoice_three_items_no_forward_fill(db_session, invoice_workbook_factory):
    """Spec section 7/30: a multi-item invoice's first row carries both
    the header and item 1; subsequent items are continuation rows with
    only item fields. Must become exactly 1 Invoice + 3 InvoiceItems,
    and the continuation rows' raw_data must NOT have invoice_no/seller/
    date filled in from the header row."""
    rows = [
        ["Tmpl", " ", "数电票（普通发票）", "2026-07-01", None, "DIGITAL001", "Seller A",
         "Product1", None, "件", 10, "1.00", 10.00, 1.0, 0.10, 10.10, "正常", 30.00, 0.30, 30.30],
        [None, None, None, None, None, None, None,
         "Product2", None, "件", 5, "1.00", 5.00, 1.0, 0.05, 5.05, None, None, None, None],
        [None, None, None, None, None, None, None,
         "Product3", None, "件", 15, "1.00", 15.00, 1.0, 0.15, 15.15, None, None, None, None],
    ]
    path = invoice_workbook_factory(rows)

    result = import_invoices(db_session, path, InvoiceDirection.PURCHASE)

    assert result.invoices_created == 1
    assert result.invoice_items_created == 3
    assert db_session.query(InvoiceModel).count() == 1
    assert db_session.query(InvoiceItemModel).count() == 3

    invoice = db_session.query(InvoiceModel).one()
    assert invoice.seller == "Seller A"
    assert invoice.digital_invoice_no == "DIGITAL001"
    assert invoice.gross_amount == Decimal("30.30")

    items = db_session.query(InvoiceItemModel).order_by(InvoiceItemModel.line_no).all()
    assert [i.product_name for i in items] == ["Product1", "Product2", "Product3"]
    assert sum((i.net_amount for i in items), Decimal("0")) == Decimal("30.00")

    # The continuation rows' raw Evidence must not have been forward-filled.
    fragments_by_row = {f.row_number: f for f in db_session.query(EvidenceFragmentModel).all()}
    continuation_row_2 = fragments_by_row[5]  # header row 3, header data row 4, continuation rows 5 and 6
    continuation_row_3 = fragments_by_row[6]
    for frag in (continuation_row_2, continuation_row_3):
        assert frag.raw_data["销方名称"] in (None, "")
        assert frag.raw_data["数电发票号码"] in (None, "")
        assert frag.raw_data["开票日期"] in (None, "")
        assert frag.raw_data["商品名称（明细）"] is not None  # item field IS present
