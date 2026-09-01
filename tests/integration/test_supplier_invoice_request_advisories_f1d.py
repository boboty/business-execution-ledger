"""Phase 2D.3-F1d — advisory/blocker separation in SUPPLIER_INVOICE_REQUEST.

On top of the frozen supplier-direction rule layer (F1b, IP-P01..IP-P07),
the F1d outcome model separates two finding channels on every Decision:

- ``blockers`` — hard findings (rule conflicts / missing compared
  Facts); the decision ``status`` is derived from these ALONE.
- ``advisories`` — explicit NON-BLOCKING findings recording a frozen
  accountant-confirmed rule consequence that is factual context and
  never a gate:
    * ``OUT_PAYMENT_PRESENT_CONTEXT_ONLY`` (IP-P01) — the scope carries
      OUT payment Facts; payment is context, never a gate;
    * ``EXISTING_INVOICE_ITEM_TAX_RATE_FACT`` (IP-P06) — an actual
      PURCHASE InvoiceItem's ``tax_rate`` is reachable as an existing
      Fact; displayed as it is, with no inference or recommendation.

Every test here proves that an advisory NEVER affects ``status``: a
scope with advisories and no blockers is still
``PREPARATION_AMOUNT_DETERMINABLE``, and an advisory coexisting with a
blocker leaves the blocker's status (e.g. ``RULE_CONFLICT``) intact.
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
    RULE_VIOLATION_BLOCKER_CODES,
    SupplierRequestAdvisoryCode,
    SupplierRequestBlockerCode,
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
# IP-P01 — OUT payment is a non-blocking advisory (advisory/blocker separation)
# ---------------------------------------------------------------------------


def test_out_payment_present_emits_context_advisory_but_never_changes_status(db_session):
    """A scope carrying OUT payment Facts gains an
    OUT_PAYMENT_PRESENT_CONTEXT_ONLY advisory (IP-P01) — while the
    payment-less scope gains nothing. Both scopes keep the identical
    PREPARATION_AMOUNT_DETERMINABLE status and zero blockers: the
    advisory is a finding channel fully separated from blockers."""
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

    # The separation: the paid scope carries the advisory, the bare one
    # does not; the payment Fact itself stays exposed as context too.
    assert [a.code for a in paid.advisories] == [
        SupplierRequestAdvisoryCode.OUT_PAYMENT_PRESENT_CONTEXT_ONLY
    ]
    assert len(paid.payment_allocations) == 1
    assert paid.payment_allocations[0].payment.direction == PaymentDirection.OUT
    assert bare.advisories == ()
    assert bare.payment_allocations == ()


def test_advisory_never_masks_a_rule_conflict(db_session):
    """An advisory coexisting with a blocker leaves the blocker's status
    intact: IP-P02 amount MISMATCH + an OUT payment on the same scope
    yields BOTH the PURCHASE_INVOICE_AMOUNT_MISMATCH blocker AND the
    OUT_PAYMENT_PRESENT_CONTEXT_ONLY advisory, with status
    RULE_CONFLICT — the advisory never softens or masks the conflict."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-02", gross_amount=Decimal("1000.00"))
    invoice, _ = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("800.00"))
    _make_invoice_allocation(db_session, invoice.id, contract)
    payment = _make_out_payment(db_session, frag.id)
    _make_payment_allocation(db_session, payment, contract)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.RULE_CONFLICT
    assert [b.code for b in decision.blockers] == [
        SupplierRequestBlockerCode.PURCHASE_INVOICE_AMOUNT_MISMATCH
    ]
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.OUT_PAYMENT_PRESENT_CONTEXT_ONLY
    ]


# ---------------------------------------------------------------------------
# IP-P06 — existing InvoiceItem tax_rate is a non-blocking advisory
# ---------------------------------------------------------------------------


def test_existing_tax_rate_fact_emits_factual_advisory(db_session):
    """An actual PURCHASE InvoiceItem's tax_rate is displayed as the
    existing Fact it is (IP-P06): the decision gains an
    EXISTING_INVOICE_ITEM_TAX_RATE_FACT advisory naming that InvoiceItem,
    the status stays PREPARATION_AMOUNT_DETERMINABLE, and no blocker is
    emitted — a Fact display is never a rule conflict and never an
    inference."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-03")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, tax_rate=Decimal("0.13"))
    _make_invoice_allocation(db_session, invoice.id, contract)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    codes = [a.code for a in decision.advisories]
    assert SupplierRequestAdvisoryCode.EXISTING_INVOICE_ITEM_TAX_RATE_FACT in codes
    tax_advisory = next(
        a for a in decision.advisories if a.code == SupplierRequestAdvisoryCode.EXISTING_INVOICE_ITEM_TAX_RATE_FACT
    )
    assert tax_advisory.related_invoice_item_ids == (invoice_item.id,)
    # The advisory explicitly negates inference and recommendation — it
    # states the Fact display, never what rate to use. It also carries no
    # Decimal field at all, so no tax-rate VALUE can leak into it.
    note_lower = tax_advisory.note.lower()
    assert "no inference" in note_lower and "no recommendation" in note_lower
    assert "should" not in note_lower
    assert not any(isinstance(getattr(tax_advisory, f.name), Decimal) for f in dataclasses.fields(tax_advisory))


def test_no_tax_rate_fact_means_no_advisory(db_session):
    """An InvoiceItem with NO tax_rate Fact produces no tax advisory at
    all — nothing is inferred and nothing is invented."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1D-04")
    item = _make_contract_item(db_session, contract, frag.id)
    invoice, invoice_item = _make_purchase_invoice(db_session, frag.id, tax_rate=None)
    _make_invoice_allocation(db_session, invoice.id, contract)
    _make_invoice_item_allocation(db_session, invoice_item, item)
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert [a.code for a in decision.advisories] == []
    assert decision.blockers == ()
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


def test_tax_rate_advisory_deduplicates_per_invoice_item():
    """Two item allocations naming the SAME InvoiceItem emit the tax-rate
    advisory exactly once — the advisory is per Fact, not per
    association. (Pure function over the F0 context, no session.)"""
    contract = _pure_contract("PO-F1D-05")
    contract_item = ContractItem(
        id=uuid.uuid4(), contract_id=contract.id, source_item_key="ITEM-1", sku=None,
        product_name="Widget Alpha", specification=None, quantity=Decimal("10"), unit=None,
        unit_price=None, gross_amount=Decimal("500.00"), tax_rate=None, net_amount=Decimal("450.00"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1D-05", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("1000.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("1000.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    invoice_item = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget Alpha", specification=None,
        unit=None, quantity=Decimal("10"), unit_price=None, net_amount=Decimal("1000.00"),
        tax_rate=Decimal("0.13"), tax_amount=Decimal("0"), gross_amount=Decimal("1000.00"),
        source_fragment_id=uuid.uuid4(),
    )

    def _item_alloc():
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
        invoice_item_allocations=(_item_alloc(), _item_alloc()),
        payment_allocations=(),
        unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    tax_advisories = [
        a for a in decision.advisories if a.code == SupplierRequestAdvisoryCode.EXISTING_INVOICE_ITEM_TAX_RATE_FACT
    ]
    assert len(tax_advisories) == 1
    assert tax_advisories[0].related_invoice_item_ids == (invoice_item.id,)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE


# ---------------------------------------------------------------------------
# Vocabulary — advisory codes are disjoint from blockers and non-judgmental
# ---------------------------------------------------------------------------


def test_advisory_vocabulary_is_disjoint_from_blockers_and_non_judgmental():
    """The advisory channel is fully separated from the blocker channel:
    the advisory-code set is exhaustive over its own vocabulary and
    disjoint from BOTH blocker classes, so an advisory can never leak
    into the status precedence. Advisory codes carry no business
    judgment — none of the rejected judgment concepts appear."""
    all_advisory_codes = {
        v for k, v in vars(SupplierRequestAdvisoryCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert NON_BLOCKING_ADVISORY_CODES == all_advisory_codes
    assert NON_BLOCKING_ADVISORY_CODES.isdisjoint(RULE_VIOLATION_BLOCKER_CODES)
    assert NON_BLOCKING_ADVISORY_CODES.isdisjoint(MISSING_FACT_BLOCKER_CODES)

    for code in NON_BLOCKING_ADVISORY_CODES:
        lowered = code.lower()
        for token in ("overdue", "should", "recommend", "eligib", "ready", "remaining", "owed", "outstanding", "unpaid"):
            assert token not in lowered, f"advisory code {code} carries judgment token {token!r}"


def test_advisory_presence_never_derives_status():
    """Status is a function of blockers alone: two clean scopes that
    differ only in advisory-bearing context (one has an OUT payment) are
    the same status, and the advisory-bearing scope carries no blocker
    whatsoever. (Pure function over the F0 context.)"""
    paid_contract = _pure_contract("PO-F1D-06A", counterparty="Supplier One")
    bare_contract = _pure_contract("PO-F1D-06B", counterparty="Supplier Two")
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2031, 1, 15), direction=PaymentDirection.OUT,
        amount=Decimal("500.00"), counterparty="Supplier One", business_type=None,
        bank_reference="REF-F1D-06", description=None, running_balance=None,
        source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    context = _context_with_scopes((
        SupplierScopeContext(
            contract=paid_contract, items=(), shipments=(),
            invoice_allocations=(), invoice_item_allocations=(),
            payment_allocations=(SupplierScopePaymentAllocation(
                allocation=PaymentAllocation(
                    id=uuid.uuid4(), payment_id=payment.id, contract_id=paid_contract.id,
                    match_case_id=uuid.uuid4(), allocated_amount=Decimal("500.00"),
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
                ),
                payment=payment,
            ),),
            unresolved_work=(),
        ),
        SupplierScopeContext(
            contract=bare_contract, items=(), shipments=(),
            invoice_allocations=(), invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
        ),
    ))

    decisions = {d.contract_id: d for d in evaluate_supplier_invoice_request_from_context(context).decisions}
    paid, bare = decisions[paid_contract.id], decisions[bare_contract.id]
    assert paid.status == bare.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert paid.blockers == () and bare.blockers == ()
    assert [a.code for a in paid.advisories] == [SupplierRequestAdvisoryCode.OUT_PAYMENT_PRESENT_CONTEXT_ONLY]
    assert bare.advisories == ()
