"""Whole-fact supersession — the minimal production Application seam
(Phase 2D.1-R5 gate fix, docs/PHASE2D1-R0-DECISIONS.md section 21).

Four typed functions, one per cutover-eligible fact type
(``CostRecognitionFact``, ``AccrualBasisFact``, ``HistoricalAccrualFact``,
``InvoiceItemAllocation``) — deliberately NOT a generic
``supersede(fact_type, fact_id, ...)`` dispatcher. Each function:

1. requires the OLD fact to exist and still be current (never already
   superseded — no forked lineage, matching the repository-level
   ``mark_superseded`` CAS guarantee one layer up);
2. requires the NEW fact to cite genuinely NEW Evidence (a different
   ``source_fragment_id`` from the old fact's own) — a correction is
   never manufactured from the same artifact that was already wrong;
3. requires the NEW fact's business scope to be COMPATIBLE with the
   OLD one — the same Contract/ContractItem/InvoiceItem the old fact
   was about, never an arbitrary cross-business replacement;
4. writes the new fact, then atomically marks the old one superseded;
   the old fact's own row is never edited in place.

Not a generic temporal/fact framework: no shared "Fact" superclass, no
polymorphic dispatch, no persistence beyond the narrow per-object
mechanics ``mark_superseded`` already established.

Gate-fix (Phase 2D.1-R5 round 2), HARD: supersession is a SECOND WRITER
for a fact type, not a way around that fact type's safety constraints.
Every function here reuses the SAME validation its fact type's ordinary
creation path applies — never a reimplementation of it, and never a
blanket exemption "because it is only a correction":

- ``InvoiceItemAllocation`` — the full section-11 check via
  ``bel.application.item_allocation.validate_item_allocation`` (11-A
  confirmed contract-level allocation, 11-B capacity and amount
  legality), with the capacity base EXCLUDING the allocation that is
  being superseded (the old one's quantity is being given back, so it
  must not also count as committed), plus the closed
  ``confirmation_type`` set (MANUAL_CONFIRMED).
- ``CostRecognitionFact`` — the closed ``basis`` set, plus: a
  ``shipment_id`` must name a real Shipment belonging to the SAME
  Contract as the fact being superseded (never a cross-contract
  retarget smuggled in as a "correction").
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from bel.application.import_close_facts import _VALID_COST_RECOGNITION_BASIS
from bel.application.item_allocation import validate_item_allocation
from bel.domain.accrual import (
    AccrualBasisFact,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
    ItemAllocationConfirmationType,
)
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    ContractItemRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    ShipmentRepository,
)


class WholeFactSupersessionError(ValueError):
    """A rejected supersession attempt — the old fact is missing/already
    superseded, the new Evidence is not genuinely new, the business
    scope is incompatible, the replacement violates this fact type's own
    safety constraints, or a concurrent supersession won the race."""


# The closed confirmation set every other InvoiceItemAllocation writer
# enforces (Phase 2B section 10) — supersession inherits it, never
# widens it.
_LEGAL_ITEM_ALLOCATION_CONFIRMATION_TYPES = frozenset({ItemAllocationConfirmationType.MANUAL_CONFIRMED})


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise WholeFactSupersessionError(f"EvidenceFragment {source_fragment_id} not found")


def _require_same_contract_shipment(
    session: Session, shipment_id: uuid.UUID, contract_id: uuid.UUID
) -> None:
    """A ``CostRecognitionFact``'s ``shipment_id`` is provenance for the
    SAME Contract the fact is about (docs/PHASE2D1-R0-DECISIONS.md
    section 3.4) — a Shipment of another Contract is a different
    business fact, never a correction of this one."""
    shipment = ShipmentRepository(session).get(shipment_id)
    if shipment is None:
        raise WholeFactSupersessionError(f"Shipment {shipment_id} not found")
    if shipment.contract_id != contract_id:
        raise WholeFactSupersessionError(
            f"Shipment {shipment_id} belongs to Contract {shipment.contract_id}, not Contract {contract_id} — "
            "shipment-evidenced cost recognition must name a Shipment of the SAME contract"
        )


def _require_valid_item_allocation(
    session: Session,
    *,
    invoice_item,
    contract_item,
    allocated_quantity,
    allocated_net_amount,
    existing_allocated_quantity: Decimal,
) -> None:
    """The one section-11 gate every InvoiceItemAllocation writer goes
    through (``bel.application.item_allocation``) — reused verbatim
    here, never reimplemented."""
    try:
        validate_item_allocation(
            session=session,
            invoice_item=invoice_item,
            contract_item=contract_item,
            allocated_quantity=allocated_quantity,
            allocated_net_amount=allocated_net_amount,
            existing_allocated_quantity=existing_allocated_quantity,
        )
    except ValueError as exc:
        raise WholeFactSupersessionError(str(exc)) from exc


def supersede_cost_recognition_fact(
    session: Session,
    *,
    superseded_fact_id: uuid.UUID,
    recognition_date: date,
    basis: str,
    source_fragment_id: uuid.UUID,
    shipment_id: uuid.UUID | None,
    created_at: datetime,
) -> CostRecognitionFact:
    repo = CostRecognitionFactRepository(session)
    old = repo.get(superseded_fact_id)
    if old is None:
        raise WholeFactSupersessionError(f"CostRecognitionFact {superseded_fact_id} not found")
    if old.superseded_by_fact_id is not None:
        raise WholeFactSupersessionError(f"CostRecognitionFact {superseded_fact_id} is already superseded")
    _require_fragment(session, source_fragment_id)
    if source_fragment_id == old.source_fragment_id:
        raise WholeFactSupersessionError(
            "supersession requires genuinely NEW Evidence — the same fragment that asserted the fact being "
            "superseded cannot also supersede it"
        )
    # Replacement legality: the SAME closed basis set every other
    # CostRecognitionFact writer enforces — a supersession never widens
    # it, and never invents a basis of its own.
    if basis not in _VALID_COST_RECOGNITION_BASIS:
        raise WholeFactSupersessionError(f"unsupported CostRecognitionFact basis {basis!r}")
    # Provenance must stay on THIS contract — a Shipment of another
    # Contract is a different business fact, never a correction.
    if shipment_id is not None:
        _require_same_contract_shipment(session, shipment_id, old.contract_id)
    # Compatible business scope: the SAME Contract this fact is about —
    # never a cross-business replacement.
    new_fact = CostRecognitionFact(
        id=uuid.uuid4(), contract_id=old.contract_id, recognition_date=recognition_date, basis=basis,
        source_fragment_id=source_fragment_id, created_at=created_at, shipment_id=shipment_id,
    )
    repo.add(new_fact)
    session.flush()
    if not repo.mark_superseded(old.id, superseded_by_fact_id=new_fact.id):
        raise WholeFactSupersessionError(
            f"CostRecognitionFact {superseded_fact_id} was superseded concurrently — refusing a second supersession"
        )
    session.flush()
    return new_fact


def supersede_accrual_basis_fact(
    session: Session,
    *,
    superseded_fact_id: uuid.UUID,
    estimated_cost,
    basis: str,
    source_fragment_id: uuid.UUID,
    quantity=None,
    created_at: datetime,
) -> AccrualBasisFact:
    repo = AccrualBasisFactRepository(session)
    old = repo.get(superseded_fact_id)
    if old is None:
        raise WholeFactSupersessionError(f"AccrualBasisFact {superseded_fact_id} not found")
    if old.superseded_by_fact_id is not None:
        raise WholeFactSupersessionError(f"AccrualBasisFact {superseded_fact_id} is already superseded")
    _require_fragment(session, source_fragment_id)
    if source_fragment_id == old.source_fragment_id:
        raise WholeFactSupersessionError(
            "supersession requires genuinely NEW Evidence — the same fragment that asserted the fact being "
            "superseded cannot also supersede it"
        )
    # Compatible scope: same Contract, same scope_type, and — if
    # item-scoped — the SAME ContractItem. Retargeting to a different
    # contract/item/scope is a new Fact, never a "correction".
    new_fact = AccrualBasisFact(
        id=uuid.uuid4(), scope_type=old.scope_type, contract_id=old.contract_id,
        contract_item_id=old.contract_item_id, quantity=quantity, estimated_cost=estimated_cost, basis=basis,
        source_fragment_id=source_fragment_id, created_at=created_at,
    )
    repo.add(new_fact)
    session.flush()
    if not repo.mark_superseded(old.id, superseded_by_fact_id=new_fact.id):
        raise WholeFactSupersessionError(
            f"AccrualBasisFact {superseded_fact_id} was superseded concurrently — refusing a second supersession"
        )
    session.flush()
    return new_fact


def supersede_historical_accrual_fact(
    session: Session,
    *,
    superseded_fact_id: uuid.UUID,
    quantity,
    estimated_cost,
    basis: str,
    source_fragment_id: uuid.UUID,
    confirmed_at: datetime,
) -> HistoricalAccrualFact:
    repo = HistoricalAccrualFactRepository(session)
    old = repo.get(superseded_fact_id)
    if old is None:
        raise WholeFactSupersessionError(f"HistoricalAccrualFact {superseded_fact_id} not found")
    if old.superseded_by_fact_id is not None:
        raise WholeFactSupersessionError(f"HistoricalAccrualFact {superseded_fact_id} is already superseded")
    _require_fragment(session, source_fragment_id)
    if source_fragment_id == old.source_fragment_id:
        raise WholeFactSupersessionError(
            "supersession requires genuinely NEW Evidence — the same fragment that asserted the fact being "
            "superseded cannot also supersede it"
        )
    # Compatible scope: the SAME ContractItem and the SAME source_period
    # — a different period is a different historical business fact, not
    # a correction of this one.
    new_fact = HistoricalAccrualFact(
        id=uuid.uuid4(), source_period=old.source_period, contract_item_id=old.contract_item_id, quantity=quantity,
        estimated_cost=estimated_cost, basis=basis, source_fragment_id=source_fragment_id, confirmed_at=confirmed_at,
    )
    repo.add(new_fact)
    session.flush()
    if not repo.mark_superseded(old.id, superseded_by_fact_id=new_fact.id):
        raise WholeFactSupersessionError(
            f"HistoricalAccrualFact {superseded_fact_id} was superseded concurrently — refusing a second "
            "supersession"
        )
    session.flush()
    return new_fact


def supersede_invoice_item_allocation(
    session: Session,
    *,
    superseded_fact_id: uuid.UUID,
    allocated_quantity,
    allocated_net_amount,
    confirmation_type: str,
    source_fragment_id: uuid.UUID,
    created_at: datetime,
) -> InvoiceItemAllocation:
    repo = InvoiceItemAllocationRepository(session)
    old = repo.get(superseded_fact_id)
    if old is None:
        raise WholeFactSupersessionError(f"InvoiceItemAllocation {superseded_fact_id} not found")
    if old.superseded_by_fact_id is not None:
        raise WholeFactSupersessionError(f"InvoiceItemAllocation {superseded_fact_id} is already superseded")
    _require_fragment(session, source_fragment_id)
    if source_fragment_id == old.source_fragment_id:
        raise WholeFactSupersessionError(
            "supersession requires genuinely NEW Evidence — the same fragment that asserted the fact being "
            "superseded cannot also supersede it"
        )
    # The replacement is an InvoiceItemAllocation like any other: it
    # passes the SAME section-11 constraints, and the closed
    # confirmation set, every other allocation writer passes.
    if confirmation_type not in _LEGAL_ITEM_ALLOCATION_CONFIRMATION_TYPES:
        raise WholeFactSupersessionError(
            f"unsupported InvoiceItemAllocation confirmation_type {confirmation_type!r} — "
            f"allowed: {sorted(_LEGAL_ITEM_ALLOCATION_CONFIRMATION_TYPES)}"
        )
    invoice_item = InvoiceItemRepository(session).get(old.invoice_item_id)
    contract_item = ContractItemRepository(session).get(old.contract_item_id)
    if invoice_item is None or contract_item is None:
        raise WholeFactSupersessionError(
            f"InvoiceItemAllocation {superseded_fact_id} references a missing invoice item or contract item"
        )
    # Capacity base EXCLUDES the allocation being superseded: its
    # quantity is being handed back by this very operation, so counting
    # it as still-committed would make every like-for-like correction of
    # a fully-allocated line impossible.
    existing_allocated_quantity = sum(
        (
            a.allocated_quantity
            for a in repo.list_for_invoice_item(old.invoice_item_id)
            if a.id != old.id
        ),
        Decimal("0"),
    )
    _require_valid_item_allocation(
        session,
        invoice_item=invoice_item,
        contract_item=contract_item,
        allocated_quantity=allocated_quantity,
        allocated_net_amount=allocated_net_amount,
        existing_allocated_quantity=existing_allocated_quantity,
    )
    # Compatible scope: the SAME (invoice_item, contract_item) pair —
    # retargeting either side is a new allocation, never a correction.
    new_fact = InvoiceItemAllocation(
        id=uuid.uuid4(), invoice_item_id=old.invoice_item_id, contract_item_id=old.contract_item_id,
        allocated_quantity=allocated_quantity, allocated_net_amount=allocated_net_amount,
        confirmation_type=confirmation_type, source_fragment_id=source_fragment_id, created_at=created_at,
    )
    repo.add(new_fact)
    session.flush()
    if not repo.mark_superseded(old.id, superseded_by_fact_id=new_fact.id):
        raise WholeFactSupersessionError(
            f"InvoiceItemAllocation {superseded_fact_id} was superseded concurrently — refusing a second "
            "supersession"
        )
    session.flush()
    return new_fact
