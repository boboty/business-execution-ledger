"""Phase 2D.3-F1d — advisory/blocker separation in SUPPLIER_INVOICE_REQUEST
(F1d PRE-GATE REPAIR re-leveling).

On top of the frozen supplier-direction rule layer (F1b, IP-P01..IP-P09),
the F1d outcome model separates two finding channels on every Decision:

- ``blockers`` — hard findings (genuinely-required data absent); the
  decision ``status`` is derived from these ALONE. Exactly one blocker
  code exists: ``MISSING_CONTRACT_GROSS_AMOUNT``.
- ``advisories`` — explicit NON-BLOCKING management reminders / review
  signals. A legitimate real-world business state is NEVER a conflict
  merely because it departs from the preferred management pattern:
    * ``SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED`` (IP-P09) — paid, no
      PURCHASE invoice yet (已付款，尚未收到对应进项发票，建议催供应商开票);
    * ``PURCHASE_INVOICE_AMOUNT_DEVIATION`` (IP-P02) — invoice amount
      deviates from the Contract reference;
    * ``PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION`` (IP-P05) — product
      name deviates from the contract product name;
    * ``MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT`` (IP-P03) — the split is
      legitimate business state;
    * ``PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS`` (IP-P04) — M:N is
      not a business error, never apportioned.

Every test here proves that an advisory NEVER affects ``status``: a
scope with advisories and no blockers is still
``PREPARATION_AMOUNT_DETERMINABLE``, an advisory coexisting with a
blocker leaves the blocker's status (``INSUFFICIENT_FACTS``) intact, and
a comparison that cannot be performed (missing product name / invoice
Fact) is a ``NOT_COMPARABLE_MISSING_FACT`` CHECK RESULT ONLY — it never
blocks preparation.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SupplierScopeContext,
    SupplierScopeInvoiceAllocation,
    SupplierScopeInvoiceItemAllocation,
    SupplierScopePaymentAllocation,
)
from bel.application.supplier_invoice_request import (
    MISSING_FACT_BLOCKER_CODES,
    NON_BLOCKING_ADVISORY_CODES,
    SupplierRequestAdvisoryCode,
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


# ---------------------------------------------------------------------------
# Helpers (independently synthetic)
# ---------------------------------------------------------------------------


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


def _make_contract_item(session, contract, fragment_id, product_name="Widget Alpha"):
    item = ContractItem(
        id=uuid.uuid4(),
        contract_id=contract.id,
        source_item_key="ITEM-1",
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


def _make_purchase_invoice(
    session,
    fragment_id,
    gross_amount=Decimal("1000.00"),
    product_name="Widget Alpha",
    tax_rate=Decimal("0.13"),
):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"PINV-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Supplier",
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
    invoice_item = InvoiceItem(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        line_no=1,
        product_name=product_name,
        specification=None,
        unit=None,
        quantity=Decimal("10"),
        unit_price=None,
        net_amount=gross_amount,
        tax_rate=tax_rate,
        tax_amount=Decimal("0"),
        gross_amount=gross_amount,
        source_fragment_id=fragment_id,
    )
    InvoiceItemRepository(session).add(invoice_item)
    session.flush()
    return invoice, invoice_item


def _make_invoice_allocation(session, invoice_id, contract):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type="INVOICE",
        subject_id=invoice_id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    allocation = InvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice_id,
        contract_id=contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=Decimal("1000.00"),
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


def _decision_for(session, contract_id):
    report = evaluate_supplier_invoice_request(session)
    return next(d for d in report.decisions if d.contract_id == contract_id)


def _context_with_scopes(scopes) -> InvoicePreparationContext:
    return InvoicePreparationContext(sales_scopes=(), supplier_scopes=tuple(scopes))


def _pure_contract(contract_no, gross_amount=Decimal("1000.00"), counterparty="Supplier"):
    return Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty=counterparty,
        buyer="Our Own Entity",
        gross_amount=gross_amount,
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(),
        created_at=NOW,
        updated_at=NOW,
    )


# ---------------------------------------------------------------------------
# IP-P09 — paid but no PURCHASE invoice is a management follow-up advisory
# ---------------------------------------------------------------------------


def test_paid_no_invoice_emits_follow_up_advisory_never_changes_status(db_session):
    """A scope carrying a confirmed OUT payment and NO associated PURCHASE
    invoice gains the SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED advisory
    (IP-P09), while the payment-less scope gains nothing. Both scopes
    keep the identical PREPARATION_AMOUNT_DETERMINABLE status and zero
    blockers: the advisory is a management reminder fully separated from
    blockers."""
    frag = _make_fragment(db_session)
    paid_contract = _make_contract(db_session, frag.id, "PO-F1D-01A", counterparty="Supplier One")
    bare_contract = _make_contract(db_session, frag.id, "PO-F1D-01B", counterparty="Supplier Two")
    payment = _make_out_payment(db_session, frag.id, amount=Decimal("500.00"))
    _make_payment_allocation(db_session, payment, paid_contract)
    db_session.commit()

    report = evaluate_supplier_invoice_request(db_session)
    decisions = {d.contract_id: d for d in report.decisions}
    paid, bare = decisions[paid_contract.id], decisions[bare_contract.id]

    # Both statuses identical and clean — payment never gates.
    assert paid.status == bare.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert paid.blockers == () and bare.blockers == ()

    # The separation: the paid scope carries the follow-up advisory, the
    # bare one does not; the payment Fact itself stays exposed as context.
    assert [a.code for a in paid.advisories] == [
        SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED
    ]
    assert "已付款" in paid.advisories[0].note and "催供应商开票" in paid.advisories[0].note
    assert len(paid.payment_allocations) == 1
    assert paid.payment_allocations[0].payment.direction == PaymentDirection.OUT
    assert bare.advisories == ()
    assert bare.payment_allocations == ()


def test_follow_up_disappears_when_invoice_associated(db_session):
    """The IP-P09 follow-up is recomputed from current Facts: once a
    PURCHASE invoice is associated, the advisory disappears on the same
    evaluation run — no Task is persisted, nothing lingers."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-01C")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract)
    payment = _make_out_payment(db_session, frag.id)
    _make_payment_allocation(db_session, payment, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.advisories == ()


# ---------------------------------------------------------------------------
# Incomplete-association boundaries — an allocation record is NOT a
# confirmed Fact (Codex Pre-Gate BLOCKER 1)
# ---------------------------------------------------------------------------


def test_payment_allocation_without_payment_fact_no_p09():
    """A payment ASSOCIATION whose Payment Fact is missing is NOT a
    confirmed OUT Payment Fact: even with no PURCHASE invoice, NO
    SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED advisory is emitted. The
    dangling allocation remains visible as factual context. (Pure
    function over the F0 context — dangling associations are unreachable
    in storage via the payment_allocations.payment_id FK.)"""
    contract = _pure_contract("PO-F1D-11")
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(),
        invoice_item_allocations=(),
        payment_allocations=(SupplierScopePaymentAllocation(
            allocation=PaymentAllocation(
                id=uuid.uuid4(), payment_id=uuid.uuid4(), contract_id=contract.id, match_case_id=uuid.uuid4(),
                allocated_amount=Decimal("500.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            payment=None,
        ),),
        unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.advisories == ()
    # The dangling association stays visible as factual context.
    assert len(decision.payment_allocations) == 1
    assert decision.payment_allocations[0].payment is None


def test_p09_emitted_when_invoice_fact_missing_but_confirmed_payment_exists():
    """An invoice ASSOCIATION whose Invoice Fact is missing does NOT count
    as "invoice already received": with a confirmed OUT Payment Fact and
    only a dangling invoice association, the
    SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED advisory IS emitted."""
    contract = _pure_contract("PO-F1D-12")
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2031, 1, 15), direction=PaymentDirection.OUT,
        amount=Decimal("1000.00"), counterparty="Supplier", business_type=None,
        bank_reference="REF-F1D-12", description=None, running_balance=None,
        source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    invoice_id = uuid.uuid4()
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("1000.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=None,
        ),),
        invoice_item_allocations=(),
        payment_allocations=(SupplierScopePaymentAllocation(
            allocation=PaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                allocated_amount=Decimal("1000.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            payment=payment,
        ),),
        unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED
    ]
    # The dangling invoice association surfaces only as a NOT_COMPARABLE
    # amount check — it never satisfies "invoice already received".
    assert len(decision.amount_checks) == 1
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT


def test_dangling_invoice_allocation_does_not_count_for_p03():
    """An invoice ASSOCIATION whose Invoice Fact is missing does NOT
    contribute to the IP-P03 multiple-invoice count: one confirmed PURCHASE
    invoice plus one dangling association yields NO
    MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT advisory — only the confirmed
    Fact is a PURCHASE invoice."""
    contract = _pure_contract("PO-F1D-13")
    confirmed_invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1D-13", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("1000.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("1000.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    dangling_invoice_id = uuid.uuid4()
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(
            SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=confirmed_invoice.id, contract_id=contract.id,
                    match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("1000.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=confirmed_invoice,
            ),
            SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=dangling_invoice_id, contract_id=contract.id,
                    match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("200.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=None,
            ),
        ),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_dangling_invoice_allocation_does_not_contribute_to_p04():
    """An invoice ASSOCIATION whose Invoice Fact is missing does NOT
    contribute to the IP-P04 spanning-contract check: two contracts
    sharing ONE dangling association (invoice=None) yield NO
    PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS advisory, and the
    confirmed report map is empty."""
    contract_a = _pure_contract("PO-F1D-14A")
    contract_b = _pure_contract("PO-F1D-14B")
    dangling_invoice_id = uuid.uuid4()

    def _scope(contract):
        return SupplierScopeContext(
            contract=contract, items=(), shipments=(),
            invoice_allocations=(SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=dangling_invoice_id, contract_id=contract.id,
                    match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("1000.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=None,
            ),),
            invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
        )

    report = evaluate_supplier_invoice_request_from_context(
        _context_with_scopes((_scope(contract_a), _scope(contract_b)))
    )
    # No confirmed PURCHASE invoice exists, so the confirmed map is empty.
    assert report.purchase_invoice_contract_map == ()
    for contract in (contract_a, contract_b):
        decision = next(d for d in report.decisions if d.contract_id == contract.id)
        assert decision.blockers == ()
        assert decision.advisories == ()
        assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_confirmed_invoices_still_drive_p03_mixed_with_dangling():
    """Confirmed PURCHASE invoices still drive IP-P03 when a dangling
    association is also present: two confirmed invoices plus one dangling
    association yield the MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT advisory
    naming ONLY the two confirmed invoice Facts — the confirmed-Fact
    filtering must not over-filter."""
    contract = _pure_contract("PO-F1D-15")

    def _confirmed_invoice(amount, key):
        return Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no=None, external_invoice_key=key, issue_date=date(2031, 1, 10),
            seller="Supplier", buyer="Our Own Entity", net_amount=amount, tax_amount=Decimal("0"),
            gross_amount=amount, invoice_status=None, source_fragment_id=uuid.uuid4(),
            created_at=NOW, updated_at=NOW,
        )

    invoice1 = _confirmed_invoice(Decimal("600.00"), "PINV-F1D-15A")
    invoice2 = _confirmed_invoice(Decimal("400.00"), "PINV-F1D-15B")
    dangling_invoice_id = uuid.uuid4()

    def _alloc(invoice, amount):
        return SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=invoice,
        )

    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(
            _alloc(invoice1, Decimal("600.00")),
            _alloc(invoice2, Decimal("400.00")),
            SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=dangling_invoice_id, contract_id=contract.id,
                    match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("100.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=None,
            ),
        ),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.blockers == ()
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT
    ]
    assert set(decision.advisories[0].related_invoice_ids) == {invoice1.id, invoice2.id}
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


# ---------------------------------------------------------------------------
# IP-P02 / IP-P05 — deviation advisories (management review, not conflict)
# ---------------------------------------------------------------------------


def test_amount_deviation_emits_advisory_and_preserves_invoice_fact(db_session):
    """IP-P02 deviation: the invoice amount differing from the Contract
    reference emits PURCHASE_INVOICE_AMOUNT_DEVIATION — a management
    review signal. The invoice Fact stays valid and exposed, the status
    stays PREPARATION_AMOUNT_DETERMINABLE, and no blocker is emitted."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-02", gross_amount=Decimal("1000.00"))
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("800.00"))
    _make_invoice_allocation(db_session, invoice.id, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION
    ]
    assert decision.advisories[0].related_invoice_ids == (invoice.id,)
    # The invoice Fact is preserved verbatim (never a conflict, never
    # apportioned, never deleted).
    assert decision.invoice_allocations[0].invoice.gross_amount == Decimal("800.00")


def test_product_name_deviation_emits_advisory(db_session):
    """IP-P05 deviation: an unequal confirmed product-name pair emits
    PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION naming the conflicting
    invoice and invoice item — a management review signal, never a
    conflict."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-03")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, product_name="Widget Beta")
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.item_name_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION
    ]
    assert decision.advisories[0].related_invoice_ids == (invoice.id,)
    assert decision.advisories[0].related_invoice_item_ids == (invoice_item.id,)


def test_missing_product_name_is_not_comparable_never_blocks(db_session):
    """A comparison impossible for an absent product name is a CHECK
    RESULT ONLY — NOT_COMPARABLE_MISSING_FACT with no blocker and no
    advisory: the optional management comparison is unavailable and does
    NOT make the Decision INSUFFICIENT_FACTS (Phase 2D.3-F1d)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-04")
    item = _make_contract_item(db_session, contract, frag.id, product_name="Widget Alpha")
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, product_name=None)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.item_name_checks[0].outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


# ---------------------------------------------------------------------------
# IP-P03 / IP-P04 — cardinality advisories (legitimate business state)
# ---------------------------------------------------------------------------


def test_multiple_invoices_advisory_only(db_session):
    """IP-P03: a Contract split across multiple PURCHASE invoices emits
    MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT — an advisory, never a
    violation, never a status change."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-05")
    invoice1, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("600.00"))
    invoice2, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("400.00"))
    _make_invoice_allocation(db_session, invoice1.id, contract)
    _make_invoice_allocation(db_session, invoice2.id, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT
    ]
    assert set(decision.advisories[0].related_invoice_ids) == {invoice1.id, invoice2.id}


def test_invoice_spans_contracts_advisory_no_apportionment(db_session):
    """IP-P04: one PURCHASE invoice on multiple Contracts emits
    PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS on every involved scope —
    the M:N relationship is not a business error, the invoice is never
    silently apportioned, and the status never changes."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "PO-F1D-06A")
    contract_b = _make_contract(db_session, frag.id, "PO-F1D-06B")
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("1000.00"))
    _make_invoice_allocation(db_session, invoice.id, contract_a)
    _make_invoice_allocation(db_session, invoice.id, contract_b)
    db_session.commit()

    report = evaluate_supplier_invoice_request(db_session)
    decisions = {d.contract_id: d for d in report.decisions}
    for contract in (contract_a, contract_b):
        decision = decisions[contract.id]
        assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
        assert decision.blockers == ()
        ip_p04 = [a for a in decision.advisories if a.code == SupplierRequestAdvisoryCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS]
        assert len(ip_p04) == 1
        assert ip_p04[0].related_invoice_ids == (invoice.id,)
        assert set(ip_p04[0].related_contract_ids) == {contract_a.id, contract_b.id}
        # Never silently apportioned: the full invoice Fact is exposed.
        assert decision.invoice_allocations[0].invoice.gross_amount == Decimal("1000.00")


# ---------------------------------------------------------------------------
# IP-P06 — existing tax_rate is CONTEXT, never an advisory
# ---------------------------------------------------------------------------


def test_existing_tax_rate_is_context_only_no_advisory(db_session):
    """An actual PURCHASE InvoiceItem's tax_rate is displayed as the
    existing Fact it is (IP-P06): it is reachable through
    invoice_item_allocations, the status stays PREPARATION_AMOUNT_DETERMINABLE,
    no blocker is emitted, and NO advisory is emitted for its presence
    (Phase 2D.3-F1d removed the old tax-rate advisory)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-07")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, tax_rate=Decimal("0.13"))
    _make_invoice_allocation(db_session, invoice.id, contract)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert decision.invoice_item_allocations[0].invoice_item.tax_rate == Decimal("0.13")


def test_no_tax_rate_fact_means_no_advisory(db_session):
    """An InvoiceItem with NO tax_rate Fact produces no finding at all —
    nothing is inferred and nothing is invented."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-08")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, tax_rate=None)
    _make_invoice_allocation(db_session, invoice.id, contract)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.advisories == ()
    assert decision.blockers == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_product_name_deviation_advisory_deduplicates_per_invoice():
    """Two item allocations naming the SAME InvoiceItem's invoice emit the
    product-name deviation advisory exactly once — the advisory is per
    invoice, not per association. (Pure function over the F0 context, no
    session.)"""
    contract = _pure_contract("PO-F1D-09")
    contract_item = ContractItem(
        id=uuid.uuid4(), contract_id=contract.id, source_item_key="ITEM-1", sku=None,
        product_name="Widget Alpha", specification=None, quantity=Decimal("10"), unit=None,
        unit_price=None, gross_amount=Decimal("500.00"), tax_rate=None, net_amount=Decimal("450.00"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1D-09", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("1000.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("1000.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    invoice_item_a = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget Beta", specification=None,
        unit=None, quantity=Decimal("10"), unit_price=None, net_amount=Decimal("1000.00"),
        tax_rate=Decimal("0.13"), tax_amount=Decimal("0"), gross_amount=Decimal("1000.00"),
        source_fragment_id=uuid.uuid4(),
    )
    invoice_item_b = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=2, product_name="Widget Beta", specification=None,
        unit=None, quantity=Decimal("5"), unit_price=None, net_amount=Decimal("500.00"),
        tax_rate=Decimal("0.13"), tax_amount=Decimal("0"), gross_amount=Decimal("500.00"),
        source_fragment_id=uuid.uuid4(),
    )

    def _item_alloc(invoice_item):
        return SupplierScopeInvoiceItemAllocation(
            allocation=InvoiceItemAllocation(
                id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=contract_item.id,
                allocated_quantity=Decimal("2"), allocated_net_amount=Decimal("200.00"),
                confirmation_type="MANUAL_CONFIRMED", source_fragment_id=uuid.uuid4(), created_at=NOW,
            ),
            invoice_item=invoice_item,
            invoice=invoice,
        )

    context = _context_with_scopes((SupplierScopeContext(
        contract=contract,
        items=(contract_item,),
        shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("1000.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=invoice,
        ),),
        invoice_item_allocations=(_item_alloc(invoice_item_a), _item_alloc(invoice_item_b)),
        payment_allocations=(),
        unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    deviation_advisories = [
        a for a in decision.advisories if a.code == SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION
    ]
    assert len(deviation_advisories) == 1
    assert deviation_advisories[0].related_invoice_ids == (invoice.id,)
    assert set(deviation_advisories[0].related_invoice_item_ids) == {invoice_item_a.id, invoice_item_b.id}
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


# ---------------------------------------------------------------------------
# Vocabulary — advisory codes are disjoint from blockers and non-judgmental
# ---------------------------------------------------------------------------


def test_advisory_vocabulary_is_disjoint_from_blockers_and_non_judgmental():
    """The advisory channel is fully separated from the blocker channel:
    the advisory-code set is exhaustive over its own vocabulary and
    disjoint from the blocker class, so an advisory can never leak into
    the status derivation. Advisory codes carry no business judgment —
    none of the rejected judgment concepts appear. The single sanctioned
    management recommendation is SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED
    (IP-P09); no other code may carry "recommend"."""
    all_advisory_codes = {
        v for k, v in vars(SupplierRequestAdvisoryCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert NON_BLOCKING_ADVISORY_CODES == all_advisory_codes
    assert NON_BLOCKING_ADVISORY_CODES.isdisjoint(MISSING_FACT_BLOCKER_CODES)

    for code in NON_BLOCKING_ADVISORY_CODES:
        lowered = code.lower()
        for token in ("overdue", "should", "eligib", "ready", "remaining", "owed", "outstanding", "unpaid", "must"):
            assert token not in lowered, f"advisory code {code} carries judgment token {token!r}"
    # The follow-up advisory is the ONE sanctioned recommendation code;
    # no other advisory code carries a recommend/inference concept.
    assert SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED in NON_BLOCKING_ADVISORY_CODES
    for code in NON_BLOCKING_ADVISORY_CODES - {SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED}:
        assert "recommend" not in code.lower(), f"advisory code {code} carries a recommendation outside IP-P09"


def test_advisory_presence_never_derives_status():
    """Status is a function of blockers alone: two clean scopes that
    differ only in advisory-bearing context (one has an amount deviation)
    are the same status, and the advisory-bearing scope carries no
    blocker whatsoever. (Pure function over the F0 context.)"""
    deviated_contract = _pure_contract("PO-F1D-10A", gross_amount=Decimal("1000.00"))
    clean_contract = _pure_contract("PO-F1D-10B", gross_amount=Decimal("1000.00"))
    invoice_id = uuid.uuid4()
    invoice = Invoice(
        id=invoice_id, direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1D-10A", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("800.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("800.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )

    def _scope(contract, with_invoice):
        return SupplierScopeContext(
            contract=contract, items=(), shipments=(),
            invoice_allocations=(SupplierScopeInvoiceAllocation(
                allocation=InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract.id, match_case_id=uuid.uuid4(),
                    allocated_gross_amount=Decimal("800.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                invoice=invoice if with_invoice else None,
            ),) if with_invoice else (),
            invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
        )

    context = _context_with_scopes((_scope(deviated_contract, with_invoice=True), _scope(clean_contract, with_invoice=False)))
    decisions = {d.contract_id: d for d in evaluate_supplier_invoice_request_from_context(context).decisions}
    deviated, clean = decisions[deviated_contract.id], decisions[clean_contract.id]
    assert deviated.status == clean.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert deviated.blockers == () and clean.blockers == ()
    assert [a.code for a in deviated.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION
    ]
    assert clean.advisories == ()
