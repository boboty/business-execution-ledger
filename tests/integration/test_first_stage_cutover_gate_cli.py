"""CLI smoke test for `bel cutover gate` (docs/FIRST-STAGE-CUTOVER-GATE.md).

The Gate is PostgreSQL-only, so the plain SQLite-convenience suite proves
the CLI seam itself: the strict period handling, the private-root
containment, and — most importantly — that public stdout is exactly the
safe verdict line (``FIRST_STAGE_CUTOVER_GATE: PASS`` / ``FAIL``) with no
traceback and no private-derived value even when the Gate FAILs. The real
PostgreSQL PASS path is covered by the ``@pytest.mark.postgres``
integration test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent


def _run_bel(db_path: Path, private_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_PRIVATE_DATA_ROOT": str(private_root), "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _upgrade_head(db_path: Path) -> None:
    from bel.infrastructure.persistence.database import make_engine
    from bel.infrastructure.persistence.models import Base

    Base.metadata.create_all(make_engine(f"sqlite:///{db_path}"))


def _make_period_root(tmp_path, period: str = "2026-01") -> Path:
    root = tmp_path / "private"
    period_dir = root / period
    (period_dir / "expected").mkdir(parents=True)
    (period_dir / "backfill-plan.json").write_text(json.dumps({"version": 1}))
    (period_dir / "expected" / "cutover-baseline.json").write_text(json.dumps({"entries": []}))
    return root


def test_cutover_gate_rejects_sqlite_with_safe_verdict(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    private_root = _make_period_root(tmp_path)

    result = _run_bel(db_path, private_root, "cutover", "gate", "--period", "2026-01")
    # SQLite is rejected for the real Gate: a clean FAIL verdict, not a
    # crash.
    assert result.returncode == 1
    assert result.stdout.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL"
    # Privacy-safe stdout: no traceback, no dialect error text, no counts.
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "sqlite" not in result.stdout.lower()
    assert "FIRST_STAGE_CUTOVER_GATE: PASS" not in result.stdout


def test_cutover_gate_missing_private_root_is_a_clean_fail(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    nonexistent_root = tmp_path / "does-not-exist"

    result = _run_bel(db_path, nonexistent_root, "cutover", "gate", "--period", "2026-01")
    assert result.returncode == 1
    assert result.stdout.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr


def test_cutover_gate_invalid_period_is_a_clean_fail(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    private_root = _make_period_root(tmp_path)

    for bad_period in ("../escape", "2026/07", "2026-13", "not-a-period"):
        result = _run_bel(db_path, private_root, "cutover", "gate", "--period", bad_period)
        assert result.returncode == 1, bad_period
        assert result.stdout.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL", bad_period
        assert "Traceback" not in result.stdout, bad_period
        assert "Traceback" not in result.stderr, bad_period


def test_cutover_gate_missing_period_dir_is_a_clean_fail(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    private_root = tmp_path / "private"
    private_root.mkdir()

    result = _run_bel(db_path, private_root, "cutover", "gate", "--period", "2026-01")
    assert result.returncode == 1
    assert result.stdout.strip() == "FIRST_STAGE_CUTOVER_GATE: FAIL"
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
