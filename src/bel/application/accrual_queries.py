"""Accrual queries backing ``bel accrual list`` and ``bel accrual get``.

Balance and status are always DERIVED from the original Accrual minus its
AccrualReversals (sections 8-9) — the stored status column is only a
cache updated through the same domain function.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from bel.domain.accrual import Accrual, AccrualReversal, get_accrual_balance, get_projected_accrual_status
from bel.infrastructure.persistence.repositories import AccrualReversalRepository, AccrualRepository


@dataclass(frozen=True)
class AccrualView:
    accrual: Accrual
    remaining_quantity: Decimal
    remaining_estimated_cost: Decimal
    reversed_quantity: Decimal
    reversed_estimated_cost: Decimal
    projected_status: str
    reversals: list[AccrualReversal]


def _view(session: Session, accrual: Accrual) -> AccrualView:
    reversal_repo = AccrualReversalRepository(session)
    reversals = reversal_repo.list_for_accrual(accrual.id)
    remaining_qty, remaining_cost, reversed_qty, reversed_cost = get_accrual_balance(accrual, reversals)
    return AccrualView(
        accrual=accrual,
        remaining_quantity=remaining_qty,
        remaining_estimated_cost=remaining_cost,
        reversed_quantity=reversed_qty,
        reversed_estimated_cost=reversed_cost,
        projected_status=get_projected_accrual_status(reversed_qty, remaining_qty),
        reversals=reversals,
    )


def list_accrual_views(session: Session) -> list[AccrualView]:
    return [_view(session, a) for a in AccrualRepository(session).list_all()]


def get_accrual_view(session: Session, accrual_id: uuid.UUID) -> AccrualView | None:
    accrual = AccrualRepository(session).get(accrual_id)
    if accrual is None:
        return None
    return _view(session, accrual)
