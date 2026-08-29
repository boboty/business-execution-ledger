from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class SalesContractRevisionType:
    """See docs/PHASE2D1-R0-DECISIONS.md section 1.1 — the three cases
    that never share one operation, reused unchanged for SalesContract
    per section 1.4 ("It reuses the SAME model as ContractItem and
    Contract... no second correction mechanism is created for the sales
    leg"). Exactly one INITIAL revision exists per anchor; SUPPLEMENT and
    CORRECTION each append a new revision."""

    INITIAL = "INITIAL"
    SUPPLEMENT = "SUPPLEMENT"
    CORRECTION = "CORRECTION"


# The versioned, correctable business-value fields living on a
# SalesContractRevision. docs/PHASE2D1-R0-DECISIONS.md section 2.2's
# minimal field list also names `our_entity` and `sales_contract_no` —
# those two are the anchor's frozen business identity (section 4.4:
# (our_entity, sales_contract_no)) and therefore live on the
# SalesContract anchor itself, immutable after creation, exactly like
# ContractItem's (contract_id, source_item_key) and Shipment's
# (contract_id, external_reference, execution_date). `customer` is
# treated uniformly with `currency`/`gross_amount`/`contract_date` for
# supplement/correct purposes — section 2.2 marks only `customer` as
# explicitly "(nullable)", but nothing freezes currency/gross_amount/
# contract_date as required at creation either, and forcing them to be
# would contradict the incremental-Fact-maintenance philosophy R1/R2
# already established. Correcting an identity-bearing field
# (`our_entity`/`sales_contract_no`) is out of scope for this round (see
# module docstring in sales_contract_facts.py) — they are simply never
# members of this tuple, so supplement/correct reject them outright as
# unknown fields, exactly like R1/R2's identity-field protection.
SALES_CONTRACT_FACT_FIELDS: tuple[str, ...] = (
    "customer",
    "currency",
    "gross_amount",
    "contract_date",
)


@dataclass
class SalesContract:
    """The assembled projection — the stable identity anchor joined with
    its current (un-superseded) SalesContractRevision. See
    docs/PHASE2D1-R0-DECISIONS.md section 2.2 for the frozen minimal
    field list. `customer` is the ONLY place in the domain an external
    sales customer is expressed (section 2.2) and may legitimately be
    `None` (section 2.3) — a scope known before its customer is."""

    id: UUID
    our_entity: str
    sales_contract_no: str
    customer: str | None
    currency: str | None
    gross_amount: Decimal | None
    contract_date: date | None
    current_source_fragment_id: UUID
    created_at: datetime


@dataclass
class SalesContractRevision:
    """One versioned assertion about a SalesContract anchor — the same
    anchor + revision model as ContractItemRevision/ShipmentRevision
    (docs/PHASE2D1-R0-DECISIONS.md section 1.3), reused as a pattern, not
    abstracted into a shared generic engine.

    ``source_fragment_id`` is required and never nullable at the schema
    level — like ShipmentRevision, SalesContract has no pre-R3a legacy
    data to accommodate, so this is a real NOT NULL column, not merely an
    application-layer rule. ``superseded_by_revision_id`` is NULL for the
    current revision and set exactly once, at the moment a later revision
    supersedes this one.

    ``asserted_field_names`` is the exact set of field names the writing
    command actually asserted, captured verbatim at write time — see
    ContractItemRevision's docstring (Phase 2D.1-R1 Codex fix round #2)
    for why this must never be reconstructed after the fact by diffing
    against the predecessor."""

    id: UUID
    sales_contract_id: UUID
    revision_type: str
    customer: str | None
    currency: str | None
    gross_amount: Decimal | None
    contract_date: date | None
    source_fragment_id: UUID
    superseded_by_revision_id: UUID | None
    created_at: datetime
    asserted_field_names: list[str] | None = None
