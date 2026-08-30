"""Phase 2B accrual / period-close domain objects.

Phase 2B deliberately uses explicit domain objects, one per fact type —
no Generic Fact Framework, no ``GenericFact(type, payload_json)``. See
docs/PHASE2B-DECISIONS.md.

The amount language below is canonical business vocabulary per A04
(AccrualRequired / cost_amount / contract_item / period) — never finance
or tax vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class AccrualStatus:
    ACTIVE = "ACTIVE"
    PARTIALLY_REVERSED = "PARTIALLY_REVERSED"
    REVERSED = "REVERSED"


class CostRecognitionBasis:
    """Why a contract reached cost-recognition condition. Phase 2B does
    NOT decide which business behavior means cost recognition — the
    Fact Pack states it explicitly and the Rule Engine only consumes the
    fact. See docs/PHASE2B-DECISIONS.md."""

    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"
    SALES_EXECUTION_CONFIRMED = "SALES_EXECUTION_CONFIRMED"
    EXPORT_EXECUTION_CONFIRMED = "EXPORT_EXECUTION_CONFIRMED"


class AccrualBasisScopeType:
    CONTRACT = "CONTRACT"
    CONTRACT_ITEM = "CONTRACT_ITEM"


class ManualBasis:
    """The only basis value accepted in Phase 2B for AccrualBasisFact and
    HistoricalAccrualFact — a human/authoritative manual confirmation."""

    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"


class ItemAllocationConfirmationType:
    MANUAL_CONFIRMED = "MANUAL_CONFIRMED"


@dataclass(frozen=True)
class CostRecognitionFact:
    """Answers: has this contract reached the current period's
    cost-recognition condition? Phase 2B does not auto-judge which
    business behavior implies cost recognition — the Fact Pack states it
    and the Rule Engine consumes the fact. See section 4 of the spec.

    ``shipment_id`` (Phase 2D.1-R2) is a provenance reference under
    docs/PHASE2D1-R0-DECISIONS.md section 3.4 — it names the Shipment
    anchor that evidenced this cost recognition, recorded once and never
    re-pointed. It records *which* shipment evidenced the recognition; it
    never creates the fact automatically (a human assertion is still
    required to create a CostRecognitionFact at all — 3.4's frozen
    "does NOT decide which business behavior means cost recognition"
    stands unchanged), and it is nullable because a CostRecognitionFact
    need not always be shipment-evidenced (MANUAL_CONFIRMED,
    SALES_EXECUTION_CONFIRMED) and because pre-R2 facts have no shipment
    to name."""

    id: UUID
    contract_id: UUID
    recognition_date: date
    basis: str
    source_fragment_id: UUID
    created_at: datetime
    shipment_id: UUID | None = None
    # Phase 2D.1-R5 pre-flight debt (docs/PHASE2D1-R0-DECISIONS.md
    # section 21/40): whole-fact supersession lineage pointer. NULL for
    # a current fact; set exactly once, at the moment a later
    # independently-evidenced Fact of the SAME type supersedes this one
    # — never re-pointed afterwards, never a stand-in for editing this
    # fact's own content in place. "Current" repository queries exclude
    # any row with this set; a history query can still see everything.
    superseded_by_fact_id: UUID | None = None


@dataclass(frozen=True)
class AccrualBasisFact:
    """Answers: if an accrual were needed, how much estimable cost does
    this business scope currently support? Amounts must be Decimal — no
    gross/1.13 style tax-rate math is ever coded. Tax rates and
    estimated costs always come from Facts. See section 5."""

    id: UUID
    scope_type: str  # CONTRACT / CONTRACT_ITEM
    contract_id: UUID
    contract_item_id: UUID | None
    quantity: Decimal | None
    estimated_cost: Decimal
    basis: str
    source_fragment_id: UUID
    created_at: datetime
    # See CostRecognitionFact.superseded_by_fact_id.
    superseded_by_fact_id: UUID | None = None


@dataclass(frozen=True)
class HistoricalAccrualFact:
    """A business-state fact that already existed when the system went
    live — a formal historical accrual that is NOT claimed to have been
    computed by this system. See section 6."""

    id: UUID
    source_period: str
    contract_item_id: UUID
    quantity: Decimal
    estimated_cost: Decimal
    basis: str
    source_fragment_id: UUID
    confirmed_at: datetime
    # See CostRecognitionFact.superseded_by_fact_id.
    superseded_by_fact_id: UUID | None = None


@dataclass(frozen=True)
class Accrual:
    """Per the frozen Domain. created_from_fact_id traces to the fact
    that justified creating the Accrual (Phase 2B: HistoricalAccrualFact
    only — future self-generated accruals may extend the source set).
    See section 7."""

    id: UUID
    period: str
    contract_item_id: UUID
    quantity: Decimal
    estimated_cost: Decimal
    basis: str
    status: str
    created_from_fact_id: UUID
    created_at: datetime


@dataclass(frozen=True)
class AccrualReversal:
    """A real reversal row — Phase 2B forbids mutating the Accrual with
    a bare ``reversed_amount += x`` because that loses history. The
    remaining balance is always DERIVED (original Accrual minus the sum
    of reversals), never an independently mutable truth. See section 8."""

    id: UUID
    accrual_id: UUID
    period: str
    invoice_item_allocation_id: UUID
    reversed_quantity: Decimal
    reversed_estimated_cost: Decimal
    created_at: datetime


@dataclass(frozen=True)
class InvoiceItemAllocation:
    """The ContractItem ↔ InvoiceItem confirmed relationship that makes
    R006 (partial receipt) safe. Phase 2B adds no automatic item-matching
    algorithm — only MANUAL_CONFIRMED, or a confirmation explicitly
    established by a synthetic/private Fact Pack. See sections 10-11."""

    id: UUID
    invoice_item_id: UUID
    contract_item_id: UUID
    allocated_quantity: Decimal
    allocated_net_amount: Decimal
    confirmation_type: str
    source_fragment_id: UUID
    created_at: datetime
    # See CostRecognitionFact.superseded_by_fact_id.
    superseded_by_fact_id: UUID | None = None


def get_accrual_balance(
    accrual: Accrual, reversals: list[AccrualReversal]
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (remaining_quantity, remaining_estimated_cost,
    reversed_quantity, reversed_estimated_cost).

    The remaining balance is derived from the original Accrual minus the
    sum of AccrualReversals — it is never a separately stored truth.
    See section 8."""
    reversed_quantity = sum((r.reversed_quantity for r in reversals), Decimal("0"))
    reversed_cost = sum((r.reversed_estimated_cost for r in reversals), Decimal("0"))
    return (
        accrual.quantity - reversed_quantity,
        accrual.estimated_cost - reversed_cost,
        reversed_quantity,
        reversed_cost,
    )


def get_projected_accrual_status(reversed_quantity: Decimal, remaining_quantity: Decimal) -> str:
    """The single status rule for every accrual (section 9):

    0 reversals AND remaining > 0            -> ACTIVE
    0 < reversed < original (remaining > 0)  -> PARTIALLY_REVERSED
    remaining == 0                            -> REVERSED
    """
    if remaining_quantity <= 0:
        return AccrualStatus.REVERSED
    if reversed_quantity > 0:
        return AccrualStatus.PARTIALLY_REVERSED
    return AccrualStatus.ACTIVE


def is_open_accrual(accrual: Accrual, reversals: list[AccrualReversal]) -> bool:
    """The single shared predicate R001 / R002 / R003 all test — a
    "not fully reversed" accrual: status in {ACTIVE, PARTIALLY_REVERSED}
    with remaining balance > 0, NOT status == ACTIVE alone. Phase 0 froze
    this as the common semantics; nobody re-implements it locally.
    See section 9 and RULES.md R001-R003."""
    remaining_quantity, remaining_cost, _, _ = get_accrual_balance(accrual, reversals)
    return remaining_quantity > 0 and remaining_cost > 0
