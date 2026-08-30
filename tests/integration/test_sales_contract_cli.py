"""CLI smoke test for `bel sales-contract` (Phase 2D.1-R3a Slice 1)
against a real migrated SQLite file — same rationale as
test_shipment_cli.py: prove the actual `bel` entry point commits real
state, not just that the application function works in-process.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bel.infrastructure.persistence.database import make_engine
from bel.infrastructure.persistence.models import Base

REPO_ROOT = Path(__file__).parent.parent.parent


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _upgrade_head(db_path: Path) -> None:
    Base.metadata.create_all(make_engine(f"sqlite:///{db_path}"))


def test_sales_contract_cli_create_supplement_correct_list_flow(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)

    create = _run_bel(
        db_path, "sales-contract", "create", "--our-entity", "Entity A", "--sales-contract-no", "SC-CLI-1",
        "--currency", "USD", "--gross-amount", "1000.00", "--contract-date", "2031-03-10",
    )
    assert create.returncode == 0, create.stderr
    assert "created" in create.stdout
    sales_contract_id = create.stdout.split()[1]

    # Replay is idempotent — resolves to the existing anchor, not a duplicate.
    replay = _run_bel(
        db_path, "sales-contract", "create", "--our-entity", "Entity A", "--sales-contract-no", "SC-CLI-1",
        "--currency", "USD", "--gross-amount", "1000.00", "--contract-date", "2031-03-10",
    )
    assert replay.returncode == 0, replay.stderr
    assert "already exists" in replay.stdout

    show = _run_bel(db_path, "sales-contract", "show", sales_contract_id)
    assert show.returncode == 0, show.stderr
    assert "customer:                   None" in show.stdout
    assert "currency:                   USD" in show.stdout

    history = _run_bel(db_path, "sales-contract", "history", sales_contract_id)
    assert history.returncode == 0, history.stderr
    assert "[INITIAL]" in history.stdout
    assert "[CURRENT]" in history.stdout
    lines = [l for l in history.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    current_revision_id = lines[0].split()[0]

    # customer is already NULL, so supplementing it is the intended path
    # (never a correction — there is no existing value to correct yet).
    supplement = _run_bel(
        db_path, "sales-contract", "supplement", "--sales-contract-id", sales_contract_id,
        "--based-on", current_revision_id, "--customer", "Customer Co",
    )
    assert supplement.returncode == 0, supplement.stderr
    assert "supplemented" in supplement.stdout

    show2 = _run_bel(db_path, "sales-contract", "show", sales_contract_id)
    assert "customer:                   Customer Co" in show2.stdout

    history2 = _run_bel(db_path, "sales-contract", "history", sales_contract_id)
    lines2 = [l for l in history2.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    current_revision_id_2 = [l.split()[0] for l in lines2 if "[CURRENT]" in l][0]

    correct = _run_bel(
        db_path, "sales-contract", "correct", "--sales-contract-id", sales_contract_id,
        "--based-on", current_revision_id_2, "--gross-amount", "1200.00",
    )
    assert correct.returncode == 0, correct.stderr
    assert "corrected" in correct.stdout

    show3 = _run_bel(db_path, "sales-contract", "show", sales_contract_id)
    assert "gross_amount:               1200.00" in show3.stdout

    history3 = _run_bel(db_path, "sales-contract", "history", sales_contract_id)
    lines3 = [l for l in history3.stdout.splitlines() if "[INITIAL]" in l or "[SUPPLEMENT]" in l or "[CORRECTION]" in l]
    assert len(lines3) == 3

    listing = _run_bel(db_path, "sales-contract", "list")
    assert listing.returncode == 0, listing.stderr
    assert sales_contract_id in listing.stdout


def test_sales_contract_cli_missing_identity_creates_no_anchor(tmp_path):
    """Phase 2D.1-R3a section 4.4, exercised through the real `bel`
    entry point: omitting --sales-contract-no (or --our-entity) must
    fail unconditionally — there is no confirmation override, unlike
    Shipment's nullable external_reference."""
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)

    missing_no = _run_bel(
        db_path, "sales-contract", "create", "--our-entity", "Entity A", "--currency", "USD",
    )
    assert missing_no.returncode == 1
    assert "missing our_entity and/or sales_contract_no" in missing_no.stdout

    missing_entity = _run_bel(
        db_path, "sales-contract", "create", "--sales-contract-no", "SC-CLI-2", "--currency", "USD",
    )
    assert missing_entity.returncode == 1
    assert "missing our_entity and/or sales_contract_no" in missing_entity.stdout

    listing = _run_bel(db_path, "sales-contract", "list")
    assert "No SalesContracts" in listing.stdout  # no anchor was created either way


def test_sales_contract_cli_conflicting_customer_rejected(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)

    create = _run_bel(
        db_path, "sales-contract", "create", "--our-entity", "Entity A", "--sales-contract-no", "SC-CLI-3",
        "--customer", "Customer Co",
    )
    assert create.returncode == 0, create.stderr
    sales_contract_id = create.stdout.split()[1]

    history = _run_bel(db_path, "sales-contract", "history", sales_contract_id)
    current_revision_id = [
        l.split()[0] for l in history.stdout.splitlines() if "[CURRENT]" in l
    ][0]

    conflict = _run_bel(
        db_path, "sales-contract", "supplement", "--sales-contract-id", sales_contract_id,
        "--based-on", current_revision_id, "--customer", "A Different Customer",
    )
    assert conflict.returncode == 1
    assert "already known as" in conflict.stdout

    show = _run_bel(db_path, "sales-contract", "show", sales_contract_id)
    assert "customer:                   Customer Co" in show.stdout  # unchanged
