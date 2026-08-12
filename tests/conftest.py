from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from sqlalchemy.orm import Session

from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base

PRIMARY_SHEET_NAME = "报关出口购销合同"


@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite with schema created directly from the ORM models.
    Schema-shape fidelity against Alembic migrations is covered
    separately by tests/integration/test_migration.py."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def write_ledger_workbook(
    path: Path,
    headers: list[str],
    data_rows: list[list[Any]],
    sheet_name: str = PRIMARY_SHEET_NAME,
    extra_sheets: list[str] | None = None,
) -> None:
    """Build a minimal workbook matching the adapter's documented shape: row 1 is
    a title row (ignored), row 2 is headers, data starts at row 3."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(["Synthetic Fixture Title Row"])
    ws.append(headers)
    for row in data_rows:
        ws.append(row)
    for name in extra_sheets or []:
        wb.create_sheet(title=name)
    wb.save(path)


@pytest.fixture
def ledger_workbook_factory(tmp_path: Path):
    def _factory(headers, data_rows, sheet_name=PRIMARY_SHEET_NAME, extra_sheets=None, filename="ledger.xlsx"):
        path = tmp_path / filename
        write_ledger_workbook(path, headers, data_rows, sheet_name=sheet_name, extra_sheets=extra_sheets)
        return path

    return _factory


INVOICE_SHEET_NAME = "sheet1"
INVOICE_HEADERS = [
    "凭证模板",
    "凭证字号",
    "发票票种",
    "开票日期",
    "发票号码",
    "数电发票号码",
    "销方名称",
    "商品名称（明细）",
    "规格型号（明细）",
    "单位（明细）",
    "数量（明细）",
    "单价（明细）",
    "金额（明细）",
    "税率（%）（明细）",
    "税额（明细）",
    "价税合计（明细）",
    "发票状态",
    "发票金额",
    "发票税额",
    "发票价税合计",
]


def write_invoice_workbook(path: Path, buyer: str, data_rows: list[list[Any]]) -> None:
    """Row 1 blank, row 2 col A is the buyer, row 3 is headers, data
    starts row 4 — matches the real invoice ledger's shape."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = INVOICE_SHEET_NAME
    ws.append([])
    ws.append([buyer])
    ws.append(INVOICE_HEADERS)
    for row in data_rows:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def invoice_workbook_factory(tmp_path: Path):
    def _factory(data_rows, buyer="Buyer Co", filename="invoices.xlsx"):
        path = tmp_path / filename
        write_invoice_workbook(path, buyer, data_rows)
        return path

    return _factory
