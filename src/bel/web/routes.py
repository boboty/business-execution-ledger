"""Phase 2C web routes.

Every page handler calls an Application Service; none builds DB objects
and none re-implements business rules. GET routes are strictly
read-only. The single write operation is the manual InvoiceItem
allocation, which runs through the same shared serialized write boundary
(``execute_manual_item_allocation``) as the CLI.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bel.application.allocate_invoice_item import execute_manual_item_allocation
from bel.application.contract_360 import get_contract_360
from bel.application.contract_business_ledger import ContractLedgerFilters, get_contract_business_ledger
from bel.application.contract_ledger_export import (
    export_contract_business_ledger_csv,
    export_contract_business_ledger_xlsx,
)
from bel.application.period_close_export import (
    build_period_close_data_product,
    export_period_close_csv,
    export_period_close_xlsx,
)
from bel.application.period_close_workbench import get_period_close_workbench, list_known_periods
from bel.application.search_contracts import search_contracts_by_no
from bel.infrastructure.persistence.database import is_database_busy
from bel.web import viewmodels

router = APIRouter()

PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def _session(request: Request):
    factory = request.app.state.session_factory
    with factory() as session:
        yield session


def _templates(request: Request):
    return request.app.state.templates


def _default_period(session: Session) -> str:
    periods = list_known_periods(session)
    if periods:
        return periods[0]
    return date.today().strftime("%Y-%m")


def _checked_period(period: str | None, session: Session) -> str:
    if period is None:
        return _default_period(session)
    if not PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    year, month = int(period[:4]), int(period[5:7])
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="period must be YYYY-MM")
    return period


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/period-close", status_code=302)


@router.get("/period-close", response_class=HTMLResponse)
def period_close_page(
    request: Request,
    period: str | None = None,
    session: Session = Depends(_session),
) -> HTMLResponse:
    # Strictly read-only: the whole handler runs under no_autoflush so a
    # pending (unflushed) object in the session is never written by the
    # default-period lookup or the workbench read.
    with session.no_autoflush:
        period = _checked_period(period, session)
        workbench = get_period_close_workbench(session, period)
    vm = viewmodels.PeriodCloseVM(workbench)
    return _templates(request).TemplateResponse(
        request, "period_close.html", {"page": "period-close", "vm": vm}
    )


@router.get("/period-close/export.xlsx")
def period_close_export_xlsx(
    request: Request,
    period: str | None = None,
    session: Session = Depends(_session),
) -> Response:
    with session.no_autoflush:
        period = _checked_period(period, session)
        workbench = get_period_close_workbench(session, period)
    product = build_period_close_data_product(workbench)
    content = export_period_close_xlsx(product)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=period-close-{period}.xlsx"},
    )


@router.get("/period-close/export.csv")
def period_close_export_csv(
    request: Request,
    period: str | None = None,
    session: Session = Depends(_session),
) -> Response:
    with session.no_autoflush:
        period = _checked_period(period, session)
        workbench = get_period_close_workbench(session, period)
    product = build_period_close_data_product(workbench)
    content = export_period_close_csv(product)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=period-close-{period}.csv"},
    )


@router.get("/contracts/search", response_class=HTMLResponse)
def contract_search_page(
    request: Request,
    no: str,
    period: str | None = None,
    session: Session = Depends(_session),
) -> HTMLResponse:
    with session.no_autoflush:
        period = _checked_period(period, session)
        matches = search_contracts_by_no(session, no.strip())
    if len(matches) == 1:
        return RedirectResponse(url=f"/contracts/{matches[0].id}?period={period}", status_code=302)
    return _templates(request).TemplateResponse(
        request,
        "contract_search.html",
        {"page": "contract-360", "vm": {"query": no.strip(), "matches": matches, "period": period}},
    )


@router.get("/contracts/{contract_id}", response_class=HTMLResponse)
def contract_360_page(
    request: Request,
    contract_id: uuid.UUID,
    period: str | None = None,
    session: Session = Depends(_session),
) -> HTMLResponse:
    with session.no_autoflush:
        period = _checked_period(period, session)
        dto = get_contract_360(session, contract_id, period)
    if dto is None:
        raise HTTPException(status_code=404, detail="contract not found")
    vm = viewmodels.Contract360VM(dto, period)
    return _templates(request).TemplateResponse(
        request, "contract_360.html", {"page": "contract-360", "vm": vm}
    )


def _bool_filter(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    if value.lower() in ("1", "true", "yes"):
        return True
    if value.lower() in ("0", "false", "no"):
        return False
    return None


def _ledger_filters_from_query(request: Request) -> ContractLedgerFilters:
    """Web only parses query args; Application layer
    (ContractLedgerFilters / get_contract_business_ledger) owns filter
    semantics — no SQL/raw field expression is ever built here."""
    q = request.query_params
    return ContractLedgerFilters(
        contract_no=q.get("contract_no") or None,
        supplier=q.get("supplier") or None,
        our_entity=q.get("our_entity") or None,
        sales_contract_no=q.get("sales_contract_no") or None,
        customer=q.get("customer") or None,
        has_unresolved=_bool_filter(q.get("has_unresolved")),
    )


@router.get("/contract-ledger", response_class=HTMLResponse)
def contract_ledger_page(
    request: Request,
    session: Session = Depends(_session),
) -> HTMLResponse:
    with session.no_autoflush:
        filters = _ledger_filters_from_query(request)
        ledger = get_contract_business_ledger(session, filters)
    vm = viewmodels.ContractBusinessLedgerVM(ledger)
    return _templates(request).TemplateResponse(
        request, "contract_ledger.html", {"page": "contract-ledger", "vm": vm}
    )


@router.get("/contract-ledger/export.csv")
def contract_ledger_export_csv(
    request: Request,
    session: Session = Depends(_session),
) -> Response:
    with session.no_autoflush:
        filters = _ledger_filters_from_query(request)
        ledger = get_contract_business_ledger(session, filters)
    content = export_contract_business_ledger_csv(ledger)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=contract-business-ledger.csv"},
    )


@router.get("/contract-ledger/export.xlsx")
def contract_ledger_export_xlsx(
    request: Request,
    session: Session = Depends(_session),
) -> Response:
    with session.no_autoflush:
        filters = _ledger_filters_from_query(request)
        ledger = get_contract_business_ledger(session, filters)
    content = export_contract_business_ledger_xlsx(ledger)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=contract-business-ledger.xlsx"},
    )


def _same_origin(request: Request) -> bool:
    """Server-side same-origin gate for write operations. Browsers always
    send an Origin header on POST (same- and cross-origin). A present
    Origin must match this server's own origin; a missing Origin is a
    non-browser client and is allowed (cross-site form/JSON attempts are
    already blocked by content-type and the absent CORS configuration)."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host")
    if not host:
        return False
    return origin in (f"http://{host}", f"https://{host}")


@router.post("/api/invoice-item-allocations", status_code=201)
def create_invoice_item_allocation(
    payload: dict, request: Request, session: Session = Depends(_session)
):
    """The only Phase 2C write. Runs through the SAME command-level
    serialized write boundary as the CLI
    (``execute_manual_item_allocation`` -> BEGIN IMMEDIATE -> commit):
    invoice lookup, contract item lookup, confirmed contract scope,
    quantity capacity, Evidence creation, InvoiceItemAllocation insert,
    single commit. On success 201; any business error is a safe 400 with
    zero partial writes; a SQLite busy error is a controlled 503."""
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request rejected")
    try:
        invoice_external_key = payload.get("invoice_external_key")
        if not isinstance(invoice_external_key, str) or not invoice_external_key:
            raise ValueError("invoice_external_key is required")
        line_no = int(payload.get("line_no"))
        contract_id = uuid.UUID(str(payload.get("contract_id")))
        source_item_key = payload.get("source_item_key")
        if not isinstance(source_item_key, str) or not source_item_key:
            raise ValueError("source_item_key is required")
        quantity = Decimal(str(payload.get("quantity")))
        net_amount = Decimal(str(payload.get("net_amount")))
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise HTTPException(status_code=400, detail=f"invalid request payload: {exc}") from exc

    try:
        allocation = execute_manual_item_allocation(
            session,
            invoice_external_key=invoice_external_key,
            line_no=line_no,
            contract_id=contract_id,
            source_item_key=source_item_key,
            quantity=quantity,
            net_amount=net_amount,
        )
    except OperationalError as exc:
        # SQLite busy: a concurrent writer held the write lock past the
        # busy timeout. The shared boundary already rolled the transaction
        # back (no partial rows); answer a controlled 503 — never a 500.
        session.rollback()
        if is_database_busy(exc):
            raise HTTPException(status_code=503, detail="database is busy; retry the request") from exc
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"id": str(allocation.id)}
