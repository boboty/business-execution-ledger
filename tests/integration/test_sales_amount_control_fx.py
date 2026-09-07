"""Core Completion (IP-S02) — the FX-based sales amount comparison
(``amount_check``) and the actual SALES invoice quantity comparison
(``invoice_quantity_check``).

    expected_sales_invoice_cny = sales_contract_usd_amount x applicable_fx_rate

The applicable rate is the latest confirmed SAFE USD/CNY rate whose
publication date is on or before the explicit ``invoice_month``'s
natural first day. Core never fetches a rate and never maintains a
holiday calendar — every scenario here supplies (or deliberately omits)
confirmed ``ApplicableFxRateEvidence`` directly to the PURE evaluation
layer (no session boundary — that boundary's provenance binding is
covered separately by ``test_fx_rate_evidence.py``).

INDEPENDENT of ``customs_check`` (covered by
``test_sales_amount_control_f1f.py``) — a customs deviation must never
affect this comparison and vice versa.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.fx_rate_evidence import ApplicableFxRateEvidence
from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    SalesScopeInvoiceAllocation,
)
from bel.application.sales_invoice_preparation import (
    SalesAmountCheckOutcome,
    SalesInvoiceAdvisoryCode,
    evaluate_sales_invoice_preparation_from_context,
)
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import ConfirmationType, SalesInvoiceAllocation
from bel.domain.sales_contract import SalesContract

NOW = datetime.now(timezone.utc)


def _sales_contract(*, quantity=None, unit=None, gross_amount=Decimal("125.00"), currency="USD"):
    return SalesContract(
        id=uuid.uuid4(), our_entity="Our Own Entity", sales_contract_no="SC-FX",
        customer="Customer FX", currency=currency, gross_amount=gross_amount,
        contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        quantity=quantity, unit=unit,
    )


def _invoice(*, gross_amount, currency, direction=InvoiceDirection.SALES):
    return Invoice(
        id=uuid.uuid4(), direction=direction, invoice_type=None, invoice_no=None, digital_invoice_no=None,
        external_invoice_key=f"SINV-FX-{uuid.uuid4().hex[:8]}", issue_date=date(2030, 9, 5),
        seller="Our Own Entity", buyer="Customer", net_amount=gross_amount, tax_amount=Decimal("0"),
        gross_amount=gross_amount, invoice_status=None, source_fragment_id=uuid.uuid4(),
        created_at=NOW, updated_at=NOW, currency=currency,
    )


def _invoice_item(*, invoice_id, quantity, unit, gross_amount=Decimal("0")):
    return InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice_id, line_no=1, product_name="Widget", specification=None,
        unit=unit, quantity=quantity, unit_price=None, net_amount=gross_amount, tax_rate=None,
        tax_amount=Decimal("0"), gross_amount=gross_amount, source_fragment_id=uuid.uuid4(),
    )


def _allocation(invoice_id, sales_contract_id):
    return SalesInvoiceAllocation(
        id=uuid.uuid4(), invoice_id=invoice_id, sales_contract_id=sales_contract_id,
        match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("0"),
        confirmation_type=ConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )


def _scope(sales_contract, *, invoice=None, invoice_items=()):
    invoice_allocations = ()
    if invoice is not None:
        invoice_allocations = (
            SalesScopeInvoiceAllocation(
                allocation=_allocation(invoice.id, sales_contract.id), invoice=invoice,
                invoice_items=tuple(invoice_items),
            ),
        )
    return SalesScopeContext(
        sales_contract=sales_contract, linked_procurement_contracts=(), invoice_allocations=invoice_allocations,
        payment_allocations=(), unresolved_work=(),
    )


def _decision(sales_contract, *, invoice_month=None, fx_rate_evidence=(), invoice=None, invoice_items=()):
    context = InvoicePreparationContext(
        sales_scopes=(_scope(sales_contract, invoice=invoice, invoice_items=invoice_items),),
        supplier_scopes=(), invoice_month=invoice_month, fx_rate_evidence=fx_rate_evidence,
    )
    return evaluate_sales_invoice_preparation_from_context(context).decisions[0]


def _fx(source="SAFE", pub_date=date(2030, 8, 30), rate=Decimal("7.2000")):
    return ApplicableFxRateEvidence(
        source=source, publication_date=pub_date, usd_cny_rate=rate, provenance_fragment_id=uuid.uuid4()
    )


# ---------------------------------------------------------------------------
# FX selection
# ---------------------------------------------------------------------------


def test_fx_missing_invoice_month():
    sc = _sales_contract()
    decision = _decision(sc, invoice_month=None, fx_rate_evidence=(_fx(),))
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING
    assert decision.amount_check.applicable_fx_rate is None


def test_fx_no_valid_rate_at_all():
    sc = _sales_contract()
    decision = _decision(sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=())
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING


def test_fx_rate_on_the_months_natural_first_day_is_applicable():
    sc = _sales_contract(gross_amount=Decimal("100.00"))
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(pub_date=date(2030, 9, 1), rate=Decimal("7.5")),),
    )
    assert decision.amount_check.applicable_fx_rate == Decimal("7.5")
    assert decision.amount_check.applicable_fx_date == date(2030, 9, 1)
    assert decision.amount_check.expected_invoice_cny == Decimal("750.00")


def test_fx_no_publication_on_month_start_selects_latest_prior():
    sc = _sales_contract(gross_amount=Decimal("100.00"))
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(_fx(pub_date=date(2030, 8, 30), rate=Decimal("7.2")),),
    )
    assert decision.amount_check.applicable_fx_rate == Decimal("7.2")
    assert decision.amount_check.applicable_fx_date == date(2030, 8, 30)


def test_fx_publication_after_month_start_is_never_applicable():
    """A rate published AFTER the month's natural first day must never be
    selected — even if it's the only rate available."""
    sc = _sales_contract()
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(_fx(pub_date=date(2030, 9, 2), rate=Decimal("7.9")),),
    )
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING


def test_fx_multiple_older_records_selects_the_latest_not_the_first():
    sc = _sales_contract(gross_amount=Decimal("100.00"))
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(
            _fx(pub_date=date(2030, 6, 1), rate=Decimal("7.0")),
            _fx(pub_date=date(2030, 8, 30), rate=Decimal("7.2")),
            _fx(pub_date=date(2030, 7, 15), rate=Decimal("7.1")),
        ),
    )
    assert decision.amount_check.applicable_fx_rate == Decimal("7.2")
    assert decision.amount_check.applicable_fx_date == date(2030, 8, 30)


def test_fx_conflicting_ambiguous_rates_on_the_same_applicable_date():
    sc = _sales_contract()
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(
            _fx(pub_date=date(2030, 8, 30), rate=Decimal("7.2")),
            _fx(pub_date=date(2030, 8, 30), rate=Decimal("7.3")),
        ),
    )
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_AMBIGUOUS


def test_fx_invalid_source_is_never_applicable():
    """Only SAFE-sourced rates are ever applicable — a differently-sourced
    rate is simply not a candidate, never a fallback."""
    sc = _sales_contract()
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(_fx(source="SOME_OTHER_SOURCE", pub_date=date(2030, 8, 30)),),
    )
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("-7.2"), Decimal("NaN"), Decimal("Infinity")])
def test_fx_non_positive_or_unrepresentable_rate_is_never_applicable(rate):
    sc = _sales_contract()
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(pub_date=date(2030, 8, 30), rate=rate),),
    )
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_FX_MISSING


def test_fx_repeated_evaluation_with_identical_inputs_is_deterministic():
    sc = _sales_contract(gross_amount=Decimal("100.00"))
    fx = (_fx(pub_date=date(2030, 8, 30), rate=Decimal("7.2")),)
    context = InvoicePreparationContext(
        sales_scopes=(_scope(sc),), supplier_scopes=(), invoice_month=date(2030, 9, 1), fx_rate_evidence=fx,
    )
    first = evaluate_sales_invoice_preparation_from_context(context)
    second = evaluate_sales_invoice_preparation_from_context(context)
    assert first == second


def test_fx_sales_contract_currency_not_usd_is_never_converted():
    sc = _sales_contract(currency="CNY", gross_amount=Decimal("100.00"))
    decision = _decision(sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(),))
    assert decision.amount_check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert decision.amount_check.applicable_fx_rate is None


# ---------------------------------------------------------------------------
# amount_check — actual CNY invoice vs FX-derived expectation
# ---------------------------------------------------------------------------


def test_actual_cny_invoice_matches_the_fx_derived_expectation():
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    invoice = _invoice(gross_amount=Decimal("900.00"), currency="CNY")
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(rate=Decimal("7.2")),), invoice=invoice,
    )
    check = decision.amount_check
    assert check.expected_invoice_cny == Decimal("900.00")
    assert check.outcome == SalesAmountCheckOutcome.MATCH
    assert decision.advisories == ()


def test_actual_cny_invoice_deviates_from_the_fx_derived_expectation():
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    invoice = _invoice(gross_amount=Decimal("850.00"), currency="CNY")
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(rate=Decimal("7.2")),), invoice=invoice,
    )
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.DEVIATION
    assert [a.code for a in decision.advisories] == [SalesInvoiceAdvisoryCode.SALES_INVOICE_AMOUNT_DEVIATION]


def test_actual_invoice_non_cny_is_never_implicitly_converted():
    """The actual invoice being USD (or any non-CNY currency) is never
    silently FX-converted for the comparison — explicit CURRENCY_MISMATCH
    with its own advisory, no amount comparison attempted."""
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    invoice = _invoice(gross_amount=Decimal("125.00"), currency="USD")
    decision = _decision(
        sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(rate=Decimal("7.2")),), invoice=invoice,
    )
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH
    assert [a.code for a in decision.advisories] == [SalesInvoiceAdvisoryCode.SALES_INVOICE_CURRENCY_DEVIATION]


def test_multiple_sales_invoices_are_never_summed_or_apportioned():
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    invoice_a = _invoice(gross_amount=Decimal("500.00"), currency="CNY")
    invoice_b = _invoice(gross_amount=Decimal("400.00"), currency="CNY")
    scope = SalesScopeContext(
        sales_contract=sc, linked_procurement_contracts=(),
        invoice_allocations=(
            SalesScopeInvoiceAllocation(allocation=_allocation(invoice_a.id, sc.id), invoice=invoice_a),
            SalesScopeInvoiceAllocation(allocation=_allocation(invoice_b.id, sc.id), invoice=invoice_b),
        ),
        payment_allocations=(), unresolved_work=(),
    )
    context = InvoicePreparationContext(
        sales_scopes=(scope,), supplier_scopes=(), invoice_month=date(2030, 9, 1),
        fx_rate_evidence=(_fx(rate=Decimal("7.2")),),
    )
    decision = evaluate_sales_invoice_preparation_from_context(context).decisions[0]
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_AMBIGUOUS_SCOPE
    assert check.sales_invoice_amount is None
    # 500 + 400 = 900 would look like a MATCH against the FX expectation —
    # never inferred.
    assert check.expected_invoice_cny == Decimal("900.00")


def test_missing_confirmed_invoice_is_not_comparable_but_fx_still_resolves():
    """Missing customs/invoice data never prevents a known USD contract
    and applicable rate from producing the expected CNY amount."""
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    decision = _decision(sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(rate=Decimal("7.2")),))
    check = decision.amount_check
    assert check.outcome == SalesAmountCheckOutcome.NOT_COMPARABLE_MISSING_FACT
    assert check.expected_invoice_cny == Decimal("900.00")


def test_note_data_carries_the_actual_usd_amount_and_rate():
    sc = _sales_contract(gross_amount=Decimal("125.00"))
    decision = _decision(sc, invoice_month=date(2030, 9, 1), fx_rate_evidence=(_fx(rate=Decimal("7.2")),))
    assert decision.invoice_note_data is not None
    assert decision.invoice_note_data.contract_usd_amount == Decimal("125.00")
    assert decision.invoice_note_data.applicable_fx_rate == Decimal("7.2")
    assert decision.invoice_note_data.expected_invoice_cny == Decimal("900.00")


# ---------------------------------------------------------------------------
# invoice_quantity_check — actual SALES invoice item quantity vs
# SalesContract.quantity (never Shipment/procurement quantity)
# ---------------------------------------------------------------------------


def test_invoice_quantity_matches_sales_contract_quantity():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item = _invoice_item(invoice_id=invoice.id, quantity=Decimal("9"), unit="piece")
    decision = _decision(sc, invoice=invoice, invoice_items=(item,))
    check = decision.invoice_quantity_check
    assert check.outcome == "MATCH"
    assert check.expected_quantity == Decimal("9")
    assert check.actual_quantity == Decimal("9")
    # Expected quantity on the decision is unaffected either way.
    assert decision.expected_quantity == Decimal("9")


def test_invoice_quantity_deviates_from_sales_contract_quantity():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item = _invoice_item(invoice_id=invoice.id, quantity=Decimal("7"), unit="piece")
    decision = _decision(sc, invoice=invoice, invoice_items=(item,))
    check = decision.invoice_quantity_check
    assert check.outcome == "DEVIATION"
    assert [a.code for a in decision.advisories] == [SalesInvoiceAdvisoryCode.SALES_INVOICE_QUANTITY_DEVIATION]
    # Expected quantity is unaffected by the deviation.
    assert decision.expected_quantity == Decimal("9")


def test_invoice_quantity_unit_mismatch_is_never_converted():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item = _invoice_item(invoice_id=invoice.id, quantity=Decimal("9"), unit="box")
    decision = _decision(sc, invoice=invoice, invoice_items=(item,))
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_UNIT_MISMATCH"
    assert decision.advisories == ()


def test_invoice_quantity_missing_sales_contract_quantity_is_not_comparable():
    sc = _sales_contract(quantity=None, unit=None)
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item = _invoice_item(invoice_id=invoice.id, quantity=Decimal("9"), unit="piece")
    decision = _decision(sc, invoice=invoice, invoice_items=(item,))
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_MISSING_FACT"


def test_invoice_quantity_no_items_on_confirmed_invoice_is_not_comparable():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    decision = _decision(sc, invoice=invoice, invoice_items=())
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_MISSING_FACT"


def test_invoice_quantity_multiple_items_are_never_aggregated():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item_a = _invoice_item(invoice_id=invoice.id, quantity=Decimal("4"), unit="piece")
    item_b = _invoice_item(invoice_id=invoice.id, quantity=Decimal("5"), unit="piece")
    decision = _decision(sc, invoice=invoice, invoice_items=(item_a, item_b))
    # 4 + 5 = 9 would look like a MATCH — never inferred.
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_AMBIGUOUS_SCOPE"
    assert decision.invoice_quantity_check.actual_quantity is None


def test_invoice_quantity_no_confirmed_invoice_is_not_comparable():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    decision = _decision(sc)
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_MISSING_FACT"


def test_invoice_quantity_multiple_confirmed_invoices_ambiguous():
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice_a = _invoice(gross_amount=Decimal("0"), currency="CNY")
    invoice_b = _invoice(gross_amount=Decimal("0"), currency="CNY")
    scope = SalesScopeContext(
        sales_contract=sc, linked_procurement_contracts=(),
        invoice_allocations=(
            SalesScopeInvoiceAllocation(allocation=_allocation(invoice_a.id, sc.id), invoice=invoice_a),
            SalesScopeInvoiceAllocation(allocation=_allocation(invoice_b.id, sc.id), invoice=invoice_b),
        ),
        payment_allocations=(), unresolved_work=(),
    )
    context = InvoicePreparationContext(sales_scopes=(scope,), supplier_scopes=())
    decision = evaluate_sales_invoice_preparation_from_context(context).decisions[0]
    assert decision.invoice_quantity_check.outcome == "NOT_COMPARABLE_AMBIGUOUS_SCOPE"


def test_invoice_quantity_never_substituted_by_shipment_or_procurement_quantity():
    """Even when a Shipment-vs-contract quantity_check independently
    exists, invoice_quantity_check never reads from it — the two checks
    are fully independent, each with its own comparison basis."""
    sc = _sales_contract(quantity=Decimal("9"), unit="piece")
    invoice = _invoice(gross_amount=Decimal("0"), currency="CNY")
    item = _invoice_item(invoice_id=invoice.id, quantity=Decimal("9"), unit="piece")
    decision = _decision(sc, invoice=invoice, invoice_items=(item,))
    # No linked procurement contract/shipment at all in this scope — the
    # Shipment-side quantity_check is NOT_COMPARABLE, but invoice_quantity_check
    # still resolves correctly from the invoice item alone.
    assert decision.quantity_check.outcome in {"NOT_COMPARABLE_MISSING_FACT", "NOT_COMPARABLE_AMBIGUOUS_SCOPE"}
    assert decision.invoice_quantity_check.outcome == "MATCH"
