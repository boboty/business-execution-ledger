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
    # Phase 2D.1-R5 pre-flight debt (docs/PHASE2D1-R0-DECISIONS.md
    # section 4.4): "Payment identity is known to be weak... the robust
    # fix is to add a source-account identifier to Payment." Stable
    # identifier of the source BANK ACCOUNT this Payment Fact came
    # from — never a counterparty/destination account, a bank
    # reference, a filename, or an import profile name. Nullable: no
    # value is fabricated for pre-existing Payment rows, and the
    # current CMB statement adapter cannot deterministically parse one
    # from the PDF text layer, so it is supplied explicitly by the
    # caller (import/backfill) rather than guessed.
    source_account_id: str | None = None
