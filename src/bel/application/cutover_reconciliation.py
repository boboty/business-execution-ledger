"""R5 Cutover Reconciliation rehearsal (Phase 2D.1-R5,
docs/PHASE2D1-R0-DECISIONS.md section 4.6, docs/V1-SCOPE.md section 7).

Rehearses reconciliation against the CONTRACT-EXECUTION FACT LAYER only
— 2D.2/2D.3/2D.4 do not exist yet, so this must not pretend they do
(no Period Close projected Decision, no outbound invoice eligibility, no
Exception Center resolution state enters this comparison).

Observation seam: ``bel.application.contract_business_ledger.get_contract_business_ledger``
— the SAME current-fact projection the R4 Ledger page/export use, never
a second query composition. This module's only job is to NORMALIZE that
projection into business-identity-keyed form (never internal UUIDs,
never ``created_at``, never insertion order) and compare it against a
**Cutover Baseline**: independently-supplied expected material, never
derived from BEL's own output (never ``read BEL result -> write
expected`` — that would make acceptance circular).

Closed outcome set, exactly three: ``MATCH``, ``BEL_CORRECTED_LEGACY``,
``UNRESOLVED``. ``UNRESOLVED = 0`` means every discrepancy has been
adjudicated — not that BEL agrees with the legacy spreadsheet
everywhere. Any ``UNRESOLVED`` entry fails the whole reconciliation run.

No fuzzy tolerance: Decimal values are compared as exact canonical
Decimal (``100`` and ``100.00`` normalize equal; a real difference of
any size does not). Collections are compared order-insensitively but
via a deterministic normalized form, never database insertion order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from bel.application.contract_business_ledger import get_contract_business_ledger

OUTCOME_MATCH = "MATCH"
OUTCOME_BEL_CORRECTED_LEGACY = "BEL_CORRECTED_LEGACY"
OUTCOME_UNRESOLVED = "UNRESOLVED"

_RECORDABLE_BASELINE_OUTCOMES = (OUTCOME_MATCH, OUTCOME_BEL_CORRECTED_LEGACY)


@dataclass(frozen=True)
class ReconciliationEntry:
    key: str
    outcome: str
    baseline_outcome: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    entries: tuple[ReconciliationEntry, ...]
    unresolved_count: int
    passed: bool


def _normalize_scalar(value: Any) -> Any:
    """Decimal-equivalence normalization only — never a fuzzy tolerance.
    A numeric string/int/float is compared as its canonical Decimal;
    everything else compares by value equality after that."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return value
    return value


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        # Deterministic normalized order — collections compare
        # order-insensitively, never by database/insertion order.
        return sorted((_normalize(v) for v in value), key=repr)
    return _normalize_scalar(value)


def _d(value) -> str | None:
    return str(value) if value is not None else None


def build_contract_execution_snapshot(session: Session) -> dict[str, dict[str, Any]]:
    """Normalized, business-identity-keyed snapshot of the
    contract-execution Fact layer — built entirely from the SAME R4
    Ledger projection the page/export use. Every key is a business
    identity string, never an internal UUID; every value is a plain
    JSON-safe dict."""
    ledger = get_contract_business_ledger(session)
    snapshot: dict[str, dict[str, Any]] = {}

    for row in ledger.rows:
        contract = row.contract
        contract_key = f"contract_no={contract.contract_no}|counterparty={contract.counterparty}"

        snapshot[f"contract:{contract_key}"] = {
            "contract_type": contract.contract_type,
            "buyer": contract.buyer,
            "gross_amount": _d(contract.gross_amount),
            "currency": contract.currency,
            "contract_date": contract.contract_date.isoformat() if contract.contract_date else None,
        }

        for item in row.items:
            snapshot[f"contract_item:{contract_key}|source_item_key={item.source_item_key}"] = {
                "sku": item.sku, "product_name": item.product_name, "specification": item.specification,
                "quantity": _d(item.quantity), "unit": item.unit, "unit_price": _d(item.unit_price),
                "gross_amount": _d(item.gross_amount), "net_amount": _d(item.net_amount),
            }

        for entry in row.shipments:
            s = entry.shipment
            snapshot[
                f"shipment:{contract_key}|external_reference={s.external_reference}|execution_date={s.execution_date.isoformat()}"
            ] = {"quantity": _d(s.quantity), "contract_item_id_known": s.contract_item_id is not None}

        for entry in row.procurement_invoices:
            invoice = entry.invoice
            key = invoice.external_invoice_key if invoice else str(entry.allocation.invoice_id)
            snapshot[f"procurement_invoice_allocation:{contract_key}|invoice={key}"] = {
                "allocated_gross_amount": _d(entry.allocation.allocated_gross_amount),
            }

        for entry in row.outgoing_payments:
            payment = entry.payment
            key = payment.bank_reference if payment and payment.bank_reference else str(entry.allocation.payment_id)
            snapshot[f"outgoing_payment_allocation:{contract_key}|payment={key}"] = {
                "allocated_amount": _d(entry.allocation.allocated_amount),
            }

        for entry in row.accruals:
            snapshot[f"accrual:{contract_key}|item_id={entry.contract_item_id}|period={entry.accrual.period}"] = {
                "remaining_quantity": _d(entry.remaining_quantity),
                "remaining_estimated_cost": _d(entry.remaining_estimated_cost),
                "current_status": entry.projected_status,
            }

        for scope in row.sales_scopes:
            sc = scope.sales_contract
            sales_key = f"our_entity={sc.our_entity}|sales_contract_no={sc.sales_contract_no}"
            snapshot[f"sales_contract:{sales_key}"] = {
                "customer": sc.customer, "currency": sc.currency, "gross_amount": _d(sc.gross_amount),
                "contract_date": sc.contract_date.isoformat() if sc.contract_date else None,
            }
            snapshot[f"procurement_sales_link:{contract_key}|{sales_key}"] = {"current": True}

            for a in scope.sales_invoice_allocations:
                invoice_key = a.invoice.external_invoice_key if a.invoice else str(a.allocation.invoice_id)
                snapshot[f"sales_invoice_allocation:{sales_key}|invoice={invoice_key}"] = {
                    "allocated_gross_amount": _d(a.allocation.allocated_gross_amount),
                }
            for a in scope.incoming_receipt_allocations:
                payment_key = a.payment.bank_reference if a.payment and a.payment.bank_reference else str(a.allocation.payment_id)
                snapshot[f"incoming_receipt_allocation:{sales_key}|payment={payment_key}"] = {
                    "allocated_amount": _d(a.allocation.allocated_amount),
                }

        snapshot[f"unresolved_indicator:{contract_key}"] = {"has_unresolved": row.has_unresolved}

    return snapshot


def reconcile(session: Session, baseline: dict[str, Any]) -> ReconciliationResult:
    """Compare the current contract-execution snapshot against an
    independently-supplied Cutover Baseline.

    ``baseline`` shape: ``{"entries": [{"key": "...", "expected": {...},
    "outcome": "MATCH" | "BEL_CORRECTED_LEGACY"}, ...]}``. A baseline
    entry naming any other outcome (including a literal "UNRESOLVED")
    is itself an unadjudicated discrepancy and counts as UNRESOLVED.

    Out-of-scope keys (section 34) never enter ``build_contract_execution_snapshot``
    in the first place, so there is nothing to special-case here — the
    snapshot's own key set already IS the in-scope set."""
    actual = build_contract_execution_snapshot(session)
    baseline_entries = {e["key"]: e for e in baseline.get("entries", [])}

    results: list[ReconciliationEntry] = []
    unresolved = 0

    for key, entry in baseline_entries.items():
        recorded_outcome = entry.get("outcome")
        actual_value = actual.get(key)
        if recorded_outcome not in _RECORDABLE_BASELINE_OUTCOMES:
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=recorded_outcome))
            unresolved += 1
            continue
        if actual_value is None:
            # expected an in-scope Fact that BEL's current state does not have.
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=recorded_outcome))
            unresolved += 1
            continue
        if _normalize(actual_value) == _normalize(entry.get("expected")):
            results.append(ReconciliationEntry(key=key, outcome=recorded_outcome, baseline_outcome=recorded_outcome))
        else:
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=recorded_outcome))
            unresolved += 1

    for key in actual:
        if key not in baseline_entries:
            # BEL holds an in-scope Fact the baseline never adjudicated.
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=None))
            unresolved += 1

    return ReconciliationResult(entries=tuple(results), unresolved_count=unresolved, passed=unresolved == 0)
