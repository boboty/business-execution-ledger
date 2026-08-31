"""CLI smoke test for `bel period-close export` — real SQLite file via
subprocess, same convention as test_phase2b_cli.py. Confirms the CLI
writes a real file (never stdout binary) using the SAME Application
Data Product path Web uses.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import openpyxl

from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
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
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
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
    engine = make_engine(f"sqlite:///{db_path}")
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


def _seed_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "bel.db"
    Base.metadata.create_all(make_engine(f"sqlite:///{db_path}"))
    contracts, invoices, facts = _setup_files(tmp_path)
    assert _run_bel(db_path, "import-contract-ledger", str(contracts)).returncode == 0
    assert _run_bel(db_path, "import-invoices", str(invoices), "--direction", "purchase").returncode == 0
    _confirm_contract_allocations(db_path)
    assert _run_bel(db_path, "import-close-facts", str(facts)).returncode == 0
    return db_path


def test_cli_export_xlsx(tmp_path):
    db_path = _seed_db(tmp_path)
    out = tmp_path / "period-close-2031-03.xlsx"
    result = _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--format", "xlsx", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert str(out) in result.stdout
    assert out.exists()
    # stdout never carries binary XLSX content.
    assert "PK" not in result.stdout

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == [
        "01_Summary",
        "02_Accrual_Required",
        "03_Prior_Accrual_Reversal",
        "04_Actual_Difference",
        "05_Contract_Level_Candidate",
        "06_Blockers",
    ]


def test_cli_export_csv(tmp_path):
    db_path = _seed_db(tmp_path)
    out = tmp_path / "period-close-2031-03.csv"
    result = _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--format", "csv", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert out.exists()

    text = out.read_text(encoding="utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert rows
    assert {r["record_type"] for r in rows} == {
        "ACCRUAL_REQUIRED",
        "PRIOR_ACCRUAL_REVERSAL",
        "ACTUAL_DIFFERENCE",
        "CONTRACT_LEVEL_CANDIDATE",
        "BLOCKER",
    }


def test_cli_export_requires_format_and_output(tmp_path):
    db_path = _seed_db(tmp_path)
    missing_format = _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--output", str(tmp_path / "x.csv"))
    assert missing_format.returncode != 0

    missing_output = _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--format", "csv")
    assert missing_output.returncode != 0


def test_web_and_cli_export_are_byte_identical(tmp_path):
    """Web and CLI must be the SAME Application Data Product path — given
    the identical database, the CSV bytes they each produce must match
    exactly (never two independent business computations)."""
    from fastapi.testclient import TestClient

    from bel.web.app import create_app

    db_path = _seed_db(tmp_path)
    cli_out = tmp_path / "period-close-2031-03-cli.csv"
    result = _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--format", "csv", "--output", str(cli_out))
    assert result.returncode == 0, result.stderr

    app = create_app(f"sqlite:///{db_path}")
    client = TestClient(app)
    web_response = client.get(f"/period-close/export.csv?period={CLOSE_PERIOD}")
    assert web_response.status_code == 200

    assert cli_out.read_bytes() == web_response.content


def test_cli_export_is_read_only(tmp_path):
    db_path = _seed_db(tmp_path)
    engine = make_engine(f"sqlite:///{db_path}")

    def _counts():
        from bel.infrastructure.persistence.models import AccrualModel, AccrualReversalModel, ContractModel

        with make_session_factory(engine)() as session:
            return {
                "contracts": session.query(ContractModel).count(),
                "accruals": session.query(AccrualModel).count(),
                "accrual_reversals": session.query(AccrualReversalModel).count(),
            }

    before = _counts()
    out = tmp_path / "period-close-2031-03.xlsx"
    _run_bel(db_path, "period-close", "export", CLOSE_PERIOD, "--format", "xlsx", "--output", str(out))
    after = _counts()
    assert before == after
