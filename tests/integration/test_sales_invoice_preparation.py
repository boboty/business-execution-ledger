"""Phase 2D.3-F1a — SALES_INVOICE_PREPARATION rule foundation.

Covers the frozen three-required-input rule layer over the F0 fact
context: explicit blockers when a required input is missing, the M:N
shipment-judgment deferral (never any/all under multiple links),
customer only from SalesContract.customer (and never a fourth blocker),
the Fact -> Decision layering (read-only, pure), and the reserved
(empty) 一致性校验 seam. Independently synthetic data throughout.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SalesScopeContext,
    SalesScopeLinkedProcurementContract,
    SupplierScopeContext,
)
from bel.application.sales_invoice_preparation import (
    REQUIRED_INPUT_ORDER,
    SALES_INVOICE_CONSISTENCY_CHECK_NAMES,
    SalesInvoicePreparationDecision,
    SalesPreparationBlockerCode,
    SalesPreparationDecisionStatus,
    SalesPreparationRequiredInput,
    evaluate_sales_invoice_preparation,
    evaluate_sales_invoice_preparation_from_context,
)
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.procurement_sales_link import ProcurementSalesLink, ProcurementSalesLinkCorrection
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    ProcurementSalesLinkRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)


def _make_fragment(session):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    EvidenceRepository(session).add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={},
        raw_data={},
        created_at=NOW,
    )
    EvidenceRepository(session).add_fragment(frag)
    session.flush()
    return frag


def _make_contract(session, fragment_id, contract_no, counterparty="Supplier"):
    contract = Contract(
        id=uuid.uuid4(),
        contract_no=contract_no,
        contract_type=None,
        counterparty=counterparty,
        buyer="Our Own Entity",
        gross_amount=Decimal("1000.00"),
        currency="CNY",
        contract_date=date(2026, 1, 1),
        current_source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


def _make_sales_contract(session, fragment_id, sales_contract_no, fields=None):
    return create_sales_contract_fact(
        session,
        our_entity="Our Own Entity",
        sales_contract_no=sales_contract_no,
        fields=fields or {},
        source_fragment_id=fragment_id,
        created_at=NOW,
    ).sales_contract


def _make_shipment(session, contract, fragment_id, external_reference):
    shipment_repo = ShipmentRepository(session)
    from bel.domain.shipment import ShipmentRevision, ShipmentRevisionType

    anchor_id = uuid.uuid4()
    shipment_repo.create_anchor(
        id=anchor_id,
        contract_id=contract.id,
        external_reference=external_reference,
        execution_date=date(2031, 2, 1),
        created_at=NOW,
    )
    shipment_repo.create_initial_revision(
        ShipmentRevision(
            id=uuid.uuid4(),
            shipment_id=anchor_id,
            revision_type=ShipmentRevisionType.INITIAL,
            contract_item_id=None,
            quantity=Decimal("5"),
            source_fragment_id=fragment_id,
            superseded_by_revision_id=None,
            created_at=NOW,
        )
    )
    session.flush()
    return shipment_repo.get(anchor_id)


def _link(session, contract, sales_contract, fragment):
    return add_procurement_sales_link(
        session,
        procurement_contract_id=contract.id,
        sales_contract_id=sales_contract.id,
        source_fragment_id=fragment.id,
        confirmation_type="AUTO_CONFIRMED",
        created_at=NOW,
    ).link


def _invalidate_link(session, link_id, fragment):
    """Pure relationship invalidation — no replacement episode."""
    ProcurementSalesLinkRepository(session).add_correction_if_uncorrected(
        ProcurementSalesLinkCorrection(
            id=uuid.uuid4(),
            superseded_link_id=link_id,
            replacement_link_id=None,
            source_fragment_id=fragment.id,
            confirmation_type="HUMAN_CONFIRMED",
            created_at=NOW,
        )
    )


# ---------------------------------------------------------------------------
# The three required inputs
# ---------------------------------------------------------------------------


def test_three_required_inputs_present_single_link_with_shipment(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-1")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-1", fields={"customer": "Customer A"})
    _link(db_session, contract, sales_contract, frag)
    shipment = _make_shipment(db_session, contract, frag.id, "SHIP-F1A-1")
    db_session.commit()

    report = evaluate_sales_invoice_preparation(db_session)
    assert len(report.decisions) == 1
    decision = report.decisions[0]

    assert decision.sales_contract_id == sales_contract.id
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()

    assert [ri.name for ri in decision.required_inputs] == list(REQUIRED_INPUT_ORDER)
    sales_input, link_input, shipment_input = decision.required_inputs
    assert sales_input.present and sales_input.source_fact_ids == (sales_contract.id,)
    assert link_input.present and link_input.source_fact_ids == (contract.id,)
    assert shipment_input.present and shipment_input.source_fact_ids == (shipment.id,)

    # customer comes only from SalesContract.customer.
    assert decision.customer == "Customer A"


def test_missing_link_emits_explicit_blocker_and_shipment_not_judged(db_session):
    frag = _make_fragment(db_session)
    _make_sales_contract(db_session, frag.id, "SC-F1A-2")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
    codes = [b.code for b in decision.blockers]
    # Exactly the missing required input is named — no invented extra
    # blockers, and no shipment claim (nothing was checked).
    assert codes == [SalesPreparationBlockerCode.NO_CURRENT_PROCUREMENT_LINK]
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "NOT_JUDGED_NO_LINKED_CONTRACT"
    assert shipment_input.source_fact_ids == ()


def test_superseded_link_counts_as_missing(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-2")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-3")
    link = _link(db_session, contract, sales_contract, frag)
    _invalidate_link(db_session, link.id, frag)
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    # Current links only — a superseded episode is not a current linked
    # procurement Contract (repository predicate reused, never re-derived).
    assert decision.status == SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
    assert [b.code for b in decision.blockers] == [SalesPreparationBlockerCode.NO_CURRENT_PROCUREMENT_LINK]


def test_missing_shipment_on_single_link_emits_blocker(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-3")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-4")
    _link(db_session, contract, sales_contract, frag)
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
    assert [b.code for b in decision.blockers] == [
        SalesPreparationBlockerCode.NO_SHIPMENT_FACT_ON_LINKED_CONTRACT
    ]
    assert decision.blockers[0].related_contract_ids == (contract.id,)
    assert decision.required_inputs[2].present is False


def test_shipments_on_unlinked_contract_do_not_satisfy_input(db_session):
    frag = _make_fragment(db_session)
    linked_contract = _make_contract(db_session, frag.id, "PO-F1A-4")
    unlinked_contract = _make_contract(db_session, frag.id, "PO-F1A-5")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-5")
    _link(db_session, linked_contract, sales_contract, frag)
    _make_shipment(db_session, unlinked_contract, frag.id, "SHIP-UNLINKED")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    # The shipment input is scoped to the LINKED contract only.
    assert [b.code for b in decision.blockers] == [
        SalesPreparationBlockerCode.NO_SHIPMENT_FACT_ON_LINKED_CONTRACT
    ]
    assert decision.required_inputs[2].source_fact_ids == ()


# ---------------------------------------------------------------------------
# M:N — never any/all shipment judgment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_shipments", ["none", "one", "both"])
def test_mn_shipment_judgment_always_deferred(db_session, with_shipments):
    """Under multiple current links the any/all shipment rule is NOT
    frozen: even when every linked contract has shipments, the rule must
    not claim the input present, and even when none does, it must not
    claim a specific contract lacks it — one explicit deferral blocker
    instead, always."""
    frag = _make_fragment(db_session)
    contract_a = _make_contract(db_session, frag.id, "PO-F1A-6A")
    contract_b = _make_contract(db_session, frag.id, "PO-F1A-6B")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-6")
    _link(db_session, contract_a, sales_contract, frag)
    _link(db_session, contract_b, sales_contract, frag)
    if with_shipments in ("one", "both"):
        _make_shipment(db_session, contract_a, frag.id, "SHIP-A")
    if with_shipments == "both":
        _make_shipment(db_session, contract_b, frag.id, "SHIP-B")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert [b.code for b in decision.blockers] == [
        SalesPreparationBlockerCode.SHIPMENT_JUDGMENT_DEFERRED_MULTIPLE_LINKS
    ]
    assert decision.status == SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "NOT_JUDGED_UNDER_MN"
    assert shipment_input.source_fact_ids == ()
    assert set(decision.blockers[0].related_contract_ids) == {contract_a.id, contract_b.id}


# ---------------------------------------------------------------------------
# customer semantics — fact only, never a fourth blocker
# ---------------------------------------------------------------------------


def test_unknown_customer_stays_unknown_and_adds_no_blocker(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-7")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-7", fields={})  # customer unknown
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-7")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    # Unknown customer is preserved as unknown — surfaced as a fact, NOT
    # judged: the three required inputs are present, so the status is
    # INPUTS_PRESENT even with customer None.
    assert decision.customer is None
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()


def test_customer_never_taken_from_contract_counterparty_or_buyer(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-8", counterparty="Supplier Zeta")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-8", fields={"customer": "Customer B"})
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-8")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.customer == "Customer B"
    assert decision.customer != "Supplier Zeta"
    assert decision.customer != "Our Own Entity"


# ---------------------------------------------------------------------------
# Fact -> Decision layering: vocabulary, purity, read-only
# ---------------------------------------------------------------------------


def test_status_and_dto_vocabulary_carry_no_eligibility_concept():
    # The decision status vocabulary has exactly two fact-completeness
    # members — no READY / NOT_READY / BLOCKED / ELIGIBLE member exists.
    assert set(vars(SalesPreparationDecisionStatus).values()) & {
        "READY",
        "NOT_READY",
        "BLOCKED",
        "ELIGIBLE",
    } == set()
    statuses = {
        v
        for k, v in vars(SalesPreparationDecisionStatus).items()
        if not k.startswith("_") and isinstance(v, str)
    }
    assert statuses == {"INPUTS_PRESENT", "INSUFFICIENT_FACTS"}

    banned_tokens = (
        "eligib",
        "ready",
        "remaining",
        "should",
        "owed",
        "outstanding",
        "amount",
        "quantity",
        "ratio",
        "apportion",
    )
    import bel.application.sales_invoice_preparation as module

    dto_types = [
        obj
        for obj in vars(module).values()
        if dataclasses.is_dataclass(obj) and getattr(obj, "__module__", None) == module.__name__
    ]
    assert {t.__name__ for t in dto_types} >= {"SalesInvoicePreparationDecision", "SalesPreparationBlocker"}
    for dto_type in dto_types:
        for f in dataclasses.fields(dto_type):
            for token in banned_tokens:
                assert token not in f.name.lower(), f"{dto_type.__name__}.{f.name} carries banned concept {token!r}"


def test_evaluation_is_strictly_read_only(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-9")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-9")
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-9")
    db_session.commit()

    def _counts():
        from bel.infrastructure.persistence import models as m

        counts = {}
        for name in dir(m):
            obj = getattr(m, name)
            if isinstance(obj, type) and hasattr(obj, "__tablename__"):
                counts[obj.__tablename__] = db_session.query(obj).count()
        return counts

    before = _counts()
    evaluate_sales_invoice_preparation(db_session)
    assert _counts() == before
    assert not db_session.dirty and not db_session.new and not db_session.deleted


def test_pure_function_over_manually_built_context_no_session():
    """The reserved Application-layer seam: the decision function is pure
    over the F0 context DTOs — no session, no DB — so the future
    consistency validation can compose with it directly."""
    sc_id, contract_id, shipment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    sales_contract = SalesContract(
        id=sc_id, our_entity="Our Own Entity", sales_contract_no="SC-PURE-1", customer="Customer C",
        currency="CNY", gross_amount=Decimal("100.00"), contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    contract = Contract(
        id=contract_id, contract_no="PO-PURE-1", contract_type=None, counterparty="Supplier",
        buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
    )
    link = ProcurementSalesLink(
        id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sc_id,
        source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
    )
    shipment = Shipment(
        id=shipment_id, contract_id=contract_id, external_reference="SHIP-PURE-1",
        execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
        current_source_fragment_id=uuid.uuid4(), created_at=NOW,
    )
    context = InvoicePreparationContext(
        sales_scopes=(
            SalesScopeContext(
                sales_contract=sales_contract,
                linked_procurement_contracts=(
                    SalesScopeLinkedProcurementContract(link=link, contract=contract),
                ),
                invoice_allocations=(),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
        supplier_scopes=(
            SupplierScopeContext(
                contract=contract,
                items=(),
                shipments=(shipment,),
                invoice_allocations=(),
                invoice_item_allocations=(),
                payment_allocations=(),
                unresolved_work=(),
            ),
        ),
    )

    report = evaluate_sales_invoice_preparation_from_context(context)
    decision = report.decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.required_inputs[2].source_fact_ids == (shipment_id,)


def test_consistency_check_seam_is_reserved_and_empty(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-10")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-10")
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-10")
    db_session.commit()

    # The 一致性校验 seam exists but is deliberately empty: the compared
    # field set ("完全一致") is not frozen, and no code path produces a
    # check result today.
    assert SALES_INVOICE_CONSISTENCY_CHECK_NAMES == ()
    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.consistency_checks == ()


def test_report_covers_every_sales_scope(db_session):
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-11")
    sc1 = _make_sales_contract(db_session, frag.id, "SC-F1A-11A")
    sc2 = _make_sales_contract(db_session, frag.id, "SC-F1A-11B")
    _link(db_session, contract, sc1, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-11")
    db_session.commit()

    report = evaluate_sales_invoice_preparation(db_session)
    assert {d.sales_contract_id for d in report.decisions} == {sc1.id, sc2.id}
    by_no = {d.sales_contract_no: d for d in report.decisions}
    assert by_no["SC-F1A-11A"].status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert by_no["SC-F1A-11B"].status == SalesPreparationDecisionStatus.INSUFFICIENT_FACTS
