"""G0 repair, Blocker 1 — two-level Click startup schema gate.

``ctx.invoked_subcommand`` at the ROOT group is the first-level command
name (``"cutover"`` for the whole cutover group), never the nested
``"gate"`` — so the nested bypass lives in the CUTOVER group callback,
not the root callback. Required behavior:

- ordinary command            -> root schema gate fires
- cutover backfill/reconcile  -> cutover-group schema gate fires
- cutover gate                -> no generic schema gate; the Gate runs and
                                 emits FIRST_STAGE_CUTOVER_GATE: FAIL itself

These tests drive the real Click CLI in-process with the schema probe
monkeypatched to simulate drift, so no PostgreSQL is required to prove
WHICH gate fires for WHICH command. The schema-drift-against-real-
PostgreSQL path (Gate reporting SCHEMA_NOT_AT_HEAD + private report) is
covered by the @pytest.mark.postgres integration test.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bel import cli as cli_mod
from bel.infrastructure.persistence.schema_gate import SchemaNotAtHeadError

DRIFT = "Database schema is not at Alembic head. Run: alembic upgrade head"


def _install_fake_drift(monkeypatch):
    """Make the CLI startup schema probe report drift, and record calls."""
    calls = []

    def fake_assert(engine):
        calls.append(engine)
        raise SchemaNotAtHeadError(DRIFT)

    monkeypatch.setattr(cli_mod, "assert_schema_at_head", fake_assert)
    return calls


def _make_gate_env(tmp_path) -> tuple[dict, Path]:
    root = tmp_path / "private"
    (root / "2026-01" / "expected").mkdir(parents=True)
    (root / "2026-01" / "backfill-plan.json").write_text(json.dumps({"version": 1}))
    (root / "2026-01" / "expected" / "cutover-baseline.json").write_text(json.dumps({"entries": []}))
    env = {
        "BEL_DATABASE_URL": f"sqlite:///{tmp_path / 'bel.db'}",
        "BEL_PRIVATE_DATA_ROOT": str(root),
    }
    return env, root


def test_ordinary_command_keeps_root_schema_gate(monkeypatch, tmp_path):
    calls = _install_fake_drift(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["period-close", "preview", "2026-01"], env=_make_gate_env(tmp_path)[0])
    assert calls  # the ROOT gate fired
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Alembic head" in result.output


def test_cutover_backfill_keeps_cutover_group_schema_gate(monkeypatch, tmp_path):
    calls = _install_fake_drift(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["cutover", "backfill", "--period", "2026-01"], env=_make_gate_env(tmp_path)[0])
    assert calls  # the CUTOVER-group gate fired (backfill is not gate)
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Alembic head" in result.output


def test_cutover_reconcile_keeps_cutover_group_schema_gate(monkeypatch, tmp_path):
    calls = _install_fake_drift(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["cutover", "reconcile", "--period", "2026-01"], env=_make_gate_env(tmp_path)[0])
    assert calls
    assert result.exit_code == 1
    assert "Alembic head" in result.output


def test_cutover_gate_bypasses_schema_gates_and_reaches_gate(monkeypatch, tmp_path):
    """The ONLY bypass: `cutover gate` must not be intercepted by either
    schema gate; it runs the Gate, which on this SQLite harness FAILs its
    own runtime_schema dimension, prints the safe verdict and writes the
    private report."""
    calls = _install_fake_drift(monkeypatch)
    env, root = _make_gate_env(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["cutover", "gate", "--period", "2026-01"], env=env)
    assert not calls  # neither the root nor the cutover group fired
    assert result.exit_code == 1
    assert result.output.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL"
    assert "Error:" not in result.output
    assert "Alembic head" not in result.output
    report = root / "reports" / "first-stage-cutover-gate-2026-01.json"
    assert report.exists()


def test_cutover_gate_without_drift_still_runs_its_own_runtime_check(tmp_path):
    """Without a fake drift the Gate still executes its OWN canonical
    runtime probe (SQLite is not the canonical runtime) and FAILs cleanly
    — proving the gate command is not reliant on the generic schema gate
    and not globally disabled."""
    env, root = _make_gate_env(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["cutover", "gate", "--period", "2026-01"], env=env)
    assert result.exit_code == 1
    assert result.output.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL"
    assert "Traceback" not in result.output
    report = root / "reports" / "first-stage-cutover-gate-2026-01.json"
    assert report.exists()
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["dimensions"]["runtime_schema"] == "FAIL"
