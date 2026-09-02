"""FIRST-STAGE CUTOVER GATE — application-layer tests.

Covers the section 16 matrix: the required PASS case and FAIL cases A-M,
plus the three "does NOT itself fail the Gate" proofs (ordinary
non-cutover TaskException, Phase 2D.3 management advisory, Period Close
blocker visible-but-not-a-cutover-discrepancy).

The unit tests run on the SQLite test harness with a CONTROLLED test
double for the PostgreSQL/schema probe (``runtime_check``) — the real
PostgreSQL-only dialect + schema-at-head probe is covered by the
PostgreSQL integration test. Every other dimension (reconciliation,
surfaces, exports, privacy, read-only) runs for real.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from bel.application.first_stage_cutover_gate import (
    MANDATORY_DIMENSIONS,
    PASS,
    FAIL,
    REASON_BACKFILL_PLAN_MISSING,
    REASON_BASELINE_MISSING,
    REASON_EXPORT_ERROR,
    REASON_EXPORT_NONDETERMINISM,
    REASON_NON_CANONICAL_DATABASE_DRIVER,
    REASON_NON_POSTGRESQL,
    REASON_PRIVATE_INPUT_ESCAPE,
    REASON_RECONCILIATION_UNRESOLVED,
    REASON_SCHEMA_NOT_AT_HEAD,
    REASON_SURFACE_ERROR,
    RuntimeCheck,
    _schema_fingerprint,
    canonical_postgresql_runtime,
    default_runtime_check,
    run_first_stage_cutover_gate,
)
from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench
from bel.application.period_close import MISSING_ACCRUAL_BASIS, build_period_close_preview
from bel.application.supplier_invoice_request import SupplierRequestAdvisoryCode
from bel.domain.accrual import CostRecognitionFact
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.exception import ExceptionStatus, ExceptionType, TaskException
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
    PaymentAllocation,
)
from bel.domain.payment import Payment, PaymentDirection
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    ExceptionRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
)
from bel.infrastructure.private_paths import (
    REASON_INVALID_PERIOD,
    REASON_ROOT_NOT_SET,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Helpers — independently synthetic setup
# ---------------------------------------------------------------------------


def _make_fragment(session: Session) -> "EvidenceFragment":
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT, sheet_name=None,
        row_number=None, locator_json={}, raw_data={}, created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(
    session: Session, contract_no="C-GATE", counterparty="Supplier", gross_amount=Decimal("1000.00")
) -> Contract:
    frag = _make_fragment(session)
    contract = Contract(
        id=uuid.uuid4(), contract_no=contract_no, contract_type="出口报关购销合同", counterparty=counterparty,
        buyer="Buyer", gross_amount=gross_amount, currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _contract_key(contract: Contract) -> str:
    return f"contract:contract_no={contract.contract_no}|counterparty={contract.counterparty}"


def _unresolved_indicator_key(contract: Contract) -> str:
    return f"unresolved_indicator:contract_no={contract.contract_no}|counterparty={contract.counterparty}"


def _expected_contract_value(contract: Contract) -> dict:
    return {
        "contract_type": contract.contract_type, "buyer": contract.buyer,
        "gross_amount": str(contract.gross_amount), "currency": contract.currency,
        "contract_date": contract.contract_date.isoformat(),
    }


def _contract_entries(contract: Contract, *, has_unresolved: bool = False) -> list[dict]:
    return [
        {"key": _contract_key(contract), "expected": _expected_contract_value(contract), "outcome": "MATCH"},
        {"key": _unresolved_indicator_key(contract), "expected": {"has_unresolved": has_unresolved}, "outcome": "MATCH"},
    ]


@pytest.fixture
def gate_root(tmp_path) -> Path:
    root = tmp_path / "private"
    (root / "2026-01" / "expected").mkdir(parents=True)
    (root / "2026-01" / "backfill-plan.json").write_text(json.dumps({"version": 1}))
    return root


def _write_baseline(period_dir: Path, entries: list[dict]) -> None:
    (period_dir / "expected" / "cutover-baseline.json").write_text(json.dumps({"entries": entries}))


def _run(session: Session, root: Path, period: str = "2026-01", runtime=None, **kwargs):
    return run_first_stage_cutover_gate(
        session,
        period=period,
        private_root=root,
        runtime_check=runtime or (lambda s: RuntimeCheck(dialect_ok=True, schema_ok=True)),
        candidate_sha="cafe0000",
        **kwargs,
    )


def _stub_engine(drivername: str, dialect_name: str, driver: str):
    """A minimal engine stand-in exposing only the metadata the canonical
    runtime classification inspects (``url.drivername`` and
    ``dialect.name``/``dialect.driver``) — lets the driver matrix be
    tested without the drivers installed."""
    from types import SimpleNamespace

    return SimpleNamespace(
        url=SimpleNamespace(drivername=drivername),
        dialect=SimpleNamespace(name=dialect_name, driver=driver),
    )


def _make_open_task(
    session: Session, exception_type: str, *, contract_id: str | None = None, identity_key: str | None = None
) -> None:
    detail = {}
    if contract_id is not None:
        detail["contract_id"] = contract_id
    if identity_key is not None:
        detail["identity_key"] = identity_key
    ExceptionRepository(session).add(
        TaskException(
            id=uuid.uuid4(), exception_type=exception_type, status=ExceptionStatus.OPEN,
            summary="synthetic open task", detail=detail, created_at=NOW,
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Required PASS case
# ---------------------------------------------------------------------------


def test_gate_passes_on_fully_prepared_database(db_session, gate_root):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()

    result = _run(db_session, gate_root)
    assert result.passed
    for dim in MANDATORY_DIMENSIONS:
        assert result.dimensions[dim] == PASS, dim
    assert result.reason_codes == ()
    assert result.report_written

    report = gate_root / "reports" / "first-stage-cutover-gate-2026-01.json"
    assert report.exists()
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["passed"] is True
    assert body["candidate_sha"] == "cafe0000"
    assert body["period"] == "2026-01"
    assert body["reconciliation"]["unresolved_count"] == 0
    assert all(
        v == "ok" if isinstance(v, str) else v.get("status") == "ok" for v in body["surfaces"].values()
    )
    assert all(all(v == "ok" for v in fmt.values()) for fmt in body["exports"].values())
    assert body["read_only"]["unchanged"] is True


def test_gate_passes_on_empty_database_empty_is_truthful(db_session, gate_root):
    """No rows at all: every surface still executes, every Data Product
    still generates (deterministically), reconciliation against an empty
    baseline passes, and the Gate PASSes — the Gate never requires a
    nonzero row."""
    _write_baseline(gate_root / "2026-01", [])
    db_session.commit()
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.work_surfaces == PASS
    assert result.data_products == PASS
    assert result.diagnostics["surfaces"]["contract_360"]["status"] == "ok"


# ---------------------------------------------------------------------------
# FAIL A/B — runtime / schema
# ---------------------------------------------------------------------------


def test_fail_sqlite_runtime(db_session, gate_root):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root, runtime=lambda s: RuntimeCheck(dialect_ok=False, schema_ok=False))
    assert not result.passed
    assert result.runtime_schema == FAIL
    assert REASON_NON_POSTGRESQL in result.reason_codes
    # The DB-dependent dimensions could not be evaluated — unevaluated is
    # FAIL, never a "not run" PASS (no "mostly ready").
    assert result.cutover_inputs == FAIL
    assert result.reconciliation == FAIL
    assert result.work_surfaces == FAIL
    assert result.data_products == FAIL
    # read_only stays PASS by construction: no DB operation ran.
    assert result.read_only == PASS


def test_default_runtime_check_rejects_sqlite(db_session):
    probe = default_runtime_check(db_session)
    assert probe.dialect_ok is False
    assert probe.schema_ok is False
    assert probe.dialect_reason_code == REASON_NON_POSTGRESQL


def test_fail_schema_not_at_head(db_session, gate_root):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root, runtime=lambda s: RuntimeCheck(dialect_ok=True, schema_ok=False))
    assert not result.passed
    assert result.runtime_schema == FAIL
    assert REASON_SCHEMA_NOT_AT_HEAD in result.reason_codes


# ---------------------------------------------------------------------------
# FAIL C/D — privacy boundary
# ---------------------------------------------------------------------------


def test_fail_missing_private_root(db_session, monkeypatch):
    monkeypatch.delenv("BEL_PRIVATE_DATA_ROOT", raising=False)
    result = run_first_stage_cutover_gate(
        db_session, period="2026-01", private_root=None,
        runtime_check=lambda s: RuntimeCheck(dialect_ok=True, schema_ok=True),
    )
    assert not result.passed
    assert result.privacy_boundary == FAIL
    assert REASON_ROOT_NOT_SET in result.reason_codes


@pytest.mark.parametrize(
    "bad",
    ["../escape", "2026/07", "2026-7", "abc", "2026-13", "2026-00", "/abs", "2026-01/../../escape"],
)
def test_fail_invalid_or_escaping_period(db_session, gate_root, bad):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root, period=bad)
    assert not result.passed
    assert result.privacy_boundary == FAIL
    assert REASON_INVALID_PERIOD in result.reason_codes


def test_fail_period_dir_symlink_escaping_root(db_session, gate_root, tmp_path):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    outside = tmp_path / "outside"
    outside.mkdir()
    # Replace the real period directory (created by the fixture) with a
    # same-looking symlink that resolves outside the private root.
    shutil.rmtree(gate_root / "2026-01")
    os.symlink(outside, gate_root / "2026-01")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.privacy_boundary == FAIL
    assert "PERIOD_ESCAPE" in result.reason_codes


# ---------------------------------------------------------------------------
# FAIL E/F — cutover inputs
# ---------------------------------------------------------------------------


def test_fail_missing_backfill_plan(db_session, gate_root):
    (gate_root / "2026-01" / "backfill-plan.json").unlink()
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_BACKFILL_PLAN_MISSING in result.reason_codes
    # An unevaluated mandatory dimension is never reported PASS.
    assert result.reconciliation == FAIL


def test_fail_missing_cutover_baseline(db_session, gate_root):
    contract = _make_contract(db_session)
    db_session.commit()
    # No baseline file at all.
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_BASELINE_MISSING in result.reason_codes
    assert result.reconciliation == FAIL


# ---------------------------------------------------------------------------
# FAIL G/H — reconciliation / backfill unresolved work
# ---------------------------------------------------------------------------


def test_fail_reconciliation_unresolved(db_session, gate_root):
    contract = _make_contract(db_session)
    entries = _contract_entries(contract)
    entries[0]["expected"]["gross_amount"] = "999999.00"  # one-cent-off style mismatch
    _write_baseline(gate_root / "2026-01", entries)
    db_session.commit()
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.reconciliation == FAIL
    assert REASON_RECONCILIATION_UNRESOLVED in result.reason_codes
    assert result.diagnostics["reconciliation"]["unresolved_count"] >= 1


def test_fail_open_backfill_task_blocks_gate(db_session, gate_root):
    contract = _make_contract(db_session)
    _make_open_task(db_session, ExceptionType.BACKFILL_IDENTITY_INCOMPLETE, identity_key="Payment|orphan|...")
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.reconciliation == FAIL
    assert REASON_RECONCILIATION_UNRESOLVED in result.reason_codes
    assert result.diagnostics["reconciliation"]["open_backfill_task_keys"]


def test_fail_resolved_backfill_task_no_longer_blocks_gate(db_session, gate_root):
    contract = _make_contract(db_session)
    _make_open_task(db_session, ExceptionType.BACKFILL_IDENTITY_INCOMPLETE, identity_key="Payment|resolved|...")
    ExceptionRepository(db_session).update_status(
        next(e for e in ExceptionRepository(db_session).list_open()).id, ExceptionStatus.RESOLVED
    )
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root)
    assert result.passed


# ---------------------------------------------------------------------------
# FAIL I — work surface raises
# ---------------------------------------------------------------------------


def test_fail_surface_error(db_session, gate_root, monkeypatch):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    import bel.application.first_stage_cutover_gate as gate_mod

    def boom(session, filters=None):
        raise RuntimeError("contract business ledger exploded")

    monkeypatch.setattr(gate_mod, "get_contract_business_ledger", boom)
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.work_surfaces == FAIL
    assert REASON_SURFACE_ERROR in result.reason_codes
    assert "error" in result.diagnostics["surfaces"]["contract_business_ledger"]


# ---------------------------------------------------------------------------
# FAIL J/K — data product export fails / nondeterminism
# ---------------------------------------------------------------------------


def test_fail_export_error(db_session, gate_root, monkeypatch):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    import bel.application.first_stage_cutover_gate as gate_mod

    def boom(product):
        raise RuntimeError("xlsx export exploded")

    monkeypatch.setattr(gate_mod, "export_contract_business_ledger_xlsx", boom)
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.data_products == FAIL
    assert REASON_EXPORT_ERROR in result.reason_codes


def test_fail_export_nondeterminism(db_session, gate_root, monkeypatch):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    import bel.application.first_stage_cutover_gate as gate_mod

    calls = {"n": 0}

    def flaky(product):
        calls["n"] += 1
        return b"x" * calls["n"]

    monkeypatch.setattr(gate_mod, "export_period_close_csv", flaky)
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.data_products == FAIL
    assert REASON_EXPORT_NONDETERMINISM in result.reason_codes
    assert result.diagnostics["exports"]["period_close"]["csv"] == "nondeterministic"


# ---------------------------------------------------------------------------
# FAIL L — report path/symlink escape never changes the verdict or leaks
# ---------------------------------------------------------------------------


def test_report_symlink_escape_is_refused_without_crashing_gate(db_session, gate_root, tmp_path):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, gate_root / "reports")
    result = _run(db_session, gate_root)
    # The verdict is unaffected — a report-escape must never leak and
    # never change PASS/FAIL.
    assert result.passed
    assert result.report_written is False
    assert not (outside / "first-stage-cutover-gate-2026-01.json").exists()


# ---------------------------------------------------------------------------
# FAIL M — read-only regression guard
# ---------------------------------------------------------------------------


def test_gate_makes_zero_business_writes(db_session, gate_root):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()

    from bel.infrastructure.persistence.models import ContractModel, TaskExceptionModel

    def _counts():
        return (
            db_session.query(ContractModel).count(),
            db_session.query(TaskExceptionModel).count(),
        )

    before = _counts()
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.read_only == PASS
    assert result.diagnostics["read_only"]["unchanged"] is True
    assert _counts() == before


def test_fingerprint_detects_business_mutation(db_session):
    """The read_only fingerprint is sound: a real mutation — even one the
    Gate would never perform — changes it, so the dimension would FAIL if
    the Gate's own code ever wrote business state."""
    _make_contract(db_session)
    db_session.commit()
    fingerprint_before = _schema_fingerprint(db_session)
    _make_contract(db_session, contract_no="C-MUT")
    db_session.commit()
    assert _schema_fingerprint(db_session) != fingerprint_before


# ---------------------------------------------------------------------------
# The three "does NOT itself fail the Gate" proofs
# ---------------------------------------------------------------------------


def test_ordinary_non_cutover_task_does_not_fail_gate(db_session, gate_root):
    """An ordinary OPEN operational Task (AllocationCapacityExceeded) is
    NOT a cutover discrepancy: it is not an OPEN backfill task, and once
    its contract's unresolved indicator is adjudicated in the baseline,
    the Gate PASSes. SoR cutover is not 'zero tasks anywhere'."""
    contract = _make_contract(db_session)
    _make_open_task(db_session, ExceptionType.ALLOCATION_CAPACITY_EXCEEDED, contract_id=str(contract.id))
    _write_baseline(gate_root / "2026-01", _contract_entries(contract, has_unresolved=True))
    db_session.commit()
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.reconciliation == PASS


def test_phase2d3_management_advisory_does_not_fail_gate(db_session, gate_root):
    """A SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED (IP-P09) advisory — a
    computed management reminder from the invoice preparation Workbench,
    never a persisted Task, never part of the reconciliation snapshot —
    coexists with a Gate PASS."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, contract_no="C-ADV", counterparty="Supplier ADV")
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2026, 1, 15), direction=PaymentDirection.OUT,
        amount=Decimal("500.00"), counterparty="Supplier ADV", business_type=None,
        bank_reference="REF-ADV-0001", description=None, running_balance=None,
        source_fragment_id=frag.id, created_at=NOW, source_account_id="ACC-ADV",
    )
    PaymentRepository(db_session).add(payment)
    db_session.flush()
    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="PAYMENT", subject_id=payment.id,
        status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
        created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    PaymentAllocationRepository(db_session).add(
        PaymentAllocation(
            id=uuid.uuid4(), payment_id=payment.id, contract_id=contract.id, match_case_id=match_case.id,
            allocated_amount=Decimal("500.00"),
            match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    db_session.commit()

    # Prove the advisory is genuinely present in the Workbench.
    workbench = get_invoice_preparation_workbench(db_session)
    advisory_codes = {a.code for d in workbench.supplier_report.decisions for a in d.advisories}
    assert SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED in advisory_codes

    # The reconciliation's payment-allocation key uses the contract's
    # SCOPE key (contract_no/counterparty, no ``contract:`` prefix).
    contract_scope_key = f"contract_no={contract.contract_no}|counterparty={contract.counterparty}"
    entries = _contract_entries(contract)
    entries.append(
        {
            "key": (
                f"outgoing_payment_allocation:{contract_scope_key}|payment=source_account_id="
                f"{payment.source_account_id}|transaction_date={payment.transaction_date.isoformat()}"
                f"|direction={payment.direction}|amount={payment.amount}|bank_reference={payment.bank_reference}"
            ),
            "expected": {"allocated_amount": "500.00"}, "outcome": "MATCH",
        }
    )
    _write_baseline(gate_root / "2026-01", entries)
    db_session.commit()
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.reconciliation == PASS
    assert result.diagnostics["surfaces"]["invoice_preparation_workbench"] == "ok"


def test_period_close_blocker_visible_but_not_a_cutover_discrepancy(db_session, gate_root):
    """A Period Close blocker (MISSING_ACCRUAL_BASIS) is a projected
    Decision, never a persisted Fact: the Exception & Task Center shows
    it (visible/operational) yet the Gate PASSes — a blocker is not
    automatically a cutover discrepancy unless baseline/gate semantics
    say so (they do not)."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session)
    CostRecognitionFactRepository(db_session).add(
        CostRecognitionFact(
            id=uuid.uuid4(), contract_id=contract.id, recognition_date=date(2026, 1, 15),
            basis="MANUAL_CONFIRMED", source_fragment_id=frag.id, created_at=NOW,
        )
    )
    db_session.commit()

    # Prove the blocker is present for the period.
    preview = build_period_close_preview(db_session, "2026-01")
    assert [b.blocker_type for b in preview.blockers] == [MISSING_ACCRUAL_BASIS]

    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.reconciliation == PASS
    assert result.diagnostics["reconciliation"]["unresolved_count"] == 0
    assert result.diagnostics["surfaces"]["exception_task_center"] == "ok"


# ---------------------------------------------------------------------------
# G0 repair, Blocker 2 — hardened private INPUT boundary at the Gate level.
# A symlinked expected/ dir, plan or baseline file, or any nested path
# resolving outside the private root must FAIL the Gate BEFORE its content
# could be parsed; the verdict and the private report location stay safe.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _prepare_contract_and_real_inputs(db_session, gate_root):
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    return contract


def test_input_real_files_inside_period_are_accepted(db_session, gate_root):
    """Task test 1: a normal real plan/baseline file inside the period is
    accepted (the PASS case already proves the full happy path)."""
    contract = _prepare_contract_and_real_inputs(db_session, gate_root)
    result = _run(db_session, gate_root)
    assert result.passed
    assert result.diagnostics["cutover_inputs"] == {
        "backfill_plan": "present", "cutover_baseline": "present",
    }


def test_input_expected_dir_symlink_outside_root_gate_fails(db_session, gate_root, tmp_path):
    """Task test 2: <period>/expected is a symlink to a directory OUTSIDE
    BEL_PRIVATE_DATA_ROOT -> FAIL (PRIVATE_INPUT_ESCAPE), never parsed."""
    contract = _prepare_contract_and_real_inputs(db_session, gate_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cutover-baseline.json").write_text(json.dumps({"entries": _contract_entries(contract)}))
    shutil.rmtree(gate_root / "2026-01" / "expected")
    os.symlink(outside, gate_root / "2026-01" / "expected")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_PRIVATE_INPUT_ESCAPE in result.reason_codes
    assert result.diagnostics["cutover_inputs"]["cutover_baseline"] == "escape"
    assert result.diagnostics["cutover_inputs"]["backfill_plan"] == "present"


def test_input_plan_symlink_outside_root_gate_fails(db_session, gate_root, tmp_path):
    """Task test 3: backfill-plan.json symlinks OUTSIDE the root -> FAIL."""
    _prepare_contract_and_real_inputs(db_session, gate_root)
    outside = tmp_path / "outside-plan.json"
    outside.write_text(json.dumps({"version": 1}))
    os.remove(gate_root / "2026-01" / "backfill-plan.json")
    os.symlink(outside, gate_root / "2026-01" / "backfill-plan.json")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_PRIVATE_INPUT_ESCAPE in result.reason_codes
    assert result.diagnostics["cutover_inputs"]["backfill_plan"] == "escape"
    assert result.diagnostics["cutover_inputs"]["cutover_baseline"] == "present"


def test_input_baseline_symlink_outside_root_gate_fails(db_session, gate_root, tmp_path):
    """Task test 4: cutover-baseline.json symlinks OUTSIDE the root ->
    FAIL, and the external content is never treated as valid input."""
    contract = _prepare_contract_and_real_inputs(db_session, gate_root)
    outside = tmp_path / "outside-baseline.json"
    outside.write_text(json.dumps({"entries": _contract_entries(contract)}))
    os.remove(gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    os.symlink(outside, gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_PRIVATE_INPUT_ESCAPE in result.reason_codes
    assert result.diagnostics["cutover_inputs"]["cutover_baseline"] == "escape"


def test_input_file_symlink_into_repository_gate_fails(db_session, gate_root):
    """Task test 5: a required FILE symlinking to an EXISTING repository
    file resolves inside the repo (outside the private root) -> ESCAPE —
    no repository content is ever treated as Gate input."""
    _prepare_contract_and_real_inputs(db_session, gate_root)
    os.remove(gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    os.symlink(_repo_root() / "pyproject.toml", gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_PRIVATE_INPUT_ESCAPE in result.reason_codes
    assert "reconciliation" not in result.diagnostics  # never parsed


def test_input_dir_symlink_into_repository_never_parsed(db_session, gate_root):
    """A directory component symlinking into the repository FAILs the Gate
    (MISSING when the repo dir has no matching file, ESCAPE when it does)
    — either way no outside content is parsed and no reconciliation runs."""
    _prepare_contract_and_real_inputs(db_session, gate_root)
    shutil.rmtree(gate_root / "2026-01" / "expected")
    os.symlink(_repo_root() / "docs", gate_root / "2026-01" / "expected")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert "reconciliation" not in result.diagnostics
    assert result.reconciliation == FAIL


def test_input_missing_remains_missing_fail(db_session, gate_root):
    """Task test 6: a genuinely missing file stays an ordinary
    missing-input FAIL (BACKFILL_PLAN_MISSING / BASELINE_MISSING), never
    confused with an escape."""
    contract = _make_contract(db_session)
    db_session.commit()
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.cutover_inputs == FAIL
    assert REASON_BASELINE_MISSING in result.reason_codes
    assert REASON_PRIVATE_INPUT_ESCAPE not in result.reason_codes


def test_rejected_external_baseline_is_never_parsed_or_reconciled(db_session, gate_root, tmp_path):
    """Task test 7: an escaping baseline whose content WOULD reconcile if
    read is still rejected at the boundary — it is never parsed and never
    reconciled (no reconciliation diagnostic, dimension unevaluated FAIL)."""
    contract = _prepare_contract_and_real_inputs(db_session, gate_root)
    outside = tmp_path / "evil-baseline.json"
    outside.write_text(json.dumps({"entries": _contract_entries(contract)}))
    os.remove(gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    os.symlink(outside, gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert REASON_PRIVATE_INPUT_ESCAPE in result.reason_codes
    assert "reconciliation" not in result.diagnostics
    assert result.reconciliation == FAIL  # unevaluated is never PASS


def test_input_escape_keeps_report_inside_private_root(db_session, gate_root, tmp_path):
    """Task tests 8+9: the private report is still written ONLY under
    BEL_PRIVATE_DATA_ROOT (never outside) even when an input escapes, and
    its content records the escape."""
    contract = _prepare_contract_and_real_inputs(db_session, gate_root)
    outside = tmp_path / "outside-baseline.json"
    outside.write_text(json.dumps({"entries": _contract_entries(contract)}))
    os.remove(gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    os.symlink(outside, gate_root / "2026-01" / "expected" / "cutover-baseline.json")
    result = _run(db_session, gate_root)
    assert not result.passed
    assert result.report_written
    report = gate_root / "reports" / "first-stage-cutover-gate-2026-01.json"
    assert report.exists()
    assert not (tmp_path / "reports").exists()
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["passed"] is False
    assert REASON_PRIVATE_INPUT_ESCAPE in body["reason_codes"]


# ---------------------------------------------------------------------------
# G0 repair, Blocker 3 — canonical postgresql+psycopg runtime contract.
# ---------------------------------------------------------------------------


def test_canonical_psycopg_runtime_identity_passes():
    """postgresql+psycopg:// (psycopg3) is the accepted canonical runtime
    — verified from engine metadata alone, no connection needed."""
    engine = make_engine("postgresql+psycopg://user:pass@localhost:5432/db")
    ok, reason = canonical_postgresql_runtime(engine)
    assert ok is True
    assert reason is None


def test_non_canonical_postgresql_drivers_rejected():
    """PostgreSQL dialect on any non-psycopg driver — psycopg2, asyncpg,
    and the bare postgresql:// default — is rejected with
    NON_CANONICAL_DATABASE_DRIVER. (Stub engines: the drivers need not be
    installed for the classification check.)"""
    for drivername, driver in (
        ("postgresql+psycopg2", "psycopg2"),
        ("postgresql+asyncpg", "asyncpg"),
        ("postgresql", "psycopg2"),  # bare postgresql:// defaults to psycopg2
    ):
        ok, reason = canonical_postgresql_runtime(_stub_engine(drivername, "postgresql", driver))
        assert ok is False, drivername
        assert reason == REASON_NON_CANONICAL_DATABASE_DRIVER, drivername


def test_sqlite_runtime_rejected_as_non_postgresql():
    engine = make_engine("sqlite://")
    ok, reason = canonical_postgresql_runtime(engine)
    assert ok is False
    assert reason == REASON_NON_POSTGRESQL


def test_gate_reports_non_canonical_driver_reason(db_session, gate_root):
    """A postgresql-dialect-but-not-psycopg runtime fails the Gate's
    runtime_schema dimension with NON_CANONICAL_DATABASE_DRIVER."""
    contract = _make_contract(db_session)
    _write_baseline(gate_root / "2026-01", _contract_entries(contract))
    db_session.commit()
    result = _run(
        db_session, gate_root,
        runtime=lambda s: RuntimeCheck(
            dialect_ok=False, schema_ok=False, dialect_reason_code=REASON_NON_CANONICAL_DATABASE_DRIVER,
        ),
    )
    assert not result.passed
    assert result.runtime_schema == FAIL
    assert REASON_NON_CANONICAL_DATABASE_DRIVER in result.reason_codes
