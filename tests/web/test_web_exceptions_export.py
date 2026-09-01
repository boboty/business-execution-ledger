"""Phase 2D.4-F2 — Exception & Task Data Product Web endpoints.

Both routes call the SAME Application Data Product path the CLI uses
(get_unresolved_work_center -> build_exception_task_data_product ->
serializer) — this file only checks the Web transport: content types,
deterministic filenames, byte identity with the direct serializer, filter
reflection, read-only, and invalid-period 400. Never a second business
computation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from bel.application.exception_task_data_product import (
    build_exception_task_data_product,
    export_exception_task_csv,
    export_exception_task_xlsx,
)
from bel.application.unresolved_work_center import (
    UnresolvedWorkFilters,
    get_unresolved_work_center,
)
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import InvoiceDirection
from bel.domain.matching import (
    MatchCase,
    MatchCaseStatus,
    MatchCandidate,
    MatchMethod,
    SubjectType,
)
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    ExceptionRepository,
    InvoiceRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
)

NOW = datetime.now(timezone.utc)


def _db_counts(session_factory) -> dict:
    from bel.infrastructure.persistence import models as m

    with session_factory() as session:
        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = session.query(obj).count()
        return counts


def _add_task(session, exception_type, detail, summary="test task"):
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(),
            exception_type=exception_type,
            status=ExceptionStatus.OPEN,
            summary=summary,
            detail=detail,
            created_at=NOW,
        )
    )
    session.flush()


def _seed(web_app):
    app = web_app()
    with app.state.session_factory() as session:
        contract = next(c for c in ContractRepository(session).list_all())
        invoice = next(i for i in InvoiceRepository(session).list_all() if i.direction == InvoiceDirection.PURCHASE)
        _add_task(
            session,
            ExceptionType.BUSINESS_KEY_CONFLICT,
            {"contract_ids": [str(contract.id)]},
            summary="WEB-EXPORT-TASK",
        )
        case = MatchCase(
            id=uuid.uuid4(),
            subject_type=SubjectType.INVOICE,
            subject_id=invoice.id,
            status=MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED,
            match_method=MatchMethod.M001,
            created_at=NOW,
            resolved_at=None,
        )
        MatchCaseRepository(session).add(case)
        session.flush()
        MatchCandidateRepository(session).add(
            MatchCandidate(id=uuid.uuid4(), match_case_id=case.id, contract_id=contract.id, created_at=NOW)
        )
        session.flush()
        session.commit()
    return app


def test_csv_endpoint_content_type_filename_and_direct_serializer_identity(web_app):
    app = _seed(web_app)
    client = TestClient(app)
    with app.state.session_factory() as session:
        center = get_unresolved_work_center(session)
    product = build_exception_task_data_product(center)
    expected = export_exception_task_csv(product)

    response = client.get("/exceptions/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert "filename=exceptions.csv" in response.headers["content-disposition"]
    assert response.content == expected


def test_xlsx_endpoint_content_type_and_period_filename(web_app):
    app = _seed(web_app)
    client = TestClient(app)
    response = client.get("/exceptions/export.xlsx?period=2031-03")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename=exceptions-2031-03.xlsx" in response.headers["content-disposition"]
    with app.state.session_factory() as session:
        center = get_unresolved_work_center(session, filters=UnresolvedWorkFilters(period="2031-03"))
    assert response.content == export_exception_task_xlsx(build_exception_task_data_product(center))


def test_web_export_reflects_filters(web_app):
    app = _seed(web_app)
    client = TestClient(app)
    response = client.get("/exceptions/export.csv?source_type=MATCH_CASE")
    text = response.content.decode("utf-8-sig")
    assert "WEB-EXPORT-TASK" not in text  # task excluded by source_type filter
    assert "INVOICE" in text  # the match summary remains


def test_export_get_causes_zero_writes(web_app):
    app = _seed(web_app)
    client = TestClient(app)
    before = _db_counts(app.state.session_factory)
    assert client.get("/exceptions/export.csv?period=2031-03").status_code == 200
    assert client.get("/exceptions/export.xlsx?period=2031-03").status_code == 200
    assert _db_counts(app.state.session_factory) == before


def test_invalid_period_is_400_on_export(web_app):
    app = _seed(web_app)
    client = TestClient(app)
    assert client.get("/exceptions/export.csv?period=2031-13").status_code == 400
    assert client.get("/exceptions/export.xlsx?period=nope").status_code == 400
