from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


class ExceptionType:
    BUSINESS_KEY_CONFLICT = "BusinessKeyConflict"
    # Phase 2A: a unique M001 candidate would push confirmed allocations
    # past the contract's gross_amount. See spec section 24.
    ALLOCATION_CAPACITY_EXCEEDED = "AllocationCapacityExceeded"


class ExceptionStatus:
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


@dataclass
class TaskException:
    """Landing object for anything a rule could not resolve with high
    confidence. Phase 1 only creates BUSINESS_KEY_CONFLICT; Phase 2A adds
    ALLOCATION_CAPACITY_EXCEEDED. See docs/RULES.md R004 and spec section 24."""

    id: UUID
    exception_type: str
    status: str
    summary: str
    detail: dict[str, Any]
    created_at: datetime
