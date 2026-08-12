from bel.adapters.excel.contract_ledger import parse_contract_ledger

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_trailing_rows_without_contract_no_are_not_business_rows(ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 100],
            [2, "C002", "SellerB", "BuyerX", 200],
            [3, None, None, None, None],  # trailing sequence continuation, no real data
            [4, None, None, None, None],
        ],
    )
    wb = parse_contract_ledger(path)
    assert len(wb.rows) == 4
    assert len(wb.business_rows) == 2
    assert len(wb.blank_trailing_rows) == 2
    assert [r.contract_no for r in wb.business_rows] == ["C001", "C002"]


def test_blank_row_still_becomes_an_evidence_fragment_candidate(ledger_workbook_factory):
    # Blank rows are excluded from Contract promotion but their raw row
    # data is still captured — nothing about the row is discarded before
    # the promotion decision. See docs/PHASE1-DECISIONS.md.
    path = ledger_workbook_factory(HEADERS, [[1, None, None, None, None]])
    wb = parse_contract_ledger(path)
    assert len(wb.rows) == 1
    row = wb.rows[0]
    assert row.is_business_row is False
    assert row.raw_data["序号"] == 1
