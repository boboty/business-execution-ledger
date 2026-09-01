"""CLI smoke test for `bel invoice-preparation export` — real SQLite file
via subprocess, same convention as test_period_close_export_cli.py.
Confirms the CLI writes a real file (never stdout binary) using the SAME
Application Data Product path Web uses (workbench -> data product ->
serializer), and performs zero database writes.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from pathlib import Path

import openpyxl

from bel.application.invoice_preparation_export import (
    build_invoice_preparation_data_product,
    export_invoice_preparation_csv,
    export_invoice_preparation_xlsx,
)
from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from tests.web.test_web_invoice_preparation import _build_workbench_db

REPO_ROOT = Path(__file__).parent.parent.parent

EXPECTED_SHEETS = [
    "01_Summary",
    "02_Sales_Preparation",
    "03_Sales_Attention",
    "04_Supplier_Request",
    "05_Supplier_Attention",
]


def _run_bel(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _setup_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "f2b-cli.db"
    _build_workbench_db(str(db_path))
    return db_path


def _expected_product_bytes(db_path: Path):
    """The SAME Application Data Product path Web uses — proof the CLI is
    not doing anything different."""
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        product = build_invoice_preparation_data_product(get_invoice_preparation_workbench(session))
    return export_invoice_preparation_xlsx(product), export_invoice_preparation_csv(product)


def test_cli_export_xlsx_matches_web_path(tmp_path):
    db_path = _setup_db(tmp_path)
    expected_xlsx, _ = _expected_product_bytes(db_path)
    out = tmp_path / "out.xlsx"
    result = _run_bel(db_path, "invoice-preparation", "export", "--format", "xlsx", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert out.read_bytes() == expected_xlsx
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == EXPECTED_SHEETS


def test_cli_export_csv_matches_web_path(tmp_path):
    db_path = _setup_db(tmp_path)
    _, expected_csv = _expected_product_bytes(db_path)
    out = tmp_path / "out.csv"
    result = _run_bel(db_path, "invoice-preparation", "export", "--format", "csv", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert out.read_bytes() == expected_csv
    records = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert records and {r["record_type"] for r in records} == {
        "SALES_PREPARATION",
        "SALES_ATTENTION",
        "SUPPLIER_REQUEST",
        "SUPPLIER_ATTENTION",
    }


def test_cli_export_writes_only_the_output_file(tmp_path):
    """The CLI writes the requested file and performs zero database writes
    (the export is a pure projection of the Workbench)."""
    db_path = _setup_db(tmp_path)
    from bel.infrastructure.persistence import models as m

    def _counts():
        engine = make_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        with make_session_factory(engine)() as session:
            counts = {}
            for name in dir(m):
                obj = getattr(m, name)
                if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                    counts[obj.__tablename__] = session.query(obj).count()
            return counts

    before = _counts()
    out = tmp_path / "out.xlsx"
    result = _run_bel(db_path, "invoice-preparation", "export", "--format", "xlsx", "--output", str(out))
    assert result.returncode == 0, result.stderr
    assert _counts() == before


def test_cli_export_rejects_bad_format(tmp_path):
    db_path = _setup_db(tmp_path)
    out = tmp_path / "out.xlsx"
    result = _run_bel(db_path, "invoice-preparation", "export", "--format", "pdf", "--output", str(out))
    assert result.returncode != 0
    assert not out.exists()
