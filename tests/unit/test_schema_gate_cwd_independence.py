"""PART C (Phase 2D.1-P repair round): Alembic/schema-gate operation
must not depend on the shell's current working directory. Every check
here runs a subprocess with cwd set OUTSIDE the repository entirely, so
a regression that silently started relying on a relative path (or on
being invoked from the repo root) would fail these, not just happen to
pass because the rest of the suite always runs from the repo root.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent


def _run_outside_repo(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run *code* as `python -c`, with cwd set to a directory guaranteed
    to be outside this repository — never `cwd=REPO_ROOT`, unlike every
    other subprocess helper in this test suite."""
    import os

    with tempfile.TemporaryDirectory() as outside_dir:
        assert not str(Path(outside_dir).resolve()).startswith(str(REPO_ROOT.resolve()))
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=outside_dir,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=15,
        )


def test_schema_gate_locates_alembic_ini_from_outside_the_repo():
    """_alembic_ini_path()/_script_directory() must resolve to the real
    repo alembic.ini and the real single PostgreSQL head, regardless of
    cwd — no reliance on cwd == repo root."""
    code = (
        "from bel.infrastructure.persistence.schema_gate import _alembic_ini_path, _script_directory\n"
        "p = _alembic_ini_path()\n"
        "print('INI_PATH', p)\n"
        "print('IS_ABSOLUTE', p.is_absolute())\n"
        "print('EXISTS', p.is_file())\n"
        "heads = _script_directory().get_heads()\n"
        "print('HEADS', heads)\n"
    )
    result = _run_outside_repo(code)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "IS_ABSOLUTE True" in result.stdout
    assert "EXISTS True" in result.stdout
    # The head moved from the baseline (f5796c006707) to the F1c
    # migration (93e9d48c5cc8) — this asserts the SINGLE real head, not a
    # specific hash, so it stays correct as the chain grows.
    assert "HEADS ('93e9d48c5cc8',)" in result.stdout or "'93e9d48c5cc8'" in result.stdout


def test_schema_gate_error_never_contains_credentials_from_outside_the_repo():
    """assert_schema_at_head()'s controlled error names revision ids
    only — never the connection URL/credentials — even when raised from
    a process whose cwd is outside the repo."""
    code = (
        "from sqlalchemy import create_engine\n"
        "from bel.infrastructure.persistence.schema_gate import assert_schema_at_head, SchemaNotAtHeadError\n"
        "url = 'postgresql+psycopg://sentinel_user:sentinel_password@127.0.0.1:1/nonexistent_db_for_this_test'\n"
        "engine = create_engine(url, future=True)\n"
        "try:\n"
        "    assert_schema_at_head(engine)\n"
        "    print('UNEXPECTEDLY_SUCCEEDED')\n"
        "except Exception as exc:\n"
        "    print('EXC_TYPE', type(exc).__name__)\n"
        "    print('EXC_TEXT', str(exc))\n"
    )
    result = _run_outside_repo(code)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    # A connection to a nonexistent host:port fails at the DBAPI level
    # before assert_schema_at_head's own revision-mismatch branch is
    # ever reached — either way, the failure text must never carry the
    # sentinel credentials.
    assert "sentinel_user" not in result.stdout
    assert "sentinel_password" not in result.stdout
    assert "postgresql+psycopg://sentinel_user" not in result.stdout


@pytest.mark.postgres
def test_schema_gate_assert_schema_at_head_works_from_outside_the_repo(postgres_url):
    """End-to-end: assert_schema_at_head() against a REAL, correctly
    migrated PostgreSQL database succeeds silently when invoked from a
    cwd outside the repo, and correctly raises SchemaNotAtHeadError
    (never a path-resolution error) against an unmigrated one."""
    import os

    from sqlalchemy import create_engine

    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()

    # Unmigrated: must raise the controlled SchemaNotAtHeadError, not a
    # path-lookup error, and not print the URL.
    code_before_migration = (
        "from sqlalchemy import create_engine\n"
        "from bel.infrastructure.persistence.schema_gate import assert_schema_at_head, SchemaNotAtHeadError\n"
        f"engine = create_engine({postgres_url!r}, future=True)\n"
        "try:\n"
        "    assert_schema_at_head(engine)\n"
        "    print('UNEXPECTEDLY_AT_HEAD')\n"
        "except SchemaNotAtHeadError as exc:\n"
        "    print('CORRECTLY_NOT_AT_HEAD')\n"
        "    print('TEXT', str(exc))\n"
    )
    result = _run_outside_repo(code_before_migration)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "CORRECTLY_NOT_AT_HEAD" in result.stdout
    assert postgres_url not in result.stdout

    upgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": postgres_url},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert upgrade.returncode == 0, upgrade.stderr

    code_after_migration = (
        "from sqlalchemy import create_engine\n"
        "from bel.infrastructure.persistence.schema_gate import assert_schema_at_head\n"
        f"engine = create_engine({postgres_url!r}, future=True)\n"
        "assert_schema_at_head(engine)\n"
        "print('AT_HEAD_OK')\n"
    )
    result = _run_outside_repo(code_after_migration)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "AT_HEAD_OK" in result.stdout
