import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from bel.domain.contract import Contract
from bel.infrastructure.persistence.repositories import ContractRepository


def test_contract_with_nonexistent_fragment_id_is_rejected(db_session):
    """current_source_fragment_id must be an enforced invariant, not just
    a declared FK that SQLite silently ignores — every Contract must be
    traceable to a real EvidenceFragment, not only the ones this
    importer happens to create. See docs/PHASE1-DECISIONS.md."""
    now = datetime.now(timezone.utc)
    orphan = Contract(
        id=uuid.uuid4(),
        contract_no="ORPHAN-001",
        contract_type=None,
        counterparty="Seller",
        buyer="Buyer",
        gross_amount=Decimal("1.00"),
        currency="CNY",
        contract_date=None,
        current_source_fragment_id=uuid.uuid4(),  # does not exist
        created_at=now,
        updated_at=now,
    )
    ContractRepository(db_session).add(orphan)

    with pytest.raises(IntegrityError):
        db_session.commit()
