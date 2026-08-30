"""Public, synthetic tests for private-acceptance output and report containment."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


RUNNER_PATH = Path(__file__).parents[1] / "private_acceptance" / "runner.py"
SPEC = importlib.util.spec_from_file_location("private_acceptance_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def test_run_scenario_stdout_is_one_redacted_line_for_pass(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "resolve_period_dir", lambda root, period: tmp_path)
    monkeypatch.setitem(runner.SCENARIOS, "P1_IMPORT", lambda period_dir: {"scenario": "P1_IMPORT"})

    assert runner.run_scenario(tmp_path, None, "P1_IMPORT") is True
    assert capsys.readouterr().out == "P1_IMPORT: PASS\n"
    assert (tmp_path / "reports" / "P1_IMPORT.json").exists()


def test_run_scenario_failure_redacts_stdout_and_keeps_diagnostic_external(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "resolve_period_dir", lambda root, period: tmp_path)

    def fail(period_dir):
        raise runner.AcceptanceError("SYNTHETIC_FAILURE", {"detail": "synthetic-secret"})

    monkeypatch.setitem(runner.SCENARIOS, "P1_IMPORT", fail)

    assert runner.run_scenario(tmp_path, None, "P1_IMPORT") is False
    captured = capsys.readouterr()
    assert captured.out == "P1_IMPORT: FAIL\n"
    assert captured.err == ""
    assert "synthetic-secret" in (tmp_path / "reports" / "P1_IMPORT.json").read_text()


def test_run_scenario_unexpected_error_is_one_redacted_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(runner, "resolve_period_dir", lambda root, period: tmp_path)

    def crash(period_dir):
        raise RuntimeError("synthetic-secret")

    monkeypatch.setitem(runner.SCENARIOS, "P1_IMPORT", crash)

    assert runner.run_scenario(tmp_path, None, "P1_IMPORT") is False
    captured = capsys.readouterr()
    assert captured.out == "P1_IMPORT: FAIL\n"
    assert captured.err == ""
    assert "synthetic-secret" not in captured.out
    assert "synthetic-secret" in (tmp_path / "reports" / "P1_IMPORT.json").read_text()


@pytest.mark.parametrize("root_value", [None, str(Path(__file__).parents[2])])
def test_main_with_unusable_root_outputs_only_scenario_failure(monkeypatch, root_value, capsys):
    if root_value is None:
        monkeypatch.delenv("BEL_PRIVATE_DATA_ROOT", raising=False)
    else:
        monkeypatch.setenv("BEL_PRIVATE_DATA_ROOT", root_value)

    assert runner.main(["P1_IMPORT"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "P1_IMPORT: FAIL\n"
    assert captured.err == ""


def test_main_unexpected_root_error_is_redacted(monkeypatch, capsys):
    def crash():
        raise RuntimeError("synthetic-secret")

    monkeypatch.setattr(runner, "resolve_private_root", crash)

    assert runner.main(["P1_IMPORT"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "P1_IMPORT: FAIL\n"
    assert captured.err == ""


def test_report_rejects_reports_symlink_into_repository(tmp_path):
    repo_root = Path(runner.__file__).resolve().parents[2]
    (tmp_path / "reports").symlink_to(repo_root, target_is_directory=True)

    runner._write_report(tmp_path, "P1_IMPORT", {"detail": "synthetic-secret"})

    assert not (tmp_path / "reports" / "P1_IMPORT.json").exists()


def test_report_rejects_reports_symlink_outside_private_root(tmp_path):
    escaped = tmp_path.parent / "escaped-reports"
    (tmp_path / "reports").symlink_to(escaped, target_is_directory=True)

    runner._write_report(tmp_path, "P1_IMPORT", {"detail": "synthetic-secret"})

    assert not escaped.exists()


def test_report_rejects_existing_report_symlink_into_repository(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    repo_file = Path(runner.__file__).resolve().parents[2] / "README.md"
    original = repo_file.read_text()
    (reports / "P1_IMPORT.json").symlink_to(repo_file)

    runner._write_report(tmp_path, "P1_IMPORT", {"detail": "synthetic-secret"})

    assert repo_file.read_text() == original


def test_report_rejects_scenario_identifier_path_escape(tmp_path):
    escaped = tmp_path.parent / "escaped-report.json"

    runner._write_report(tmp_path, "../escaped-report", {"detail": "synthetic-secret"})

    assert not escaped.exists()


# ---------------------------------------------------------------------------
# resolve_period_dir — explicit --period boundary (Phase 2D.1-R5 final gate
# fix). A period must be a closed YYYY-MM identifier resolving, after
# symlink resolution, strictly inside the private root.
# ---------------------------------------------------------------------------


def test_resolve_period_dir_valid_explicit_period_accepted(tmp_path):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()

    result = runner.resolve_period_dir(tmp_path, "2026-01")

    assert result == period_dir.resolve(strict=True)


def test_resolve_period_dir_rejects_dotdot(tmp_path):
    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, "..")
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_resolve_period_dir_rejects_relative_escape(tmp_path):
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, "../outside")
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_resolve_period_dir_rejects_absolute_path(tmp_path):
    external = tmp_path.parent / "external-2026-02"
    external.mkdir()

    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, str(external))
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_resolve_period_dir_rejects_nested_path(tmp_path):
    nested = tmp_path / "2026-01" / "sub"
    nested.mkdir(parents=True)
    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, "2026-01/sub")
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_resolve_period_dir_rejects_symlink_outside_private_root(tmp_path):
    outside = tmp_path.parent / "outside-2026-01"
    outside.mkdir()
    (tmp_path / "2026-01").symlink_to(outside, target_is_directory=True)

    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, "2026-01")
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_resolve_period_dir_rejects_symlink_into_repository(tmp_path):
    repo_root = Path(runner.__file__).resolve().parents[2]
    (tmp_path / "2026-01").symlink_to(repo_root, target_is_directory=True)

    with pytest.raises(runner.AcceptanceError) as exc_info:
        runner.resolve_period_dir(tmp_path, "2026-01")
    assert exc_info.value.reason_code == runner.REASON_PERIOD_NOT_FOUND


def test_run_scenario_period_escape_never_invokes_scenario_function(monkeypatch, tmp_path, capsys):
    """An escape attempt through --period must be rejected by
    resolve_period_dir BEFORE run_scenario ever calls the scenario
    function (which is what would read backfill-plan.json / expected
    material and execute backfill) — and stdout stays exactly the
    redacted PASS/FAIL line, never the rejected path."""
    called: list[Path] = []

    def spy(period_dir):
        called.append(period_dir)
        return {"scenario": "P1_IMPORT"}

    monkeypatch.setitem(runner.SCENARIOS, "P1_IMPORT", spy)

    result = runner.run_scenario(tmp_path, "..", "P1_IMPORT")

    assert result is False
    assert called == []  # the scenario function was never invoked
    captured = capsys.readouterr()
    assert captured.out == "P1_IMPORT: FAIL\n"
    assert captured.err == ""
    assert ".." not in captured.out
    assert str(tmp_path) not in captured.out


def test_resolve_period_dir_autodiscovery_still_finds_latest(tmp_path):
    (tmp_path / "2025-11").mkdir()
    (tmp_path / "2026-01").mkdir()
    (tmp_path / "not-a-period").mkdir()

    result = runner.resolve_period_dir(tmp_path, None)

    assert result == (tmp_path / "2026-01").resolve(strict=True)


# ---------------------------------------------------------------------------
# P2D_CUTOVER_RECONCILIATION — synthetic end-to-end, public-safe throughout.
# ---------------------------------------------------------------------------


def _write_synthetic_ledger(path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报关出口购销合同"
    ws.append(["Title"])
    ws.append(["序号", "合同编码", "卖方", "买方", "金额"])
    ws.append([1, "C-SYNTH", "SupplierSynth", "BuyerSynth", 100])
    wb.save(path)


def test_p2d_missing_plan_is_not_ready_and_redacted(tmp_path, capsys):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()

    result = runner.run_scenario(tmp_path, "2026-01", "P2D_CUTOVER_RECONCILIATION")
    assert result is False
    captured = capsys.readouterr()
    assert captured.out == "P2D_CUTOVER_RECONCILIATION: FAIL\n"
    report = (tmp_path / "reports" / "P2D_CUTOVER_RECONCILIATION.json").read_text()
    assert "NOT_READY" in report


def test_p2d_missing_baseline_is_reported_privately_only(tmp_path, capsys):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()
    (period_dir / "contracts").mkdir()
    _write_synthetic_ledger(period_dir / "contracts" / "ledger.xlsx")
    (period_dir / "backfill-plan.json").write_text('{"version": 1, "contracts": {"path": "contracts/ledger.xlsx"}}')

    result = runner.run_scenario(tmp_path, "2026-01", "P2D_CUTOVER_RECONCILIATION")
    assert result is False
    captured = capsys.readouterr()
    assert captured.out == "P2D_CUTOVER_RECONCILIATION: FAIL\n"
    assert captured.err == ""


def test_p2d_pass_end_to_end_stdout_is_scenario_id_only(tmp_path, capsys):
    period_dir = tmp_path / "2026-01"
    (period_dir / "contracts").mkdir(parents=True)
    (period_dir / "expected").mkdir()
    _write_synthetic_ledger(period_dir / "contracts" / "ledger.xlsx")
    (period_dir / "backfill-plan.json").write_text('{"version": 1, "contracts": {"path": "contracts/ledger.xlsx"}}')
    baseline = {
        "entries": [
            {
                "key": "contract:contract_no=C-SYNTH|counterparty=SupplierSynth",
                "expected": {
                    "contract_type": "出口报关购销合同", "buyer": "BuyerSynth", "gross_amount": "100.00",
                    "currency": "CNY", "contract_date": None,
                },
                "outcome": "MATCH",
            },
            {
                "key": "unresolved_indicator:contract_no=C-SYNTH|counterparty=SupplierSynth",
                "expected": {"has_unresolved": False}, "outcome": "MATCH",
            },
        ]
    }
    (period_dir / "expected" / "cutover-baseline.json").write_text(__import__("json").dumps(baseline))

    result = runner.run_scenario(tmp_path, "2026-01", "P2D_CUTOVER_RECONCILIATION")
    captured = capsys.readouterr()
    assert result is True
    assert captured.out == "P2D_CUTOVER_RECONCILIATION: PASS\n"
    assert captured.err == ""
    # The full diagnostic (business identities, amounts) lands ONLY in
    # the private report — never in stdout (spec section 35, HARD).
    report_path = tmp_path / "reports" / "P2D_CUTOVER_RECONCILIATION.json"
    assert report_path.exists()
    assert "C-SYNTH" in report_path.read_text()
    assert "C-SYNTH" not in captured.out
