from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import click
from sqlalchemy.exc import OperationalError

from bel.application.accrual_queries import get_accrual_view, list_accrual_views
from bel.application.allocate_invoice_item import execute_manual_item_allocation
from bel.application.contract_item_facts import (
    ContractItemFactError,
    execute_correct_contract_item_fact,
    execute_create_contract_item_fact,
    execute_supplement_contract_item_fact,
    get_contract_item,
    get_contract_item_history,
)
from bel.application.get_contract import get_contract
from bel.application.get_invoice import get_invoice
from bel.application.get_payment import get_payment
from bel.application.import_bank import import_bank_statement
from bel.application.import_close_facts import CloseFactPackError, import_close_facts
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.application.list_exceptions import list_exceptions
from bel.application.list_matches import list_match_cases
from bel.application.matching import confirm_match, match_invoices, match_payments
from bel.application.period_close import build_period_close_preview
from bel.application.search_contracts import search_contracts_by_no
from bel.application.procurement_sales_link import (
    ProcurementSalesLinkFactError,
    execute_add_procurement_sales_link,
    execute_correct_procurement_sales_link,
    execute_reestablish_procurement_sales_link,
    get_relationship_history,
    list_current_links_for_procurement_contract,
    list_current_links_for_sales_contract,
)
from bel.application.sales_contract_facts import (
    SalesContractFactError,
    execute_correct_sales_contract_fact,
    execute_create_sales_contract_fact,
    execute_supplement_sales_contract_fact,
    find_sales_contract_by_identity,
    get_sales_contract,
    get_sales_contract_history,
    list_sales_contracts,
)
from bel.application.sales_matching import (
    SalesMatchError,
    confirm_sales_invoice_match,
    confirm_sales_payment_match,
    list_sales_match_cases,
    list_sales_match_candidates,
    propose_sales_invoice_match,
    propose_sales_payment_match,
)
from bel.application.shipment_facts import (
    ShipmentFactError,
    execute_correct_shipment_fact,
    execute_create_shipment_fact,
    execute_supplement_shipment_fact,
    get_shipment,
    get_shipment_history,
    list_shipments_for_contract,
)
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.database import is_database_busy, make_engine, make_session_factory

DEFAULT_DB_PATH = "bel.db"


def _session_factory(db_path: str):
    engine = make_engine(db_path)
    return make_session_factory(engine)


@click.group()
@click.option("--db", "db_path", default=DEFAULT_DB_PATH, show_default=True, help="SQLite database file path.")
@click.pass_context
def cli(ctx: click.Context, db_path: str) -> None:
    """Business Execution Ledger CLI.

    Schema is owned by Alembic migrations, not this CLI — run
    `alembic upgrade head` before first use.
    """
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path


@cli.command("import-contract-ledger")
@click.argument("xlsx_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def import_contract_ledger_cmd(ctx: click.Context, xlsx_path: Path) -> None:
    """Import a contract ledger workbook (合同台账 Excel)."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        result = import_contract_ledger(session, xlsx_path)

    if result.is_reimport:
        click.echo("Import completed (re-import — same file already on record, 0 new facts)")
        click.echo("")
        click.echo(f"  evidence document: {result.evidence_document_id}")
        click.echo(f"  sha256: {result.sha256}")
        return

    click.echo("Import completed")
    click.echo("")
    click.echo("Document:")
    click.echo(f"  sheets: {len(result.sheets)}")
    click.echo(f"  primary sheet: {result.primary_sheet}")
    click.echo(f"  columns: {result.primary_sheet_columns}")
    click.echo("")
    click.echo("Records:")
    click.echo(f"  business rows: {result.business_rows}")
    click.echo(f"  blank trailing rows: {result.blank_trailing_rows}")
    click.echo(f"  contracts created: {result.contracts_created}")
    click.echo(f"  contract items created: {result.contract_items_created}")
    click.echo("")
    click.echo("Integrity:")
    click.echo(f"  gross amount: {result.gross_amount_total:,.2f}")
    click.echo(f"  distinct sellers: {result.distinct_sellers}")
    click.echo(f"  distinct buyers: {result.distinct_buyers}")
    click.echo(f"  distinct owners: {result.distinct_owners}")
    click.echo(f"  distinct customs receivers: {result.distinct_customs_receivers}")
    click.echo(f"  missing 外销合同编码: {result.missing_export_contract_no}")
    click.echo(f"  business key conflicts: {len(result.business_key_conflicts)}")
    for c in result.business_key_conflicts:
        click.echo(f"    - {c.contract_no}: {[str(i) for i in c.contract_ids]}")
    click.echo("")
    click.echo(f"  evidence document: {result.evidence_document_id}")
    click.echo(f"  sha256: {result.sha256}")


@cli.group("contract")
def contract_group() -> None:
    """Contract queries."""


@contract_group.command("search")
@click.option("--no", "contract_no", required=True, help="contract_no to search for.")
@click.pass_context
def contract_search(ctx: click.Context, contract_no: str) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        contracts = search_contracts_by_no(session, contract_no)

    if not contracts:
        click.echo(f"No contracts found for contract_no={contract_no!r}")
        return

    click.echo(f"{len(contracts)} contract(s) found for contract_no={contract_no!r}:")
    for c in contracts:
        click.echo(f"  {c.id}  {c.counterparty} -> {c.buyer}  {c.gross_amount} {c.currency}")


@contract_group.command("get")
@click.argument("contract_id", type=click.UUID)
@click.pass_context
def contract_get(ctx: click.Context, contract_id: uuid.UUID) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        trace = get_contract(session, contract_id)

    if trace is None:
        click.echo(f"No contract with id={contract_id}")
        return

    c = trace.contract
    click.echo("Contract Fact")
    click.echo(f"  id:            {c.id}")
    click.echo(f"  contract_no:   {c.contract_no}")
    click.echo(f"  contract_type: {c.contract_type}")
    click.echo(f"  counterparty:  {c.counterparty}")
    click.echo(f"  buyer:         {c.buyer}")
    click.echo(f"  gross_amount:  {c.gross_amount} {c.currency}")
    click.echo(f"  contract_date: {c.contract_date}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Source Fragment")
    click.echo(f"  id:    {trace.fragment.id}")
    click.echo(f"  sheet: {trace.fragment.sheet_name}")
    click.echo(f"  row:   {trace.fragment.row_number}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Workbook / Sheet / Row")
    click.echo(f"  file:    {trace.document.file_name}")
    click.echo(f"  sha256:  {trace.document.sha256}")
    click.echo(f"  sheet:   {trace.fragment.sheet_name}")
    click.echo(f"  row:     {trace.fragment.row_number}")
    click.echo("  raw_data:")
    for k, v in trace.fragment.raw_data.items():
        click.echo(f"    {k}: {v!r}")


@cli.group("exception")
def exception_group() -> None:
    """Exception / Task queries."""


@exception_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include resolved exceptions too (default: open only).")
@click.pass_context
def exception_list(ctx: click.Context, show_all: bool) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        exceptions = list_exceptions(session, open_only=not show_all)

    if not exceptions:
        click.echo("No exceptions.")
        return

    for e in exceptions:
        click.echo(f"{e.id}  [{e.status}] {e.exception_type}: {e.summary}")


@cli.command("import-invoices")
@click.argument("xlsx_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--direction",
    type=click.Choice(["purchase", "sales", "unknown"], case_sensitive=False),
    required=True,
    help="Explicit — never guessed from the file or its name.",
)
@click.pass_context
def import_invoices_cmd(ctx: click.Context, xlsx_path: Path, direction: str) -> None:
    """Import a purchase/sales invoice ledger workbook."""
    direction_value = {
        "purchase": InvoiceDirection.PURCHASE,
        "sales": InvoiceDirection.SALES,
        "unknown": InvoiceDirection.UNKNOWN,
    }[direction.lower()]

    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        result = import_invoices(session, xlsx_path, direction_value)

    if result.is_reimport:
        click.echo("Import completed (re-import — same file already on record, 0 new facts)")
        click.echo("")
        click.echo(f"  evidence document: {result.evidence_document_id}")
        click.echo(f"  sha256: {result.sha256}")
        return

    click.echo("Import completed")
    click.echo("")
    click.echo(f"  direction: {result.direction}")
    click.echo(f"  buyer: {result.buyer}")
    click.echo("")
    click.echo("Records:")
    click.echo(f"  invoices created: {result.invoices_created}")
    click.echo(f"  invoice items created: {result.invoice_items_created}")
    click.echo("")
    click.echo("Totals:")
    click.echo(f"  net_amount:   {result.net_amount_total:,.2f}")
    click.echo(f"  tax_amount:   {result.tax_amount_total:,.2f}")
    click.echo(f"  gross_amount: {result.gross_amount_total:,.2f}")
    click.echo("")
    click.echo(f"  evidence document: {result.evidence_document_id}")
    click.echo(f"  sha256: {result.sha256}")


@cli.command("import-bank")
@click.argument("pdf_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--profile", type=click.Choice(["cmb"], case_sensitive=False), required=True, help="Bank statement layout profile.")
@click.pass_context
def import_bank_cmd(ctx: click.Context, pdf_path: Path, profile: str) -> None:
    """Import a bank statement PDF (deterministic text-layer parsing, no OCR)."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        result = import_bank_statement(session, pdf_path, profile.lower())

    if result.is_reimport:
        click.echo("Import completed (re-import — same file already on record, 0 new facts)")
        click.echo("")
        click.echo(f"  evidence document: {result.evidence_document_id}")
        click.echo(f"  sha256: {result.sha256}")
        return

    click.echo("Import completed")
    click.echo("")
    click.echo("Records:")
    click.echo(f"  payments created: {result.payments_created}")
    click.echo("")
    click.echo("Reconciliation:")
    click.echo(f"  opening balance: {result.opening_balance:,.2f}" if result.opening_balance is not None else "  opening balance: (not found)")
    click.echo(f"  total IN:        {result.total_in:,.2f}")
    click.echo(f"  total OUT:       {result.total_out:,.2f}")
    click.echo(f"  closing balance: {result.closing_balance:,.2f}" if result.closing_balance is not None else "  closing balance: (not found)")
    if result.opening_balance is not None and result.closing_balance is not None:
        computed = result.opening_balance + result.total_in - result.total_out
        ok = "OK" if computed == result.closing_balance else "MISMATCH"
        click.echo(f"  computed closing: {computed:,.2f}  [{ok}]")
    click.echo("")
    click.echo(f"  evidence document: {result.evidence_document_id}")
    click.echo(f"  sha256: {result.sha256}")


@cli.group("invoice")
def invoice_group() -> None:
    """Invoice queries."""


@invoice_group.command("get")
@click.argument("invoice_id", type=click.UUID)
@click.pass_context
def invoice_get(ctx: click.Context, invoice_id: uuid.UUID) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        trace = get_invoice(session, invoice_id)

    if trace is None:
        click.echo(f"No invoice with id={invoice_id}")
        return

    inv = trace.invoice
    click.echo("Invoice Fact")
    click.echo(f"  id:                  {inv.id}")
    click.echo(f"  direction:           {inv.direction}")
    click.echo(f"  digital_invoice_no:  {inv.digital_invoice_no}")
    click.echo(f"  invoice_no:          {inv.invoice_no}")
    click.echo(f"  issue_date:          {inv.issue_date}")
    click.echo(f"  seller:              {inv.seller}")
    click.echo(f"  buyer:               {inv.buyer}")
    click.echo(f"  net/tax/gross:       {inv.net_amount} / {inv.tax_amount} / {inv.gross_amount}")
    click.echo(f"  invoice_status:      {inv.invoice_status}")
    click.echo(f"  items ({len(trace.items)}):")
    for item in trace.items:
        click.echo(f"    line {item.line_no}: {item.product_name!r} qty={item.quantity} gross={item.gross_amount}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Source Fragment")
    click.echo(f"  id:    {trace.fragment.id}")
    click.echo(f"  sheet: {trace.fragment.sheet_name}  row: {trace.fragment.row_number}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Workbook")
    click.echo(f"  file:   {trace.document.file_name}")
    click.echo(f"  sha256: {trace.document.sha256}")


@cli.group("payment")
def payment_group() -> None:
    """Payment queries."""


@payment_group.command("get")
@click.argument("payment_id", type=click.UUID)
@click.pass_context
def payment_get(ctx: click.Context, payment_id: uuid.UUID) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        trace = get_payment(session, payment_id)

    if trace is None:
        click.echo(f"No payment with id={payment_id}")
        return

    p = trace.payment
    click.echo("Payment Fact")
    click.echo(f"  id:               {p.id}")
    click.echo(f"  transaction_date: {p.transaction_date}")
    click.echo(f"  direction:        {p.direction}")
    click.echo(f"  amount:           {p.amount}")
    click.echo(f"  counterparty:     {p.counterparty}")
    click.echo(f"  business_type:    {p.business_type}")
    click.echo(f"  bank_reference:   {p.bank_reference}")
    click.echo(f"  running_balance:  {p.running_balance}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Source Fragment (PDF transaction)")
    click.echo(f"  id:       {trace.fragment.id}")
    click.echo(f"  locator:  {trace.fragment.locator_json}")
    click.echo("    |")
    click.echo("    v")
    click.echo("Bank Statement")
    click.echo(f"  file:   {trace.document.file_name}")
    click.echo(f"  sha256: {trace.document.sha256}")
    click.echo("  raw_data (bank's original signed representation):")
    for k, v in trace.fragment.raw_data.items():
        click.echo(f"    {k}: {v!r}")


@cli.group("match")
def match_group() -> None:
    """M001 matching."""


@match_group.command("invoices")
@click.pass_context
def match_invoices_cmd(ctx: click.Context) -> None:
    """Run M001 against all unmatched PURCHASE invoices."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        summary = match_invoices(session)
    _echo_match_summary(summary)


@match_group.command("payments")
@click.pass_context
def match_payments_cmd(ctx: click.Context) -> None:
    """Run M001 against all unmatched OUT payments."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        summary = match_payments(session)
    _echo_match_summary(summary)


@match_group.command("run")
@click.pass_context
def match_run_cmd(ctx: click.Context) -> None:
    """Run M001 against both invoices and payments."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        inv_summary = match_invoices(session)
        pay_summary = match_payments(session)
    click.echo("Invoices:")
    _echo_match_summary(inv_summary, indent="  ")
    click.echo("Payments:")
    _echo_match_summary(pay_summary, indent="  ")


def _echo_match_summary(summary, indent: str = "") -> None:
    click.echo(f"{indent}eligible_total (counterparty in contract set): {summary.eligible_total}")
    click.echo(f"{indent}out_of_scope (counterparty not contract-related): {summary.out_of_scope}")
    click.echo(f"{indent}auto_confirmed:              {summary.auto_confirmed}")
    click.echo(f"{indent}human_confirmation_required: {summary.human_confirmation_required}")
    click.echo(f"{indent}unmatched:                   {summary.unmatched}")
    click.echo(f"{indent}capacity_exceeded:           {summary.capacity_exceeded}")
    click.echo(f"{indent}already_matched_skipped:     {summary.already_matched_skipped}")


@match_group.command("list")
@click.option("--status", default=None, help="Filter by MatchCase status (e.g. HUMAN_CONFIRMATION_REQUIRED).")
@click.pass_context
def match_list(ctx: click.Context, status: str | None) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        cases = list_match_cases(session, status=status.upper() if status else None)

    if not cases:
        click.echo("No match cases.")
        return

    for c in cases:
        click.echo(f"{c.id}  [{c.status}] {c.subject_type} {c.subject_id}  method={c.match_method}")


@match_group.command("confirm")
@click.argument("match_case_id", type=click.UUID)
@click.option("--contract", "contract_id", type=click.UUID, required=True, help="Contract to confirm the match against.")
@click.pass_context
def match_confirm(ctx: click.Context, match_case_id: uuid.UUID, contract_id: uuid.UUID) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        try:
            confirm_match(session, match_case_id, contract_id)
            session.commit()
        except ValueError as exc:
            click.echo(f"Error: {exc}")
            raise SystemExit(1) from exc

    click.echo(f"MatchCase {match_case_id} confirmed against contract {contract_id} -> RESOLVED")


@cli.command("import-close-facts")
@click.argument("json_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def import_close_facts_cmd(ctx: click.Context, json_path: Path) -> None:
    """Import a Close Fact Pack (人工补充事实 for period close)."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = import_close_facts(session, json_path)
    except CloseFactPackError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    if result.is_reimport:
        click.echo("Import completed (re-import — same file already on record, 0 new facts)")
        click.echo("")
        click.echo(f"  evidence document: {result.evidence_document_id}")
        click.echo(f"  sha256: {result.sha256}")
        return

    click.echo("Import completed")
    click.echo("")
    click.echo(f"  contract items:            {result.contract_items_created} created / {result.contract_items_skipped} skipped")
    click.echo(f"  cost recognition facts:    {result.cost_recognition_facts_created} created / {result.cost_recognition_facts_skipped} skipped")
    click.echo(f"  accrual basis facts:       {result.accrual_basis_facts_created} created / {result.accrual_basis_facts_skipped} skipped")
    click.echo(f"  historical accrual facts:  {result.historical_accrual_facts_created} created / {result.historical_accrual_facts_skipped} skipped")
    click.echo(f"  accruals:                  {result.accruals_created} created / {result.accruals_skipped} skipped")
    click.echo(f"  invoice item allocations:  {result.invoice_item_allocations_created} created / {result.invoice_item_allocations_skipped} skipped")
    click.echo(f"  accrual reversals:         {result.accrual_reversals_created} created / {result.accrual_reversals_skipped} skipped")
    click.echo(f"  source periods:            {', '.join(result.source_periods) or '(none)'}")
    click.echo("")
    click.echo(f"  evidence document: {result.evidence_document_id}")
    click.echo(f"  sha256: {result.sha256}")


@cli.group("accrual")
def accrual_group() -> None:
    """Accrual queries."""


@accrual_group.command("list")
@click.pass_context
def accrual_list(ctx: click.Context) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        views = list_accrual_views(session)

    if not views:
        click.echo("No accruals.")
        return

    for v in views:
        a = v.accrual
        click.echo(
            f"{a.id}  [{v.projected_status}] item={a.contract_item_id} period={a.period} "
            f"qty={a.quantity} cost={a.estimated_cost} remaining_qty={v.remaining_quantity} "
            f"remaining_cost={v.remaining_estimated_cost}"
        )


@accrual_group.command("get")
@click.argument("accrual_id", type=click.UUID)
@click.pass_context
def accrual_get(ctx: click.Context, accrual_id: uuid.UUID) -> None:
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        view = get_accrual_view(session, accrual_id)

    if view is None:
        click.echo(f"No accrual with id={accrual_id}")
        return

    a = view.accrual
    click.echo("Accrual")
    click.echo(f"  id:                     {a.id}")
    click.echo(f"  period:                 {a.period}")
    click.echo(f"  contract_item_id:       {a.contract_item_id}")
    click.echo(f"  quantity:               {a.quantity}")
    click.echo(f"  estimated_cost:         {a.estimated_cost}")
    click.echo(f"  basis:                  {a.basis}")
    click.echo(f"  status:                 {a.status}")
    click.echo(f"  created_from_fact_id:   {a.created_from_fact_id}")
    click.echo(f"  created_at:             {a.created_at}")
    click.echo("")
    click.echo("Balance (derived: original - reversals)")
    click.echo(f"  reversed_quantity:       {view.reversed_quantity}")
    click.echo(f"  reversed_estimated_cost: {view.reversed_estimated_cost}")
    click.echo(f"  remaining_quantity:      {view.remaining_quantity}")
    click.echo(f"  remaining_estimated_cost: {view.remaining_estimated_cost}")
    click.echo(f"  projected_status:        {view.projected_status}")
    click.echo("")
    click.echo(f"Reversals ({len(view.reversals)}):")
    for r in view.reversals:
        click.echo(
            f"  {r.id}  period={r.period} allocation={r.invoice_item_allocation_id} "
            f"qty={r.reversed_quantity} cost={r.reversed_estimated_cost}"
        )


@cli.command("invoice-item-allocate")
@click.option("--invoice", "invoice_external_key", required=True, help="Invoice external_invoice_key (数电发票号码).")
@click.option("--line", "line_no", type=int, required=True, help="Invoice line number (1-based).")
@click.option("--contract", "contract_id", type=click.UUID, required=True, help="Contract id.")
@click.option("--item", "source_item_key", required=True, help="ContractItem source_item_key.")
@click.option("--quantity", required=True, help="Quantity to allocate.")
@click.option("--net-amount", required=True, help="Allocated net amount.")
@click.pass_context
def invoice_item_allocate(
    ctx: click.Context,
    invoice_external_key: str,
    line_no: int,
    contract_id: uuid.UUID,
    source_item_key: str,
    quantity: str,
    net_amount: str,
) -> None:
    """Manually confirm a ContractItem <-> InvoiceItem allocation (11-A/B/C enforced).

    Runs through the SAME serialized write boundary as the Web API
    (``execute_manual_item_allocation``), so the CLI and Web share one
    command-level BEGIN IMMEDIATE transaction model."""
    from sqlalchemy.exc import OperationalError

    from bel.infrastructure.persistence.database import is_database_busy

    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            allocation = execute_manual_item_allocation(
                session,
                invoice_external_key=invoice_external_key,
                line_no=line_no,
                contract_id=contract_id,
                source_item_key=source_item_key,
                quantity=Decimal(quantity),
                net_amount=Decimal(net_amount),
            )
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise
    except ValueError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    click.echo(
        f"InvoiceItemAllocation {allocation.id} created: invoice_item={allocation.invoice_item_id} "
        f"contract_item={allocation.contract_item_id} qty={allocation.allocated_quantity} "
        f"net={allocation.allocated_net_amount} [{allocation.confirmation_type}]"
    )


_CONTRACT_ITEM_FIELD_OPTIONS = (
    click.option("--sku", "sku", default=None, help="Product SKU."),
    click.option("--product-name", "product_name", default=None, help="Product name."),
    click.option("--specification", "specification", default=None, help="Specification."),
    click.option("--quantity", "quantity", default=None, help="Quantity."),
    click.option("--unit", "unit", default=None, help="Unit."),
    click.option("--unit-price", "unit_price", default=None, help="Unit price."),
    click.option("--gross-amount", "gross_amount", default=None, help="Gross amount."),
    click.option("--tax-rate", "tax_rate", default=None, help="Tax rate."),
    click.option("--net-amount", "net_amount", default=None, help="Net amount."),
)


def _contract_item_field_options(f):
    for option in reversed(_CONTRACT_ITEM_FIELD_OPTIONS):
        f = option(f)
    return f


_DECIMAL_FIELDS = ("quantity", "unit_price", "gross_amount", "tax_rate", "net_amount")


def _fields_from_options(
    sku, product_name, specification, quantity, unit, unit_price, gross_amount, tax_rate, net_amount
) -> dict:
    """Only options the caller actually passed become dict entries — an
    omitted --flag is "not asserted this call", not "assert NULL"."""
    raw = {
        "sku": sku,
        "product_name": product_name,
        "specification": specification,
        "quantity": quantity,
        "unit": unit,
        "unit_price": unit_price,
        "gross_amount": gross_amount,
        "tax_rate": tax_rate,
        "net_amount": net_amount,
    }
    fields = {k: v for k, v in raw.items() if v is not None}
    for key in _DECIMAL_FIELDS:
        if key in fields:
            fields[key] = Decimal(fields[key])
    return fields


@cli.group("contract-item")
def contract_item_group() -> None:
    """ContractItem Fact maintenance (Phase 2D.1-R1): create, supplement,
    correct and inspect the everyday intake path for ContractItem — a
    human confirmation IS Evidence (docs/DOMAIN.md), traceable exactly
    like an imported one."""


@contract_item_group.command("create")
@click.option("--contract", "contract_id", type=click.UUID, required=True, help="Contract id.")
@click.option("--item", "source_item_key", required=True, help="ContractItem source_item_key.")
@_contract_item_field_options
@click.pass_context
def contract_item_create(ctx: click.Context, contract_id, source_item_key, **field_options) -> None:
    """Assert a new ContractItem (case A — it did not exist before). A
    duplicate (contract, item) asserting the SAME values is an exact
    replay or corroborating Evidence (no new anchor); asserting
    DIFFERENT values is an explicit conflict — this never guesses
    whether the caller meant supplement or correction; use those
    commands explicitly."""
    fields = _fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_create_contract_item_fact(
                session, contract_id=contract_id, source_item_key=source_item_key, fields=fields
            )
    except ContractItemFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    if result.created:
        outcome = "created"
    elif result.replay:
        outcome = "already exists (exact replay — same Evidence, same content)"
    elif result.corroborating:
        outcome = "already exists (corroborating Evidence — different fragment, same content)"
    else:
        outcome = "already exists"
    click.echo(
        f"ContractItem {result.item.id} {outcome}: "
        f"contract={result.item.contract_id} item={result.item.source_item_key} "
        f"product={result.item.product_name!r} quantity={result.item.quantity}"
    )


@contract_item_group.command("supplement")
@click.option("--item-id", "contract_item_id", type=click.UUID, required=True, help="ContractItem id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_contract_item_field_options
@click.pass_context
def contract_item_supplement(ctx: click.Context, contract_item_id, based_on_revision_id, **field_options) -> None:
    """Fill in a previously-unknown field (case B-supplement). Rejected
    if any given field already holds a DIFFERENT known value — that is a
    correction, not a supplement."""
    fields = _fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_supplement_contract_item_fact(
                session,
                contract_item_id=contract_item_id,
                based_on_revision_id=based_on_revision_id,
                fields=fields,
            )
    except ContractItemFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"ContractItem {result.item.id} supplemented"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"product={result.item.product_name!r} quantity={result.item.quantity}"
    )


@contract_item_group.command("correct")
@click.option("--item-id", "contract_item_id", type=click.UUID, required=True, help="ContractItem id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_contract_item_field_options
@click.pass_context
def contract_item_correct(ctx: click.Context, contract_item_id, based_on_revision_id, **field_options) -> None:
    """Correct a previously-asserted value that was wrong (case
    B-correction). Rejected if any given field currently has no value —
    that is a supplement, not a correction. If persisted derived records
    (allocations/accruals) still reference this item, a
    ContractItemFactSuperseded Task is raised naming them."""
    fields = _fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_correct_contract_item_fact(
                session,
                contract_item_id=contract_item_id,
                based_on_revision_id=based_on_revision_id,
                fields=fields,
            )
    except ContractItemFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"ContractItem {result.item.id} corrected"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"product={result.item.product_name!r} quantity={result.item.quantity}"
    )


@contract_item_group.command("show")
@click.argument("item_id", type=click.UUID)
@click.pass_context
def contract_item_show(ctx: click.Context, item_id) -> None:
    """Show the current authoritative ContractItem state."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        item = get_contract_item(session, item_id)
    if item is None:
        click.echo(f"Error: ContractItem {item_id} not found")
        raise SystemExit(1)

    click.echo(f"ContractItem {item.id}")
    click.echo(f"  contract_id:                {item.contract_id}")
    click.echo(f"  source_item_key:            {item.source_item_key}")
    click.echo(f"  sku:                        {item.sku}")
    click.echo(f"  product_name:               {item.product_name}")
    click.echo(f"  specification:              {item.specification}")
    click.echo(f"  quantity:                   {item.quantity}")
    click.echo(f"  unit:                       {item.unit}")
    click.echo(f"  unit_price:                 {item.unit_price}")
    click.echo(f"  gross_amount:               {item.gross_amount}")
    click.echo(f"  tax_rate:                   {item.tax_rate}")
    click.echo(f"  net_amount:                 {item.net_amount}")
    click.echo(f"  current_source_fragment_id: {item.current_source_fragment_id}")
    click.echo(f"  created_at:                 {item.created_at}")


@contract_item_group.command("history")
@click.argument("item_id", type=click.UUID)
@click.pass_context
def contract_item_history(ctx: click.Context, item_id) -> None:
    """List every revision ever asserted for this ContractItem, oldest
    first — the full audit trail, including superseded revisions."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        revisions = get_contract_item_history(session, item_id)
    if not revisions:
        click.echo(f"ContractItem {item_id} has no revisions (not found, or anchor with no history)")
        return

    click.echo(f"ContractItem {item_id} — {len(revisions)} revision(s):")
    for r in revisions:
        current_marker = " [CURRENT]" if r.superseded_by_revision_id is None else f" [superseded by {r.superseded_by_revision_id}]"
        click.echo(
            f"  {r.id} [{r.revision_type}]{current_marker} product={r.product_name!r} quantity={r.quantity} "
            f"source_fragment={r.source_fragment_id} created_at={r.created_at}"
        )


_SHIPMENT_FIELD_OPTIONS = (
    click.option("--item", "contract_item_id", type=click.UUID, default=None, help="ContractItem id (item scope, where known)."),
    click.option("--quantity", "quantity", default=None, help="Quantity."),
)


def _shipment_field_options(f):
    for option in reversed(_SHIPMENT_FIELD_OPTIONS):
        f = option(f)
    return f


def _shipment_fields_from_options(contract_item_id, quantity) -> dict:
    """Only options the caller actually passed become dict entries — an
    omitted --flag is "not asserted this call", not "assert NULL"."""
    raw = {"contract_item_id": contract_item_id, "quantity": quantity}
    fields = {k: v for k, v in raw.items() if v is not None}
    if "quantity" in fields:
        fields["quantity"] = Decimal(fields["quantity"])
    return fields


@cli.group("shipment")
def shipment_group() -> None:
    """Shipment Fact maintenance (Phase 2D.1-R2): create, supplement,
    correct and inspect the everyday intake path for Shipment — a human
    confirmation IS Evidence (docs/DOMAIN.md), traceable exactly like an
    imported one. Records export EXECUTION only — never invoice
    eligibility, and never a ProcurementSalesLink."""


@shipment_group.command("create")
@click.option("--contract", "contract_id", type=click.UUID, required=True, help="Contract id (procurement leg).")
@click.option("--execution-date", "execution_date", required=True, help="Execution date, YYYY-MM-DD.")
@click.option(
    "--external-ref",
    "external_reference",
    default=None,
    help="Declaration/booking reference as recorded. Omitting it leaves the business identity incomplete "
    "(docs/PHASE2D1-R0-DECISIONS.md section 4.4): no anchor is created and no dedup is attempted — a Task "
    "is raised instead, unless --confirm-incomplete-identity is also given.",
)
@click.option(
    "--confirm-incomplete-identity",
    "identity_confirmed",
    is_flag=True,
    default=False,
    help="Explicit human confirmation to create a Shipment anchor despite having no --external-ref. Without "
    "this flag, an omitted --external-ref only raises a SHIPMENT_IDENTITY_INCOMPLETE Task — no anchor is created.",
)
@_shipment_field_options
@click.pass_context
def shipment_create(
    ctx: click.Context, contract_id, execution_date, external_reference, identity_confirmed, **field_options
) -> None:
    """Assert a new Shipment (case A — it did not exist before). A
    duplicate (contract, external-ref, execution-date) asserting the SAME
    values is an exact replay or corroborating Evidence (no new anchor);
    asserting DIFFERENT values is an explicit conflict (a
    ShipmentIdentityConflict Task is raised; the existing Shipment is
    unchanged) — this never guesses whether the caller meant supplement
    or correction."""
    fields = _shipment_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_create_shipment_fact(
                session,
                contract_id=contract_id,
                external_reference=external_reference,
                execution_date=date.fromisoformat(execution_date),
                fields=fields,
                identity_confirmed=identity_confirmed,
            )
    except ShipmentFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    if result.created:
        outcome = "created"
    elif result.replay:
        outcome = "already exists (exact replay — same Evidence, same content)"
    elif result.corroborating:
        outcome = "already exists (corroborating Evidence — different fragment, same content)"
    else:
        outcome = "already exists"
    click.echo(
        f"Shipment {result.shipment.id} {outcome}: contract={result.shipment.contract_id} "
        f"external_reference={result.shipment.external_reference!r} execution_date={result.shipment.execution_date} "
        f"item={result.shipment.contract_item_id} quantity={result.shipment.quantity}"
    )


@shipment_group.command("supplement")
@click.option("--shipment-id", "shipment_id", type=click.UUID, required=True, help="Shipment id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_shipment_field_options
@click.pass_context
def shipment_supplement(ctx: click.Context, shipment_id, based_on_revision_id, **field_options) -> None:
    """Fill in a previously-unknown field (case B-supplement). Rejected
    if any given field already holds a DIFFERENT known value — that is a
    correction, not a supplement."""
    fields = _shipment_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_supplement_shipment_fact(
                session, shipment_id=shipment_id, based_on_revision_id=based_on_revision_id, fields=fields
            )
    except ShipmentFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"Shipment {result.shipment.id} supplemented"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"item={result.shipment.contract_item_id} quantity={result.shipment.quantity}"
    )


@shipment_group.command("correct")
@click.option("--shipment-id", "shipment_id", type=click.UUID, required=True, help="Shipment id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_shipment_field_options
@click.pass_context
def shipment_correct(ctx: click.Context, shipment_id, based_on_revision_id, **field_options) -> None:
    """Correct a previously-asserted value that was wrong (case
    B-correction). Rejected if any given field currently has no value —
    that is a supplement, not a correction. If a persisted
    CostRecognitionFact still references this Shipment, a
    ShipmentFactSuperseded Task is raised naming it."""
    fields = _shipment_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_correct_shipment_fact(
                session, shipment_id=shipment_id, based_on_revision_id=based_on_revision_id, fields=fields
            )
    except ShipmentFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"Shipment {result.shipment.id} corrected"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"item={result.shipment.contract_item_id} quantity={result.shipment.quantity}"
    )


@shipment_group.command("show")
@click.argument("shipment_id", type=click.UUID)
@click.pass_context
def shipment_show(ctx: click.Context, shipment_id) -> None:
    """Show the current authoritative Shipment state."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        shipment = get_shipment(session, shipment_id)
    if shipment is None:
        click.echo(f"Error: Shipment {shipment_id} not found")
        raise SystemExit(1)

    click.echo(f"Shipment {shipment.id}")
    click.echo(f"  contract_id:                {shipment.contract_id}")
    click.echo(f"  external_reference:         {shipment.external_reference}")
    click.echo(f"  execution_date:             {shipment.execution_date}")
    click.echo(f"  contract_item_id:           {shipment.contract_item_id}")
    click.echo(f"  quantity:                   {shipment.quantity}")
    click.echo(f"  current_source_fragment_id: {shipment.current_source_fragment_id}")
    click.echo(f"  created_at:                 {shipment.created_at}")


@shipment_group.command("history")
@click.argument("shipment_id", type=click.UUID)
@click.pass_context
def shipment_history(ctx: click.Context, shipment_id) -> None:
    """List every revision ever asserted for this Shipment, oldest first
    — the full audit trail, including superseded revisions."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        revisions = get_shipment_history(session, shipment_id)
    if not revisions:
        click.echo(f"Shipment {shipment_id} has no revisions (not found, or anchor with no history)")
        return

    click.echo(f"Shipment {shipment_id} — {len(revisions)} revision(s):")
    for r in revisions:
        current_marker = " [CURRENT]" if r.superseded_by_revision_id is None else f" [superseded by {r.superseded_by_revision_id}]"
        click.echo(
            f"  {r.id} [{r.revision_type}]{current_marker} item={r.contract_item_id} quantity={r.quantity} "
            f"source_fragment={r.source_fragment_id} created_at={r.created_at}"
        )


@shipment_group.command("list")
@click.option("--contract", "contract_id", type=click.UUID, required=True, help="Contract id.")
@click.pass_context
def shipment_list(ctx: click.Context, contract_id) -> None:
    """List every Shipment for a Contract, oldest first — one Contract
    may have many Shipments (docs/PHASE2D1-R0-DECISIONS.md section 3.3)."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        shipments = list_shipments_for_contract(session, contract_id)
    if not shipments:
        click.echo(f"No Shipments for contract {contract_id}")
        return

    click.echo(f"Shipments for contract {contract_id} — {len(shipments)}:")
    for s in shipments:
        click.echo(
            f"  {s.id} external_reference={s.external_reference!r} execution_date={s.execution_date} "
            f"item={s.contract_item_id} quantity={s.quantity}"
        )


_SALES_CONTRACT_FIELD_OPTIONS = (
    click.option(
        "--customer",
        "customer",
        default=None,
        help="External sales customer, from sales-side Evidence only. Never Contract.buyer, never a "
        "sales-scope reference number, never a customs/shipping party.",
    ),
    click.option("--currency", "currency", default=None, help="Currency."),
    click.option("--gross-amount", "gross_amount", default=None, help="Gross amount."),
    click.option("--contract-date", "contract_date", default=None, help="Contract date, YYYY-MM-DD."),
)


def _sales_contract_field_options(f):
    for option in reversed(_SALES_CONTRACT_FIELD_OPTIONS):
        f = option(f)
    return f


def _sales_contract_fields_from_options(customer, currency, gross_amount, contract_date) -> dict:
    """Only options the caller actually passed become dict entries — an
    omitted --flag is "not asserted this call", not "assert NULL"."""
    raw = {"customer": customer, "currency": currency, "gross_amount": gross_amount, "contract_date": contract_date}
    fields = {k: v for k, v in raw.items() if v is not None}
    if "gross_amount" in fields:
        fields["gross_amount"] = Decimal(fields["gross_amount"])
    if "contract_date" in fields:
        fields["contract_date"] = date.fromisoformat(fields["contract_date"])
    return fields


@cli.group("sales-contract")
def sales_contract_group() -> None:
    """SalesContract Fact maintenance (Phase 2D.1-R3a Slice 1): create,
    supplement, correct and inspect the sales-side twin of Contract — the
    only place an external sales customer is expressed. A human
    confirmation IS Evidence (docs/DOMAIN.md), traceable exactly like an
    imported one. Never creates a ProcurementSalesLink (Slice 2)."""


@sales_contract_group.command("create")
@click.option("--our-entity", "our_entity", default=None, help="Our own contracting entity on the sales leg.")
@click.option("--sales-contract-no", "sales_contract_no", default=None, help="Sales contract number.")
@_sales_contract_field_options
@click.pass_context
def sales_contract_create(ctx: click.Context, our_entity, sales_contract_no, **field_options) -> None:
    """Assert a new SalesContract (case A — it did not exist before).
    Omitting --our-entity or --sales-contract-no raises a
    SalesContractIdentityIncomplete Task — NO anchor is created (there is
    no confirmation override, unlike Shipment). Omitting --customer is
    fine: the anchor IS created, with an unresolved-customer Task.
    A duplicate (our-entity, sales-contract-no) asserting the SAME values
    is an exact replay or corroborating Evidence (no new anchor);
    asserting DIFFERENT values is an explicit conflict (a
    BusinessKeyConflict Task is raised; the existing SalesContract is
    unchanged)."""
    fields = _sales_contract_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_create_sales_contract_fact(
                session, our_entity=our_entity, sales_contract_no=sales_contract_no, fields=fields
            )
    except SalesContractFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    if result.created:
        outcome = "created"
    elif result.replay:
        outcome = "already exists (exact replay — same Evidence, same content)"
    elif result.corroborating:
        outcome = "already exists (corroborating Evidence — different fragment, same content)"
    else:
        outcome = "already exists"
    click.echo(
        f"SalesContract {result.sales_contract.id} {outcome}: our_entity={result.sales_contract.our_entity!r} "
        f"sales_contract_no={result.sales_contract.sales_contract_no!r} customer={result.sales_contract.customer!r} "
        f"currency={result.sales_contract.currency} gross_amount={result.sales_contract.gross_amount}"
    )


@sales_contract_group.command("supplement")
@click.option("--sales-contract-id", "sales_contract_id", type=click.UUID, required=True, help="SalesContract id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_sales_contract_field_options
@click.pass_context
def sales_contract_supplement(ctx: click.Context, sales_contract_id, based_on_revision_id, **field_options) -> None:
    """Fill in a previously-unknown field (case B-supplement), most
    commonly --customer once sales-side Evidence identifies it — this
    resolves the anchor's unresolved-customer Task. Rejected if any given
    field already holds a DIFFERENT known value — that is a correction,
    not a supplement."""
    fields = _sales_contract_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_supplement_sales_contract_fact(
                session, sales_contract_id=sales_contract_id, based_on_revision_id=based_on_revision_id, fields=fields
            )
    except SalesContractFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"SalesContract {result.sales_contract.id} supplemented"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"customer={result.sales_contract.customer!r}"
    )


@sales_contract_group.command("correct")
@click.option("--sales-contract-id", "sales_contract_id", type=click.UUID, required=True, help="SalesContract id (the anchor).")
@click.option(
    "--based-on", "based_on_revision_id", type=click.UUID, required=True, help="Revision id believed to be current."
)
@_sales_contract_field_options
@click.pass_context
def sales_contract_correct(ctx: click.Context, sales_contract_id, based_on_revision_id, **field_options) -> None:
    """Correct a previously-asserted value that was wrong (case
    B-correction). Rejected if any given field currently has no value —
    that is a supplement, not a correction."""
    fields = _sales_contract_fields_from_options(**field_options)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_correct_sales_contract_fact(
                session, sales_contract_id=sales_contract_id, based_on_revision_id=based_on_revision_id, fields=fields
            )
    except SalesContractFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(
        f"SalesContract {result.sales_contract.id} corrected"
        f"{' (idempotent replay — no new revision)' if not result.revision_written else ''}: "
        f"customer={result.sales_contract.customer!r}"
    )


@sales_contract_group.command("show")
@click.argument("sales_contract_id", type=click.UUID)
@click.pass_context
def sales_contract_show(ctx: click.Context, sales_contract_id) -> None:
    """Show the current authoritative SalesContract state."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        sales_contract = get_sales_contract(session, sales_contract_id)
    if sales_contract is None:
        click.echo(f"Error: SalesContract {sales_contract_id} not found")
        raise SystemExit(1)

    click.echo(f"SalesContract {sales_contract.id}")
    click.echo(f"  our_entity:                 {sales_contract.our_entity}")
    click.echo(f"  sales_contract_no:          {sales_contract.sales_contract_no}")
    click.echo(f"  customer:                   {sales_contract.customer}")
    click.echo(f"  currency:                   {sales_contract.currency}")
    click.echo(f"  gross_amount:               {sales_contract.gross_amount}")
    click.echo(f"  contract_date:              {sales_contract.contract_date}")
    click.echo(f"  current_source_fragment_id: {sales_contract.current_source_fragment_id}")
    click.echo(f"  created_at:                 {sales_contract.created_at}")


@sales_contract_group.command("history")
@click.argument("sales_contract_id", type=click.UUID)
@click.pass_context
def sales_contract_history(ctx: click.Context, sales_contract_id) -> None:
    """List every revision ever asserted for this SalesContract, oldest
    first — the full audit trail, including superseded revisions."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        revisions = get_sales_contract_history(session, sales_contract_id)
    if not revisions:
        click.echo(f"SalesContract {sales_contract_id} has no revisions (not found, or anchor with no history)")
        return

    click.echo(f"SalesContract {sales_contract_id} — {len(revisions)} revision(s):")
    for r in revisions:
        current_marker = " [CURRENT]" if r.superseded_by_revision_id is None else f" [superseded by {r.superseded_by_revision_id}]"
        click.echo(
            f"  {r.id} [{r.revision_type}]{current_marker} customer={r.customer!r} currency={r.currency} "
            f"source_fragment={r.source_fragment_id} created_at={r.created_at}"
        )


@sales_contract_group.command("list")
@click.pass_context
def sales_contract_list(ctx: click.Context) -> None:
    """List every SalesContract, oldest first."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        sales_contracts = list_sales_contracts(session)
    if not sales_contracts:
        click.echo("No SalesContracts")
        return

    click.echo(f"SalesContracts — {len(sales_contracts)}:")
    for sc in sales_contracts:
        click.echo(
            f"  {sc.id} our_entity={sc.our_entity!r} sales_contract_no={sc.sales_contract_no!r} "
            f"customer={sc.customer!r}"
        )


@cli.group("sales-link")
def sales_link_group() -> None:
    """ProcurementSalesLink Fact maintenance (Phase 2D.1-R3a Slice 2):
    add / correct / invalidate / reestablish the confirmed
    procurement-Contract <-> SalesContract relationship, and inspect its
    history. A human confirmation IS Evidence — every command here
    creates its own MANUAL_FACT fragment. Never apportions any amount or
    quantity across the bridge."""


@sales_link_group.command("add")
@click.option("--procurement-contract", "procurement_contract_id", type=click.UUID, required=True)
@click.option("--sales-contract", "sales_contract_id", type=click.UUID, required=True)
@click.pass_context
def sales_link_add(ctx: click.Context, procurement_contract_id, sales_contract_id) -> None:
    """ADD: assert a confirmed relationship for a business key that has
    never existed before (no current, no retired episode). If the
    business key has retired history, this is rejected — use `reestablish`
    instead. A duplicate ADD for an already-current pair is an idempotent
    replay or corroborating Evidence, never a second episode."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_add_procurement_sales_link(
                session, procurement_contract_id=procurement_contract_id, sales_contract_id=sales_contract_id
            )
    except ProcurementSalesLinkFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    if result.created:
        outcome = "created"
    elif result.replay:
        outcome = "already exists (exact replay — same Evidence)"
    elif result.corroborating:
        outcome = "already exists (corroborating Evidence — different fragment, same relationship)"
    else:
        outcome = "already exists"
    click.echo(
        f"ProcurementSalesLink {result.link.id} {outcome}: procurement_contract={result.link.procurement_contract_id} "
        f"sales_contract={result.link.sales_contract_id} confirmation_type={result.link.confirmation_type}"
    )


@sales_link_group.command("correct")
@click.option("--superseded-link", "superseded_link_id", type=click.UUID, required=True)
@click.option("--replacement-procurement-contract", "replacement_procurement_contract_id", type=click.UUID, default=None)
@click.option("--replacement-sales-contract", "replacement_sales_contract_id", type=click.UUID, default=None)
@click.pass_context
def sales_link_correct(
    ctx: click.Context, superseded_link_id, replacement_procurement_contract_id, replacement_sales_contract_id
) -> None:
    """CORRECT: retire a CURRENT episode with a replacement relationship
    (pass both --replacement-* options) — use `invalidate` instead for a
    pure invalidation with no replacement. Always HUMAN_CONFIRMED."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_correct_procurement_sales_link(
                session,
                superseded_link_id=superseded_link_id,
                replacement_procurement_contract_id=replacement_procurement_contract_id,
                replacement_sales_contract_id=replacement_sales_contract_id,
            )
    except ProcurementSalesLinkFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    replay_note = " (idempotent replay — no new correction)" if result.replay else ""
    replacement_note = f" replacement={result.replacement_link.id}" if result.replacement_link else " (pure invalidation)"
    click.echo(f"ProcurementSalesLink {superseded_link_id} corrected{replay_note}:{replacement_note}")


@sales_link_group.command("invalidate")
@click.option("--superseded-link", "superseded_link_id", type=click.UUID, required=True)
@click.pass_context
def sales_link_invalidate(ctx: click.Context, superseded_link_id) -> None:
    """Pure INVALIDATE: retire a CURRENT episode with no replacement —
    the relationship simply does not exist. Shorthand for `correct` with
    no --replacement-* options."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_correct_procurement_sales_link(session, superseded_link_id=superseded_link_id)
    except ProcurementSalesLinkFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    replay_note = " (idempotent replay — no new correction)" if result.replay else ""
    click.echo(f"ProcurementSalesLink {superseded_link_id} invalidated{replay_note}")


@sales_link_group.command("reestablish")
@click.option("--procurement-contract", "procurement_contract_id", type=click.UUID, required=True)
@click.option("--sales-contract", "sales_contract_id", type=click.UUID, required=True)
@click.pass_context
def sales_link_reestablish(ctx: click.Context, procurement_contract_id, sales_contract_id) -> None:
    """REESTABLISH: the business key has a retired episode and no
    current one. Always HUMAN_CONFIRMED, always requires genuinely new
    Evidence — never resurrects the old episode, always writes a new
    one."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = execute_reestablish_procurement_sales_link(
                session, procurement_contract_id=procurement_contract_id, sales_contract_id=sales_contract_id
            )
    except ProcurementSalesLinkFactError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc
    except OperationalError as exc:
        if is_database_busy(exc):
            click.echo("Error: database is busy; retry when the other write completes")
            raise SystemExit(1) from exc
        raise

    click.echo(f"ProcurementSalesLink {result.link.id} reestablished: confirmation_type={result.link.confirmation_type}")


@sales_link_group.command("history")
@click.option("--procurement-contract", "procurement_contract_id", type=click.UUID, required=True)
@click.option("--sales-contract", "sales_contract_id", type=click.UUID, required=True)
@click.pass_context
def sales_link_history(ctx: click.Context, procurement_contract_id, sales_contract_id) -> None:
    """List every assertion episode ever recorded for this business key,
    oldest first, each annotated CURRENT or retired-by-<correction id>."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        history = get_relationship_history(session, procurement_contract_id, sales_contract_id)
    if not history:
        click.echo(f"No episodes for ({procurement_contract_id}, {sales_contract_id})")
        return

    click.echo(f"Relationship ({procurement_contract_id}, {sales_contract_id}) — {len(history)} episode(s):")
    for entry in history:
        marker = " [CURRENT]" if entry.current else f" [retired by correction {entry.correction.id}]"
        click.echo(
            f"  {entry.episode.id}{marker} confirmation_type={entry.episode.confirmation_type} "
            f"source_fragment={entry.episode.source_fragment_id} created_at={entry.episode.created_at}"
        )


@sales_link_group.command("list")
@click.option("--procurement-contract", "procurement_contract_id", type=click.UUID, default=None)
@click.option("--sales-contract", "sales_contract_id", type=click.UUID, default=None)
@click.pass_context
def sales_link_list(ctx: click.Context, procurement_contract_id, sales_contract_id) -> None:
    """List current links for one procurement Contract OR one
    SalesContract (pass exactly one). Enumerates only — never sums or
    aggregates any amount/quantity across the bridge."""
    if (procurement_contract_id is None) == (sales_contract_id is None):
        click.echo("Error: pass exactly one of --procurement-contract or --sales-contract")
        raise SystemExit(1)

    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        if procurement_contract_id is not None:
            links = list_current_links_for_procurement_contract(session, procurement_contract_id)
        else:
            links = list_current_links_for_sales_contract(session, sales_contract_id)
    if not links:
        click.echo("No current ProcurementSalesLinks")
        return

    click.echo(f"Current ProcurementSalesLinks — {len(links)}:")
    for link in links:
        click.echo(
            f"  {link.id} procurement_contract={link.procurement_contract_id} sales_contract={link.sales_contract_id} "
            f"confirmation_type={link.confirmation_type}"
        )


def _parse_allocation_pairs(raw_pairs: tuple[str, ...]) -> list[tuple[uuid.UUID, Decimal]]:
    """Parses `--allocate <sales_contract_id>:<amount>` options. Amounts
    are always caller-supplied and Decimal — never computed/split here."""
    pairs = []
    for raw in raw_pairs:
        try:
            sc_id_str, amount_str = raw.split(":", 1)
            pairs.append((uuid.UUID(sc_id_str), Decimal(amount_str)))
        except (ValueError, ArithmeticError) as exc:
            click.echo(f"Error: --allocate value {raw!r} must be <sales_contract_id>:<amount>")
            raise SystemExit(1) from exc
    return pairs


@cli.group("sales-match")
def sales_match_group() -> None:
    """Sales-side manual matching (Phase 2D.1-R3b): explicit human
    proposal + confirmation of a SALES invoice or IN payment against one
    or more SalesContracts. No automatic sales matching algorithm exists
    — every candidate and every allocation amount is supplied explicitly."""


@sales_match_group.group("invoice")
def sales_match_invoice_group() -> None:
    """Sales-side matching for SALES invoices."""


@sales_match_invoice_group.command("propose")
@click.option("--invoice", "invoice_id", type=click.UUID, required=True)
@click.option("--sales-contract", "sales_contract_ids", type=click.UUID, required=True, multiple=True, help="Repeatable.")
@click.pass_context
def sales_match_invoice_propose(ctx: click.Context, invoice_id, sales_contract_ids) -> None:
    """Propose one or more candidate SalesContracts for a SALES invoice
    — creates a HUMAN_CONFIRMATION_REQUIRED MatchCase. Candidates are
    never computed automatically; pass every one explicitly."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = propose_sales_invoice_match(
                session, invoice_id=invoice_id, sales_contract_ids=list(sales_contract_ids),
                created_at=datetime.now(timezone.utc),
            )
            session.commit()
    except SalesMatchError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    outcome = "created" if result.created else ("already exists (replay)" if result.replay else "already exists")
    click.echo(f"MatchCase {result.match_case.id} {outcome}: status={result.match_case.status}")


@sales_match_invoice_group.command("confirm")
@click.option("--match-case", "match_case_id", type=click.UUID, required=True)
@click.option(
    "--allocate", "raw_allocations", multiple=True, required=True,
    help="Repeatable <sales_contract_id>:<amount>, e.g. --allocate 11111111-.../60.00",
)
@click.pass_context
def sales_match_invoice_confirm(ctx: click.Context, match_case_id, raw_allocations) -> None:
    """Confirm a proposed MatchCase with the COMPLETE allocation set for
    this invoice in one submission — never a single-target call."""
    pairs = _parse_allocation_pairs(raw_allocations)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = confirm_sales_invoice_match(
                session, match_case_id=match_case_id, allocations=pairs, created_at=datetime.now(timezone.utc),
            )
            session.commit()
    except SalesMatchError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    replay_note = " (idempotent replay)" if result.replay else ""
    click.echo(
        f"MatchCase {result.match_case.id} confirmed{replay_note} -> {result.match_case.status}: "
        f"{len(result.allocations)} allocation(s)"
    )


@sales_match_group.group("payment")
def sales_match_payment_group() -> None:
    """Sales-side matching for IN payments."""


@sales_match_payment_group.command("propose")
@click.option("--payment", "payment_id", type=click.UUID, required=True)
@click.option("--sales-contract", "sales_contract_ids", type=click.UUID, required=True, multiple=True, help="Repeatable.")
@click.pass_context
def sales_match_payment_propose(ctx: click.Context, payment_id, sales_contract_ids) -> None:
    """Propose one or more candidate SalesContracts for an IN payment."""
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = propose_sales_payment_match(
                session, payment_id=payment_id, sales_contract_ids=list(sales_contract_ids), created_at=datetime.now(timezone.utc),
            )
            session.commit()
    except SalesMatchError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    outcome = "created" if result.created else ("already exists (replay)" if result.replay else "already exists")
    click.echo(f"MatchCase {result.match_case.id} {outcome}: status={result.match_case.status}")


@sales_match_payment_group.command("confirm")
@click.option("--match-case", "match_case_id", type=click.UUID, required=True)
@click.option("--allocate", "raw_allocations", multiple=True, required=True, help="Repeatable <sales_contract_id>:<amount>.")
@click.pass_context
def sales_match_payment_confirm(ctx: click.Context, match_case_id, raw_allocations) -> None:
    """Confirm a proposed MatchCase with the COMPLETE allocation set for
    this payment in one submission."""
    pairs = _parse_allocation_pairs(raw_allocations)
    session_factory = _session_factory(ctx.obj["db_path"])
    try:
        with session_factory() as session:
            result = confirm_sales_payment_match(
                session, match_case_id=match_case_id, allocations=pairs, created_at=datetime.now(timezone.utc),
            )
            session.commit()
    except SalesMatchError as exc:
        click.echo(f"Error: {exc}")
        raise SystemExit(1) from exc

    replay_note = " (idempotent replay)" if result.replay else ""
    click.echo(
        f"MatchCase {result.match_case.id} confirmed{replay_note} -> {result.match_case.status}: "
        f"{len(result.allocations)} allocation(s)"
    )


@sales_match_group.command("list")
@click.option("--status", default=None, help="Filter by MatchCase status.")
@click.pass_context
def sales_match_list(ctx: click.Context, status: str | None) -> None:
    """List sales-leg MatchCases only — a procurement case never appears
    here (docs/PHASE2D1-R0-DECISIONS.md section 2.7's leg separation)."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        cases = list_sales_match_cases(session, status=status.upper() if status else None)

    if not cases:
        click.echo("No sales match cases.")
        return
    for c in cases:
        click.echo(f"{c.id}  [{c.status}] {c.subject_type} {c.subject_id}  method={c.match_method}")


@sales_match_group.command("show")
@click.argument("match_case_id", type=click.UUID)
@click.pass_context
def sales_match_show(ctx: click.Context, match_case_id) -> None:
    """Show a sales MatchCase's candidates."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        candidates = list_sales_match_candidates(session, match_case_id)
    if not candidates:
        click.echo(f"No candidates for MatchCase {match_case_id} (or it does not exist).")
        return
    click.echo(f"MatchCase {match_case_id} candidates:")
    for c in candidates:
        click.echo(f"  {c.sales_contract_id}")


@cli.group("period-close")
def period_close_group() -> None:
    """Period close (preview only — read-only, never commits)."""


@period_close_group.command("preview")
@click.argument("period", type=str)
@click.pass_context
def period_close_preview(ctx: click.Context, period: str) -> None:
    """Compute a stateless Period Close Preview for <YYYY-MM>. Pure query:
    writes nothing, creates no vouchers/accounting entries/events."""
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        preview = build_period_close_preview(session, period)

    click.echo(f"Period Close Preview: {preview.period}  (period_end={preview.period_end})")
    click.echo("")
    click.echo("Prior Accrual Reversals (R001+R006):")
    for r in preview.prior_accrual_reversals:
        click.echo(
            f"  accrual={r.accrual_id} source_period={r.source_period} item={r.contract_item_id} "
            f"reversal_qty={r.reversal_quantity} reversal_cost={r.reversal_estimated_cost} "
            f"projected_remaining_qty={r.projected_remaining_quantity} "
            f"projected_remaining_cost={r.projected_remaining_cost} status={r.projected_status}"
        )
    click.echo("")
    click.echo("New Accrual Requirements (R002):")
    for a in preview.new_accrual_requirements:
        click.echo(
            f"  [{a.level}] contract={a.contract_id} item={a.contract_item_id} "
            f"qty={a.quantity} estimated_cost={a.estimated_cost} basis={a.basis}"
        )
    click.echo("")
    click.echo("Contract-Level Candidates (R007):")
    for c in preview.contract_level_candidates:
        click.echo(
            f"  [{c.level}] contract={c.contract_id} estimated_cost={c.estimated_cost} "
            f"blocking_reason={c.blocking_reason}"
        )
    click.echo("")
    click.echo("Accrual Actual Differences (R005):")
    for d in preview.accrual_actual_differences:
        click.echo(
            f"  item={d.contract_item_id} actual_net_cost={d.actual_net_cost} "
            f"reversed_estimated_cost={d.reversed_estimated_cost} difference={d.difference}"
        )
    click.echo("")
    click.echo("Blockers (diagnostics only, not Decisions):")
    if not preview.blockers:
        click.echo("  (none)")
    for b in preview.blockers:
        click.echo(f"  {b.blocker_type} contract={b.contract_id} item={b.contract_item_id or '-'}")
    click.echo("")
    click.echo("Summary:")
    for key, value in preview.summary.items():
        click.echo(f"  {key}: {value}")


@cli.command("web")
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind.")
@click.option("--port", default=8000, show_default=True, type=int, help="TCP port to bind.")
@click.pass_context
def web_cmd(ctx: click.Context, host: str, port: int) -> None:
    """Serve the Phase 2C workbench (月结工作台 + 合同360°).

    Reads the SAME database as the rest of the CLI. The server is
    read-only except for the manual InvoiceItem allocation API, and it
    never opens a browser.
    """
    db_path = ctx.obj["db_path"]
    if db_path == ":memory:":
        raise click.ClickException(
            "the Web runtime requires a file SQLite database; ':memory:' is "
            "test-only and unsupported (it has no concurrent Web guarantee)"
        )
    if host not in ("127.0.0.1", "::1", "localhost"):
        click.echo(
            "WARNING: BEL may display private business data.\n"
            "Remote exposure requires your own network/access controls.",
            err=True,
        )

    import uvicorn

    from bel.web.app import create_app

    app = create_app(db_path)
    click.echo("Business Execution Ledger")
    click.echo(f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _resolve_cutover_period_dir(period: str) -> Path:
    """Same rejection semantics as
    tests/private_acceptance/runner.py's resolve_private_root/
    resolve_period_dir (root unset, root inside the repository, period
    not found) — implemented independently here rather than importing
    from that test module, but deliberately not re-inventing the rules
    themselves (section 36)."""
    import os

    raw_root = os.environ.get("BEL_PRIVATE_DATA_ROOT")
    if not raw_root:
        raise click.ClickException("BEL_PRIVATE_DATA_ROOT is not set")
    try:
        root = Path(raw_root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(f"BEL_PRIVATE_DATA_ROOT does not resolve: {exc}") from exc
    repo_root = Path(__file__).resolve().parents[2]
    try:
        root.relative_to(repo_root)
        raise click.ClickException("BEL_PRIVATE_DATA_ROOT must not be inside the repository")
    except ValueError:
        pass
    if not root.is_dir():
        raise click.ClickException("BEL_PRIVATE_DATA_ROOT is not a directory")
    period_dir = (root / period).resolve(strict=False)
    try:
        period_dir.relative_to(root)
    except ValueError:
        raise click.ClickException(f"period {period!r} does not resolve inside BEL_PRIVATE_DATA_ROOT")
    if not period_dir.is_dir():
        raise click.ClickException(f"period directory not found: {period_dir}")
    return period_dir


@cli.group("cutover")
def cutover_group() -> None:
    """Phase 2D.1-R5 cutover infrastructure/rehearsal — backfill and
    reconciliation only. Never a final-cutover switch: passing
    reconciliation here does not declare BEL the System of Record."""


@cutover_group.command("backfill")
@click.option("--period", required=True, help="Period directory name under BEL_PRIVATE_DATA_ROOT, e.g. 2026-07.")
@click.pass_context
def cutover_backfill_cmd(ctx: click.Context, period: str) -> None:
    """Run the identity-aware backfill plan (backfill-plan.json) for one
    period against the local BEL database. Never prints private business
    values — only per-section created/replay/task counts."""
    from bel.application.cutover_plan import run_backfill_plan

    period_dir = _resolve_cutover_period_dir(period)
    plan_path = period_dir / "backfill-plan.json"
    if not plan_path.exists():
        raise click.ClickException(f"backfill-plan.json not found under {period_dir}")

    import json

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        result = run_backfill_plan(session, plan, period_dir=period_dir, created_at=datetime.now(timezone.utc))

    click.echo(f"cutover backfill — period={period}")
    for section, outcome in result.sections.items():
        if isinstance(outcome, list):
            for entry in outcome:
                click.echo(f"  {section}: created={entry.get('created')} tasks={len(entry.get('tasks', []))}")
        elif isinstance(outcome, dict) and "created" in outcome:
            click.echo(f"  {section}: created={outcome['created']} tasks={len(outcome.get('tasks', []))}")
        else:
            click.echo(f"  {section}: done")


@cutover_group.command("reconcile")
@click.option("--period", required=True, help="Period directory name under BEL_PRIVATE_DATA_ROOT, e.g. 2026-07.")
@click.pass_context
def cutover_reconcile_cmd(ctx: click.Context, period: str) -> None:
    """Reconcile the local BEL database's current contract-execution
    state against the private Cutover Baseline for one period. Prints
    only the scenario verdict and the UNRESOLVED count — full diagnostics
    (business identities, amounts) are never printed to stdout."""
    from bel.application.cutover_reconciliation import reconcile

    period_dir = _resolve_cutover_period_dir(period)
    baseline_path = period_dir / "expected" / "cutover-baseline.json"
    if not baseline_path.exists():
        raise click.ClickException(f"expected/cutover-baseline.json not found under {period_dir}")

    import json

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    session_factory = _session_factory(ctx.obj["db_path"])
    with session_factory() as session:
        result = reconcile(session, baseline)

    click.echo(f"P2D_CUTOVER_RECONCILIATION: {'PASS' if result.passed else 'FAIL'}")
    click.echo(f"unresolved_count={result.unresolved_count}")


if __name__ == "__main__":
    cli()
