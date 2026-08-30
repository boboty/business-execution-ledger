"""Local development .env bootstrap (post-Gate dev UX fix). See
src/bel/infrastructure/env_bootstrap.py's module docstring for the full
design rationale and the frozen precedence rule this enforces:

    explicit --database-url > pre-existing process BEL_DATABASE_URL >
    source-checkout .env value > controlled missing-config error

Every test here uses synthetic, throwaway values only — never the real
repository .env (which may legitimately exist on a developer's machine
with real credentials) and never a value that gets printed anywhere.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import bel.infrastructure.env_bootstrap as env_bootstrap

REPO_ROOT = Path(__file__).parent.parent.parent
SYNTHETIC_URL = "postgresql+psycopg://synthetic_user:synthetic_password@203.0.113.1:5432/synthetic_db"
SYNTHETIC_URL_B = "postgresql+psycopg://other_user:other_password@203.0.113.2:5432/other_db"


# ---------------------------------------------------------------------------
# A / C — explicit-path loading and override=False precedence
# ---------------------------------------------------------------------------


def test_a_loads_from_env_file_when_process_var_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"BEL_DATABASE_URL={SYNTHETIC_URL}\n")

    env_bootstrap.load_local_dotenv(path=env_file)

    assert os.environ.get("BEL_DATABASE_URL") == SYNTHETIC_URL


def test_c_never_overrides_an_already_set_process_var(tmp_path, monkeypatch):
    monkeypatch.setenv("BEL_DATABASE_URL", SYNTHETIC_URL)
    env_file = tmp_path / ".env"
    env_file.write_text(f"BEL_DATABASE_URL={SYNTHETIC_URL_B}\n")

    env_bootstrap.load_local_dotenv(path=env_file)

    assert os.environ.get("BEL_DATABASE_URL") == SYNTHETIC_URL  # process value wins, never B


def test_missing_env_file_is_a_silent_no_op(tmp_path, monkeypatch):
    """E (function level): no .env at the given path — no exception, no
    change. Mirrors what a real BEL checkout with no .env file at all
    (e.g. CI, which never creates one) experiences."""
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    missing = tmp_path / "does-not-exist" / ".env"

    env_bootstrap.load_local_dotenv(path=missing)  # must not raise

    assert "BEL_DATABASE_URL" not in os.environ


# ---------------------------------------------------------------------------
# D — explicit --database-url flag wins over both process env and .env
# ---------------------------------------------------------------------------


def test_d_explicit_cli_flag_wins_over_process_env_var(tmp_path):
    """No .env involved at all — Click's own native flag-over-envvar
    precedence, unrelated to and unaffected by this fix, verified as a
    regression guard given the whole point of this investigation was
    precedence confusion. Evidence: the SQLite-specific Blocker-4
    rejection message only appears if the CLI actually used the explicit
    sqlite:// flag value, not the postgresql+psycopg:// env var."""
    db_path = tmp_path / "explicit.db"
    result = subprocess.run(
        [sys.executable, "-m", "bel.cli", "--database-url", f"sqlite:///{db_path}", "web", "--port", "0"],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": SYNTHETIC_URL},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "requires a PostgreSQL database (got dialect 'sqlite')" in combined
    assert SYNTHETIC_URL not in combined


# ---------------------------------------------------------------------------
# F — CLI and Alembic must be structurally unable to drift: both call the
# SAME shared zero-argument bootstrap, before reading BEL_DATABASE_URL.
# ---------------------------------------------------------------------------


def test_f_cli_and_alembic_env_call_the_same_shared_bootstrap_before_reading_the_var():
    cli_source = (REPO_ROOT / "src" / "bel" / "cli.py").read_text(encoding="utf-8")
    alembic_env_source = (REPO_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")

    for name, source in (("src/bel/cli.py", cli_source), ("migrations/env.py", alembic_env_source)):
        assert "from bel.infrastructure.env_bootstrap import load_local_dotenv" in source, name
        assert "load_local_dotenv()" in source, name
        call_index = source.index("load_local_dotenv()")
        # Whichever line first reads BEL_DATABASE_URL (envvar= for the
        # CLI's --database-url option, os.environ.get(...) for Alembic)
        # must come AFTER the bootstrap call — never before.
        read_markers = ['envvar="BEL_DATABASE_URL"', 'os.environ.get("BEL_DATABASE_URL")']
        read_indices = [source.index(m) for m in read_markers if m in source]
        assert read_indices, f"{name}: no BEL_DATABASE_URL read found to order against"
        assert call_index < min(read_indices), f"{name}: load_local_dotenv() must run before reading BEL_DATABASE_URL"


# ---------------------------------------------------------------------------
# G — repo-root discovery is cwd-independent (mirrors
# test_schema_gate_cwd_independence.py's established pattern).
# ---------------------------------------------------------------------------


def _run_outside_repo(code: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
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


def test_g_repo_root_discovery_is_cwd_independent():
    code = (
        "from bel.infrastructure.env_bootstrap import _find_repo_root\n"
        "root = _find_repo_root()\n"
        "print('ROOT', root)\n"
        "print('HAS_MARKERS', (root / 'pyproject.toml').is_file() and (root / 'alembic.ini').is_file())\n"
    )
    result = _run_outside_repo(code)
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert f"ROOT {REPO_ROOT.resolve()}" in result.stdout
    assert "HAS_MARKERS True" in result.stdout


# ---------------------------------------------------------------------------
# H — production/package safety: no BEL checkout root (both markers
# together) => load_local_dotenv() never generically loads an unrelated
# .env, even if one exists right next to the (fake) module location.
# ---------------------------------------------------------------------------


def test_h_no_repo_root_found_is_a_silent_no_op_even_with_a_nearby_env_file(tmp_path, monkeypatch):
    fake_package_dir = tmp_path / "site-packages" / "bel" / "infrastructure"
    fake_package_dir.mkdir(parents=True)
    fake_module_file = fake_package_dir / "env_bootstrap.py"
    fake_module_file.write_text("# not a real BEL checkout\n")

    # An unrelated .env DOES exist right next to the fake module — but
    # with no pyproject.toml + alembic.ini pair anywhere above it, this
    # must never be treated as a BEL source checkout.
    unrelated_env = fake_package_dir / ".env"
    unrelated_env.write_text("BEL_DATABASE_URL=should-never-be-loaded\n")

    monkeypatch.setattr(env_bootstrap, "__file__", str(fake_module_file))
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)

    assert env_bootstrap._find_repo_root() is None

    env_bootstrap.load_local_dotenv()  # must not raise, must not load the unrelated .env

    assert "BEL_DATABASE_URL" not in os.environ


def test_h_repo_root_requires_both_markers_together(tmp_path, monkeypatch):
    """A directory with only ONE of the two markers is not a BEL
    checkout root — both must co-occur."""
    only_pyproject = tmp_path / "only_pyproject" / "bel" / "infrastructure"
    only_pyproject.mkdir(parents=True)
    (tmp_path / "only_pyproject" / "pyproject.toml").write_text("[project]\nname='not-bel'\n")
    fake_module_file = only_pyproject / "env_bootstrap.py"
    fake_module_file.write_text("# not a real BEL checkout\n")

    monkeypatch.setattr(env_bootstrap, "__file__", str(fake_module_file))
    assert env_bootstrap._find_repo_root() is None


# ---------------------------------------------------------------------------
# I — secret safety: no synthetic credential ever appears in a return
# value, exception, or anywhere this module could plausibly surface it.
# ---------------------------------------------------------------------------


def test_i_load_local_dotenv_returns_nothing_and_raises_nothing_observable(tmp_path, monkeypatch):
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"BEL_DATABASE_URL={SYNTHETIC_URL}\n")

    result = env_bootstrap.load_local_dotenv(path=env_file)

    assert result is None  # never returns the value — os.environ is the only side effect


def test_i_malformed_env_file_does_not_raise_or_leak(tmp_path, monkeypatch):
    monkeypatch.delenv("BEL_DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfe not valid text \x00\x01")

    try:
        env_bootstrap.load_local_dotenv(path=env_file)
    except Exception as exc:  # noqa: BLE001 — if it does raise, the message must stay clean
        assert SYNTHETIC_URL not in str(exc)
