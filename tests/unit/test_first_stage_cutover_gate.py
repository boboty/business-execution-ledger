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
    REASON_NON_POSTGRESQL,
    REASON_RECONCILIATION_UNRESOLVED,
    REASON_SCHEMA_NOT_AT_HEAD,
    REASON_SURFACE_ERROR,
    RuntimeCheck,
    _schema_fingerprint,
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
