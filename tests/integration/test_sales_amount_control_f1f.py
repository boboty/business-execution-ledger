"""Phase 2D.3-F1f — Sales amount control: the IP-S02 three-way
management comparison.

Implements the frozen IP-S02 comparison as a MANAGEMENT control (never a
workflow gate, never a RULE_CONFLICT):

    SalesContract gross amount
      vs export/customs declared amount (Shipment/Export Fact)
      vs confirmed SALES Invoice gross amount

Only the unambiguous 1:1:1 scope is compared:

- exactly ONE confirmed SALES Invoice Fact (a SalesInvoiceAllocation
  whose Invoice Fact is missing or not direction SALES is NOT a confirmed
  Invoice Fact — never a sum, never "newest", never "by amount");
- exactly ONE current ProcurementSalesLink AND exactly ONE current
  Shipment on that linked Contract for the declaration leg;
- amounts are compared ONLY when all three amounts AND all three
  currencies exist and the three currencies are explicitly equal — no FX,
  no default currency, no implicit same-currency assumption.

Outcomes (SalesAmountCheckOutcome): MATCH / DEVIATION /
NOT_COMPARABLE_MISSING_FACT / NOT_COMPARABLE_CURRENCY_MISMATCH /
NOT_COMPARABLE_AMBIGUOUS_SCOPE. A same-currency amount inequality emits
the NON-BLOCKING SALES_INVOICE_AMOUNT_DEVIATION advisory; an explicit
currency mismatch emits SALES_INVOICE_CURRENCY_DEVIATION (no amount
comparison, no FX). NOT_COMPARABLE_* are check results ONLY — they never
block invoice preparation, never change status, and receipt chronology
never affects the comparison.

Test 14 (F1d/F1e regression) is enforced primarily by the existing
supplier-side suites (test_supplier_invoice_request_advisories_f1d.py,
test_invoice_currency_f1e.py); this file adds one composition test
proving the sales comparison and the currency-safe supplier P02
comparison evaluate independently on the same fact set.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    SalesScopeInvoiceAllocation,
    SalesScopeLinkedProcurementContract,
    SupplierScopeContext,
)
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_invoice_preparation import (
    NON_BLOCKING_ADVISORY_CODES,
    SALES_AMOUNT_CONSISTENCY_CHECK_NAME,
    SalesAmountCheckOutcome,
    SalesInvoiceAdvisoryCode,
    SalesPreparationBlockerCode,
    SalesPreparationDecisionStatus,
    evaluate_sales_invoice_preparation,
    evaluate_sales_invoice_preparation_from_context,
)
from bel.application.shipment_facts import create_shipment_fact
from bel.application.supplier_invoice_request import (
    SupplierRequestAdvisoryCode,
    SupplierRequestCheckOutcome,
    SupplierRequestDecisionStatus,
    evaluate_supplier_invoice_request,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    SalesInvoiceAllocation,
    SalesPaymentAllocation,
    SubjectType,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
    PaymentRepository,
    SalesInvoiceAllocationRepository,
    SalesPaymentAllocationRepository,
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


def _make_contract(session, fragment_id, contract_no, *, gross_amount=Decimal("1000.00"), currency="CNY"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty="Supplier",
        buyer="Our Own Entity",
        gross_amount=gross_amount,
        currency=currency,
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(session, fragment_id, sales_contract_no, fields=None):
    return create_sales_contract_fact(
        session,
        our_entity="Our Own Entity",
        sales_contract_no=sales_contract_no,
        fields=fields or {},
        source_fragment_id=fragment_id,
        created_at=NOW,
    ).sales_contract


def _make_shipment(session, contract, fragment_id, external_reference, *, fields=None):
    result = create_shipment_fact(
        session,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=date(2031, 2, 1),
        fields=fields or {},
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    session.flush()
    return result.shipment


def _link(session, contract, sales_contract, fragment):
    return add_procurement_sales_link(
        session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=fragment.id,
        confirmation_type="AUTO_CONFIRMED",
        created_at=NOW,
    ).link


def _make_sales_invoice(session, fragment_id, *, gross_amount=Decimal("100.00"), currency="USD", issue_date=None):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"SINV-F1F-{uuid.uuid4().hex[:8]}",
        issue_date=issue_date or date(2031, 1, 10),
        seller="Our Own Entity",
        buyer="Customer",
        net_amount=gross_amount,
        tax_amount=Decimal("0"),
        gross_amount=gross_amount,
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
        currency=currency,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_sales_invoice_allocation(session, invoice, sales_contract, *, allocated=None):
    """Persist a confirmed SALES-invoice allocation through the
    authoritative repository path: a pending INVOICE MatchCase, then the
    allocation with confirmation_type HUMAN_CONFIRMED."""
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    allocation = SalesInvoiceAllocation(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        sales_contract_id=sales_contract.id,
        match_case_id=match_case.id,
        allocated_gross_amount=allocated if allocated is not None else invoice.gross_amount,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    SalesInvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _make_in_receipt(session, fragment_id, transaction_date, *, amount=Decimal("100.00")):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=transaction_date,
        direction=PaymentDirection.IN,
        amount=amount,
        counterparty="Customer",
        business_type=None,
        bank_reference=f"REF-F1F-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


def _make_in_receipt_allocation(session, payment, sales_contract, *, allocated=None):
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.PAYMENT,
        subject_id=payment.id,
        status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
        match_method=MatchMethod.MANUAL_SALES_SCOPE,
        created_at=NOW,
        resolved_at=None,
    )
    MatchCaseRepository(session).add(match_case)
    session.flush()
    allocation = SalesPaymentAllocation(
        id=uuid.uuid4(),
        payment_id=payment.id,
        sales_contract_id=sales_contract.id,
        match_case_id=match_case.id,
        allocated_amount=allocated if allocated is not None else payment.amount,
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED,
        created_at=NOW,
    )
    SalesPaymentAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _decision_for(session, sales_contract_id):
    report = evaluate_sales_invoice_preparation(session)
    return next(d for d in report.decisions if d.sales_contract_id == sales_contract_id)


def _context_with_scopes(sales_scopes, supplier_scopes) -> InvoicePreparationContext:
    return InvoicePreparationContext(sales_scopes=tuple(sales_scopes), supplier_scopes=tuple(supplier_scopes))


# ---------------------------------------------------------------------------
# The unambiguous 1:1:1 scope — MATCH / DEVIATION / currency-mismatch
# ---------------------------------------------------------------------------


def test_1_match_same_explicit_currency_same_amount_no_advisory(db_session):
    """SalesContract 100 USD == Shipment declared 100 USD == SALES
    Invoice 100 USD -> MATCH, zero advisories, zero blockers, no status
    change."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-1")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-1",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-1",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check is not None
    assert check.outcome == SalesAmountCheckOutcome.MATCH
    assert check.check_name == SALES_AMOUNT_CONSISTENCY_CHECK_NAME
    assert check.sales_contract_amount == Decimal("100.00")
    assert check.sales_contract_currency == "USD"
    assert check.declared_amount == Decimal("100.00")
    assert check.declared_currency == "USD"
    assert check.sales_invoice_amount == Decimal("100.00")
    assert check.sales_invoice_currency == "USD"


def test_2_amount_deviation_same_currency_advisory_only(db_session):
    """Same scope, same explicit currency, one amount differs -> DEVIATION
    + SALES_INVOICE_AMOUNT_DEVIATION advisory; never a blocker, never a
    RULE_CONFLICT, status unchanged."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-2")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-2",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-2",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    # The SALES Invoice deviates from the contract/declaration reference.
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("90.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract, allocated=Decimal("90.00"))
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.DEVIATION
    assert [a.code for a in decision.advisories] == [
        SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION
    ]
    # The deviation is a management review signal — the Invoice Fact stays
    # valid and nothing is a rule conflict (the decision has no conflict
    # vocabulary at all).
    assert decision.amount_check.sales_invoice_amount == Decimal("90.00")


def test_3_currency_mismatch_no_fx_no_amount_deviation(db_session):
    """Explicit currencies not all equal -> NOT_COMPARABLE_CURRENCY_MISMATCH
    + SALES_INVOICE_CURRENCY_DEVIATION; no amount comparison, no FX, no
    amount deviation advisory — even when the amounts are numerically
    equal."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-3")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-3",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    # Declaration in EUR, invoice in USD — the amount comparison is
    # refused (no FX), even though both amounts are numerically 100.
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-3",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "EUR"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.blockers == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH
    assert check.outcome != SalesAmountCheckOutcome.MATCH
    assert check.sales_contract_currency == "USD"
    assert check.declared_currency == "EUR"
    assert check.sales_invoice_currency == "USD"
    codes = [a.code for a in decision.advisories]
    assert codes == [SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION]
    assert SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION not in codes


# ---------------------------------------------------------------------------
# Missing compared Facts — NOT_COMPARABLE_MISSING_FACT, never a blocker
# ---------------------------------------------------------------------------


def test_4_invoice_currency_missing_is_not_comparable_no_implicit_currency(db_session):
    """Invoice.currency is None -> NOT_COMPARABLE_MISSING_FACT; no implicit
    currency is invented, no blocker, no status change."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-4")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-4",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-4",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency=None)
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.sales_invoice_currency is None
    assert check.sales_contract_currency == "USD"
    assert check.declared_currency == "USD"


def test_5_shipment_declared_currency_missing_no_default(db_session):
    """Shipment.declared_currency is None -> NOT_COMPARABLE_MISSING_FACT;
    no default currency is manufactured for the declaration leg."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-5")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-5",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    # Declaration amount known WITHOUT its currency — a representable
    # incomplete Fact, never defaulted.
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-5",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00")},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.declared_currency is None
    assert check.declared_amount == Decimal("100.00")


def test_6_sales_contract_currency_missing_is_not_comparable(db_session):
    """SalesContract.currency is None -> NOT_COMPARABLE_MISSING_FACT; the
    SalesContract gross amount without its currency is an incomplete Fact,
    never compared under an implicit currency."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-6")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-6",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00")},  # no currency
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-6",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.sales_contract_currency is None
    assert check.sales_contract_amount == Decimal("100.00")


def test_7_no_confirmed_sales_invoice_fact_is_not_comparable(db_session):
    """No SALES-invoice association at all -> NOT_COMPARABLE_MISSING_FACT
    — a check result only, never "may not issue invoice"."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-7")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-7",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-7",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.sales_invoice_id is None
    assert check.sales_invoice_amount is None
    # The declaration leg is still exposed inspectably.
    assert check.declared_amount == Decimal("100.00")


def test_8_allocation_without_invoice_fact_is_not_a_confirmed_invoice_fact():
    """A SalesInvoiceAllocation whose Invoice Fact is missing (invoice is
    None) is NOT a confirmed SALES Invoice Fact: with a fully-resolvable
    declaration leg and only a dangling allocation, the outcome is still
    NOT_COMPARABLE_MISSING_FACT — the allocation is context only, never
    promoted into Invoice Fact semantics (Codex Pre-Gate F1d BLOCKER 1
    applied on the sales side)."""
    sc_id, contract_id, shipment_id, dangling_invoice_id = (uuid.uuid4() for _ in range(4))
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-F1F-8",
        customer="Customer F1F", currency="USD", gross_amount=Decimal("100.00"),
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    contract = Contract(
        id=contract_id, contract_no="PO-F1F-8", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("1000.00"), currency="USD", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    link = _pure_link(contract_id, sc_id)
    shipment = Shipment(
        id=shipment_id, contract_id=contract_id, external_reference="SHIP-F1F-8",
        execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        declared_amount=Decimal("100.00"), declared_currency="USD",
    )
    context = _context_with_scopes(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(
                    SalesScopeLinkedProcurementContract(link=link, contract=contract),
                ),
                invoice_allocations=(
                    SalesScopeInvoiceAllocation(
                        allocation=SalesInvoiceAllocation(
                            id=uuid.uuid4(), invoice_id=dangling_invoice_id, sales_contract_id=sc_id,
                            match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("100.00"),
                            confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
                        ),
                        invoice=None,  # the Invoice Fact is missing
                    ),
                ),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract, items=(), shipments=(shipment,),
                invoice_allocations=(), invoice_item_allocations=(),
                payment_allocations=(), unresolved_work=(),
            ),
        ),
    )

    decision = evaluate_sales_invoice_preparation_from_context(context).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.sales_invoice_id is None
    # The declaration leg IS resolved — the invoice leg is what is missing.
    assert check.shipment_id == shipment_id
    assert check.declared_amount == Decimal("100.00")


# ---------------------------------------------------------------------------
# Cardinality ambiguity — NOT_COMPARABLE_AMBIGUOUS_SCOPE, no sum / choice
# ---------------------------------------------------------------------------


def test_9_multiple_confirmed_sales_invoices_ambiguous_scope(db_session):
    """More than one confirmed SALES Invoice Fact -> NOT_COMPARABLE_
    AMBIGUOUS_SCOPE: no invoice is summed, no newest/first/by-amount
    choice is made, and no blocker is emitted."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-9")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-9",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-9",
        fields={"quantity": Decimal("10"), "declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice_a = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("60.00"), currency="USD")
    invoice_b = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("40.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice_a, sales_contract, allocated=Decimal("60.00"))
    _make_sales_invoice_allocation(db_session, invoice_b, sales_contract, allocated=Decimal("40.00"))
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE
    # No arbitrary selection, no sum: no invoice candidate is chosen and
    # no invoice amount is presented.
    assert check.sales_invoice_id is None
    assert check.sales_invoice_amount is None


def test_10_multiple_current_links_ambiguous_scope_no_blocker(db_session):
    """Multiple current ProcurementSalesLinks -> NOT_COMPARABLE_
    AMBIGUOUS_SCOPE: no link/shipment is chosen, no blocker is emitted,
    and invoice preparation stays open (INPUTS_PRESENT)."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "PO-F1F-10A")
    contract_b = _make_contract(db_session, frag.id, "PO-F1F-10B")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-10",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract_a, sales_contract, frag)
    _link(db_session, contract_b, sales_contract, frag)
    # Both linked contracts have shipments — even so, no single declaration
    # anchor is chosen (IP-S04 / F1f: never any-vs-all, never a guess).
    _make_shipment(
        db_session, contract_a, frag.id, "SHIP-F1F-10A",
        fields={"declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    _make_shipment(
        db_session, contract_b, frag.id, "SHIP-F1F-10B",
        fields={"declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE
    assert check.shipment_id is None
    assert check.declared_amount is None
    # The confirmed invoice IS resolvable, but the declaration scope is
    # ambiguous — cardinality ambiguity takes precedence over comparing
    # against a chosen declaration.
    assert check.sales_invoice_id == invoice.id


def test_11_one_link_multiple_shipments_ambiguous_scope_no_sum(db_session):
    """One current link but MULTIPLE current Shipment/Export Facts on the
    linked Contract -> NOT_COMPARABLE_AMBIGUOUS_SCOPE: declaration amounts
    are never summed and no shipment is chosen."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-11")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-11",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-11A",
        fields={"declared_amount": Decimal("60.00"), "declared_currency": "USD"},
    )
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-11B",
        fields={"declared_amount": Decimal("40.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE
    # No sum (60+40=100 would look like a MATCH — never inferred), no
    # arbitrary choice, no shipment candidate presented.
    assert check.shipment_id is None
    assert check.declared_amount is None
    assert check.declared_currency is None


def test_12_no_shipment_fact_is_not_comparable_no_workflow_gate(db_session):
    """One current link but NO Shipment Fact on the linked Contract ->
    NOT_COMPARABLE_MISSING_FACT; a missing Shipment is NOT "may not issue
    invoice" — the scope stays INPUTS_PRESENT with zero blockers."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-12")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-12",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.shipment_id is None
    # The confirmed invoice is resolvable and exposed.
    assert check.sales_invoice_id == invoice.id


# ---------------------------------------------------------------------------
# Receipt chronology — IP-S03: ordering never affects the comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoice_date,receipt_date",
    [
        (date(2031, 1, 10), date(2031, 1, 20)),  # Case A: SALES invoice before IN receipt
        (date(2031, 1, 20), date(2031, 1, 10)),  # Case B: IN receipt before SALES invoice
    ],
)
def test_13_receipt_chronology_never_changes_ip_s02_comparison(db_session, invoice_date, receipt_date):
    """The IP-S02 comparison semantics are IDENTICAL for invoice-before-
    receipt and receipt-before-invoice: the receipt Fact (and its
    allocation) is never consulted by the comparison, no chronology
    finding exists, and no blocker/status difference is caused by the
    ordering."""
    assert (invoice_date < receipt_date) or (receipt_date < invoice_date)
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-13")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-13",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-13",
        fields={"declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD", issue_date=invoice_date)
    _make_sales_invoice_allocation(db_session, invoice, sales_contract)
    receipt = _make_in_receipt(db_session, frag.id, transaction_date=receipt_date)
    _make_in_receipt_allocation(db_session, receipt, sales_contract)
    db_session.commit()

    decision = _decision_for(db_session, sales_contract.id)
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.MATCH
    assert check.sales_invoice_amount == Decimal("100.00")
    # No chronology finding is emitted anywhere on the decision.
    assert decision.consistency_checks == ()


# ---------------------------------------------------------------------------
# F1d/F1e regression — sales and supplier rule layers compose independently
# ---------------------------------------------------------------------------


def test_14_sales_comparison_and_currency_safe_supplier_p02_compose(db_session):
    """F1d/F1e regression on one fact set: a MATCHing sales comparison and
    a currency-safe supplier P02 comparison evaluate independently — the
    sales decision carries its IP-S02 result, the supplier decision carries
    its own amount check, and neither leaks into the other. The supplier
    scope demonstrates the F1e currency-safe path (same explicit currency,
    exact match)."""
    frag = _make_fragment(db_session)
    # One procurement Contract shared by BOTH sides of the bridge.
    contract = _make_contract(db_session, frag.id, "PO-F1F-14", gross_amount=Decimal("100.00"), currency="USD")
    sales_contract = _make_sales_contract(
        db_session, frag.id, "SC-F1F-14",
        fields={"customer": "Customer F1F", "gross_amount": Decimal("100.00"), "currency": "USD"},
    )
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(
        db_session, contract, frag.id, "SHIP-F1F-14",
        fields={"declared_amount": Decimal("100.00"), "declared_currency": "USD"},
    )
    sales_invoice = _make_sales_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_sales_invoice_allocation(db_session, sales_invoice, sales_contract)
    # Supplier side: a PURCHASE invoice on the same procurement Contract
    # matching its gross amount in the same explicit currency.
    purchase_invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"PINV-F1F-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Supplier",
        buyer="Our Own Entity",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
        currency="USD",
    )
    InvoiceRepository(db_session).add(purchase_invoice)
    db_session.flush()
    purchase_match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=purchase_invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(purchase_match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(),
            invoice_id=purchase_invoice.id,
            contract_id=contract.id,
            match_case_id=purchase_match_case.id,
            allocated_gross_amount=Decimal("100.00"),
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    )
    db_session.commit()

    sales_decision = _decision_for(db_session, sales_contract.id)
    supplier_report = evaluate_supplier_invoice_request(db_session)
    supplier_decision = next(d for d in supplier_report.decisions if d.contract_id == contract.id)

    # Sales side: the IP-S02 three-way comparison is MATCH, no advisory.
    assert sales_decision.amount_check.outcome == SalesAmountCheckOutcome.MATCH
    assert sales_decision.advisories == ()
    assert sales_decision.blockers == ()

    # Supplier side (F1d/F1e): P02 currency-safe MATCH on the same
    # explicit currency, no advisory, status determinable.
    assert supplier_decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert supplier_decision.blockers == ()
    assert supplier_decision.advisories == ()
    assert len(supplier_decision.amount_checks) == 1
    assert supplier_decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.MATCH
    assert supplier_decision.amount_checks[0].compared_invoice_currency == "USD"
    assert supplier_decision.amount_checks[0].contract_currency == "USD"


def test_14b_supplier_currency_safe_p02_deviation_still_green(db_session):
    """F1e regression mirrored on the F1f fact set: the supplier P02
    amount deviation advisory (same explicit currency, unequal amounts) is
    emitted exactly as frozen, independent of any sales comparison."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1F-14B", gross_amount=Decimal("100.00"), currency="USD")
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"PINV-F1F-{uuid.uuid4().hex[:8]}",
        issue_date=date(2031, 1, 10),
        seller="Supplier",
        buyer="Our Own Entity",
        net_amount=Decimal("90.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("90.00"),
        invoice_status=None,
        source_fragment_id=frag.id,
        created_at=NOW,
        updated_at=NOW,
        currency="USD",
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(),
        subject_type=SubjectType.INVOICE,
        subject_id=invoice.id,
        status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001,
        created_at=NOW,
        resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            contract_id=contract.id,
            match_case_id=match_case.id,
            allocated_gross_amount=Decimal("90.00"),
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED,
            created_at=NOW,
        )
    )
    db_session.commit()

    supplier_decision = next(
        d for d in evaluate_supplier_invoice_request(db_session).decisions if d.contract_id == contract.id
    )
    assert supplier_decision.blockers == ()
    assert supplier_decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert supplier_decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert [a.code for a in supplier_decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION
    ]


# ---------------------------------------------------------------------------
# DTO contract — the check is always emitted and exposes its scope
# ---------------------------------------------------------------------------


def test_amount_check_is_always_present_and_scoped(db_session):
    """Every sales scope gets exactly one IP-S02 check (the SalesContract
    always exists), and the check's outcome is a member of the frozen
    outcome vocabulary."""
    frag = _make_fragment(db_session)
    _make_sales_contract(db_session, frag.id, "SC-F1F-ALWAYS")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.amount_check is not None
    assert decision.amount_check.outcome in {
        SalesAmountCheckOutcome.MATCH,
        SalesAmountCheckOutcome.DEVIATION,
        SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
        SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH,
        SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE,
    }


def test_advisory_vocabulary_is_disjoint_and_non_blocking():
    """The sales advisory-code set is exhaustive over
    SalesInvoiceAdvisoryCode and never overlaps the (empty) blocker class:
    an advisory can never leak into a status/blocker channel, and the two
    deviation advisories are the only members."""
    all_advisory_codes = {
        v for k, v in vars(SalesInvoiceAdvisoryCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert NON_BLOCKING_ADVISORY_CODES == all_advisory_codes
    # The sales blocker class is empty — nothing is EVER a blocker from
    # this rule layer, so every advisory is trivially disjoint from it.
    blocker_codes = {
        v for k, v in vars(SalesPreparationBlockerCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert blocker_codes == set()
    assert NON_BLOCKING_ADVISORY_CODES.isdisjoint(blocker_codes)
    assert SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION in NON_BLOCKING_ADVISORY_CODES
    assert SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION in NON_BLOCKING_ADVISORY_CODES


def _pure_link(contract_id, sales_contract_id):
    from bel.domain.procurement_sales_link import ProcurementSalesLink

    return ProcurementSalesLink(
        id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
        source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
    )
