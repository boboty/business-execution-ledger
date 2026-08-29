"""CLI smoke test for `bel contract-item` (Phase 2D.1-R1) against a real
migrated SQLite file — same rationale as test_phase2b_cli.py: prove the
actual `bel` entry point commits real state, not just that the
application function works in-process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository

REPO_ROOT = Path(__file__).parent.parent.parent
NOW = datetime.now(timezone.utc)


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", "--db", str(db_path), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _seed_contract(db_path: Path) -> uuid.UUID:
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="seed.json", sha256="c" * 64, source_type="test", imported_at=NOW)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(),
            evidence_document_id=doc.id,
            fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None,
            row_number=None,
            locator_json={},
            raw_data={},
            created_at=NOW,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(),
            contract_no="C-CLI-1",
            contract_type=None,
            counterparty="Supplier",
            buyer="Buyer Co",
            gross_amount=Decimal("1000.00"),
            currency="CNY",
            contract_date=None,
            current_source_fragment_id=frag.id,
            created_at=NOW,
            updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.commit()
        return contract.id


def test_contract_item_cli_create_supplement_correct_flow(tmp_path):
    db_path = tmp_path / "bel.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    contract_id = _seed_contract(db_path)

    create = _run_bel(
        db_path, "contract-item", "create", "--contract", str(contract_id), "--item", "ITEM-A", "--product-name", "Widget"
    )
    assert create.returncode == 0, create.stderr
    assert "created" in create.stdout
    item_id = create.stdout.split()[1]

    # Replay is idempotent — resolves to the existing anchor, not a duplicate.
    replay = _run_bel(
        db_path, "contract-item", "create", "--contract", str(contract_id), "--item", "ITEM-A", "--product-name", "Widget"
    )
    assert replay.returncode == 0, replay.stderr
    assert "already exists" in replay.stdout

    show = _run_bel(db_path, "contract-item", "show", item_id)
    assert show.returncode == 0, show.stderr
    assert "product_name:               Widget" in show.stdout
    assert "quantity:                   None" in show.stdout

    history = _run_bel(db_path, "contract-item", "history", item_id)
    assert history.returncode == 0, history.stderr
    assert "[INITIAL]" in history.stdout
    assert "[CURRENT]" in history.stdout
    revision_id = history.stdout.splitlines()[1].split()[0]

    supplement = _run_bel(
        db_path,
        "contract-item",
        "supplement",
        "--item-id",
        item_id,
        "--based-on",
        revision_id,
        "--quantity",
        "10",
    )
    assert supplement.returncode == 0, supplement.stderr
    assert "supplemented" in supplement.stdout

    history2 = _run_bel(db_path, "contract-item", "history", item_id)
    lines = [l for l in history2.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    assert len(lines) == 2
    current_revision_id = [l for l in lines if "[CURRENT]" in l][0].split()[0]

    # Supplementing an already-known, DIFFERENT value is rejected.
    conflict = _run_bel(
        db_path,
        "contract-item",
        "supplement",
        "--item-id",
        item_id,
        "--based-on",
        current_revision_id,
        "--product-name",
        "SomethingElse",
    )
    assert conflict.returncode == 1
    assert "use correction" in conflict.stdout

    correct = _run_bel(
        db_path,
        "contract-item",
        "correct",
        "--item-id",
        item_id,
        "--based-on",
        current_revision_id,
        "--quantity",
        "12",
    )
    assert correct.returncode == 0, correct.stderr
    assert "corrected" in correct.stdout

    show2 = _run_bel(db_path, "contract-item", "show", item_id)
    assert "quantity:                   12" in show2.stdout

    history3 = _run_bel(db_path, "contract-item", "history", item_id)
    lines3 = [l for l in history3.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    assert len(lines3) == 3
