"""Hardened private-data-root / period containment, descriptor-anchored
private input reads, and report-write tests (shared helpers used by the
FIRST-STAGE CUTOVER GATE).

Every rule here is the R5 discipline: the private root must resolve to a
real directory outside the repository; a period is a closed YYYY-MM
identifier opened through a descriptor-anchored ``PrivatePeriodReader``
with NO symlink allowed in ANY component; private reports are written only
under ``<root>/reports/`` through a check/write gap-proof descriptor
(``O_DIRECTORY`` + ``O_NOFOLLOW``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bel.infrastructure.private_paths import (
    REASON_INPUT_ESCAPE,
    REASON_INPUT_MISSING,
    REASON_INPUT_TOO_LARGE,
    REASON_INPUT_UNSAFE_TYPE,
    REASON_INVALID_PERIOD,
    REASON_PERIOD_ESCAPE,
    REASON_PERIOD_NOT_FOUND,
    REASON_ROOT_INSIDE_REPO,
    REASON_ROOT_NOT_DIR,
    REASON_ROOT_NOT_SET,
    PrivatePeriodReader,
    PrivateRootError,
    resolve_private_root,
    write_private_report,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _make_root(tmp_path) -> Path:
    root = tmp_path / "private"
    root.mkdir()
    return root


def _period_root(tmp_path) -> Path:
    root = _make_root(tmp_path)
    period_dir = root / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# resolve_private_root
# ---------------------------------------------------------------------------


def test_root_not_set_raises(monkeypatch):
    monkeypatch.delenv("BEL_PRIVATE_DATA_ROOT", raising=False)
    with pytest.raises(PrivateRootError) as excinfo:
        resolve_private_root()
    assert excinfo.value.reason_code == REASON_ROOT_NOT_SET


def test_explicit_root_outside_repo_is_accepted(tmp_path):
    root = _make_root(tmp_path)
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
    root = _make_root(tmp_path)
    monkeypatch.setenv("BEL_PRIVATE_DATA_ROOT", str(root))
    assert resolve_private_root() == root.resolve()


# ---------------------------------------------------------------------------
# PrivatePeriodReader.open — strict YYYY-MM, real period dir, NO symlink
# ---------------------------------------------------------------------------


def test_valid_period_opens_inside_root(tmp_path):
    root = _period_root(tmp_path)
    with PrivatePeriodReader.open(root, "2026-01"):
        pass  # opens cleanly and closes


def test_invalid_period_strings_rejected(tmp_path):
    root = _make_root(tmp_path)
    for bad in ("../escape", "2026/07", "2026-7", "abc", "2026-13", "2026-00", "2026-01/../escape", "/abs"):
        with pytest.raises(PrivateRootError) as excinfo:
            PrivatePeriodReader.open(root, bad)
        assert excinfo.value.reason_code == REASON_INVALID_PERIOD, bad


def test_missing_period_dir_rejected(tmp_path):
    root = _make_root(tmp_path)
    with pytest.raises(PrivateRootError) as excinfo:
        PrivatePeriodReader.open(root, "2026-07")
    assert excinfo.value.reason_code == REASON_PERIOD_NOT_FOUND


def test_period_symlink_outside_root_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = _make_root(tmp_path)
    os.symlink(outside, root / "2026-07")
    with pytest.raises(PrivateRootError) as excinfo:
        PrivatePeriodReader.open(root, "2026-07")
    assert excinfo.value.reason_code == REASON_PERIOD_ESCAPE


def test_period_symlink_inside_root_also_rejected(tmp_path):
    """The strong Gate rule: NO symlink in ANY input path component — even
    a period symlink resolving back inside the private root is rejected
    (the descriptor O_NOFOLLOW open refuses it)."""
    root = _make_root(tmp_path)
    real = root / "real-2026-07"
    real.mkdir()
    os.symlink(real, root / "2026-07")
    with pytest.raises(PrivateRootError) as excinfo:
        PrivatePeriodReader.open(root, "2026-07")
    assert excinfo.value.reason_code == REASON_PERIOD_ESCAPE


def test_reader_rejects_reads_after_close(tmp_path):
    root = _period_root(tmp_path)
    reader = PrivatePeriodReader.open(root, "2026-01")
    reader.close()
    with pytest.raises(PrivateRootError):
        reader.read("backfill-plan.json")


# ---------------------------------------------------------------------------
# PrivatePeriodReader.read — descriptor-anchored, no symlinks, regular files
# ---------------------------------------------------------------------------


def test_reads_real_file_inside_period(tmp_path):
    root = _period_root(tmp_path)
    (root / "2026-01" / "expected" / "cutover-baseline.json").write_text('{"entries": []}')
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        assert reader.read("expected/cutover-baseline.json") == b'{"entries": []}'


def test_missing_file_is_missing_not_escape(tmp_path):
    root = _period_root(tmp_path)
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_MISSING


def test_directory_named_as_file_is_missing(tmp_path):
    root = _period_root(tmp_path)
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("backfill-plan.json")  # only `expected/` exists
        assert excinfo.value.reason_code == REASON_INPUT_MISSING


def test_rejects_arbitrary_paths(tmp_path):
    root = _period_root(tmp_path)
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        for bad in ("/etc/passwd", "~/x", "../escape.json", "expected/../../etc/passwd", "expected//x", ""):
            with pytest.raises(PrivateRootError) as excinfo:
                reader.read(bad)
            assert excinfo.value.reason_code == REASON_INPUT_ESCAPE, bad


def test_rejects_expected_dir_symlink_outside_root(tmp_path):
    root = _period_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cutover-baseline.json").write_text("outside")
    os.rmdir(root / "2026-01" / "expected")
    os.symlink(outside, root / "2026-01" / "expected")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_rejects_plan_file_symlink_outside_root(tmp_path):
    root = _period_root(tmp_path)
    outside = tmp_path / "outside-file.json"
    outside.write_text("outside plan")
    os.symlink(outside, root / "2026-01" / "backfill-plan.json")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("backfill-plan.json")
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_rejects_baseline_file_symlink_outside_root(tmp_path):
    root = _period_root(tmp_path)
    outside = tmp_path / "outside-baseline.json"
    outside.write_text("outside baseline")
    os.symlink(outside, root / "2026-01" / "expected" / "cutover-baseline.json")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_rejects_symlink_that_stays_inside_root(tmp_path):
    """The strong Gate rule applies to a final control file too: a baseline
    symlink resolving back inside the root is still a symlink and is
    rejected — never accepted merely because its target is contained."""
    root = _period_root(tmp_path)
    real = root / "2026-01" / "real-baseline.json"
    real.write_text('{"entries": [1]}')
    os.symlink(real, root / "2026-01" / "expected" / "cutover-baseline.json")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_rejects_nested_escape_into_repository(tmp_path):
    root = _period_root(tmp_path)
    os.rmdir(root / "2026-01" / "expected")
    os.symlink(_repo_root() / "docs", root / "2026-01" / "expected")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        # docs/ARCHITECTURE.md exists in the repo: an escape, never read.
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/ARCHITECTURE.md")
        assert excinfo.value.reason_code == REASON_INPUT_ESCAPE


def test_rejects_fifo_without_blocking(tmp_path):
    """A planted FIFO at the input path must be rejected by type (never
    blocked on, never read)."""
    root = _period_root(tmp_path)
    os.mkfifo(root / "2026-01" / "expected" / "cutover-baseline.json")
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_UNSAFE_TYPE


def test_rejects_oversized_input(monkeypatch, tmp_path):
    root = _period_root(tmp_path)
    (root / "2026-01" / "expected" / "cutover-baseline.json").write_text("x" * 4096)
    monkeypatch.setattr("bel.infrastructure.private_paths._PRIVATE_INPUT_MAX_BYTES", 1024)
    with PrivatePeriodReader.open(root, "2026-01") as reader:
        with pytest.raises(PrivateRootError) as excinfo:
            reader.read("expected/cutover-baseline.json")
        assert excinfo.value.reason_code == REASON_INPUT_TOO_LARGE


def test_reader_anchors_to_original_period_dir_on_replacement(monkeypatch, tmp_path):
    """Deterministic descriptor-anchoring proof: once the reader has opened
    the period directory, renaming/replacing it in the filesystem cannot
    redirect reads — the anchored descriptor reads the ORIGINAL directory."""
    import bel.infrastructure.private_paths as pp

    root = _period_root(tmp_path)
    (root / "2026-01" / "backfill-plan.json").write_text("ORIGINAL-PLAN")

    swapped = {"n": 0}

    def _swap():
        swapped["n"] += 1
        # Replace the period directory with a symlink to an outside tree
        # carrying a DIFFERENT plan.
        outside = root / "outside"
        outside.mkdir()
        (outside / "backfill-plan.json").write_text("OUTSIDE-PLAN")
        os.rename(root / "2026-01", root / "2026-01.original")
        os.symlink(outside, root / "2026-01")

    monkeypatch.setattr(pp, "_input_read_test_hook", _swap)
    try:
        with PrivatePeriodReader.open(root, "2026-01") as reader:
            data = reader.read("backfill-plan.json")
        assert swapped["n"] == 1
        # The read came from the ORIGINAL anchored directory, never the
        # outside replacement.
        assert data == b"ORIGINAL-PLAN"
    finally:
        monkeypatch.setattr(pp, "_input_read_test_hook", None)


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
