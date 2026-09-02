"""Hardened private-data-root / period containment and report-write tests
(shared helpers used by the FIRST-STAGE CUTOVER GATE).

Every rule here is the R5 discipline: the private root must resolve to a
real directory outside the repository, a period is a closed YYYY-MM
identifier that must survive symlink resolution inside that root, and
private reports are written only under ``<root>/reports/`` through a
check/write gap-proof descriptor (``O_DIRECTORY`` + ``O_NOFOLLOW``).
"""

from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from bel.infrastructure.private_paths import (
    REASON_INPUT_ESCAPE,
    REASON_INPUT_MISSING,
    REASON_INVALID_PERIOD,
    REASON_PERIOD_ESCAPE,
    REASON_PERIOD_NOT_FOUND,
    REASON_ROOT_INSIDE_REPO,
    REASON_ROOT_NOT_DIR,
    REASON_ROOT_NOT_SET,
    PrivateRootError,
    read_private_file,
    resolve_period_dir,
    resolve_private_root,
    write_private_report,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# resolve_private_root
# ---------------------------------------------------------------------------


def test_root_not_set_raises(monkeypatch):
    monkeypatch.delenv("BEL_PRIVATE_DATA_ROOT", raising=False)
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_private_root()
    assert excinfo.value.reason_code == REASON_ROOT_NOT_SET


def test_explicit_root_outside_repo_is_accepted(tmp_path):
    root = tmp_path / "private"
    root.mkdir()
    assert resolve_private_root(root) == root.resolve()


def test_explicit_root_inside_repo_is_rejected():
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_private_root(_repo_root())
    assert excinfo.value.reason_code == REASON_ROOT_INSIDE_REPO


def test_nested_repo_subdirectory_is_rejected():
    nested = _repo_root() / "docs"
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_private_root(nested)
    assert excinfo.value.reason_code == REASON_ROOT_INSIDE_REPO


def test_root_that_is_not_a_directory_is_rejected(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_private_root(target)
    assert excinfo.value.reason_code == REASON_ROOT_NOT_DIR


def test_nonexistent_root_is_rejected(tmp_path):
    with pytest.raises(PrivateRootError):
        resolve_private_root(tmp_path / "does-not-exist")


def test_env_root_is_used_when_no_explicit_root(tmp_path, monkeypatch):
    root = tmp_path / "private"
    root.mkdir()
    monkeypatch.setenv("BEL_PRIVATE_DATA_ROOT", str(root))
    assert resolve_private_root() == root.resolve()


# ---------------------------------------------------------------------------
# resolve_period_dir — strict YYYY-MM, containment after symlink resolution
# ---------------------------------------------------------------------------


def _make_root(tmp_path) -> Path:
    root = tmp_path / "private"
    root.mkdir()
    return root


def test_valid_period_resolves_inside_root(tmp_path):
    root = _make_root(tmp_path)
    (root / "2026-07").mkdir()
    assert resolve_period_dir(root, "2026-07") == (root / "2026-07").resolve()


def test_invalid_period_strings_rejected(tmp_path):
    root = _make_root(tmp_path)
    for bad in ("../escape", "2026/07", "2026-7", "abc", "2026-13", "2026-00", "2026-01/../escape", "/abs"):
        with pytest.raises(PrivateRootError) as excinfo:
            resolve_period_dir(root, bad)
        assert excinfo.value.reason_code == REASON_INVALID_PERIOD, bad


def test_missing_period_dir_rejected(tmp_path):
    root = _make_root(tmp_path)
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_period_dir(root, "2026-07")
    assert excinfo.value.reason_code == REASON_PERIOD_NOT_FOUND


def test_period_symlink_escaping_root_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _make_root(tmp_path)
    # A same-looking symlinked period directory whose target sits outside
    # the private root must never be accepted.
    os.symlink(outside, root / "2026-07")
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_period_dir(root, "2026-07")
    assert excinfo.value.reason_code == REASON_PERIOD_ESCAPE


def test_period_symlink_inside_root_is_accepted_after_resolution(tmp_path):
    root = _make_root(tmp_path)
    real = root / "real-2026-07"
    real.mkdir()
    os.symlink(real, root / "2026-07")
    resolved = resolve_period_dir(root, "2026-07")
    assert resolved == real.resolve()


# ---------------------------------------------------------------------------
# write_private_report — private-only, symlink-escape-proof
# ---------------------------------------------------------------------------


def test_report_writes_under_private_root(tmp_path):
    root = _make_root(tmp_path)
    ok = write_private_report(root, "first-stage-cutover-gate-2026-07.json", {"period": "2026-07", "passed": True})
    assert ok
    report = root / "reports" / "first-stage-cutover-gate-2026-07.json"
    assert report.exists()
    assert json.loads(report.read_text(encoding="utf-8"))["passed"] is True


def test_report_refuses_when_reports_is_symlink_outside_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _make_root(tmp_path)
    os.symlink(outside, root / "reports")
    ok = write_private_report(root, "first-stage-cutover-gate-2026-07.json", {"x": 1})
    assert ok is False
    assert not (outside / "first-stage-cutover-gate-2026-07.json").exists()


def test_report_refuses_when_reports_symlinks_into_repo(tmp_path):
    root = _make_root(tmp_path)
    os.symlink(_repo_root(), root / "reports")
    ok = write_private_report(root, "first-stage-cutover-gate-2026-07.json", {"x": 1})
    assert ok is False
    assert not (_repo_root() / "first-stage-cutover-gate-2026-07.json").exists()


def test_report_refuses_existing_report_symlink_into_repo(tmp_path):
    root = _make_root(tmp_path)
    (root / "reports").mkdir()
    # Pre-plant a report file that is a symlink into the repository — a
    # final-component swap must refuse rather than follow it.
    os.symlink(_repo_root() / "pyproject.toml", root / "reports" / "first-stage-cutover-gate-2026-07.json")
    ok = write_private_report(root, "first-stage-cutover-gate-2026-07.json", {"x": 1})
    assert ok is False


def test_report_refuses_root_inside_repo():
    ok = write_private_report(_repo_root(), "first-stage-cutover-gate-2026-07.json", {"x": 1})
    assert ok is False


# ---------------------------------------------------------------------------
# read_private_file — hardened private INPUT boundary (G0 repair, Blocker 2)
# ---------------------------------------------------------------------------


def _input_root(tmp_path) -> Path:
    root = tmp_path / "private"
    period_dir = root / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    return root


def test_private_input_reads_real_file_inside_root(tmp_path):
    root = _input_root(tmp_path)
    (root / "2026-01" / "expected" / "cutover-baseline.json").write_text('{"entries": []}')
    assert read_private_file(root, "2026-01/expected/cutover-baseline.json") == b'{"entries": []}'


def test_private_input_missing_file_is_missing_not_escape(tmp_path):
    root = _input_root(tmp_path)
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/expected/cutover-baseline.json")
    assert excinfo.value.reason_code == REASON_INPUT_MISSING


def test_private_input_directory_named_as_file_is_missing(tmp_path):
    root = _input_root(tmp_path)
    # ``expected`` exists but as a directory; a plan named after a dir is
    # not a regular file -> MISSING, never a guessed read.
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/backfill-plan.json")
    assert excinfo.value.reason_code == REASON_INPUT_MISSING


def test_private_input_rejects_arbitrary_paths(tmp_path):
    root = _input_root(tmp_path)
    for bad in ("/etc/passwd", "~/x", "../escape.json", "2026-01/../../etc/passwd", "2026-01//x", ""):
        with pytest.raises(PrivateRootError) as excinfo:
            read_private_file(root, bad)
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE, bad


def test_private_input_rejects_expected_dir_symlink_outside_root(tmp_path):
    root = _input_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cutover-baseline.json").write_text("outside")
    os.rmdir(root / "2026-01" / "expected")  # the fixture-created empty dir
    os.symlink(outside, root / "2026-01" / "expected")
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/expected/cutover-baseline.json")
    assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_private_input_rejects_plan_file_symlink_outside_root(tmp_path):
    root = _input_root(tmp_path)
    outside = tmp_path / "outside-file.json"
    outside.write_text("outside plan")
    os.symlink(outside, root / "2026-01" / "backfill-plan.json")
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/backfill-plan.json")
    assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_private_input_rejects_baseline_file_symlink_outside_root(tmp_path):
    root = _input_root(tmp_path)
    outside = tmp_path / "outside-baseline.json"
    outside.write_text("outside baseline")
    os.symlink(outside, root / "2026-01" / "expected" / "cutover-baseline.json")
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/expected/cutover-baseline.json")
    assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_private_input_rejects_nested_escape_into_repository(tmp_path):
    root = _input_root(tmp_path)
    # ``expected`` symlinks into the repository (a dir outside the root).
    os.rmdir(root / "2026-01" / "expected")
    os.symlink(_repo_root() / "docs", root / "2026-01" / "expected")
    with pytest.raises(PrivateRootError) as excinfo:
        read_private_file(root, "2026-01/expected/ARCHITECTURE.md")
    assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_private_input_accepts_symlink_that_stays_inside_root(tmp_path):
    root = _input_root(tmp_path)
    real = root / "2026-01" / "real-baseline.json"
    real.write_text('{"entries": [1]}')
    os.symlink(real, root / "2026-01" / "expected" / "cutover-baseline.json")
    # Resolves strictly inside the root and is a regular file — accepted,
    # exactly like resolve_period_dir's containment discipline.
    assert read_private_file(root, "2026-01/expected/cutover-baseline.json") == b'{"entries": [1]}'
