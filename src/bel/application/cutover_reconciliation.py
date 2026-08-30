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

Gate-fix (Phase 2D.1-R5 round 2) HARD invariants:

- No internal UUID ever stands in for a business identity. Every
  snapshot key/value is a genuine business identity field. A fact whose
  identity is incomplete, or a computed key that collides with another
  fact's, is NEVER silently keyed by its own row id and never silently
  overwrites the colliding entry — both become permanent, unconditional
  ``unresolved:``-prefixed entries that no baseline can pre-adjudicate.
- Every OPEN backfill-produced ``TaskException``
  (``BackfillIdentityIncomplete`` / ``BackfillIdentityAmbiguous`` /
  ``BackfillConflict``) enters the snapshot as one such unconditional
  ``unresolved:`` entry too — a backfill run that left ANY of these open
  can never pass reconciliation, whether or not it maps to a specific
  Contract.
- Decimal-equivalence normalization is applied ONLY to fields known to
  be genuinely Decimal-typed (``_DECIMAL_FIELD_NAMES``); every other
  field (business strings like a contract/sales-contract number) always
  compares as an exact string — ``"00123"`` and ``"123"`` are NEVER
  treated as equal.

Gate-fix (Phase 2D.1-R5 round 2) M:N duplicate observation: the R4
primary axis emits ONE row per PROCUREMENT Contract and nests every
linked sales scope inside it (docs/V1-SCOPE.md section 5 item 1), so one
SalesContract legitimately linked to N procurement Contracts projects its
SAME scope-level facts N times — identical business key AND identical
fact content. That is a projection artifact, never a duplicate business
identity: those keys are collapsed to ONE observation here
(``_MANY_TO_ONE_DEDUPE_PREFIXES``), so a legal many-to-one bridge never
misfires ``duplicate_identity``. Two things are deliberately NOT
deduped, so a genuine collision still fails the Gate unconditionally:

- the same key carrying DIFFERENT content anywhere (never a silent
  "first one wins" overwrite);
- every procurement-axis namespace (contract / contract_item /
  shipment / procurement invoice / outgoing payment / accrual) — two
  Contracts sharing a business key is a real identity collision even
  when their content happens to agree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from bel.application.contract_business_ledger import get_contract_business_ledger
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.infrastructure.persistence.repositories import ExceptionRepository

OUTCOME_MATCH = "MATCH"
OUTCOME_BEL_CORRECTED_LEGACY = "BEL_CORRECTED_LEGACY"
OUTCOME_UNRESOLVED = "UNRESOLVED"

_RECORDABLE_BASELINE_OUTCOMES = (OUTCOME_MATCH, OUTCOME_BEL_CORRECTED_LEGACY)

_UNRESOLVED_PREFIX = "unresolved:"

# The SALES-SCOPE-level namespaces only. Each is keyed by the
# SalesContract's own business identity alone (never by the procurement
# Contract it was reached through), so the R4 primary axis legitimately
# emits the very same (key, content) pair once per linked procurement
# Contract — see the module docstring. Everything else (the procurement
# axis) keeps the unconditional duplicate_identity rule.
_MANY_TO_ONE_DEDUPE_PREFIXES = (
    "sales_contract:",
    "sales_invoice_allocation:",
    "incoming_receipt_allocation:",
)

# R5 backfill's own unresolved-work producers (docs/ROADMAP.md 2D.1-R5
# gate fix) — the ONLY exception types this module treats as
# reconciliation-blocking backfill tasks. Pre-existing exception types
# (BusinessKeyConflict, AllocationCapacityExceeded, ...) are out of this
# round's scope and are left alone.
_BACKFILL_EXCEPTION_TYPES = (
    ExceptionType.BACKFILL_IDENTITY_INCOMPLETE,
    ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS,
    ExceptionType.BACKFILL_CONFLICT,
)

# Only these field NAMES are genuinely Decimal-typed business values —
# every other field (business strings: contract numbers, references,
# product names, customer names, ...) compares as an exact string,
# never coerced through Decimal. A field name is unambiguous across
# this module's whole snapshot shape (never reused with a different
# meaning), so a flat set checked at any nesting depth is correct.
_DECIMAL_FIELD_NAMES = {
    "gross_amount",
    "net_amount",
    "quantity",
    "unit_price",
    "allocated_gross_amount",
    "allocated_amount",
    "allocated_quantity",
    "remaining_quantity",
    "remaining_estimated_cost",
    "estimated_cost",
}


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


def _normalize_decimal(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return value


def _normalize_value(field_name: str | None, value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalize_value(k, v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return sorted((_normalize_value(field_name, v) for v in value), key=repr)
    if field_name in _DECIMAL_FIELD_NAMES:
        return _normalize_decimal(value)
    # Every non-Decimal field — including a numeric-LOOKING business
    # string like a contract number — compares as an exact value, never
    # coerced. "00123" and "123" are deliberately NOT normalized equal.
    return value


def _normalize(value: Any) -> Any:
    return _normalize_value(None, value)


def _d(value) -> str | None:
    return str(value) if value is not None else None


def _payment_identity_key(payment) -> str | None:
    """The full R5 Payment business identity
    (docs/PHASE2D1-R0-DECISIONS.md section 4.4):
    ``(source_account_id, transaction_date, direction, amount, bank_reference)``.
    Returns None — never a partial/UUID substitute — when any part is
    missing."""
    if payment is None:
        return None
    if not payment.source_account_id or not payment.bank_reference:
        return None
    return (
        f"source_account_id={payment.source_account_id}|transaction_date={payment.transaction_date.isoformat()}"
        f"|direction={payment.direction}|amount={_d(payment.amount)}|bank_reference={payment.bank_reference}"
    )


class _SnapshotBuilder:
    """Collects (key, value) pairs first and resolves duplicates in a
    second pass — a silent dict-assignment can never let a later
    colliding key overwrite an earlier one. Every entry with an
    incomplete business identity is routed straight to an unconditional
    ``unresolved:`` marker and never attempts the normal key at all.

    A repeated key is collapsed ONLY when it is one of the
    sales-scope-level namespaces AND every one of its occurrences
    carries identical fact content (``_dedupe_eligible_keys``) — the
    legal many-to-one bridge's projection artifact. Any other repeat,
    and any content disagreement, stays an unconditional
    ``duplicate_identity`` entry."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, dict[str, Any]]] = []
        self._unresolved_ordinal = 0

    def add(self, key: str, value: dict[str, Any]) -> None:
        self._entries.append((key, value))

    def add_unresolved(self, category: str, reason: str, **context: Any) -> None:
        self._unresolved_ordinal += 1
        key = f"{_UNRESOLVED_PREFIX}{category}:{self._unresolved_ordinal}"
        self._entries.append((key, {"reason": reason, **context}))

    def _dedupe_eligible_keys(self) -> set[str]:
        """Repeated sales-scope-level keys whose EVERY occurrence carries
        identical fact content — the same fact observed once per linked
        procurement Contract, never two different facts under one
        business identity. Compared NORMALIZED (so Decimal formatting
        differences do not manufacture a collision) and with ``==``
        (never ``repr``, so ``Decimal("5.0")`` and ``Decimal("5.00")``
        still compare equal)."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for key, value in self._entries:
            grouped.setdefault(key, []).append(value)
        eligible: set[str] = set()
        for key, values in grouped.items():
            if len(values) < 2 or not key.startswith(_MANY_TO_ONE_DEDUPE_PREFIXES):
                continue
            first = _normalize(values[0])
            if all(_normalize(v) == first for v in values[1:]):
                eligible.add(key)
        return eligible

    def build(self) -> dict[str, dict[str, Any]]:
        counts = Counter(k for k, _ in self._entries)
        dedupe_eligible = self._dedupe_eligible_keys()
        snapshot: dict[str, dict[str, Any]] = {}
        collision_ordinal = 0
        for key, value in self._entries:
            if key.startswith(_UNRESOLVED_PREFIX):
                snapshot[key] = value
                continue
            if counts[key] > 1 and key not in dedupe_eligible:
                collision_ordinal += 1
                # Duplicate business identity — never a silent dict
                # overwrite. BOTH/ALL colliding occurrences become their
                # own permanent, unconditional unresolved entries.
                snapshot[f"{_UNRESOLVED_PREFIX}duplicate_identity:{collision_ordinal}"] = {
                    "reason": "duplicate_business_identity", "key": key,
                }
            else:
                snapshot[key] = value
        return snapshot


def build_contract_execution_snapshot(session: Session) -> dict[str, dict[str, Any]]:
    """Normalized, business-identity-keyed snapshot of the
    contract-execution Fact layer — built entirely from the SAME R4
    Ledger projection the page/export use, PLUS every OPEN R5
    backfill-produced Task (never omitted just because it cannot be
    mapped to a specific Contract). Every key is a business identity
    string or an unconditional ``unresolved:`` marker — never an
    internal UUID standing in for one."""
    ledger = get_contract_business_ledger(session)
    builder = _SnapshotBuilder()

    for row in ledger.rows:
        contract = row.contract
        contract_key = f"contract_no={contract.contract_no}|counterparty={contract.counterparty}"

        builder.add(
            f"contract:{contract_key}",
            {
                "contract_type": contract.contract_type,
                "buyer": contract.buyer,
                "gross_amount": _d(contract.gross_amount),
                "currency": contract.currency,
                "contract_date": contract.contract_date.isoformat() if contract.contract_date else None,
            },
        )

        item_source_key_by_id = {item.id: item.source_item_key for item in row.items}

        for item in row.items:
            if not item.source_item_key:
                builder.add_unresolved(
                    "contract_item_identity", "missing_source_item_key", contract=contract_key,
                )
                continue
            builder.add(
                f"contract_item:{contract_key}|source_item_key={item.source_item_key}",
                {
                    "sku": item.sku, "product_name": item.product_name, "specification": item.specification,
                    "quantity": _d(item.quantity), "unit": item.unit, "unit_price": _d(item.unit_price),
                    "gross_amount": _d(item.gross_amount), "net_amount": _d(item.net_amount),
                },
            )

        for entry in row.shipments:
            s = entry.shipment
            if not s.external_reference:
                builder.add_unresolved(
                    "shipment_identity", "missing_external_reference", contract=contract_key,
                    execution_date=s.execution_date.isoformat(),
                )
                continue
            builder.add(
                f"shipment:{contract_key}|external_reference={s.external_reference}|execution_date={s.execution_date.isoformat()}",
                {"quantity": _d(s.quantity), "contract_item_id_known": s.contract_item_id is not None},
            )

        for entry in row.procurement_invoices:
            invoice = entry.invoice
            if invoice is None or not invoice.external_invoice_key:
                builder.add_unresolved(
                    "procurement_invoice_identity", "missing_external_invoice_key", contract=contract_key,
                )
                continue
            builder.add(
                f"procurement_invoice_allocation:{contract_key}|invoice={invoice.external_invoice_key}",
                {"allocated_gross_amount": _d(entry.allocation.allocated_gross_amount)},
            )

        for entry in row.outgoing_payments:
            payment_key = _payment_identity_key(entry.payment)
            if payment_key is None:
                builder.add_unresolved(
                    "outgoing_payment_identity", "incomplete_payment_identity", contract=contract_key,
                )
                continue
            builder.add(
                f"outgoing_payment_allocation:{contract_key}|payment={payment_key}",
                {"allocated_amount": _d(entry.allocation.allocated_amount)},
            )

        for entry in row.accruals:
            source_item_key = item_source_key_by_id.get(entry.contract_item_id)
            if not source_item_key:
                builder.add_unresolved(
                    "accrual_identity", "missing_contract_item_identity", contract=contract_key,
                    period=entry.accrual.period,
                )
                continue
            builder.add(
                f"accrual:{contract_key}|source_item_key={source_item_key}|period={entry.accrual.period}",
                {
                    "remaining_quantity": _d(entry.remaining_quantity),
                    "remaining_estimated_cost": _d(entry.remaining_estimated_cost),
                    "current_status": entry.projected_status,
                },
            )

        for scope in row.sales_scopes:
            sc = scope.sales_contract
            sales_key = f"our_entity={sc.our_entity}|sales_contract_no={sc.sales_contract_no}"
            builder.add(
                f"sales_contract:{sales_key}",
                {
                    "customer": sc.customer, "currency": sc.currency, "gross_amount": _d(sc.gross_amount),
                    "contract_date": sc.contract_date.isoformat() if sc.contract_date else None,
                },
            )
            builder.add(f"procurement_sales_link:{contract_key}|{sales_key}", {"current": True})

            for a in scope.sales_invoice_allocations:
                invoice = a.invoice
                if invoice is None or not invoice.external_invoice_key:
                    builder.add_unresolved(
                        "sales_invoice_identity", "missing_external_invoice_key", sales_contract=sales_key,
                    )
                    continue
                builder.add(
                    f"sales_invoice_allocation:{sales_key}|invoice={invoice.external_invoice_key}",
                    {"allocated_gross_amount": _d(a.allocation.allocated_gross_amount)},
                )
            for a in scope.incoming_receipt_allocations:
                payment_key = _payment_identity_key(a.payment)
                if payment_key is None:
                    builder.add_unresolved(
                        "incoming_receipt_identity", "incomplete_payment_identity", sales_contract=sales_key,
                    )
                    continue
                builder.add(
                    f"incoming_receipt_allocation:{sales_key}|payment={payment_key}",
                    {"allocated_amount": _d(a.allocation.allocated_amount)},
                )

        builder.add(f"unresolved_indicator:{contract_key}", {"has_unresolved": row.has_unresolved})

    for exc in ExceptionRepository(session).list_all():
        if exc.status != ExceptionStatus.OPEN:
            continue
        if exc.exception_type not in _BACKFILL_EXCEPTION_TYPES:
            continue
        # An OPEN backfill Task ALWAYS blocks reconciliation — whether or
        # not it maps to a specific Contract (spec gate-fix section 1).
        # Keyed by the task's own persisted id: this is the task's OWN
        # canonical identity, not a stand-in for a missing business
        # identity — the whole reason the Task exists is that no clean
        # business identity could be established.
        builder.add_unresolved(
            "backfill_task", "open_backfill_task", exception_type=exc.exception_type, task_id=str(exc.id),
            identity_key=exc.detail.get("identity_key"),
        )

    return builder.build()


def reconcile(session: Session, baseline: dict[str, Any]) -> ReconciliationResult:
    """Compare the current contract-execution snapshot against an
    independently-supplied Cutover Baseline.

    ``baseline`` shape: ``{"entries": [{"key": "...", "expected": {...},
    "outcome": "MATCH" | "BEL_CORRECTED_LEGACY"}, ...]}``. A baseline
    entry naming any other outcome (including a literal "UNRESOLVED")
    is itself an unadjudicated discrepancy and counts as UNRESOLVED.

    Any snapshot key beginning with ``unresolved:`` (an incomplete/
    duplicate business identity, or an OPEN backfill Task) is ALWAYS
    UNRESOLVED — no baseline entry can pre-adjudicate one away.

    Out-of-scope keys (section 34) never enter ``build_contract_execution_snapshot``
    in the first place, so there is nothing to special-case here — the
    snapshot's own key set already IS the in-scope set."""
    actual = build_contract_execution_snapshot(session)
    baseline_entries = {e["key"]: e for e in baseline.get("entries", [])}

    results: list[ReconciliationEntry] = []
    unresolved = 0

    resolvable_actual_keys: set[str] = set()
    for key in actual:
        if key.startswith(_UNRESOLVED_PREFIX):
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=None))
            unresolved += 1
        else:
            resolvable_actual_keys.add(key)

    for key, entry in baseline_entries.items():
        if key.startswith(_UNRESOLVED_PREFIX):
            continue  # never a baseline-adjudicatable key
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

    for key in resolvable_actual_keys:
        if key not in baseline_entries:
            # BEL holds an in-scope Fact the baseline never adjudicated.
            results.append(ReconciliationEntry(key=key, outcome=OUTCOME_UNRESOLVED, baseline_outcome=None))
            unresolved += 1

    return ReconciliationResult(entries=tuple(results), unresolved_count=unresolved, passed=unresolved == 0)
