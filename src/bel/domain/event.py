from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


class BusinessEventType:
    """Append-only audit event types. Not an event-sourcing mechanism —
    see A05/Phase 0 non-goals in docs/ARCHITECTURE.md and docs/V1-SCOPE.md."""

    CONTRACT_IMPORTED = "CONTRACT_IMPORTED"
    BUSINESS_KEY_CONFLICT_DETECTED = "BUSINESS_KEY_CONFLICT_DETECTED"

    # Phase 2A — spec section 28.
    INVOICE_IMPORTED = "INVOICE_IMPORTED"
    PAYMENT_IMPORTED = "PAYMENT_IMPORTED"
    INVOICE_MATCH_AUTO_CONFIRMED = "INVOICE_MATCH_AUTO_CONFIRMED"
    PAYMENT_MATCH_AUTO_CONFIRMED = "PAYMENT_MATCH_AUTO_CONFIRMED"
    MATCH_HUMAN_CONFIRMATION_REQUIRED = "MATCH_HUMAN_CONFIRMATION_REQUIRED"
    MATCH_HUMAN_CONFIRMED = "MATCH_HUMAN_CONFIRMED"
    MATCH_REJECTED = "MATCH_REJECTED"
    ALLOCATION_CAPACITY_EXCEEDED = "ALLOCATION_CAPACITY_EXCEEDED"


@dataclass(frozen=True)
class BusinessEvent:
    id: UUID
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
