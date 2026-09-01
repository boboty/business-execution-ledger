"""Exception & Task Center — global unresolved-work read projection
(Phase 2D.4-F1, docs/PHASE2D4-DECISIONS.md).

    authoritative unresolved sources
        -> one neutral global read projection
        -> filters
        -> read-only Web Center

One application-level aggregation over the full frozen inventory:

    TASK_EXCEPTION          persisted TaskException rows (the produced set)
    MATCH_CASE              persisted MatchCase in HUMAN_CONFIRMATION_REQUIRED
    COMPUTED_BLOCKER        Period Close blockers recomputed for a requested
                            period — ZERO items when no period is supplied

This is deliberately NOT ``_collect_unresolved_work``
(``contract_business_ledger.py``), which is contract-scoped by construction
and drops genuinely unmappable items. F1 preserves unmappable work globally
(``docs/PHASE2D4-DECISIONS.md`` §4): an item with no Contract anchor still
appears. It reuses the structured-scope-resolution discipline of that
function — scope is resolved ONLY from structured ``detail`` keys and
explicit repository lookups by id, never by parsing ``summary`` text.

F1 is strictly read-only: the whole projection runs under
``session.no_autoflush`` and never writes business state. No generic
RESOLVE exists here, nothing is persisted for computed blockers, and the
source semantics of the three classes are never flattened into one storage
object.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from bel.application.period_close import (
    ITEM_MATCH_REQUIRED_FOR_REVERSAL,
    MISSING_ACCRUAL_BASIS,
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE,
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE,
    build_period_close_preview,
)
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.domain.matching import MatchCaseStatus, SubjectType
from bel.infrastructure.persistence.repositories import (
    ContractItemRepository,
    ExceptionRepository,
    MatchCandidateRepository,
    MatchCaseRepository,
    ProcurementSalesLinkRepository,
    SalesMatchCandidateRepository,
    ShipmentRepository,
)

# ---------------------------------------------------------------------------
# Frozen source taxonomy and resolution vocabulary
# ---------------------------------------------------------------------------


class SourceType:
    TASK_EXCEPTION = "TASK_EXCEPTION"
    MATCH_CASE = "MATCH_CASE"
    COMPUTED_BLOCKER = "COMPUTED_BLOCKER"


class ScopeType:
    """The structured business scopes a Center item may trace to. These are
    trace/navigation data, never the item's identity (identity is
    ``(source_type, source_id)`` — docs/PHASE2D4-DECISIONS.md §4)."""

    PROCUREMENT_CONTRACT = "PROCUREMENT_CONTRACT"
    SALES_CONTRACT = "SALES_CONTRACT"
    CONTRACT_ITEM = "CONTRACT_ITEM"
    SHIPMENT = "SHIPMENT"


class ResolutionRoute:
    """Frozen presentation vocabulary — where the underlying issue is
    corrected/confirmed. Navigation guidance only; F1 executes nothing.
    ``REVIEW_ONLY`` is the default, not an error
    (docs/PHASE2D4-DECISIONS.md §6)."""

    CONFIRM_MATCH = "CONFIRM_MATCH"
    CONFIRM_RELATIONSHIP = "CONFIRM_RELATIONSHIP"
    SUPPLY_FACT = "SUPPLY_FACT"
    REVIEW_ONLY = "REVIEW_ONLY"


class ComputedBlockerStatus:
    """The constant status of every COMPUTED_BLOCKER item — "present for the
    requested period". There is no lifecycle; created_at stays None."""

    PRESENT = "PRESENT"


COMPUTED_BLOCKER_PROVENANCE = "bel.application.period_close"

# Deterministic one-line presentation text for each computed blocker type.
# Existence/type of a blocker is decided exclusively by period_close.py.
COMPUTED_BLOCKER_SUMMARIES: dict[str, str] = {
    ITEM_MATCH_REQUIRED_FOR_REVERSAL: "发票已确认到本合同，但尚未确认对应哪一项合同商品",
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE: "同一商品存在多笔未结暂估，无法判断此次到票归属哪一笔",
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE: "存在多条可用发票明细，无法判断实际成本来源",
    MISSING_ACCRUAL_BASIS: "已满足成本确认条件，但缺少可确认的暂估成本依据",
}

# Producer module provenance for TaskException types. BUSINESS_KEY_CONFLICT is
# deliberately None: it is produced by two different modules
# (import_contract_ledger and sales_contract_facts — §1A) and the persisted
# row does not store which; provenance is only set where already available.
_EXCEPTION_PRODUCERS: dict[str, str] = {
    ExceptionType.ALLOCATION_CAPACITY_EXCEEDED: "bel.application.matching",
    ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED: "bel.application.contract_item_facts",
    ExceptionType.SHIPMENT_FACT_SUPERSEDED: "bel.application.shipment_facts",
    ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE: "bel.application.shipment_facts",
    ExceptionType.SHIPMENT_IDENTITY_CONFLICT: "bel.application.shipment_facts",
    ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE: "bel.application.sales_contract_facts",
    ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED: "bel.application.sales_contract_facts",
    ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED: "bel.application.procurement_sales_link",
    ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES: "bel.application.procurement_sales_link",
    ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT: "bel.application.procurement_sales_link",
    ExceptionType.BACKFILL_IDENTITY_INCOMPLETE: "bel.application.cutover_backfill",
    ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS: "bel.application.cutover_backfill",
    ExceptionType.BACKFILL_CONFLICT: "bel.application.cutover_backfill",
}

# Resolution-route mapping (frozen §6 / task §10).
_EXCEPTION_ROUTES: dict[str, str] = {
    ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED: ResolutionRoute.CONFIRM_RELATIONSHIP,
    ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE: ResolutionRoute.SUPPLY_FACT,
}


# ---------------------------------------------------------------------------
# Neutral DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnresolvedWorkScope:
    """One repeatable structured business scope reference. Scope ids are
    trace/navigation data — never the unresolved-work item's identity."""

    scope_type: str
    scope_id: uuid.UUID


@dataclass(frozen=True)
class UnresolvedWorkItem:
    """The frozen neutral presentation DTO
    (docs/PHASE2D4-DECISIONS.md §3). One item per authoritative source;
    a source tracing to several candidate scopes keeps ONE row and carries
    them all in ``scopes`` — it is never duplicated into several Center
    rows. Identity is exactly ``(source_type, source_id)``.

    Every field that a source has no value for stays explicitly ``None``
    (or empty tuple) — nothing is guessed and nothing is defaulted.
    ``source_id`` is the persisted object's own UUID for
    TASK_EXCEPTION/MATCH_CASE, and a stable deterministic key for
    COMPUTED_BLOCKER (no persisted object exists)."""

    source_type: str
    source_id: uuid.UUID | str
    code: str
    status: str
    summary: str
    created_at: datetime | None

    scopes: tuple[UnresolvedWorkScope, ...] = ()

    procurement_contract_id: uuid.UUID | None = None
    sales_contract_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    payment_id: uuid.UUID | None = None
    shipment_id: uuid.UUID | None = None
    match_case_id: uuid.UUID | None = None

    resolution_route: str = ResolutionRoute.REVIEW_ONLY
    provenance: str | None = None


@dataclass(frozen=True)
class UnresolvedWorkFilters:
    """Frozen minimal F1 filters (§12). No priority/assignee/SLA/due date/
    department/workflow state — no existing Fact supports them.

    Semantics:
      ``status``      exact status on the neutral projection (item.status).
      ``open_only``   TaskException-only convenience: True (default) shows
                      OPEN tasks; False shows every task status. Ignored when
                      ``status`` is set (the explicit status wins).
      ``source_type`` one of SourceType.
      ``code``        exact machine code (exception_type / match_method /
                      blocker_type).
      ``procurement_contract_id`` / ``sales_contract_id``
                      match an item carrying that structured scope.
      ``period``      YYYY-MM; when supplied, computes that period's
                      blockers. Invalid values are an explicit validation
                      error (ValueError), never silently ignored."""

    status: str | None = None
    open_only: bool | None = None
    source_type: str | None = None
    code: str | None = None
    procurement_contract_id: uuid.UUID | None = None
    sales_contract_id: uuid.UUID | None = None
    period: str | None = None


@dataclass(frozen=True)
class UnresolvedWorkCenter:
    """Result of the one Application projection. ``counts`` reflects the
    filtered/sorted items the caller sees (``total`` plus one per source
    type)."""

    items: tuple[UnresolvedWorkItem, ...]
    filters: UnresolvedWorkFilters
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# Period validation (same convention as Period Close)
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def validate_period(period: str) -> None:
    """Raise ValueError unless *period* is a valid YYYY-MM (month 1–12).
    The exact same convention Period Close uses for its own period."""
    if not _PERIOD_RE.match(period):
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    year, month = int(period[:4]), int(period[5:7])
    if month < 1 or month > 12:
        raise ValueError(f"period must be YYYY-MM, got {period!r}")


def _uuid_or_none(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _first_or_none(scopes: list[UnresolvedWorkScope], scope_type: str) -> uuid.UUID | None:
    for scope in scopes:
        if scope.scope_type == scope_type:
            return scope.scope_id
    return None


def _ordered_scopes(scopes: list[UnresolvedWorkScope]) -> tuple[UnresolvedWorkScope, ...]:
    """Deterministic scope ordering — by type then id — so the projection is
    stable across runs regardless of producer ``detail`` key order."""
    return tuple(sorted(scopes, key=lambda s: (s.scope_type, str(s.scope_id))))


def _add(scopes: list[UnresolvedWorkScope], scope_type: str, scope_id: uuid.UUID | None) -> None:
    if scope_id is not None:
        scopes.append(UnresolvedWorkScope(scope_type=scope_type, scope_id=scope_id))


# ---------------------------------------------------------------------------
# TASK_EXCEPTION aggregation
# ---------------------------------------------------------------------------


def _task_exception_scopes(
    exc,
    item_repo: ContractItemRepository,
    shipment_repo: ShipmentRepository,
    link_repo: ProcurementSalesLinkRepository,
) -> tuple[UnresolvedWorkScope, ...]:
    """Resolve a persisted TaskException's business scopes from its
    STRUCTURED ``detail`` fields and repository lookup only. ``summary`` is
    never parsed for identity/scope. A type with no resolvable anchor (e.g.
    SALES_CONTRACT_IDENTITY_INCOMPLETE, the backfill types) returns no
    scopes — the item still appears globally; it is never dropped."""
    detail = exc.detail or {}
    scopes: list[UnresolvedWorkScope] = []

    if exc.exception_type == ExceptionType.BUSINESS_KEY_CONFLICT:
        # procurement contract_ids where a structured list exists, else a
        # sales_contract_id.
        contract_ids = detail.get("contract_ids")
        if isinstance(contract_ids, list):
            for raw_id in contract_ids:
                _add(scopes, ScopeType.PROCUREMENT_CONTRACT, _uuid_or_none(raw_id))
        else:
            _add(scopes, ScopeType.SALES_CONTRACT, _uuid_or_none(detail.get("sales_contract_id")))

    elif exc.exception_type == ExceptionType.ALLOCATION_CAPACITY_EXCEEDED:
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, _uuid_or_none(detail.get("contract_id")))

    elif exc.exception_type == ExceptionType.CONTRACT_ITEM_FACT_SUPERSEDED:
        item_id = _uuid_or_none(detail.get("contract_item_id"))
        item = item_repo.get(item_id) if item_id is not None else None
        # The structured id stays as trace even when the object is missing;
        # only the derived contract scope falls away — the task is preserved.
        _add(scopes, ScopeType.CONTRACT_ITEM, item_id)
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, item.contract_id if item is not None else None)

    elif exc.exception_type == ExceptionType.SHIPMENT_FACT_SUPERSEDED:
        shipment_id = _uuid_or_none(detail.get("shipment_id"))
        shipment = shipment_repo.get(shipment_id) if shipment_id is not None else None
        _add(scopes, ScopeType.SHIPMENT, shipment_id)
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, shipment.contract_id if shipment is not None else None)

    elif exc.exception_type == ExceptionType.SHIPMENT_IDENTITY_INCOMPLETE:
        # procurement contract scope only — no Shipment anchor exists.
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, _uuid_or_none(detail.get("contract_id")))

    elif exc.exception_type == ExceptionType.SHIPMENT_IDENTITY_CONFLICT:
        shipment_id = _uuid_or_none(detail.get("shipment_id"))
        shipment = shipment_repo.get(shipment_id) if shipment_id is not None else None
        _add(scopes, ScopeType.SHIPMENT, shipment_id)
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, shipment.contract_id if shipment is not None else None)

    elif exc.exception_type == ExceptionType.SALES_CONTRACT_IDENTITY_INCOMPLETE:
        # No canonical scope anchor is ever created — must still appear.
        pass

    elif exc.exception_type == ExceptionType.SALES_CONTRACT_CUSTOMER_UNRESOLVED:
        _add(scopes, ScopeType.SALES_CONTRACT, _uuid_or_none(detail.get("sales_contract_id")))

    elif exc.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_UNCONFIRMED:
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, _uuid_or_none(detail.get("procurement_contract_id")))
        _add(scopes, ScopeType.SALES_CONTRACT, _uuid_or_none(detail.get("sales_contract_id")))

    elif exc.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_MULTIPLE_SCOPES:
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, _uuid_or_none(detail.get("procurement_contract_id")))
        sales_ids = detail.get("sales_contract_ids")
        if isinstance(sales_ids, list):
            for raw_id in sales_ids:
                _add(scopes, ScopeType.SALES_CONTRACT, _uuid_or_none(raw_id))

    elif exc.exception_type == ExceptionType.PROCUREMENT_SALES_LINK_CORRECTION_CONFLICT:
        link_id = _uuid_or_none(detail.get("superseded_link_id"))
        link = link_repo.get(link_id) if link_id is not None else None
        if link is not None:
            _add(scopes, ScopeType.PROCUREMENT_CONTRACT, link.procurement_contract_id)
            _add(scopes, ScopeType.SALES_CONTRACT, link.sales_contract_id)

    # Backfill identity types: no structured canonical id is ever stored
    # (identity lives inside identity_key text, which is never parsed) —
    # business scope stays empty and the item remains globally visible.
    elif exc.exception_type in {
        ExceptionType.BACKFILL_IDENTITY_INCOMPLETE,
        ExceptionType.BACKFILL_IDENTITY_AMBIGUOUS,
        ExceptionType.BACKFILL_CONFLICT,
    }:
        pass

    # Any other persisted type (e.g. a future producer, or the
    # declared-but-unproduced PROCUREMENT_SALES_LINK_CONFLICT if a row of it
    # ever exists) still projects with empty scopes and REVIEW_ONLY — never
    # dropped, never synthesized when no row exists.
    return _ordered_scopes(scopes)


def _task_exception_items(session: Session, filters: UnresolvedWorkFilters) -> list[UnresolvedWorkItem]:
    """Persisted TaskException rows -> Center items. Only OPEN tasks by
    default; ``open_only``/``status`` filters extend or restrict that."""
    if filters.status is not None:
        task_status_filter: str | None = None  # the neutral status filter decides
    elif filters.open_only is False:
        task_status_filter = None
    else:
        task_status_filter = ExceptionStatus.OPEN

    item_repo = ContractItemRepository(session)
    shipment_repo = ShipmentRepository(session)
    link_repo = ProcurementSalesLinkRepository(session)

    items: list[UnresolvedWorkItem] = []
    for exc in ExceptionRepository(session).list_all():
        if task_status_filter is not None and exc.status != task_status_filter:
            continue
        scopes = _task_exception_scopes(exc, item_repo, shipment_repo, link_repo)
        items.append(
            UnresolvedWorkItem(
                source_type=SourceType.TASK_EXCEPTION,
                source_id=exc.id,
                code=exc.exception_type,
                status=exc.status,
                summary=exc.summary,
                created_at=exc.created_at,
                scopes=scopes,
                procurement_contract_id=_first_or_none(scopes, ScopeType.PROCUREMENT_CONTRACT),
                sales_contract_id=_first_or_none(scopes, ScopeType.SALES_CONTRACT),
                shipment_id=_first_or_none(scopes, ScopeType.SHIPMENT),
                resolution_route=_EXCEPTION_ROUTES.get(exc.exception_type, ResolutionRoute.REVIEW_ONLY),
                provenance=_EXCEPTION_PRODUCERS.get(exc.exception_type),
            )
        )
    return items


# ---------------------------------------------------------------------------
# MATCH_CASE aggregation
# ---------------------------------------------------------------------------


def _match_case_items(session: Session) -> list[UnresolvedWorkItem]:
    """Persisted MatchCase rows in HUMAN_CONFIRMATION_REQUIRED -> Center
    items. Candidate scopes come from the real candidate rows
    (MatchCandidate for the procurement leg, SalesMatchCandidate for the
    sales leg). UNMATCHED and REJECTED are never Center sources."""

    # The subject trace: a case whose subject_type makes the invoice/payment
    # explicit carries that id directly — no object lookup required.
    match_candidate_repo = MatchCandidateRepository(session)
    sales_candidate_repo = SalesMatchCandidateRepository(session)

    items: list[UnresolvedWorkItem] = []
    for case in MatchCaseRepository(session).list_by_status(MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED):
        scopes: list[UnresolvedWorkScope] = []
        for candidate in match_candidate_repo.list_for_case(case.id):
            _add(scopes, ScopeType.PROCUREMENT_CONTRACT, candidate.contract_id)
        for candidate in sales_candidate_repo.list_for_case(case.id):
            _add(scopes, ScopeType.SALES_CONTRACT, candidate.sales_contract_id)
        scopes = _ordered_scopes(scopes)

        invoice_id = case.subject_id if case.subject_type == SubjectType.INVOICE else None
        payment_id = case.subject_id if case.subject_type == SubjectType.PAYMENT else None
        sales_leg = any(s.scope_type == ScopeType.SALES_CONTRACT for s in scopes)
        summary = (
            f"{case.subject_type} {case.subject_id} 需要人工确认销售侧匹配"
            if sales_leg
            else f"{case.subject_type} {case.subject_id} 需要人工确认匹配"
        )

        items.append(
            UnresolvedWorkItem(
                source_type=SourceType.MATCH_CASE,
                source_id=case.id,
                code=case.match_method,
                status=case.status,
                summary=summary,
                created_at=case.created_at,
                scopes=scopes,
                procurement_contract_id=_first_or_none(scopes, ScopeType.PROCUREMENT_CONTRACT),
                sales_contract_id=_first_or_none(scopes, ScopeType.SALES_CONTRACT),
                invoice_id=invoice_id,
                payment_id=payment_id,
                match_case_id=case.id,
                resolution_route=ResolutionRoute.CONFIRM_MATCH,
                provenance="bel.application.sales_matching" if sales_leg else "bel.application.matching",
            )
        )
    return items


# ---------------------------------------------------------------------------
# COMPUTED_BLOCKER aggregation
# ---------------------------------------------------------------------------


def _computed_blocker_source_id(period: str, blocker) -> str:
    """The frozen deterministic source_id for a computed blocker (§8): a
    pure function of the period, blocker_type and every scope id the blocker
    carries, in fixed canonical order — contract_id, then contract_item_id,
    then accrual_id, then the full accrual_ids tuple sorted by str(uuid).

    Readable pipe-delimited encoding. Every component is a fixed-format
    token (YYYY-MM / an uppercase constant / a hex UUID), so the encoding
    is collision-free. No random UUID, no Python hash(), no DB row — the
    same facts recompute the same key across calls and processes.
    """
    parts = [period, blocker.blocker_type, str(blocker.contract_id)]
    if blocker.contract_item_id is not None:
        parts.append(str(blocker.contract_item_id))
    if blocker.accrual_id is not None:
        parts.append(str(blocker.accrual_id))
    if blocker.accrual_ids:
        parts.extend(sorted(str(a) for a in blocker.accrual_ids))
    return "|".join(parts)


def _computed_blocker_items(session: Session, period: str) -> list[UnresolvedWorkItem]:
    """Recompute the Period Close preview for *period* and project its
    blockers. Nothing is persisted and no TaskException row is created.
    Created_at stays None (period-scoped, not an event)."""
    validate_period(period)
    preview = build_period_close_preview(session, period)
    items: list[UnresolvedWorkItem] = []
    for blocker in preview.blockers:
        scopes: list[UnresolvedWorkScope] = []
        _add(scopes, ScopeType.PROCUREMENT_CONTRACT, blocker.contract_id)
        _add(scopes, ScopeType.CONTRACT_ITEM, blocker.contract_item_id)
        scopes = _ordered_scopes(scopes)
        items.append(
            UnresolvedWorkItem(
                source_type=SourceType.COMPUTED_BLOCKER,
                source_id=_computed_blocker_source_id(period, blocker),
                code=blocker.blocker_type,
                status=ComputedBlockerStatus.PRESENT,
                summary=COMPUTED_BLOCKER_SUMMARIES.get(blocker.blocker_type, blocker.blocker_type),
                created_at=None,
                scopes=scopes,
                procurement_contract_id=_first_or_none(scopes, ScopeType.PROCUREMENT_CONTRACT),
                resolution_route=ResolutionRoute.REVIEW_ONLY,
                provenance=COMPUTED_BLOCKER_PROVENANCE,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Filters and deterministic ordering
# ---------------------------------------------------------------------------


def _item_has_scope(item: UnresolvedWorkItem, scope_type: str, scope_id: uuid.UUID) -> bool:
    return any(s.scope_type == scope_type and s.scope_id == scope_id for s in item.scopes)


def _apply_filters(items: list[UnresolvedWorkItem], filters: UnresolvedWorkFilters) -> list[UnresolvedWorkItem]:
    """Neutral projection filtering — never mutates data. ``status`` /
    ``open_only`` interplay is resolved in ``_task_exception_items`` for
    TaskException rows; here the explicit ``status`` filter applies to every
    source type uniformly (computed blockers have status PRESENT)."""
    result = []
    for item in items:
        if filters.source_type is not None and item.source_type != filters.source_type:
            continue
        if filters.code is not None and item.code != filters.code:
            continue
        if filters.status is not None and item.status != filters.status:
            continue
        if (
            filters.procurement_contract_id is not None
            and not _item_has_scope(item, ScopeType.PROCUREMENT_CONTRACT, filters.procurement_contract_id)
        ):
            continue
        if (
            filters.sales_contract_id is not None
            and not _item_has_scope(item, ScopeType.SALES_CONTRACT, filters.sales_contract_id)
        ):
            continue
        result.append(item)
    return result


def _sort_items(items: list[UnresolvedWorkItem]) -> list[UnresolvedWorkItem]:
    """One documented deterministic ordering. Persisted items (created_at is
    a real event time) newest-first by created_at, tie-broken by
    source_type then source_id. Computed blockers have created_at=None by
    design (period-scoped, not an event) so they sort as their own stable
    group AFTER persisted items, ordered by code then source_id — avoiding
    any unstable mixed None/datetime comparison."""
    persisted = [i for i in items if i.created_at is not None]
    computed = [i for i in items if i.created_at is None]
    persisted.sort(key=lambda i: (-i.created_at.timestamp(), i.source_type, str(i.source_id)))
    computed.sort(key=lambda i: (i.code, str(i.source_id)))
    return persisted + computed


def _counts(items: list[UnresolvedWorkItem]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(items),
        SourceType.TASK_EXCEPTION: 0,
        SourceType.MATCH_CASE: 0,
        SourceType.COMPUTED_BLOCKER: 0,
    }
    for item in items:
        counts[item.source_type] = counts.get(item.source_type, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The one Application path
# ---------------------------------------------------------------------------


def get_unresolved_work_center(
    session: Session,
    *,
    filters: UnresolvedWorkFilters | None = None,
) -> UnresolvedWorkCenter:
    """Compose the global Exception & Task Center read projection.

    Strict read-only: the whole body runs under ``session.no_autoflush`` and
    performs zero business-state writes. Without a requested period the
    Center aggregates only persisted unresolved sources (TASK_EXCEPTION +
    MATCH_CASE); ``filters.period`` adds that period's computed blockers.
    """
    filters = filters or UnresolvedWorkFilters()
    if filters.period is not None:
        validate_period(filters.period)

    with session.no_autoflush:
        items: list[UnresolvedWorkItem] = []
        items.extend(_task_exception_items(session, filters))
        items.extend(_match_case_items(session))
        if filters.period is not None:
            items.extend(_computed_blocker_items(session, filters.period))

        items = _apply_filters(items, filters)
        items = _sort_items(items)

    return UnresolvedWorkCenter(items=tuple(items), filters=filters, counts=_counts(items))
