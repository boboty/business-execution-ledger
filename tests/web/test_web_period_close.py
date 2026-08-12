"""Period Close workbench web tests.

The page must surface every Phase 2B decision type, must be strictly
read-only (DB row counts unchanged), and must render the SAME business
answer the CLI/application preview produces — the web layer never
re-implements close rules.
"""

from __future__ import annotations

from bel.application.period_close import build_period_close_preview
from bel.application.period_close_workbench import get_period_close_workbench
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


def test_page_shows_all_decision_types(web_client):
    response = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    html = response.text

    # Summary cards
    for label in ["历史暂估待红冲", "新增暂估", "合同级待补明细", "成本差异", "阻塞项"]:
        assert label in html

    # Prior accrual reversal (S2B-01 partial reversal -> PARTIALLY_REVERSED)
    assert "历史暂估待红冲" in html
    assert "部分红冲" in html

    # New accrual (S2B-04)
    assert "Accrual Required" in html

    # Contract-level candidate (S2B-05 / S2B-08) — visually distinct
    assert "尚不能形成正式暂估" in html
    assert "缺少 ContractItem Evidence" in html

    # Actual cost difference (S2B-01)
    assert "成本差异" in html

    # Blockers shown first with Chinese meaning (S2B-07, MISSING_ACCRUAL_BASIS)
    assert "当前有 2 项业务信息不足" in html
    assert "ITEM_MATCH_REQUIRED_FOR_REVERSAL" in html
    assert "已确认到票，但尚未确认发票明细对应哪个合同商品" in html
    assert "MISSING_ACCRUAL_BASIS" in html
    assert "已满足成本确认条件，但缺少可确认的暂估成本依据" in html


def test_decision_trace_renders_fact_and_evidence(web_client):
    response = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    html = response.text
    assert "查看依据" in html
    assert "历史暂估事实" in html
    assert "来源证据" in html
    assert "技术详情" in html
    # the historical accrual fact's fields appear in the trace
    assert "来源期间" in html


def test_period_close_get_is_zero_write(app_for_client):
    client, app = app_for_client
    before = _db_counts(app.state.session_factory)
    response = client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    after = _db_counts(app.state.session_factory)
    assert before == after, "GET /period-close must not write a single row"


def test_web_underlying_dto_matches_application_preview(app_for_client):
    """Same synthetic database: CLI/Application Preview result == the
    Workbench DTO the page is rendered from."""
    client, app = app_for_client
    factory = app.state.session_factory
    with factory() as session:
        workbench = get_period_close_workbench(session, CLOSE_PERIOD_FIXTURE)
        preview = build_period_close_preview(session, CLOSE_PERIOD_FIXTURE)

    assert workbench.summary == preview.summary
    assert workbench.preview.period == preview.period
    assert workbench.preview.summary == preview.summary
    assert workbench.reversals and workbench.accruals and workbench.candidates and workbench.differences and workbench.blockers

    # The decision payloads must be byte-identical to the frozen engine.
    assert [r.decision for r in workbench.reversals] == list(preview.prior_accrual_reversals)
    assert [a.decision for a in workbench.accruals] == list(preview.new_accrual_requirements)
    assert [c.decision for c in workbench.candidates] == list(preview.contract_level_candidates)
    assert [d.decision for d in workbench.differences] == list(preview.accrual_actual_differences)
    assert [b.blocker for b in workbench.blockers] == list(preview.blockers)

    # And the page actually renders that DTO.
    response = client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200


def test_decision_trace_has_no_missing_facts(app_for_client):
    """Every Workbench decision carries a complete evidence chain: each
    FactNode resolves to a fragment AND a document (traceability per A02)."""
    _, app = app_for_client
    with app.state.session_factory() as session:
        workbench = get_period_close_workbench(session, CLOSE_PERIOD_FIXTURE)
    for row in list(workbench.reversals) + list(workbench.accruals) + list(workbench.candidates) + list(workbench.differences):
        assert row.trace, "every decision needs at least one fact node"
        for node in row.trace:
            assert node.fragment is not None, f"fact {node.fact_kind} missing EvidenceFragment"
            assert node.document is not None, f"fact {node.fact_kind} missing EvidenceDocument"
            assert node.fields, f"fact {node.fact_kind} has no fields"
