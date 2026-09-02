"""FIRST-STAGE CUTOVER GATE (docs/FIRST-STAGE-CUTOVER-GATE.md).

A read-only readiness GATE that answers exactly one question:

    Is THIS PostgreSQL database, built from THIS private cutover source
    and business-confirmed Cutover Baseline, ready for BEL to be declared
    the System of Record?

The Gate is NOT a cutover switch. It never performs the switch, never
mutates a "system_of_record" flag, never demotes Excel, never invents
baseline data, never repairs discrepancies, never auto-resolves Tasks and
never runs backfill as part of the judgment. It JUDGES an already-prepared
target database (expected sequence: fresh PostgreSQL -> ``alembic upgrade
head`` -> approved backfill plan executed -> human/business corrections ->
FIRST-STAGE CUTOVER GATE -> PASS -> human decision to declare SoR).

Seven mandatory dimensions, each PASS/FAIL, no weighted score, no
"mostly ready":

    runtime_schema     effective runtime is the canonical BEL PostgreSQL
                       dialect postgresql+psycopg:// (psycopg3) and schema
                       revision == Alembic head (no startup migration, no
                       auto-upgrade; sqlite and every non-psycopg driver
                       rejected)
    cutover_inputs     backfill-plan.json + expected/cutover-baseline.json
                       present AND contained for the period (never
                       synthesized; read only through the hardened private
                       input boundary, so a nested/final symlink escape is
                       rejected before any outside-root file is parsed)
    reconciliation     canonical bel.application.cutover_reconciliation
                       passes: ``passed`` and ``unresolved_count == 0``
    work_surfaces      the five first-stage Application projections execute
    data_products      the four first-stage Data Products generate, each
                       CSV/XLSX twice and byte-identical
    privacy_boundary   private root set/resolved outside the repo, strict
                       YYYY-MM period, no symlink/path escape
    read_only          zero business-state writes (full-schema fingerprint
                       before == after)

A mandatory FAIL can never be compensated by a warning.

Public stdout from the CLI stays exactly ``FIRST_STAGE_CUTOVER_GATE:
PASS`` / ``FIRST_STAGE_CUTOVER_GATE: FAIL``. All diagnostics — including
private-derived values — are written ONLY under
``$BEL_PRIVATE_DATA_ROOT/reports/first-stage-cutover-gate-<YYYY-MM>.json``.

Reconciliation semantics are reused, never duplicated: the canonical
``reconcile`` already treats every OPEN backfill-produced TaskException
(BackfillIdentityIncomplete / BackfillIdentityAmbiguous / BackfillConflict)
as an unconditional UNRESOLVED entry, so the Gate's ``unresolved cutover
discrepancy = 0`` requirement covers backfill unresolved work without a
second implementation. Ordinary operational unresolved work (non-backfill
Tasks, HUMAN_CONFIRMATION_REQUIRED match cases, a Period Close blocker that
is present but is not a cutover discrepancy) may legitimately coexist with
a PASS — SoR cutover is not "zero tasks anywhere".
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from bel.application.contract_360 import get_contract_360
from bel.application.contract_business_ledger import ContractLedgerFilters, get_contract_business_ledger
from bel.application.contract_ledger_export import (
    export_contract_business_ledger_csv,
    export_contract_business_ledger_xlsx,
)
from bel.application.cutover_reconciliation import reconcile
from bel.application.exception_task_data_product import (
    build_exception_task_data_product,
    export_exception_task_csv,
    export_exception_task_xlsx,
)
from bel.application.invoice_preparation_export import (
    build_invoice_preparation_data_product,
    export_invoice_preparation_csv,
    export_invoice_preparation_xlsx,
)
from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench
from bel.application.period_close_export import (
    build_period_close_data_product,
    export_period_close_csv,
    export_period_close_xlsx,
)
from bel.application.period_close_workbench import get_period_close_workbench
from bel.application.unresolved_work_center import UnresolvedWorkFilters, get_unresolved_work_center
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import ContractRepository
from bel.infrastructure.persistence.schema_gate import SchemaNotAtHeadError, assert_schema_at_head
from bel.infrastructure.private_paths import (
    REASON_INPUT_ESCAPE,
    REASON_INPUT_MISSING,
    REASON_INPUT_TOO_LARGE,
    REASON_INPUT_UNSAFE_TYPE,
    PrivatePeriodReader,
    PrivateRootError,
    resolve_private_root,
    write_private_report,
)

# ---------------------------------------------------------------------------
# Dimensions and statuses
# ---------------------------------------------------------------------------

DIM_RUNTIME_SCHEMA = "runtime_schema"
DIM_CUTOVER_INPUTS = "cutover_inputs"
DIM_RECONCILIATION = "reconciliation"
DIM_WORK_SURFACES = "work_surfaces"
DIM_DATA_PRODUCTS = "data_products"
DIM_PRIVACY_BOUNDARY = "privacy_boundary"
DIM_READ_ONLY = "read_only"

MANDATORY_DIMENSIONS = (
    DIM_RUNTIME_SCHEMA,
    DIM_CUTOVER_INPUTS,
    DIM_RECONCILIATION,
    DIM_WORK_SURFACES,
    DIM_DATA_PRODUCTS,
    DIM_PRIVACY_BOUNDARY,
    DIM_READ_ONLY,
)

PASS = "PASS"
FAIL = "FAIL"

# Machine-readable failure reason codes (written only to the private report).
REASON_NON_POSTGRESQL = "NON_POSTGRESQL_DIALECT"
REASON_NON_CANONICAL_DATABASE_DRIVER = "NON_CANONICAL_DATABASE_DRIVER"
REASON_SCHEMA_NOT_AT_HEAD = "SCHEMA_NOT_AT_HEAD"
REASON_BACKFILL_PLAN_MISSING = "BACKFILL_PLAN_MISSING"
REASON_BASELINE_MISSING = "BASELINE_MISSING"
# Private-boundary input reason codes surfaced on the result (aliased to the
# shared codes in bel.infrastructure.private_paths so they can never drift).
REASON_PRIVATE_INPUT_ESCAPE = REASON_INPUT_ESCAPE
REASON_PRIVATE_INPUT_UNSAFE_TYPE = REASON_INPUT_UNSAFE_TYPE
REASON_PRIVATE_INPUT_TOO_LARGE = REASON_INPUT_TOO_LARGE
REASON_BASELINE_PARSE = "BASELINE_PARSE_ERROR"
REASON_RECONCILIATION_UNRESOLVED = "RECONCILIATION_UNRESOLVED"
REASON_RECONCILIATION_ERROR = "RECONCILIATION_ERROR"
REASON_SURFACE_ERROR = "SURFACE_ERROR"
REASON_EXPORT_ERROR = "EXPORT_ERROR"
REASON_EXPORT_NONDETERMINISM = "EXPORT_NONDETERMINISM"
REASON_BUSINESS_STATE_MUTATED = "BUSINESS_STATE_MUTATED"
REASON_UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

REPORT_PREFIX = "first-stage-cutover-gate"

# The five required first-stage work surfaces (section 9).
SURFACE_NAMES = (
    "contract_business_ledger",
    "contract_360",
    "period_close_workbench",
    "invoice_preparation_workbench",
    "exception_task_center",
)

# The four required first-stage Data Products (section 10).
PRODUCT_NAMES = ("contract_ledger", "period_close", "invoice_preparation", "exception_task")
EXPORT_FORMATS = ("csv", "xlsx")


@dataclass(frozen=True)
class RuntimeCheck:
    """Result of probing the target runtime: the effective SQLAlchemy
    runtime is the canonical BEL PostgreSQL dialect (``postgresql+psycopg``)
    AND the schema revision equals Alembic head. ``*_reason_code`` is the
    stable machine-readable failure reason (used on the result's
    ``reason_codes``); ``*_reason`` is a diagnostic string for the private
    report only."""

    dialect_ok: bool
    schema_ok: bool
    dialect_reason_code: str | None = None
    schema_reason_code: str | None = None
    dialect_reason: str | None = None
    schema_reason: str | None = None


@dataclass(frozen=True)
class FirstStageCutoverGateResult:
    """Neutral Gate result. Every dimension is exactly PASS or FAIL — no
    score, no READY_WITH_WARNINGS. ``passed`` is True only when ALL
    mandatory dimensions PASS. ``diagnostics`` is private material for the
    report only (it may contain business-derived values); it is never
    printed."""

    period: str
    runtime_schema: str
    cutover_inputs: str
    reconciliation: str
    work_surfaces: str
    data_products: str
    privacy_boundary: str
    read_only: str
    reason_codes: tuple[str, ...]
    passed: bool
    report_written: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def dimensions(self) -> dict[str, str]:
        return {
            DIM_RUNTIME_SCHEMA: self.runtime_schema,
            DIM_CUTOVER_INPUTS: self.cutover_inputs,
            DIM_RECONCILIATION: self.reconciliation,
            DIM_WORK_SURFACES: self.work_surfaces,
            DIM_DATA_PRODUCTS: self.data_products,
            DIM_PRIVACY_BOUNDARY: self.privacy_boundary,
            DIM_READ_ONLY: self.read_only,
        }


# ---------------------------------------------------------------------------
# Runtime probe — PostgreSQL only, schema must equal Alembic head.
# ---------------------------------------------------------------------------


def canonical_postgresql_runtime(engine) -> tuple[bool, str | None]:
    """True iff the effective SQLAlchemy runtime is exactly the canonical
    BEL production runtime: the psycopg3 PostgreSQL dialect
    (``postgresql+psycopg://``).

    Verified from engine metadata (never the URL's credentials): the
    drivername is ``postgresql+psycopg`` AND ``dialect.name == "postgresql"``
    AND ``dialect.driver == "psycopg"``. The bare ``postgresql://`` form
    (defaults to psycopg2), ``postgresql+psycopg2://``,
    ``postgresql+asyncpg://``, and SQLite are all rejected — the URL is
    never rewritten and no driver fallback is attempted.

    Returns ``(ok, None)`` or ``(False, reason_code)`` where the reason
    code is ``REASON_NON_CANONICAL_DATABASE_DRIVER`` for a PostgreSQL
    dialect on a non-canonical driver, and ``REASON_NON_POSTGRESQL`` for
    any other dialect (e.g. SQLite)."""
    drivername = getattr(engine.url, "drivername", None)
    dialect_name = getattr(getattr(engine, "dialect", None), "name", None)
    dialect_driver = getattr(getattr(engine, "dialect", None), "driver", None)
    if (
        drivername == "postgresql+psycopg"
        and dialect_name == "postgresql"
        and dialect_driver == "psycopg"
    ):
        return True, None
    if dialect_name == "postgresql":
        return False, REASON_NON_CANONICAL_DATABASE_DRIVER
    return False, REASON_NON_POSTGRESQL


def default_runtime_check(session: Session) -> RuntimeCheck:
    """The production runtime probe: the effective runtime must be exactly
    the canonical ``postgresql+psycopg`` PostgreSQL dialect AND the schema
    revision must equal the single Alembic head. No startup migration and
    no auto-upgrade are ever performed — a mismatch is a FAIL, never an
    automatic fix. SQLite (test-only convenience) and every non-psycopg
    PostgreSQL driver are rejected for the real Gate."""
    engine = session.get_bind()
    dialect_ok, dialect_reason_code = canonical_postgresql_runtime(engine)
    if not dialect_ok:
        drivername = getattr(engine.url, "drivername", None)
        return RuntimeCheck(
            dialect_ok=False,
            schema_ok=False,
            dialect_reason_code=dialect_reason_code,
            dialect_reason=(
                f"unsupported database runtime {drivername!r} — the FIRST-STAGE CUTOVER GATE "
                "requires exactly postgresql+psycopg:// (PostgreSQL, psycopg3 driver)"
            ),
        )
    try:
        assert_schema_at_head(engine)
    except SchemaNotAtHeadError as exc:
        return RuntimeCheck(
            dialect_ok=True,
            schema_ok=False,
            schema_reason_code=REASON_SCHEMA_NOT_AT_HEAD,
            schema_reason=str(exc),
        )
    return RuntimeCheck(dialect_ok=True, schema_ok=True)


# ---------------------------------------------------------------------------
# Read-only fingerprint — full-schema business-state hash.
# ---------------------------------------------------------------------------


def _schema_fingerprint(session: Session) -> str:
    """A deterministic hash over every row of every table — any new Fact,
    new TaskException, status/MatchCase transition, allocation, accrual,
    correction, relationship or blocker snapshot changes it. Computed with
    ``no_autoflush`` so the fingerprint itself can never trigger a write."""
    hasher = hashlib.sha256()
    with session.no_autoflush:
        for table in sorted(Base.metadata.sorted_tables, key=lambda t: t.name):
            hasher.update(table.name.encode("utf-8"))
            order_by = list(table.primary_key.columns)
            stmt = table.select().order_by(*order_by) if order_by else table.select()
            for row in session.execute(stmt).all():
                for value in row:
                    hasher.update(repr(value).encode("utf-8"))
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Work surfaces — execute the existing Application projections, never
# reproduce business logic inside the Gate.
# ---------------------------------------------------------------------------


def _contract_360_surface(session: Session, period: str) -> dict[str, Any]:
    """Contract 360 requires an existing contract id. The Gate proves the
    canonical path remains operational on the first deterministic contract
    when one exists; a genuinely empty database is truthful and passes
    (nothing to compose) rather than failing for want of a row."""
    contracts = sorted(ContractRepository(session).list_all(), key=lambda c: (c.contract_no, str(c.id)))
    if not contracts:
        return {"status": "ok", "note": "no contract to compose — empty database is truthful"}
    result = get_contract_360(session, contracts[0].id, period)
    if result is None:
        return {"status": "error", "error": "get_contract_360 returned None for an existing contract"}
    return {"status": "ok", "contracts": len(contracts)}


def _run_surfaces(session: Session, period: str) -> tuple[bool, dict[str, Any]]:
    """Execute all five first-stage projections. Read-only by construction
    (each runs under its own ``session.no_autoflush``); an exception on any
    one is a FAIL. Nonzero rows are NOT required — empty is truthful."""
    ok = True
    results: dict[str, Any] = {}

    try:
        get_contract_business_ledger(session, ContractLedgerFilters())
        results["contract_business_ledger"] = "ok"
    except Exception as exc:  # noqa: BLE001 — one surface failing must not abort the rest
        results["contract_business_ledger"] = {"error": str(exc)}
        ok = False

    try:
        results["contract_360"] = _contract_360_surface(session, period)
        if results["contract_360"].get("status") == "error":
            ok = False
    except Exception as exc:  # noqa: BLE001
        results["contract_360"] = {"error": str(exc)}
        ok = False

    try:
        get_period_close_workbench(session, period)
        results["period_close_workbench"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["period_close_workbench"] = {"error": str(exc)}
        ok = False

    try:
        get_invoice_preparation_workbench(session)
        results["invoice_preparation_workbench"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["invoice_preparation_workbench"] = {"error": str(exc)}
        ok = False

    try:
        get_unresolved_work_center(session, filters=UnresolvedWorkFilters(period=period))
        results["exception_task_center"] = "ok"
    except Exception as exc:  # noqa: BLE001
        results["exception_task_center"] = {"error": str(exc)}
        ok = False

    return ok, results


# ---------------------------------------------------------------------------
# Data Products — existing canonical builders/serializers, generated twice
# from the same state/filter, byte-identical required. Nothing is saved
# into the repository; bytes stay in memory.
# ---------------------------------------------------------------------------


def _generate_product(session: Session, period: str, name: str) -> dict[str, bytes]:
    """One full generation of one first-stage Data Product (CSV + XLSX)
    through the canonical Application path — builder -> serializer. Raises
    on any failure; the caller turns a raise into a FAIL."""
    if name == "contract_ledger":
        ledger = get_contract_business_ledger(session, ContractLedgerFilters())
        return {
            "csv": export_contract_business_ledger_csv(ledger),
            "xlsx": export_contract_business_ledger_xlsx(ledger),
        }
    if name == "period_close":
        workbench = get_period_close_workbench(session, period)
        product = build_period_close_data_product(workbench)
        return {
            "csv": export_period_close_csv(product),
            "xlsx": export_period_close_xlsx(product),
        }
    if name == "invoice_preparation":
        workbench = get_invoice_preparation_workbench(session)
        product = build_invoice_preparation_data_product(workbench)
        return {
            "csv": export_invoice_preparation_csv(product),
            "xlsx": export_invoice_preparation_xlsx(product),
        }
    if name == "exception_task":
        center = get_unresolved_work_center(session, filters=UnresolvedWorkFilters(period=period))
        product = build_exception_task_data_product(center)
        return {
            "csv": export_exception_task_csv(product),
            "xlsx": export_exception_task_xlsx(product),
        }
    raise ValueError(f"unknown data product {name!r}")


def _verify_exports(session: Session, period: str) -> tuple[bool, bool, dict[str, Any]]:
    """Generate every required Data Product twice from the same state/filter
    and require byte-identical output per format. Returns
    ``(ok, any_error, results)`` — a raise and nondeterminism are distinct
    FAIL reasons."""
    ok = True
    any_error = False
    results: dict[str, Any] = {}
    for name in PRODUCT_NAMES:
        try:
            first = _generate_product(session, period, name)
            second = _generate_product(session, period, name)
        except Exception as exc:  # noqa: BLE001
            results[name] = {"error": str(exc)}
            ok = False
            any_error = True
            continue
        results[name] = {}
        for fmt in EXPORT_FORMATS:
            if first[fmt] == second[fmt]:
                results[name][fmt] = "ok"
            else:
                results[name][fmt] = "nondeterministic"
                ok = False
    return ok, any_error, results


# ---------------------------------------------------------------------------
# Report document (PRIVATE — written only under the private root).
# ---------------------------------------------------------------------------


def _application_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("bel")
    except Exception:  # noqa: BLE001 — best-effort report field
        return None


def candidate_sha() -> str | None:
    """Best-effort git HEAD of the repository — a private report field,
    never stdout. Read-only and failure-tolerant."""
    from bel.infrastructure.private_paths import repo_root

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root()), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            return sha or None
    except Exception:  # noqa: BLE001
        pass
    return None


def build_report_document(result: FirstStageCutoverGateResult, *, candidate_sha: str | None = None) -> dict[str, Any]:
    """Compose the private report body. Private-derived values are allowed
    here because this document is written ONLY under
    ``$BEL_PRIVATE_DATA_ROOT/reports/`` and is never duplicated to stdout
    or into the repository."""
    document: dict[str, Any] = {
        "gate": "first-stage-cutover-gate",
        "period": result.period,
        "candidate_sha": candidate_sha,
        "application_version": _application_version(),
        "passed": result.passed,
        "dimensions": result.dimensions,
        "reason_codes": list(result.reason_codes),
        "report_written": result.report_written,
    }
    document.update(result.diagnostics)
    return document


# ---------------------------------------------------------------------------
# The Gate.
# ---------------------------------------------------------------------------


def _failure_result(period: str, *, reason_codes: tuple[str, ...], diagnostics: dict[str, Any]) -> FirstStageCutoverGateResult:
    """A fully-FAIL result for a Gate that could not meaningfully run
    (e.g. an unexpected exception). No dimension is reported as PASS —
    an inability to judge is never a PASS."""
    return FirstStageCutoverGateResult(
        period=period,
        runtime_schema=FAIL,
        cutover_inputs=FAIL,
        reconciliation=FAIL,
        work_surfaces=FAIL,
        data_products=FAIL,
        privacy_boundary=FAIL,
        read_only=FAIL,
        reason_codes=reason_codes,
        passed=False,
        report_written=False,
        diagnostics=diagnostics,
    )


def _run_gate_impl(
    session: Session,
    *,
    period: str,
    private_root: Path | None,
    candidate_sha: str | None,
    runtime_check: Callable[[Session], RuntimeCheck],
    write_report: bool,
) -> FirstStageCutoverGateResult:
    dims: dict[str, str] = {name: PASS for name in MANDATORY_DIMENSIONS}
    reasons: list[str] = []
    diagnostics: dict[str, Any] = {}
    root: Path | None = None

    def _classify_input(exc: PrivateRootError) -> str:
        """Map a PrivatePeriodReader failure onto a diagnostic label."""
        if exc.reason_code == REASON_INPUT_MISSING:
            return "missing"
        if exc.reason_code == REASON_INPUT_ESCAPE:
            return "escape"
        if exc.reason_code == REASON_INPUT_UNSAFE_TYPE:
            return "unsafe_type"
        if exc.reason_code == REASON_INPUT_TOO_LARGE:
            return "too_large"
        return "escape"

    def _input_reason(exc: PrivateRootError, missing_reason: str) -> str:
        """A missing required file keeps its file-specific reason; any
        other private-boundary rejection surfaces its own precise code
        (PRIVATE_INPUT_ESCAPE / PRIVATE_INPUT_UNSAFE_TYPE /
        PRIVATE_INPUT_TOO_LARGE)."""
        return missing_reason if exc.reason_code == REASON_INPUT_MISSING else exc.reason_code

    # The descriptor-anchored period reader is opened once (privacy phase)
    # and closed in the finally below — whether or not the DB-dependent
    # dimensions ever run — so no anchored fd ever outlives the Gate.
    reader: PrivatePeriodReader | None = None
    try:
        # ---- 1. Privacy boundary (private root + strict YYYY-MM period). ----
        try:
            root = resolve_private_root(private_root)
        except PrivateRootError as exc:
            dims[DIM_PRIVACY_BOUNDARY] = FAIL
            reasons.append(exc.reason_code)
            diagnostics["privacy_error"] = str(exc)
        if root is not None:
            diagnostics["private_root"] = str(root)
            try:
                reader = PrivatePeriodReader.open(root, period)
            except PrivateRootError as exc:
                dims[DIM_PRIVACY_BOUNDARY] = FAIL
                reasons.append(exc.reason_code)
                diagnostics["period_error"] = str(exc)

        # ---- 2. Runtime / schema (canonical postgresql+psycopg, == head). ----
        probe = runtime_check(session)
        engine_bind = session.get_bind()
        diagnostics["runtime"] = {
            "dialect": getattr(engine_bind.dialect, "name", None),
            "dialect_driver": getattr(getattr(engine_bind, "dialect", None), "driver", None),
            "drivername": getattr(engine_bind.url, "drivername", None),
            "dialect_ok": probe.dialect_ok,
            "schema_ok": probe.schema_ok,
            "dialect_reason_code": probe.dialect_reason_code,
            "schema_reason_code": probe.schema_reason_code,
            "dialect_reason": probe.dialect_reason,
            "schema_reason": probe.schema_reason,
        }
        if not probe.dialect_ok:
            dims[DIM_RUNTIME_SCHEMA] = FAIL
            reasons.append(probe.dialect_reason_code or REASON_NON_POSTGRESQL)
        elif not probe.schema_ok:
            dims[DIM_RUNTIME_SCHEMA] = FAIL
            reasons.append(probe.schema_reason_code or REASON_SCHEMA_NOT_AT_HEAD)

        # ---- 3. DB-dependent dimensions. ----
        db_ok = (
            dims[DIM_RUNTIME_SCHEMA] == PASS
            and dims[DIM_PRIVACY_BOUNDARY] == PASS
            and reader is not None
        )
        if not db_ok:
            # A mandatory failure (runtime/schema or privacy) prevented the
            # DB-dependent readiness dimensions from being evaluated — an
            # unevaluated mandatory dimension is never reported PASS (there is
            # no "mostly ready"). ``read_only`` stays PASS: no DB operation ran,
            # so the Gate made no business write. The blocking reason codes
            # above already explain the FAIL.
            for dim in (
                DIM_CUTOVER_INPUTS,
                DIM_RECONCILIATION,
                DIM_WORK_SURFACES,
                DIM_DATA_PRODUCTS,
            ):
                dims[dim] = FAIL
        else:
            fingerprint_before = _schema_fingerprint(session)
            assert reader is not None

            # 3a. Required cutover inputs — never synthesized, never
            # inferred, and read ONLY through the descriptor-anchored
            # private-input boundary. A symlinked period/``expected`` dir, a
            # symlinked plan/baseline file, a non-regular input type, an
            # oversized input, or any path escape is rejected BEFORE its
            # content could be parsed.
            cutover_inputs_diag: dict[str, Any] = {}
            baseline_bytes: bytes | None = None

            try:
                reader.read("backfill-plan.json")
            except PrivateRootError as exc:
                cutover_inputs_diag["backfill_plan"] = _classify_input(exc)
                dims[DIM_CUTOVER_INPUTS] = FAIL
                reasons.append(_input_reason(exc, REASON_BACKFILL_PLAN_MISSING))
            else:
                cutover_inputs_diag["backfill_plan"] = "present"

            try:
                baseline_bytes = reader.read("expected/cutover-baseline.json")
            except PrivateRootError as exc:
                cutover_inputs_diag["cutover_baseline"] = _classify_input(exc)
                dims[DIM_CUTOVER_INPUTS] = FAIL
                reasons.append(_input_reason(exc, REASON_BASELINE_MISSING))
            else:
                cutover_inputs_diag["cutover_baseline"] = "present"

            diagnostics["cutover_inputs"] = cutover_inputs_diag

            if dims[DIM_CUTOVER_INPUTS] == FAIL:
                # A mandatory input is missing/escaping, so reconciliation
                # cannot be evaluated — an unevaluated mandatory dimension is
                # never a PASS. The input reason code(s) above already explain
                # why; no separate reason is added.
                dims[DIM_RECONCILIATION] = FAIL

            if dims[DIM_CUTOVER_INPUTS] == PASS:
                # 3b. Private cutover reconciliation — the canonical
                # implementation, never a second one. UNRESOLVED == 0 required.
                # OPEN backfill-produced Tasks already surface as
                # unconditional UNRESOLVED inside ``reconcile``, so no
                # duplicate check exists. ``baseline_bytes`` came from the
                # hardened input boundary, so parsing it immediately is safe.
                assert baseline_bytes is not None  # inputs PASS means both read
                try:
                    baseline = json.loads(baseline_bytes.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    dims[DIM_RECONCILIATION] = FAIL
                    reasons.append(REASON_BASELINE_PARSE)
                    diagnostics["reconciliation_error"] = str(exc)
                else:
                    try:
                        reconciliation = reconcile(session, baseline)
                    except Exception as exc:  # noqa: BLE001
                        dims[DIM_RECONCILIATION] = FAIL
                        reasons.append(REASON_RECONCILIATION_ERROR)
                        diagnostics["reconciliation_error"] = str(exc)
                    else:
                        diagnostics["reconciliation"] = {
                            "unresolved_count": reconciliation.unresolved_count,
                            "passed": reconciliation.passed,
                            "entries": [
                                {
                                    "key": e.key,
                                    "outcome": e.outcome,
                                    "baseline_outcome": e.baseline_outcome,
                                }
                                for e in reconciliation.entries
                            ],
                            "open_backfill_task_keys": [
                                e.key
                                for e in reconciliation.entries
                                if e.key.startswith("unresolved:backfill_task")
                            ],
                        }
                        if not (reconciliation.passed and reconciliation.unresolved_count == 0):
                            dims[DIM_RECONCILIATION] = FAIL
                            reasons.append(REASON_RECONCILIATION_UNRESOLVED)

            # 3c. Required work surfaces — same target DB, existing paths.
            surfaces_ok, surfaces = _run_surfaces(session, period)
            diagnostics["surfaces"] = surfaces
            if not surfaces_ok:
                dims[DIM_WORK_SURFACES] = FAIL
                reasons.append(REASON_SURFACE_ERROR)

            # 3d. Required Data Products — same target DB, byte-deterministic.
            exports_ok, any_export_error, exports = _verify_exports(session, period)
            diagnostics["exports"] = exports
            if not exports_ok:
                dims[DIM_DATA_PRODUCTS] = FAIL
                reasons.append(REASON_EXPORT_ERROR if any_export_error else REASON_EXPORT_NONDETERMINISM)

            # 3e. Read-only — the Gate must not mutate the thing it judges.
            fingerprint_after = _schema_fingerprint(session)
            diagnostics["read_only"] = {
                "fingerprint_before": fingerprint_before,
                "fingerprint_after": fingerprint_after,
                "unchanged": fingerprint_before == fingerprint_after,
            }
            if fingerprint_after != fingerprint_before:
                dims[DIM_READ_ONLY] = FAIL
                reasons.append(REASON_BUSINESS_STATE_MUTATED)
    finally:
        if reader is not None:
            try:
                reader.close()
            except OSError:
                pass

    # Reason codes are reported once each (e.g. two escaping inputs share
    # one PRIVATE_INPUT_ESCAPE code rather than stacking duplicates).
    reason_codes = tuple(dict.fromkeys(reasons))

    result = FirstStageCutoverGateResult(
        period=period,
        runtime_schema=dims[DIM_RUNTIME_SCHEMA],
        cutover_inputs=dims[DIM_CUTOVER_INPUTS],
        reconciliation=dims[DIM_RECONCILIATION],
        work_surfaces=dims[DIM_WORK_SURFACES],
        data_products=dims[DIM_DATA_PRODUCTS],
        privacy_boundary=dims[DIM_PRIVACY_BOUNDARY],
        read_only=dims[DIM_READ_ONLY],
        reason_codes=reason_codes,
        passed=all(value == PASS for value in dims.values()),
        report_written=False,
        diagnostics=diagnostics,
    )

    # ---- 4. Private report (only ever under the private root). ----
    if write_report and root is not None:
        report = build_report_document(result, candidate_sha=candidate_sha)
        written = write_private_report(root, f"{REPORT_PREFIX}-{period}.json", report)
        result = replace(result, report_written=written)

    return result


def run_first_stage_cutover_gate(
    session: Session,
    *,
    period: str,
    private_root: Path | None = None,
    candidate_sha: str | None = None,
    runtime_check: Callable[[Session], RuntimeCheck] | None = None,
    write_report: bool = True,
) -> FirstStageCutoverGateResult:
    """Judge an already-prepared target database for first-stage cutover.

    ``session`` is a session over the canonical ``BEL_DATABASE_URL``
    runtime. ``period`` is a strict ``YYYY-MM`` period. ``private_root``
    defaults to ``$BEL_PRIVATE_DATA_ROOT``. ``runtime_check`` is a
    controlled test double seam for the PostgreSQL/schema probe (the
    default performs the real probe). ``write_report`` controls whether
    the private diagnostic report is written.

    Never raises for an expected failure (missing root, missing baseline,
    UNRESOLVED, a surface error, ...): each becomes a FAIL dimension on
    the returned result. Only a genuine unexpected error inside the Gate's
    own machinery produces a fully-FAIL result — with the traceback
    confined to the private report, never stdout.
    """
    try:
        return _run_gate_impl(
            session,
            period=period,
            private_root=private_root,
            candidate_sha=candidate_sha,
            runtime_check=runtime_check or default_runtime_check,
            write_report=write_report,
        )
    except Exception as exc:  # noqa: BLE001 — an unexpected error is a FAIL, not a crash/leak
        diagnostics: dict[str, Any] = {"unexpected_error": str(exc), "traceback": traceback.format_exc()}
        result = _failure_result(period, reason_codes=(REASON_UNEXPECTED_ERROR,), diagnostics=diagnostics)
        if write_report:
            try:
                root = resolve_private_root(private_root)
                write_private_report(root, f"{REPORT_PREFIX}-{period}.json", build_report_document(result))
            except Exception:  # noqa: BLE001 — best-effort diagnostic
                pass
        return result
