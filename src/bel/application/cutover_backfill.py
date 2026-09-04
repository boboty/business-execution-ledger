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

``ContractItem`` has no natural raw-Excel-column source in the current
adapters (the contract ledger promotes only ``contract_no``/
``counterparty``/``buyer``/``gross_amount`` for CANONICAL fields — see
``bel.adapters.excel.contract_ledger``); its Human-Confirmed structured
entries go through ``bel.application.cutover_fact_pack``'s closed
allowlist exclusively (see that module) — this file never builds its
own ad-hoc MANUAL_FACT for it.

``SalesContract``/``ProcurementSalesLink`` DO have a frozen, genuine
Evidence basis (docs/PHASE2D1-R0-DECISIONS.md section 2.4's "the
procurement ledger's sales-scope reference column as a backfill basis"):
the SAME contract-ledger row's ``外销合同编码`` (export sales-contract
code — preserved verbatim in ``raw_data``, never promoted to a canonical
Contract field) paired with that SAME row's ``买方`` (``Contract.buyer``
— our own entity), using that row's OWN EvidenceFragment as
``source_fragment_id`` for both the SalesContract and the link. This
NEVER stitches fields across two different rows/fragments, and
``customer`` is NEVER inferred from it — see ``_backfill_sales_scope_basis``.

``Shipment`` has no genuine-Evidence source implemented in this round
(no export/customs-declaration adapter exists) — ``backfill_shipments``
below remains available for a future genuine-Evidence caller, but the
backfill PLAN (``cutover_plan.py``) does not wire it to any manifest
section: a plan assertion must never stand in for genuine export
Evidence (gate-fix section 2, HARD).
"""

from __future__ import annotations

import json
import string
import uuid
from collections import Counter
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
from bel.application.matching import normalized_contract_counterparties
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
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.normalize import normalize_counterparty
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ContractRepository,
    EvidenceRepository,
    ExceptionRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    PaymentRepository,
    SalesContractRepository,
)

CONTRACT_LEDGER_SOURCE_TYPE = "contract_ledger_xlsx"
INVOICE_LEDGER_SOURCE_TYPE = "invoice_ledger_xlsx"
BANK_STATEMENT_SOURCE_TYPE = "cmb_bank_statement_pdf"
SCOPE_DECISION_SOURCE_TYPE = "cutover_payment_scope_decision"
DEFAULT_CONTRACT_TYPE = "出口报关购销合同"
DEFAULT_CURRENCY = "CNY"

# Payment-scope adjudication (cutover-only private input): a decision is
# applied ONLY when it is an explicit OUT_OF_SCOPE that a human has
# confirmed. A DRAFT pending business confirmation is never applied.
SCOPE_DECISION_OUTCOME_OUT_OF_SCOPE = "OUT_OF_SCOPE"
SCOPE_DECISION_CONFIRMATION_HUMAN = "HUMAN_CONFIRMED"
SCOPE_DECISION_CONFIRMATION_DRAFT = "DRAFT_NEEDS_BUSINESS_CONFIRMATION"
_SCOPE_DECISION_CONFIRMATIONS = (
    SCOPE_DECISION_CONFIRMATION_HUMAN,
    SCOPE_DECISION_CONFIRMATION_DRAFT,
)


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


_TASK_KIND_TO_EXCEPTION_TYPE = {
    "IDENTITY_INCOMPLETE": ExceptionType.BACKFILL_IDENTITY_INCOMPLETE,
    "IDENTITY_AMBIGUOUS": ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS,
    "CONFLICT": ExceptionType.BACKFILL_CONFLICT,
}


@dataclass
class BackfillTaskRef:
    """One unresolved backfill row — never silently skipped, never
    silently guessed, and never ONLY an in-memory return value: every
    instance corresponds to a real, persisted, OPEN ``TaskException``
    (``task_exception_id``), which is what lets
    ``bel.application.cutover_reconciliation`` see it. ``kind`` is one
    of: IDENTITY_INCOMPLETE, IDENTITY_AMBIGUOUS, CONFLICT."""

    kind: str
    detail: dict[str, Any]
    task_exception_id: uuid.UUID


@dataclass
class BackfillOutcome:
    created: int = 0
    replay_or_corroborating: int = 0
    tasks: list[BackfillTaskRef] = field(default_factory=list)


def _find_or_create_backfill_task(
    session: Session, *, kind: str, fact_type: str, identity_key: str, summary: str, extra: dict[str, Any] | None = None,
    created_at: datetime,
) -> BackfillTaskRef:
    """Persists an OPEN ``TaskException`` for one unresolved backfill
    row — idempotently. A rerun that resolves to the SAME
    ``identity_key`` (a stable business-identity-derived string, never a
    row number or fragment id alone) reuses the existing OPEN row rather
    than creating a duplicate, so re-running the same plan against
    unchanged source data never piles up Tasks."""
    exception_type = _TASK_KIND_TO_EXCEPTION_TYPE[kind]
    exception_repo = ExceptionRepository(session)
    for existing in exception_repo.list_open():
        if existing.exception_type == exception_type and existing.detail.get("identity_key") == identity_key:
            return BackfillTaskRef(kind=kind, detail=existing.detail, task_exception_id=existing.id)

    detail = {"fact_type": fact_type, "identity_key": identity_key, **(extra or {})}
    task = TaskException(
        id=uuid.uuid4(), exception_type=exception_type, status=ExceptionStatus.OPEN, summary=summary, detail=detail,
        created_at=created_at,
    )
    exception_repo.add(task)
    session.flush()
    return BackfillTaskRef(kind=kind, detail=detail, task_exception_id=task.id)


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


def _get_or_create_document(
    session: Session, file_path: Path, *, source_type: str, now: datetime, sha256: str | None = None,
) -> tuple[EvidenceDocument, bool]:
    sha256 = sha256 or compute_sha256(file_path)
    evidence_repo = EvidenceRepository(session)
    existing = evidence_repo.find_document_by_sha256(sha256)
    if existing is not None:
        return existing, True
    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=source_type, imported_at=now
    )
    evidence_repo.add_document(document)
    return document, False


EXPORT_SALES_CONTRACT_NO_RAW_KEY = "外销合同编码"


def _backfill_sales_scope_basis(
    session: Session, *, contract: Contract, row, source_fragment_id: uuid.UUID, created_at: datetime
) -> None:
    """The frozen legacy-ledger sales-scope basis
    (docs/PHASE2D1-R0-DECISIONS.md section 2.4): the SAME contract-ledger
    row's ``外销合同编码`` (export sales-contract code, preserved in
    ``raw_data``) paired with that SAME row's ``买方``
    (``Contract.buyer`` — our own entity) is sufficient genuine Evidence
    to establish a ``customer=NULL`` SalesContract scope and the
    ProcurementSalesLink basis, using that row's OWN fragment as
    ``source_fragment_id`` for both. Never stitches fields across two
    different rows/fragments; ``customer`` is NEVER inferred — the
    unresolved-customer Task R3a already raises stands.

    A no-op when either half is missing on this row — there is
    deliberately no fallback and no cross-row search."""
    export_sales_contract_no = row.raw_data.get(EXPORT_SALES_CONTRACT_NO_RAW_KEY)
    if isinstance(export_sales_contract_no, str):
        export_sales_contract_no = export_sales_contract_no.strip()
    if not export_sales_contract_no or not contract.buyer:
        return

    our_entity = contract.buyer
    identity_key = f"SalesScopeBasis|{our_entity}|{export_sales_contract_no}"
    try:
        sales_result = create_sales_contract_fact(
            session, our_entity=our_entity, sales_contract_no=str(export_sales_contract_no), fields={},
            source_fragment_id=source_fragment_id, created_at=created_at,
        )
    except (SalesContractFactConflict, SalesContractFactError) as exc:
        _find_or_create_backfill_task(
            session, kind="CONFLICT", fact_type="SalesContract", identity_key=identity_key,
            summary=f"Sales-scope basis for Contract {contract.contract_no}: conflicting SalesContract content",
            extra={"reason": str(exc)}, created_at=created_at,
        )
        return

    try:
        add_procurement_sales_link(
            session, procurement_contract_id=contract.id, sales_contract_id=sales_result.sales_contract.id,
            source_fragment_id=source_fragment_id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED,
            created_at=created_at,
        )
    except (ProcurementSalesLinkFactConflict, ProcurementSalesLinkFactError) as exc:
        _find_or_create_backfill_task(
            session, kind="CONFLICT", fact_type="ProcurementSalesLink", identity_key=identity_key,
            summary=f"Sales-scope basis link for Contract {contract.contract_no}: conflicting link content",
            extra={"reason": str(exc)}, created_at=created_at,
        )


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
        identity_key = f"Contract|{row.contract_no}|{row.counterparty}"
        if not row.contract_no or not row.counterparty:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_INCOMPLETE", fact_type="Contract", identity_key=identity_key,
                    summary=f"Contract backfill row {row.row_number}: identity incomplete",
                    extra={
                        "missing_contract_no": not bool(row.contract_no),
                        "missing_counterparty": not bool(row.counterparty),
                    },
                    created_at=now,
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
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_AMBIGUOUS", fact_type="Contract", identity_key=identity_key,
                    summary=f"Contract backfill row {row.row_number}: identity ambiguous",
                    extra={"reason": str(exc)}, created_at=now,
                )
            )
            continue
        except (ContractFactConflict, ContractFactError) as exc:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="Contract", identity_key=identity_key,
                    summary=f"Contract backfill row {row.row_number}: conflicting content",
                    extra={"reason": str(exc)}, created_at=now,
                )
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
        _backfill_sales_scope_basis(session, contract=result.contract, row=row, source_fragment_id=fragment_id, created_at=now)
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
        identity_key = f"ContractItem|{contract_no}|{counterparty}|{source_item_key or f'index={index}'}"
        if not source_item_key:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_INCOMPLETE", fact_type="ContractItem", identity_key=identity_key,
                    summary=f"ContractItem backfill entry {index}: source_item_key missing", created_at=now,
                )
            )
            continue
        matches = contract_repo.find_by_identity(contract_no, counterparty)
        if len(matches) == 0:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_INCOMPLETE", fact_type="ContractItem", identity_key=identity_key,
                    summary=f"ContractItem backfill entry {index}: no matching Contract",
                    extra={"reason": "no matching Contract"}, created_at=now,
                )
            )
            continue
        if len(matches) > 1:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_AMBIGUOUS", fact_type="ContractItem", identity_key=identity_key,
                    summary=f"ContractItem backfill entry {index}: ambiguous Contract identity",
                    extra={"matches": len(matches)}, created_at=now,
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
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="ContractItem", identity_key=identity_key,
                    summary=f"ContractItem backfill entry {index}: conflicting content", extra={"reason": str(exc)},
                    created_at=now,
                )
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
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_INCOMPLETE", fact_type="Invoice",
                    identity_key=f"Invoice|{direction}|index={group_index}",
                    summary=f"Invoice backfill group {group_index}: external key missing", created_at=now,
                )
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
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="Invoice", identity_key=f"Invoice|{external_key}",
                    summary=f"Invoice backfill: {external_key} has conflicting content", created_at=now,
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
# Payment backfill — direction-scoped eligibility, explicit adjudication,
# then identity (source_account_id, transaction_date, direction, amount,
# bank_reference)
# ---------------------------------------------------------------------------


def _first_stage_out_scope_parties(session: Session) -> set[str] | None:
    """The normalized counterparty set of the CURRENT first-stage
    PROCUREMENT (OUT) Payment scope: the canonical M001 eligibility basis
    (``bel.application.matching.normalized_contract_counterparties`` — the
    single membership rule, never a second competing rule). This drives a
    deterministic first-stage OUT exclusion only. Sales-side IN receipts
    are NEVER auto-excluded by membership in this set — absence from known
    ``SalesContract.customer`` values is not evidence of OUT_OF_SCOPE (an
    unknown payer, a payer/customer mismatch, or ``customer=NULL`` all stay
    conservative). Returns ``None`` when no procurement Contract exists at
    all: an empty scope cannot affirmatively establish a row as OUTSIDE, so
    callers keep the conservative path."""
    parties = normalized_contract_counterparties(ContractRepository(session).list_all())
    return parties or None


def _import_scope_decision_evidence(session: Session, path: Path, applied: list[dict[str, Any]], *, now: datetime) -> None:
    """Record the APPLIED (HUMAN_CONFIRMED) scope adjudication as Evidence
    under a distinct source_type (``cutover_payment_scope_decision``), so
    the intake disposition is auditable without any schema change. One
    EvidenceDocument per artifact file (dedup by sha256); one fragment per
    applied decision, located by the SAME bank EvidenceFragment locator
    (page, transaction_index) — a source-row pointer, never Payment
    identity, never a generated bank_reference. A DRAFT artifact is never
    imported here because it disposes nothing."""
    if not applied:
        return
    document, is_reimport = _get_or_create_document(session, path, source_type=SCOPE_DECISION_SOURCE_TYPE, now=now)
    if is_reimport:
        return
    evidence_repo = EvidenceRepository(session)
    for entry in applied:
        fragment = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=document.id, fragment_kind=FragmentKind.MANUAL_FACT,
            sheet_name=None, row_number=None,
            locator_json={"page": int(entry["page"]), "transaction_index": int(entry["transaction_index"])},
            raw_data=dict(entry), created_at=now,
        )
        evidence_repo.add_fragment(fragment)
    session.flush()


def _read_payment_scope_decision_artifact(path: Path, *, expected_source_sha256: str) -> list[dict[str, Any]]:
    """Load the cutover-only Payment-scope adjudication artifact
    (``<period>/facts/payment-scope-decisions.json``) and bind it to the
    EXACT immutable bank source document.

    Binding & validation happen in full BEFORE anything is applied (no
    partial artifact may affect database state):

      1. schema: a version-1 JSON object with an ``entries`` list;
      2. source binding: ``source_sha256`` must be present and EQUAL to the
         SHA256 of the exact bank source file this artifact was confirmed
         against — a mismatch or a missing value is a HARD FAIL, and the
         artifact can never silently bind to a different source document;
      3. locator schema: every entry's ``page`` / ``transaction_index``
         must be an exact non-negative JSON/Python integer — numeric
         strings, floats, booleans, null and negatives are rejected, never
         coerced;
      4. duplicate locators within one artifact are rejected (no
         last-write-wins, no tolerance even for identical payloads);
      5. the artifact is fully validated before any source parsing or
         database interaction.

    This phase is deliberately pure: no Evidence or other database object
    exists until the source is parsed and every confirmed locator is proved
    to resolve exactly once."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payment scope decisions must be a JSON object")
    if raw.get("version") != 1:
        raise ValueError("payment scope decisions require version=1")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ValueError("payment scope decisions require an 'entries' list")

    source_sha256 = raw.get("source_sha256")
    if (
        not isinstance(source_sha256, str)
        or len(source_sha256) != 64
        or any(char not in string.hexdigits for char in source_sha256)
        or source_sha256 != expected_source_sha256
    ):
        raise ValueError(
            "payment scope decision artifact source_sha256 does not match the exact bank source document — "
            "refusing to apply any decision"
        )

    seen: set[tuple[int, int]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("payment scope decision entries must be objects")
        if entry.get("decision") != SCOPE_DECISION_OUTCOME_OUT_OF_SCOPE:
            raise ValueError(f"payment scope decision supports only decision={SCOPE_DECISION_OUTCOME_OUT_OF_SCOPE!r}")
        confirmation_type = entry.get("confirmation_type")
        if confirmation_type not in _SCOPE_DECISION_CONFIRMATIONS:
            raise ValueError(
                "payment scope decision confirmation_type must be one of "
                f"{sorted(_SCOPE_DECISION_CONFIRMATIONS)!r}"
            )
        page, transaction_index = entry.get("page"), entry.get("transaction_index")
        # Strict non-negative integers; booleans are ints in Python and
        # must be rejected — type() is used, never int() coercion.
        if type(page) is not int or page < 0 or type(transaction_index) is not int or transaction_index < 0:
            raise ValueError(
                "payment scope decision locators (page, transaction_index) must be exact non-negative integers"
            )
        locator = (page, transaction_index)
        if locator in seen:
            raise ValueError("payment scope decision artifact contains a duplicate locator")
        seen.add(locator)
    return entries


def _resolve_payment_scope_decisions(
    entries: list[dict[str, Any]], transactions: list[ParsedBankTransaction],
) -> list[dict[str, Any]]:
    """Resolve every confirmed adjudication against its already-bound source.

    The full artifact was schema-validated before parsing. This second pure
    phase proves one-to-one source-row resolution before any decision can
    create Evidence or alter the eventual Payment disposition."""
    available = Counter((t.page_index, t.transaction_index) for t in transactions)
    applied: list[dict[str, Any]] = []
    for entry in entries:
        if entry["confirmation_type"] == SCOPE_DECISION_CONFIRMATION_HUMAN:
            locator = (entry["page"], entry["transaction_index"])
            if available[locator] != 1:
                raise ValueError(
                    "payment scope decision locator must resolve to exactly one transaction in the bound source"
                )
            applied.append(entry)
    return applied


def _load_payment_scope_decisions(
    session: Session,
    path: Path,
    transactions: list[ParsedBankTransaction],
    *,
    expected_source_sha256: str,
    now: datetime,
) -> dict[tuple[int, int], dict[str, Any]]:
    """Compatibility wrapper for focused tests and non-plan callers.

    Production plan execution uses the same two pure validation phases
    before creating its bank EvidenceDocument; this wrapper keeps the
    private artifact Evidence write after both phases have succeeded."""
    entries = _read_payment_scope_decision_artifact(path, expected_source_sha256=expected_source_sha256)
    applied = _resolve_payment_scope_decisions(entries, transactions)
    if applied:
        _import_scope_decision_evidence(session, path, applied, now=now)
    return {(e["page"], e["transaction_index"]): e for e in applied}


def backfill_payment_transactions(
    session: Session,
    transactions: list[ParsedBankTransaction],
    *,
    source_account_id: str | None,
    document_id: uuid.UUID,
    created_at: datetime | None = None,
    scope_decisions: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> BackfillOutcome:
    """The identity-resolution core, factored out so it can be tested
    without a real PDF: ``backfill_payments`` below is a thin wrapper
    that parses a file and calls this.

    Decision order for every parsed row:
      1. the bank EvidenceFragment is created FIRST (Bank Statement !=
         Payment Ledger — an excluded movement is never deleted, never
         omitted);
      2. deterministic OUT procurement scope filter (M001 counterparty
         membership only) — a procurement OUT payment to a non-scope party
         is excluded without any further input;
      3. explicit private scope adjudication (HUMAN_CONFIRMED
         OUT_OF_SCOPE, keyed by source locator) — the only way a
         non-business IN bank movement is excluded;
      4. otherwise the row continues conservatively (an IN receipt is never
         auto-excluded merely because its payer is unknown / mismatched /
         ``customer=NULL`` — it may exist before any SalesContract is
         known);
      5. the frozen Payment identity validation runs unchanged: a row with
         incomplete identity stays IDENTITY_INCOMPLETE.

    Missing ``bank_reference`` is NEVER an out-of-scope reason. ``scope_decisions``
    locators (page_index, transaction_index) identify the source Evidence
    row a decision refers to; they are never used as Payment business
    identity and never generate a bank_reference."""
    now = created_at or datetime.now(timezone.utc)
    outcome = BackfillOutcome()
    payment_repo = PaymentRepository(session)
    evidence_repo = EvidenceRepository(session)
    scope_decisions = scope_decisions or {}
    out_scope_parties = _first_stage_out_scope_parties(session)

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

        # Step 2 — deterministic OUT procurement scope filter (M001
        # membership). Evidence above is kept; nothing below is produced.
        if direction == PaymentDirection.OUT and out_scope_parties is not None:
            normalized_cp = normalize_counterparty(txn.counterparty)
            if normalized_cp and normalized_cp not in out_scope_parties:
                continue

        # Step 3 — explicit HUMAN_CONFIRMED OUT_OF_SCOPE adjudication,
        # anchored to this source Evidence row's own locator.
        if (txn.page_index, txn.transaction_index) in scope_decisions:
            continue

        if not source_account_id or not txn.bank_reference:
            # No stable business identity exists for this row (that is
            # the whole problem) — the best available deterministic
            # dedup key uses whatever IS present plus the transaction's
            # own position, so an exact rerun of the SAME source still
            # reuses the same OPEN Task rather than piling up duplicates.
            identity_key = (
                f"Payment|incomplete|account={source_account_id}|date={txn.transaction_date}|direction={direction}"
                f"|amount={amount}|bank_reference={txn.bank_reference}|txn={txn.transaction_index}"
            )
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_INCOMPLETE", fact_type="Payment", identity_key=identity_key,
                    summary=f"Payment backfill transaction {txn.transaction_index}: identity incomplete",
                    extra={
                        "missing_source_account_id": not bool(source_account_id),
                        "missing_bank_reference": not bool(txn.bank_reference),
                    },
                    created_at=now,
                )
            )
            continue

        payment_identity_key = (
            f"Payment|{source_account_id}|{txn.transaction_date}|{direction}|{amount}|{txn.bank_reference}"
        )
        matches = payment_repo.find_by_identity(
            source_account_id=source_account_id, transaction_date=txn.transaction_date, direction=direction,
            amount=amount, bank_reference=txn.bank_reference,
        )
        if len(matches) > 1:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="IDENTITY_AMBIGUOUS", fact_type="Payment", identity_key=payment_identity_key,
                    summary=f"Payment backfill transaction {txn.transaction_index}: ambiguous identity",
                    extra={"matches": len(matches)}, created_at=now,
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
                    _find_or_create_backfill_task(
                        session, kind="CONFLICT", fact_type="Payment", identity_key=payment_identity_key,
                        summary=f"Payment backfill transaction {txn.transaction_index}: conflicting content",
                        created_at=now,
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
    session: Session,
    file_path: Path,
    profile: str,
    *,
    source_account_id: str | None,
    created_at: datetime | None = None,
    scope_decisions_path: Path | None = None,
) -> BackfillOutcome:
    """``source_account_id`` is an explicit caller-supplied input seam
    (section 7): the current CMB statement adapter cannot deterministically
    parse an account identifier from the PDF text layer, so it is never
    guessed from the filename, counterparty, or profile name.

    ``scope_decisions_path`` (optional, cutover-only) names a private
    Payment-scope adjudication artifact that the backfill PLAN has already
    resolved strictly inside the period directory. Only HUMAN_CONFIRMED
    OUT_OF_SCOPE decisions are applied and recorded as Evidence
    (source_type ``cutover_payment_scope_decision``); the path itself is
    orchestration — it never becomes a business fact or a rule manifest.

    Every adjudication artifact is BOUND to the exact immutable bank source
    document: the artifact's ``source_sha256`` must equal the SHA256 of
    ``file_path``, or the whole load hard-fails and nothing is applied. The
    SHA is provenance binding only — it is never Payment identity and never
    a bank_reference fallback. An invalid artifact (mismatch, malformed
    locator, duplicate, unresolvable) raises BEFORE any bank EvidenceDocument
    is created, so an invalid artifact can never partially affect database
    state nor mask a later corrected retry as a re-import."""
    if profile != "cmb":
        raise ValueError(f"unsupported bank profile {profile!r}")
    now = created_at or datetime.now(timezone.utc)
    source_sha256 = compute_sha256(file_path)
    artifact_entries: list[dict[str, Any]] = []
    if scope_decisions_path is not None:
        artifact_entries = _read_payment_scope_decision_artifact(
            scope_decisions_path, expected_source_sha256=source_sha256
        )

    # Pure reads + complete source-row resolution before any DB write.
    parsed = parse_cmb_bank_statement(file_path)
    if compute_sha256(file_path) != source_sha256:
        raise ValueError("bank source changed while validating payment scope decisions")
    scope_decisions: dict[tuple[int, int], dict[str, Any]] = {}
    applied_decisions = _resolve_payment_scope_decisions(artifact_entries, parsed.transactions)
    scope_decisions = {(entry["page"], entry["transaction_index"]): entry for entry in applied_decisions}

    document, is_reimport = _get_or_create_document(
        session, file_path, source_type=BANK_STATEMENT_SOURCE_TYPE, now=now, sha256=source_sha256
    )
    if is_reimport:
        return BackfillOutcome()
    if scope_decisions_path is not None:
        _import_scope_decision_evidence(session, scope_decisions_path, applied_decisions, now=now)
    return backfill_payment_transactions(
        session, parsed.transactions, source_account_id=source_account_id, document_id=document.id, created_at=now,
        scope_decisions=scope_decisions,
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
        contract_no, counterparty = entry.get("contract_no"), entry.get("counterparty")
        identity_key = f"Shipment|{contract_no}|{counterparty}|index={index}"
        matches = contract_repo.find_by_identity(contract_no, counterparty)
        if len(matches) != 1:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session,
                    kind="IDENTITY_AMBIGUOUS" if len(matches) > 1 else "IDENTITY_INCOMPLETE",
                    fact_type="Shipment", identity_key=identity_key,
                    summary=f"Shipment backfill entry {index}: Contract identity unresolved",
                    extra={"matches": len(matches)}, created_at=now,
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
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="Shipment",
                    identity_key=f"Shipment|{contract_no}|{counterparty}|{entry.get('external_reference')}|{entry.get('execution_date')}",
                    summary=f"Shipment backfill entry {index}: conflicting content", extra={"reason": str(exc)},
                    created_at=now,
                )
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
        our_entity, sales_contract_no = entry.get("our_entity"), entry.get("sales_contract_no")
        try:
            result = create_sales_contract_fact(
                session, our_entity=our_entity, sales_contract_no=sales_contract_no,
                fields=entry.get("fields", {}), source_fragment_id=source_fragment_id, created_at=now,
            )
        except (SalesContractFactConflict, SalesContractFactError) as exc:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="SalesContract",
                    identity_key=f"SalesContract|{our_entity}|{sales_contract_no}",
                    summary=f"SalesContract backfill entry {index}: conflicting content", extra={"reason": str(exc)},
                    created_at=now,
                )
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
        contract_no, counterparty = entry.get("contract_no"), entry.get("counterparty")
        sales_our_entity, sales_contract_no = entry.get("sales_our_entity"), entry.get("sales_contract_no")
        identity_key = f"ProcurementSalesLink|{contract_no}|{counterparty}|{sales_our_entity}|{sales_contract_no}"
        contract_matches = contract_repo.find_by_identity(contract_no, counterparty)
        sales_contract = sales_contract_repo.find_by_identity(sales_our_entity, sales_contract_no)
        if len(contract_matches) != 1 or sales_contract is None:
            outcome.tasks.append(
                _find_or_create_backfill_task(
                    session,
                    kind="IDENTITY_AMBIGUOUS" if len(contract_matches) > 1 else "IDENTITY_INCOMPLETE",
                    fact_type="ProcurementSalesLink", identity_key=identity_key,
                    summary=f"ProcurementSalesLink backfill entry {index}: identity unresolved",
                    extra={"contract_matches": len(contract_matches), "sales_contract_found": sales_contract is not None},
                    created_at=now,
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
                _find_or_create_backfill_task(
                    session, kind="CONFLICT", fact_type="ProcurementSalesLink", identity_key=identity_key,
                    summary=f"ProcurementSalesLink backfill entry {index}: conflicting content",
                    extra={"reason": str(exc)}, created_at=now,
                )
            )
            continue
        if result.created:
            outcome.created += 1
        else:
            outcome.replay_or_corroborating += 1
    return outcome
