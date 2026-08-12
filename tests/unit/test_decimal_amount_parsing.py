from decimal import Decimal

from bel.adapters.excel.contract_ledger import parse_contract_ledger

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_amounts_parsed_as_decimal_not_float(ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 52233.04],
            [2, "C002", "SellerB", "BuyerX", 8505],  # int cell, no decimals in source
        ],
    )
    wb = parse_contract_ledger(path)
    amounts = [r.gross_amount for r in wb.business_rows]
    assert all(isinstance(a, Decimal) for a in amounts)
    assert amounts[0] == Decimal("52233.04")
    assert amounts[1] == Decimal("8505")


def test_decimal_sum_has_no_float_drift(ledger_workbook_factory):
    # Values chosen because naive float summation of many such numbers
    # accumulates visible binary-rounding error; Decimal must not.
    values = [Decimal("0.10")] * 10 + [Decimal("33894.40"), Decimal("23763.20")]
    rows = [[i + 1, f"C{i:03d}", "Seller", "Buyer", str(v)] for i, v in enumerate(values)]
    path = ledger_workbook_factory(HEADERS, rows)
    wb = parse_contract_ledger(path)
    total = sum((r.gross_amount for r in wb.business_rows), Decimal("0"))
    assert total == Decimal("57658.60")
