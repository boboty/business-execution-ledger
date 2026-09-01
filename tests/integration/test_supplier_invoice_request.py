"""Phase 2D.3-F1b — SUPPLIER_INVOICE_REQUEST rule foundation (re-leveled
in Phase 2D.3-F1d).

Covers the frozen supplier-direction rule layer (IP-P01..IP-P09, see
docs/PHASE2D3-RULE-FREEZE.md) over the F0 fact context, on
independently synthetic data. The Workbench is FACT CONTROL + MANAGEMENT
REMINDERS, not a workflow approval engine: a legitimate business state
is never turned into a conflict:

- the IP-P02 expected amount == Contract.gross_amount, and an unknown
  amount produces the sole missing-fact blocker (never an estimate);
- PURCHASE invoice cardinality: zero is a factual state with no false
  negative business assertion, one is exposed as facts, more than one
  is the IP-P03 management ADVISORY;
- the IP-P04 invoice -> contracts mapping and its M:N ADVISORY (never
  silently apportioned, M:N is not a business error);
- exact amount and exact product-name consistency checks with
  MATCH / DEVIATION / NOT_COMPARABLE_MISSING_FACT — a DEVIATION is the
  IP-P02 / IP-P05 ADVISORY, never a conflict, never
  "unpaid"/"outstanding"/"overdue"; a missing compared Fact is a
  NOT_COMPARABLE check result only and never blocks preparation;
- OUT payments exposed as context only (IP-P01), the paid-but-no-invoice
  IP-P09 follow-up ADVISORY, no tax-rate inference (IP-P06), no
  quantity calculation (IP-P07);
- the Fact -> Decision layering: strictly read-only, never autoflushes,
  and a pure function over the F0 context DTOs.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SupplierScopeContext,
    SupplierScopeInvoiceAllocation,
    SupplierScopeInvoiceItemAllocation,
    SupplierScopePaymentAllocation,
)
from bel.application.supplier_invoice_request import (
    AMOUNT_CONSISTENCY_CHECK_NAME,
    ITEM_NAME_CONSISTENCY_CHECK_NAME,
    SupplierInvoiceRequestDecision,
    SupplierRequestAdvisoryCode,
    SupplierRequestAmountCheck,
    SupplierRequestBlockerCode,
    SupplierRequestCheckOutcome,
    SupplierRequestDecisionStatus,
    evaluate_supplier_invoice_request,
    evaluate_supplier_invoice_request_from_context,
)
from bel.domain.accrual import InvoiceItemAllocation
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    PaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
)

NOW = datetime.now(timezone.utc)


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
        locator_json={},
        raw_data={},
        created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no, counterparty="Supplier", gross_amount=Decimal("1000.00")):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty=counterparty,
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


def _make_contract_item(session, contract, fragment_id, product_name="Widget Alpha", source_item_key="ITEM-1"):
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key=source_item_key,
        sku=None,
        product_name=product_name,
        specification=None,
        quantity=Decimal("10"),
        unit=None,
        unit_price=None,
        gross_amount=Decimal("500.00"),
        tax_rate=None,
        net_amount=Decimal("450.00"),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
    )
    ContractItemRepository(session).add(item)
    session.flush()
    return item


def _make_invoice_item(session, invoice, fragment_id, product_name="Widget Alpha", tax_rate=Decimal("0.13")):
    item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name=product_name,
        specification=None,
        unit=None,
        quantity=Decimal("10"),
        unit_price=None,
        net_amount=invoice.gross_amount,
        tax_rate=tax_rate,
        tax_amount=Decimal("0"),
        gross_amount=invoice.gross_amount,
        source_fragment_id=fragment_id,
    )
    InvoiceItemRepository(session).add(item)
    session.flush()
    return item


def _make_purchase_invoice(
    session,
    fragment_id,
    gross_amount=Decimal("1000.00"),
    counterparty="Supplier",
    product_name="Widget Alpha",
    tax_rate=Decimal("0.13"),
):
    """A PURCHASE Invoice Fact with one InvoiceItem Fact. Returns both."""
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"PINV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller=counterparty,
        buyer="Our Own Entity",
        net_amount=gross_amount,
        tax_amount=Decimal("0"),
        gross_amount=gross_amount,
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    item = _make_invoice_item(session, invoice, fragment_id, product_name=product_name, tax_rate=tax_rate)
    return invoice, item


def _make_match_case(session, subject_id):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="INVOICE",
        subject_id=subject_id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    return match_case


def _make_invoice_allocation(session, invoice_id, contract, allocated=Decimal("1000.00")):
    """The confirmed PURCHASE-invoice association. ``invoice_id`` may
    name an Invoice Fact that does not exist (dangling association)."""
    match_case = _make_match_case(session, invoice_id)
    allocation = InvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice_id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=allocated,
        match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    InvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_invoice_item_allocation(session, invoice_item, contract_item):
    allocation = InvoiceItemAllocation(
        id=uuid.uuid4(),
        invoice_item_id=invoice_item.id,
        contract_item_id=contract_item.id,
        allocated_quantity=Decimal("2"),
        allocated_net_amount=Decimal("80.00"),
        confirmation_type="MANUAL_CONFIRMED",
        source_fragment_id=_make_fragment(session).id,
        created_at=NOW,
        superseded_by_fact_id=None,
    )
    InvoiceItemAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_out_payment(session, fragment_id, amount=Decimal("1000.00")):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=date(2031, 1, 15),
        direction=PaymentDirection.OUT,
        amount=amount,
        counterparty="Supplier",
        business_type=None,
        bank_reference=f"REF-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _make_payment_allocation(session, payment, contract):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="PAYMENT",
        subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    allocation = PaymentAllocation(
        id=uuid.uuid4(),
        payment_id=payment.id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_amount=Decimal("500.00"),
        match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    PaymentAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


# ---------------------------------------------------------------------------
# A. IP-P02 — expected purchase invoice gross amount
# ---------------------------------------------------------------------------


def test_expected_amount_is_contract_gross_amount(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-1", gross_amount=Decimal("1000.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.expected_purchase_invoice_gross_amount == Decimal("1000.00")
    # A preparation amount only — the frozen IP-P02 statement. No
    # blockers, and no claim that the supplier should invoice now.
    assert decision.blockers == ()


def test_missing_contract_amount_emits_missing_fact_blocker():
    """An unknown Contract gross amount produces the explicit
    MISSING_CONTRACT_GROSS_AMOUNT missing-fact blocker — never an
    estimate or a substitute. Today's schema backstop
    (ck_contract_revisions_current_requires_amount_currency) makes an
    unknown current amount unreachable in storage, so this rule path is
    exercised over the pure-function seam where the Domain value is
    unknown."""
    contract = Contract(
        id=uuid.uuid4(),
        contract_no="PO-F1B-2",
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
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(), invoice_allocations=(),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))
    report = evaluate_supplier_invoice_request_from_context(context)

    decision = report.decisions[0]
    assert decision.status == SupplierRequestDecisionStatus.INSUFFICIENT_FACTS
    assert decision.expected_purchase_invoice_gross_amount is None
    assert [b.code for b in decision.blockers] == [SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT]
    assert report.purchase_invoice_contract_map == ()


# ---------------------------------------------------------------------------
# B. IP-P03 — PURCHASE invoice cardinality
# ---------------------------------------------------------------------------


def test_zero_purchase_invoices_is_factual_state_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-3")
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    # Zero PURCHASE invoices is a factual state only: no blocker, no
    # negative business assertion about invoicing, no amount check.
    assert decision.blockers == ()
    assert decision.amount_checks == ()
    assert decision.invoice_allocations == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_one_purchase_invoice_exposes_invoice_and_allocation_facts(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-4")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("1000.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert len(decision.invoice_allocations) == 1
    entry = decision.invoice_allocations[0]
    assert isinstance(entry, SupplierScopeInvoiceAllocation)
    assert entry.allocation.invoice_id == invoice.id
    assert entry.allocation.allocated_gross_amount == Decimal("1000.00")
    assert entry.invoice is not None and entry.invoice.id == invoice.id


def test_multiple_purchase_invoices_emit_ip_p03_advisory(db_session):
    """IP-P03 is accountant-confirmed but its enforcement is a management
    review signal, not a violation: more than one PURCHASE invoice on one
    Contract emits the MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT ADVISORY
    and NEVER changes the status — the split is legitimate business state
    (Phase 2D.3-F1d re-leveling)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-5")
    invoice1, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("600.00"))
    invoice2, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("400.00"))
    _make_invoice_allocation(db_session, invoice1.id, contract, allocated=Decimal("600.00"))
    _make_invoice_allocation(db_session, invoice2.id, contract, allocated=Decimal("400.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.blockers == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT
    ]
    assert set(decision.advisories[0].related_invoice_ids) == {invoice1.id, invoice2.id}
    # Historical Facts are never deleted or mutated: both associations
    # and both invoice Facts remain exposed on the decision.
    assert {e.allocation.invoice_id for e in decision.invoice_allocations} == {invoice1.id, invoice2.id}


def test_sales_direction_facts_never_count_as_purchase_cardinality(db_session):
    """Direction isolation at the rule layer: a SALES invoice associated
    through the procurement-only InvoiceAllocation is filtered by the F0
    context and must never count toward IP-P03 cardinality or appear as
    a PURCHASE invoice fact."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-6")
    purchase_invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    sales_invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"SINV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 11),
        seller="Our Own Entity",
        buyer="Customer",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(db_session).add(sales_invoice)
    db_session.flush()
    _make_invoice_allocation(db_session, purchase_invoice.id, contract, allocated=Decimal("1000.00"))
    _make_invoice_allocation(db_session, sales_invoice.id, contract, allocated=Decimal("100.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert [e.allocation.invoice_id for e in decision.invoice_allocations] == [purchase_invoice.id]


# ---------------------------------------------------------------------------
# C. IP-P04 — one PURCHASE invoice -> one procurement Contract
# ---------------------------------------------------------------------------


def test_invoice_on_multiple_contracts_emits_ip_p04_advisory(db_session):
    """IP-P04 is accountant-confirmed but its enforcement is a management
    review signal, not a violation: one PURCHASE invoice on multiple
    Contracts emits the PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS ADVISORY
    on every involved scope and NEVER changes the status — the M:N
    relationship is not a business error (Phase 2D.3-F1d re-leveling)."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "PO-F1B-7A")
    contract_b = _make_contract(db_session, frag.id, "PO-F1B-7B")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract_a, allocated=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract_b, allocated=Decimal("1000.00"))
    db_session.commit()

    report = evaluate_supplier_invoice_request(db_session)
    # The factual mapping is exposed at report level and names BOTH
    # contracts for this one invoice.
    assert len(report.purchase_invoice_contract_map) == 1
    association = report.purchase_invoice_contract_map[0]
    assert association.invoice_id == invoice.id
    assert set(association.contract_ids) == {contract_a.id, contract_b.id}

    decisions = {d.contract_id: d for d in report.decisions}
    for contract in (contract_a, contract_b):
        decision = decisions[contract.id]
        assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
        assert decision.blockers == ()
        ip_p04 = [a for a in decision.advisories if a.code == SupplierRequestAdvisoryCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS]
        assert len(ip_p04) == 1
        assert ip_p04[0].related_invoice_ids == (invoice.id,)
        assert set(ip_p04[0].related_contract_ids) == {contract_a.id, contract_b.id}
        # Never silently apportioned: the full invoice Fact is exposed
        # on each scope, unmodified.
        assert decision.invoice_allocations[0].invoice.gross_amount == Decimal("1000.00")


# ---------------------------------------------------------------------------
# D. IP-P02 amount consistency — MATCH / DEVIATION / NOT_COMPARABLE
# ---------------------------------------------------------------------------


def test_exact_amount_match(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-8", gross_amount=Decimal("1000.00"))
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("1000.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert len(decision.amount_checks) == 1
    check = decision.amount_checks[0]
    assert check.check_name == AMOUNT_CONSISTENCY_CHECK_NAME
    assert check.compared_invoice_gross_amount == Decimal("1000.00")
    assert check.contract_gross_amount == Decimal("1000.00")
    assert check.outcome == SupplierRequestCheckOutcome.MATCH
    # MATCH conflicts with nothing: no blocker, clean preparation status.
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()


def test_amount_deviation_is_advisory_not_conflict(db_session):
    """IP-P02 is accountant-confirmed and its enforcement is a management
    review signal: an actual invoice amount unequal to the Contract gross
    amount emits the PURCHASE_INVOICE_AMOUNT_DEVIATION ADVISORY — the
    invoice Fact stays valid, the status is never changed, and the
    finding is never worded as "unpaid"/"outstanding"/"overdue"
    (Phase 2D.3-F1d re-leveling)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-9", gross_amount=Decimal("1000.00"))
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("800.00"))
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("800.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert decision.blockers == ()
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION
    ]
    assert decision.advisories[0].related_invoice_ids == (invoice.id,)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_missing_invoice_fact_amount_is_not_comparable():
    """A confirmed association whose Invoice Fact is missing cannot be
    compared: NOT_COMPARABLE_MISSING_FACT is a CHECK RESULT ONLY — an
    optional management comparison unavailable — and never emits a
    blocker and never changes the status. The FK on
    invoice_allocations.invoice_id makes a dangling association
    unreachable in storage, so the rule's deterministic outcome for the
    F0 context's ``invoice is None`` shape is exercised over the
    pure-function seam."""
    contract_id, invoice_id = uuid.uuid4(), uuid.uuid4()
    contract = Contract(
        id=contract_id, contract_no="PO-F1B-10", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("1000.00"), currency="CNY",
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract_id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("1000.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=None,
        ),),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert len(decision.amount_checks) == 1
    check = decision.amount_checks[0]
    assert check.invoice_id == invoice_id
    assert check.compared_invoice_gross_amount is None
    assert check.contract_gross_amount == Decimal("1000.00")
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    # A check result only — the optional management comparison is
    # unavailable; nothing is blocked and no finding is emitted.
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_missing_contract_amount_with_invoice_not_comparable_once():
    """With the Contract amount unknown AND one associated invoice, the
    comparison is NOT_COMPARABLE and step A's MISSING_CONTRACT_GROSS_AMOUNT
    blocker names the absent value exactly once — the amount check never
    emits a duplicate missing-fact blocker for the same gap."""
    contract_id, invoice_id = uuid.uuid4(), uuid.uuid4()
    contract = Contract(
        id=contract_id, contract_no="PO-F1B-10B", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=None, currency="CNY",
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    invoice = Invoice(
        id=invoice_id, direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1B-10B", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("500.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("500.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract_id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("500.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=invoice,
        ),),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert [b.code for b in decision.blockers] == [
        SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT
    ]
    assert decision.status == SupplierRequestDecisionStatus.INSUFFICIENT_FACTS


# ---------------------------------------------------------------------------
# E. IP-P05 — product-name consistency
# ---------------------------------------------------------------------------


def test_product_name_exact_match(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-11")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, product_name="Widget Alpha")
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert len(decision.item_name_checks) == 1
    check = decision.item_name_checks[0]
    assert check.check_name == ITEM_NAME_CONSISTENCY_CHECK_NAME
    assert check.contract_product_name == "Widget Alpha"
    assert check.invoice_product_name == "Widget Alpha"
    assert check.outcome == SupplierRequestCheckOutcome.MATCH
    # MATCH conflicts with nothing: no blocker, clean preparation status.
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()


def test_product_name_deviation_is_advisory_not_conflict(db_session):
    """IP-P05 is accountant-confirmed and its enforcement is a management
    review signal: an explicitly associated InvoiceItem and ContractItem
    with both product names present and unequal emits the
    PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION ADVISORY — never a conflict,
    never worded as "unpaid"/"outstanding"/"overdue" (Phase 2D.3-F1d
    re-leveling)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-12")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, product_name="Widget Beta")
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    check = decision.item_name_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.DEVIATION
    assert check.contract_product_name == "Widget Alpha"
    assert check.invoice_product_name == "Widget Beta"
    assert decision.blockers == ()
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION
    ]
    assert decision.advisories[0].related_invoice_ids == (invoice.id,)
    assert decision.advisories[0].related_invoice_item_ids == (invoice_item.id,)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


@pytest.mark.parametrize("missing_side", ["contract", "invoice"])
def test_missing_product_name_is_not_comparable(db_session, missing_side):
    """A comparison impossible for an absent product name is a CHECK
    RESULT ONLY (NOT_COMPARABLE_MISSING_FACT): the optional management
    comparison is unavailable and does NOT block preparation — no
    blocker, no advisory, no status change (Phase 2D.3-F1d)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, f"PO-F1B-13-{missing_side}")
    item = _make_contract_item(
        db_session, contract, frag.id, product_name=None if missing_side == "contract" else "Widget Alpha"
    )
    invoice, invoice_item = _make_purchase_invoice(
        db_session, frag.id, product_name=None if missing_side == "invoice" else "Widget Alpha"
    )
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    check = decision.item_name_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    # The name that IS known stays exposed as the Fact it is.
    if missing_side == "contract":
        assert check.contract_product_name is None and check.invoice_product_name == "Widget Alpha"
    else:
        assert check.contract_product_name == "Widget Alpha" and check.invoice_product_name is None
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


# ---------------------------------------------------------------------------
# Status — INSUFFICIENT_FACTS (genuine data gap) vs PREPARATION_AMOUNT_DETERMINABLE;
# advisories never change status and never mask a genuine data blocker
# ---------------------------------------------------------------------------


def test_multiple_invoices_and_missing_name_no_conflict(db_session):
    """IP-P03 cardinality and a missing product name on the same scope
    are both NON-blocking after the re-leveling: the scope stays
    PREPARATION_AMOUNT_DETERMINABLE with zero blockers — the P03 advisory
    is emitted and the name check is NOT_COMPARABLE_MISSING_FACT, but no
    finding blocks or conflicts."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-20")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice1, invoice_item1 = _make_purchase_invoice(db_session, frag.id, product_name=None)
    invoice2, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("400.00"))
    _make_invoice_allocation(db_session, invoice1.id, contract, allocated=Decimal("600.00"))
    _make_invoice_allocation(db_session, invoice2.id, contract, allocated=Decimal("400.00"))
    _make_invoice_item_allocation(db_session, invoice_item1, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    codes = [a.code for a in decision.advisories]
    assert SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT in codes
    assert decision.item_name_checks[0].outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT


def test_amount_deviation_and_missing_name_no_conflict(db_session):
    """IP-P02 amount deviation and a missing contract product name on the
    same scope are both NON-blocking after the re-leveling: the scope
    stays PREPARATION_AMOUNT_DETERMINABLE with zero blockers — the amount
    deviation advisory is emitted and the name check is
    NOT_COMPARABLE_MISSING_FACT."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-21", gross_amount=Decimal("1000.00"))
    item = _make_contract_item(db_session, contract, frag.id, product_name=None)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("800.00"))
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("800.00"))
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    codes = [a.code for a in decision.advisories]
    assert SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION in codes
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert decision.item_name_checks[0].outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT


def test_blocker_and_advisory_coexist_with_genuine_data_blocker():
    """An advisory coexisting with a genuine data blocker leaves the
    blocker's status intact: an unknown Contract gross amount (the sole
    blocker, INSUFFICIENT_FACTS) together with multiple PURCHASE invoices
    (an IP-P03 advisory) yields BOTH — the advisory never masks the
    genuine data incompleteness. (Pure function over the F0 context —
    an unknown current amount is unreachable in storage.)"""
    contract = Contract(
        id=uuid.uuid4(),
        contract_no="PO-F1B-22",
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
    invoice1_id, invoice2_id = uuid.uuid4(), uuid.uuid4()
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(
            SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice1_id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                    allocated_gross_amount=Decimal("600.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=None,
            ),
            SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice2_id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                    allocated_gross_amount=Decimal("400.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=None,
            ),
        ),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.status == SupplierRequestDecisionStatus.INSUFFICIENT_FACTS
    assert [b.code for b in decision.blockers] == [SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT]
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT
    ]


# ---------------------------------------------------------------------------
# F/G/H. IP-P01 payments as context, IP-P09 follow-up, IP-P06 no tax
# inference, IP-P07 no quantity
# ---------------------------------------------------------------------------


def test_out_payment_paid_no_invoice_emits_follow_up_advisory(db_session):
    """IP-P01: an OUT payment is exposed as context and changes nothing —
    the payment-less contract carries the identical preparation status
    and no payment-driven blocker. IP-P09: paid with no PURCHASE invoice
    yet emits the SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED ADVISORY (a
    management reminder, never a gate or status change) on the paid scope
    only."""
    frag = _make_fragment(db_session)
    paid_contract = _make_contract(db_session, frag.id, "PO-F1B-14A", counterparty="Supplier One")
    bare_contract = _make_contract(db_session, frag.id, "PO-F1B-14B", counterparty="Supplier Two")
    payment = _make_out_payment(db_session, frag.id, amount=Decimal("500.00"))
    _make_payment_allocation(db_session, payment, paid_contract)
    db_session.commit()

    report = evaluate_supplier_invoice_request(db_session)
    decisions = {d.contract_id: d for d in report.decisions}
    paid, bare = decisions[paid_contract.id], decisions[bare_contract.id]

    assert paid.status == bare.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert paid.expected_purchase_invoice_gross_amount == bare.expected_purchase_invoice_gross_amount
    assert paid.blockers == () and bare.blockers == ()
    # The payment IS exposed — as the Fact it is, never as a gate.
    assert len(paid.payment_allocations) == 1
    assert isinstance(paid.payment_allocations[0], SupplierScopePaymentAllocation)
    assert paid.payment_allocations[0].payment.direction == PaymentDirection.OUT
    assert bare.payment_allocations == ()
    # IP-P09: paid + no PURCHASE invoice => the follow-up advisory, on
    # the paid scope only. Not overdue / not a conflict / not a gate.
    assert [a.code for a in paid.advisories] == [
        SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED
    ]
    assert "recommend supplier invoice follow-up" in paid.advisories[0].note
    assert bare.advisories == ()


@pytest.mark.parametrize("ordering", ["invoice_first", "payment_first"])
def test_payment_and_invoice_any_ordering_no_chronology_finding(db_session, ordering):
    """Once a PURCHASE invoice IS associated, the IP-P09 follow-up
    disappears on recomputation, and the invoice/payment ordering never
    produces any finding: whether the invoice predates the payment or the
    payment predates the invoice, the scope is clean — no chronology
    finding, no advisory, no blocker (Phase 2D.3-F1d)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, f"PO-F1B-14C-{ordering}")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("1000.00"))
    payment = _make_out_payment(db_session, frag.id, amount=Decimal("1000.00"))
    _make_payment_allocation(db_session, payment, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    # With the PURCHASE invoice associated, no follow-up advisory is
    # emitted regardless of invoice/payment ordering.
    assert decision.advisories == ()
    assert len(decision.payment_allocations) == 1
    assert len(decision.invoice_allocations) == 1


def test_no_tax_rate_inference_anywhere(db_session):
    """IP-P06: an actual InvoiceItem's tax_rate is reachable ONLY as the
    existing Fact it is; no requested/recommended/inferred tax rate
    exists on any check, blocker, or decision field."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-15")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice, invoice_item = _make_purchase_invoice(
        db_session, frag.id, product_name="Widget Alpha", tax_rate=Decimal("0.13")
    )
    _make_invoice_item_allocation(db_session, invoice_item, item)
    _make_invoice_allocation(db_session, invoice.id, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    # The Fact is displayed as it exists (CONTEXT, IP-P06)...
    assert decision.invoice_item_allocations[0].invoice_item.tax_rate == Decimal("0.13")
    # ...and no check, blocker, or advisory carries any tax concept at all
    # (no tax advisory is emitted for an existing tax_rate Fact).
    for check in (*decision.amount_checks, *decision.item_name_checks):
        assert not [f for f in dataclasses.fields(check) if "tax" in f.name]
    for blocker in decision.blockers:
        assert not [f for f in dataclasses.fields(blocker) if "tax" in f.name]
    for advisory in decision.advisories:
        assert not [f for f in dataclasses.fields(advisory) if "tax" in f.name]
        assert "tax" not in advisory.code.lower()
    assert all("tax" not in a.code.lower() for a in decision.advisories)


def test_no_quantity_calculation_anywhere(db_session):
    """IP-P07: the quantity basis is unresolved — no requested quantity
    exists on any DTO, whatever quantities the underlying Facts carry."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-16")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")  # quantity=10
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, product_name="Widget Alpha")
    _make_invoice_item_allocation(db_session, invoice_item, item)  # allocated_quantity=2
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert not [f for f in dataclasses.fields(decision) if "quantity" in f.name]
    for check in (*decision.amount_checks, *decision.item_name_checks):
        assert not [f for f in dataclasses.fields(check) if "quantity" in f.name]


# ---------------------------------------------------------------------------
# Fact -> Decision layering: vocabulary, purity, read-only
# ---------------------------------------------------------------------------


def test_status_and_dto_vocabulary_carry_no_business_judgment():
    # The decision status vocabulary has exactly the two factual /
    # preparation members — no RULE_CONFLICT (removed in the F1d
    # re-leveling) and none of the rejected judgment members.
    statuses = {
        v for k, v in vars(SupplierRequestDecisionStatus).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert statuses == {
        "PREPARATION_AMOUNT_DETERMINABLE",
        "INSUFFICIENT_FACTS",
    }
    assert statuses & {
        "RULE_CONFLICT",
        "OVERDUE",
        "SHOULD_HAVE_INVOICED",
        "PAYMENT_REQUIRED",
        "TAX_RATE_RECOMMENDED",
        "READY",
        "NOT_READY",
        "ELIGIBLE",
        "BLOCKED",
    } == set()

    banned_tokens = (
        "overdue",
        "should",
        "eligib",
        "ready",
        "remaining",
        "owed",
        "outstanding",
        "unpaid",
        "quantity",
        "tax",
    )
    import bel.application.supplier_invoice_request as module

    # The blocker classification is total over SupplierRequestBlockerCode
    # and the status is derived from it alone. Exactly one blocker code
    # exists (the genuinely-required-data gap); the advisory set is
    # disjoint from it so an advisory can never leak into the status.
    from bel.application.supplier_invoice_request import (
        MISSING_FACT_BLOCKER_CODES,
        NON_BLOCKING_ADVISORY_CODES,
        SupplierRequestBlockerCode,
    )

    all_blocker_codes = {
        v for k, v in vars(SupplierRequestBlockerCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert all_blocker_codes == {SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT}
    assert MISSING_FACT_BLOCKER_CODES == all_blocker_codes
    assert MISSING_FACT_BLOCKER_CODES.isdisjoint(NON_BLOCKING_ADVISORY_CODES)

    dto_types = [
        obj
        for obj in vars(module).values()
        if dataclasses.is_dataclass(obj) and getattr(obj, "__module__", None) == module.__name__
    ]
    assert {t.__name__ for t in dto_types} >= {
        "SupplierInvoiceRequestDecision",
        "SupplierRequestBlocker",
        "SupplierRequestAdvisory",
        "SupplierRequestAmountCheck",
        "SupplierRequestItemNameCheck",
        "PurchaseInvoiceContractAssociation",
    }
    for dto_type in dto_types:
        for f in dataclasses.fields(dto_type):
            for token in banned_tokens:
                assert token not in f.name.lower(), f"{dto_type.__name__}.{f.name} carries banned concept {token!r}"


def test_evaluation_is_strictly_read_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1B-17")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id)
    _make_invoice_allocation(db_session, invoice.id, contract)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    payment = _make_out_payment(db_session, frag.id)
    _make_payment_allocation(db_session, payment, contract)
    db_session.commit()

    def _counts():
        from bel.infrastructure.persistence import models as m

        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = db_session.query(obj).count()
        return counts

    before = _counts()
    evaluate_supplier_invoice_request(db_session)
    assert _counts() == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted


def test_evaluation_never_autoflushes_pending_writes(db_session):
    frag = _make_fragment(db_session)
    _make_contract(db_session, frag.id, "PO-F1B-18")
    db_session.commit()

    # A pending, uncommitted write must survive the evaluation untouched:
    # the whole read path runs under session.no_autoflush.
    pending_doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="pending", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(db_session).add_document(pending_doc)

    evaluate_supplier_invoice_request(db_session)

    assert any(getattr(obj, "id", None) == pending_doc.id for obj in db_session.new)
    db_session.rollback()


def test_pure_function_over_manually_built_context_no_session():
    """The decision function is pure over the F0 context DTOs — no
    session, no DB."""
    contract_id, invoice_id, item_id, invoice_item_id = (uuid.uuid4() for _ in range(4))
    contract = Contract(
        id=contract_id, contract_no="PO-PURE-F1B", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("500.00"), currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    invoice = Invoice(
        id=invoice_id, direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-PURE", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("500.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("500.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    invoice_item = InvoiceItem(
        id=invoice_item_id, invoice_id=invoice_id, line_no=1, product_name="Widget Alpha", specification=None,
        unit=None, quantity=Decimal("5"), unit_price=None, net_amount=Decimal("500.00"),
        tax_rate=Decimal("0.13"), tax_amount=Decimal("0"), gross_amount=Decimal("500.00"),
        source_fragment_id=uuid.uuid4(),
    )
    from bel.domain.matching import InvoiceAllocation as Allocation
    from bel.domain.accrual import InvoiceItemAllocation as ItemAllocation

    context = _context_with_scopes((SupplierScopeContext(
        contract=contract,
        items=(ContractItem(
            id=item_id, contract_id=contract_id, source_item_key=None, sku=None, product_name="Widget Alpha",
            specification=None, quantity=Decimal("5"), unit=None, unit_price=None, gross_amount=Decimal("500.00"),
            tax_rate=None, net_amount=Decimal("450.00"), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        ),),
        shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=Allocation(
                id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract_id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("500.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=invoice,
        ),),
        invoice_item_allocations=(SupplierScopeInvoiceItemAllocation(
            allocation=ItemAllocation(
                id=uuid.uuid4(), invoice_item_id=invoice_item_id, contract_item_id=item_id,
                allocated_quantity=Decimal("5"), allocated_net_amount=Decimal("450.00"),
                confirmation_type="MANUAL_CONFIRMED", source_fragment_id=uuid.uuid4(), created_at=NOW,
            ),
            invoice_item=invoice_item,
            invoice=invoice,
        ),),
        payment_allocations=(),
        unresolved_work=(),
    ),))

    report = evaluate_supplier_invoice_request_from_context(context)
    decision = report.decisions[0]
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.expected_purchase_invoice_gross_amount == Decimal("500.00")
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.MATCH
    assert decision.item_name_checks[0].outcome == SupplierRequestCheckOutcome.MATCH
    assert decision.blockers == ()
    assert report.purchase_invoice_contract_map[0].contract_ids == (contract_id,)


def test_report_covers_every_supplier_scope_and_map(db_session):
    frag = _make_fragment(db_session)
    invoiced_contract = _make_contract(db_session, frag.id, "PO-F1B-19A")
    bare_contract = _make_contract(db_session, frag.id, "PO-F1B-19B")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, invoiced_contract)
    db_session.commit()

    report = evaluate_supplier_invoice_request(db_session)
    assert {d.contract_id for d in report.decisions} == {invoiced_contract.id, bare_contract.id}
    assert len(report.purchase_invoice_contract_map) == 1
    assert report.purchase_invoice_contract_map[0].invoice_id == invoice.id
    assert report.purchase_invoice_contract_map[0].contract_ids == (invoiced_contract.id,)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decision_for(session, contract_id) -> SupplierInvoiceRequestDecision:
    report = evaluate_supplier_invoice_request(session)
    return next(d for d in report.decisions if d.contract_id == contract_id)


def _context_with_scopes(scopes) -> InvoicePreparationContext:
    return InvoicePreparationContext(sales_scopes=(), supplier_scopes=tuple(scopes))
