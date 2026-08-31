"""Regression coverage for the disposable test-database contract
(tests/pg_disposable.py) — the guard that keeps the destructive
PostgreSQL fixtures from ever targeting the runtime database.

The destructive PostgreSQL fixtures (``pg_runtime``, the Alembic
round-trip tests, the real-runtime Web smoke) DROP and recreate
schemas/databases. These tests prove the contract that makes that safe:

1. A runtime ``BEL_DATABASE_URL`` — however production-like — can NEVER
   activate the destructive fixture: the ``postgres_url`` fixture skips
   before the test body (and its DROPs) can run.
2. Activation requires BOTH an explicit disposable test URL
   (``BEL_TEST_DATABASE_URL``) AND an explicit opt-in
   (``BEL_TEST_DATABASE_DISPOSABLE=1``); anything else skips.
3. When the contract IS satisfied, the fixture hands out the disposable
   URL (and only that URL) — the CI ephemeral database path keeps
   working.

No test here connects to any database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

from tests.pg_disposable import (
    DISPOSABLE_OPT_IN_VAR,
    DISPOSABLE_URL_VAR,
    resolve_disposable_postgres_url,
)

REPO_ROOT = Path(__file__).parent.parent.parent

RUNTIME_URL = "postgresql+psycopg://runtime_user:runtime_password@db-host:5432/bel_production"
DISPOSABLE_URL = "postgresql+psycopg://test_user:test_password@localhost:5432/bel_test_disposable"


# ---------------------------------------------------------------------------
# 1+2. Resolver matrix — the runtime URL is structurally unreachable.
# ---------------------------------------------------------------------------


def test_resolver_returns_none_when_only_the_runtime_url_is_set() -> None:
    """Only BEL_DATABASE_URL configured — the exact configuration that
    must NOT activate anything destructive. The resolver must never fall
    back to it."""
    env = {"BEL_DATABASE_URL": RUNTIME_URL}
    assert resolve_disposable_postgres_url(env) is None


def test_resolver_returns_none_with_test_url_but_no_opt_in() -> None:
    env = {DISPOSABLE_URL_VAR: DISPOSABLE_URL}
    assert resolve_disposable_postgres_url(env) is None


def test_resolver_requires_the_exact_opt_in_value_1() -> None:
    env = {DISPOSABLE_URL_VAR: DISPOSABLE_URL}
    for wrong in ("", "0", "true", "yes", " 1 ", "1.0", "2"):
        assert resolve_disposable_postgres_url({**env, DISPOSABLE_OPT_IN_VAR: wrong}) is None, wrong


def test_resolver_returns_none_for_a_non_postgresql_test_url() -> None:
    env = {DISPOSABLE_URL_VAR: "sqlite:///tmp/bel-test.db", DISPOSABLE_OPT_IN_VAR: "1"}
    assert resolve_disposable_postgres_url(env) is None


def test_resolver_returns_the_disposable_url_when_fully_opted_in() -> None:
    env = {DISPOSABLE_URL_VAR: DISPOSABLE_URL, DISPOSABLE_OPT_IN_VAR: "1"}
    assert resolve_disposable_postgres_url(env) == DISPOSABLE_URL


def test_resolver_never_reads_the_runtime_variable_even_when_fully_opted_in() -> None:
    """Even with the contract satisfied, the resolved URL comes only from
    the test-harness variable — the runtime URL is ignored."""
    env = {
        "BEL_DATABASE_URL": RUNTIME_URL,
        DISPOSABLE_URL_VAR: DISPOSABLE_URL,
        DISPOSABLE_OPT_IN_VAR: "1",
    }
    assert resolve_disposable_postgres_url(env) == DISPOSABLE_URL


# ---------------------------------------------------------------------------
# 2+3. End-to-end through the REAL collection hook AND the REAL
#      ``postgres_url`` fixture (copied verbatim into a pytester
#      sandbox): a destructive-looking postgres-marked test never runs
#      its body from BEL_DATABASE_URL alone, while unmarked tests keep
#      running; and it DOES run (body executes) once the disposable
#      contract is satisfied.
# ---------------------------------------------------------------------------


PYTESTER_DUMMY = """
import pytest

@pytest.mark.postgres
def test_destructive_body_would_drop(postgres_url):
    raise RuntimeError(f"destructive body executed with {postgres_url}")

def test_unmarked_control_still_runs():
    assert True
"""


def _pytester_project(pytester: pytest.Pytester) -> None:
    real_conftest_src = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    # Prepend only a sys.path preamble, and only AFTER the copied
    # conftest's `from __future__` line (which must stay first).
    first_line_end = real_conftest_src.index("\n")
    pytester.makeconftest(
        real_conftest_src[: first_line_end + 1]
        + "import sys\n"
        + f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        + real_conftest_src[first_line_end + 1 :]
    )
    pytester.makepyfile(test_dummy=PYTESTER_DUMMY)


def _outcome_names(result: pytest.RunResult) -> dict[str, list[str]]:
    return result.parseoutcomes() or {}


def test_collection_hook_skips_postgres_tests_from_runtime_url_alone(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytester_project(pytester)
    monkeypatch.setenv("BEL_DATABASE_URL", RUNTIME_URL)
    monkeypatch.delenv(DISPOSABLE_URL_VAR, raising=False)
    monkeypatch.delenv(DISPOSABLE_OPT_IN_VAR, raising=False)
    result = pytester.runpytest("-ra")
    outcomes = _outcome_names(result)
    assert outcomes.get("passed") == 1, "unmarked control must still run"
    assert outcomes.get("skipped") == 1, "destructive test must skip"
    assert outcomes.get("failed", 0) == 0
    result.assert_outcomes(passed=1, skipped=1)
    # The skip happens in the fixture — BEFORE the destructive body — and
    # the reason names the contract, not the runtime variable.
    result.stdout.fnmatch_lines(["*BEL_DATABASE_URL alone NEVER activates*"])


def test_collection_hook_skips_postgres_tests_with_test_url_but_no_opt_in(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytester_project(pytester)
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    monkeypatch.setenv(DISPOSABLE_URL_VAR, DISPOSABLE_URL)
    monkeypatch.delenv(DISPOSABLE_OPT_IN_VAR, raising=False)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, skipped=1)


def test_collection_hook_activates_destructive_tests_only_when_fully_opted_in(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytester_project(pytester)
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    monkeypatch.setenv(DISPOSABLE_URL_VAR, DISPOSABLE_URL)
    monkeypatch.setenv(DISPOSABLE_OPT_IN_VAR, "1")
    # The destructive body executes now (and deliberately fails, since no
    # database exists in this sandbox) — proving the contract activates
    # exactly when satisfied, and only with the disposable URL.
    result = pytester.runpytest()
    outcomes = _outcome_names(result)
    assert outcomes.get("failed") == 1
    assert outcomes.get("skipped", 0) == 0
    result.stdout.fnmatch_lines(["*destructive body executed with " + DISPOSABLE_URL + "*"])


def test_collection_hook_skips_postgres_tests_when_nothing_is_configured(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytester_project(pytester)
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    monkeypatch.delenv(DISPOSABLE_URL_VAR, raising=False)
    monkeypatch.delenv(DISPOSABLE_OPT_IN_VAR, raising=False)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, skipped=1)
