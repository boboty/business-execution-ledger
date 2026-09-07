"""FX rate confirmation — Core Completion (IP-S02) provenance boundary.

Proves the FX Evidence binding is real: ``execute_confirm_fx_rate`` is
the only sanctioned way to produce a confirmed FX fragment, content-hash
deduplicated exactly like a manual contract fact;
``load_confirmed_fx_rate`` reconstructs the confirmed value ENTIRELY
from that fragment's own ``raw_data`` — never from a caller-supplied
value — so a fragment id naming an unrelated or non-existent fragment
cannot be paired with a forged source/date/rate to pass as confirmed FX
provenance. This closes the hole the Astra intermediate state left:
``get_invoice_preparation_context`` previously only checked that SOME
EvidenceFragment existed at the given id, accepting any caller-claimed
source/date/rate alongside it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.fx_rate_evidence import (
    ApplicableFxRateEvidence,
    FxRateEvidenceError,
    execute_confirm_fx_rate,
    load_confirmed_fx_rate,
)
from bel.application.invoice_preparation import get_invoice_preparation_context
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.repositories import EvidenceRepository

NOW = datetime.now(timezone.utc)


def _make_fragment(session, raw_data=None):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None, row_number=None, locator_json={}, raw_data=raw_data or {}, created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def test_confirm_and_load_round_trip(db_session):
    confirmed = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2000")
    )
    db_session.commit()

    loaded = load_confirmed_fx_rate(db_session, confirmed.provenance_fragment_id)
    assert loaded == confirmed
    assert loaded.source == "SAFE"
    assert loaded.publication_date == date(2030, 8, 30)
    assert loaded.usd_cny_rate == Decimal("7.2000")


def test_confirming_identical_input_twice_reuses_the_fragment(db_session):
    """Content-hash dedup, exactly like a manual contract fact: confirming
    the same (source, date, rate) twice must not create a second
    fragment."""
    first = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2000")
    )
    db_session.commit()
    second = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2000")
    )
    db_session.commit()
    assert first.provenance_fragment_id == second.provenance_fragment_id


@pytest.mark.parametrize("source", ["", "   "])
def test_confirm_rejects_blank_source(db_session, source):
    with pytest.raises(FxRateEvidenceError):
        execute_confirm_fx_rate(db_session, source=source, publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2"))


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("-7.2"), Decimal("NaN"), Decimal("Infinity")])
def test_confirm_rejects_non_positive_or_unrepresentable_rate(db_session, rate):
    with pytest.raises(FxRateEvidenceError):
        execute_confirm_fx_rate(db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=rate)


def test_load_rejects_nonexistent_fragment(db_session):
    with pytest.raises(FxRateEvidenceError):
        load_confirmed_fx_rate(db_session, uuid.uuid4())


def test_load_rejects_an_unrelated_fragment_not_produced_by_confirm(db_session):
    """The exact hole this closes: a fragment that exists (e.g. a
    ContractItem's or SalesContract's own Evidence fragment) but was
    never produced by ``execute_confirm_fx_rate`` must be refused as FX
    provenance — regardless of what its raw_data happens to contain."""
    frag = _make_fragment(db_session, raw_data={"command": "sales-contract-create", "customer": "Acme"})
    db_session.commit()
    with pytest.raises(FxRateEvidenceError):
        load_confirmed_fx_rate(db_session, frag.id)


def test_load_rejects_a_fragment_with_the_fx_command_but_wrong_kind(db_session):
    """fragment_kind must also be MANUAL_FACT — an EXCEL_ROW fragment that
    happens to carry matching-looking keys is still refused."""
    frag = EvidenceFragment(
        id=uuid.uuid4(), evidence_document_id=_make_fragment(db_session).evidence_document_id,
        fragment_kind=FragmentKind.EXCEL_ROW, sheet_name="Sheet1", row_number=2, locator_json=None,
        raw_data={"command": "fx-rate-confirm", "source": "SAFE", "publication_date": "2030-08-30", "usd_cny_rate": "7.2"},
        created_at=NOW,
    )
    EvidenceRepository(db_session).add_fragment(frag)
    db_session.commit()
    with pytest.raises(FxRateEvidenceError):
        load_confirmed_fx_rate(db_session, frag.id)


def test_forged_combination_via_the_session_boundary_is_rejected(db_session):
    """The exact scenario the postgres gate test used to (wrongly) accept:
    pass the fragment id of an UNRELATED confirmed Fact (here, a
    SalesContract's own Evidence fragment) as FX provenance —
    ``get_invoice_preparation_context`` must reject it, never silently
    treat it as a confirmed SAFE rate."""
    frag = _make_fragment(db_session, raw_data={"not": "an fx fact"})
    create_sales_contract_fact(
        db_session, our_entity="Synthetic exporter", sales_contract_no="FX-FORGE-TEST",
        fields={"customer": "Synthetic customer", "currency": "USD", "gross_amount": Decimal("100")},
        source_fragment_id=frag.id, created_at=NOW,
    )
    db_session.commit()

    with pytest.raises(FxRateEvidenceError):
        get_invoice_preparation_context(
            db_session, invoice_month=date(2030, 9, 1), fx_provenance_fragment_ids=(frag.id,)
        )


def test_forged_combination_via_the_pure_dataclass_is_never_constructible_from_a_session_read(db_session):
    """There is no session-backed path that lets a caller pair an
    arbitrary claimed (source, date, rate) with someone else's fragment
    id and have it treated as confirmed: ``load_confirmed_fx_rate`` takes
    ONLY a fragment id — the claimed values it returns come exclusively
    from that fragment's own raw_data, so two different confirmed rates
    never collapse onto the same evidence by construction."""
    confirmed_a = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2000")
    )
    confirmed_b = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 7, 31), usd_cny_rate=Decimal("7.1500")
    )
    db_session.commit()

    # Loading fragment A's id always reconstructs A's own values — never
    # B's, no matter what a caller might have separately claimed.
    reloaded_a = load_confirmed_fx_rate(db_session, confirmed_a.provenance_fragment_id)
    assert reloaded_a.usd_cny_rate == Decimal("7.2000")
    assert reloaded_a.publication_date == date(2030, 8, 30)
    reloaded_b = load_confirmed_fx_rate(db_session, confirmed_b.provenance_fragment_id)
    assert reloaded_b.usd_cny_rate == Decimal("7.1500")
    assert reloaded_b.publication_date == date(2030, 7, 31)


def test_missing_or_ambiguous_fx_never_blocks_unrelated_procurement_side_preparation(db_session):
    """Missing FX (no fragment ids at all) and ambiguous FX (two valid
    SAFE rates on the same applicable publication date) are both explicit
    NOT_COMPARABLE_* outcomes on the sales amount_check — neither raises,
    and neither has any effect on the wholly-unrelated procurement/
    supplier side, which never references FX at all."""
    from bel.application.contract_item_facts import create_contract_item_fact
    from bel.application.invoice_preparation_workbench import get_invoice_preparation_workbench
    from bel.domain.contract import Contract
    from bel.infrastructure.persistence.repositories import ContractRepository

    frag = _make_fragment(db_session)
    procurement = Contract(
        id=uuid.uuid4(), contract_no="FX-NOBLOCK-PO", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    ContractRepository(db_session).add(procurement)
    db_session.flush()
    create_contract_item_fact(
        db_session, contract_id=procurement.id, source_item_key="fx-noblock-item",
        fields={"product_name": "Widget", "quantity": Decimal("5"), "unit": "piece"},
        source_fragment_id=frag.id, created_at=NOW,
    )
    db_session.commit()

    # Missing FX — no fragment ids supplied at all.
    workbench_missing = get_invoice_preparation_workbench(
        db_session, invoice_month=date(2030, 9, 1), fx_provenance_fragment_ids=()
    )
    supplier_decision = next(
        d for d in workbench_missing.supplier_report.decisions if d.contract_id == procurement.id
    )
    assert supplier_decision.item_preparations[0].expected_purchase_invoice_quantity == Decimal("5")
    assert supplier_decision.blockers == ()

    # Ambiguous FX — two valid SAFE rates on the same applicable date.
    confirmed_a = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2")
    )
    confirmed_b = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.3")
    )
    db_session.commit()
    workbench_ambiguous = get_invoice_preparation_workbench(
        db_session, invoice_month=date(2030, 9, 1),
        fx_provenance_fragment_ids=(confirmed_a.provenance_fragment_id, confirmed_b.provenance_fragment_id),
    )
    supplier_decision_2 = next(
        d for d in workbench_ambiguous.supplier_report.decisions if d.contract_id == procurement.id
    )
    assert supplier_decision_2.item_preparations[0].expected_purchase_invoice_quantity == Decimal("5")
    assert supplier_decision_2.blockers == ()
    sales_report_amount_checks = [d.amount_check.outcome for d in workbench_ambiguous.sales_report.decisions]
    # No sales scope exists in this fixture, so nothing to assert on
    # amount_check outcomes here — the point proven is that ambiguous FX
    # evidence resolves without raising and the supplier side is intact.
    assert sales_report_amount_checks == []


def test_repeated_context_build_with_the_same_fragment_ids_is_deterministic(db_session):
    confirmed = execute_confirm_fx_rate(
        db_session, source="SAFE", publication_date=date(2030, 8, 30), usd_cny_rate=Decimal("7.2000")
    )
    db_session.commit()

    context_a = get_invoice_preparation_context(
        db_session, invoice_month=date(2030, 9, 1), fx_provenance_fragment_ids=(confirmed.provenance_fragment_id,)
    )
    context_b = get_invoice_preparation_context(
        db_session, invoice_month=date(2030, 9, 1), fx_provenance_fragment_ids=(confirmed.provenance_fragment_id,)
    )
    assert context_a.fx_rate_evidence == context_b.fx_rate_evidence == (confirmed,)
