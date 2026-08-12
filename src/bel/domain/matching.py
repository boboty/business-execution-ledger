from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


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


class AllocationMatchMethod:
    """The specific mechanism an Allocation was created under — always
    this one literal value in Phase 2A, per spec section 17."""

    EXACT_COUNTERPARTY_AMOUNT_UNIQUE = "EXACT_COUNTERPARTY_AMOUNT_UNIQUE"


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
