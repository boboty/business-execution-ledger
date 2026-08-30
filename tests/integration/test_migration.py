"""Migration-driven schema creation against the PostgreSQL Migration
Epoch chain (migrations/postgresql_versions/) — the active chain since
Phase 2D.1-P. Runs against a real PostgreSQL database (BEL_DATABASE_URL);
skipped automatically otherwise — see tests/conftest.py's ``postgres``
marker handling and docs/PERSISTENCE-MIGRATION-POLICY.md.

This is the literal "B. empty DB -> upgrade head PASS" / "current == head"
/ "alembic check clean" evidence item. The old chain
(migrations/versions/) is frozen legacy history and is intentionally not
exercised here — see the 5 skipped *_migration.py files for that history's
own (now-inert) tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).parent.parent.parent

EXPECTED_TABLES = {
    "evidence_documents",
    "evidence_fragments",
    "contracts",
    "contract_items",
    "contract_item_revisions",
    "business_events",
    "task_exceptions",
    "import_runs",
    "alembic_version",
}


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


@pytest.mark.postgres
def test_alembic_upgrade_head_creates_full_schema(postgres_url):
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    result = _alembic(postgres_url, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= tables

    # contract_no must never get a UNIQUE index — duplicates are expected.
    contract_indexes = inspector.get_indexes("contracts") + inspector.get_unique_constraints("contracts")
    assert not any("contract_no" in idx.get("column_names", []) and idx.get("unique", True) for idx in contract_indexes)

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current = set(context.get_current_heads())
    script_dir = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))
    heads = set(script_dir.get_heads())
    assert current == heads

    check = _alembic(postgres_url, "check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout

    engine.dispose()


@pytest.mark.postgres
def test_alembic_head_to_base_to_head_round_trips_cleanly(postgres_url):
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")

    assert _alembic(postgres_url, "upgrade", "head").returncode == 0
    assert _alembic(postgres_url, "downgrade", "base").returncode == 0

    inspector = inspect(engine)
    remaining_tables = set(inspector.get_table_names()) - {"alembic_version"}
    assert not remaining_tables

    assert _alembic(postgres_url, "upgrade", "head").returncode == 0
    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())

    check = _alembic(postgres_url, "check")
    assert check.returncode == 0, check.stdout + check.stderr

    engine.dispose()
