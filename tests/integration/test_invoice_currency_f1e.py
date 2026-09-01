"""Phase 2D.3-F1e — Canonical Invoice currency + currency-safe purchase
amount comparison.

Closes the last canonical currency gap on the Invoice: ``Invoice.currency``
is the currency EXPLICITLY stated by the Invoice Evidence/source — never
defaulted (no CNY/USD), never inferred (not from buyer/seller/country,
not from Contract/SalesContract currency, not from an amount), never
FX-converted, and NOT part of Invoice identity (``external_invoice_key``,
Invoice identity and matching identity are unchanged; existing rows stay
valid with ``currency = None``).

On top of that Fact, IP-P02's amount comparison becomes currency-safe:
the preparation reference is ``Contract.gross_amount`` +
``Contract.currency`` (``expected_purchase_invoice_currency``), and the
amount is compared ONLY when both currencies are explicit and exactly
comparable:

- MATCH — same explicit currency, exact gross amount equality;
- DEVIATION (amount) — same explicit currency, inequality ->
  ``PURCHASE_INVOICE_AMOUNT_DEVIATION`` ADVISORY;
- NOT_COMPARABLE_CURRENCY_MISMATCH — both explicit but different ->
  ``PURCHASE_INVOICE_CURRENCY_DEVIATION`` ADVISORY (no amount comparison);
- NOT_COMPARABLE_MISSING_FACT — any required amount/currency Fact absent
  (including a missing ``Invoice.currency``): a CHECK RESULT ONLY, never
  a blocker — preparation stays determinable from
  ``Contract.gross_amount`` + ``Contract.currency``.

Every finding is ADVISORY / a check result — never a ``RULE_CONFLICT``,
never a status change (F1d semantics preserved).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SupplierScopeContext,
    SupplierScopeInvoiceAllocation,
)
from bel.application.import_invoices import import_invoices
from bel.application.supplier_invoice_request import (
    SupplierRequestAdvisoryCode,
    SupplierRequestCheckOutcome,
    SupplierRequestDecisionStatus,
    evaluate_supplier_invoice_request,
    evaluate_supplier_invoice_request_from_context,
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
)
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
)
from fixtures.synthetic.phase2b_close import PHASE2B_INVOICE_ROWS

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


def _make_contract(
    session, fragment_id, contract_no, *, gross_amount=Decimal("100.00"), currency="USD"
):
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


def _make_purchase_invoice(session, fragment_id, *, gross_amount=Decimal("100.00"), currency="USD"):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.PURCHASE,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"PINV-F1E-{uuid.uuid4().hex[:8]}",
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
        currency=currency,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_invoice_allocation(session, invoice_id, contract, allocated=Decimal("100.00")):
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
        allocated_gross_amount=allocated,
        match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
        confirmation_type=ConfirmationType.AUTO_CONFIRMED,
        created_at=NOW,
    )
    InvoiceAllocationRepository(session).add(allocation)
    session.flush()
    return allocation


def _decision_for(session, contract_id):
    report = evaluate_supplier_invoice_request(session)
    return next(d for d in report.decisions if d.contract_id == contract_id)


def _context_with_scopes(scopes) -> InvoicePreparationContext:
    return InvoicePreparationContext(sales_scopes=(), supplier_scopes=tuple(scopes))


# ---------------------------------------------------------------------------
# Invoice canonical field — explicit currency, None, no default
# ---------------------------------------------------------------------------


def test_invoice_currency_can_carry_explicit_value():
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1E-CUR", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW, currency="USD",
    )
    assert invoice.currency == "USD"


def test_invoice_currency_may_be_none():
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1E-NOCUR", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    assert invoice.currency is None


def test_invoice_currency_has_no_default():
    """The canonical currency is Evidence-derived only: a freshly built
    Invoice (the shape every pre-F1e constructor uses) carries
    ``currency is None`` — never a manufactured CNY/USD."""
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1E-DEF", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW,
    )
    assert invoice.currency is None
    assert invoice.currency not in ("CNY", "USD")


def test_invoice_identity_fields_unchanged_by_currency():
    """``currency`` is NOT an identity field: external_invoice_key and
    the identity-bearing fields are exactly what the constructor was
    given, and a currency value or absence changes none of them."""
    key = "PINV-F1E-IDENT"
    a = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key=key, issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW, currency="USD",
    )
    b = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key=key, issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW, currency=None,
    )
    assert a.external_invoice_key == b.external_invoice_key == key
    assert (a.invoice_no, a.digital_invoice_no, a.issue_date, a.seller, a.buyer) == (
        b.invoice_no, b.digital_invoice_no, b.issue_date, b.seller, b.buyer,
    )


# ---------------------------------------------------------------------------
# Persistence — explicit currency and NULL round-trip (SQLite via repository)
# ---------------------------------------------------------------------------


def test_currency_round_trip_explicit(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_purchase_invoice(db_session, frag.id, currency="USD")
    db_session.commit()

    reloaded = InvoiceRepository(db_session).get(invoice.id)
    assert reloaded is not None
    assert reloaded.currency == "USD"
    assert reloaded.gross_amount == Decimal("100.00")


def test_currency_round_trip_null(db_session):
    frag = _make_fragment(db_session)
    invoice = _make_purchase_invoice(db_session, frag.id, currency=None)
    db_session.commit()

    reloaded = InvoiceRepository(db_session).get(invoice.id)
    assert reloaded is not None
    assert reloaded.currency is None
    # The pre-F1e row shape (no explicit currency) is unchanged otherwise.
    assert reloaded.external_invoice_key == invoice.external_invoice_key


# ---------------------------------------------------------------------------
# IP-P02 — currency-safe amount comparison (scenarios A-F)
# ---------------------------------------------------------------------------


def test_scenario_a_match_same_currency_exact_amount(db_session):
    """A. Contract 100 USD vs Invoice 100 USD -> MATCH, and no
    amount/currency deviation advisory."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-A", gross_amount=Decimal("100.00"), currency="USD")
    invoice = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="USD")
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("100.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.advisories == ()
    assert len(decision.amount_checks) == 1
    check = decision.amount_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.MATCH
    assert check.compared_invoice_currency == "USD"
    assert check.contract_currency == "USD"
    assert check.compared_invoice_gross_amount == Decimal("100.00")
    assert check.contract_gross_amount == Decimal("100.00")


def test_scenario_b_amount_deviation_same_currency(db_session):
    """B. 100 USD vs 90 USD -> DEVIATION (amount) ->
    PURCHASE_INVOICE_AMOUNT_DEVIATION; preparation remains determinable;
    never a RULE_CONFLICT."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-B", gross_amount=Decimal("100.00"), currency="USD")
    invoice = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("90.00"), currency="USD")
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("90.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.expected_purchase_invoice_gross_amount == Decimal("100.00")
    assert decision.expected_purchase_invoice_currency == "USD"
    assert decision.amount_checks[0].outcome == SupplierRequestCheckOutcome.DEVIATION
    assert decision.amount_checks[0].compared_invoice_currency == "USD"
    assert decision.amount_checks[0].contract_currency == "USD"
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION
    ]


def test_scenario_c_currency_mismatch_no_amount_comparison(db_session):
    """C. 100 USD vs 100 EUR -> NOT_COMPARABLE_CURRENCY_MISMATCH ->
    PURCHASE_INVOICE_CURRENCY_DEVIATION; no amount mismatch, no amount
    deviation advisory, never a RULE_CONFLICT, still determinable."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-C", gross_amount=Decimal("100.00"), currency="USD")
    invoice = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency="EUR")
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("100.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    check = decision.amount_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH
    assert check.compared_invoice_currency == "EUR"
    assert check.contract_currency == "USD"
    # No amount deviation is implied by the currency mismatch.
    assert check.compared_invoice_gross_amount == Decimal("100.00")
    assert check.contract_gross_amount == Decimal("100.00")
    codes = [a.code for a in decision.advisories]
    assert codes == [SupplierRequestAdvisoryCode.PURCHASE_INVOICE_CURRENCY_DEVIATION]
    assert SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION not in codes


def test_scenario_d_invoice_currency_missing_is_not_comparable(db_session):
    """D. Contract currency known, Invoice currency None ->
    NOT_COMPARABLE_MISSING_FACT; no implicit currency is invented; the
    preparation reference stays determinable (status unchanged)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-D", gross_amount=Decimal("100.00"), currency="USD")
    invoice = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("100.00"), currency=None)
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("100.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.status == SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE
    assert decision.blockers == ()
    assert decision.advisories == ()
    check = decision.amount_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.compared_invoice_currency is None
    assert check.contract_currency == "USD"
    # Preparation determinable from the Contract Facts alone.
    assert decision.expected_purchase_invoice_gross_amount == Decimal("100.00")
    assert decision.expected_purchase_invoice_currency == "USD"


def test_scenario_e_contract_currency_missing_is_not_comparable():
    """E. Invoice currency known, Contract currency missing ->
    NOT_COMPARABLE_MISSING_FACT; no implicit currency. (Pure function —
    the assembled Contract's currency is required in storage, but the
    rule stays deterministic for the missing-Fact shape.)"""
    contract_id, invoice_id = uuid.uuid4(), uuid.uuid4()
    contract = Contract(
        id=contract_id, contract_no="PO-F1E-E", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency=None,
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    invoice = Invoice(
        id=invoice_id, direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="PINV-F1E-E", issue_date=date(2031, 1, 10),
        seller="Supplier", buyer="Our Own Entity", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW, currency="USD",
    )
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(SupplierScopeInvoiceAllocation(
            allocation=InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice_id, contract_id=contract_id, match_case_id=uuid.uuid4(),
                allocated_gross_amount=Decimal("100.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            ),
            invoice=invoice,
        ),),
        invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    check = decision.amount_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.compared_invoice_currency == "USD"
    assert check.contract_currency is None
    # No implicit currency, no amount deviation, no advisory.
    assert decision.advisories == ()
    assert decision.blockers == ()


def test_scenario_f_no_fx_conversion_never_match(db_session):
    """F. No FX conversion: a USD amount vs a CNY amount of a numerically
    different value must never be converted, never declared MATCH — the
    outcome is the explicit-currency-mismatch check result, and an amount
    that happens to be numerically equal is still not compared."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-F", gross_amount=Decimal("100.00"), currency="USD")
    invoice = _make_purchase_invoice(db_session, frag.id, gross_amount=Decimal("700.00"), currency="CNY")
    _make_invoice_allocation(db_session, invoice.id, contract, allocated=Decimal("700.00"))
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    check = decision.amount_checks[0]
    assert check.outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH
    assert check.outcome != SupplierRequestCheckOutcome.MATCH
    assert [a.code for a in decision.advisories] == [
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_CURRENCY_DEVIATION
    ]


# ---------------------------------------------------------------------------
# IP-P02 — Decision DTO exposes the explicit monetary scope
# ---------------------------------------------------------------------------


def test_decision_exposes_expected_currency(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1E-DTO", gross_amount=Decimal("500.00"), currency="EUR")
    db_session.commit()

    decision = _decision_for(db_session, contract.id)
    assert decision.expected_purchase_invoice_gross_amount == Decimal("500.00")
    assert decision.expected_purchase_invoice_currency == "EUR"


def test_decision_expected_currency_none_when_amount_missing():
    """The expected currency pair moves together: when the Contract
    amount is unknown (the sole blocker), no reference currency is
    presented either."""
    contract = Contract(
        id=uuid.uuid4(), contract_no="PO-F1E-DTO-BLOCK", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=None, currency="CNY",
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    context = _context_with_scopes((SupplierScopeContext(
        contract=contract, items=(), shipments=(),
        invoice_allocations=(), invoice_item_allocations=(), payment_allocations=(), unresolved_work=(),
    ),))

    decision = evaluate_supplier_invoice_request_from_context(context).decisions[0]
    assert decision.expected_purchase_invoice_gross_amount is None
    assert decision.expected_purchase_invoice_currency is None
    assert decision.status == SupplierRequestDecisionStatus.INSUFFICIENT_FACTS
    assert len(decision.blockers) == 1


# ---------------------------------------------------------------------------
# Intake boundary — the current purchase invoice Excel source has no
# explicit currency -> imported Invoice.currency is None, no CNY default
# ---------------------------------------------------------------------------


def test_scenario_g_purchase_excel_import_has_no_currency(db_session, invoice_workbook_factory):
    """G. The current purchase invoice Excel source provides NO canonical
    currency field, so every imported Invoice carries ``currency is None``
    — no CNY default is manufactured merely because the invoice is
    domestic."""
    path = invoice_workbook_factory(PHASE2B_INVOICE_ROWS)
    result = import_invoices(db_session, path, InvoiceDirection.PURCHASE)
    assert result.invoices_created == len(PHASE2B_INVOICE_ROWS)

    invoices = InvoiceRepository(db_session).list_all()
    assert len(invoices) == len(PHASE2B_INVOICE_ROWS)
    assert all(inv.currency is None for inv in invoices)
