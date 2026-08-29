"""Close Fact Pack import.

V1 already allows "人工补充事实" (manually supplied facts). Phase 2B
builds a deliberately narrow Close Fact Pack — contract items, cost
recognition facts, accrual basis facts, historical accrual facts,
invoice item allocations and (Phase 2B decision, see
docs/PHASE2B-DECISIONS.md) accrual reversals for go-live
already-partially-reversed state. This is NOT a generic import platform:
the pack is versioned and its sections are fixed.

Every entry becomes an EvidenceFragment (fragment_kind=MANUAL_FACT,
locator = {"section": ..., "index": ...}) so every typed Fact stays
traceable to Evidence per A02 (Decision -> Fact -> Evidence).

Idempotency: re-importing the same bytes (same sha256) creates zero new
facts (the document already exists). Beyond that, contract items,
facts, accruals, item allocations and reversals are also skip-if-exists
so a compatible re-import never doubles a fact — in particular, a
historical accrual is never imported twice into two ACTIVE Accruals.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bel.adapters.common import compute_sha256
from bel.application.contract_item_facts import create_contract_item_fact
from bel.application.item_allocation import validate_item_allocation
from bel.infrastructure.persistence.database import is_database_busy
from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualBasisScopeType,
    AccrualReversal,
    AccrualStatus,
    CostRecognitionBasis,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
    ItemAllocationConfirmationType,
    ManualBasis,
    get_accrual_balance,
    get_projected_accrual_status,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.normalize import normalize_counterparty
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualReversalRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)

SOURCE_TYPE = "close_fact_pack_json"
PACK_VERSION = 1

_SECTIONS = (
    "contract_items",
    "cost_recognition_facts",
    "accrual_basis_facts",
    "historical_accrual_facts",
    "invoice_item_allocations",
    "accrual_reversals",
)

_VALID_COST_RECOGNITION_BASIS = {
    CostRecognitionBasis.MANUAL_CONFIRMED,
    CostRecognitionBasis.SALES_EXECUTION_CONFIRMED,
    CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED,
}


class CloseFactPackError(ValueError):
    """A rejected Fact Pack — malformed, ambiguous selector, or a
    section-11 safety violation. Surfaces to the CLI as an explicit
    failure, never a silent partial import."""


@dataclass
class CloseFactImportResult:
    evidence_document_id: uuid.UUID
    file_name: str
    sha256: str
    is_reimport: bool
    contract_items_created: int = 0
    contract_items_skipped: int = 0
    cost_recognition_facts_created: int = 0
    cost_recognition_facts_skipped: int = 0
    accrual_basis_facts_created: int = 0
    accrual_basis_facts_skipped: int = 0
    historical_accrual_facts_created: int = 0
    historical_accrual_facts_skipped: int = 0
    accruals_created: int = 0
    accruals_skipped: int = 0
    invoice_item_allocations_created: int = 0
    invoice_item_allocations_skipped: int = 0
    accrual_reversals_created: int = 0
    accrual_reversals_skipped: int = 0
    source_periods: list[str] = field(default_factory=list)


def _d(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise CloseFactPackError(f"expected a number, got {value!r}")


def _date(value: object, label: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise CloseFactPackError(f"{label}: expected an ISO date string, got {value!r}")


def _datetime(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise CloseFactPackError(f"{label}: expected an ISO datetime string, got {value!r}")


def _selector_from(entry: dict, section: str) -> dict:
    selector = entry.get("contract_selector")
    if not isinstance(selector, dict) or "contract_no" not in selector:
        raise CloseFactPackError(f"{section}: every entry needs contract_selector.contract_no")
    if "counterparty" not in selector:
        raise CloseFactPackError(f"{section}: every entry needs contract_selector.counterparty")
    return selector


class _ContractResolver:
    """contract_no + counterparty business selector -> exactly one
    Contract. 0 matches and >1 matches both reject — never "take the
    first one" (spec section 14)."""

    def __init__(self, session: Session) -> None:
        self._contracts = ContractRepository(session).list_all()
        self._cache: dict[tuple[str, str | None], Contract] = {}

    def resolve(self, selector: dict, section: str) -> Contract:
        contract_no = selector["contract_no"]
        counterparty_norm = normalize_counterparty(selector["counterparty"])
        key = (contract_no, counterparty_norm)
        if key in self._cache:
            return self._cache[key]

        matches = [
            c
            for c in self._contracts
            if c.contract_no == contract_no
            and counterparty_norm is not None
            and normalize_counterparty(c.counterparty) == counterparty_norm
        ]
        if len(matches) == 0:
            raise CloseFactPackError(
                f"{section}: contract selector {selector!r} resolved to 0 contracts — rejecting"
            )
        if len(matches) > 1:
            raise CloseFactPackError(
                f"{section}: contract selector {selector!r} resolved to {len(matches)} contracts — rejecting, not guessing"
            )
        self._cache[key] = matches[0]
        return matches[0]


def _invoice_item_lookup(session: Session) -> dict[tuple[str, int], uuid.UUID]:
    """invoice external_key + line_no -> invoice_item_id. Duplicate
    keys are an error (ambiguous), never a guess."""
    result: dict[tuple[str, int], uuid.UUID] = {}
    invoice_repo = InvoiceRepository(session)
    for item in InvoiceItemRepository(session).list_all():
        invoice = invoice_repo.get(item.invoice_id)
        if invoice is None or invoice.external_invoice_key is None:
            continue
        key = (invoice.external_invoice_key, item.line_no)
        if key in result and result[key] != item.id:
            raise CloseFactPackError(f"invoice_item key {key!r} resolves to multiple invoice items — rejecting")
        result[key] = item.id
    return result


def import_close_facts(session: Session, file_path: Path) -> CloseFactImportResult:
    """Import a Close Fact Pack. A SQLite busy/lock error (a concurrent
    writer holding the write lock) is surfaced as a controlled
    ``CloseFactPackError`` after rolling the whole transaction back — the
    import never leaves partial business state."""
    try:
        return _import_close_facts(session, file_path)
    except OperationalError as exc:
        session.rollback()
        if is_database_busy(exc):
            raise CloseFactPackError(
                "database is busy; the import was rolled back — retry when the "
                "other write completes"
            ) from exc
        raise


def _import_close_facts(session: Session, file_path: Path) -> CloseFactImportResult:
    now = datetime.now(timezone.utc)
    sha256 = compute_sha256(file_path)

    evidence_repo = EvidenceRepository(session)
    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        return CloseFactImportResult(
            evidence_document_id=existing_document.id,
            file_name=file_path.name,
            sha256=sha256,
            is_reimport=True,
        )

    try:
        pack = json.loads(file_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CloseFactPackError(f"unreadable close fact pack: {exc}") from exc
    if pack.get("version") != PACK_VERSION:
        raise CloseFactPackError(f"unsupported pack version: {pack.get('version')!r} (expected {PACK_VERSION})")

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_path.name, sha256=sha256, source_type=SOURCE_TYPE, imported_at=now
    )
    evidence_repo.add_document(document)

    result = CloseFactImportResult(
        evidence_document_id=document.id,
        file_name=file_path.name,
        sha256=sha256,
        is_reimport=False,
    )

    contract_repo = ContractRepository(session)
    item_repo = ContractItemRepository(session)
    cost_rec_repo = CostRecognitionFactRepository(session)
    basis_repo = AccrualBasisFactRepository(session)
    hist_repo = HistoricalAccrualFactRepository(session)
    accrual_repo = AccrualRepository(session)
    alloc_repo = InvoiceItemAllocationRepository(session)
    reversal_repo = AccrualReversalRepository(session)

    # Pass 1 — EvidenceFragments for every entry (A02 traceability).
    # Flush before any typed fact references a fragment id.
    entry_lists: dict[str, list[dict]] = {}
    for section in _SECTIONS:
        entries = pack.get(section, [])
        if not isinstance(entries, list):
            raise CloseFactPackError(f"{section}: expected a list, got {type(entries).__name__}")
        entry_lists[section] = entries

    fragment_ids: dict[tuple[str, int], uuid.UUID] = {}
    for section in _SECTIONS:
        for index, entry in enumerate(entry_lists[section]):
            fragment = EvidenceFragment(
                id=uuid.uuid4(),
                evidence_document_id=document.id,
                fragment_kind=FragmentKind.MANUAL_FACT,
                sheet_name=None,
                row_number=None,
                locator_json={"section": section, "index": index},
                raw_data=entry,
                created_at=now,
            )
            evidence_repo.add_fragment(fragment)
            fragment_ids[(section, index)] = fragment.id
    session.flush()

    resolver = _ContractResolver(session)
    invoice_item_ids = _invoice_item_lookup(session)

    # Pass 2 — contract items. (contract_id, source_item_key) is the
    # stable business identity (docs/PHASE2D1-R0-DECISIONS.md section
    # 4.4). Routed through create_contract_item_fact so the Close Fact
    # Pack and the Phase 2D.1-R1 CLI/Web commands share ONE authoritative
    # ContractItem write path (anchor + INITIAL revision) rather than
    # two. A duplicate key is NOT a blind skip (Phase 2D.1-R1 Codex fix
    # round, BLOCKER 1): a re-import that agrees with the existing
    # INITIAL assertion is silently corroborating (this pass's pre-fix
    # skip-if-exists behaviour, preserved for that case only); a
    # re-import whose file asserts DIFFERENT values under the SAME
    # business identity now surfaces as a ContractItemFactConflict ->
    # CloseFactPackError, exactly per section 6 of the fix round — the
    # importer must not silently swallow a genuinely conflicting business
    # value under cover of "the item already exists".
    contract_item_ids: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    for index, entry in enumerate(entry_lists["contract_items"]):
        fragment_id = fragment_ids[("contract_items", index)]
        selector = _selector_from(entry, "contract_items")
        contract = resolver.resolve(selector, "contract_items")
        source_item_key = entry.get("source_item_key")
        if not source_item_key:
            raise CloseFactPackError("contract_items: source_item_key is required")
        fact_fields = {
            "sku": entry.get("sku"),
            "product_name": entry.get("product_name"),
            "specification": entry.get("specification"),
            "quantity": _d(entry["quantity"]) if entry.get("quantity") is not None else None,
            "unit": entry.get("unit"),
            "unit_price": _d(entry["unit_price"]) if entry.get("unit_price") is not None else None,
            "gross_amount": _d(entry["gross_amount"]) if entry.get("gross_amount") is not None else None,
            "tax_rate": _d(entry["tax_rate"]) if entry.get("tax_rate") is not None else None,
            "net_amount": _d(entry["net_amount"]) if entry.get("net_amount") is not None else None,
        }
        try:
            fact_result = create_contract_item_fact(
                session,
                contract_id=contract.id,
                source_item_key=source_item_key,
                fields=fact_fields,
                source_fragment_id=fragment_id,
                created_at=now,
            )
        except ValueError as exc:
            raise CloseFactPackError(f"contract_items: {exc}") from exc
        contract_item_ids[(contract.id, source_item_key)] = fact_result.item.id
        if fact_result.created:
            result.contract_items_created += 1
        else:
            result.contract_items_skipped += 1
    session.flush()

    def _item_id(contract: Contract, entry: dict, section: str) -> uuid.UUID:
        source_item_key = entry.get("source_item_key")
        if not source_item_key:
            raise CloseFactPackError(f"{section}: source_item_key is required")
        try:
            return contract_item_ids[(contract.id, source_item_key)]
        except KeyError as exc:
            raise CloseFactPackError(
                f"{section}: contract {contract.contract_no!r} has no contract item with source_item_key "
                f"{source_item_key!r} in this pack"
            ) from exc

    # Pass 3 — cost recognition facts.
    for index, entry in enumerate(entry_lists["cost_recognition_facts"]):
        fragment_id = fragment_ids[("cost_recognition_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "cost_recognition_facts"), "cost_recognition_facts")
        basis = entry.get("basis")
        if basis not in _VALID_COST_RECOGNITION_BASIS:
            raise CloseFactPackError(f"cost_recognition_facts: unsupported basis {basis!r}")
        recognition_date = _date(entry["recognition_date"], "cost_recognition_facts")
        if cost_rec_repo.find_duplicate(contract.id, recognition_date, basis) is not None:
            result.cost_recognition_facts_skipped += 1
            continue
        cost_rec_repo.add(
            CostRecognitionFact(
                id=uuid.uuid4(),
                contract_id=contract.id,
                recognition_date=recognition_date,
                basis=basis,
                source_fragment_id=fragment_id,
                created_at=now,
            )
        )
        result.cost_recognition_facts_created += 1

    # Pass 4 — accrual basis facts.
    for index, entry in enumerate(entry_lists["accrual_basis_facts"]):
        fragment_id = fragment_ids[("accrual_basis_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "accrual_basis_facts"), "accrual_basis_facts")
        scope_type = entry.get("scope_type")
        if scope_type not in {AccrualBasisScopeType.CONTRACT, AccrualBasisScopeType.CONTRACT_ITEM}:
            raise CloseFactPackError(f"accrual_basis_facts: unsupported scope_type {scope_type!r}")
        basis = entry.get("basis", ManualBasis.MANUAL_CONFIRMED)
        estimated_cost = _d(entry["estimated_cost"])
        if scope_type == AccrualBasisScopeType.CONTRACT_ITEM:
            contract_item_id = _item_id(contract, entry, "accrual_basis_facts")
            quantity = _d(entry["quantity"]) if entry.get("quantity") is not None else None
        else:
            contract_item_id = None
            quantity = None
        if basis_repo.find_duplicate(contract.id, scope_type, contract_item_id, estimated_cost, basis) is not None:
            result.accrual_basis_facts_skipped += 1
            continue
        basis_repo.add(
            AccrualBasisFact(
                id=uuid.uuid4(),
                scope_type=scope_type,
                contract_id=contract.id,
                contract_item_id=contract_item_id,
                quantity=quantity,
                estimated_cost=estimated_cost,
                basis=basis,
                source_fragment_id=fragment_id,
                created_at=now,
            )
        )
        result.accrual_basis_facts_created += 1
    session.flush()

    # Pass 5 — historical accrual facts -> Accrual. An accrual for the
    # same (contract_item, source_period) is never created twice, so a
    # repeated historical accrual can never produce two ACTIVE Accruals.
    accrual_by_item_period: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    for index, entry in enumerate(entry_lists["historical_accrual_facts"]):
        fragment_id = fragment_ids[("historical_accrual_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "historical_accrual_facts"), "historical_accrual_facts")
        source_period = entry.get("source_period")
        if not source_period:
            raise CloseFactPackError("historical_accrual_facts: source_period is required")
        contract_item_id = _item_id(contract, entry, "historical_accrual_facts")
        quantity = _d(entry["quantity"])
        estimated_cost = _d(entry["estimated_cost"])
        basis = entry.get("basis", ManualBasis.MANUAL_CONFIRMED)
        confirmed_at = _datetime(entry.get("confirmed_at"), "historical_accrual_facts")

        existing_accrual = accrual_repo.find_by_item_and_period(contract_item_id, source_period)
        if existing_accrual is not None:
            accrual_by_item_period[(contract_item_id, source_period)] = existing_accrual.id
            result.accruals_skipped += 1
        else:
            existing_fact = hist_repo.find_duplicate(contract_item_id, source_period, quantity, estimated_cost, basis)
            if existing_fact is not None:
                fact_id = existing_fact.id
                result.historical_accrual_facts_skipped += 1
            else:
                fact_id = uuid.uuid4()
                hist_repo.add(
                    HistoricalAccrualFact(
                        id=fact_id,
                        source_period=source_period,
                        contract_item_id=contract_item_id,
                        quantity=quantity,
                        estimated_cost=estimated_cost,
                        basis=basis,
                        source_fragment_id=fragment_id,
                        confirmed_at=confirmed_at,
                    )
                )
                result.historical_accrual_facts_created += 1

            accrual = Accrual(
                id=uuid.uuid4(),
                period=source_period,
                contract_item_id=contract_item_id,
                quantity=quantity,
                estimated_cost=estimated_cost,
                basis=basis,
                status=AccrualStatus.ACTIVE,
                created_from_fact_id=fact_id,
                created_at=now,
            )
            accrual_repo.add(accrual)
            accrual_by_item_period[(contract_item_id, source_period)] = accrual.id
            result.accruals_created += 1
        if source_period not in result.source_periods:
            result.source_periods.append(source_period)
    session.flush()

    # Pass 6 — InvoiceItemAllocation (section-11 safety enforced).
    allocation_ids: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
    for index, entry in enumerate(entry_lists["invoice_item_allocations"]):
        fragment_id = fragment_ids[("invoice_item_allocations", index)]
        contract = resolver.resolve(_selector_from(entry, "invoice_item_allocations"), "invoice_item_allocations")
        invoice_ref = entry.get("invoice")
        if not isinstance(invoice_ref, dict) or "external_key" not in invoice_ref or "line_no" not in invoice_ref:
            raise CloseFactPackError("invoice_item_allocations: invoice.external_key and invoice.line_no are required")
        try:
            invoice_item_id = invoice_item_ids[(invoice_ref["external_key"], int(invoice_ref["line_no"]))]
        except KeyError as exc:
            raise CloseFactPackError(
                f"invoice_item_allocations: no invoice item for {invoice_ref!r} — the invoice must be imported first"
            ) from exc
        contract_item_id = _item_id(contract, entry, "invoice_item_allocations")
        allocated_quantity = _d(entry["allocated_quantity"])
        allocated_net_amount = _d(entry["allocated_net_amount"])
        confirmation_type = entry.get("confirmation_type", ItemAllocationConfirmationType.MANUAL_CONFIRMED)

        existing = alloc_repo.find(invoice_item_id, contract_item_id)
        if existing is not None:
            allocation_ids[(invoice_item_id, contract_item_id)] = existing.id
            result.invoice_item_allocations_skipped += 1
            continue

        invoice_item = InvoiceItemRepository(session).get(invoice_item_id)
        contract_item = item_repo.get(contract_item_id)
        if invoice_item is None or contract_item is None:
            raise CloseFactPackError("invoice_item_allocations: referenced invoice item or contract item not found")
        try:
            validate_item_allocation(
                session=session,
                invoice_item=invoice_item,
                contract_item=contract_item,
                allocated_quantity=allocated_quantity,
                allocated_net_amount=allocated_net_amount,
            )
        except ValueError as exc:
            raise CloseFactPackError(str(exc)) from exc

        allocation_id = uuid.uuid4()
        alloc_repo.add(
            InvoiceItemAllocation(
                id=allocation_id,
                invoice_item_id=invoice_item_id,
                contract_item_id=contract_item_id,
                allocated_quantity=allocated_quantity,
                allocated_net_amount=allocated_net_amount,
                confirmation_type=confirmation_type,
                source_fragment_id=fragment_id,
                created_at=now,
            )
        )
        allocation_ids[(invoice_item_id, contract_item_id)] = allocation_id
        result.invoice_item_allocations_created += 1
    session.flush()

    # Pass 7 — AccrualReversal (go-live partial-reversal state). Links an
    # already-created item allocation to an already-created accrual.
    pending_status: set[uuid.UUID] = set()
    for index, entry in enumerate(entry_lists["accrual_reversals"]):
        contract = resolver.resolve(_selector_from(entry, "accrual_reversals"), "accrual_reversals")
        source_item_key = entry.get("source_item_key")
        accrual_source_period = entry.get("accrual_source_period")
        if not source_item_key or not accrual_source_period:
            raise CloseFactPackError("accrual_reversals: source_item_key and accrual_source_period are required")
        try:
            contract_item_id = contract_item_ids[(contract.id, source_item_key)]
        except KeyError as exc:
            raise CloseFactPackError(
                f"accrual_reversals: contract {contract.contract_no!r} has no contract item {source_item_key!r} in this pack"
            ) from exc
        try:
            accrual_id = accrual_by_item_period[(contract_item_id, accrual_source_period)]
        except KeyError as exc:
            raise CloseFactPackError(
                f"accrual_reversals: no accrual for item {source_item_key!r} in period {accrual_source_period!r}"
            ) from exc

        invoice_ref = entry.get("invoice")
        if not isinstance(invoice_ref, dict) or "external_key" not in invoice_ref or "line_no" not in invoice_ref:
            raise CloseFactPackError("accrual_reversals: invoice.external_key and invoice.line_no are required")
        try:
            invoice_item_id = invoice_item_ids[(invoice_ref["external_key"], int(invoice_ref["line_no"]))]
        except KeyError as exc:
            raise CloseFactPackError(f"accrual_reversals: no invoice item for {invoice_ref!r}") from exc
        try:
            allocation_id = allocation_ids[(invoice_item_id, contract_item_id)]
        except KeyError as exc:
            raise CloseFactPackError(
                f"accrual_reversals: no item allocation for {invoice_ref!r} -> {source_item_key!r}"
            ) from exc

        reversed_quantity = _d(entry["reversed_quantity"])
        reversed_estimated_cost = _d(entry["reversed_estimated_cost"])
        if reversal_repo.find_by_allocation(accrual_id, allocation_id) is not None:
            result.accrual_reversals_skipped += 1
            continue
        reversal_repo.add(
            AccrualReversal(
                id=uuid.uuid4(),
                accrual_id=accrual_id,
                period=entry.get("period", accrual_source_period),
                invoice_item_allocation_id=allocation_id,
                reversed_quantity=reversed_quantity,
                reversed_estimated_cost=reversed_estimated_cost,
                created_at=now,
            )
        )
        result.accrual_reversals_created += 1
        pending_status.add(accrual_id)

    # Recompute every touched Accrual's status from original minus
    # reversals — the only sanctioned source of truth (section 8/9).
    for accrual_id in pending_status:
        accrual = accrual_repo.get(accrual_id)
        if accrual is None:
            continue
        reversals = reversal_repo.list_for_accrual(accrual_id)
        remaining_qty, _, reversed_qty, _ = get_accrual_balance(accrual, reversals)
        status = get_projected_accrual_status(reversed_qty, remaining_qty)
        accrual_repo.update_status(accrual_id, status)

    session.commit()
    return result
