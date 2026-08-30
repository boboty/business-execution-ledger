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
    engine = make_engine(":memory:")
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
    # The path resolves inside the period dir, but the FILE is not a
    # valid contract ledger workbook — this proves the plan mechanism
    # has no special affinity for expected/ and would fail to parse it
    # as a source, never silently treat it as one.
    with pytest.raises(Exception):
        run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)
