from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class InvoiceDirection:
    PURCHASE = "PURCHASE"
    SALES = "SALES"
    UNKNOWN = "UNKNOWN"


@dataclass
class Invoice:
    """direction is never guessed from the file — the importer takes it
    as an explicit CLI argument. See docs/PHASE2A-DECISIONS.md.

    Phase 2D.3-F1e (docs/PHASE2D3-RULE-FREEZE.md IP-P02): ``currency``
    is the canonical currency explicitly stated by the Invoice
    Evidence/source. It is Evidence-derived ONLY — never defaulted to
    CNY/USD, never inferred from buyer/seller/country, never copied from
    ``Contract.currency`` / ``SalesContract.currency``, never inferred
    from an amount, and never FX-converted. ``None`` means the source
    stated no explicit currency (an incomplete Fact, never a defaulted
    value). It is NOT an identity field: ``external_invoice_key`` /
    Invoice identity / matching identity are unchanged, and existing
    rows remain valid with ``currency = None``."""

    id: UUID
    direction: str
    invoice_type: str | None
    invoice_no: str | None
    digital_invoice_no: str | None
    external_invoice_key: str | None
    issue_date: date | None
    seller: str | None
    buyer: str | None
    net_amount: Decimal
    tax_amount: Decimal
    gross_amount: Decimal
    invoice_status: str | None
    source_fragment_id: UUID
    created_at: datetime
    updated_at: datetime
    # Phase 2D.3-F1e canonical currency — placed at the end (after the
    # non-default fields) so the dataclass ordering stays valid and every
    # existing keyword constructor site keeps working; ``None`` is the
    # no-explicit-currency default, never a manufactured domestic value.
    currency: str | None = None


@dataclass
class InvoiceItem:
    """InvoiceItem is not a ContractItem — see docs/PHASE2A-DECISIONS.md.
    An invoice can name a product; that never creates or implies a
    ContractItem."""

    id: UUID
    invoice_id: UUID
    line_no: int
    product_name: str | None
    specification: str | None
    unit: str | None
    quantity: Decimal | None
    unit_price: Decimal | None
    net_amount: Decimal
    tax_rate: Decimal | None
    tax_amount: Decimal
    gross_amount: Decimal
    source_fragment_id: UUID
