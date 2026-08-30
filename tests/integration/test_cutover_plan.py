"""Phase 2D.1-R5 — backfill source plan: closed sections, path safety
(section 10/58), and one end-to-end run wiring Contract -> ContractItem
-> Shipment -> SalesContract -> ProcurementSalesLink together.
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
CONTRACT_HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


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
    ledger_path = period_dir / "contracts" / "ledger.xlsx"
    _write_ledger(ledger_path, [[1, "C001", "SellerA", "BuyerX", 100]])

    plan = {
        "version": 1,
        "contracts": {"path": "contracts/ledger.xlsx"},
        "contract_items": {
            "entries": [
                {"contract_no": "C001", "counterparty": "SellerA", "source_item_key": "ITEM-1", "fields": {"product_name": "Widget"}}
            ]
        },
        "shipments": {
            "entries": [
                {"contract_no": "C001", "counterparty": "SellerA", "external_reference": "EXP-1", "execution_date": "2026-01-15", "quantity": "10"}
            ]
        },
        "sales_contracts": {"entries": [{"our_entity": "Entity A", "sales_contract_no": "SC-1", "fields": {"customer": "Cust"}}]},
        "procurement_sales_links": {
            "entries": [{"contract_no": "C001", "counterparty": "SellerA", "sales_our_entity": "Entity A", "sales_contract_no": "SC-1"}]
        },
    }
    result = run_backfill_plan(db_session, plan, period_dir=period_dir, created_at=NOW)
    assert result.sections["contracts"]["created"] == 1
    assert result.sections["contract_items"]["created"] == 1
    assert result.sections["shipments"]["created"] == 1
    assert result.sections["sales_contracts"]["created"] == 1
    assert result.sections["procurement_sales_links"]["created"] == 1

    contract = ContractRepository(db_session).find_by_contract_no("C001")[0]
    assert len(ContractItemRepository(db_session).list_for_contract(contract.id)) == 1
    sales_contract = SalesContractRepository(db_session).list_all()[0]
    assert ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id) is not None


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
