"""CLI smoke test for `bel shipment` (Phase 2D.1-R2) against a real
migrated SQLite file — same rationale as test_contract_item_cli.py: prove
the actual `bel` entry point commits real state, not just that the
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
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="seed.json", sha256="d" * 64, source_type="test", imported_at=NOW)
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
            contract_no="C-SHIP-CLI-1",
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


def test_shipment_cli_create_supplement_correct_list_flow(tmp_path):
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
        db_path, "shipment", "create", "--contract", str(contract_id), "--execution-date", "2031-03-10",
        "--external-ref", "EXP-001", "--quantity", "100",
    )
    assert create.returncode == 0, create.stderr
    assert "created" in create.stdout
    shipment_id = create.stdout.split()[1]

    # Replay is idempotent — resolves to the existing anchor, not a duplicate.
    replay = _run_bel(
        db_path, "shipment", "create", "--contract", str(contract_id), "--execution-date", "2031-03-10",
        "--external-ref", "EXP-001", "--quantity", "100",
    )
    assert replay.returncode == 0, replay.stderr
    assert "already exists" in replay.stdout

    show = _run_bel(db_path, "shipment", "show", shipment_id)
    assert show.returncode == 0, show.stderr
    assert "external_reference:         EXP-001" in show.stdout
    assert "quantity:                   100" in show.stdout

    history = _run_bel(db_path, "shipment", "history", shipment_id)
    assert history.returncode == 0, history.stderr
    assert "[INITIAL]" in history.stdout
    assert "[CURRENT]" in history.stdout
    lines = [l for l in history.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    current_revision_id = lines[0].split()[0]

    supplement = _run_bel(
        db_path, "shipment", "supplement", "--shipment-id", shipment_id, "--based-on", current_revision_id,
        "--item", str(uuid.uuid4()),
    )
    # Unknown ContractItem -> rejected explicitly, never silently ignored.
    assert supplement.returncode == 1
    assert "not found" in supplement.stdout

    # A real supplement (quantity is already known, so this is a conflict — use quantity via correct instead).
    correct = _run_bel(
        db_path, "shipment", "correct", "--shipment-id", shipment_id, "--based-on", current_revision_id,
        "--quantity", "120",
    )
    assert correct.returncode == 0, correct.stderr
    assert "corrected" in correct.stdout

    show2 = _run_bel(db_path, "shipment", "show", shipment_id)
    assert "quantity:                   120" in show2.stdout

    history2 = _run_bel(db_path, "shipment", "history", shipment_id)
    lines2 = [l for l in history2.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    assert len(lines2) == 2

    listing = _run_bel(db_path, "shipment", "list", "--contract", str(contract_id))
    assert listing.returncode == 0, listing.stderr
    assert shipment_id in listing.stdout


def test_shipment_cli_null_reference_requires_explicit_confirmation(tmp_path):
    """Phase 2D.1-R2 Codex fix round, BLOCKER 1, exercised through the
    real `bel` entry point: omitting --external-ref must fail without
    --confirm-incomplete-identity, and succeed with it."""
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

    unconfirmed = _run_bel(
        db_path, "shipment", "create", "--contract", str(contract_id), "--execution-date", "2031-03-15",
        "--quantity", "50",
    )
    assert unconfirmed.returncode == 1
    assert "identity incomplete" in unconfirmed.stdout

    listing = _run_bel(db_path, "shipment", "list", "--contract", str(contract_id))
    assert "No Shipments" in listing.stdout  # no anchor was created

    confirmed = _run_bel(
        db_path, "shipment", "create", "--contract", str(contract_id), "--execution-date", "2031-03-15",
        "--quantity", "50", "--confirm-incomplete-identity",
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert "created" in confirmed.stdout

    listing2 = _run_bel(db_path, "shipment", "list", "--contract", str(contract_id))
    assert "No Shipments" not in listing2.stdout
