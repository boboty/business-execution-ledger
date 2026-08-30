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
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from bel.infrastructure.persistence.database import make_engine

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


@pytest.mark.postgres
def test_percent_encoded_credentials_survive_alembic_and_the_cli_web_path(postgres_url):
    """BLOCKER 1: a URL-encoded password (%40 for '@', %25 for '%', %2F
    for '/' — all valid, all likely for a generated production
    credential) must not break migrations/env.py's Alembic Config
    handling, and the effective URL reaching SQLAlchemy on the Alembic
    side and the CLI/Web side (make_engine/DatabaseRuntime) must be
    semantically identical — same host, same user, same database, same
    literal password. Neither path may leak the password in its output.

    The bug this guards: alembic.config.Config is ConfigParser-backed
    with BasicInterpolation, which treats a bare '%' as the start of a
    '%(name)s' reference — `config.set_main_option("sqlalchemy.url", ...)`
    raised `ValueError: invalid interpolation syntax` the instant a
    %-encoded URL was stored, before any connection was even attempted.
    """
    admin_engine = create_engine(postgres_url, future=True)
    role = f"bel_pct_test_{uuid.uuid4().hex[:8]}"
    dbname = f"bel_pct_test_{uuid.uuid4().hex[:8]}"
    # Deliberately contains '@', '%', and '/' — each requires percent-
    # encoding in a URL and each has a distinct, meaningful escape.
    raw_password = "pa@ss%wo/rd"
    encoded_password = quote(raw_password, safe="")

    # CREATE/DROP DATABASE cannot run inside a transaction block.
    # CREATE ROLE ... PASSWORD does not accept a bind parameter (Postgres
    # rejects a $1 placeholder there — it wants a literal string constant
    # in the grammar), so the password is embedded as a SQL literal:
    # single quotes doubled per standard SQL escaping, AND '%' doubled
    # separately because exec_driver_sql's psycopg cursor scans the raw
    # query TEXT for its own %s/%b/%t placeholder syntax even with no
    # bind params supplied. Test-controlled literal, not attacker input.
    sql_quoted_password = raw_password.replace("'", "''").replace("%", "%%")
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {dbname}")
        connection.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        connection.exec_driver_sql(f"CREATE ROLE {role} LOGIN PASSWORD '{sql_quoted_password}'")
        connection.exec_driver_sql(f"CREATE DATABASE {dbname} OWNER {role}")

    try:
        parts = urlsplit(postgres_url)
        host_port = parts.hostname + (f":{parts.port}" if parts.port else "")
        encoded_url = urlunsplit(
            (parts.scheme, f"{role}:{encoded_password}@{host_port}", f"/{dbname}", "", "")
        )

        # 1-4: alembic upgrade head with %40/%25/%2F all present at once,
        # never a ConfigParser interpolation error, and the schema it
        # produces is the real one — proving the URL reached SQLAlchemy
        # unmodified, not merely that no exception was raised.
        upgrade = _alembic(encoded_url, "upgrade", "head")
        assert upgrade.returncode == 0, upgrade.stderr
        assert "invalid interpolation syntax" not in (upgrade.stdout + upgrade.stderr)

        current = _alembic(encoded_url, "current")
        assert current.returncode == 0
        assert "(head)" in current.stdout

        # 5: CLI/Web (make_engine/DatabaseRuntime) reaches the SAME
        # database via the SAME URL string — not a second, independently
        # decoded credential path.
        cli_engine = make_engine(encoded_url)
        with cli_engine.connect() as connection:
            connected_user = connection.execute(text("select current_user")).scalar_one()
            connected_db = connection.execute(text("select current_database()")).scalar_one()
        assert connected_user == role
        assert connected_db == dbname
        cli_engine.dispose()

        # 6: no controlled error leaks the password or the full
        # credential-bearing URL in either path's output.
        for stream in (upgrade.stdout, upgrade.stderr, current.stdout, current.stderr):
            assert raw_password not in stream
            assert encoded_password not in stream
            assert encoded_url not in stream
    finally:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS {dbname}")
            connection.exec_driver_sql(f"DROP ROLE IF EXISTS {role}")
        admin_engine.dispose()
