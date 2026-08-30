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


class ContractRevisionType:
    """See docs/PHASE2D1-R0-DECISIONS.md section 1.1 — the three cases
    that never share one operation, now closing the Phase 2D.1-R5
    pre-flight debt: Contract itself needed the SAME anchor+revision
    seam ContractItem/Shipment/SalesContract already have, so a revised
    legacy ledger can supplement/correct a Contract's non-identity
    values instead of creating a duplicate anchor."""

    INITIAL = "INITIAL"
    SUPPLEMENT = "SUPPLEMENT"
    CORRECTION = "CORRECTION"


# The versioned business-value fields living on a Contract revision —
# everything on Contract except its identity (id, contract_no,
# counterparty, created_at) and the derived updated_at (the current
# revision's own created_at). contract_no/counterparty are
# identity-bearing and deliberately excluded: changing either is
# RE-IDENTIFICATION (docs/PHASE2D1-R0-DECISIONS.md section 4.4), never a
# plain correction — excluding them from this field set makes that
# structurally impossible through the ordinary supplement/correct path.
CONTRACT_FACT_FIELDS: tuple[str, ...] = ("contract_type", "buyer", "gross_amount", "currency", "contract_date")


@dataclass
class ContractRevision:
    """One versioned assertion about a Contract anchor — the same shape
    as ContractItemRevision (docs/PHASE2D1-R0-DECISIONS.md section 1.3).
    ``gross_amount``/``currency`` stay Optional here (matching every
    other revision dataclass's relaxed schema-level typing for direct
    repository callers) even though the assembled ``Contract`` dataclass
    requires them non-NULL; ``bel.application.contract_facts`` is what
    enforces that requirement for every new revision it writes."""

    id: UUID
    contract_id: UUID
    revision_type: str
    contract_type: str | None
    buyer: str | None
    gross_amount: Decimal | None
    currency: str | None
    contract_date: date | None
    source_fragment_id: UUID | None
    superseded_by_revision_id: UUID | None
    created_at: datetime
    asserted_field_names: list[str] | None = None


@dataclass
class ContractItem:
    """First-class per docs/DOMAIN.md. Phase 1 never synthesizes these —
    only a contract with genuine per-item evidence gets one.

    source_item_key is an implementation-level stable reference used by
    Fact Pack selectors — not a global business key and not a SKU. See
    docs/PHASE2B-DECISIONS.md.

    Phase 2D.1-R1: this dataclass is now an assembled projection — the
    stable identity anchor joined with its current (un-superseded)
    ContractItemRevision. Its shape is unchanged from before R1 (a hard
    requirement of docs/PHASE2D1-R0-DECISIONS.md section 1.3) so every
    existing consumer (period_close.py, contract_360.py, ...) sees no
    change. current_source_fragment_id now genuinely tracks the current
    revision's Evidence, unlike the pre-R1 field of the same name, which
    docs/PHASE2D1-R0-DECISIONS.md documented as never updated."""

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


class ContractItemRevisionType:
    """See docs/PHASE2D1-R0-DECISIONS.md section 1.1 — the three cases
    that never share one operation. Exactly one INITIAL revision exists
    per anchor; SUPPLEMENT and CORRECTION each append a new revision."""

    INITIAL = "INITIAL"
    SUPPLEMENT = "SUPPLEMENT"
    CORRECTION = "CORRECTION"


# The versioned business-value fields living on a revision — everything
# on ContractItem except its identity (id, contract_id, source_item_key,
# created_at). Shared by the domain, repository and application layers
# so the field set has exactly one definition.
CONTRACT_ITEM_FACT_FIELDS: tuple[str, ...] = (
    "sku",
    "product_name",
    "specification",
    "quantity",
    "unit",
    "unit_price",
    "gross_amount",
    "tax_rate",
    "net_amount",
)


@dataclass
class ContractItemRevision:
    """One versioned assertion about a ContractItem anchor. See
    docs/PHASE2D1-R0-DECISIONS.md section 1.3 — the anchor is stable and
    never superseded; business values live here instead.

    ``source_fragment_id`` is a provenance reference (never re-pointed):
    the exact Evidence for THIS revision. ``superseded_by_revision_id``
    is NULL for the current revision and set exactly once, at the moment
    a later revision supersedes this one — it is never otherwise
    mutated, and a retired revision's own fields never change again.

    ``asserted_field_names`` is the exact set of field names the command
    that created this revision was actually asked to assert — captured
    verbatim at write time, never reconstructed after the fact (Phase
    2D.1-R1 Codex fix round #2: diffing a revision against its
    predecessor cannot recover a field the caller re-asserted with its
    ALREADY-current value, since the stored snapshot looks identical
    either way). ``None`` only for revisions with no captured intent —
    pre-R1 data carried forward by the migration, and the
    ``ContractItemRepository.add()`` test convenience — for which
    ``_asserted_fields`` falls back to a best-effort reconstruction."""

    id: UUID
    contract_item_id: UUID
    revision_type: str
    sku: str | None
    product_name: str | None
    specification: str | None
    quantity: Decimal | None
    unit: str | None
    unit_price: Decimal | None
    gross_amount: Decimal | None
    tax_rate: Decimal | None
    net_amount: Decimal | None
    # Nullable at the schema/Domain level for the same reason
    # Contract/ContractItem's own current_source_fragment_id was nullable
    # pre-R1 (no DB-level Evidence enforcement — see
    # docs/PHASE2D1-R0-DECISIONS.md section 1.2). The R1 application
    # commands (bel.application.contract_item_facts) require a real,
    # resolvable fragment for every revision they create; this looser
    # schema-level type only accommodates direct repository callers
    # (tests, fixtures) outside that command boundary.
    source_fragment_id: UUID | None
    superseded_by_revision_id: UUID | None
    created_at: datetime
    asserted_field_names: list[str] | None = None
