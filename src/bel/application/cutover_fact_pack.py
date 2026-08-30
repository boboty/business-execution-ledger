"""Human-Confirmed Cutover Fact Pack (Phase 2D.1-R5,
docs/PHASE2D1-R0-DECISIONS.md section 4.3).

Some historical business cannot have its original Evidence
reconstructed. This module is the ONE place such a confirmation may
enter BEL, as a **closed allowlist** of Fact types — every entry below
is a rule *input*, never a rule *output*:

    ContractItem
    HistoricalAccrualFact
    CostRecognitionFact
    AccrualBasisFact
    InvoiceItemAllocation

Explicitly, permanently forbidden — Invoice/InvoiceItem/Payment (external
facts: they come from real source documents or not at all — permitting a
"confirmed" Payment would let a legacy 已付款 column re-enter as a Fact
under another name), Shipment (an export execution either has real
Evidence or is unresolved), and Accrual/AccrualReversal/InvoiceAllocation/
PaymentAllocation/SalesInvoiceAllocation/SalesPaymentAllocation/
ProcurementSalesLink/SalesContract (rule OUTPUTS and derived records — a
cutover fact may never express one). A pack naming ANY forbidden section
is rejected in full, atomically, before a single Fact is written.

Deliberate divergence from ``bel.application.import_close_facts`` (the
existing, separately-frozen Phase 2B Close Fact Pack, which this module
reuses resolution helpers from but is not layered on top of): importing
a ``historical_accrual_facts`` entry here creates ONLY the
``HistoricalAccrualFact`` row — never an ``Accrual``. Close Fact Pack's
eager Accrual creation is that importer's own established Phase 2B
behaviour; auto-creating an ``Accrual`` here would smuggle a forbidden
rule-output fact type in through an allowed one.

Every cutover Fact traces to Evidence carrying
``FragmentKind.MANUAL_FACT`` and the distinct ``EvidenceDocument.source_type``
``cutover_baseline_manual`` — never impersonating a bank statement,
invoice workbook, contract ledger, or shipment source. That source_type
is the machine-readable basis a future query uses to tell a
confirmed-at-cutover Fact from a genuinely evidenced one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from bel.application.contract_item_facts import create_contract_item_fact
from bel.application.import_close_facts import (
    _VALID_COST_RECOGNITION_BASIS,
    _ContractResolver,
    _d,
    _date,
    _datetime,
    _invoice_item_lookup,
    _selector_from,
)
from bel.application.item_allocation import validate_item_allocation
from bel.domain.accrual import (
    AccrualBasisFact,
    AccrualBasisScopeType,
    CostRecognitionFact,
    HistoricalAccrualFact,
    InvoiceItemAllocation,
    ItemAllocationConfirmationType,
    ManualBasis,
)
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    ContractItemRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
)

CUTOVER_SOURCE_TYPE = "cutover_baseline_manual"
PACK_VERSION = 1

ALLOWED_SECTIONS = (
    "contract_items",
    "historical_accrual_facts",
    "cost_recognition_facts",
    "accrual_basis_facts",
    "invoice_item_allocations",
)

# Named explicitly (not merely "anything not in ALLOWED_SECTIONS") so a
# reviewer can see the forbidden list is a deliberate enumeration, not an
# implicit default. Section 19 / Gate D's closed-allowlist requirement.
FORBIDDEN_SECTIONS = (
    "invoices",
    "invoice_items",
    "payments",
    "shipments",
    "accruals",
    "accrual_reversals",
    "invoice_allocations",
    "payment_allocations",
    "sales_invoice_allocations",
    "sales_payment_allocations",
    "procurement_sales_links",
    "sales_contracts",
)


class CutoverFactPackError(ValueError):
    """A rejected cutover fact pack — malformed, ambiguous selector, or
    a forbidden-section violation. Never a silent partial write."""


class CutoverFactPackForbidden(CutoverFactPackError):
    """The pack names a section outside the closed allowlist. Raised
    before ANY Evidence or Fact is written — atomic reject, zero
    facts (section 19/39's HARD requirement)."""


@dataclass
class CutoverFactPackResult:
    evidence_document_id: uuid.UUID
    file_name: str
    sha256: str
    is_reimport: bool
    contract_items_created: int = 0
    contract_items_skipped: int = 0
    historical_accrual_facts_created: int = 0
    historical_accrual_facts_skipped: int = 0
    cost_recognition_facts_created: int = 0
    cost_recognition_facts_skipped: int = 0
    accrual_basis_facts_created: int = 0
    accrual_basis_facts_skipped: int = 0
    invoice_item_allocations_created: int = 0
    invoice_item_allocations_skipped: int = 0
    source_periods: list[str] = field(default_factory=list)


def validate_cutover_fact_pack(pack: dict[str, Any]) -> None:
    """Reject the WHOLE pack if it names any section outside the closed
    allowlist — including every explicitly-forbidden type and any
    unrecognised key. Called before any write; the caller must never
    catch this and proceed with a filtered subset."""
    if not isinstance(pack, dict):
        raise CutoverFactPackError("cutover fact pack must be a JSON object")
    known = set(ALLOWED_SECTIONS) | {"version"}
    present = set(pack.keys())
    forbidden_present = present & set(FORBIDDEN_SECTIONS)
    unknown_present = present - known - set(FORBIDDEN_SECTIONS)
    if forbidden_present or unknown_present:
        raise CutoverFactPackForbidden(
            "cutover fact pack contains section(s) outside the closed allowlist "
            f"{sorted(ALLOWED_SECTIONS)}: forbidden={sorted(forbidden_present)}, "
            f"unrecognised={sorted(unknown_present)}"
        )
    for section in ALLOWED_SECTIONS:
        entries = pack.get(section, [])
        if not isinstance(entries, list):
            raise CutoverFactPackError(f"{section}: expected a list, got {type(entries).__name__}")


def import_cutover_fact_pack(
    session: Session, pack: dict[str, Any], *, file_name: str, created_at: datetime | None = None
) -> CutoverFactPackResult:
    """Import a validated Human-Confirmed Cutover Fact Pack. Idempotent
    on the pack's own JSON content (sha256), exactly like the Close Fact
    Pack — a byte-identical re-run creates zero new Evidence/Facts."""
    validate_cutover_fact_pack(pack)
    if pack.get("version") not in (None, PACK_VERSION):
        raise CutoverFactPackError(f"unsupported cutover fact pack version: {pack.get('version')!r}")

    now = created_at or datetime.now(timezone.utc)
    payload = json.dumps(pack, sort_keys=True, default=str).encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()

    evidence_repo = EvidenceRepository(session)
    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        return CutoverFactPackResult(
            evidence_document_id=existing_document.id, file_name=file_name, sha256=sha256, is_reimport=True
        )

    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=file_name, sha256=sha256, source_type=CUTOVER_SOURCE_TYPE, imported_at=now
    )
    evidence_repo.add_document(document)

    result = CutoverFactPackResult(
        evidence_document_id=document.id, file_name=file_name, sha256=sha256, is_reimport=False
    )

    entry_lists: dict[str, list[dict]] = {section: pack.get(section, []) for section in ALLOWED_SECTIONS}

    fragment_ids: dict[tuple[str, int], uuid.UUID] = {}
    for section in ALLOWED_SECTIONS:
        for index, entry in enumerate(entry_lists[section]):
            fragment = EvidenceFragment(
                id=uuid.uuid4(),
                evidence_document_id=document.id,
                fragment_kind=FragmentKind.MANUAL_FACT,
                sheet_name=None,
                row_number=None,
                locator_json={"section": section, "index": index, "cutover": True},
                raw_data=entry,
                created_at=now,
            )
            evidence_repo.add_fragment(fragment)
            fragment_ids[(section, index)] = fragment.id
    session.flush()

    resolver = _ContractResolver(session)
    invoice_item_ids = _invoice_item_lookup(session)
    item_repo = ContractItemRepository(session)

    # contract_items — routed through the SAME R1 write path as every
    # other ContractItem intake.
    contract_item_ids: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    for index, entry in enumerate(entry_lists["contract_items"]):
        fragment_id = fragment_ids[("contract_items", index)]
        contract = resolver.resolve(_selector_from(entry, "contract_items"), "contract_items")
        source_item_key = entry.get("source_item_key")
        if not source_item_key:
            raise CutoverFactPackError("contract_items: source_item_key is required")
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
                session, contract_id=contract.id, source_item_key=source_item_key, fields=fact_fields,
                source_fragment_id=fragment_id, created_at=now,
            )
        except ValueError as exc:
            raise CutoverFactPackError(f"contract_items: {exc}") from exc
        contract_item_ids[(contract.id, source_item_key)] = fact_result.item.id
        if fact_result.created:
            result.contract_items_created += 1
        else:
            result.contract_items_skipped += 1
    session.flush()

    def _item_id(contract, entry: dict, section: str) -> uuid.UUID:
        source_item_key = entry.get("source_item_key")
        if not source_item_key:
            raise CutoverFactPackError(f"{section}: source_item_key is required")
        if (contract.id, source_item_key) in contract_item_ids:
            return contract_item_ids[(contract.id, source_item_key)]
        existing = item_repo.find_by_contract_and_key(contract.id, source_item_key)
        if existing is None:
            raise CutoverFactPackError(
                f"{section}: contract {contract.contract_no!r} has no contract item with source_item_key "
                f"{source_item_key!r} — it must already exist or be present in this pack's contract_items section"
            )
        contract_item_ids[(contract.id, source_item_key)] = existing.id
        return existing.id

    # cost_recognition_facts
    cost_rec_repo = CostRecognitionFactRepository(session)
    for index, entry in enumerate(entry_lists["cost_recognition_facts"]):
        fragment_id = fragment_ids[("cost_recognition_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "cost_recognition_facts"), "cost_recognition_facts")
        basis = entry.get("basis")
        if basis not in _VALID_COST_RECOGNITION_BASIS:
            raise CutoverFactPackError(f"cost_recognition_facts: unsupported basis {basis!r}")
        recognition_date = _date(entry["recognition_date"], "cost_recognition_facts")
        if cost_rec_repo.find_duplicate(contract.id, recognition_date, basis) is not None:
            result.cost_recognition_facts_skipped += 1
            continue
        cost_rec_repo.add(
            CostRecognitionFact(
                id=uuid.uuid4(), contract_id=contract.id, recognition_date=recognition_date, basis=basis,
                source_fragment_id=fragment_id, created_at=now, shipment_id=None,
            )
        )
        result.cost_recognition_facts_created += 1

    # accrual_basis_facts
    basis_repo = AccrualBasisFactRepository(session)
    for index, entry in enumerate(entry_lists["accrual_basis_facts"]):
        fragment_id = fragment_ids[("accrual_basis_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "accrual_basis_facts"), "accrual_basis_facts")
        scope_type = entry.get("scope_type")
        if scope_type not in {AccrualBasisScopeType.CONTRACT, AccrualBasisScopeType.CONTRACT_ITEM}:
            raise CutoverFactPackError(f"accrual_basis_facts: unsupported scope_type {scope_type!r}")
        basis = entry.get("basis", ManualBasis.MANUAL_CONFIRMED)
        if basis != ManualBasis.MANUAL_CONFIRMED:
            raise CutoverFactPackError("accrual_basis_facts: only MANUAL_CONFIRMED is a valid basis")
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
                id=uuid.uuid4(), scope_type=scope_type, contract_id=contract.id, contract_item_id=contract_item_id,
                quantity=quantity, estimated_cost=estimated_cost, basis=basis, source_fragment_id=fragment_id,
                created_at=now,
            )
        )
        result.accrual_basis_facts_created += 1
    session.flush()

    # historical_accrual_facts — deliberately does NOT create an Accrual
    # (see module docstring). Only the Fact itself is a permitted
    # cutover-fact type.
    hist_repo = HistoricalAccrualFactRepository(session)
    for index, entry in enumerate(entry_lists["historical_accrual_facts"]):
        fragment_id = fragment_ids[("historical_accrual_facts", index)]
        contract = resolver.resolve(_selector_from(entry, "historical_accrual_facts"), "historical_accrual_facts")
        source_period = entry.get("source_period")
        if not source_period:
            raise CutoverFactPackError("historical_accrual_facts: source_period is required")
        contract_item_id = _item_id(contract, entry, "historical_accrual_facts")
        quantity = _d(entry["quantity"])
        estimated_cost = _d(entry["estimated_cost"])
        basis = entry.get("basis", ManualBasis.MANUAL_CONFIRMED)
        if basis != ManualBasis.MANUAL_CONFIRMED:
            raise CutoverFactPackError("historical_accrual_facts: only MANUAL_CONFIRMED is a valid basis")
        confirmed_at = _datetime(entry.get("confirmed_at"), "historical_accrual_facts") if entry.get("confirmed_at") else now

        existing_fact = hist_repo.find_duplicate(contract_item_id, source_period, quantity, estimated_cost, basis)
        if existing_fact is not None:
            result.historical_accrual_facts_skipped += 1
        else:
            hist_repo.add(
                HistoricalAccrualFact(
                    id=uuid.uuid4(), source_period=source_period, contract_item_id=contract_item_id,
                    quantity=quantity, estimated_cost=estimated_cost, basis=basis, source_fragment_id=fragment_id,
                    confirmed_at=confirmed_at,
                )
            )
            result.historical_accrual_facts_created += 1
        if source_period not in result.source_periods:
            result.source_periods.append(source_period)
    session.flush()

    # invoice_item_allocations
    alloc_repo = InvoiceItemAllocationRepository(session)
    for index, entry in enumerate(entry_lists["invoice_item_allocations"]):
        fragment_id = fragment_ids[("invoice_item_allocations", index)]
        contract = resolver.resolve(_selector_from(entry, "invoice_item_allocations"), "invoice_item_allocations")
        invoice_ref = entry.get("invoice")
        if not isinstance(invoice_ref, dict) or "external_key" not in invoice_ref or "line_no" not in invoice_ref:
            raise CutoverFactPackError("invoice_item_allocations: invoice.external_key and invoice.line_no are required")
        try:
            invoice_item_id = invoice_item_ids[(invoice_ref["external_key"], int(invoice_ref["line_no"]))]
        except KeyError as exc:
            raise CutoverFactPackError(
                f"invoice_item_allocations: no invoice item for {invoice_ref!r} — the invoice must already exist"
            ) from exc
        contract_item_id = _item_id(contract, entry, "invoice_item_allocations")
        allocated_quantity = _d(entry["allocated_quantity"])
        allocated_net_amount = _d(entry["allocated_net_amount"])

        existing = alloc_repo.find(invoice_item_id, contract_item_id)
        if existing is not None:
            result.invoice_item_allocations_skipped += 1
            continue

        invoice_item = InvoiceItemRepository(session).get(invoice_item_id)
        contract_item = item_repo.get(contract_item_id)
        if invoice_item is None or contract_item is None:
            raise CutoverFactPackError("invoice_item_allocations: referenced invoice item or contract item not found")
        try:
            validate_item_allocation(
                session=session, invoice_item=invoice_item, contract_item=contract_item,
                allocated_quantity=allocated_quantity, allocated_net_amount=allocated_net_amount,
            )
        except ValueError as exc:
            raise CutoverFactPackError(str(exc)) from exc

        alloc_repo.add(
            InvoiceItemAllocation(
                id=uuid.uuid4(), invoice_item_id=invoice_item_id, contract_item_id=contract_item_id,
                allocated_quantity=allocated_quantity, allocated_net_amount=allocated_net_amount,
                confirmation_type=ItemAllocationConfirmationType.MANUAL_CONFIRMED, source_fragment_id=fragment_id,
                created_at=now,
            )
        )
        result.invoice_item_allocations_created += 1
    session.flush()

    return result
