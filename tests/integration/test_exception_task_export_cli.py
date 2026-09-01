"""CLI smoke test for `bel exceptions export` — real SQLite file via
subprocess, same convention as test_period_close_export_cli.py. Confirms
the CLI writes a real file using the SAME Application Data Product path
Web uses, accepts the same filters, is byte-identical to the Web export
for the same state/filters, and performs zero writes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.repositories import ExceptionRepository

REPO_ROOT = Path(__file__).parent.parent.parent
PERIOD = "2031-03"


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _seed_db(tmp_path: Path) -> Path:
    """Phase 2B synthetic DB plus one OPEN task and one HCR MatchCase —
    enough for the exceptions Data Product to carry all three sources when
    a period is requested (the phase2b fixture supplies the blocker)."""
    from bel.domain.invoice import InvoiceDirection
    from bel.domain.matching import MatchCase, MatchCaseStatus, MatchCandidate, MatchMethod, SubjectType
    from bel.infrastructure.persistence.repositories import (
        ContractRepository,
        InvoiceRepository,
        MatchCandidateRepository,
        MatchCaseRepository,
    )
    from tests.web.conftest import build_phase2b_db

    db_path = tmp_path / "exceptions-export.db"
    build_phase2b_db(db_path, tmp_path, with_payment=True)

    engine = make_engine(f"sqlite:///{db_path}")
    with make_session_factory(engine)() as session:
        contract = next(c for c in ContractRepository(session).list_all())
        invoice = next(i for i in InvoiceRepository(session).list_all() if i.direction == InvoiceDirection.PURCHASE)
        ExceptionRepository(session).add(
            TaskException(
                id=uuid.uuid4(),
                exception_type=ExceptionType.BUSINESS_KEY_CONFLICT,
                status=ExceptionStatus.OPEN,
                summary="CLI-EXPORT-TASK",
                detail={"contract_ids": [str(contract.id)]},
                created_at=datetime.now(timezone.utc),
            )
        )
        session.flush()
        case = MatchCase(
            id=uuid.uuid4(),
            subject_type=SubjectType.INVOICE,
            subject_id=invoice.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
            match_method=MatchMethod.M001,
            created_at=datetime.now(timezone.utc),
            resolved_at=None,
        )
        MatchCaseRepository(session).add(case)
        session.flush()
        MatchCandidateRepository(session).add(
            MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=contract.id, created_at=datetime.now(timezone.utc))
        )
        session.commit()
    return db_path


def test_cli_export_csv_writes_file_with_filters(tmp_path):
    db_path = _seed_db(tmp_path)
    out = tmp_path / "exceptions.csv"
    result = _run_bel(
        db_path,
        "exceptions", "export",
        "--format", "csv",
        "--output", str(out),
        "--period", PERIOD,
        "--source-type", "MATCH_CASE",
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    text = out.read_text(encoding="utf-8-sig")
    assert "record_type" in text.splitlines()[0]
    assert "CLI-EXPORT-TASK" not in text  # source_type filter excluded the task
    assert "INVOICE" in text  # the match summary remains


def test_cli_export_requires_format_and_output(tmp_path):
    db_path = _seed_db(tmp_path)
    missing_format = _run_bel(db_path, "exceptions", "export", "--output", str(tmp_path / "x.csv"))
    assert missing_format.returncode != 0
    missing_output = _run_bel(db_path, "exceptions", "export", "--format", "csv")
    assert missing_output.returncode != 0


def test_cli_export_invalid_period_is_clean_error(tmp_path):
    db_path = _seed_db(tmp_path)
    result = _run_bel(
        db_path,
        "exceptions", "export",
        "--format", "csv",
        "--output", str(tmp_path / "x.csv"),
        "--period", "2031-13",
    )
    assert result.returncode != 0
    assert "period must be YYYY-MM" in result.stderr


def test_web_and_cli_export_are_byte_identical(tmp_path):
    """Web and CLI must be the SAME Application Data Product path — given
    the identical database and filters, the CSV bytes they each produce
    must match exactly (never two independent business computations)."""
    from bel.application.exception_task_data_product import build_exception_task_data_product, export_exception_task_csv
    from bel.application.unresolved_work_center import UnresolvedWorkFilters, get_unresolved_work_center
    from bel.web.app import create_app

    db_path = _seed_db(tmp_path)
    cli_out = tmp_path / "exceptions-cli.csv"
    result = _run_bel(
        db_path,
        "exceptions", "export",
        "--format", "csv",
        "--output", str(cli_out),
        "--period", PERIOD,
    )
    assert result.returncode == 0, result.stderr

    app = create_app(f"sqlite:///{db_path}")
    client = TestClient(app)
    web_response = client.get(f"/exceptions/export.csv?period={PERIOD}")
    assert web_response.status_code == 200
    assert cli_out.read_bytes() == web_response.content

    # And both equal the direct application-layer serializer output.
    with app.state.session_factory() as session:
        center = get_unresolved_work_center(session, filters=UnresolvedWorkFilters(period=PERIOD))
    direct = export_exception_task_csv(build_exception_task_data_product(center))
    assert cli_out.read_bytes() == direct


def test_cli_export_is_read_only(tmp_path):
    db_path = _seed_db(tmp_path)
    engine = make_engine(f"sqlite:///{db_path}")

    def _counts():
        from bel.infrastructure.persistence.models import (
            ContractModel,
            InvoiceAllocationModel,
            MatchCaseModel,
            TaskExceptionModel,
        )

        with make_session_factory(engine)() as session:
            return {
                "contracts": session.query(ContractModel).count(),
                "task_exceptions": session.query(TaskExceptionModel).count(),
                "match_cases": session.query(MatchCaseModel).count(),
                "invoice_allocations": session.query(InvoiceAllocationModel).count(),
            }

    before = _counts()
    out = tmp_path / "exceptions.xlsx"
    result = _run_bel(
        db_path,
        "exceptions", "export",
        "--format", "xlsx",
        "--output", str(out),
        "--period", PERIOD,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert _counts() == before
