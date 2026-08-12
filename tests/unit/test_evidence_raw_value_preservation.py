from bel.adapters.excel.contract_ledger import parse_contract_ledger

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "备注", "退税到账日期", "是否付款"]


def test_raw_data_preserves_malformed_and_mixed_type_values_verbatim(ledger_workbook_factory):
    """Ledgers of this shape can have 备注 holding both text ('已执行完')
    and a stray exchange-rate-shaped number, and 退税到账日期 holding both
    real dates and a malformed string like '205/8/14'. Phase 1 must store
    these exactly as found — no coercion, no "helpful" correction. See
    spec section 8."""
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 100, 4.2953, "205/8/14", "是"],
            [2, "C002", "SellerB", "BuyerX", 200, "已执行完", None, "是"],
        ],
    )
    wb = parse_contract_ledger(path)
    row1, row2 = wb.business_rows

    assert row1.raw_data["备注"] == 4.2953
    assert row1.raw_data["退税到账日期"] == "205/8/14"
    assert row2.raw_data["备注"] == "已执行完"
    # Canonical Contract fields are unaffected by the dirty columns —
    # they simply don't participate in gross_amount/contract_no/etc.
    assert row1.gross_amount.__class__.__name__ == "Decimal"
    assert row1.contract_no == "C001"


def test_raw_data_is_not_promoted_into_canonical_fields(ledger_workbook_factory):
    path = ledger_workbook_factory(HEADERS, [[1, "C001", "SellerA", "BuyerX", 100, "已执行完", None, "是"]])
    wb = parse_contract_ledger(path)
    row = wb.business_rows[0]
    # 备注/退税到账日期/是否付款 never leak into the canonical fields the
    # adapter exposes — they only exist inside raw_data.
    assert not hasattr(row, "remark")
    assert not hasattr(row, "is_paid")
    assert row.raw_data["是否付款"] == "是"
