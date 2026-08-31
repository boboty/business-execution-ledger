"""Destructive PostgreSQL test-harness contract — TEST-HARNESS
CONFIGURATION ONLY.

Several PostgreSQL tests are destructive by design: they DROP and
recreate schemas/databases so every test starts from empty (the
``pg_runtime`` fixture, the Alembic migration round-trip tests, the
real-runtime Web smoke). They must therefore never implicitly target
BEL's runtime database: ``BEL_DATABASE_URL`` stays BEL's single runtime
DB configuration and is never consulted by a destructive fixture.

Destructive PostgreSQL tests run only against an explicitly configured
disposable test database, gated behind an explicit opt-in:

    BEL_TEST_DATABASE_URL         e.g. postgresql+psycopg://.../bel_test
                                  — a database you accept losing
    BEL_TEST_DATABASE_DISPOSABLE  exactly ``1`` — "this database is
                                  disposable, destructive tests may
                                  drop/reset it"

Missing or incomplete configuration resolves to NO test database: the
tests skip BEFORE any DROP is issued. A runtime ``BEL_DATABASE_URL``,
however production-like or however innocent, can never activate the
destructive fixtures — see
``tests/unit/test_postgres_disposable_contract.py`` for the regression
coverage of exactly that.

This module is imported only by test code (``tests/conftest.py`` and
tests exercising the contract). It must never be imported by
``src/bel/`` runtime code.
"""

from __future__ import annotations

from collections.abc import Mapping

DISPOSABLE_URL_VAR = "BEL_TEST_DATABASE_URL"
DISPOSABLE_OPT_IN_VAR = "BEL_TEST_DATABASE_DISPOSABLE"

DISPOSABLE_SKIP_REASON = (
    "destructive PostgreSQL tests require a disposable test database: "
    f"set {DISPOSABLE_URL_VAR} to a postgresql+psycopg:// URL you accept "
    f"losing AND set {DISPOSABLE_OPT_IN_VAR}=1 to opt in explicitly. "
    "BEL_DATABASE_URL alone NEVER activates these tests — it is the "
    "runtime configuration and is never dropped or reset by the test "
    "suite (see tests/pg_disposable.py)."
)


def is_disposable_test_database_configured(env: Mapping[str, str]) -> bool:
    """True iff BOTH the disposable test URL and the explicit ``=1``
    opt-in are present. The URL must be a real PostgreSQL URL — the
    destructive fixtures are PostgreSQL-specific, so anything else
    (including an inherited SQLite convenience URL) counts as
    unconfigured rather than as a misconfiguration to crash on."""
    if env.get(DISPOSABLE_OPT_IN_VAR, "") != "1":
        return False
    return env.get(DISPOSABLE_URL_VAR, "").startswith("postgresql")


def resolve_disposable_postgres_url(env: Mapping[str, str]) -> str | None:
    """The disposable test database URL, or ``None`` when the contract is
    not satisfied. Reads ONLY the two test-harness variables — never
    ``BEL_DATABASE_URL``, which is the runtime configuration and must be
    structurally unreachable here."""
    if not is_disposable_test_database_configured(env):
        return None
    return env[DISPOSABLE_URL_VAR]
