"""R5 backfill source plan (Phase 2D.1-R5, docs/ROADMAP.md 2D.1-R5
section 10).

A small, versioned, CLOSED manifest naming which backfill sources apply
for one period — never a generic ETL/workflow DSL, and never an
arbitrary Python/plugin command. Every path in the plan is validated to
resolve INSIDE the period directory before it is ever opened; ``../``,
an absolute path, and a symlink escape are all rejected the same way
``tests/private_acceptance/runner.py`` already rejects them for its own
private-root paths (the identical discipline, reused here rather than
reinvented).

Shared by the CLI seam (``bel cutover backfill``) and the private
acceptance runner (``P2D_CUTOVER_RECONCILIATION``) — one execution path,
not two.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from bel.application.cutover_backfill import (
    BackfillOutcome,
    backfill_contract_items,
    backfill_contracts,
    backfill_invoices,
    backfill_payments,
    backfill_procurement_sales_links,
    backfill_sales_contracts,
    backfill_shipments,
)
from bel.application.cutover_fact_pack import CutoverFactPackResult, import_cutover_fact_pack
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.repositories import EvidenceRepository

PLAN_VERSION = 1
CLOSED_PLAN_SECTIONS = (
    "version",
    "contracts",
    "contract_items",
    "invoices",
    "payments",
    "shipments",
    "sales_contracts",
    "procurement_sales_links",
    "cutover_fact_pack",
)


class CutoverPlanError(ValueError):
    """A rejected backfill plan — unknown section, malformed entry, or a
    path that does not resolve inside the period directory."""


class CutoverPlanPathEscape(CutoverPlanError):
    """A plan path attempted to escape the period directory (``../``, an
    absolute path, or a symlink pointing outside it). This is a HARD
    security boundary, not a business-logic conflict."""


@dataclass
class PlanRunResult:
    sections: dict[str, Any]


def _resolve_plan_path(period_dir: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise CutoverPlanError("plan path must be a non-empty string")
    if relative.startswith("/") or relative.startswith("~"):
        raise CutoverPlanPathEscape(f"plan path must be relative to the period directory, got {relative!r}")
    if ".." in Path(relative).parts:
        raise CutoverPlanPathEscape(f"plan path must not contain '..', got {relative!r}")
    # Containment is checked against the non-strict resolution first (so
    # a merely-nonexistent target reports a normal file-not-found, never
    # masked as a path-escape finding), THEN existence is required —
    # this also catches a symlink whose resolved target escapes the
    # period directory, which the lexical '..' check alone cannot.
    resolved_period_dir = period_dir.resolve(strict=True)
    candidate = (period_dir / relative).resolve(strict=False)
    try:
        candidate.relative_to(resolved_period_dir)
    except ValueError as exc:
        raise CutoverPlanPathEscape(
            f"plan path {relative!r} resolves outside the period directory {resolved_period_dir}"
        ) from exc
    if not candidate.exists():
        raise CutoverPlanError(f"plan path {relative!r} does not exist under the period directory")
    return candidate


def validate_plan(plan: dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise CutoverPlanError("backfill plan must be a JSON object")
    unknown = sorted(set(plan.keys()) - set(CLOSED_PLAN_SECTIONS))
    if unknown:
        raise CutoverPlanError(f"backfill plan contains unrecognised section(s): {unknown}")
    if plan.get("version") not in (None, PLAN_VERSION):
        raise CutoverPlanError(f"unsupported backfill plan version: {plan.get('version')!r}")


def _outcome_to_dict(outcome: BackfillOutcome) -> dict[str, Any]:
    return {
        "created": outcome.created,
        "replay_or_corroborating": outcome.replay_or_corroborating,
        "tasks": [{"kind": t.kind, "detail": t.detail} for t in outcome.tasks],
    }


def _manual_entries_fragment(session: Session, *, raw_data: dict, source_type: str, now: datetime) -> EvidenceFragment:
    """The Evidence for a plan section supplied as inline structured
    entries (contract_items/shipments/sales_contracts/procurement_sales_links)
    rather than a source file — the plan itself, or the specific section
    of it, IS the Evidence, recorded as MANUAL_FACT."""
    import hashlib
    import json
    import uuid

    payload = json.dumps(raw_data, sort_keys=True, default=str).encode("utf-8")
    sha256 = hashlib.sha256(payload).hexdigest()
    evidence_repo = EvidenceRepository(session)
    existing_document = evidence_repo.find_document_by_sha256(sha256)
    if existing_document is not None:
        existing_fragment = evidence_repo.find_fragment_by_document(existing_document.id)
        if existing_fragment is not None:
            return existing_fragment
    document = EvidenceDocument(
        id=uuid.uuid4(), file_name=f"backfill-plan-{source_type}-{now.isoformat()}.json", sha256=sha256,
        source_type=source_type, imported_at=now,
    )
    evidence_repo.add_document(document)
    fragment = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=document.id, fragment_kind=FragmentKind.MANUAL_FACT, sheet_name=None,
        row_number=None, locator_json={"plan_section": source_type}, raw_data=raw_data, created_at=now,
    )
    evidence_repo.add_fragment(fragment)
    session.flush()
    return fragment


def run_backfill_plan(session: Session, plan: dict[str, Any], *, period_dir: Path, created_at: datetime) -> PlanRunResult:
    """Execute one validated backfill plan. Every path section resolves
    strictly inside ``period_dir`` before it is opened; every entries
    section is real Evidence-bearing structured input, never derived
    status. NEVER reads anything under ``expected/`` — that is
    reconciliation's own concern, not backfill's (section 47, HARD)."""
    validate_plan(plan)
    sections: dict[str, Any] = {}

    if "contracts" in plan:
        path = _resolve_plan_path(period_dir, plan["contracts"]["path"])
        sections["contracts"] = _outcome_to_dict(backfill_contracts(session, path, created_at=created_at))

    if "invoices" in plan:
        results = []
        for entry in plan["invoices"]:
            path = _resolve_plan_path(period_dir, entry["path"])
            direction = entry.get("direction")
            if direction not in ("PURCHASE", "SALES"):
                raise CutoverPlanError(f"invoices: direction must be PURCHASE or SALES, got {direction!r}")
            results.append(_outcome_to_dict(backfill_invoices(session, path, direction, created_at=created_at)))
        sections["invoices"] = results

    if "payments" in plan:
        results = []
        for entry in plan["payments"]:
            path = _resolve_plan_path(period_dir, entry["path"])
            profile = entry.get("profile", "cmb")
            source_account_id = entry.get("source_account_id")
            results.append(
                _outcome_to_dict(
                    backfill_payments(session, path, profile, source_account_id=source_account_id, created_at=created_at)
                )
            )
        sections["payments"] = results

    if "contract_items" in plan:
        entries = plan["contract_items"].get("entries", [])
        fragment = _manual_entries_fragment(
            session, raw_data={"entries": entries}, source_type="backfill_plan_contract_items", now=created_at
        )
        sections["contract_items"] = _outcome_to_dict(
            backfill_contract_items(session, entries, source_fragment_id=fragment.id, created_at=created_at)
        )

    if "shipments" in plan:
        entries = plan["shipments"].get("entries", [])
        fragment = _manual_entries_fragment(
            session, raw_data={"entries": entries}, source_type="backfill_plan_shipments", now=created_at
        )
        sections["shipments"] = _outcome_to_dict(
            backfill_shipments(session, entries, source_fragment_id=fragment.id, created_at=created_at)
        )

    if "sales_contracts" in plan:
        entries = plan["sales_contracts"].get("entries", [])
        fragment = _manual_entries_fragment(
            session, raw_data={"entries": entries}, source_type="backfill_plan_sales_contracts", now=created_at
        )
        sections["sales_contracts"] = _outcome_to_dict(
            backfill_sales_contracts(session, entries, source_fragment_id=fragment.id, created_at=created_at)
        )

    if "procurement_sales_links" in plan:
        entries = plan["procurement_sales_links"].get("entries", [])
        fragment = _manual_entries_fragment(
            session, raw_data={"entries": entries}, source_type="backfill_plan_procurement_sales_links", now=created_at
        )
        sections["procurement_sales_links"] = _outcome_to_dict(
            backfill_procurement_sales_links(session, entries, source_fragment_id=fragment.id, created_at=created_at)
        )

    if "cutover_fact_pack" in plan:
        import json

        path = _resolve_plan_path(period_dir, plan["cutover_fact_pack"]["path"])
        pack = json.loads(path.read_text(encoding="utf-8"))
        result: CutoverFactPackResult = import_cutover_fact_pack(
            session, pack, file_name=path.name, created_at=created_at
        )
        sections["cutover_fact_pack"] = asdict(result)

    session.commit()
    return PlanRunResult(sections=sections)
