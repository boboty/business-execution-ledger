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
    # Phase 2D.1-R1: a ContractItem CORRECTION superseded a revision that
    # persisted derived records (InvoiceItemAllocation, Accrual,
    # AccrualBasisFact, HistoricalAccrualFact) still identity-reference.
    # Per docs/PHASE2D1-R0-DECISIONS.md section 1.5, none of those
    # records is edited or invalidated automatically — this Task is
    # raised so a human decides what, if anything, must be redone.
    CONTRACT_ITEM_FACT_SUPERSEDED = "ContractItemFactSuperseded"
    # Phase 2D.1-R2: a Shipment CORRECTION superseded a revision whose
    # anchor a persisted CostRecognitionFact.shipment_id still names.
    # Same policy as CONTRACT_ITEM_FACT_SUPERSEDED (section 1.5): the
    # CostRecognitionFact is never edited or re-pointed automatically —
    # this Task is raised so a human decides what, if anything, must be
    # redone.
    SHIPMENT_FACT_SUPERSEDED = "ShipmentFactSuperseded"
    # Phase 2D.1-R2 Codex fix round, BLOCKER 1: a Shipment create with no
    # `external_reference` has an incomplete business identity
    # (docs/PHASE2D1-R0-DECISIONS.md section 4.4: "requires human
    # confirmation"). The Evidence is preserved and this Task is raised;
    # no Shipment anchor is created until a human explicitly confirms
    # (bel.application.shipment_facts.create_shipment_fact's
    # `identity_confirmed` parameter).
    SHIPMENT_IDENTITY_INCOMPLETE = "ShipmentIdentityIncomplete"
    # Phase 2D.1-R2 Codex fix round, BLOCKER 2: a Shipment create names
    # the SAME full business identity as an existing anchor, but under
    # DIFFERENT Evidence asserting DIFFERENT content (section 4.4: "Same
    # key, different Evidence -> Task"). The existing anchor/revision is
    # left completely unchanged; a human must resolve the conflict via an
    # explicit supplement or correction.
    SHIPMENT_IDENTITY_CONFLICT = "ShipmentIdentityConflict"
    # Phase 2D.1-R3a Slice 1: a SalesContract create is missing
    # `our_entity` and/or `sales_contract_no` (docs/PHASE2D1-R0-DECISIONS.md
    # section 4.4: "NO canonical anchor may be created"). Unlike Shipment,
    # there is no confirmation override for this — the identity is
    # genuinely required, not merely "incomplete but usable". Evidence is
    # preserved and this Task is raised instead of any anchor.
    SALES_CONTRACT_IDENTITY_INCOMPLETE = "SalesContractIdentityIncomplete"
    # Phase 2D.1-R3a Slice 1: a SalesContract was created (or currently
    # stands) with `customer = NULL` (section 2.3: "A SalesContract may
    # legitimately exist before its customer is known... the scope
    # carries an unresolved-customer Task. It is never guessed."). Closed
    # (ExceptionStatus.RESOLVED) once a SUPPLEMENT fills in `customer`.
    SALES_CONTRACT_CUSTOMER_UNRESOLVED = "SalesContractCustomerUnresolved"
    # Phase 2D.1-R3a Slice 2: Evidence merely SUGGESTS a procurement/sales
    # pairing (e.g. a candidate scope reference) without an explicit
    # AUTO_CONFIRMED/HUMAN_CONFIRMED confirmation action
    # (docs/PHASE2D1-R0-DECISIONS.md section 2.4: "a link exists only for
    # a confirmed relationship... unresolved, a Task"). No
    # ProcurementSalesLink row is ever created for this — zero link rows,
    # a persisted Task instead. Idempotent by exact replay of the same
    # unresolved Evidence.
    PROCUREMENT_SALES_LINK_UNCONFIRMED = "ProcurementSalesLinkUnconfirmed"
    # Phase 2D.1-R3a Slice 2: new Evidence conflicts with a CURRENT
    # ProcurementSalesLink episode's assertion (section 2.4: "Conflicting
    # Evidence — never overwrites and never silently re-points a current
    # link. It produces a Task, and the current assertion is unchanged
    # until a human confirms"). The current episode is left completely
    # unchanged; only an explicit human-confirmed CORRECT/INVALIDATE
    # changes the authoritative relationship.
    PROCUREMENT_SALES_LINK_CONFLICT = "ProcurementSalesLinkConflict"
    # Phase 2D.1-R3a Slice 2: one procurement Contract now has more than
    # one CURRENT ProcurementSalesLink to different SalesContracts
    # (section 2.4's cardinality table: "Allowed structurally, but
    # ambiguous: which sales scope that contract's cost serves is
    # undecidable from the link alone. Produces a Task; the system does
    # not choose an attribution."). This never blocks the ADD that
    # created the second link — it only surfaces the ambiguity.
    PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES = "ProcurementSalesLinkMultipleScopes"
    # Phase 2D.1-R3a Slice 2 (correction lineage invariant): a SECOND,
    # DIFFERENT correction was attempted against a `superseded_link_id`
    # that already has one (docs/PHASE2D1-R0-DECISIONS.md section 2.4:
    # "superseded_link_id is semantically unique... a different
    # replacement for an already-corrected superseded_link_id is a
    # conflict -> Task / reject. No second correction record is written,
    # and the existing lineage is not altered").
    PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT = "ProcurementSalesLinkCorrectionConflict"


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
