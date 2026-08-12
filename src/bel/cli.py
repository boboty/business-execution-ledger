from __future__ import annotations

import uuid
from pathlib import Path

import click

from bel.application.get_contract import get_contract
from bel.application.get_invoice import get_invoice
from bel.application.get_payment import get_payment
from bel.application.import_bank import import_bank_statement
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.application.list_exceptions import list_exceptions
from bel.application.list_matches import list_match_cases
from bel.application.matching import confirm_match, match_invoices, match_payments
from bel.application.search_contracts import search_contracts_by_no
from bel.domain.invoice import InvoiceDirection
from bel.infrastructure.persistence.database import make_engine, make_session_factory

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


if __name__ == "__main__":
    cli()
