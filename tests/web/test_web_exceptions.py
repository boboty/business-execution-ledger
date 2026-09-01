"""Phase 2D.4-F1 — /exceptions — the read-only Exception & Task Center.

Web tests over the frozen contract (docs/PHASE2D4-DECISIONS.md §13-§16):
filters (source_type / code / status / procurement scope / sales scope /
valid + invalid period), all three source labels rendered, unmappable work
visibly retained, no generic resolve control, no POST endpoint, zero writes
on GET, and the period-less explanation for computed blockers.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import (
    MatchCase,
    MatchCaseStatus,
    MatchCandidate,
    MatchMethod,
    SubjectType,
)
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    ExceptionRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
)

NOW = datetime.now(timezone.utc)


def _add_task(session, exception_type, detail, summary="test task", status=ExceptionStatus.OPEN):
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=exception_type,
            status=status,
            summary=summary,
            detail=detail,
            created_at=NOW,
        )
    )
    session.flush()


def _add_procurement_hcr(session, contract_id, invoice_id):
    case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice_id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(case)
    session.flush()
    MatchCandidateRepository(session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=contract_id, created_at=NOW)
    )
    session.flush()
    return case


def _seed_blocker_contract(session):
    """A contract + cost recognition fact with no accrual basis — produces a
    MISSING_ACCRUAL_BASIS blocker for 2031-03, the Phase 2B fixture period."""
    from datetime import date
    from decimal import Decimal

    from bel.domain.contract import Contract
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.domain.accrual import CostRecognitionFact
    from bel.infrastructure.persistence.repositories import (
        CostRecognitionFactRepository,
        EvidenceRepository,
    )

    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"section": "test", "index": 0},
        raw_data={},
        created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    contract = Contract(
        id=uuid.uuid4(),
        contract_no="PO-WEB-BLOCKER",
        contract_type=None,
        counterparty="Supplier Web",
        buyer="Our Own Entity",
        gross_amount=Decimal("5000.00"),
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    CostRecognitionFactRepository(session).add(
        CostRecognitionFact(
            id=uuid.uuid4(),
            contract_id=contract.id,
            recognition_date=date(2031, 2, 28),
            basis="MANUAL_CONFIRMED",
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    )
    session.flush()
    return contract


def _db_counts(session_factory) -> dict:
    from bel.infrastructure.persistence import models as m

    with session_factory() as session:
        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = session.query(obj).count()
        return counts


def _seed_base_center(web_app):
    """Build the synthetic Phase 2B DB and add one OPEN task, one
    procurement HCR MatchCase and one blocker-producing contract."""
    app = web_app()
    with app.state.session_factory() as session:
        contract = next(c for c in ContractRepository(session).list_all())
        invoice = next(i for i in InvoiceRepository(session).list_all() if i.direction == InvoiceDirection.PURCHASE)
        _add_task(
            session,
            ExceptionType.BUSINESS_KEY_CONFLICT,
            {"contract_ids": [str(contract.id)]},
            summary="WEB-UNIQUE-TASK-SUMMARY",
        )
        _add_task(
            session,
            ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE,
            {"source_fragment_id": str(uuid.uuid4())},
            summary="WEB-UNMAPPABLE-TASK-SUMMARY",
        )
        _add_procurement_hcr(session, contract.id, invoice.id)
        _seed_blocker_contract(session)
        session.commit()
    return app


def test_page_renders_all_three_source_labels_and_selected_period(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions?period=2031-03")
    assert response.status_code == 200
    assert "异常与任务中心" in response.text
    for label in ("系统任务", "待确认匹配", "月结阻断项"):
        assert label in response.text, f"missing source label: {label}"
    assert "当前期间" in response.text and "2031-03" in response.text
    # The computed blocker is visibly non-persisted / current-period.
    assert "本期计算 · 未持久化" in response.text


def test_page_get_causes_zero_writes(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    before = _db_counts(app.state.session_factory)
    response = client.get("/exceptions?period=2031-03")
    assert response.status_code == 200
    assert _db_counts(app.state.session_factory) == before, "GET /exceptions must not write a single row"


def test_period_less_page_explains_blockers_require_period(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions")
    assert response.status_code == 200
    assert "月结阻断项需选择期间后计算" in response.text
    # No misleading "0 blockers for all time" phrasing; no period shown.
    assert "本期计算 · 未持久化" not in response.text


def test_unmappable_item_visibly_retained(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions")
    assert "WEB-UNMAPPABLE-TASK-SUMMARY" in response.text
    assert "尚未形成可定位的业务对象" in response.text


def test_no_generic_resolve_control(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions")
    for forbidden in ("标记完成", "关闭异常", "一键解决", "通用解决", "resolve"):
        assert forbidden not in response.text, f"generic resolve control must not appear: {forbidden}"
    # The resolution column is guidance-only copy.
    assert "查看并人工处理" in response.text
    assert "去确认匹配" in response.text


def test_no_post_endpoint(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.post("/exceptions")
    assert response.status_code == 405


def test_source_type_filter(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions?source_type=MATCH_CASE")
    assert "WEB-UNIQUE-TASK-SUMMARY" not in response.text
    assert "INVOICE" in response.text  # the procurement match summary text


def test_code_filter(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions?code=BusinessKeyConflict")
    assert "WEB-UNIQUE-TASK-SUMMARY" in response.text
    assert "WEB-UNMAPPABLE-TASK-SUMMARY" not in response.text


def test_task_status_filter(web_app):
    app = _seed_base_center(web_app)
    with app.state.session_factory() as session:
        _add_task(
            session,
            ExceptionType.BACKFILL_CONFLICT,
            {"fact_type": "x", "identity_key": "k"},
            summary="WEB-RESOLVED-TASK",
            status=ExceptionStatus.RESOLVED,
        )
        session.commit()
    client = TestClient(app)
    response = client.get("/exceptions?status=RESOLVED")
    assert "WEB-RESOLVED-TASK" in response.text
    assert "WEB-UNIQUE-TASK-SUMMARY" not in response.text


def test_procurement_scope_filter(web_app):
    app = web_app()
    with app.state.session_factory() as session:
        contract = next(c for c in ContractRepository(session).list_all())
        _add_task(session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": [str(contract.id)]}, summary="WEB-P-FILTER")
        other = next(c for c in ContractRepository(session).list_all() if c.id != contract.id)
        _add_task(session, ExceptionType.ALLOCATION_CAPACITY_EXCEEDED, {"contract_id": str(other.id)}, summary="WEB-P-OTHER")
        session.commit()
    client = TestClient(app)
    response = client.get(f"/exceptions?procurement_contract_id={contract.id}")
    assert "WEB-P-FILTER" in response.text
    assert "WEB-P-OTHER" not in response.text


def test_sales_scope_filter(web_app):
    from bel.application.sales_contract_facts import create_sales_contract_fact
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    app = web_app()
    with app.state.session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT, sheet_name=None, row_number=None, locator_json={"index": 0}, raw_data={}, created_at=NOW)
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        s1 = create_sales_contract_fact(session, our_entity="Our Own Entity", sales_contract_no="WEB-SC-1", fields={"customer": "Customer A"}, source_fragment_id=frag.id, created_at=NOW).sales_contract
        s2 = create_sales_contract_fact(session, our_entity="Our Own Entity", sales_contract_no="WEB-SC-2", fields={"customer": "Customer B"}, source_fragment_id=frag.id, created_at=NOW).sales_contract
        session.flush()
        _add_task(session, ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED, {"sales_contract_id": str(s1.id)}, summary="WEB-S-FILTER")
        _add_task(session, ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED, {"sales_contract_id": str(s2.id)}, summary="WEB-S-OTHER")
        session.commit()
    client = TestClient(app)
    response = client.get(f"/exceptions?sales_contract_id={s1.id}")
    assert "WEB-S-FILTER" in response.text
    assert "WEB-S-OTHER" not in response.text


def test_valid_period_returns_200_and_invalid_returns_400(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    assert client.get("/exceptions?period=2031-03").status_code == 200
    assert client.get("/exceptions?period=2031-13").status_code == 400
    assert client.get("/exceptions?period=nope").status_code == 400


def test_malformed_scope_id_is_400(web_app):
    app = _seed_base_center(web_app)
    client = TestClient(app)
    response = client.get("/exceptions?procurement_contract_id=not-a-uuid")
    assert response.status_code == 400
