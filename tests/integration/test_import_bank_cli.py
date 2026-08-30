"""CLI smoke test for `bel import-bank --source-account-id` (Phase
2D.1-R5 round 2): the ordinary bank intake records the caller-supplied
source-account identifier on every Payment it creates, and omitting the
option keeps the pre-round behaviour (NULL, everything else identical).
Runs the real `bel` entry point against a real migrated SQLite file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", "--db", str(db_path), *args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")},
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


def _source_accounts(db_path: Path) -> list[str | None]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from bel.infrastructure.persistence.database import make_engine, make_session_factory
    from bel.infrastructure.persistence.repositories import PaymentRepository

    session_factory = make_session_factory(make_engine(str(db_path)))
    with session_factory() as session:
        return [p.source_account_id for p in PaymentRepository(session).list_all()]


def _synthetic_bank_pdf(path: Path) -> None:
    from fixtures.synthetic import scenarios
    from fixtures.synthetic.bank_pdf import build_cmb_bank_statement_pdf

    build_cmb_bank_statement_pdf(path, scenarios.OPENING_BALANCE, scenarios.PAYMENT_TRANSACTIONS)


def test_import_bank_source_account_id_is_persisted(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    pdf_path = tmp_path / "bank.pdf"
    _synthetic_bank_pdf(pdf_path)

    result = _run_bel(db_path, "import-bank", str(pdf_path), "--profile", "cmb", "--source-account-id", "ACC-CLI")

    assert result.returncode == 0, result.stderr
    assert "Import completed" in result.stdout
    assert "ACC-CLI" in result.stdout
    accounts = _source_accounts(db_path)
    assert accounts, "no Payment was created"
    assert set(accounts) == {"ACC-CLI"}


def test_import_bank_without_source_account_id_is_unchanged(tmp_path):
    """Backward compatibility: the option is optional, and omitting it
    leaves every Payment with a NULL source_account_id — the same state
    every pre-round import produced."""
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    pdf_path = tmp_path / "bank.pdf"
    _synthetic_bank_pdf(pdf_path)

    result = _run_bel(db_path, "import-bank", str(pdf_path), "--profile", "cmb")

    assert result.returncode == 0, result.stderr
    assert "(not supplied)" in result.stdout
    accounts = _source_accounts(db_path)
    assert accounts, "no Payment was created"
    assert set(accounts) == {None}


def test_import_bank_matching_semantics_unchanged_by_source_account(tmp_path):
    """The seam adds an identifier, nothing else: the same number of
    Payment facts is created and a re-import is still detected as a
    re-import (0 new facts) — the pre-round behaviour, unchanged."""
    from fixtures.synthetic import scenarios

    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    pdf_path = tmp_path / "bank.pdf"
    _synthetic_bank_pdf(pdf_path)

    first = _run_bel(db_path, "import-bank", str(pdf_path), "--profile", "cmb", "--source-account-id", "ACC-CLI")
    second = _run_bel(db_path, "import-bank", str(pdf_path), "--profile", "cmb", "--source-account-id", "ACC-CLI")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert f"payments created: {len(scenarios.PAYMENT_TRANSACTIONS)}" in first.stdout
    assert "re-import" in second.stdout
    assert len(_source_accounts(db_path)) == len(scenarios.PAYMENT_TRANSACTIONS)
