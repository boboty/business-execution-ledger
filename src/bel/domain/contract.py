from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


@dataclass
class Contract:
    """A canonical business fact. contract_no is a business key, not a
    unique constraint — see docs/DOMAIN.md."""

    id: UUID
    contract_no: str
    contract_type: str | None
    counterparty: str | None
    buyer: str | None
    gross_amount: Decimal
    currency: str
    contract_date: date | None
    current_source_fragment_id: UUID
    created_at: datetime
    updated_at: datetime


@dataclass
class ContractItem:
    """First-class per docs/DOMAIN.md. Phase 1 never synthesizes these —
    only a contract with genuine per-item evidence gets one.

    source_item_key is an implementation-level stable reference used by
    Fact Pack selectors — not a global business key and not a SKU. See
    docs/PHASE2B-DECISIONS.md."""

    id: UUID
    contract_id: UUID
    source_item_key: str | None
    sku: str | None
    product_name: str | None
    specification: str | None
    quantity: Decimal | None
    unit: str | None
    unit_price: Decimal | None
    gross_amount: Decimal | None
    tax_rate: Decimal | None
    net_amount: Decimal | None
    current_source_fragment_id: UUID | None
    created_at: datetime
