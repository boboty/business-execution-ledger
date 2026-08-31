"""BLOCKER 4 (Phase 2D.1-P repair round): the official `bel web` CLI
entry point must enforce the frozen persistence contract (PostgreSQL =
production, SQLite = test-only convenience) explicitly, not just by
documentation — while `create_app()` called directly (as the rest of the
Web test suite does, via an injected/URL-built SQLite runtime) stays
completely unaffected. See src/bel/cli.py's web_cmd and
docs/PERSISTENCE-MIGRATION-POLICY.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent


def test_web_cli_rejects_sqlite_database_url(tmp_path):
    """`bel --database-url sqlite:///...  web` must fail with a clean,
    controlled error — never start a server, never a raw traceback. No
    real PostgreSQL needed: the rejection happens before any DB
    connection is attempted, so this runs in the plain SQLite-convenience
    suite (no @pytest.mark.postgres)."""
    db_path = tmp_path / "foo.db"
    result = subprocess.run(
        [sys.executable, "-m", "bel.cli", "--database-url", f"sqlite:///{db_path}", "web", "--port", "0"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "PostgreSQL" in result.stdout or "PostgreSQL" in result.stderr
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_web_cli_rejects_sqlite_memory_database_url():
    """Same rejection for the in-memory form."""
    result = subprocess.run(
        [sys.executable, "-m", "bel.cli", "--database-url", "sqlite://", "web", "--port", "0"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "PostgreSQL" in result.stdout or "PostgreSQL" in result.stderr
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.postgres
def test_web_cli_accepts_postgresql_database_url(postgres_url):
    """`bel web` against a real, migrated PostgreSQL database starts and
    serves normally — the dialect check does not false-positive-reject
    the production contract it exists to protect. Covers Contract
    Ledger, Period Close Workbench, and the Phase 2D.2 Period Close Data
    Product export routes against the SAME real PostgreSQL runtime (one
    server startup, several requests) — this HTTP coverage elsewhere
    uses an injected SQLite runtime and does not by itself prove any of
    these pages/routes work against PostgreSQL."""
    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()

    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": postgres_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert upgrade.returncode == 0, upgrade.stderr

    proc = subprocess.Popen(
        [sys.executable, "-m", "bel.cli", "web", "--port", "8823"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": postgres_url},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        import urllib.request

        deadline = time.monotonic() + 10
        last_error = None
        status = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:8823/contract-ledger", timeout=1) as resp:
                    status = resp.status
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.3)
        assert status == 200, f"server never became ready: {last_error}"

        # Same server, same real PostgreSQL runtime, second request — the
        # Period Close Workbench page must also serve against PostgreSQL,
        # not only Contract Ledger. Existing Period Close HTTP coverage
        # elsewhere uses an injected SQLite runtime and does not satisfy
        # this requirement on its own.
        with urllib.request.urlopen("http://127.0.0.1:8823/period-close", timeout=5) as resp:
            period_close_status = resp.status
        assert period_close_status == 200

        # Phase 2D.2: the Data Product export routes must also serve
        # against the same real PostgreSQL runtime — an empty database is
        # still a valid (empty) export, never an error.
        with urllib.request.urlopen("http://127.0.0.1:8823/period-close/export.xlsx", timeout=5) as resp:
            xlsx_status = resp.status
            xlsx_content_type = resp.headers.get("Content-Type")
        assert xlsx_status == 200
        assert xlsx_content_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        with urllib.request.urlopen("http://127.0.0.1:8823/period-close/export.csv", timeout=5) as resp:
            csv_status = resp.status
            csv_content_type = resp.headers.get("Content-Type")
        assert csv_status == 200
        assert csv_content_type == "text/csv; charset=utf-8"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
