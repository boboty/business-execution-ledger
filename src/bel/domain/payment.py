from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class PaymentDirection:
    IN = "IN"
    OUT = "OUT"


@dataclass
class Payment:
    """A cash-movement fact, not an accounting payment voucher.
    amount is always positive; direction carries the sign. The bank's
    own signed representation stays in Evidence.raw_data untouched. See
    docs/PHASE2A-DECISIONS.md."""

    id: UUID
    transaction_date: date
    direction: str
    amount: Decimal
    counterparty: str | None
    business_type: str | None
    bank_reference: str | None
    description: str | None
    running_balance: Decimal | None
    source_fragment_id: UUID
    created_at: datetime
