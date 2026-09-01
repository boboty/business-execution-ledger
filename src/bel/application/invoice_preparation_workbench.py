"""Invoice Preparation Workbench — one read-only application path
(Phase 2D.3-F2a).

Composes the F0 factual context with the two F1 rule layers:

    get_invoice_preparation_context()
        + evaluate_sales_invoice_preparation(...)
        + evaluate_supplier_invoice_request(...)
        ->
    InvoicePreparationWorkbench

This module is APPLICATION ORCHESTRATION ONLY. It introduces NO new
business rule: the IP-S02 comparison, supplier P02/P03/P04/P05
comparisons, P09 follow-up, cardinality handling and currency-safe
comparison all come from the already-frozen F1 evaluation layers, and
nothing here re-derives "current" semantics or re-implements a decision.
A single read-only path lets the Workbench page (and, later, the F2b
data product) consume exactly the same canonical outputs.

The Workbench is FACT CONTROL + MANAGEMENT REMINDERS, NOT a workflow
approval engine: the composed reports never decide whether an invoice
may be issued or requested, never gate preparation on a comparison being
available, and never persist a Decision, Reminder, Task or Invoice.

Strictly read-only: evaluation is a pure function of the F0 context.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    get_invoice_preparation_context,
)
from bel.application.sales_invoice_preparation import (
    SalesInvoicePreparationReport,
    evaluate_sales_invoice_preparation_from_context,
)
from bel.application.supplier_invoice_request import (
    SupplierInvoiceRequestReport,
    evaluate_supplier_invoice_request_from_context,
)


@dataclass(frozen=True)
class InvoicePreparationWorkbench:
    """The composed read-only Workbench projection: the F0 fact context
    plus the two canonical F1 reports over that SAME context. The reports
    are the rule layers' own outputs — nothing here recomputes them, and
    the facts stay reachable through ``context`` for presentation."""

    context: InvoicePreparationContext
    sales_report: SalesInvoicePreparationReport
    supplier_report: SupplierInvoiceRequestReport


def get_invoice_preparation_workbench_from_context(
    context: InvoicePreparationContext,
) -> InvoicePreparationWorkbench:
    """Pure composition over an F0 context — no session, no I/O, no
    mutation. Both F1 reports are evaluated over the SAME context, so the
    page can never diverge between the two directions or from the facts
    it renders."""
    return InvoicePreparationWorkbench(
        context=context,
        sales_report=evaluate_sales_invoice_preparation_from_context(context),
        supplier_report=evaluate_supplier_invoice_request_from_context(context),
    )


def get_invoice_preparation_workbench(session: Session) -> InvoicePreparationWorkbench:
    """Compose the complete read-only Workbench over the session. The F0
    context is built UNFILTERED (an axis filter must never blind either
    F1 report), exactly as the F1 session entry points do. Strictly
    read-only."""
    context = get_invoice_preparation_context(session)
    return get_invoice_preparation_workbench_from_context(context)
