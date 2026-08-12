"""Unit tests for the shared accrual semantics (sections 8-9): the
balance is always DERIVED from the original Accrual minus its reversals,
status follows the single get_projected_accrual_status rule, and
is_open_accrual is the one predicate R001/R002/R003 all share.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from bel.domain.accrual import (
    Accrual,
    AccrualReversal,
    AccrualStatus,
    get_accrual_balance,
    get_projected_accrual_status,
    is_open_accrual,
)

NOW = datetime.now(timezone.utc)


def _accrual(quantity="100", cost="1200.00"):
    return Accrual(
        id=uuid.uuid4(),
        period="2031-02",
        contract_item_id=uuid.uuid4(),
        quantity=Decimal(quantity),
        estimated_cost=Decimal(cost),
        basis="MANUAL_CONFIRMED",
        status=AccrualStatus.ACTIVE,
        created_from_fact_id=uuid.uuid4(),
        created_at=NOW,
    )


def _reversal(accrual, quantity, cost):
    return AccrualReversal(
        id=uuid.uuid4(),
        accrual_id=accrual.id,
        period="2031-03",
        invoice_item_allocation_id=uuid.uuid4(),
        reversed_quantity=Decimal(quantity),
        reversed_estimated_cost=Decimal(cost),
        created_at=NOW,
    )


def test_remaining_quantity_and_amount_are_derived():
    accrual = _accrual()
    reversals = [_reversal(accrual, "35", "420.00")]
    remaining_qty, remaining_cost, reversed_qty, reversed_cost = get_accrual_balance(accrual, reversals)
    assert remaining_qty == Decimal("65")
    assert remaining_cost == Decimal("780.00")
    assert reversed_qty == Decimal("35")
    assert reversed_cost == Decimal("420.00")


def test_zero_reversals_means_full_original_balance():
    accrual = _accrual("40", "880.00")
    remaining_qty, remaining_cost, reversed_qty, reversed_cost = get_accrual_balance(accrual, [])
    assert remaining_qty == Decimal("40")
    assert remaining_cost == Decimal("880.00")
    assert reversed_qty == Decimal("0")
    assert reversed_cost == Decimal("0.00")


def test_open_accrual_predicate_positive():
    accrual = _accrual()
    assert is_open_accrual(accrual, []) is True
    assert is_open_accrual(accrual, [_reversal(accrual, "35", "420.00")]) is True


def test_open_accrual_predicate_negative_when_fully_reversed():
    accrual = _accrual()
    assert is_open_accrual(accrual, [_reversal(accrual, "100", "1200.00")]) is False


def test_status_partial_and_full():
    accrual = _accrual()
    partial = [_reversal(accrual, "35", "420.00")]
    remaining_qty, _, reversed_qty, _ = get_accrual_balance(accrual, partial)
    assert get_projected_accrual_status(reversed_qty, remaining_qty) == AccrualStatus.PARTIALLY_REVERSED

    full = [_reversal(accrual, "100", "1200.00")]
    remaining_qty, _, reversed_qty, _ = get_accrual_balance(accrual, full)
    assert get_projected_accrual_status(reversed_qty, remaining_qty) == AccrualStatus.REVERSED


def test_status_active_when_zero_reversals():
    accrual = _accrual()
    remaining_qty, _, reversed_qty, _ = get_accrual_balance(accrual, [])
    assert get_projected_accrual_status(reversed_qty, remaining_qty) == AccrualStatus.ACTIVE


def test_partially_reversed_accrual_keeps_participating():
    """The confirmed Phase 0 principle: a PARTIALLY_REVERSED accrual with
    remaining balance > 0 is still open — it must keep blocking a
    duplicate accrual (R003) and keep matching later invoices (R001)."""
    accrual = _accrual()
    reversals = [_reversal(accrual, "65", "780.00")]
    assert is_open_accrual(accrual, reversals) is True
    remaining_qty, remaining_cost, _, _ = get_accrual_balance(accrual, reversals)
    assert remaining_qty == Decimal("35")
    assert remaining_cost == Decimal("420.00")


def test_remaining_is_never_a_separately_mutable_truth():
    """The remaining balance must be original minus sum(reversals). A
    caller cannot fabricate a remaining amount out of thin air."""
    accrual = _accrual("100", "500.00")
    reversals = [_reversal(accrual, "40", "200.00")]
    remaining_qty, remaining_cost, _, _ = get_accrual_balance(accrual, reversals)
    assert remaining_qty == Decimal("60")
    assert remaining_cost == Decimal("300.00")
