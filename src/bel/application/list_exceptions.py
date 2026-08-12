from __future__ import annotations

from sqlalchemy.orm import Session

from bel.domain.exception import TaskException
from bel.infrastructure.persistence.repositories import ExceptionRepository


def list_exceptions(session: Session, open_only: bool = True) -> list[TaskException]:
    repo = ExceptionRepository(session)
    return repo.list_open() if open_only else repo.list_all()
