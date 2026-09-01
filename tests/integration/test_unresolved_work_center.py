"""Phase 2D.4-F1 — Exception & Task Center, application projection.

Focused tests over the frozen contract in
docs/PHASE2D4-DECISIONS.md / docs/PHASE2D4-ACCEPTANCE.md:

  - TASK_EXCEPTION: the 14 produced types project without crash; OPEN is
    the default view; unmappable items (SalesContractIdentityIncomplete,
    backfill) stay globally visible; a missing referenced object never
    drops the task; scope comes only from structured detail + repository
    lookup (never summary text); the declared-but-unproduced enum is not
    synthesized.
  - MATCH_CASE: HUMAN_CONFIRMATION_REQUIRED appears in both legs with
    candidate scopes preserved on ONE row; UNMATCHED/REJECTED never
    appear; CONFIRM_MATCH route; the projection writes nothing.
  - COMPUTED_BLOCKER: zero items without a period; recomputed blockers
    with a deterministic, scope-stable, multi-accrual-canonical source_id;
    never persisted; created_at None; REVIEW_ONLY.
  - BOUNDARIES: advisories and MISSING_CONTRACT_GROSS_AMOUNT are not
    Center sources; R009-R015 are not implemented.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import InvoicePreparationContext
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.supplier_invoice_request import (
    SupplierRequestBlockerCode,
    SupplierScopeContext,
    evaluate_supplier_invoice_request_from_context,
)
from bel.application.unresolved_work_center import (
    ResolutionRoute,
    ScopeType,
    SourceType,
    UnresolvedWorkFilters,
    get_unresolved_work_center,
    validate_period,
)
from bel.domain.accrual import Accrual, AccrualStatus, CostRecognitionFact, InvoiceItemAllocation
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    MatchCase,
    MatchCaseStatus,
    MatchCandidate,
    MatchMethod,
    SalesMatchCandidate,
    SubjectType,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    ExceptionRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
    PaymentRepository,
    SalesMatchCandidateRepository,
)

NOW = datetime.now(timezone.utc)

# The complete currently-PRODUCED TaskException type set (docs §1A); the
# declared-but-unproduced PROCUREMENT_SALES_LINK_CONFLICT is deliberately
# absent.
PRODUCED_EXCEPTION_TYPES = [
    ExceptionType.BUSINESS_KEY_CONFLICT,
    ExceptionType.ALLOCATION_CAPACITY_EXCEEDED,
    ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED,
    ExceptionType.SHIPMENT_FACT_SUPERSEDED,
    ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE,
    ExceptionType.SHIPMENT_IDENTITY_CONFLICT,
    ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE,
    ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED,
    ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED,
    ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES,
    ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT,
    ExceptionType.BACKFILL_IDENTITY_INCOMPLETE,
    ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS,
    ExceptionType.BACKFILL_CONFLICT,
]


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _make_fragment(session):
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
    return frag


def _make_contract(session, fragment_id, contract_no=None, gross_amount=Decimal("5000.00")):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty="Supplier A",
        buyer="Our Own Entity",
        gross_amount=gross_amount,
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(session, fragment_id, sales_contract_no=None, customer=None):
    fields = {}
    if customer:
        fields["customer"] = customer
    result = create_sales_contract_fact(
        session,
        our_entity="Our Own Entity",
        sales_contract_no=sales_contract_no or f"SC-{uuid.uuid4().hex[:8]}",
        fields=fields,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    session.flush()
    return result.sales_contract


def _make_contract_item(session, contract, source_item_key="ITEM-A", fragment_id=None):
    if fragment_id is None:
        fragment_id = _make_fragment(session).id
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key=source_item_key,
        sku=None,
        product_name="Widget",
        specification=None,
        quantity=Decimal("100"),
        unit="件",
        unit_price=None,
        gross_amount=None,
        tax_rate=None,
        net_amount=None,
        current_source_fragment_id=fragment_id,
        created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _make_invoice(session, fragment_id, direction, external_key=None, issue_date="2026-01-05"):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=direction,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=external_key or f"INV-{uuid.uuid4().hex[:8]}",
        issue_date=datetime.strptime(issue_date, "%Y-%m-%d").date(),
        seller="Seller Co",
        buyer="Buyer Co",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_invoice_item(session, invoice, fragment_id, quantity="100", net_amount="100.00"):
    item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name="Widget",
        specification=None,
        unit="件",
        quantity=Decimal(quantity),
        unit_price=None,
        net_amount=Decimal(net_amount),
        tax_rate=None,
        tax_amount=Decimal("0"),
        gross_amount=Decimal(net_amount),
        source_fragment_id=fragment_id,
    )
    InvoiceItemRepository(session).add(item)
    session.flush()
    return item


def _make_payment(session, fragment_id, direction):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2026, 1, 6),
        direction=direction,
        amount=Decimal("50.00"),
        counterparty="Counterparty Co",
        business_type=None,
        bank_reference=f"BR-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _make_task(session, exception_type, detail, status=ExceptionStatus.OPEN, summary=None):
    task = TaskException(
        id=uuid.uuid4(),
        exception_type=exception_type,
        status=status,
        summary=summary or f"task {exception_type}",
        detail=detail,
        created_at=NOW,
    )
    ExceptionRepository(session).add(task)
    session.flush()
    return task


def _make_procurement_hcr(session, contract, subject_id=None, subject_type=SubjectType.INVOICE):
    case = MatchCase(
        id=uuid.uuid4(),
        subject_type=subject_type,
        subject_id=subject_id or uuid.uuid4(),
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(case)
    session.flush()
    MatchCandidateRepository(session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=contract.id, created_at=NOW)
    )
    session.flush()
    return case


def _make_sales_hcr(session, sales_contract, invoice_id=None, payment_id=None):
    if invoice_id is not None:
        subject_type, subject_id = SubjectType.INVOICE, invoice_id
    else:
        subject_type, subject_id = SubjectType.PAYMENT, payment_id
    case = MatchCase(
        id=uuid.uuid4(),
        subject_type=subject_type,
        subject_id=subject_id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(case)
    session.flush()
    SalesMatchCandidateRepository(session).add(
        SalesMatchCandidate(id=uuid.uuid4(), match_case_id=case.id, sales_contract_id=sales_contract.id, created_at=NOW)
    )
    session.flush()
    return case


def _db_counts(session) -> dict:
    from bel.infrastructure.persistence import models as m

    counts = {}
    for name in dir(m):
        obj = getattr(m, name)
        if isinstance(obj, type) and hasattr(obj, "__tablename__"):
            counts[obj.__tablename__] = session.query(obj).count()
    return counts


# ---------------------------------------------------------------------------
# TASK_EXCEPTION
# ---------------------------------------------------------------------------


def test_all_14_produced_types_project_without_crash(db_session):
    for i, exc_type in enumerate(PRODUCED_EXCEPTION_TYPES):
        detail = {
            ExceptionType.BUSINESS_KEY_CONFLICT: {"contract_ids": [str(uuid.uuid4())]},
            ExceptionType.ALLOCATION_CAPACITY_EXCEEDED: {"contract_id": str(uuid.uuid4())},
            ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED: {"contract_item_id": str(uuid.uuid4())},
            ExceptionType.SHIPMENT_FACT_SUPERSEDED: {"shipment_id": str(uuid.uuid4())},
            ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE: {"contract_id": str(uuid.uuid4())},
            ExceptionType.SHIPMENT_IDENTITY_CONFLICT: {"shipment_id": str(uuid.uuid4())},
            ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE: {"source_fragment_id": str(uuid.uuid4())},
            ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED: {"sales_contract_id": str(uuid.uuid4())},
            ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED: {
                "procurement_contract_id": str(uuid.uuid4()),
                "sales_contract_id": str(uuid.uuid4()),
            },
            ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES: {
                "procurement_contract_id": str(uuid.uuid4()),
                "sales_contract_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
            },
            ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT: {"superseded_link_id": str(uuid.uuid4())},
            ExceptionType.BACKFILL_IDENTITY_INCOMPLETE: {"fact_type": "Invoice", "identity_key": "k"},
            ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS: {"fact_type": "Contract", "identity_key": "k", "matches": []},
            ExceptionType.BACKFILL_CONFLICT: {"fact_type": "Payment", "identity_key": "k", "reason": "r"},
        }[exc_type]
        _make_task(db_session, exc_type, detail)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    task_items = [i for i in center.items if i.source_type == SourceType.TASK_EXCEPTION]
    assert len(task_items) == 14
    assert {i.code for i in task_items} == set(PRODUCED_EXCEPTION_TYPES)


def test_open_task_appears_and_resolved_does_not_by_default(db_session):
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []}, status=ExceptionStatus.OPEN)
    _make_task(db_session, ExceptionType.BACKFILL_CONFLICT, {"fact_type": "x", "identity_key": "k"}, status=ExceptionStatus.RESOLVED)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    task_items = [i for i in center.items if i.source_type == SourceType.TASK_EXCEPTION]
    assert len(task_items) == 1
    assert task_items[0].code == ExceptionType.BUSINESS_KEY_CONFLICT


def test_unmappable_sales_contract_identity_incomplete_appears(db_session):
    _make_task(db_session, ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE, {"source_fragment_id": str(uuid.uuid4())})
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE]
    assert len(items) == 1
    assert items[0].scopes == ()
    assert items[0].procurement_contract_id is None
    assert items[0].sales_contract_id is None


def test_backfill_task_without_scope_appears(db_session):
    _make_task(
        db_session,
        ExceptionType.BACKFILL_IDENTITY_INCOMPLETE,
        {"fact_type": "Invoice", "identity_key": "something/something"},
    )
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.BACKFILL_IDENTITY_INCOMPLETE]
    assert len(items) == 1
    assert items[0].scopes == ()
    assert items[0].procurement_contract_id is None
    assert items[0].sales_contract_id is None


def test_missing_referenced_scope_object_does_not_drop_task(db_session):
    missing_shipment_id = uuid.uuid4()
    _make_task(db_session, ExceptionType.SHIPMENT_FACT_SUPERSEDED, {"shipment_id": str(missing_shipment_id)})
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.SHIPMENT_FACT_SUPERSEDED]
    assert len(items) == 1  # never dropped
    assert items[0].procurement_contract_id is None  # unresolved trace stays None
    # The structured shipment id is preserved as a trace scope.
    assert (ScopeType.SHIPMENT, missing_shipment_id) in {(s.scope_type, s.scope_id) for s in items[0].scopes}


def test_structured_scope_mapping_works_for_procurement(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-CENTER-SCOPE")
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": [str(contract.id)]})
    _make_task(db_session, ExceptionType.ALLOCATION_CAPACITY_EXCEEDED, {"contract_id": str(contract.id)})
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    for item in [i for i in center.items if i.source_type == SourceType.TASK_EXCEPTION]:
        assert item.procurement_contract_id == contract.id
        assert (ScopeType.PROCUREMENT_CONTRACT, contract.id) in {(s.scope_type, s.scope_id) for s in item.scopes}


def test_shipment_superseded_resolves_contract_via_lookup(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-SHIP-LOOKUP")
    shipment_result = None
    from bel.application.shipment_facts import create_shipment_fact

    shipment_result = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="SR-1",
        execution_date=date(2026, 2, 1),
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.flush()
    _make_task(db_session, ExceptionType.SHIPMENT_FACT_SUPERSEDED, {"shipment_id": str(shipment_result.shipment.id)})
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.SHIPMENT_FACT_SUPERSEDED]
    assert len(items) == 1
    assert items[0].procurement_contract_id == contract.id


def test_summary_text_not_used_for_scope_inference(db_session):
    _make_task(
        db_session,
        ExceptionType.BUSINESS_KEY_CONFLICT,
        {"contract_no": "PO-NOT-A-STRUCTURED-ID"},
        summary="合同 PO-NOT-A-STRUCTURED-ID 与 C-FAKE 冲突，请处理",
    )
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.BUSINESS_KEY_CONFLICT]
    assert len(items) == 1
    # summary text (even containing contract numbers) must never produce a scope.
    assert items[0].scopes == ()
    assert items[0].procurement_contract_id is None
    assert items[0].sales_contract_id is None


def test_declared_but_unproduced_enum_is_not_synthesized(db_session):
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []})
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    codes = {i.code for i in center.items}
    assert ExceptionType.PROCUREMENT_SALES_LINK_CONFLICT not in codes


def test_sales_contract_customer_unresolved_route(db_session):
    # create_sales_contract_fact(customer=None) is itself the producer that
    # raises the SalesContractCustomerUnresolved task (docs §1A).
    frag = _make_fragment(db_session)
    sales_contract = _make_sales_contract(db_session, frag.id, customer=None)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED]
    assert len(items) == 1
    assert items[0].sales_contract_id == sales_contract.id
    assert items[0].resolution_route == ResolutionRoute.CONFIRM_RELATIONSHIP


def test_shipment_identity_incomplete_route(db_session):
    _make_task(
        db_session,
        ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE,
        {"contract_id": str(uuid.uuid4()), "source_fragment_id": str(uuid.uuid4())},
    )
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE]
    assert len(items) == 1
    assert items[0].resolution_route == ResolutionRoute.SUPPLY_FACT


def test_link_correction_conflict_resolves_via_link_lookup(db_session):
    frag = _make_fragment(db_session)
    procurement = _make_contract(db_session, frag.id, "PO-LINK-P")
    sales = _make_sales_contract(db_session, frag.id)
    from bel.application.procurement_sales_link import add_procurement_sales_link

    result = add_procurement_sales_link(
        db_session,
        procurement_contract_id=procurement.id,
        sales_contract_id=sales.id,
        source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    db_session.flush()
    _make_task(
        db_session,
        ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT,
        {"superseded_link_id": str(result.link.id), "conflicting_source_fragment_id": str(uuid.uuid4())},
    )
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.code == ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT]
    assert len(items) == 1
    assert items[0].procurement_contract_id == procurement.id
    assert items[0].sales_contract_id == sales.id


# ---------------------------------------------------------------------------
# MATCH_CASE
# ---------------------------------------------------------------------------


def test_procurement_hcr_appears(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-MC-1")
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    case = _make_procurement_hcr(db_session, contract, subject_id=invoice.id)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    match_items = [i for i in center.items if i.source_type == SourceType.MATCH_CASE]
    assert len(match_items) == 1
    item = match_items[0]
    assert item.source_id == case.id
    assert item.match_case_id == case.id
    assert item.invoice_id == invoice.id
    assert item.code == MatchMethod.M001
    assert item.resolution_route == ResolutionRoute.CONFIRM_MATCH
    assert (ScopeType.PROCUREMENT_CONTRACT, contract.id) in {(s.scope_type, s.scope_id) for s in item.scopes}


def test_sales_invoice_hcr_appears(db_session):
    frag = _make_fragment(db_session)
    sales_contract = _make_sales_contract(db_session, frag.id)
    sales_invoice = _make_invoice(db_session, frag.id, InvoiceDirection.SALES, "SINV-1")
    case = _make_sales_hcr(db_session, sales_contract, invoice_id=sales_invoice.id)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.source_type == SourceType.MATCH_CASE]
    assert len(items) == 1
    assert items[0].source_id == case.id
    assert items[0].invoice_id == sales_invoice.id
    assert items[0].sales_contract_id == sales_contract.id
    assert items[0].resolution_route == ResolutionRoute.CONFIRM_MATCH


def test_sales_payment_hcr_appears(db_session):
    frag = _make_fragment(db_session)
    sales_contract = _make_sales_contract(db_session, frag.id)
    payment = _make_payment(db_session, frag.id, PaymentDirection.IN)
    case = _make_sales_hcr(db_session, sales_contract, payment_id=payment.id)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    items = [i for i in center.items if i.source_type == SourceType.MATCH_CASE]
    assert len(items) == 1
    assert items[0].payment_id == payment.id
    assert items[0].sales_contract_id == sales_contract.id


def test_candidate_scopes_preserved_as_one_item(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "PO-MC-A")
    c2 = _make_contract(db_session, frag.id, "PO-MC-B")
    case = _make_procurement_hcr(db_session, c1)
    MatchCandidateRepository(db_session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=c2.id, created_at=NOW)
    )
    db_session.flush()
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    match_items = [i for i in center.items if i.source_type == SourceType.MATCH_CASE]
    assert len(match_items) == 1  # one item, not one per candidate
    scope_ids = {(s.scope_type, s.scope_id) for s in match_items[0].scopes}
    assert (ScopeType.PROCUREMENT_CONTRACT, c1.id) in scope_ids
    assert (ScopeType.PROCUREMENT_CONTRACT, c2.id) in scope_ids


def test_unmatched_does_not_appear(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-MC-UNM")
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.UNMATCHED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(db_session).add(case)
    db_session.flush()
    MatchCandidateRepository(db_session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=contract.id, created_at=NOW)
    )
    db_session.flush()
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    assert all(i.source_id != case.id for i in center.items)


def test_rejected_does_not_appear(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-MC-REJ")
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.REJECTED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(case)
    db_session.flush()
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    assert all(i.source_id != case.id for i in center.items)


def test_match_case_projection_is_zero_write(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-MC-ZW")
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    _make_procurement_hcr(db_session, contract, subject_id=invoice.id)
    db_session.commit()

    before = _db_counts(db_session)
    get_unresolved_work_center(db_session)
    db_session.commit()
    assert _db_counts(db_session) == before


# ---------------------------------------------------------------------------
# COMPUTED_BLOCKER
# ---------------------------------------------------------------------------


def _seed_missing_basis_blocker(session, contract_no="PO-BLOCKER-1"):
    """One contract with a cost recognition fact but no accrual basis —
    produces a MISSING_ACCRUAL_BASIS CloseBlocker for any later period."""
    frag = _make_fragment(session)
    contract = _make_contract(session, frag.id, contract_no)
    CostRecognitionFactRepository(session).add(
        CostRecognitionFact(
            id=uuid.uuid4(),
            contract_id=contract.id,
            recognition_date=date(2026, 2, 28),
            basis="MANUAL_CONFIRMED",
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    )
    session.flush()
    return contract


def test_no_period_no_computed_blockers(db_session):
    _seed_missing_basis_blocker(db_session)
    db_session.commit()

    center = get_unresolved_work_center(db_session)
    assert center.counts[SourceType.COMPUTED_BLOCKER] == 0
    assert all(i.source_type != SourceType.COMPUTED_BLOCKER for i in center.items)


def test_period_adds_current_blockers(db_session):
    _seed_missing_basis_blocker(db_session)
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    blocker_items = [i for i in center.items if i.source_type == SourceType.COMPUTED_BLOCKER]
    assert len(blocker_items) >= 1
    assert all(i.status == "PRESENT" for i in blocker_items)
    assert all(i.created_at is None for i in blocker_items)
    assert all(i.resolution_route == ResolutionRoute.REVIEW_ONLY for i in blocker_items)
    assert all(i.provenance == "bel.application.period_close" for i in blocker_items)


def test_source_id_stable_across_recompute(db_session):
    contract = _seed_missing_basis_blocker(db_session)
    db_session.commit()

    f1 = UnresolvedWorkFilters(period="2026-03")
    f2 = UnresolvedWorkFilters(period="2026-03")
    center1 = get_unresolved_work_center(db_session, filters=f1)
    center2 = get_unresolved_work_center(db_session, filters=f2)
    sid1 = {i.source_id for i in center1.items if i.source_type == SourceType.COMPUTED_BLOCKER}
    sid2 = {i.source_id for i in center2.items if i.source_type == SourceType.COMPUTED_BLOCKER}
    assert sid1 == sid2 == {f"2026-03|MISSING_ACCRUAL_BASIS|{contract.id}"}


def test_source_id_changes_when_period_changes(db_session):
    contract = _seed_missing_basis_blocker(db_session)
    db_session.commit()

    center_p3 = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    center_p4 = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-04"))
    sid3 = {i.source_id for i in center_p3.items if i.source_type == SourceType.COMPUTED_BLOCKER}
    sid4 = {i.source_id for i in center_p4.items if i.source_type == SourceType.COMPUTED_BLOCKER}
    assert sid3 != sid4


def test_source_id_distinguishes_scope_changes(db_session):
    _seed_missing_basis_blocker(db_session, "PO-BLOCKER-A")
    _seed_missing_basis_blocker(db_session, "PO-BLOCKER-B")
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    sids = {i.source_id for i in center.items if i.source_type == SourceType.COMPUTED_BLOCKER}
    assert len(sids) == 2  # two distinct contracts -> two distinct keys


def test_multi_accrual_ids_canonical_order_stable(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-MULTI-ACCRUAL")
    item = _make_contract_item(db_session, contract)
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE, "PUR-MA-1", issue_date="2026-01-05")
    invoice_item = _make_invoice_item(db_session, invoice, frag.id)
    InvoiceItemAllocationRepository(db_session).add(
        InvoiceItemAllocation(
            id=uuid.uuid4(),
            invoice_item_id=invoice_item.id,
            contract_item_id=item.id,
            allocated_quantity=Decimal("100"),
            allocated_net_amount=Decimal("100.00"),
            confirmation_type="MANUAL_CONFIRMED",
            source_fragment_id=frag.id,
            created_at=NOW,
        )
    )
    db_session.flush()
    a1 = _make_accrual(db_session, item, "2026-01")
    a2 = _make_accrual(db_session, item, "2026-02")
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    blockers = [i for i in center.items if i.source_type == SourceType.COMPUTED_BLOCKER]
    multi = [i for i in blockers if i.code == "MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE"]
    assert len(multi) == 1
    expected = "2026-03|MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE|{0}|{1}|{2}|{3}".format(
        contract.id, item.id, *sorted(str(a.id) for a in (a1, a2))
    )
    assert multi[0].source_id == expected

    # Stable across a recompute on a fresh session.
    center2 = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    multi2 = [i for i in center2.items if i.source_type == SourceType.COMPUTED_BLOCKER and i.code == "MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE"]
    assert multi2[0].source_id == expected


def _make_accrual(session, contract_item, period):
    accrual = Accrual(
        id=uuid.uuid4(),
        period=period,
        contract_item_id=contract_item.id,
        quantity=Decimal("100"),
        estimated_cost=Decimal("1200.00"),
        basis="MANUAL_CONFIRMED",
        status=AccrualStatus.ACTIVE,
        created_from_fact_id=uuid.uuid4(),
        created_at=NOW,
    )
    AccrualRepository(session).add(accrual)
    session.flush()
    return accrual


def test_blocker_never_persisted_and_center_is_zero_write(db_session):
    _seed_missing_basis_blocker(db_session)
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []})
    db_session.commit()

    before = _db_counts(db_session)
    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="2026-03"))
    assert center.counts[SourceType.COMPUTED_BLOCKER] >= 1
    db_session.commit()
    after = _db_counts(db_session)
    assert before == after, "the Center (including computed blockers) must write nothing"


def test_invalid_period_rejected_explicitly(db_session):
    with pytest.raises(ValueError):
        validate_period("2026-13")
    with pytest.raises(ValueError):
        validate_period("2026-1")
    with pytest.raises(ValueError):
        get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(period="not-a-period"))


# ---------------------------------------------------------------------------
# BOUNDARIES
# ---------------------------------------------------------------------------


def test_advisory_codes_are_not_center_sources(db_session):
    from bel.application.sales_invoice_preparation import SalesInvoiceAdvisoryCode
    from bel.application.supplier_invoice_request import SupplierRequestAdvisoryCode

    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []})
    db_session.commit()

    advisory_codes = {
        getattr(SalesInvoiceAdvisoryCode, n)
        for n in dir(SalesInvoiceAdvisoryCode)
        if not n.startswith("_") and isinstance(getattr(SalesInvoiceAdvisoryCode, n), str)
    } | {
        getattr(SupplierRequestAdvisoryCode, n)
        for n in dir(SupplierRequestAdvisoryCode)
        if not n.startswith("_") and isinstance(getattr(SupplierRequestAdvisoryCode, n), str)
    }

    center = get_unresolved_work_center(db_session)
    item_codes = {i.code for i in center.items}
    assert not (item_codes & advisory_codes)
    assert all(i.source_type in (SourceType.TASK_EXCEPTION, SourceType.MATCH_CASE, SourceType.COMPUTED_BLOCKER) for i in center.items)


def test_missing_contract_gross_amount_is_not_a_center_source(db_session):
    """MISSING_CONTRACT_GROSS_AMOUNT is a locally-computed, scope-scoped
    invoice-preparation blocker — deliberately outside the Center taxonomy
    (docs §2). It never becomes a TaskException row and never appears as a
    Center item."""
    contract = Contract(
        id=uuid.uuid4(),
        contract_no="PO-BLK-GROSS",
        contract_type=None,
        counterparty="Supplier",
        buyer="Our Own Entity",
        gross_amount=None,
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )
    context = InvoicePreparationContext(
        sales_scopes=(),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract,
                items=(),
                shipments=(),
                invoice_allocations=(),
                invoice_item_allocations=(),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
    )
    report = evaluate_supplier_invoice_request_from_context(context)
    assert [b.code for b in report.decisions[0].blockers] == [SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT]
    # The pure evaluation wrote nothing and no TaskException exists.
    assert ExceptionRepository(db_session).list_all() == []

    center = get_unresolved_work_center(db_session)
    assert all(i.code != SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT for i in center.items)


def test_r009_r015_not_implemented(db_session):
    """No R009-R015 code is a Center source and no such TaskException is
    ever synthesized."""
    from bel.domain.exception import ExceptionType as ET

    _make_task(db_session, ET.BUSINESS_KEY_CONFLICT, {"contract_ids": []})
    db_session.commit()

    r_rules = {"R009", "R010", "R011", "R012", "R013", "R014", "R015"}
    center = get_unresolved_work_center(db_session)
    item_codes = {i.code for i in center.items}
    assert not (item_codes & r_rules)


# ---------------------------------------------------------------------------
# FILTERS (application level)
# ---------------------------------------------------------------------------


def test_source_type_filter(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-FILTER-ST")
    invoice = _make_invoice(db_session, frag.id, InvoiceDirection.PURCHASE)
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": [str(contract.id)]})
    _make_procurement_hcr(db_session, contract, subject_id=invoice.id)
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(source_type=SourceType.MATCH_CASE))
    assert {i.source_type for i in center.items} == {SourceType.MATCH_CASE}


def test_code_filter(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-FILTER-CODE")
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": [str(contract.id)]})
    _make_task(db_session, ExceptionType.ALLOCATION_CAPACITY_EXCEEDED, {"contract_id": str(contract.id)})
    db_session.commit()

    center = get_unresolved_work_center(
        db_session, filters=UnresolvedWorkFilters(code=ExceptionType.ALLOCATION_CAPACITY_EXCEEDED)
    )
    assert [i.code for i in center.items] == [ExceptionType.ALLOCATION_CAPACITY_EXCEEDED]


def test_task_status_filter(db_session):
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []}, status=ExceptionStatus.OPEN)
    _make_task(db_session, ExceptionType.BACKFILL_CONFLICT, {"fact_type": "x", "identity_key": "k"}, status=ExceptionStatus.RESOLVED)
    db_session.commit()

    open_center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(status=ExceptionStatus.OPEN))
    assert {i.code for i in open_center.items} == {ExceptionType.BUSINESS_KEY_CONFLICT}

    resolved_center = get_unresolved_work_center(
        db_session, filters=UnresolvedWorkFilters(status=ExceptionStatus.RESOLVED)
    )
    assert {i.code for i in resolved_center.items} == {ExceptionType.BACKFILL_CONFLICT}


def test_open_only_false_shows_resolved(db_session):
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": []}, status=ExceptionStatus.OPEN)
    _make_task(db_session, ExceptionType.BACKFILL_CONFLICT, {"fact_type": "x", "identity_key": "k"}, status=ExceptionStatus.RESOLVED)
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(open_only=False))
    assert {i.code for i in center.items if i.source_type == SourceType.TASK_EXCEPTION} == {
        ExceptionType.BUSINESS_KEY_CONFLICT,
        ExceptionType.BACKFILL_CONFLICT,
    }


def test_procurement_scope_filter(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, "PO-FILTER-P1")
    c2 = _make_contract(db_session, frag.id, "PO-FILTER-P2")
    _make_task(db_session, ExceptionType.BUSINESS_KEY_CONFLICT, {"contract_ids": [str(c1.id)]})
    _make_task(db_session, ExceptionType.ALLOCATION_CAPACITY_EXCEEDED, {"contract_id": str(c2.id)})
    db_session.commit()

    center = get_unresolved_work_center(
        db_session, filters=UnresolvedWorkFilters(procurement_contract_id=c1.id)
    )
    assert {i.code for i in center.items} == {ExceptionType.BUSINESS_KEY_CONFLICT}
    assert all(i.procurement_contract_id == c1.id for i in center.items)


def test_sales_scope_filter(db_session):
    # sales contracts created WITH a customer, so no auto-raised
    # SalesContractCustomerUnresolved task interferes with the two explicit
    # tasks this test asserts on.
    frag = _make_fragment(db_session)
    s1 = _make_sales_contract(db_session, frag.id, customer="Customer A")
    s2 = _make_sales_contract(db_session, frag.id, customer="Customer B")
    _make_task(db_session, ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED, {"sales_contract_id": str(s1.id)})
    _make_task(db_session, ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED, {"sales_contract_id": str(s2.id)})
    db_session.commit()

    center = get_unresolved_work_center(db_session, filters=UnresolvedWorkFilters(sales_contract_id=s1.id))
    assert len(center.items) == 1
    assert center.items[0].sales_contract_id == s1.id
