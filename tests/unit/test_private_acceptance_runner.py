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
