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

from bel.application.contract_business_ledger import get_contract_business_ledger
from bel.application.cutover_reconciliation import (
    OUTCOME_BEL_CORRECTED_LEGACY,
    OUTCOME_MATCH,
    OUTCOME_UNRESOLVED,
    _SnapshotBuilder,
    build_contract_execution_snapshot,
    reconcile,
)
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_matching import (
    confirm_sales_invoice_match,
    confirm_sales_payment_match,
    propose_sales_invoice_match,
    propose_sales_payment_match,
)
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceRepository,
    PaymentRepository,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
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
# Gate-fix round 2 — the M:N sales scope (P1 -> S1 and P2 -> S1)
# ---------------------------------------------------------------------------


def _make_sales_contract(session, sales_contract_no="S1"):
    frag = _make_fragment(session)
    return create_sales_contract_fact(
        session, our_entity="Buyer", sales_contract_no=sales_contract_no,
        fields={
            "customer": "Customer One", "currency": "CNY", "gross_amount": Decimal("500.00"),
            "contract_date": date(2026, 1, 1),
        },
        source_fragment_id=frag.id, created_at=NOW,
    ).sales_contract


def _link_procurement_to_sales(session, contract, sales_contract) -> None:
    frag = _make_fragment(session)
    add_procurement_sales_link(
        session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )


def _allocate_sales_invoice(session, sales_contract, amount):
    """A genuine R3b confirmation: SALES invoice -> SalesInvoiceAllocation
    -> this SalesContract. Never written through a repository bypass."""
    frag = _make_fragment(session)
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.SALES, invoice_type=None, invoice_no="INV-S1",
        digital_invoice_no=None, external_invoice_key="INV-S1", issue_date=date(2026, 1, 3), seller="Buyer",
        buyer="Customer One", net_amount=amount, tax_amount=Decimal("0"), gross_amount=amount,
        invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    proposal = propose_sales_invoice_match(
        session, invoice_id=invoice.id, sales_contract_ids=[sales_contract.id], created_at=NOW
    )
    confirm_sales_invoice_match(
        session, match_case_id=proposal.match_case.id, allocations=[(sales_contract.id, amount)], created_at=NOW
    )
    return invoice


def _allocate_incoming_receipt(session, sales_contract, amount):
    """A genuine R3b confirmation: IN payment -> SalesPaymentAllocation."""
    frag = _make_fragment(session)
    payment = Payment(
        id=uuid.uuid4(), transaction_date=date(2026, 1, 5), direction=PaymentDirection.IN, amount=amount,
        counterparty="Customer One", business_type=None, bank_reference="REF-S1", description=None,
        running_balance=None, source_fragment_id=frag.id, created_at=NOW, source_account_id="ACC-S1",
    )
    PaymentRepository(session).add(payment)
    session.flush()
    proposal = propose_sales_payment_match(
        session, payment_id=payment.id, sales_contract_ids=[sales_contract.id], created_at=NOW
    )
    confirm_sales_payment_match(
        session, match_case_id=proposal.match_case.id, allocations=[(sales_contract.id, amount)], created_at=NOW
    )
    return payment


def _build_many_to_one_scenario(db_session, *, invoice_amount=Decimal("200.00"), receipt_amount=Decimal("80.00")):
    """ONE SalesContract legitimately linked to TWO procurement Contracts
    (docs/V1-SCOPE.md section 5 item 1's primary axis) — the M:N shape
    that used to misfire duplicate_identity."""
    p1 = _make_contract(db_session, contract_no="P1")
    p2 = _make_contract(db_session, contract_no="P2")
    s1 = _make_sales_contract(db_session)
    _link_procurement_to_sales(db_session, p1, s1)
    _link_procurement_to_sales(db_session, p2, s1)
    invoice = _allocate_sales_invoice(db_session, s1, invoice_amount)
    payment = _allocate_incoming_receipt(db_session, s1, receipt_amount)
    db_session.commit()
    return p1, p2, s1, invoice, payment


def _sales_key(sales_contract):
    return f"our_entity={sales_contract.our_entity}|sales_contract_no={sales_contract.sales_contract_no}"


def _link_key(contract, sales_contract):
    return (
        f"procurement_sales_link:contract_no={contract.contract_no}|counterparty={contract.counterparty}"
        f"|{_sales_key(sales_contract)}"
    )


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


def test_14_business_string_numeric_lookalikes_never_normalized_equal(db_session):
    """Adversarial regression (gate-fix section 4): a business string
    field must NEVER be coerced through Decimal — "00123" and "123" stay
    distinct even though both parse as numbers."""
    contract = _make_contract(db_session, contract_no="C-STR")
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_MATCH)
    # contract_type happens to look numeric-ish in this adversarial case.
    entries[0]["expected"]["contract_type"] = "00" + (contract.contract_type or "")
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed  # "00出口..." must never equal "出口..." via Decimal coercion

    from bel.application.cutover_reconciliation import _normalize

    assert _normalize({"contract_no": "00123"}) != _normalize({"contract_no": "123"})
    assert _normalize({"gross_amount": "100"}) == _normalize({"gross_amount": "100.00"})


# ---------------------------------------------------------------------------
# Gate-fix round 2 — one SalesContract linked to N procurement Contracts
# (P1 -> S1, P2 -> S1) must never be reported as duplicate_identity
# ---------------------------------------------------------------------------


def test_many_to_one_sales_scope_is_observed_once_not_as_duplicate_identity(db_session):
    p1, p2, s1, invoice, payment = _build_many_to_one_scenario(db_session)

    # Precondition: the R4 primary axis really does project the SAME
    # scope twice — one row per procurement Contract.
    ledger = get_contract_business_ledger(db_session)
    assert len(ledger.rows) == 2
    assert [len(row.sales_scopes) for row in ledger.rows] == [1, 1]
    assert {scope.sales_contract.id for row in ledger.rows for scope in row.sales_scopes} == {s1.id}

    snapshot = build_contract_execution_snapshot(db_session)
    assert not any("duplicate_identity" in key for key in snapshot)

    # Each scope-level fact appears under exactly ONE business key.
    assert len([k for k in snapshot if k.startswith("sales_contract:")]) == 1
    assert len([k for k in snapshot if k.startswith("sales_invoice_allocation:")]) == 1
    assert len([k for k in snapshot if k.startswith("incoming_receipt_allocation:")]) == 1
    # ...while the procurement axis keeps one entry per procurement row.
    assert len([k for k in snapshot if k.startswith("procurement_sales_link:")]) == 2
    assert f"sales_invoice_allocation:{_sales_key(s1)}|invoice={invoice.external_invoice_key}" in snapshot


def test_many_to_one_sales_scope_reconciles_against_a_complete_baseline(db_session):
    """End-to-end: the same M:N shape still has to reconcile, with every
    scope-level fact adjudicated exactly once."""
    p1, p2, s1, _invoice, payment = _build_many_to_one_scenario(db_session)
    entries = _contract_entries(p1, OUTCOME_MATCH) + _contract_entries(p2, OUTCOME_MATCH)
    entries.append(
        {
            "key": f"sales_contract:{_sales_key(s1)}",
            "expected": {
                "customer": s1.customer, "currency": s1.currency, "gross_amount": str(s1.gross_amount),
                "contract_date": s1.contract_date.isoformat(),
            },
            "outcome": OUTCOME_MATCH,
        }
    )
    for contract in (p1, p2):
        entries.append({"key": _link_key(contract, s1), "expected": {"current": True}, "outcome": OUTCOME_MATCH})
    entries.append(
        {
            "key": f"sales_invoice_allocation:{_sales_key(s1)}|invoice=INV-S1",
            "expected": {"allocated_gross_amount": "200.00"}, "outcome": OUTCOME_MATCH,
        }
    )
    entries.append(
        {
            "key": (
                f"incoming_receipt_allocation:{_sales_key(s1)}|payment=source_account_id={payment.source_account_id}"
                f"|transaction_date={payment.transaction_date.isoformat()}|direction={payment.direction}"
                f"|amount={payment.amount}|bank_reference={payment.bank_reference}"
            ),
            "expected": {"allocated_amount": "80.00"}, "outcome": OUTCOME_MATCH,
        }
    )

    result = reconcile(db_session, {"entries": entries})
    assert result.passed
    assert result.unresolved_count == 0


def test_genuine_contract_identity_collision_stays_unresolved(db_session):
    """The dedupe is ONE namespace wide: two Contracts sharing a business
    key is a real collision — UNRESOLVED even when their content happens
    to agree, and a baseline can never pre-adjudicate it away."""
    c1 = _make_contract(db_session, contract_no="P1", gross_amount=Decimal("100.00"))
    c2 = _make_contract(db_session, contract_no="P1", gross_amount=Decimal("999.00"))
    db_session.commit()

    snapshot = build_contract_execution_snapshot(db_session)
    collisions = [k for k in snapshot if k.startswith("unresolved:duplicate_identity:")]
    assert collisions  # the procurement axis is never deduplicated
    assert _contract_key(c1) not in snapshot  # == _contract_key(c2)

    entries = _contract_entries(c1, OUTCOME_MATCH)
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed
    assert any(e.key in collisions for e in result.entries if e.outcome == OUTCOME_UNRESOLVED)


def test_genuine_contract_identity_collision_with_identical_content_stays_unresolved(db_session):
    """Adversarial: even a byte-identical duplicate Contract is a
    duplicate IDENTITY, not a repeat observation — only the
    sales-scope namespaces are ever collapsed."""
    _make_contract(db_session, contract_no="P1", gross_amount=Decimal("100.00"))
    _make_contract(db_session, contract_no="P1", gross_amount=Decimal("100.00"))
    db_session.commit()

    snapshot = build_contract_execution_snapshot(db_session)
    assert [k for k in snapshot if k.startswith("unresolved:duplicate_identity:")]


def test_sales_scope_key_with_conflicting_content_is_never_deduped():
    """The collapse is content-conditioned, not prefix-conditioned: the
    same sales-scope business key carrying DIFFERENT fact content is a
    duplicate identity and stays UNRESOLVED — never a silent
    "first occurrence wins"."""
    key = "sales_contract:our_entity=Buyer|sales_contract_no=S1"
    builder = _SnapshotBuilder()
    builder.add(key, {"customer": "Customer One"})
    builder.add(key, {"customer": "Customer Two"})
    snapshot = builder.build()
    assert len([k for k in snapshot if k.startswith("unresolved:duplicate_identity:")]) == 2
    assert key not in snapshot

    # Positive control: identical content collapses to ONE observation.
    builder = _SnapshotBuilder()
    builder.add(key, {"customer": "Customer One"})
    builder.add(key, {"customer": "Customer One"})
    assert builder.build() == {key: {"customer": "Customer One"}}


# ---------------------------------------------------------------------------
# Gate-fix — backfill unresolved work is included in the Gate (section 1)
# ---------------------------------------------------------------------------


def test_backfill_task_blocks_reconciliation_even_unmapped_to_a_contract(db_session):
    """An OPEN backfill Task that cannot be mapped to any Contract (a
    Payment with no source_account_id at all) must still make
    reconciliation UNRESOLVED — never silently absent from the Gate."""
    from bel.application.cutover_backfill import backfill_payment_transactions
    from bel.adapters.pdf.cmb_bank_statement import ParsedBankTransaction

    doc_id = uuid.uuid4()
    from bel.infrastructure.persistence.repositories import EvidenceRepository as _ER

    _ER(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="f" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    txn = ParsedBankTransaction(
        page_index=0, transaction_index=0, raw_data={}, transaction_date=date(2026, 1, 1), business_type="转账",
        bank_reference="REF-ORPHAN", signed_amount=Decimal("50.00"), running_balance=Decimal("1000.00"),
        counterparty="Someone", description=None,
    )
    outcome = backfill_payment_transactions(db_session, [txn], source_account_id=None, document_id=doc_id, created_at=NOW)
    assert outcome.tasks

    db_session.commit()
    result = reconcile(db_session, {"entries": []})
    assert not result.passed
    assert any("backfill_task" in e.key for e in result.entries if e.outcome == OUTCOME_UNRESOLVED)


def test_backfill_task_resolution_clears_reconciliation_block(db_session):
    from bel.application.cutover_backfill import backfill_contracts
    from bel.domain.exception import ExceptionStatus
    from bel.infrastructure.persistence.repositories import ExceptionRepository
    from pathlib import Path
    import tempfile
    import openpyxl

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "ledger.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "报关出口购销合同"
        ws.append(["Title"])
        ws.append(["序号", "合同编码", "卖方", "买方", "金额"])
        ws.append([1, "C-TASK", None, "BuyerX", 100])
        wb.save(path)
        backfill_contracts(db_session, path)

    exception_repo = ExceptionRepository(db_session)
    open_task = next(e for e in exception_repo.list_open())
    db_session.commit()
    unresolved_before = reconcile(db_session, {"entries": []})
    assert not unresolved_before.passed

    exception_repo.update_status(open_task.id, ExceptionStatus.RESOLVED)
    db_session.commit()
    result_after = reconcile(db_session, {"entries": []})
    assert not any(e.key.startswith("unresolved:backfill_task") for e in result_after.entries)


# ---------------------------------------------------------------------------
# Gate-fix round 3 — duplicate/pseudo-key baseline entries are never
# resolved by "last one wins" dict construction; both must FAIL.
# ---------------------------------------------------------------------------


def test_duplicate_baseline_key_unresolved_then_match_fails(db_session):
    """A. The SAME key appears twice: once UNRESOLVED, once MATCH. A
    dict-comprehension would let the later MATCH entry silently win and
    could even PASS if it happens to agree with actual — that must never
    happen. Any duplicate key is an invalid/ambiguous baseline: FAIL."""
    contract = _make_contract(db_session)
    db_session.commit()
    key = _contract_key(contract)
    entries = [
        {"key": key, "expected": {}, "outcome": OUTCOME_UNRESOLVED},
        {"key": key, "expected": _expected_contract_value(contract), "outcome": OUTCOME_MATCH},
        {"key": _unresolved_indicator_key(contract), "expected": {"has_unresolved": False}, "outcome": OUTCOME_MATCH},
    ]
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed
    assert any(e.key == key and e.outcome == OUTCOME_UNRESOLVED for e in result.entries)


def test_duplicate_baseline_key_match_expected_a_then_match_expected_b_fails(db_session):
    """B. The SAME key appears twice, both claiming MATCH, but with
    CONFLICTING ``expected`` payloads. Whichever one BEL's actual state
    happens to agree with, the baseline itself is ambiguous — FAIL."""
    contract = _make_contract(db_session)
    db_session.commit()
    key = _contract_key(contract)
    conflicting_expected = dict(_expected_contract_value(contract))
    conflicting_expected["gross_amount"] = "1.00"
    entries = [
        {"key": key, "expected": _expected_contract_value(contract), "outcome": OUTCOME_MATCH},
        {"key": key, "expected": conflicting_expected, "outcome": OUTCOME_MATCH},
        {"key": _unresolved_indicator_key(contract), "expected": {"has_unresolved": False}, "outcome": OUTCOME_MATCH},
    ]
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed
    assert any(e.key == key and e.outcome == OUTCOME_UNRESOLVED for e in result.entries)


def test_duplicate_baseline_key_not_double_reported(db_session):
    """A duplicate baseline key produces exactly ONE UNRESOLVED
    reconciliation entry for that key — never also a second "actual key
    the baseline never adjudicated" entry for the same key."""
    contract = _make_contract(db_session)
    db_session.commit()
    key = _contract_key(contract)
    entries = [
        {"key": key, "expected": _expected_contract_value(contract), "outcome": OUTCOME_MATCH},
        {"key": key, "expected": _expected_contract_value(contract), "outcome": OUTCOME_MATCH},
        {"key": _unresolved_indicator_key(contract), "expected": {"has_unresolved": False}, "outcome": OUTCOME_MATCH},
    ]
    result = reconcile(db_session, {"entries": entries})
    matching_entries = [e for e in result.entries if e.key == key]
    assert len(matching_entries) == 1
    assert matching_entries[0].outcome == OUTCOME_UNRESOLVED


def test_baseline_key_prefixed_unresolved_is_never_silently_skipped(db_session):
    """A baseline entry naming its OWN key with the ``unresolved:``
    prefix must never simply vanish via a silent ``continue`` — it
    surfaces as its own UNRESOLVED entry and fails the run."""
    contract = _make_contract(db_session)
    db_session.commit()
    entries = _contract_entries(contract, OUTCOME_MATCH)
    entries.append({"key": "unresolved:smuggled:1", "expected": {}, "outcome": OUTCOME_MATCH})
    result = reconcile(db_session, {"entries": entries})
    assert not result.passed
    assert any(e.key == "unresolved:smuggled:1" and e.outcome == OUTCOME_UNRESOLVED for e in result.entries)
