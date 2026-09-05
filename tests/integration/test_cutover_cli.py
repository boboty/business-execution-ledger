"""CLI smoke test for `bel cutover backfill` / `bel cutover reconcile`
(Phase 2D.1-R5) against a real migrated SQLite file and a synthetic
BEL_PRIVATE_DATA_ROOT — proves the actual `bel` entry point wires the
plan/baseline path resolution and prints only the scenario verdict.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

CONTRACT_HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


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


def _write_ledger(path: Path, rows: list[list]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报关出口购销合同"
    ws.append(["Title"])
    ws.append(CONTRACT_HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_cutover_backfill_and_reconcile_cli(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)

    private_root = tmp_path / "private"
    period_dir = private_root / "2026-01"
    (period_dir / "contracts").mkdir(parents=True)
    (period_dir / "expected").mkdir(parents=True)
    _write_ledger(period_dir / "contracts" / "ledger.xlsx", [[1, "C-CLI", "SupplierCLI", "BuyerCLI", 42]])
    (period_dir / "backfill-plan.json").write_text(
        json.dumps({"version": 1, "contracts": {"path": "contracts/ledger.xlsx"}})
    )
    baseline = {
        "entries": [
            {
                "key": "contract:contract_no=C-CLI|counterparty=SupplierCLI",
                "expected": {
                    "contract_type": "出口报关购销合同", "buyer": "BuyerCLI", "gross_amount": "42.00",
                    "currency": "CNY", "contract_date": None,
                },
                "outcome": "MATCH",
            },
            {
                "key": "unresolved_indicator:contract_no=C-CLI|counterparty=SupplierCLI",
                "expected": {"has_unresolved": False}, "outcome": "MATCH",
            },
        ]
    }
    (period_dir / "expected" / "cutover-baseline.json").write_text(json.dumps(baseline))

    backfill_result = _run_bel(db_path, private_root, "cutover", "backfill", "--period", "2026-01")
    assert backfill_result.returncode == 0, backfill_result.stderr
    assert backfill_result.stdout.strip() == "P2D_CUTOVER_BACKFILL: DONE"
    # No business counts, identities, or amounts ever printed to stdout.
    assert "created=" not in backfill_result.stdout
    assert "tasks=" not in backfill_result.stdout
    assert "SupplierCLI" not in backfill_result.stdout

    backfill_report_path = private_root / "reports" / "cutover-backfill-2026-01.json"
    assert backfill_report_path.exists()
    backfill_report = json.loads(backfill_report_path.read_text(encoding="utf-8"))
    assert backfill_report["sections"]["contracts"]["created"] == 1

    reconcile_result = _run_bel(db_path, private_root, "cutover", "reconcile", "--period", "2026-01")
    assert reconcile_result.returncode == 0, reconcile_result.stderr
    assert reconcile_result.stdout.strip() == "P2D_CUTOVER_RECONCILIATION: PASS"
    # No unresolved_count, business identity, or amount ever printed to stdout.
    assert "unresolved_count" not in reconcile_result.stdout
    assert "SupplierCLI" not in reconcile_result.stdout

    reconcile_report_path = private_root / "reports" / "cutover-reconciliation-2026-01.json"
    assert reconcile_report_path.exists()
    reconcile_report = json.loads(reconcile_report_path.read_text(encoding="utf-8"))
    assert reconcile_report["unresolved_count"] == 0
    assert len(reconcile_report["entries"]) == 2


def test_cutover_backfill_rejects_period_dir_outside_private_root(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    private_root = tmp_path / "private"
    private_root.mkdir()

    result = _run_bel(db_path, private_root, "cutover", "backfill", "--period", "../escape")
    assert result.returncode != 0
    assert "does not resolve inside" in (result.stdout + result.stderr)


def test_cutover_reconcile_missing_private_root_is_a_clean_error(tmp_path):
    db_path = tmp_path / "bel.db"
    _upgrade_head(db_path)
    nonexistent_root = tmp_path / "does-not-exist"

    result = _run_bel(db_path, nonexistent_root, "cutover", "reconcile", "--period", "2026-01")
    assert result.returncode != 0
    assert "does not resolve" in (result.stdout + result.stderr)


def test_cutover_reconcile_malformed_json_is_private_failure(tmp_path):
    db_path = tmp_path / 'bel.db'
    _upgrade_head(db_path)
    root = tmp_path / 'private'
    expected = root / '2026-01' / 'expected'
    expected.mkdir(parents=True)
    (expected / 'cutover-baseline.json').write_text('{SYNTHETIC_SENSITIVE_MARKER')
    result = _run_bel(db_path, root, 'cutover', 'reconcile', '--period', '2026-01')
    assert result.returncode != 0
    assert result.stdout.strip() == 'P2D_CUTOVER_RECONCILIATION: FAIL'
    assert result.stderr == ''
    assert 'SYNTHETIC_SENSITIVE_MARKER' not in result.stdout
    assert (root / 'reports' / 'cutover-reconciliation-2026-01.json').is_file()
