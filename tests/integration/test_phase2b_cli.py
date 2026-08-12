"""CLI smoke test for the Phase 2B commands against a real SQLite file —
the same lesson as Phase 2A: a purely golden-driven check would not catch
a CLI that silently commits nothing (see docs/PHASE2A-DECISIONS.md).

Uses subprocess so the actual `bel` entry point and the real migrated DB
are exercised, independent of the pytest in-memory session.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
)
from fixtures.synthetic import scenarios
from fixtures.synthetic.phase2b_close import CLOSE_FACT_PACK
from tests.conftest import write_invoice_workbook, write_ledger_workbook

REPO_ROOT = Path(__file__).parent.parent.parent
CLOSE_PERIOD = "2031-03"


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", "--db", str(db_path), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _setup_files(tmp_path: Path):
    from fixtures.synthetic.phase2b_close import PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS, PHASE2B_INVOICE_ROWS

    contracts = tmp_path / "contracts.xlsx"
    invoices = tmp_path / "invoices.xlsx"
    facts = tmp_path / "close-facts.json"
    write_ledger_workbook(contracts, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
    write_invoice_workbook(invoices, scenarios.BUYER, PHASE2B_INVOICE_ROWS)
    facts.write_text(json.dumps(CLOSE_FACT_PACK, indent=2))
    return contracts, invoices, facts


def _confirm_contract_allocations(db_path: Path) -> None:
    engine = make_engine(str(db_path))
    now = datetime.now(timezone.utc)
    with make_session_factory(engine)() as session:
        for external_key, contract_no in [
            ("DIGITAL-CLOSE-001", "PO-CLOSE-001"),
            ("DIGITAL-CLOSE-002", "PO-CLOSE-002"),
            ("DIGITAL-CLOSE-005", "PO-CLOSE-005"),
            ("DIGITAL-CLOSE-006", "PO-CLOSE-006"),
        ]:
            invoice = InvoiceRepository(session).find_by_external_key(external_key)
            contract = next(c for c in ContractRepository(session).list_all() if c.contract_no == contract_no)
            match_case = MatchCase(
                id=uuid.uuid4(),
                subject_type="INVOICE",
                subject_id=invoice.id,
                status=MatchCaseStatus.AUTO_CONFIRMED,
                match_method=MatchMethod.M001,
                created_at=now,
                resolved_at=now,
            )
            MatchCaseRepository(session).add(match_case)
            session.flush()
            InvoiceAllocationRepository(session).add(
                InvoiceAllocation(
                    id=uuid.uuid4(),
                    invoice_id=invoice.id,
                    contract_id=contract.id,
                    match_case_id=match_case.id,
                    allocated_gross_amount=invoice.gross_amount,
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED,
                    created_at=now,
                )
            )
        session.commit()


def test_phase2b_cli_flow(tmp_path):
    db_path = tmp_path / "bel.db"
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )
    assert upgrade.returncode == 0, upgrade.stderr

    contracts, invoices, facts = _setup_files(tmp_path)

    first_import = _run_bel(db_path, "import-contract-ledger", str(contracts))
    assert first_import.returncode == 0, first_import.stderr
    assert "contracts created: 8" in first_import.stdout

    invoice_import = _run_bel(db_path, "import-invoices", str(invoices), "--direction", "purchase")
    assert invoice_import.returncode == 0, invoice_import.stderr

    _confirm_contract_allocations(db_path)

    facts_import = _run_bel(db_path, "import-close-facts", str(facts))
    assert facts_import.returncode == 0, facts_import.stderr
    assert "accruals:                  4 created / 0 skipped" in facts_import.stdout

    # Re-import is idempotent through the CLI too.
    reimport = _run_bel(db_path, "import-close-facts", str(facts))
    assert "re-import" in reimport.stdout

    preview = _run_bel(db_path, "period-close", "preview", CLOSE_PERIOD)
    assert preview.returncode == 0, preview.stderr
    assert "prior_accrual_reversals: 2" in preview.stdout
    assert "new_accrual_requirements: 1" in preview.stdout
    assert "contract_level_candidates: 2" in preview.stdout
    assert "accrual_actual_differences: 2" in preview.stdout
    assert "ITEM_MATCH_REQUIRED_FOR_REVERSAL" in preview.stdout
    assert "MISSING_ACCRUAL_BASIS" in preview.stdout

    # Preview is stateless: a second run prints the same summary.
    preview_again = _run_bel(db_path, "period-close", "preview", CLOSE_PERIOD)
    assert preview_again.stdout == preview.stdout

    accrual_list = _run_bel(db_path, "accrual", "list")
    assert accrual_list.returncode == 0, accrual_list.stderr
    assert "[PARTIALLY_REVERSED]" in accrual_list.stdout
    assert "[ACTIVE]" in accrual_list.stdout
