"""Phase 2D.3-F1a — SALES_INVOICE_PREPARATION rule foundation (re-leveled
in Phase 2D.3-F1d).

Covers the frozen three-input rule layer over the F0 fact context, on
independently synthetic data. SALES_INVOICE_PREPARATION is NOT a process
gate: the three inputs report FACT COMPLETENESS / COMPARISON AVAILABILITY,
never invoice eligibility:

- the genuinely-required input is the SalesContract (present by
  construction); a missing link (management/context linkage) and a
  missing Shipment (export-management anchor) NEVER emit a blocker and
  NEVER change the status — the comparison they would feed is simply
  recorded unavailable;
- under M:N the any/all shipment judgment is not frozen — the shipment
  input is recorded NOT_JUDGED_UNDER_MN_UNRESOLVED, never a blocker, and
  the M:N linked-contract facts stay visible;
- customer only from SalesContract.customer (never a fourth input, never
  a finding);
- the Fact -> Decision layering (read-only, pure), and the reserved
  (empty) 一致性校验 seam.
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
    SalesScopeInvoiceAllocation,
    SalesScopeLinkedProcurementContract,
    SalesScopePaymentAllocation,
    SupplierScopeContext,
)
from bel.application.sales_invoice_preparation import (
    REQUIRED_INPUT_ORDER,
    SALES_INVOICE_CONSISTENCY_CHECK_NAMES,
    SalesInvoicePreparationDecision,
    SalesPreparationBlocker,
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
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import SalesInvoiceAllocation, SalesPaymentAllocation
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ProcurementSalesLink, ProcurementSalesLinkCorrection
from bel.domain.sales_contract import SalesContract
from bel.domain.shipment import Shipment
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceRepository,
    PaymentRepository,
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


def _make_sales_invoice(session, fragment_id, issue_date):
    invoice = Invoice(
        id=uuid.uuid4(),
        direction=InvoiceDirection.SALES,
        invoice_type=None,
        invoice_no=None,
        digital_invoice_no=None,
        external_invoice_key=f"SINV-{uuid.uuid4().hex[:8]}",
        issue_date=issue_date,
        seller="Our Own Entity",
        buyer="Customer",
        net_amount=Decimal("100.00"),
        tax_amount=Decimal("0"),
        gross_amount=Decimal("100.00"),
        invoice_status=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
        updated_at=NOW,
    )
    InvoiceRepository(session).add(invoice)
    session.flush()
    return invoice


def _make_in_receipt(session, fragment_id, transaction_date):
    payment = Payment(
        id=uuid.uuid4(),
        transaction_date=transaction_date,
        direction=PaymentDirection.IN,
        amount=Decimal("100.00"),
        counterparty="Customer",
        business_type=None,
        bank_reference=f"REF-{uuid.uuid4().hex[:8]}",
        description=None,
        running_balance=None,
        source_fragment_id=fragment_id,
        created_at=NOW,
    )
    PaymentRepository(session).add(payment)
    session.flush()
    return payment


# ---------------------------------------------------------------------------
# The three inputs — fact completeness / comparison availability
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


def test_missing_link_is_no_eligibility_blocker_comparison_unavailable(db_session):
    """The link is a management/context linkage, not an eligibility input:
    a scope with NO current link stays INPUTS_PRESENT with zero blockers;
    the link input is recorded as comparison-unavailable, and the
    shipment input is not judged (nothing to judge it on)."""
    frag = _make_fragment(db_session)
    _make_sales_contract(db_session, frag.id, "SC-F1A-2")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    link_input = decision.required_inputs[1]
    assert link_input.present is False
    assert link_input.source_fact_ids == ()
    assert link_input.note == "PROCUREMENT_COMPARISON_UNAVAILABLE_NO_LINK"
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "NOT_JUDGED_NO_LINKED_CONTRACT"
    assert shipment_input.source_fact_ids == ()


def test_superseded_link_is_no_eligibility_blocker_comparison_unavailable(db_session):
    """Current links only — a superseded episode is not a current linked
    procurement Contract (repository predicate reused, never re-derived).
    The missing linkage still never blocks: INPUTS_PRESENT, zero
    blockers."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-2")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-3")
    link = _link(db_session, contract, sales_contract, frag)
    _invalidate_link(db_session, link.id, frag)
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.required_inputs[1].present is False


def test_missing_shipment_is_no_eligibility_blocker_comparison_unavailable(db_session):
    """The Shipment is an export-management anchor, not an eligibility
    input: a scope with a current link but NO Shipment stays INPUTS_PRESENT
    with zero blockers — the export comparison is recorded unavailable,
    never "may not issue invoice"."""
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-3")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-4")
    _link(db_session, contract, sales_contract, frag)
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    # The linked contract stays visible as the comparison scope.
    assert decision.required_inputs[1].present is True
    assert decision.required_inputs[1].source_fact_ids == (contract.id,)
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "EXPORT_COMPARISON_UNAVAILABLE"
    assert shipment_input.source_fact_ids == ()


def test_shipments_on_unlinked_contract_do_not_satisfy_input(db_session):
    frag = _make_fragment(db_session)
    linked_contract = _make_contract(db_session, frag.id, "PO-F1A-4")
    unlinked_contract = _make_contract(db_session, frag.id, "PO-F1A-5")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-5")
    _link(db_session, linked_contract, sales_contract, frag)
    _make_shipment(db_session, unlinked_contract, frag.id, "SHIP-UNLINKED")
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    # The shipment input is scoped to the LINKED contract only — a
    # shipment on an unlinked contract is never attributed to it. Still
    # never an eligibility blocker.
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "EXPORT_COMPARISON_UNAVAILABLE"
    assert shipment_input.source_fact_ids == ()


# ---------------------------------------------------------------------------
# Receipt chronology — IP-S03: SALES invoice / IN receipt ordering is
# never a gate, never a finding (Codex Pre-Gate BLOCKER 2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invoice_date,receipt_date",
    [
        (date(2031, 1, 10), date(2031, 1, 20)),  # Case A: SALES invoice before IN receipt
        (date(2031, 1, 20), date(2031, 1, 10)),  # Case B: IN receipt before SALES invoice
    ],
)
def test_sales_invoice_receipt_ordering_never_gates(db_session, invoice_date, receipt_date):
    """IP-S03 (ACCOUNTANT_CONFIRMED, CONTEXT): a SALES invoice and an IN
    receipt may appear in either order with NO chronology blocker, NO
    chronology advisory, and NO status/gate difference caused by the
    ordering. The dates are EXPLICIT in the fixtures and genuinely
    REVERSED across the two parameterizations."""
    assert (invoice_date < receipt_date) or (receipt_date < invoice_date)
    frag = _make_fragment(db_session)
    contract = _make_contract(db_session, frag.id, "PO-F1A-12")
    sales_contract = _make_sales_contract(db_session, frag.id, "SC-F1A-12", fields={"customer": "Customer D"})
    _link(db_session, contract, sales_contract, frag)
    _make_shipment(db_session, contract, frag.id, "SHIP-F1A-12")
    # The SALES invoice and IN receipt Facts exist in the ledger with
    # explicit, genuinely-reversed dates.
    _make_sales_invoice(db_session, frag.id, issue_date=invoice_date)
    _make_in_receipt(db_session, frag.id, transaction_date=receipt_date)
    db_session.commit()

    decision = evaluate_sales_invoice_preparation(db_session).decisions[0]
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    assert decision.consistency_checks == ()
    assert decision.customer == "Customer D"


def test_sales_invoice_receipt_in_context_never_changes_decision():
    """Even when the SALES invoice and IN receipt associations ARE
    carried in the F0 sales context (via SalesInvoiceAllocation /
    SalesPaymentAllocation), the sales decision is byte-for-byte
    identical for both orderings: it never consults them and derives no
    status, gate, or finding from their presence or dates. (Pure function
    over the F0 context DTOs.)"""
    sales_contract_id, contract_id, shipment_id, invoice_id, receipt_id = (uuid.uuid4() for _ in range(5))

    def _context(invoice_date, receipt_date):
        sales_contract = SalesContract(
            id=sales_contract_id, our_entity="Our Own Entity", sales_contract_no="SC-CHRONO-1",
            customer="Customer E", currency="CNY", gross_amount=Decimal("100.00"),
            contract_date=date(2026, 1, 1), current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        )
        contract = Contract(
            id=contract_id, contract_no="PO-CHRONO-1", contract_type=None, counterparty="Supplier",
            buyer="Our Own Entity", gross_amount=Decimal("100.00"), currency="CNY", contract_date=date(2026, 1, 1),
            current_source_fragment_id=uuid.uuid4(), created_at=NOW, updated_at=NOW,
        )
        link = ProcurementSalesLink(
            id=uuid.uuid4(), procurement_contract_id=contract_id, sales_contract_id=sales_contract_id,
            source_fragment_id=uuid.uuid4(), confirmation_type="AUTO_CONFIRMED", created_at=NOW,
        )
        shipment = Shipment(
            id=shipment_id, contract_id=contract_id, external_reference="SHIP-CHRONO-1",
            execution_date=date(2031, 2, 1), contract_item_id=None, quantity=Decimal("1"),
            current_source_fragment_id=uuid.uuid4(), created_at=NOW,
        )
        sales_invoice = Invoice(
            id=invoice_id, direction=InvoiceDirection.SALES, invoice_type=None, invoice_no=None,
            digital_invoice_no=None, external_invoice_key="SINV-CHRONO-1", issue_date=invoice_date,
            seller="Our Own Entity", buyer="Customer", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=uuid.uuid4(),
            created_at=NOW, updated_at=NOW,
        )
        in_receipt = Payment(
            id=receipt_id, transaction_date=receipt_date, direction=PaymentDirection.IN,
            amount=Decimal("100.00"), counterparty="Customer", business_type=None,
            bank_reference="REF-CHRONO-1", description=None, running_balance=None,
            source_fragment_id=uuid.uuid4(), created_at=NOW,
        )
        return InvoicePreparationContext(
            sales_scopes=(
                SalesScopeContext(
                    sales_contract=sales_contract,
                    linked_procurement_contracts=(
                        SalesScopeLinkedProcurementContract(link=link, contract=contract),
                    ),
                    invoice_allocations=(SalesScopeInvoiceAllocation(
                        allocation=SalesInvoiceAllocation(
                            id=uuid.uuid4(), invoice_id=invoice_id, sales_contract_id=sales_contract_id,
                            match_case_id=uuid.uuid4(), allocated_gross_amount=Decimal("100.00"),
                            confirmation_type="AUTO_CONFIRMED", created_at=NOW,
                        ),
                        invoice=sales_invoice,
                    ),),
                    payment_allocations=(SalesScopePaymentAllocation(
                        allocation=SalesPaymentAllocation(
                            id=uuid.uuid4(), payment_id=receipt_id, sales_contract_id=sales_contract_id,
                            match_case_id=uuid.uuid4(), allocated_amount=Decimal("100.00"),
                            confirmation_type="AUTO_CONFIRMED", created_at=NOW,
                        ),
                        payment=in_receipt,
                    ),),
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

    case_a = evaluate_sales_invoice_preparation_from_context(
        _context(invoice_date=date(2031, 1, 10), receipt_date=date(2031, 1, 20))
    ).decisions[0]
    case_b = evaluate_sales_invoice_preparation_from_context(
        _context(invoice_date=date(2031, 1, 20), receipt_date=date(2031, 1, 10))
    ).decisions[0]

    # Both orderings: same status, zero blockers, no consistency checks —
    # and byte-for-byte identical decisions.
    assert case_a.status == case_b.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert case_a.blockers == () and case_b.blockers == ()
    assert case_a == case_b


# ---------------------------------------------------------------------------
# M:N — never any/all shipment judgment, never a blocker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_shipments", ["none", "one", "both"])
def test_mn_shipment_judgment_always_unresolved_never_blocker(db_session, with_shipments):
    """Under multiple current links the any/all shipment rule is NOT
    frozen: even when every linked contract has shipments, the shipment
    input is NOT judged; even when none does, no specific contract is
    claimed to lack one — one explicit unresolved-comparison note instead,
    always, and NEVER a blocker (Phase 2D.3-F1d re-leveling). The M:N
    linked-contract facts stay visible."""
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
    assert decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert decision.blockers == ()
    # M:N facts stay visible: every currently-linked contract id is on
    # the link input, resolved or not.
    assert set(decision.required_inputs[1].source_fact_ids) == {contract_a.id, contract_b.id}
    shipment_input = decision.required_inputs[2]
    assert shipment_input.present is False
    assert shipment_input.note == "NOT_JUDGED_UNDER_MN_UNRESOLVED"
    assert shipment_input.source_fact_ids == ()


# ---------------------------------------------------------------------------
# customer semantics — fact only, never a fourth input
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
    # judged: customer presence is deliberately NOT one of the three
    # inputs and adds no finding here.
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


def test_only_genuinely_required_sales_scope_data_would_be_insufficient():
    """INSUFFICIENT_FACTS is reserved for genuinely-required sales-scope
    data (the SalesContract) being missing — where preparation data
    cannot be built. The F0 construction provides a SalesContract for
    every sales scope, so — exactly like the schema-backstopped supplier
    amount path — the status member is deterministic but unreachable
    today. The re-leveled model proves the reachable inverse: the
    blocker vocabulary is empty, so NO code path can derive
    INSUFFICIENT_FACTS from a missing link, a missing Shipment, or the
    M:N deferral."""
    statuses = {
        v for k, v in vars(SalesPreparationDecisionStatus).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert statuses == {"INPUTS_PRESENT", "INSUFFICIENT_FACTS"}
    blocker_codes = {
        v for k, v in vars(SalesPreparationBlockerCode).items() if not k.startswith("_") and isinstance(v, str)
    }
    assert blocker_codes == set()


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
    assert by_no["SC-F1A-11A"].required_inputs[1].present is True
    # SC-F1A-11B has no current link — the linkage is management context
    # only, so it stays INPUTS_PRESENT with zero blockers and the link
    # input recorded comparison-unavailable.
    sc2_decision = by_no["SC-F1A-11B"]
    assert sc2_decision.status == SalesPreparationDecisionStatus.INPUTS_PRESENT
    assert sc2_decision.blockers == ()
    assert sc2_decision.required_inputs[1].present is False
