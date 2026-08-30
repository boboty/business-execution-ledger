"""R5 identity-aware backfill (Phase 2D.1-R5,
docs/PHASE2D1-R0-DECISIONS.md section 4).

    legacy Evidence
          |
          v
    business-identity-aware resolution (per fact type, section 4.4)
          |
          v
    existing canonical Fact services / repositories

This module reuses EXISTING adapters/parsers
(``bel.adapters.excel.contract_ledger``, ``bel.adapters.excel.invoice_ledger``,
``bel.adapters.pdf.cmb_bank_statement``) and EXISTING Fact-maintenance
primitives (``contract_facts``, ``contract_item_facts``, ``shipment_facts``,
``sales_contract_facts``, ``procurement_sales_link``) — it never forks
parsing logic and never re-implements identity resolution those modules
already do correctly. What is new here is purely the ORCHESTRATION:
turning a parsed row into the right create/replay/conflict outcome, and
a Task rather than a guess whenever identity is incomplete or ambiguous.

Backfill imports Facts, never derived status (section 17, HARD): there
is no code path anywhere in this module that promotes a legacy
derived/status column into a canonical Fact. Every outcome is exactly
one of CREATED / REPLAY-OR-CORROBORATING / TASK.

This module NEVER reads ``$BEL_PRIVATE_DATA_ROOT/<period>/expected/`` —
that is acceptance material for ``cutover_reconciliation``, never a Fact
source (section 47, HARD).

``ContractItem``, ``SalesContract`` and ``ProcurementSalesLink`` have no
natural raw-Excel-column source in the current adapters (the contract
ledger promotes only ``contract_no``/``counterparty``/``buyer``/
``gross_amount`` — see ``bel.adapters.excel.contract_ledger`` — and
carries no frozen sales-scope-reference column mapping). Backfilling
these three therefore takes an explicit structured entry list (the same
shape discipline as ``cutover_fact_pack``'s selectors) rather than
guessing a column mapping that was never frozen — guessing one would
itself violate the "does not guess" principle this whole module exists
to uphold.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from bel.adapters.common import compute_sha256
from bel.adapters.excel.contract_ledger import parse_contract_ledger
from bel.adapters.excel.invoice_ledger import parse_invoice_ledger
from bel.adapters.pdf.cmb_bank_statement import ParsedBankTransaction, parse_cmb_bank_statement
from bel.application.contract_facts import (
    ContractFactAmbiguous,
    ContractFactConflict,
    ContractFactError,
    create_contract_fact,
)
from bel.application.contract_item_facts import (
    ContractItemFactConflict,
    ContractItemFactError,
    create_contract_item_fact,
)
from bel.application.procurement_sales_link import (
    ProcurementSalesLinkFactConflict,
    ProcurementSalesLinkFactError,
    add_procurement_sales_link,
)
from bel.application.sales_contract_facts import (
    SalesContractFactConflict,
    SalesContractFactError,
    create_sales_contract_fact,
)
from bel.application.shipment_facts import ShipmentFactConflict, ShipmentFactError, create_shipment_fact
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    PaymentRepository,
    SalesContractRepository,
)

CONTRACT_LEDGER_SOURCE_TYPE = "contract_ledger_xlsx"
INVOICE_LEDGER_SOURCE_TYPE = "invoice_ledger_xlsx"
BANK_STATEMENT_SOURCE_TYPE = "cmb_bank_statement_pdf"
DEFAULT_CONTRACT_TYPE = "出口报关购销合同"
DEFAULT_CURRENCY = "CNY"


def _as_date(value: Any) -> date:
    """Plan/entry input is JSON, so a date arrives as an ISO string as
    often as a real ``date`` object (a caller building entries in
    Python may pass either) — accepted, never guessed at, since both
    forms name the exact same unambiguous value."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise ValueError(f"expected an ISO date string or date object, got {value!r}")


@dataclass
class BackfillTaskRef:
    """One unresolved backfill row — never silently skipped, never
    silently guessed. ``kind`` is one of: IDENTITY_INCOMPLETE,
    IDENTITY_AMBIGUOUS, CONFLICT."""

    kind: str
    detail: dict[str, Any]


@dataclass
class BackfillOutcome:
    created: int = 0
    replay_or_corroborating: int = 0
    tasks: list[BackfillTaskRef] = field(default_factory=list)


def _row_fragments(
    session: Session, document: EvidenceDocument, rows: list[tuple[int, str, dict]], *, now: datetime
) -> dict[int, uuid.UUID]:
    """One EvidenceFragment per row, flushed before anything references
    a fragment id — the same FK-ordering discipline every importer in
    this codebase follows."""
    evidence_repo = EvidenceRepository(session)
    fragment_ids: dict[int, uuid.UUID] = {}
    for row_number, sheet_name, raw_data in rows:
        fragment = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=document.id, fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name=sheet_name, row_number=row_number, locator_json=None, raw_data=raw_data, created_at=now,
        )
        evidence_repo.add_fragment(fragment)
        fragment_ids[row_number] = fragment.id
    session.flush()
    return fragment_ids


def _get_or_create_document(session: Session, file_path: Path, *, source_type: str, now: datetime) -> tuple[EvidenceDocument, bool]:
    sha256 = compute_sha256(file_path)
    evidence_repo = EvidenceRepository(session)
    existing = evidence_repo.find_document_by_sha256(sha256)
    if existing is not None:
        return existing, True
    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=source_type, imported_at=now
    )
    evidence_repo.add_document(document)
    return document, False


# ---------------------------------------------------------------------------
# Contract backfill — identity (contract_no, counterparty)
# ---------------------------------------------------------------------------


def backfill_contracts(session: Session, file_path: Path, *, created_at: datetime | None = None) -> BackfillOutcome:
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    document, is_reimport = _get_or_create_document(session, file_path, source_type=CONTRACT_LEDGER_SOURCE_TYPE, now=now)
    if is_reimport:
        return outcome

    parsed = parse_contract_ledger(file_path)
    business_rows = [r for r in parsed.rows if r.is_business_row]
    fragment_ids = _row_fragments(
        session, document, [(r.row_number, r.sheet_name, r.raw_data) for r in business_rows], now=now
    )

    for row in business_rows:
        fragment_id = fragment_ids[row.row_number]
        if not row.contract_no or not row.counterparty:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_INCOMPLETE",
                    detail={
                        "fact_type": "Contract", "row_number": row.row_number,
                        "missing_contract_no": not bool(row.contract_no),
                        "missing_counterparty": not bool(row.counterparty),
                    },
                )
            )
            continue
        fields = {
            "contract_type": DEFAULT_CONTRACT_TYPE, "buyer": row.buyer, "gross_amount": row.gross_amount,
            "currency": DEFAULT_CURRENCY, "contract_date": None,
        }
        try:
            result = create_contract_fact(
                session, contract_no=row.contract_no, counterparty=row.counterparty, fields=fields,
                source_fragment_id=fragment_id, created_at=now,
            )
        except ContractFactAmbiguous as exc:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_AMBIGUOUS",
                    detail={"fact_type": "Contract", "row_number": row.row_number, "reason": str(exc)},
                )
            )
            continue
        except (ContractFactConflict, ContractFactError) as exc:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="CONFLICT",
                    detail={"fact_type": "Contract", "row_number": row.row_number, "reason": str(exc)},
                )
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome


# ---------------------------------------------------------------------------
# ContractItem backfill — identity (contract_id, source_item_key), via
# explicit structured entries (no natural ledger column source).
# ---------------------------------------------------------------------------


def backfill_contract_items(
    session: Session, entries: list[dict[str, Any]], *, source_fragment_id: uuid.UUID, created_at: datetime | None = None
) -> BackfillOutcome:
    """Each entry: ``{"contract_no", "counterparty", "source_item_key", "fields": {...}}``.
    ``source_fragment_id`` names the (already-created) Evidence this
    whole batch traces to — the caller builds it, exactly like every
    other backfill entry point that reads real Evidence."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    contract_repo = ContractRepository(session)

    for index, entry in enumerate(entries):
        contract_no = entry.get("contract_no")
        counterparty = entry.get("counterparty")
        source_item_key = entry.get("source_item_key")
        if not source_item_key:
            outcome.tasks.append(
                BackfillTaskRef(kind="IDENTITY_INCOMPLETE", detail={"fact_type": "ContractItem", "index": index})
            )
            continue
        matches = contract_repo.find_by_identity(contract_no, counterparty)
        if len(matches) == 0:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_INCOMPLETE",
                    detail={"fact_type": "ContractItem", "index": index, "reason": "no matching Contract"},
                )
            )
            continue
        if len(matches) > 1:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_AMBIGUOUS",
                    detail={"fact_type": "ContractItem", "index": index, "matches": len(matches)},
                )
            )
            continue
        try:
            result = create_contract_item_fact(
                session, contract_id=matches[0].id, source_item_key=source_item_key,
                fields=entry.get("fields", {}), source_fragment_id=source_fragment_id, created_at=now,
            )
        except (ContractItemFactConflict, ContractItemFactError) as exc:
            outcome.tasks.append(
                BackfillTaskRef(kind="CONFLICT", detail={"fact_type": "ContractItem", "index": index, "reason": str(exc)})
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome


# ---------------------------------------------------------------------------
# Invoice backfill — identity external_invoice_key
# ---------------------------------------------------------------------------


def backfill_invoices(
    session: Session, file_path: Path, direction: str, *, created_at: datetime | None = None
) -> BackfillOutcome:
    """``direction`` is a required, explicit caller argument — never
    inferred from party names (section 10, HARD). Identity is
    ``external_invoice_key`` — the SAME ``digital_invoice_no``-as-key
    rule ``import_invoices.py`` already applies (section 10 of the
    Phase 2A spec), reused rather than reinvented."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    document, is_reimport = _get_or_create_document(session, file_path, source_type=INVOICE_LEDGER_SOURCE_TYPE, now=now)
    if is_reimport:
        return outcome

    parsed = parse_invoice_ledger(file_path)
    invoice_repo = InvoiceRepository(session)
    item_repo = InvoiceItemRepository(session)

    row_fragment_ids = _row_fragments(
        session, document, [(r.row_number, parsed.sheet_name, r.raw_data) for r in parsed.rows], now=now
    )

    for group_index, group in enumerate(parsed.groups):
        header = group.header
        external_key = header.digital_invoice_no
        if not external_key:
            outcome.tasks.append(
                BackfillTaskRef(kind="IDENTITY_INCOMPLETE", detail={"fact_type": "Invoice", "index": group_index})
            )
            continue

        existing = invoice_repo.find_by_external_key(external_key)
        incoming_content = (direction, header.invoice_net_amount, header.invoice_tax_amount, header.invoice_gross_amount)
        if existing is not None:
            existing_content = (existing.direction, existing.net_amount, existing.tax_amount, existing.gross_amount)
            if existing_content == incoming_content:
                outcome.replay_or_corroborating += 1
                continue
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="CONFLICT",
                    detail={"fact_type": "Invoice", "external_invoice_key": external_key, "index": group_index},
                )
            )
            continue

        invoice = Invoice(
            id=uuid.uuid4(), direction=direction, invoice_type=header.invoice_type, invoice_no=header.invoice_no,
            digital_invoice_no=header.digital_invoice_no, external_invoice_key=external_key,
            issue_date=header.issue_date, seller=header.seller, buyer=parsed.buyer,
            net_amount=header.invoice_net_amount, tax_amount=header.invoice_tax_amount,
            gross_amount=header.invoice_gross_amount, invoice_status=header.invoice_status,
            source_fragment_id=row_fragment_ids[header.row_number], created_at=now, updated_at=now,
        )
        invoice_repo.add(invoice)
        session.flush()
        for line_no, item_row in enumerate(group.item_rows, start=1):
            item_repo.add(
                InvoiceItem(
                    id=uuid.uuid4(), invoice_id=invoice.id, line_no=line_no, product_name=item_row.product_name,
                    specification=item_row.specification, unit=item_row.unit, quantity=item_row.quantity,
                    unit_price=item_row.unit_price, net_amount=item_row.item_net_amount, tax_rate=item_row.tax_rate,
                    tax_amount=item_row.item_tax_amount, gross_amount=item_row.item_gross_amount,
                    source_fragment_id=row_fragment_ids[item_row.row_number],
                )
            )
        outcome.created += 1
    session.flush()
    return outcome


# ---------------------------------------------------------------------------
# Payment backfill — identity (source_account_id, transaction_date,
# direction, amount, bank_reference)
# ---------------------------------------------------------------------------


def backfill_payment_transactions(
    session: Session,
    transactions: list[ParsedBankTransaction],
    *,
    source_account_id: str | None,
    document_id: uuid.UUID,
    created_at: datetime | None = None,
) -> BackfillOutcome:
    """The identity-resolution core, factored out so it can be tested
    without a real PDF: ``backfill_payments`` below is a thin wrapper
    that parses a file and calls this."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    payment_repo = PaymentRepository(session)
    evidence_repo = EvidenceRepository(session)

    for txn in transactions:
        fragment = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=document_id, fragment_kind=FragmentKind.PDF_TRANSACTION,
            sheet_name=None, row_number=None,
            locator_json={"page": txn.page_index, "transaction_index": txn.transaction_index},
            raw_data=txn.raw_data, created_at=now,
        )
        evidence_repo.add_fragment(fragment)
        session.flush()

        direction = PaymentDirection.IN if txn.signed_amount >= 0 else PaymentDirection.OUT
        amount = abs(txn.signed_amount)

        if not source_account_id or not txn.bank_reference:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_INCOMPLETE",
                    detail={
                        "fact_type": "Payment", "transaction_index": txn.transaction_index,
                        "missing_source_account_id": not bool(source_account_id),
                        "missing_bank_reference": not bool(txn.bank_reference),
                    },
                )
            )
            continue

        matches = payment_repo.find_by_identity(
            source_account_id=source_account_id, transaction_date=txn.transaction_date, direction=direction,
            amount=amount, bank_reference=txn.bank_reference,
        )
        if len(matches) > 1:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_AMBIGUOUS",
                    detail={"fact_type": "Payment", "transaction_index": txn.transaction_index, "matches": len(matches)},
                )
            )
            continue
        if len(matches) == 1:
            existing = matches[0]
            same_content = (
                existing.counterparty == txn.counterparty
                and existing.business_type == txn.business_type
                and existing.description == txn.description
            )
            if same_content:
                outcome.replay_or_corroborating += 1
            else:
                outcome.tasks.append(
                    BackfillTaskRef(
                        kind="CONFLICT",
                        detail={"fact_type": "Payment", "transaction_index": txn.transaction_index},
                    )
                )
            continue

        payment_repo.add(
            Payment(
                id=uuid.uuid4(), transaction_date=txn.transaction_date, direction=direction, amount=amount,
                counterparty=txn.counterparty, business_type=txn.business_type, bank_reference=txn.bank_reference,
                description=txn.description, running_balance=txn.running_balance, source_fragment_id=fragment.id,
                created_at=now, source_account_id=source_account_id,
            )
        )
        outcome.created += 1
    session.flush()
    return outcome


def backfill_payments(
    session: Session, file_path: Path, profile: str, *, source_account_id: str | None, created_at: datetime | None = None
) -> BackfillOutcome:
    """``source_account_id`` is an explicit caller-supplied input seam
    (section 7): the current CMB statement adapter cannot deterministically
    parse an account identifier from the PDF text layer, so it is never
    guessed from the filename, counterparty, or profile name."""
    if profile != "cmb":
        raise ValueError(f"unsupported bank profile {profile!r}")
    now = created_at or datetime.now(timezone.utc)
    document, is_reimport = _get_or_create_document(session, file_path, source_type=BANK_STATEMENT_SOURCE_TYPE, now=now)
    if is_reimport:
        return BackfillOutcome()
    parsed = parse_cmb_bank_statement(file_path)
    return backfill_payment_transactions(
        session, parsed.transactions, source_account_id=source_account_id, document_id=document.id, created_at=now
    )


# ---------------------------------------------------------------------------
# Shipment backfill — reuses R2's already identity-aware create_shipment_fact
# ---------------------------------------------------------------------------


def backfill_shipments(
    session: Session, entries: list[dict[str, Any]], *, source_fragment_id: uuid.UUID, created_at: datetime | None = None
) -> BackfillOutcome:
    """Each entry: ``{"contract_no", "counterparty", "external_reference",
    "execution_date", "quantity", "contract_item_source_key"}``. Delegates
    entirely to ``create_shipment_fact`` (R2), which already implements
    every incomplete-identity / conflict outcome section 13 requires — no
    Shipment is ever fabricated from a legacy derived status."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    contract_repo = ContractRepository(session)
    item_repo = ContractItemRepository(session)

    for index, entry in enumerate(entries):
        matches = contract_repo.find_by_identity(entry.get("contract_no"), entry.get("counterparty"))
        if len(matches) != 1:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_AMBIGUOUS" if len(matches) > 1 else "IDENTITY_INCOMPLETE",
                    detail={"fact_type": "Shipment", "index": index, "matches": len(matches)},
                )
            )
            continue
        contract = matches[0]
        contract_item_id = None
        item_key = entry.get("contract_item_source_key")
        if item_key:
            item = item_repo.find_by_contract_and_key(contract.id, item_key)
            contract_item_id = item.id if item else None

        quantity = entry.get("quantity")
        try:
            result = create_shipment_fact(
                session, contract_id=contract.id, external_reference=entry.get("external_reference"),
                execution_date=_as_date(entry["execution_date"]),
                fields={"contract_item_id": contract_item_id, "quantity": Decimal(str(quantity)) if quantity is not None else None},
                source_fragment_id=source_fragment_id, created_at=now,
                identity_confirmed=bool(entry.get("identity_confirmed", False)),
            )
        except (ShipmentFactConflict, ShipmentFactError) as exc:
            outcome.tasks.append(
                BackfillTaskRef(kind="CONFLICT", detail={"fact_type": "Shipment", "index": index, "reason": str(exc)})
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome


# ---------------------------------------------------------------------------
# SalesContract backfill — reuses R3a's already identity-aware
# create_sales_contract_fact
# ---------------------------------------------------------------------------


def backfill_sales_contracts(
    session: Session, entries: list[dict[str, Any]], *, source_fragment_id: uuid.UUID, created_at: datetime | None = None
) -> BackfillOutcome:
    """Each entry: ``{"our_entity", "sales_contract_no", "fields": {...}}``.
    ``customer`` missing is legitimate — the anchor is still created,
    with an unresolved-customer Task, exactly as R3a already does."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()

    for index, entry in enumerate(entries):
        try:
            result = create_sales_contract_fact(
                session, our_entity=entry.get("our_entity"), sales_contract_no=entry.get("sales_contract_no"),
                fields=entry.get("fields", {}), source_fragment_id=source_fragment_id, created_at=now,
            )
        except (SalesContractFactConflict, SalesContractFactError) as exc:
            outcome.tasks.append(
                BackfillTaskRef(kind="CONFLICT", detail={"fact_type": "SalesContract", "index": index, "reason": str(exc)})
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome


# ---------------------------------------------------------------------------
# ProcurementSalesLink backfill — reuses R3a's already idempotent /
# no-resurrection add_procurement_sales_link
# ---------------------------------------------------------------------------


def backfill_procurement_sales_links(
    session: Session, entries: list[dict[str, Any]], *, source_fragment_id: uuid.UUID, created_at: datetime | None = None
) -> BackfillOutcome:
    """Each entry: ``{"contract_no", "counterparty", "sales_our_entity",
    "sales_contract_no"}``. A backfill rerun is NEVER a REESTABLISH
    (section 16, HARD) — ``add_procurement_sales_link`` only ever ADDs
    or replays; resurrecting a retired pair requires an explicit human
    REESTABLISH action outside this module."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    contract_repo = ContractRepository(session)
    sales_contract_repo = SalesContractRepository(session)

    for index, entry in enumerate(entries):
        contract_matches = contract_repo.find_by_identity(entry.get("contract_no"), entry.get("counterparty"))
        sales_contract = sales_contract_repo.find_by_identity(entry.get("sales_our_entity"), entry.get("sales_contract_no"))
        if len(contract_matches) != 1 or sales_contract is None:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="IDENTITY_AMBIGUOUS" if len(contract_matches) > 1 else "IDENTITY_INCOMPLETE",
                    detail={
                        "fact_type": "ProcurementSalesLink", "index": index,
                        "contract_matches": len(contract_matches), "sales_contract_found": sales_contract is not None,
                    },
                )
            )
            continue
        try:
            result = add_procurement_sales_link(
                session, procurement_contract_id=contract_matches[0].id, sales_contract_id=sales_contract.id,
                source_fragment_id=source_fragment_id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
                created_at=now,
            )
        except (ProcurementSalesLinkFactConflict, ProcurementSalesLinkFactError) as exc:
            outcome.tasks.append(
                BackfillTaskRef(
                    kind="CONFLICT", detail={"fact_type": "ProcurementSalesLink", "index": index, "reason": str(exc)}
                )
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome
