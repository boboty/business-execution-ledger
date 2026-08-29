"""ProcurementSalesLink — the canonical procurement/sales bridge (Phase
2D.1-R3a Slice 2, docs/PHASE2D1-R0-DECISIONS.md section 2.4).

This is deliberately NOT a Fact-with-revisions object like
`ContractItem`/`Shipment`/`SalesContract`: a relationship either exists
(as a confirmed assertion episode) or it doesn't. There is no
"supplement a field" case — the minimum field set below is asserted
whole, once, per episode. Correction works at the RELATIONSHIP level
(supersede the whole episode via `ProcurementSalesLinkCorrection`), never
by editing a field on the link row.

Two layers of identity — FROZEN (section 2.4's "Two-layer identity: business
key vs assertion episode"):

    Relationship business key = (procurement_contract_id, sales_contract_id)
                                 WHICH relationship this is
    ProcurementSalesLink row  = ONE confirmed assertion episode —
                                 one occasion the relationship was
                                 confirmed to hold

Frozen invariant: **at most ONE CURRENT assertion episode per business
key** — never "only one row for the pair across all history". A business
key may accumulate several episodes over time (ADD, then later
INVALIDATE, then later REESTABLISH), at most one current.

`current(link)` is defined ONLY as: no `ProcurementSalesLinkCorrection`
names it as `superseded_link_id` (docs/PHASE2D1-R0-DECISIONS.md section
2.4's "Deterministic current-link selection"). This is why there is no
`status`/`is_current`/`superseded_by_link_id` field on this dataclass —
adding one would create a second, competing source of truth for
"current", and the frozen text explicitly forbids deciding currency by
inspecting timestamps or a mutable field. The link row is immutable from
the moment it is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


class ConfirmationType:
    """How an assertion episode came to be confirmed — provenance, not a
    workflow state. There is no `PENDING`/`OPEN`/`PROPOSED` member: an
    unconfirmed relationship never becomes a `ProcurementSalesLink` row
    at all (docs/PHASE2D1-R0-DECISIONS.md section 2.4: "a link exists
    only for a confirmed relationship"). Evidence that merely suggests a
    pairing produces a `Task`, never a row here."""

    AUTO_CONFIRMED = "AUTO_CONFIRMED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"


@dataclass
class ProcurementSalesLink:
    """One confirmed assertion episode. Minimum V1 fields, frozen
    (docs/PHASE2D1-R0-DECISIONS.md section 2.4) — deliberately excludes
    `amount`/`quantity`/`allocation_ratio` (no apportionment across a
    many-to-many edge), `contract_item_id` (`ContractItem` does not
    participate in V1), `invoice_id`/`payment_id` (R3b), and any
    `status`/`is_current`/`superseded_by_link_id` (currency is derived
    from `ProcurementSalesLinkCorrection` lineage, never stored here)."""

    id: UUID
    procurement_contract_id: UUID
    sales_contract_id: UUID
    source_fragment_id: UUID
    confirmation_type: str
    created_at: datetime


@dataclass
class ProcurementSalesLinkCorrection:
    """An append-only correction Fact that retires exactly one assertion
    episode (docs/PHASE2D1-R0-DECISIONS.md section 2.4's "Relationship
    correction: supersession and invalidation"). The link row it targets
    is never mutated or deleted.

    `replacement_link_id`:
      - `None`   -> pure invalidation: the relationship simply does not
                    exist; there is no replacement.
      - not None -> replacement: the relationship was actually with a
                    different business key, now asserted by the
                    referenced (possibly newly created) current episode.

    `superseded_link_id` is semantically unique — an episode may be
    superseded at most once, enforced at the storage level (never a
    second correction for the same episode, never a lineage fork).

    V1 freezes `confirmation_type` to `HUMAN_CONFIRMED` for every
    correction: corrective Evidence alone never flips authority
    (docs/PHASE2D1-R0-DECISIONS.md: "Only a human confirmation changes
    the authoritative current relationship")."""

    id: UUID
    superseded_link_id: UUID
    replacement_link_id: UUID | None
    source_fragment_id: UUID
    confirmation_type: str
    created_at: datetime
