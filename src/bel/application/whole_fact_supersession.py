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
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy.orm import Session

from bel.domain.accrual import AccrualBasisFact, CostRecognitionFact, HistoricalAccrualFact, InvoiceItemAllocation
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
)


class WholeFactSupersessionError(ValueError):
    """A rejected supersession attempt — the old fact is missing/already
    superseded, the new Evidence is not genuinely new, the business
    scope is incompatible, or a concurrent supersession won the race."""


def _require_fragment(session: Session, source_fragment_id: uuid.UUID) -> None:
    if EvidenceRepository(session).get_fragment(source_fragment_id) is None:
        raise WholeFactSupersessionError(f"EvidenceFragment {source_fragment_id} not found")


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
