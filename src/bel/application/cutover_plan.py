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

Gate-fix (Phase 2D.1-R5 round 2), HARD: this manifest is an
ORCHESTRATION document, never business Evidence. Earlier this module
fabricated ONE shared ``MANUAL_FACT`` fragment per ``contract_items`` /
``shipments`` / ``sales_contracts`` / ``procurement_sales_links`` plan
section and used it as if it were genuine per-entry Evidence — that is
removed. What replaces each:

- ``contract_items`` — ContractItem IS on the Human-Confirmed Cutover
  Fact closed allowlist (``bel.application.cutover_fact_pack``); put its
  entries in the ``cutover_fact_pack`` section's own file instead, which
  already tags them with the distinct ``cutover_baseline_manual``
  source_type and one real fragment per entry.
- ``sales_contracts`` / ``procurement_sales_links`` — SalesContract and
  ProcurementSalesLink are NOT on that allowlist and were never meant to
  be confirmable via a bare manifest assertion. The frozen, genuine
  Evidence basis for them (docs/PHASE2D1-R0-DECISIONS.md section 2.4) is
  the SAME contract-ledger row's own 买方/外销合同编码 pair — implemented
  automatically inside ``bel.application.cutover_backfill.backfill_contracts``
  (see ``_backfill_sales_scope_basis``), never as a plan section.
- ``shipments`` — Shipment must come from genuine export Evidence; no
  such source adapter exists in this codebase, so there is no plan
  section for it. ``cutover_backfill.backfill_shipments`` remains
  available as a direct-call primitive for a FUTURE genuine-Evidence
  caller, but this plan never wires it to a bare manifest assertion.

Gate-fix (Phase 2D.1-R5 round 2), HARD: no source path in the plan may
name ``<period>/expected/`` — that directory holds reconciliation's OWN
independently-supplied acceptance material (the Cutover Baseline), never
a backfill Fact source (section 47). The rejection happens at the path
boundary, BEFORE the file is ever opened, and AFTER symlink resolution,
so a literal ``expected/ledger.xlsx`` and a symlink whose resolved target
sits inside ``expected/`` are rejected identically — even when the file
named is a perfectly valid source workbook.

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

from bel.application.cutover_backfill import BackfillOutcome, backfill_contracts, backfill_invoices, backfill_payments
from bel.application.cutover_fact_pack import CutoverFactPackResult, import_cutover_fact_pack

PLAN_VERSION = 1
CLOSED_PLAN_SECTIONS = (
    "version",
    "contracts",
    "invoices",
    "payments",
    "cutover_fact_pack",
)

# Reconciliation's own acceptance material, never a backfill Fact source.
_EXPECTED_DIR_NAME = "expected"


class CutoverPlanError(ValueError):
    """A rejected backfill plan — unknown section, malformed entry, or a
    path that does not resolve inside the period directory."""


class CutoverPlanPathEscape(CutoverPlanError):
    """A plan path attempted to escape the period directory (``../``, an
    absolute path, or a symlink pointing outside it). This is a HARD
    security boundary, not a business-logic conflict."""


class CutoverPlanExpectedPath(CutoverPlanPathEscape):
    """A plan path named ``<period>/expected/`` — either literally, or
    through a symlink whose resolved target sits inside it. That
    directory is reconciliation's own independently-supplied acceptance
    material, never a Fact source (section 47, HARD), so it is rejected
    at the path boundary whether or not the file it names would
    otherwise parse as a valid source."""


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
    # expected/ is rejected both lexically and after resolution: a plan
    # naming it is never a Fact source, and the resolved check also
    # catches a symlink whose TARGET sits inside expected/ even though
    # the plan's own path string does not mention it.
    if Path(relative).parts[0] == _EXPECTED_DIR_NAME:
        raise CutoverPlanExpectedPath(
            f"plan path {relative!r} names {_EXPECTED_DIR_NAME}/ — reconciliation's acceptance material, "
            "never a backfill source"
        )
    # Containment is checked against the non-strict resolution first (so
    # a merely-nonexistent target reports a normal file-not-found, never
    # masked as a path-escape finding), THEN existence is required —
    # this also catches a symlink whose resolved target escapes the
    # period directory, which the lexical '..' check alone cannot.
    resolved_period_dir = period_dir.resolve(strict=True)
    candidate = (period_dir / relative).resolve(strict=False)
    try:
        relative_to_period = candidate.relative_to(resolved_period_dir)
    except ValueError as exc:
        raise CutoverPlanPathEscape(
            f"plan path {relative!r} resolves outside the period directory {resolved_period_dir}"
        ) from exc
    # A symlink (or a symlinked subdirectory) whose RESOLVED target sits
    # inside expected/ is the same violation as naming expected/
    # literally — rejected at the boundary, before the file is opened.
    if relative_to_period.parts and relative_to_period.parts[0] == _EXPECTED_DIR_NAME:
        raise CutoverPlanExpectedPath(
            f"plan path {relative!r} resolves inside {_EXPECTED_DIR_NAME}/ — reconciliation's acceptance "
            "material, never a backfill source"
        )
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
        "tasks": [{"kind": t.kind, "detail": t.detail, "task_exception_id": str(t.task_exception_id)} for t in outcome.tasks],
    }


def run_backfill_plan(session: Session, plan: dict[str, Any], *, period_dir: Path, created_at: datetime) -> PlanRunResult:
    """Execute one validated backfill plan. Every path section resolves
    strictly inside ``period_dir`` before it is opened. NEVER reads
    anything under ``expected/`` — that is reconciliation's own concern,
    not backfill's (section 47, HARD)."""
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
