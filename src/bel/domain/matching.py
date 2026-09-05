from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

# Frozen storage precision for every allocated-amount column this module
# writes to (NUMERIC(18,2) — see the R3b migration and models.py). This
# EXACT bound is also the DB CHECK constraint on both
# Sales*AllocationModel tables — the two must agree, not merely both be
# "reasonable", or a value the canonical validator accepts can still
# reach the DB and raise a raw IntegrityError instead of a clean
# rejection (Gate 2D.1-R3b fix round #2's second BLOCKER).
_STORAGE_QUANTUM = Decimal("0.01")
_STORAGE_MAX_ABS = Decimal(10) ** 16  # exclusive upper bound, matching the DB CHECK constraints exactly


def validate_storable_amount(amount: Decimal) -> None:
    """Phase 2D.1-R3b Gate fix rounds, BLOCKER 2 (four rounds): the
    single canonical check for "is this a legitimate, storable, positive
    allocation amount" — used by BOTH the application layer
    (`bel.application.sales_matching._validate_allocation_pairs`) and
    the repository layer's `add()` methods (the actual authoritative
    write primitive), so neither can be bypassed independently of the
    other, AND so nothing this function accepts can still be rejected
    later by the DB CHECK constraints as a raw `IntegrityError`.

    Round 1 checked only SCALE (at most 2 decimal places) — missed a
    3-decimal-place value. Round 2 added a digit-count-based upper bound
    (reject a 16-or-more-integer-digit value) — this MISSED the actual
    failure mode: SQLite has no native fixed-point decimal type, so
    SQLAlchemy binds `Decimal` values to it via `float` (IEEE-754 double,
    53 bits of mantissa), and a 14-integer-digit value ending in `.99`
    (`10**14 - 0.01`) is silently corrupted despite being comfortably
    under that threshold — the failure is a property of a SPECIFIC
    value's binary representation, not digit count. Round 3 replaced the
    digit-count heuristic with an exact round-trip check
    (`Decimal(str(float(quantized))) == quantized`) — but DROPPED the
    absolute upper bound entirely, so a value like exactly `10**16`
    (which floats CAN represent losslessly, since it is a "round"
    power-of-ten magnitude) passed this function only to be rejected
    moments later by the DB's `< 10**16` CHECK constraint as a raw
    `IntegrityError` the caller never expected.

    Round 4's fix: BOTH conditions must hold, independently — this
    function is authoritative for BOTH concerns:

        0 < quantized < 10**16                          (matches the DB CHECK exactly)
        AND
        Decimal(str(float(quantized))) == quantized     (catches in-range values SQLite would still corrupt)

    Neither condition subsumes the other: `10**16` satisfies the
    round-trip check but violates the magnitude bound; `10**14 - 0.01`
    satisfies the magnitude bound but violates the round-trip check.
    Both must be checked."""
    if not isinstance(amount, Decimal):
        raise ValueError(f"amount must be a Decimal, got {type(amount)}")
    if not amount.is_finite():
        raise ValueError(f"amount must be a finite value, got {amount}")
    try:
        quantized = amount.quantize(_STORAGE_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError(f"amount {amount} cannot be represented at NUMERIC(18,2) precision") from exc
    if quantized != amount:
        raise ValueError(
            f"amount {amount} cannot be represented exactly at NUMERIC(18,2) precision (at most 2 decimal places)"
        )
    if amount <= 0:
        raise ValueError(f"amount must be positive, got {amount}")
    if quantized >= _STORAGE_MAX_ABS:
        raise ValueError(
            f"amount {amount} is out of the allowed storage range — must satisfy 0 < amount < {_STORAGE_MAX_ABS} "
            "(matches the database's own CHECK constraint exactly, so this is never merely deferred to a raw "
            "IntegrityError at write time)"
        )
    if Decimal(str(float(quantized))) != quantized:
        raise ValueError(
            f"amount {amount} cannot survive this database's storage round-trip unchanged (SQLite stores "
            "NUMERIC values as IEEE-754 double-precision floats, which cannot exactly represent every value "
            "at this magnitude) — rejecting rather than silently corrupting the authoritative amount"
        )


class SubjectType:
    INVOICE = "INVOICE"
    PAYMENT = "PAYMENT"


class MatchCaseStatus:
    AUTO_CONFIRMED = "AUTO_CONFIRMED"
    HUMAN_CONFIRMATION_REQUIRED = "HUMAN_CONFIRMATION_REQUIRED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    UNMATCHED = "UNMATCHED"


class MatchMethod:
    """The rule that produced a MatchCase. Only M001 exists in Phase 2A —
    see docs/RULES.md-adjacent docs/PHASE2A-DECISIONS.md."""

    M001 = "M001"
    # Phase 2D.1-R3b: the sales leg's ONLY method — an explicit human
    # proposal naming candidate SalesContracts, never an automatic
    # amount/counterparty algorithm (docs/PHASE2D1-R0-DECISIONS.md
    # section 2.8: automatic sales matching REQUIRES BUSINESS RULE
    # FREEZE, deliberately not attempted this round). Named descriptively
    # rather than "M002" — this is not a numbered business rule, just a
    # manual-proposal marker on a reused MatchCase.
    MANUAL_SALES_SCOPE = "MANUAL_SALES_SCOPE"


class AllocationMatchMethod:
    """The specific mechanism an Allocation was created under.

    ``EXACT_COUNTERPARTY_AMOUNT_UNIQUE`` — Phase 2A's original literal
    (spec section 17): the subject had exactly ONE candidate contract.

    ``EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL`` — added by the confirmed
    chronological-matching business rule (docs/PHASE2A-DECISIONS.md): the
    subject had several EXACTLY equivalent candidate contracts (same
    counterparty + same amount), and BEL allocated it deterministically to
    the earliest candidate with sufficient remaining capacity. It is NOT a
    unique-candidate decision, so it is never mislabelled as
    ``..._UNIQUE``."""

    EXACT_COUNTERPARTY_AMOUNT_UNIQUE = "EXACT_COUNTERPARTY_AMOUNT_UNIQUE"
    EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL = "EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL"


class ConfirmationType:
    AUTO_CONFIRMED = "AUTO_CONFIRMED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"


@dataclass
class MatchCase:
    id: UUID
    subject_type: str
    subject_id: UUID
    status: str
    match_method: str
    created_at: datetime
    resolved_at: datetime | None


@dataclass
class MatchCandidate:
    """Real rows, not a JSON blob on MatchCase — see spec section 25."""

    id: UUID
    match_case_id: UUID
    contract_id: UUID
    created_at: datetime


@dataclass
class InvoiceAllocation:
    id: UUID
    invoice_id: UUID
    contract_id: UUID
    match_case_id: UUID
    allocated_gross_amount: Decimal
    match_method: str
    confirmation_type: str
    created_at: datetime


@dataclass
class PaymentAllocation:
    id: UUID
    payment_id: UUID
    contract_id: UUID
    match_case_id: UUID
    allocated_amount: Decimal
    match_method: str
    confirmation_type: str
    created_at: datetime


@dataclass
class SalesInvoiceAllocation:
    """The sales-side twin of `InvoiceAllocation`
    (docs/PHASE2D1-R0-DECISIONS.md section 2.7) — same allocation
    semantics and shape, a physically separate object targeting
    `sales_contract_id` instead of `contract_id`. A `SALES` invoice can
    never be attributed through `InvoiceAllocation`; this is the only
    table that can express it. No `match_method` field: R3b's only
    method is `MatchMethod.MANUAL_SALES_SCOPE`, already recorded on the
    `MatchCase` this allocation's `match_case_id` points to — recording
    it a second time here would just be redundant, not a frozen field."""

    id: UUID
    invoice_id: UUID
    sales_contract_id: UUID
    match_case_id: UUID
    allocated_gross_amount: Decimal
    confirmation_type: str
    created_at: datetime


@dataclass
class SalesPaymentAllocation:
    """The sales-side twin of `PaymentAllocation`
    (docs/PHASE2D1-R0-DECISIONS.md section 2.7) — an `IN` receipt can
    never be attributed through `PaymentAllocation`; this is the only
    table that can express it."""

    id: UUID
    payment_id: UUID
    sales_contract_id: UUID
    match_case_id: UUID
    allocated_amount: Decimal
    confirmation_type: str
    created_at: datetime


@dataclass
class SalesMatchCandidate:
    """A human-confirmation candidate for a sales-leg `MatchCase` — never
    an allocation itself (docs/PHASE2D1-R0-DECISIONS.md section 2.7's
    `MatchCase` reuse). Deliberately not a generalisation of
    `MatchCandidate` (whose `contract_id` is a hard FK to `contracts.id`,
    confirming it can only ever name a procurement contract) — a
    separate object, same shape, targeting `sales_contract_id` instead."""

    id: UUID
    match_case_id: UUID
    sales_contract_id: UUID
    created_at: datetime
