"""Phase 2D.1-R4 — Contract Business Ledger application-layer projection.

Covers the HARD invariants from docs/ROADMAP.md's 2D.1-R4 spec: one row
per PROCUREMENT contract, party roles (buyer is never a customer key),
no cross-bridge sales aggregation (section 13/34), current-state
resolution through the existing anchor+revision / current-episode seams
(never re-derived), absence-vs-negation, unresolved-work association via
structured IDs only, filters, and deterministic ordering.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.contract_business_ledger import (
    ContractLedgerFilters,
    get_contract_business_ledger,
)
from bel.application.contract_item_facts import correct_contract_item_fact, create_contract_item_fact
from bel.application.procurement_sales_link import (
    add_procurement_sales_link,
    correct_procurement_sales_link,
    reestablish_procurement_sales_link,
)
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_matching import (
    confirm_sales_invoice_match,
    confirm_sales_payment_match,
    propose_sales_invoice_match,
    propose_sales_payment_match,
)
from bel.application.shipment_facts import correct_shipment_fact, create_shipment_fact
from bel.domain.accrual import Accrual, AccrualStatus
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchCandidate,
    MatchMethod,
    PaymentAllocation,
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
    EvidenceRepository,
    ExceptionRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _make_fragment(session, raw_data=None):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={"section": "test", "index": 0},
        raw_data=raw_data or {},
        created_at=NOW,
    )
    evidence_repo.add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no=None, counterparty="Supplier A", buyer="Our Own Entity"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no or f"C-{uuid.uuid4().hex[:8]}",
        contract_type=None,
        counterparty=counterparty,
        buyer=buyer,
        gross_amount=Decimal("1000.00"),
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(
    session, fragment_id, sales_contract_no=None, our_entity="Our Own Entity", customer=None, contract_date=None
):
    fields = {}
    if customer:
        fields["customer"] = customer
    if contract_date:
        fields["contract_date"] = contract_date
    result = create_sales_contract_fact(
        session,
        our_entity=our_entity,
        sales_contract_no=sales_contract_no or f"SC-{uuid.uuid4().hex[:8]}",
        fields=fields,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    return result.sales_contract


def _link(session, contract, sales_contract, fragment=None):
    frag = fragment or _make_fragment(session)
    result = add_procurement_sales_link(
        session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id,
        confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    return result.link


def _make_purchase_invoice_allocation(session, contract, amount=Decimal("100.00")):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"INV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2026, 1, 5),
        seller=contract.counterparty,
        buyer=contract.buyer,
        net_amount=amount,
        tax_amount=Decimal("0"),
        gross_amount=amount,
        invoice_status=None,
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    MatchCandidateRepository(session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=match_case.id, contract_id=contract.id, created_at=NOW)
    )
    session.flush()
    allocation = InvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=amount,
        match_method=MatchMethod.M001,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    InvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return invoice, allocation


def _make_out_payment_allocation(session, contract, amount=Decimal("50.00")):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2026, 1, 6),
        direction=PaymentDirection.OUT,
        amount=amount,
        counterparty=contract.counterparty,
        business_type=None,
        bank_reference=f"BR-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.PAYMENT,
        subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    MatchCandidateRepository(session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=match_case.id, contract_id=contract.id, created_at=NOW)
    )
    session.flush()
    allocation = PaymentAllocation(
        id=uuid.uuid4(),
        payment_id=payment.id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_amount=amount,
        match_method=MatchMethod.M001,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    PaymentAllocationRepository(session).add(allocation)
    session.flush()
    return payment, allocation


def _make_sales_invoice_allocation(session, sales_contract, amount=Decimal("100.00")):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"SINV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2026, 1, 10),
        seller=sales_contract.our_entity,
        buyer=sales_contract.customer,
        net_amount=amount,
        tax_amount=Decimal("0"),
        gross_amount=amount,
        invoice_status=None,
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    proposal = propose_sales_invoice_match(
        session, invoice_id=invoice.id, sales_contract_ids=[sales_contract.id], created_at=NOW
    )
    result = confirm_sales_invoice_match(
        session, match_case_id=proposal.match_case.id, allocations=[(sales_contract.id, amount)], created_at=NOW
    )
    session.flush()
    return invoice, result


# ---------------------------------------------------------------------------
# A/B/C — primary axis and basic scope
# ---------------------------------------------------------------------------


def test_a_contract_with_no_related_facts(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.items == ()
    assert row.shipments == ()
    assert row.procurement_invoices == ()
    assert row.outgoing_payments == ()
    assert row.accruals == ()
    assert row.sales_scopes == ()
    assert row.has_unresolved is False


def test_b_contract_with_items_one_row(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget", "quantity": Decimal("10"), "unit": "PCS"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-B",
        fields={"product_name": "Gadget", "quantity": Decimal("5"), "unit": "PCS"},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    rows = [r for r in ledger.rows if r.contract.id == contract.id]
    assert len(rows) == 1  # ONE primary row per procurement contract
    assert len(rows[0].items) == 2


def test_c_multiple_shipments_heterogeneous_units_never_summed(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-1",
        execution_date=date(2026, 1, 2),
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-2",
        execution_date=date(2026, 1, 3),
        fields={"quantity": Decimal("20")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.shipments) == 2
    quantities = sorted(s.shipment.quantity for s in row.shipments)
    assert quantities == [Decimal("10"), Decimal("20")]
    # No "total quantity" field is exposed anywhere on the row/shipment DTO.
    assert not hasattr(row, "total_shipment_quantity")


# ---------------------------------------------------------------------------
# D — corrected ContractItem shows only CURRENT state
# ---------------------------------------------------------------------------


def test_d_corrected_item_shows_current_value_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    created = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget", "quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    current_revision = ContractItemRepository(db_session).get_current_revision(created.item.id)
    frag2 = _make_fragment(db_session)
    correct_contract_item_fact(
        db_session,
        contract_item_id=created.item.id,
        based_on_revision_id=current_revision.id,
        fields={"quantity": Decimal("99")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.items) == 1
    assert row.items[0].quantity == Decimal("99")


def test_e_corrected_shipment_shows_current_value_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    created = create_shipment_fact(
        db_session,
        contract_id=contract.id,
        external_reference="EXP-1",
        execution_date=date(2026, 1, 2),
        fields={"quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    current_revision = ShipmentRepository(db_session).get_current_revision(created.shipment.id)
    frag2 = _make_fragment(db_session)
    correct_shipment_fact(
        db_session,
        shipment_id=created.shipment.id,
        based_on_revision_id=current_revision.id,
        fields={"quantity": Decimal("42")},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.shipments) == 1
    assert row.shipments[0].shipment.quantity == Decimal("42")


# ---------------------------------------------------------------------------
# F/G/H — sales scope + customer NULL/known
# ---------------------------------------------------------------------------


def test_f_g_sales_scope_with_unknown_customer_still_shown(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer=None)
    _link(db_session, contract, sales_contract)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.sales_scopes) == 1
    assert row.sales_scopes[0].sales_contract.customer is None  # unknown, never hidden


def test_sales_scope_projects_contract_date(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(
        db_session, frag.id, customer="Customer", contract_date=date(2026, 3, 15)
    )
    _link(db_session, contract, sales_contract)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.sales_scopes[0].sales_contract.contract_date == date(2026, 3, 15)


def test_h_customer_supplement_shows_current(db_session):
    from bel.application.sales_contract_facts import supplement_sales_contract_fact
    from bel.infrastructure.persistence.repositories import SalesContractRepository

    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer=None)
    _link(db_session, contract, sales_contract)
    current_revision = SalesContractRepository(db_session).get_current_revision(sales_contract.id)
    frag2 = _make_fragment(db_session)
    supplement_sales_contract_fact(
        db_session,
        sales_contract_id=sales_contract.id,
        based_on_revision_id=current_revision.id,
        fields={"customer": "Customer Co"},
        source_fragment_id=frag2.id,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.sales_scopes[0].sales_contract.customer == "Customer Co"


# ---------------------------------------------------------------------------
# I/J — retired / reestablished bridge current semantics
# ---------------------------------------------------------------------------


def test_i_retired_link_excluded(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    link = _link(db_session, contract, sales_contract)

    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session,
        superseded_link_id=link.id,
        source_fragment_id=frag2.id,
        confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.sales_scopes == ()


def test_j_reestablished_link_current_exactly_once(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id)
    link = _link(db_session, contract, sales_contract)

    frag2 = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session,
        superseded_link_id=link.id,
        source_fragment_id=frag2.id,
        confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    frag3 = _make_fragment(db_session)
    reestablish_procurement_sales_link(
        db_session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=frag3.id,
        confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.sales_scopes) == 1
    assert row.sales_scopes[0].sales_contract.id == sales_contract.id


# ---------------------------------------------------------------------------
# K/L/M — M:N and the critical no-cross-bridge-aggregation invariant
# ---------------------------------------------------------------------------


def test_k_one_procurement_several_sales(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sc1 = _make_sales_contract(db_session, frag.id, customer="Customer 1")
    sc2 = _make_sales_contract(db_session, frag.id, customer="Customer 2")
    _link(db_session, contract, sc1)
    _link(db_session, contract, sc2)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    customers = {s.sales_contract.customer for s in row.sales_scopes}
    assert customers == {"Customer 1", "Customer 2"}


def test_l_several_procurement_one_sales(db_session):
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, contract_no="PO-A")
    contract_b = _make_contract(db_session, frag.id, contract_no="PO-B")
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Shared Customer")
    _link(db_session, contract_a, sales_contract)
    _link(db_session, contract_b, sales_contract)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row_a = next(r for r in ledger.rows if r.contract.id == contract_a.id)
    row_b = next(r for r in ledger.rows if r.contract.id == contract_b.id)
    assert row_a.sales_scopes[0].sales_contract.id == sales_contract.id
    assert row_b.sales_scopes[0].sales_contract.id == sales_contract.id


def test_m_no_cross_bridge_aggregation_or_apportionment(db_session):
    """Adversarial test (spec section 34/13): two procurement contracts
    both link to the SAME SalesContract, which has confirmed
    SalesInvoiceAllocation = 100. Neither row may show a total of 100+100
    nor 50+50 — both must show the SAME scope-level fact of 100, and
    there must be no field anywhere claiming a procurement-attributed
    sales total."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, contract_no="PO-A")
    contract_b = _make_contract(db_session, frag.id, contract_no="PO-B")
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Shared Customer")
    _link(db_session, contract_a, sales_contract)
    _link(db_session, contract_b, sales_contract)
    _make_sales_invoice_allocation(db_session, sales_contract, amount=Decimal("100.00"))
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row_a = next(r for r in ledger.rows if r.contract.id == contract_a.id)
    row_b = next(r for r in ledger.rows if r.contract.id == contract_b.id)

    scope_a = row_a.sales_scopes[0]
    scope_b = row_b.sales_scopes[0]
    assert len(scope_a.sales_invoice_allocations) == 1
    assert len(scope_b.sales_invoice_allocations) == 1
    assert scope_a.sales_invoice_allocations[0].allocation.allocated_gross_amount == Decimal("100.00")
    assert scope_b.sales_invoice_allocations[0].allocation.allocated_gross_amount == Decimal("100.00")

    # No forbidden field names exist anywhere on the DTOs (structural guard).
    forbidden = {"sales_invoice_amount_for_contract", "sales_receipt_amount_for_contract", "sales_total"}
    for row in (row_a, row_b):
        assert not (forbidden & set(vars(row).keys()))
        for scope in row.sales_scopes:
            assert not (forbidden & set(vars(scope).keys()))


# ---------------------------------------------------------------------------
# N/O/P/Q — allocation states, per-scope only for sales
# ---------------------------------------------------------------------------


def test_n_purchase_invoice_allocation(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    invoice, allocation = _make_purchase_invoice_allocation(db_session, contract)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.procurement_invoices) == 1
    assert row.procurement_invoices[0].invoice.id == invoice.id


def test_o_out_payment_allocation(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    payment, allocation = _make_out_payment_allocation(db_session, contract)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.outgoing_payments) == 1
    assert row.outgoing_payments[0].payment.id == payment.id


def test_procurement_columns_reject_misdirected_allocations(db_session):
    """Adversarial (Gate G/H): InvoiceAllocation/PaymentAllocation carry
    no direction constraint of their own — only the M001 matching pass
    that writes them today filters to PURCHASE/OUT. A SALES invoice or
    IN payment wrongly attributed through these procurement tables (e.g.
    a future bug, a bad manual write) must NEVER be displayed under
    "procurement invoices" / "outgoing payments"."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)

    sales_invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key="MISDIRECTED-SALES-INV",
        issue_date=date(2026, 1, 5),
        seller="Our Own Entity",
        buyer="Some Customer",
        net_amount=Decimal("10"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(db_session).add(sales_invoice)
    db_session.flush()
    bad_match_case_1 = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=sales_invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(bad_match_case_1)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(),
            invoice_id=sales_invoice.id,
            contract_id=contract.id,
            match_case_id=bad_match_case_1.id,
            allocated_gross_amount=Decimal("10"),
            match_method=MatchMethod.M001,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    )

    in_payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2026, 1, 6),
        direction=PaymentDirection.IN,
        amount=Decimal("20"),
        counterparty="Some Customer",
        business_type=None,
        bank_reference="MISDIRECTED-IN",
        description=None,
        running_balance=None,
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    PaymentRepository(db_session).add(in_payment)
    db_session.flush()
    bad_match_case_2 = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.PAYMENT,
        subject_id=in_payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(bad_match_case_2)
    db_session.flush()
    PaymentAllocationRepository(db_session).add(
        PaymentAllocation(
            id=uuid.uuid4(),
            payment_id=in_payment.id,
            contract_id=contract.id,
            match_case_id=bad_match_case_2.id,
            allocated_amount=Decimal("20"),
            match_method=MatchMethod.M001,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.procurement_invoices == ()
    assert row.outgoing_payments == ()


def test_p_sales_invoice_allocation_per_scope(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Customer")
    _link(db_session, contract, sales_contract)
    invoice, _ = _make_sales_invoice_allocation(db_session, sales_contract, amount=Decimal("77.00"))
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    scope = row.sales_scopes[0]
    assert len(scope.sales_invoice_allocations) == 1
    assert scope.sales_invoice_allocations[0].invoice.id == invoice.id
    assert scope.sales_invoice_allocations[0].allocation.allocated_gross_amount == Decimal("77.00")
    assert scope.incoming_receipt_allocations == ()


def _make_incoming_receipt_allocation(session, sales_contract, amount=Decimal("60.00")):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2026, 1, 20),
        direction=PaymentDirection.IN,
        amount=amount,
        counterparty=sales_contract.customer,
        business_type=None,
        bank_reference=f"BR-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    proposal = propose_sales_payment_match(
        session, payment_id=payment.id, sales_contract_ids=[sales_contract.id], created_at=NOW
    )
    result = confirm_sales_payment_match(
        session, match_case_id=proposal.match_case.id, allocations=[(sales_contract.id, amount)], created_at=NOW
    )
    session.flush()
    return payment, result


def test_q_incoming_receipt_allocation_per_scope(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Customer")
    _link(db_session, contract, sales_contract)
    payment, _ = _make_incoming_receipt_allocation(db_session, sales_contract, amount=Decimal("60.00"))
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    scope = row.sales_scopes[0]
    assert len(scope.incoming_receipt_allocations) == 1
    assert scope.incoming_receipt_allocations[0].payment.id == payment.id
    assert scope.incoming_receipt_allocations[0].allocation.allocated_amount == Decimal("60.00")
    assert scope.sales_invoice_allocations == ()


# ---------------------------------------------------------------------------
# R — current accrual state, never period-close projection
# ---------------------------------------------------------------------------


def test_r_current_accrual_state(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    created = create_contract_item_fact(
        db_session,
        contract_id=contract.id,
        source_item_key="ITEM-A",
        fields={"product_name": "Widget", "quantity": Decimal("10")},
        source_fragment_id=frag.id,
        created_at=NOW,
    )
    accrual = Accrual(
        id=uuid.uuid4(),
        period="2026-01",
        contract_item_id=created.item.id,
        quantity=Decimal("10"),
        estimated_cost=Decimal("500.00"),
        basis="TEST",
        status=AccrualStatus.ACTIVE,
        created_from_fact_id=uuid.uuid4(),
        created_at=NOW,
    )
    AccrualRepository(db_session).add(accrual)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert len(row.accruals) == 1
    assert row.accruals[0].remaining_estimated_cost == Decimal("500.00")
    assert row.accruals[0].projected_status == AccrualStatus.ACTIVE
    # Never the period-close engine's projected decision object.
    assert not hasattr(row, "decisions")


# ---------------------------------------------------------------------------
# S/T — unresolved work via structured IDs only
# ---------------------------------------------------------------------------


def test_s_open_task_shows_indicator_resolved_does_not(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    unrelated_contract = _make_contract(db_session, frag.id, contract_no="UNRELATED")
    exception = TaskException(
        id=uuid.uuid4(),
        exception_type=ExceptionType.ALLOCATION_CAPACITY_EXCEEDED,
        status=ExceptionStatus.OPEN,
        summary="test",
        detail={"contract_id": str(contract.id)},
        created_at=NOW,
    )
    ExceptionRepository(db_session).add(exception)
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    unrelated_row = next(r for r in ledger.rows if r.contract.id == unrelated_contract.id)
    assert row.has_unresolved is True
    assert unrelated_row.has_unresolved is False  # unrelated Task never contaminates

    ExceptionRepository(db_session).update_status(exception.id, ExceptionStatus.RESOLVED)
    db_session.commit()
    ledger2 = get_contract_business_ledger(db_session)
    row2 = next(r for r in ledger2.rows if r.contract.id == contract.id)
    assert row2.has_unresolved is False


def test_t_hcr_match_case_procurement_leg(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key="INV-HCR",
        issue_date=date(2026, 1, 5),
        seller=contract.counterparty,
        buyer=contract.buyer,
        net_amount=Decimal("10"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    MatchCandidateRepository(db_session).add(
        MatchCandidate(id=uuid.uuid4(), match_case_id=match_case.id, contract_id=contract.id, created_at=NOW)
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.has_unresolved is True


def test_t_hcr_sales_match_case_flags_linked_procurement_row(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Customer")
    _link(db_session, contract, sales_contract)

    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key="SINV-HCR",
        issue_date=date(2026, 1, 5),
        seller=sales_contract.our_entity,
        buyer=sales_contract.customer,
        net_amount=Decimal("10"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("10"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    propose_sales_invoice_match(
        db_session, invoice_id=invoice.id, sales_contract_ids=[sales_contract.id], created_at=NOW
    )
    db_session.commit()

    ledger = get_contract_business_ledger(db_session)
    row = next(r for r in ledger.rows if r.contract.id == contract.id)
    assert row.sales_scopes[0].has_unresolved is True
    assert row.has_unresolved is True


# ---------------------------------------------------------------------------
# U — filters
# ---------------------------------------------------------------------------


def test_u_filters_by_contract_no_supplier_our_entity_customer(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, contract_no="PO-0001", counterparty="Alpha Supplier", buyer="Entity One")
    c2 = _make_contract(db_session, frag.id, contract_no="PO-0002", counterparty="Beta Supplier", buyer="Entity Two")
    sc = _make_sales_contract(db_session, frag.id, our_entity="Entity One", customer="Big Customer")
    _link(db_session, c1, sc)
    db_session.commit()

    by_no = get_contract_business_ledger(db_session, ContractLedgerFilters(contract_no="0001"))
    assert {r.contract.id for r in by_no.rows} == {c1.id}

    by_supplier = get_contract_business_ledger(db_session, ContractLedgerFilters(supplier="beta"))
    assert {r.contract.id for r in by_supplier.rows} == {c2.id}

    by_entity = get_contract_business_ledger(db_session, ContractLedgerFilters(our_entity="Entity Two"))
    assert {r.contract.id for r in by_entity.rows} == {c2.id}

    by_customer = get_contract_business_ledger(db_session, ContractLedgerFilters(customer="big"))
    assert {r.contract.id for r in by_customer.rows} == {c1.id}

    # Unknown customer must never match Contract.buyer.
    by_customer_no_match = get_contract_business_ledger(db_session, ContractLedgerFilters(customer="Entity Two"))
    assert c2.id not in {r.contract.id for r in by_customer_no_match.rows}


def test_u_has_unresolved_filter(db_session):
    frag = _make_fragment(db_session)
    c1 = _make_contract(db_session, frag.id, contract_no="PO-R1")
    c2 = _make_contract(db_session, frag.id, contract_no="PO-R2")
    ExceptionRepository(db_session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=ExceptionType.ALLOCATION_CAPACITY_EXCEEDED,
            status=ExceptionStatus.OPEN,
            summary="t",
            detail={"contract_id": str(c1.id)},
            created_at=NOW,
        )
    )
    db_session.commit()

    unresolved_only = get_contract_business_ledger(db_session, ContractLedgerFilters(has_unresolved=True))
    ids = {r.contract.id for r in unresolved_only.rows}
    assert c1.id in ids
    assert c2.id not in ids


# ---------------------------------------------------------------------------
# V — deterministic ordering
# ---------------------------------------------------------------------------


def test_v_deterministic_ordering_by_contract_no(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, contract_no="PO-003")
    _make_contract(db_session, frag.id, contract_no="PO-001")
    _make_contract(db_session, frag.id, contract_no="PO-002")
    db_session.commit()

    ledger1 = get_contract_business_ledger(db_session)
    ledger2 = get_contract_business_ledger(db_session)
    order1 = [r.contract.contract_no for r in ledger1.rows]
    order2 = [r.contract.contract_no for r in ledger2.rows]
    assert order1 == order2 == sorted(order1)


# ---------------------------------------------------------------------------
# Zero-write
# ---------------------------------------------------------------------------


def _table_counts(session):
    from bel.infrastructure.persistence import models as m

    counts = {}
    for name in dir(m):
        obj = getattr(m, name)
        if isinstance(obj, type) and hasattr(obj, "__tablename__"):
            counts[obj.__tablename__] = session.query(obj).count()
    return counts


def test_zero_write(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id)
    sales_contract = _make_sales_contract(db_session, frag.id, customer="Customer")
    _link(db_session, contract, sales_contract)
    db_session.commit()

    before = _table_counts(db_session)
    get_contract_business_ledger(db_session)
    after = _table_counts(db_session)
    assert before == after
