from bel.adapters.excel.contract_ledger import parse_contract_ledger

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_contract_no_whitespace_is_stripped_not_reinterpreted(ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "  C001  ", "SellerA", "BuyerX", 100],
            [2, "C002", "SellerB", "BuyerX", 200],
        ],
    )
    wb = parse_contract_ledger(path)
    contract_nos = [r.contract_no for r in wb.business_rows]
    assert contract_nos == ["C001", "C002"]
    # Not reinterpreted: no case-folding, no separator normalization.
    assert wb.business_rows[1].contract_no == "C002"


def test_contract_no_is_never_derived_from_other_fields(ledger_workbook_factory):
    # A row with no 合同编码 must not be promoted, even if every other
    # field looks like a perfectly good business row. No implicit
    # guessing — see section 6 of the Phase 1 spec.
    path = ledger_workbook_factory(
        HEADERS,
        [[1, None, "SellerA", "BuyerX", 100]],
    )
    wb = parse_contract_ledger(path)
    assert wb.business_rows == []
