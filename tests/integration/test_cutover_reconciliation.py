"""Phase 2D.1-R5 — Cutover Reconciliation rehearsal.

Covers the test matrix from the R5 spec section 56: MATCH/BEL_CORRECTED_LEGACY/
UNRESOLVED outcomes, the UNRESOLVED=0 gate, internal-UUID/insertion-order
independence, extra/missing in-scope facts, and Decimal-equivalence
normalization without fuzzy tolerance.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.cutover_reconciliation import (
    OUTCOME_BEL_CORRECTED_LEGACY,
    OUTCOME_MATCH,
    OUTCOME_UNRESOLVED,
    build_contract_execution_snapshot,
    reconcile,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import ContractRepository, EvidenceRepository

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _make_fragment(session):
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


def _make_contract(session, contract_no="C-RECON", counterparty="Supplier", gross_amount=Decimal("1000.00")):
    frag = _make_fragment(session)
    contract = Contract(
        id=uuid.uuid4(), contract_no=contract_no, contract_type="出口报关购销合同", counterparty=counterparty,
        buyer="Buyer", gross_amount=gross_amount, currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _contract_key(contract):
    return f"contract:contract_no={contract.contract_no}|counterparty={contract.counterparty}"


def _unresolved_indicator_key(contract):
    return f"unresolved_indicator:contract_no={contract.contract_no}|counterparty={contract.counterparty}"


def _contract_entries(contract, outcome):
    """Every contract row also produces an unresolved_indicator entry in
    the snapshot (section 30) — a complete baseline for a bare contract
    (no items/shipments/etc.) needs both adjudicated."""
    return [
        {"key": _contract_key(contract), "expected": _expected_contract_value(contract), "outcome": outcome},
        {"key": _unresolved_indicator_key(contract), "expected": {"has_unresolved": False}, "outcome": outcome},
    ]


def _expected_contract_value(contract):
    return {
        "contract_type": contract.contract_type, "buyer": contract.buyer,
        "gross_amount": str(contract.gross_amount), "currency": contract.currency,
        "contract_date": contract.contract_date.isoformat(),
    }


# ---------------------------------------------------------------------------
# 1/2 — MATCH
# ---------------------------------------------------------------------------


def test_1_match_actual_equals_expected_passes(db_session):
    contract = _make_contract(db_session)
    db_session.commit()
    baseline = {"entries": _contract_entries(contract, OUTCOME_MATCH)}
    result = reconcile(db_session, baseline)
    assert result.passed
    assert result.unresolved_count == 0


def test_2_match_mismatch_fails(db_session):
    contract = _make_contract(db_session)
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_MATCH)
    entries[0]["expected"]["gross_amount"] = "999999.00"
    baseline = {"entries": entries}
    result = reconcile(db_session, baseline)
    assert not result.passed
    assert result.unresolved_count == 1


# ---------------------------------------------------------------------------
# 3/4 — BEL_CORRECTED_LEGACY
# ---------------------------------------------------------------------------


def test_3_bel_corrected_legacy_actual_equals_adjudicated_expected_passes(db_session):
    contract = _make_contract(db_session, gross_amount=Decimal("500.00"))
    db_session.commit()
    baseline = {"entries": _contract_entries(contract, OUTCOME_BEL_CORRECTED_LEGACY)}
    result = reconcile(db_session, baseline)
    assert result.passed


def test_4_bel_corrected_legacy_actual_differs_from_adjudicated_fails(db_session):
    contract = _make_contract(db_session, gross_amount=Decimal("500.00"))
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_BEL_CORRECTED_LEGACY)
    entries[0]["expected"]["gross_amount"] = "1.00"
    baseline = {"entries": entries}
    result = reconcile(db_session, baseline)
    assert not result.passed


# ---------------------------------------------------------------------------
# 5/6 — UNRESOLVED gate
# ---------------------------------------------------------------------------


def test_5_any_unresolved_fails_whole_run(db_session):
    c1 = _make_contract(db_session, contract_no="C-1")
    c2 = _make_contract(db_session, contract_no="C-2")
    db_session.commit()
    entries = _contract_entries(c1, OUTCOME_MATCH)
    c2_entries = _contract_entries(c2, OUTCOME_MATCH)
    c2_entries[0]["outcome"] = "UNRESOLVED"
    baseline = {"entries": entries + c2_entries}
    result = reconcile(db_session, baseline)
    assert not result.passed
    assert result.unresolved_count == 1


def test_6_no_unresolved_all_resolved_correct_passes(db_session):
    c1 = _make_contract(db_session, contract_no="C-1")
    c2 = _make_contract(db_session, contract_no="C-2")
    db_session.commit()
    baseline = {"entries": _contract_entries(c1, OUTCOME_MATCH) + _contract_entries(c2, OUTCOME_BEL_CORRECTED_LEGACY)}
    result = reconcile(db_session, baseline)
    assert result.passed


# ---------------------------------------------------------------------------
# 7/8 — internal UUIDs and insertion order never affect comparison
# ---------------------------------------------------------------------------


def test_7_internal_uuids_never_enter_the_snapshot(db_session):
    contract = _make_contract(db_session)
    db_session.commit()
    snapshot = build_contract_execution_snapshot(db_session)
    key = _contract_key(contract)
    assert key in snapshot
    assert str(contract.id) not in str(snapshot[key])


def test_8_different_insertion_order_same_business_facts_passes(db_session):
    c1 = _make_contract(db_session, contract_no="C-A")
    c2 = _make_contract(db_session, contract_no="C-B")
    db_session.commit()
    # Deliberately listed in the OPPOSITE order from creation.
    baseline = {"entries": _contract_entries(c2, OUTCOME_MATCH) + _contract_entries(c1, OUTCOME_MATCH)}
    result = reconcile(db_session, baseline)
    assert result.passed


# ---------------------------------------------------------------------------
# 9/10 — extra / missing in-scope facts
# ---------------------------------------------------------------------------


def test_9_actual_extra_in_scope_fact_fails(db_session):
    c1 = _make_contract(db_session, contract_no="C-1")
    _make_contract(db_session, contract_no="C-2")  # never adjudicated in baseline
    db_session.commit()
    baseline = {"entries": _contract_entries(c1, OUTCOME_MATCH)}
    result = reconcile(db_session, baseline)
    assert not result.passed


def test_10_expected_missing_required_fact_fails(db_session):
    c1 = _make_contract(db_session, contract_no="C-1")
    db_session.commit()
    fake_key = "contract:contract_no=DOES-NOT-EXIST|counterparty=Nobody"
    entries = _contract_entries(c1, OUTCOME_MATCH)
    entries.append({"key": fake_key, "expected": {"gross_amount": "1"}, "outcome": OUTCOME_MATCH})
    baseline = {"entries": entries}
    result = reconcile(db_session, baseline)
    assert not result.passed


# ---------------------------------------------------------------------------
# 11 — out-of-scope legacy field is not automatically a discrepancy
# ---------------------------------------------------------------------------


def test_11_snapshot_never_includes_out_of_scope_categories(db_session):
    """Period Close projected Decision / outbound eligibility / Exception
    Center resolution state must never appear in the snapshot (section
    30's explicit exclusion) — structural check on the key namespace."""
    contract = _make_contract(db_session)
    db_session.commit()
    snapshot = build_contract_execution_snapshot(db_session)
    for key in snapshot:
        assert not key.startswith("period_close_decision:")
        assert not key.startswith("outbound_invoice_eligibility:")
        assert not key.startswith("exception_resolution:")


# ---------------------------------------------------------------------------
# 12/13 — Decimal normalization, no fuzzy tolerance
# ---------------------------------------------------------------------------


def test_12_decimal_formatting_equivalent_normalizes_equal(db_session):
    contract = _make_contract(db_session, gross_amount=Decimal("100.00"))
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_MATCH)
    entries[0]["expected"]["gross_amount"] = "100"  # different formatting, same value
    result = reconcile(db_session, {"entries": entries})
    assert result.passed


def test_13_real_decimal_difference_fails_no_fuzzy_tolerance(db_session):
    contract = _make_contract(db_session, gross_amount=Decimal("100.00"))
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_MATCH)
    entries[0]["expected"]["gross_amount"] = "100.01"  # one cent off — must still FAIL
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed
