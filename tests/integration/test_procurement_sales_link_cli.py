"""CLI smoke test for `bel sales-link` (Phase 2D.1-R3a Slice 2) against a
real migrated SQLite file — same rationale as test_sales_contract_cli.py:
prove the actual `bel` entry point commits real state, not just that the
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


def _upgrade_head(db_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _seed_contract_and_sales_contract(db_path: Path) -> tuple[uuid.UUID, uuid.UUID]:
    engine = make_engine(str(db_path))
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        doc = EvidenceDocument(id=uuid.uuid4(), file_name="seed.json", sha256="1" * 64, source_type="test", imported_at=NOW)
        EvidenceRepository(session).add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=NOW,
        )
        EvidenceRepository(session).add_fragment(frag)
        session.flush()
        contract = Contract(
            id=uuid.uuid4(), contract_no="C-PSL-CLI-1", contract_type=None, counterparty="Supplier",
            buyer="Buyer Co", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.commit()

        from bel.application.sales_contract_facts import create_sales_contract_fact

        sales_contract = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-PSL-CLI-1", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        session.commit()
        return contract.id, sales_contract.id


def test_sales_link_cli_add_history_invalidate_reestablish_flow(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    contract_id, sales_contract_id = _seed_contract_and_sales_contract(db_path)

    add = _run_bel(db_path, "sales-link", "add", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    assert add.returncode == 0, add.stderr
    assert "created" in add.stdout
    link_id = add.stdout.split()[1]

    replay = _run_bel(db_path, "sales-link", "add", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    assert replay.returncode == 0, replay.stderr
    assert "already exists (exact replay" in replay.stdout

    history = _run_bel(db_path, "sales-link", "history", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    assert history.returncode == 0, history.stderr
    assert "[CURRENT]" in history.stdout
    assert link_id in history.stdout

    listing = _run_bel(db_path, "sales-link", "list", "--procurement-contract", str(contract_id))
    assert listing.returncode == 0, listing.stderr
    assert link_id in listing.stdout

    invalidate = _run_bel(db_path, "sales-link", "invalidate", "--superseded-link", link_id)
    assert invalidate.returncode == 0, invalidate.stderr
    assert "invalidated" in invalidate.stdout

    listing2 = _run_bel(db_path, "sales-link", "list", "--procurement-contract", str(contract_id))
    assert "No current ProcurementSalesLinks" in listing2.stdout

    # ADD on a retired pair must be rejected — REESTABLISH is required.
    add_after_retire = _run_bel(db_path, "sales-link", "add", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    assert add_after_retire.returncode == 1
    assert "REESTABLISH" in add_after_retire.stdout

    reestablish = _run_bel(db_path, "sales-link", "reestablish", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    assert reestablish.returncode == 0, reestablish.stderr
    assert "reestablished" in reestablish.stdout
    new_link_id = reestablish.stdout.split()[1]
    assert new_link_id != link_id

    history2 = _run_bel(db_path, "sales-link", "history", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_id))
    lines = [l for l in history2.stdout.splitlines() if l.strip().startswith(link_id) or l.strip().startswith(new_link_id)]
    assert len(lines) == 2
    assert any("[CURRENT]" in l and new_link_id in l for l in lines)
    assert any("retired by correction" in l and link_id in l for l in lines)


def test_sales_link_cli_correct_with_replacement(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    contract_id, sales_contract_x_id = _seed_contract_and_sales_contract(db_path)

    engine = make_engine(str(db_path))
    with make_session_factory(engine)() as session:
        from bel.infrastructure.persistence.repositories import SalesContractRepository

        current = SalesContractRepository(session).get(sales_contract_x_id)
        from bel.application.sales_contract_facts import create_sales_contract_fact

        sales_contract_y = create_sales_contract_fact(
            session, our_entity="Entity A", sales_contract_no="SC-PSL-CLI-2", fields={},
            source_fragment_id=current.current_source_fragment_id, created_at=NOW,
        ).sales_contract
        session.commit()
        sales_contract_y_id = sales_contract_y.id

    add = _run_bel(db_path, "sales-link", "add", "--procurement-contract", str(contract_id), "--sales-contract", str(sales_contract_x_id))
    assert add.returncode == 0, add.stderr
    link_id = add.stdout.split()[1]

    correct = _run_bel(
        db_path, "sales-link", "correct", "--superseded-link", link_id,
        "--replacement-procurement-contract", str(contract_id), "--replacement-sales-contract", str(sales_contract_y_id),
    )
    assert correct.returncode == 0, correct.stderr
    assert "corrected" in correct.stdout
    assert "replacement=" in correct.stdout

    listing_x = _run_bel(db_path, "sales-link", "list", "--sales-contract", str(sales_contract_x_id))
    assert "No current ProcurementSalesLinks" in listing_x.stdout
    listing_y = _run_bel(db_path, "sales-link", "list", "--sales-contract", str(sales_contract_y_id))
    assert "No current ProcurementSalesLinks" not in listing_y.stdout
