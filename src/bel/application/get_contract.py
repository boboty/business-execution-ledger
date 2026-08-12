from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository


@dataclass
class ContractTrace:
    """Contract Fact -> Source Fragment -> Workbook/Sheet/Row, per A02."""

    contract: Contract
    fragment: EvidenceFragment
    document: EvidenceDocument


def get_contract(session: Session, contract_id: uuid.UUID) -> ContractTrace | None:
    contract = ContractRepository(session).get(contract_id)
    if contract is None:
        return None

    evidence_repo = EvidenceRepository(session)
    fragment = evidence_repo.get_fragment(contract.current_source_fragment_id)
    if fragment is None:
        raise RuntimeError(
            f"data integrity error: contract {contract.id} references missing fragment "
            f"{contract.current_source_fragment_id}"
        )
    document = evidence_repo.get_document(fragment.evidence_document_id)
    if document is None:
        raise RuntimeError(
            f"data integrity error: fragment {fragment.id} references missing document "
            f"{fragment.evidence_document_id}"
        )

    return ContractTrace(contract=contract, fragment=fragment, document=document)
