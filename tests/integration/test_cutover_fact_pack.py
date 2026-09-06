"""Phase 2D.1-R5 — Human-Confirmed Cutover Fact Pack closed allowlist.

Covers the test matrix from the R5 spec section 55: each allowed type
(A-E) and every forbidden type (F-P) individually, atomic all-or-nothing
rejection on a mixed pack, and the distinct cutover Evidence source_type.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from bel.application.cutover_fact_pack import (
    CUTOVER_SOURCE_TYPE,
    CutoverFactPackError,
    CutoverFactPackForbidden,
    CutoverFactPackNotReady,
    import_cutover_fact_pack,
    validate_cutover_fact_pack,
    validate_post_match_readiness,
)
from bel.application.matching import match_invoices
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    HistoricalAccrualFactRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
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


def _make_contract(session, contract_no="C-CUTOVER", counterparty="Supplier"):
    frag = _make_fragment(session)
    contract = Contract(
        id=uuid.uuid4(), contract_no=contract_no, contract_type=None, counterparty=counterparty, buyer="Buyer",
        gross_amount=Decimal("1000.00"), currency="CNY", contract_date=date(2026, 1, 1),
        current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
    )
    ContractRepository(session).add(contract)
    session.flush()
    return contract


# ---------------------------------------------------------------------------
# Allowed types (A-E)
# ---------------------------------------------------------------------------


def test_a_allowed_contract_item(db_session):
    contract = _make_contract(db_session)
    pack = {
        "contract_items": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "product_name": "Widget", "quantity": "10",
            }
        ]
    }
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert result.contract_items_created == 1

    doc = EvidenceRepository(db_session).get_document(result.evidence_document_id)
    assert doc.source_type == CUTOVER_SOURCE_TYPE


def test_b_allowed_historical_accrual_fact_no_accrual_created(db_session):
    contract = _make_contract(db_session)
    pack = {
        "contract_items": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "product_name": "Widget",
            }
        ],
        "historical_accrual_facts": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "source_period": "2025-12", "quantity": "5", "estimated_cost": "200.00",
            }
        ],
    }
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert result.historical_accrual_facts_created == 1
    # Deliberate divergence from Close Fact Pack — no Accrual is ever
    # created by a cutover pack (Accrual is a forbidden rule-output type).
    assert AccrualRepository(db_session).list_all() == []


def test_c_allowed_cost_recognition_fact(db_session):
    contract = _make_contract(db_session)
    pack = {
        "cost_recognition_facts": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "recognition_date": "2025-12-15", "basis": "MANUAL_CONFIRMED",
            }
        ]
    }
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert result.cost_recognition_facts_created == 1
    assert len(CostRecognitionFactRepository(db_session).list_all()) == 1


def test_d_allowed_accrual_basis_fact(db_session):
    contract = _make_contract(db_session)
    pack = {
        "accrual_basis_facts": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "scope_type": "CONTRACT", "estimated_cost": "500.00",
            }
        ]
    }
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert result.accrual_basis_facts_created == 1
    assert len(AccrualBasisFactRepository(db_session).list_all()) == 1


def test_e_allowed_invoice_item_allocation(db_session):
    contract = _make_contract(db_session)
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-CUTOVER", issue_date=date(2025, 12, 1),
        seller=contract.counterparty, buyer=contract.buyer, net_amount=Decimal("100"), tax_amount=Decimal("0"),
        gross_amount=Decimal("100"), invoice_status=None, source_fragment_id=_make_fragment(db_session).id,
        created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    item = InvoiceItem(
        id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None,
        unit=None, quantity=Decimal("10"), unit_price=None, net_amount=Decimal("100"), tax_rate=None,
        tax_amount=Decimal("0"), gross_amount=Decimal("100"), source_fragment_id=invoice.source_fragment_id,
    )
    InvoiceItemRepository(db_session).add(item)
    db_session.flush()

    # Item-level allocation requires an existing CONFIRMED contract-level
    # InvoiceAllocation (Phase 2C precondition, unrelated to R5) — set up
    # exactly as the ordinary manual-allocation flow would.
    from bel.domain.matching import ConfirmationType, InvoiceAllocation, MatchCase, MatchCaseStatus, MatchMethod
    from bel.infrastructure.persistence.repositories import InvoiceAllocationRepository, MatchCaseRepository

    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id, status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001, created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id, match_case_id=match_case.id,
            allocated_gross_amount=Decimal("100"), match_method=MatchMethod.M001,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    db_session.flush()

    pack = {
        "contract_items": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "product_name": "Widget",
            }
        ],
        "invoice_item_allocations": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "invoice": {"external_key": "INV-CUTOVER", "line_no": 1},
                "allocated_quantity": "10", "allocated_net_amount": "100",
            }
        ],
    }
    validate_post_match_readiness(db_session, pack)
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert result.invoice_item_allocations_created == 1
    assert len(InvoiceItemAllocationRepository(db_session).list_all()) == 1


# ---------------------------------------------------------------------------
# Forbidden types (F-P) — each individually rejected
# ---------------------------------------------------------------------------


FORBIDDEN_SECTIONS = [
    "invoices", "invoice_items", "payments", "shipments", "accruals", "accrual_reversals",
    "invoice_allocations", "payment_allocations", "sales_invoice_allocations",
    "sales_payment_allocations", "procurement_sales_links", "sales_contracts",
]


@pytest.mark.parametrize("section", FORBIDDEN_SECTIONS)
def test_forbidden_section_rejected(section):
    pack = {section: [{"anything": "goes here"}]}
    with pytest.raises(CutoverFactPackForbidden):
        validate_cutover_fact_pack(pack)


def test_unknown_section_rejected():
    with pytest.raises(CutoverFactPackForbidden):
        validate_cutover_fact_pack({"totally_made_up_section": []})


def test_mixed_valid_and_forbidden_atomic_reject(db_session):
    contract = _make_contract(db_session)
    pack = {
        "contract_items": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "source_item_key": "ITEM-1", "product_name": "Widget",
            }
        ],
        "payments": [{"amount": "100.00"}],
    }
    with pytest.raises(CutoverFactPackForbidden):
        import_cutover_fact_pack(db_session, pack, file_name="pack.json")

    # Zero facts written — atomic reject, not a partial import.
    assert EvidenceRepository(db_session).find_document_by_sha256("x") is None
    from bel.infrastructure.persistence.models import ContractItemModel

    assert db_session.query(ContractItemModel).count() == 0


def test_source_type_never_impersonates_real_sources(db_session):
    contract = _make_contract(db_session)
    pack = {
        "cost_recognition_facts": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "recognition_date": "2025-12-15", "basis": "MANUAL_CONFIRMED",
            }
        ]
    }
    result = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    doc = EvidenceRepository(db_session).get_document(result.evidence_document_id)
    assert doc.source_type not in {
        "cmb_bank_statement_pdf", "invoice_ledger_xlsx", "contract_ledger_xlsx", "close_fact_pack_json",
    }
    assert doc.source_type == "cutover_baseline_manual"


def test_reimport_same_pack_is_idempotent(db_session):
    contract = _make_contract(db_session)
    pack = {
        "cost_recognition_facts": [
            {
                "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
                "recognition_date": "2025-12-15", "basis": "MANUAL_CONFIRMED",
            }
        ]
    }
    first = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    second = import_cutover_fact_pack(db_session, pack, file_name="pack.json")
    assert first.is_reimport is False
    assert second.is_reimport is True
    assert len(CostRecognitionFactRepository(db_session).list_all()) == 1


def test_dependency_stages_share_evidence_and_complete_after_matching(db_session):
    contract = _make_contract(db_session)
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-STAGED", issue_date=date(2025, 12, 1),
        seller=contract.counterparty, buyer=contract.buyer, net_amount=Decimal("1000"), tax_amount=Decimal("0"),
        gross_amount=Decimal("1000"), invoice_status=None, source_fragment_id=_make_fragment(db_session).id,
        created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    InvoiceItemRepository(db_session).add(
        InvoiceItem(
            id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None,
            unit=None, quantity=Decimal("10"), unit_price=None, net_amount=Decimal("1000"), tax_rate=None,
            tax_amount=Decimal("0"), gross_amount=Decimal("1000"), source_fragment_id=invoice.source_fragment_id,
        )
    )
    pack = {
        "contract_items": [{
            "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
            "source_item_key": "ITEM-STAGED", "product_name": "Widget", "quantity": "10",
        }],
        "historical_accrual_facts": [{
            "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
            "source_item_key": "ITEM-STAGED", "source_period": "2025-11", "quantity": "1",
            "estimated_cost": "10",
        }],
        "invoice_item_allocations": [{
            "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
            "source_item_key": "ITEM-STAGED", "invoice": {"external_key": "INV-STAGED", "line_no": 1},
            "allocated_quantity": "10", "allocated_net_amount": "1000",
        }],
    }

    pre = import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="pre_match")
    replay_pre = import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="pre_match")
    assert pre.contract_items_created == 1
    assert replay_pre.contract_items_skipped == 1
    assert InvoiceItemAllocationRepository(db_session).list_all() == []

    with pytest.raises(CutoverFactPackNotReady, match="POST_MATCH_CONFIRMED_CONTRACT_ALLOCATION_MISSING"):
        validate_post_match_readiness(db_session, pack)
    match_invoices(db_session)
    validate_post_match_readiness(db_session, pack)
    post = import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="post_match")
    replay_post = import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="post_match")
    assert post.invoice_item_allocations_created == 1
    assert replay_post.invoice_item_allocations_skipped == 1
    assert post.evidence_document_id == pre.evidence_document_id
    fragments = EvidenceRepository(db_session).list_fragments_for_document(pre.evidence_document_id)
    assert len(fragments) == 3
    assert all(fragment.fragment_kind == FragmentKind.MANUAL_FACT for fragment in fragments)
    assert len(AccrualRepository(db_session).list_all()) == 0


def test_post_stage_still_enforces_confirmed_same_contract_edge(db_session):
    contract = _make_contract(db_session)
    wrong_contract = _make_contract(db_session, contract_no="C-WRONG", counterparty="Other Supplier")
    invoice = Invoice(
        id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
        digital_invoice_no=None, external_invoice_key="INV-NO-EDGE", issue_date=date(2025, 12, 1),
        seller=contract.counterparty, buyer=contract.buyer, net_amount=Decimal("10"), tax_amount=Decimal("0"),
        gross_amount=Decimal("10"), invoice_status=None, source_fragment_id=_make_fragment(db_session).id,
        created_at=NOW, updated_at=NOW,
    )
    InvoiceRepository(db_session).add(invoice)
    db_session.flush()
    InvoiceItemRepository(db_session).add(
        InvoiceItem(
            id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="Widget", specification=None,
            unit=None, quantity=Decimal("1"), unit_price=None, net_amount=Decimal("10"), tax_rate=None,
            tax_amount=Decimal("0"), gross_amount=Decimal("10"), source_fragment_id=invoice.source_fragment_id,
        )
    )
    from bel.domain.matching import ConfirmationType, InvoiceAllocation, MatchCase, MatchCaseStatus, MatchMethod
    from bel.infrastructure.persistence.repositories import InvoiceAllocationRepository, MatchCaseRepository

    match_case = MatchCase(
        id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id, status=MatchCaseStatus.AUTO_CONFIRMED,
        match_method=MatchMethod.M001, created_at=NOW, resolved_at=NOW,
    )
    MatchCaseRepository(db_session).add(match_case)
    db_session.flush()
    InvoiceAllocationRepository(db_session).add(
        InvoiceAllocation(
            id=uuid.uuid4(), invoice_id=invoice.id, contract_id=wrong_contract.id, match_case_id=match_case.id,
            allocated_gross_amount=Decimal("10"), match_method=MatchMethod.M001,
            confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
    )
    db_session.flush()
    pack = {
        "contract_items": [{
            "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
            "source_item_key": "ITEM-NO-EDGE", "product_name": "Widget",
        }],
        "invoice_item_allocations": [{
            "contract_selector": {"contract_no": contract.contract_no, "counterparty": contract.counterparty},
            "source_item_key": "ITEM-NO-EDGE", "invoice": {"external_key": "INV-NO-EDGE", "line_no": 1},
            "allocated_quantity": "1", "allocated_net_amount": "10",
        }],
    }
    import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="pre_match")
    with pytest.raises(CutoverFactPackNotReady, match="POST_MATCH_CONFIRMED_CONTRACT_ALLOCATION_MISSING"):
        validate_post_match_readiness(db_session, pack)
    with pytest.raises(CutoverFactPackError):
        import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="post_match")
    assert InvoiceItemAllocationRepository(db_session).list_all() == []


def test_malformed_post_entry_rejected_before_any_pack_write(db_session):
    pack = {
        "contract_items": [{"contract_selector": {}, "source_item_key": "ITEM"}],
        "invoice_item_allocations": [{"invoice": {"external_key": "INV"}}],
    }
    with pytest.raises(CutoverFactPackError):
        import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="pre_match")
    from bel.infrastructure.persistence.models import EvidenceDocumentModel

    assert db_session.query(EvidenceDocumentModel).count() == 0


@pytest.mark.parametrize(
    ("section", "selector"),
    [
        ("contract_items", None),
        ("historical_accrual_facts", "not-an-object"),
        ("cost_recognition_facts", {"counterparty": "Supplier"}),
        ("accrual_basis_facts", {"contract_no": "  ", "counterparty": "Supplier"}),
        ("invoice_item_allocations", {"contract_no": "C", "counterparty": "  "}),
    ],
)
def test_malformed_selector_in_every_allowed_section_is_atomic(db_session, section, selector):
    entries = {
        "contract_items": {"source_item_key": "ITEM"},
        "historical_accrual_facts": {
            "source_item_key": "ITEM", "source_period": "2025-11", "quantity": "1", "estimated_cost": "1",
        },
        "cost_recognition_facts": {"recognition_date": "2025-11-01", "basis": "MANUAL_CONFIRMED"},
        "accrual_basis_facts": {"scope_type": "CONTRACT", "estimated_cost": "1"},
        "invoice_item_allocations": {
            "source_item_key": "ITEM", "invoice": {"external_key": "INV", "line_no": 1},
            "allocated_quantity": "1", "allocated_net_amount": "1",
        },
    }
    entry = entries[section]
    if selector is not None:
        entry["contract_selector"] = selector
    with pytest.raises(CutoverFactPackError):
        import_cutover_fact_pack(db_session, {section: [entry]}, file_name="pack.json", stage="pre_match")
    from bel.infrastructure.persistence.models import EvidenceDocumentModel

    assert db_session.query(EvidenceDocumentModel).filter_by(source_type=CUTOVER_SOURCE_TYPE).count() == 0


@pytest.mark.parametrize(
    "selector",
    [
        {"contract_no": "C"},
        {"contract_no": "C", "counterparty": ""},
        {"contract_no": "C", "counterparty": "Supplier"},
    ],
)
def test_malformed_post_selector_prevents_pre_stage_writes(db_session, selector):
    if selector.get("counterparty") == "Supplier":
        selector = {"contract_no": "", "counterparty": "Supplier"}
    pack = {
        "contract_items": [{
            "contract_selector": {"contract_no": "C", "counterparty": "Supplier"},
            "source_item_key": "ITEM",
        }],
        "invoice_item_allocations": [{
            "contract_selector": selector, "source_item_key": "ITEM",
            "invoice": {"external_key": "INV", "line_no": 1},
            "allocated_quantity": "1", "allocated_net_amount": "1",
        }],
    }
    with pytest.raises(CutoverFactPackError):
        import_cutover_fact_pack(db_session, pack, file_name="pack.json", stage="pre_match")
    from bel.infrastructure.persistence.models import ContractItemModel

    assert db_session.query(ContractItemModel).count() == 0
