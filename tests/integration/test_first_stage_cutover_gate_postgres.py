"""FIRST-STAGE CUTOVER GATE — real PostgreSQL integration path.

Proves the exact section 17B requirements against a REAL migrated
PostgreSQL runtime (not the SQLite test harness):

- target dialect is PostgreSQL
- schema revision == Alembic head (``alembic upgrade head`` was run)
- the Gate can inspect an already-prepared database
- zero business writes

Runs only against the disposable test database contract
(``BEL_TEST_DATABASE_URL`` + ``BEL_TEST_DATABASE_DISPOSABLE=1``, see
tests/pg_disposable.py) — never against ``BEL_DATABASE_URL``. Skipped
automatically otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

NOW = datetime.now(timezone.utc)


def _upgrade_head(postgres_url: str) -> None:
    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": postgres_url},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert upgrade.returncode == 0, upgrade.stderr


def _reset_schema(postgres_url: str) -> None:
    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()


def _make_period_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "private"
    period_dir = root / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    (period_dir / "backfill-plan.json").write_text(json.dumps({"version": 1}))
    return root, period_dir


def _prepare_contract(session, contract_no: str, counterparty: str):
    """Prepare one contract through the canonical Fact service (the same
    primitive ``cutover_backfill`` itself calls) — an approved way to
    reach an already-prepared cutover state."""
    from bel.application.contract_facts import create_contract_fact
    from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
    from bel.infrastructure.persistence.repositories import EvidenceRepository

    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256="a" * 64, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None, row_number=None, locator_json={}, raw_data={}, created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return create_contract_fact(
        session,
        contract_no=contract_no,
        counterparty=counterparty,
        fields={
            "contract_type": "出口报关购销合同", "buyer": "BuyerPG", "gross_amount": Decimal("1000.00"),
            "currency": "CNY", "contract_date": date(2026, 1, 1),
        },
        source_fragment_id=frag.id,
        created_at=NOW,
    ).contract


def _write_baseline(period_dir: Path, entries: list[dict]) -> None:
    (period_dir / "expected" / "cutover-baseline.json").write_text(json.dumps({"entries": entries}))


@pytest.mark.postgres
def test_first_stage_cutover_gate_postgres(postgres_url: str, tmp_path: Path):
    from bel.application.first_stage_cutover_gate import PASS, run_first_stage_cutover_gate
    from bel.infrastructure.persistence.database import make_engine, make_session_factory
    from bel.infrastructure.persistence.models import ContractModel

    _reset_schema(postgres_url)
    _upgrade_head(postgres_url)

    root, period_dir = _make_period_root(tmp_path)

    # Prepare the database (approved fact path) and its business-confirmed
    # Cutover Baseline.
    session_factory = make_session_factory(make_engine(postgres_url))
    with session_factory() as session:
        contract = _prepare_contract(session, "C-PG", "SupplierPG")
        session.commit()
        entries = [
            {
                "key": f"contract:contract_no={contract.contract_no}|counterparty={contract.counterparty}",
                "expected": {
                    "contract_type": contract.contract_type, "buyer": contract.buyer,
                    "gross_amount": str(contract.gross_amount), "currency": contract.currency,
                    "contract_date": contract.contract_date.isoformat(),
                },
                "outcome": "MATCH",
            },
            {
                "key": f"unresolved_indicator:contract_no={contract.contract_no}|counterparty={contract.counterparty}",
                "expected": {"has_unresolved": False},
                "outcome": "MATCH",
            },
        ]
        _write_baseline(period_dir, entries)

        contracts_before = session.query(ContractModel).count()

        # The Gate against the REAL PostgreSQL runtime.
        result = run_first_stage_cutover_gate(
            session, period="2026-01", private_root=root, candidate_sha="pg-integration"
        )
        assert result.runtime_schema == PASS, result.reason_codes
        assert result.passed, result.reason_codes
        assert result.diagnostics["runtime"]["dialect"] == "postgresql"
        assert result.diagnostics["runtime"]["schema_ok"] is True
        assert result.diagnostics["read_only"]["unchanged"] is True
        assert result.report_written
        assert (root / "reports" / "first-stage-cutover-gate-2026-01.json").exists()

        # Zero business writes — the gate's own full-schema fingerprint is
        # unchanged and a direct table-count spot check agrees.
        assert session.query(ContractModel).count() == contracts_before

    # The full CLI path against PostgreSQL prints the safe PASS verdict.
    proc = subprocess.run(
        [sys.executable, "-m", "bel.cli", "cutover", "gate", "--period", "2026-01"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": postgres_url, "BEL_PRIVATE_DATA_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "FIRST_STAGE_CUTOVER_GATE: PASS"
    assert "Traceback" not in proc.stdout
    assert "Traceback" not in proc.stderr
