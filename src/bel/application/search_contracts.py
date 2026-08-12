from __future__ import annotations

from sqlalchemy.orm import Session

from bel.domain.contract import Contract
from bel.infrastructure.persistence.repositories import ContractRepository


def search_contracts_by_no(session: Session, contract_no: str) -> list[Contract]:
    """contract_no is a business key, not unique — this can return more
    than one Contract. See docs/DOMAIN.md."""
    return ContractRepository(session).find_by_contract_no(contract_no)
