from __future__ import annotations

from sqlalchemy.orm import Session

from bel.domain.matching import MatchCase
from bel.infrastructure.persistence.repositories import MatchCaseRepository


def list_match_cases(session: Session, status: str | None = None) -> list[MatchCase]:
    repo = MatchCaseRepository(session)
    return repo.list_by_status(status) if status else repo.list_all()
