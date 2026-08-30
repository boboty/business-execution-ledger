"""Startup schema-revision gate (Phase 2D.1-P, Part G).

A revision CHECK, never an automatic migration — BEL must never silently
migrate a production database at application startup. Used by both CLI
(the ``cli()`` group callback) and Web (``create_app``) before either
begins doing real work.

PostgreSQL-only: SQLite has no active Alembic chain at all (it is a
test-only convenience — see ``database.py``'s module docstring), so this
gate no-ops for any non-PostgreSQL engine. An injected/explicit SQLite
``DatabaseRuntime`` bypasses this gate by construction; that bypass is
deliberate and unsupported for production — production Web/CLI paths only
ever run against ``postgresql+psycopg://`` URLs, where the gate is always
enforced.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine


class SchemaNotAtHeadError(RuntimeError):
    pass


def _alembic_ini_path() -> Path:
    """Resolve ``alembic.ini`` at the repo root as an ABSOLUTE path,
    independent of the caller's current working directory — found by
    walking up from this file rather than assuming a cwd. An absolute
    path is required: ``Config(path)`` combined with alembic.ini's
    ``%(here)s`` tokens (for ``script_location`` /``version_locations``)
    only resolves correctly when the ini file itself is addressed
    unambiguously — a bare ``ScriptDirectory(some_dir)`` construction
    does NOT pick up ``version_locations`` at all and silently reports no
    revisions, which is why this goes through ``Config`` +
    ``ScriptDirectory.from_config`` instead."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        f"could not locate alembic.ini by walking up from {here} — is schema_gate.py running "
        "from within a business-execution-ledger checkout?"
    )


def _script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(_alembic_ini_path())))


def assert_schema_at_head(engine: Engine) -> None:
    """Raise ``SchemaNotAtHeadError`` unless *engine*'s current Alembic
    revision equals the single expected head under
    ``migrations/postgresql_versions``. No-ops for any dialect other than
    PostgreSQL (see module docstring)."""
    if engine.dialect.name != "postgresql":
        return

    heads = set(_script_directory().get_heads())

    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current = set(context.get_current_heads())

    if current == heads:
        return

    raise SchemaNotAtHeadError(
        "Database schema is not at Alembic head. Run: alembic upgrade head\n"
        f"(current revision(s): {sorted(current) or ['<none — empty database>']}, "
        f"expected head(s): {sorted(heads)})"
    )
