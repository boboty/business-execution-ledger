"""Period Close Data Product web endpoints (Phase 2D.2). Both routes call
the SAME Application Data Product path the CLI uses — this file only
checks the Web transport (content-type, filename, read-only, period
validation), never a second business computation.
"""

from __future__ import annotations

import csv
import io

import openpyxl

from bel.application.period_close_export import build_period_close_data_product
from bel.application.period_close_workbench import get_period_close_workbench
from tests.web.conftest import CLOSE_PERIOD_FIXTURE


def _db_counts(session_factory) -> dict[str, int]:
    from bel.infrastructure.persistence.models import (
        AccrualBasisFactModel,
        AccrualModel,
        AccrualReversalModel,
        BusinessEventModel,
        ContractItemModel,
        ContractModel,
        CostRecognitionFactModel,
        EvidenceDocumentModel,
        EvidenceFragmentModel,
        HistoricalAccrualFactModel,
        ImportRunModel,
        InvoiceAllocationModel,
        InvoiceItemAllocationModel,
        InvoiceItemModel,
        InvoiceModel,
        MatchCandidateModel,
        MatchCaseModel,
        PaymentAllocationModel,
        PaymentModel,
    )

    models = [
        AccrualBasisFactModel,
        AccrualModel,
        AccrualReversalModel,
        BusinessEventModel,
        ContractItemModel,
        ContractModel,
        CostRecognitionFactModel,
        EvidenceDocumentModel,
        EvidenceFragmentModel,
        HistoricalAccrualFactModel,
        ImportRunModel,
        InvoiceAllocationModel,
        InvoiceItemAllocationModel,
        InvoiceItemModel,
        InvoiceModel,
        MatchCandidateModel,
        MatchCaseModel,
        PaymentAllocationModel,
        PaymentModel,
    ]
    with session_factory() as session:
        return {m.__tablename__: session.query(m).count() for m in models}


def test_page_shows_download_links(web_client):
    response = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    html = response.text
    assert f"/period-close/export.xlsx?period={CLOSE_PERIOD_FIXTURE}" in html
    assert f"/period-close/export.csv?period={CLOSE_PERIOD_FIXTURE}" in html


def test_export_xlsx_content_type_and_filename(web_client):
    response = web_client.get(f"/period-close/export.xlsx?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert response.headers["content-disposition"] == f"attachment; filename=period-close-{CLOSE_PERIOD_FIXTURE}.xlsx"

    wb = openpyxl.load_workbook(io.BytesIO(response.content))
    assert wb.sheetnames == [
        "01_Summary",
        "02_Accrual_Required",
        "03_Prior_Accrual_Reversal",
        "04_Actual_Difference",
        "05_Contract_Level_Candidate",
        "06_Blockers",
    ]


def test_export_csv_content_type_and_filename(web_client):
    response = web_client.get(f"/period-close/export.csv?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == f"attachment; filename=period-close-{CLOSE_PERIOD_FIXTURE}.csv"

    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert rows
    assert {r["record_type"] for r in rows} == {
        "ACCRUAL_REQUIRED",
        "PRIOR_ACCRUAL_REVERSAL",
        "ACTUAL_DIFFERENCE",
        "CONTRACT_LEVEL_CANDIDATE",
        "BLOCKER",
    }


def test_export_invalid_period_is_400(web_client):
    response = web_client.get("/period-close/export.xlsx?period=not-a-period")
    assert response.status_code == 400
    response = web_client.get("/period-close/export.csv?period=2031-13")
    assert response.status_code == 400


def test_export_is_zero_write(app_for_client):
    client, app = app_for_client
    before = _db_counts(app.state.session_factory)
    client.get(f"/period-close/export.xlsx?period={CLOSE_PERIOD_FIXTURE}")
    client.get(f"/period-close/export.csv?period={CLOSE_PERIOD_FIXTURE}")
    after = _db_counts(app.state.session_factory)
    assert before == after, "GET export routes must not write a single row"


def test_web_export_matches_application_data_product(app_for_client):
    """Same synthetic database: the Web XLSX/CSV bytes must be produced
    from the exact same Application-layer Data Product the CLI builds —
    the route itself must not recompute anything."""
    client, app = app_for_client
    with app.state.session_factory() as session:
        workbench = get_period_close_workbench(session, CLOSE_PERIOD_FIXTURE)
    expected_product = build_period_close_data_product(workbench)

    response = client.get(f"/period-close/export.csv?period={CLOSE_PERIOD_FIXTURE}")
    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) == len(expected_product.all_rows)
