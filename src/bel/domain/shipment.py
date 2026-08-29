from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


class ShipmentRevisionType:
    """See docs/PHASE2D1-R0-DECISIONS.md section 1.1 — the three cases
    that never share one operation, reused unchanged for Shipment per
    section 1.4 ("Yes — designed in from R2"). Exactly one INITIAL
    revision exists per anchor; SUPPLEMENT and CORRECTION each append a
    new revision."""

    INITIAL = "INITIAL"
    SUPPLEMENT = "SUPPLEMENT"
    CORRECTION = "CORRECTION"


# The versioned, correctable business-value fields living on a
# ShipmentRevision. docs/PHASE2D1-R0-DECISIONS.md section 3.2's minimal
# field list also names contract_id, external_reference and
# execution_date — those three are the anchor's frozen business identity
# (section 4.4: (contract_id, external_reference, execution_date)) and
# therefore live on the Shipment anchor itself, immutable after creation,
# exactly like ContractItem's (contract_id, source_item_key). Correcting
# an identity component is out of R2's scope, per the same reasoning R1
# applied to ContractItem's identity fields (section 4.4's
# "re-identification... always produces a Task", deferred to R5).
SHIPMENT_FACT_FIELDS: tuple[str, ...] = (
    "contract_item_id",
    "quantity",
)


@dataclass
class Shipment:
    """The assembled projection — the stable identity anchor joined with
    its current (un-superseded) ShipmentRevision. See
    docs/PHASE2D1-R0-DECISIONS.md section 3.2 for the frozen minimal
    field list and section 3.3 for the Contract association (a Shipment
    names exactly one procurement Contract; a shipment genuinely
    spanning contracts is an unresolved case out of R2's scope, never a
    silent split)."""

    id: UUID
    contract_id: UUID
    external_reference: str | None
    execution_date: date
    contract_item_id: UUID | None
    quantity: Decimal | None
    current_source_fragment_id: UUID
    created_at: datetime


@dataclass
class ShipmentRevision:
    """One versioned assertion about a Shipment anchor — the same
    anchor + revision model as ContractItemRevision
    (docs/PHASE2D1-R0-DECISIONS.md section 1.3), reused as a pattern, not
    abstracted into a shared generic engine.

    ``source_fragment_id`` is required and never nullable at the schema
    level (section 3.2: "source_fragment_id required — Evidence trace,
    never nullable") — unlike ContractItemRevision, Shipment has no
    pre-R2 legacy data to accommodate, so this frozen requirement is
    enforced as a real NOT NULL column, not merely at the application
    layer. ``superseded_by_revision_id`` is NULL for the current revision
    and set exactly once, at the moment a later revision supersedes this
    one.

    ``asserted_field_names`` is the exact set of field names the writing
    command actually asserted, captured verbatim at write time — see
    ContractItemRevision's docstring for why this must never be
    reconstructed after the fact by diffing against the predecessor."""

    id: UUID
    shipment_id: UUID
    revision_type: str
    contract_item_id: UUID | None
    quantity: Decimal | None
    source_fragment_id: UUID
    superseded_by_revision_id: UUID | None
    created_at: datetime
    asserted_field_names: list[str] | None = None
