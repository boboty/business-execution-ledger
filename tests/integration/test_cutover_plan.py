"""Phase 2D.1-R5 — backfill source plan: closed sections, path safety
(section 10/58), and one end-to-end run wiring Contract backfill through
to the automatic legacy-ledger sales-scope basis (SalesContract +
ProcurementSalesLink, both from GENUINE same-row Evidence — gate-fix
section 2). ``contract_items``/``shipments``/``sales_contracts``/
``procurement_sales_links`` are deliberately NOT plan sections any
more — see ``bel.application.cutover_plan``'s module docstring.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from bel.application.cutover_plan import (
    CutoverPlanError,
    CutoverPlanExpectedPath,
    CutoverPlanPathEscape,
    run_backfill_plan,
    validate_plan,
)
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
)

NOW = datetime.now(timezone.utc)
CONTRACT_HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "外销合同编码"]


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _write_ledger(path: Path, rows: list[list]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报关出口购销合同"
    ws.append(["Title"])
    ws.append(CONTRACT_HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_unknown_section_rejected():
    with pytest.raises(CutoverPlanError):
        validate_plan({"totally_made_up": {}})


def test_unsupported_version_rejected():
    with pytest.raises(CutoverPlanError):
        validate_plan({"version": 999})


def test_path_traversal_rejected(db_session, tmp_path):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()
    plan = {"contracts": {"path": "../escape.xlsx"}}
    with pytest.raises(CutoverPlanPathEscape):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)


def test_absolute_path_rejected(db_session, tmp_path):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()
    plan = {"contracts": {"path": "/etc/passwd"}}
    with pytest.raises(CutoverPlanPathEscape):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)


def test_symlink_escape_rejected(db_session, tmp_path):
    period_dir = tmp_path / "2026-01"
    period_dir.mkdir()
    outside = tmp_path / "outside.xlsx"
    _write_ledger(outside, [[1, "C001", "SellerA", "BuyerX", 100]])
    link = period_dir / "linked.xlsx"
    link.symlink_to(outside)
    plan = {"contracts": {"path": "linked.xlsx"}}
    with pytest.raises(CutoverPlanPathEscape):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)


def test_end_to_end_plan_wires_full_chain(db_session, tmp_path):
    period_dir = tmp_path / "2026-01"
    (period_dir / "contracts").mkdir(parents=True)
    (period_dir / "facts").mkdir(parents=True)
    ledger_path = period_dir / "contracts" / "ledger.xlsx"
    # Column 6 (外销合同编码) on the SAME row as the procurement contract
    # is the frozen genuine-Evidence basis for the sales scope + link —
    # no separate plan section needed for either.
    _write_ledger(ledger_path, [[1, "C001", "SellerA", "BuyerX", 100, "SC-1"]])

    pack_path = period_dir / "facts" / "cutover-pack.json"
    pack_path.write_text(
        json.dumps(
            {
                "contract_items": [
                    {
                        "contract_selector": {"contract_no": "C001", "counterparty": "SellerA"},
                        "source_item_key": "ITEM-1", "product_name": "Widget",
                    }
                ]
            }
        )
    )

    plan = {
        "version": 1,
        "contracts": {"path": "contracts/ledger.xlsx"},
        "cutover_fact_pack": {"path": "facts/cutover-pack.json"},
    }
    result = run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)
    assert result.sections["contracts"]["created"] == 1
    assert result.sections["cutover_fact_pack"]["contract_items_created"] == 1

    contract = ContractRepository(db_session).find_by_contract_no("C001")[0]
    assert len(ContractItemRepository(db_session).list_for_contract(contract.id)) == 1
    # The sales scope + link were established automatically from the
    # SAME contract-ledger row's own genuine Evidence — never via a
    # plan-level "entries" assertion.
    sales_contract = SalesContractRepository(db_session).list_all()[0]
    assert sales_contract.sales_contract_no == "SC-1"
    assert sales_contract.our_entity == "BuyerX"
    assert sales_contract.customer is None  # never inferred
    assert ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id) is not None


def test_plan_rejects_removed_entries_sections(db_session, tmp_path):
    """contract_items/shipments/sales_contracts/procurement_sales_links
    are no longer valid plan sections (gate-fix section 2, HARD) — a
    plan naming them is rejected outright, never silently ignored."""
    for section in ("contract_items", "shipments", "sales_contracts", "procurement_sales_links"):
        with pytest.raises(CutoverPlanError):
            validate_plan({section: {"entries": []}})


def test_plan_never_reads_expected_directory(db_session, tmp_path):
    period_dir = tmp_path / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    (period_dir / "expected" / "cutover-baseline.json").write_text(json.dumps({"entries": []}))
    plan = {"contracts": {"path": "expected/cutover-baseline.json"}}
    # Rejected at the path boundary — never opened, never parsed.
    with pytest.raises(CutoverPlanExpectedPath):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)


def test_valid_source_workbook_inside_expected_is_rejected_at_the_boundary(db_session, tmp_path):
    """The strongest form of the section-47 guard: the file inside
    expected/ is a perfectly VALID contract-ledger workbook, so nothing
    but the path boundary itself can stop it — and it does, before the
    file is ever opened or parsed."""
    period_dir = tmp_path / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    ledger_path = period_dir / "expected" / "ledger.xlsx"
    _write_ledger(ledger_path, [[1, "C001", "SellerA", "BuyerX", 100]])

    plan = {"contracts": {"path": "expected/ledger.xlsx"}}
    with pytest.raises(CutoverPlanExpectedPath):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)
    assert ContractRepository(db_session).list_all() == []  # nothing was imported

    # The very same workbook is importable from a legitimate location,
    # proving the rejection is about the path and not the file.
    (period_dir / "contracts").mkdir()
    ledger_path.rename(period_dir / "contracts" / "ledger.xlsx")
    result = run_backfill_plan(
        db_session, {"contracts": {"path": "contracts/ledger.xlsx"}}, period_dir=period_dir, created_at=NOW
    )
    assert result.sections["contracts"]["created"] == 1


def test_source_path_resolving_into_expected_via_symlink_is_rejected(db_session, tmp_path):
    """A symlink whose RESOLVED target sits inside expected/ is the same
    violation as naming expected/ literally — the plan's own path string
    never mentions expected/ at all."""
    period_dir = tmp_path / "2026-01"
    (period_dir / "expected").mkdir(parents=True)
    hidden = period_dir / "expected" / "ledger.xlsx"
    _write_ledger(hidden, [[1, "C001", "SellerA", "BuyerX", 100]])
    link = period_dir / "innocent-name.xlsx"
    link.symlink_to(hidden)

    plan = {"contracts": {"path": "innocent-name.xlsx"}}
    with pytest.raises(CutoverPlanExpectedPath):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)
    assert ContractRepository(db_session).list_all() == []


def test_malformed_late_section_is_rejected_before_backfill(monkeypatch, db_session, tmp_path):
    from datetime import datetime, timezone
    from bel.application import cutover_plan
    calls = []
    (tmp_path / 'contracts.xlsx').write_bytes(b'')

    def backfill(*args, **kwargs):
        calls.append(True)
        return cutover_plan.BackfillOutcome()

    monkeypatch.setattr(cutover_plan, 'backfill_contracts', backfill)
    plan = {'version': 1, 'contracts': {'path': 'contracts.xlsx'}, 'payments': [None]}
    with pytest.raises(cutover_plan.CutoverPlanError):
        cutover_plan.run_backfill_plan(db_session, plan, period_dir=tmp_path, created_at=datetime.now(timezone.utc))
    assert calls == []
