"""Contract 360° web tests.

The page must show the contract, its items, the confirmed invoices
(with the item-allocation state), the explicitly-allocated payments,
the accrual balances (derived via the shared domain function), the
current-period close judgment (the SAME engine, filtered — never a
second rule set), and the aggregated evidence. All GET, strictly
read-only.
"""

from __future__ import annotations

from tests.web.conftest import CLOSE_PERIOD_FIXTURE


def _db_counts(session_factory) -> dict[str, int]:
    from bel.infrastructure.persistence.models import (
        AccrualBasisFactModel,
        AccrualModel,
        AccrualReversalModel,
        BusinessEventModel,
        ContractItemModel,
        ContractModel,
        CostRecognitionFactModel,
        EvidenceDocumentModel,
        EvidenceFragmentModel,
        HistoricalAccrualFactModel,
        ImportRunModel,
        InvoiceAllocationModel,
        InvoiceItemAllocationModel,
        InvoiceItemModel,
        InvoiceModel,
        MatchCandidateModel,
        MatchCaseModel,
        PaymentAllocationModel,
        PaymentModel,
    )

    models = [
        AccrualBasisFactModel,
        AccrualModel,
        AccrualReversalModel,
        BusinessEventModel,
        ContractItemModel,
        ContractModel,
        CostRecognitionFactModel,
        EvidenceDocumentModel,
        EvidenceFragmentModel,
        HistoricalAccrualFactModel,
        ImportRunModel,
        InvoiceAllocationModel,
        InvoiceItemAllocationModel,
        InvoiceItemModel,
        InvoiceModel,
        MatchCandidateModel,
        MatchCaseModel,
        PaymentAllocationModel,
        PaymentModel,
    ]
    with session_factory() as session:
        return {m.__tablename__: session.query(m).count() for m in models}


def test_contract_header(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "合同360°" in html
    assert "合同信息" in html
    assert "PO-CLOSE-001" in html
    assert "SupplierCloseAlpha" in html
    assert "1300.00" in html
    assert "CNY" in html


def test_contract_items_shown(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "合同商品" in html
    assert "ITEM-A" in html
    assert "Alpha Widget" in html
    assert "当前暂估状态" in html


def test_invoice_area_with_manual_allocation_state(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "发票" in html
    assert "DIGITAL-CLOSE-001" in html
    assert "进项" in html
    assert "匹配方式" in html
    assert "AUTO_CONFIRMED" in html
    # line 1 is already linked to a contract item via the fact pack
    assert "已关联" in html
    # invoice item table columns
    assert "行号" in html
    assert "未税金额" in html


def test_unallocated_invoice_item_offers_manual_allocation_form(web_client, contract_id_by_no):
    """PO-CLOSE-006 is contract-confirmed but has no item match — the page
    must offer an explicit, non-preselected allocation form."""
    contract_id = contract_id_by_no["PO-CLOSE-006"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "未关联" in html
    assert "关联合同明细" in html
    assert "请选择合同商品" in html  # never preselected
    assert "原发票数量" in html
    assert "DIGITAL-CLOSE-006" in html


def test_payment_area_shows_only_allocated_payments(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "付款" in html
    assert "付款/收款" in html
    assert "455.00" in html
    assert "SupplierCloseAlpha" in html
    assert "AUTO_CONFIRMED" in html


def test_accrual_balance_area(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "暂估余额" in html
    assert "2031-02" in html  # source period
    assert "100" in html  # original quantity
    assert "1200.00" in html  # original estimated cost
    assert "剩余数量" in html
    assert "未红冲" in html  # ACTIVE (no committed reversals)


def test_current_period_decisions_filtered_to_contract(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "当前期间业务判断" in html
    # S2B-01 partial reversal judgment for THIS contract
    assert "到票数量" in html
    assert "部分红冲" in html


def test_contract_with_blocker_shows_its_blocker(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-006"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "ITEM_MATCH_REQUIRED_FOR_REVERSAL" in html
    assert "已确认到票，但尚未确认发票明细对应哪个合同商品" in html


def test_evidence_aggregation(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "证据" in html
    assert "合同 Evidence" in html
    assert "合同商品 Evidence" in html
    assert "发票 Evidence" in html
    assert "付款 Evidence" in html
    assert "历史暂估事实 Evidence" in html
    assert "人工明细关联 Evidence" in html
    assert "元数据" in html
    assert "close_fact_pack_json" in html  # source type of fact-pack evidence


def test_contract_360_get_is_zero_write(app_for_client, contract_id_by_no):
    client, app = app_for_client
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    before = _db_counts(app.state.session_factory)
    response = client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    after = _db_counts(app.state.session_factory)
    assert before == after, "GET /contracts/{id} must not write a single row"
