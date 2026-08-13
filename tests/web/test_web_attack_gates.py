"""Permanent Codex Gate A web attacks (spec section 32):

B. A SALES invoice must never read as "采购已到票" — it never suppresses
   an AccrualRequired and never drives a reversal.
C. Multiple open Accruals on the same item + one available allocation
   must surface MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE — the web
   page must not auto-choose one of them.

These build dedicated synthetic DBs (invented values only) and assert
the rendered page agrees with the frozen engine.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from bel.application.period_close import (
    MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE,
    build_period_close_preview,
)
from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualBasisScopeType,
    AccrualStatus,
    CostRecognitionFact,
    InvoiceItemAllocation,
)
from bel.domain.contract import Contract, ContractItem
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection, InvoiceItem
from bel.domain.matching import (
    AllocationMatchMethod,
    ConfirmationType,
    InvoiceAllocation,
    MatchCase,
    MatchCaseStatus,
    MatchMethod,
)
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualRepository,
    ContractItemRepository,
    ContractRepository,
    CostRecognitionFactRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
    MatchCaseRepository,
)

NOW = datetime.now(timezone.utc)
WEB_PERIOD = "2031-03"


class _Seed:
    """Small repository-based synthetic builder for the attack scenarios."""

    def __init__(self, session) -> None:
        self.session = session
        self.ev = EvidenceRepository(session)
        doc = EvidenceDocument(
            id=uuid.uuid4(), file_name="synthetic.xlsx", sha256=uuid.uuid4().hex,
            source_type="synthetic", imported_at=NOW,
        )
        self.ev.add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name="s1", row_number=1, locator_json=None, raw_data={}, created_at=NOW,
        )
        self.ev.add_fragment(frag)
        session.flush()
        self.frag = frag

    def contract(self, no: str) -> Contract:
        c = Contract(
            id=uuid.uuid4(), contract_no=no, contract_type=None, counterparty="SupplierGate",
            buyer="BuyerGate", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=self.frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(self.session).add(c)
        self.session.flush()
        return c

    def item(self, contract: Contract, qty: str, key: str = "ITEM-A") -> ContractItem:
        i = ContractItem(
            id=uuid.uuid4(), contract_id=contract.id, source_item_key=key, sku=None,
            product_name="Gate Widget", specification=None, quantity=Decimal(qty), unit="件",
            unit_price=None, gross_amount=None, tax_rate=None, net_amount=None,
            current_source_fragment_id=self.frag.id, created_at=NOW,
        )
        ContractItemRepository(self.session).add(i)
        self.session.flush()
        return i

    def invoice(self, item: ContractItem, external_key: str, direction: str, issue: str, qty: str, net: str) -> InvoiceItem:
        inv = Invoice(
            id=uuid.uuid4(), direction=direction, invoice_type=None, invoice_no=None,
            digital_invoice_no=external_key, external_invoice_key=external_key,
            issue_date=date.fromisoformat(issue), seller="SupplierGate", buyer="BuyerGate",
            net_amount=Decimal(net), tax_amount=Decimal("0"), gross_amount=Decimal(net),
            invoice_status=None, source_fragment_id=self.frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(self.session).add(inv)
        self.session.flush()
        ii = InvoiceItem(
            id=uuid.uuid4(), invoice_id=inv.id, line_no=1, product_name="Gate Widget",
            specification=None, unit="件", quantity=Decimal(qty), unit_price=None,
            net_amount=Decimal(net), tax_rate=None, tax_amount=Decimal("0"),
            gross_amount=Decimal(net), source_fragment_id=self.frag.id,
        )
        InvoiceItemRepository(self.session).add(ii)
        self.session.flush()
        return ii

    def confirm(self, invoice_item: InvoiceItem, contract: Contract) -> None:
        match_case = MatchCase(
            id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice_item.invoice_id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
            created_at=NOW, resolved_at=NOW,
        )
        MatchCaseRepository(self.session).add(match_case)
        self.session.flush()
        InvoiceAllocationRepository(self.session).add(
            InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice_item.invoice_id, contract_id=contract.id,
                match_case_id=match_case.id, allocated_gross_amount=Decimal("1000.00"),
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=NOW,
            )
        )
        self.session.flush()

    def item_allocation(self, invoice_item: InvoiceItem, contract_item: ContractItem, qty: str, net: str) -> None:
        alloc = InvoiceItemAllocation(
            id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=contract_item.id,
            allocated_quantity=Decimal(qty), allocated_net_amount=Decimal(net),
            confirmation_type="MANUAL_CONFIRMED", source_fragment_id=self.frag.id, created_at=NOW,
        )
        InvoiceItemAllocationRepository(self.session).add(alloc)
        self.session.flush()

    def cost_recognition(self, contract: Contract, date_str: str = "2031-02-28") -> None:
        CostRecognitionFactRepository(self.session).add(
            CostRecognitionFact(
                id=uuid.uuid4(), contract_id=contract.id,
                recognition_date=date.fromisoformat(date_str), basis="MANUAL_CONFIRMED",
                source_fragment_id=self.frag.id, created_at=NOW,
            )
        )
        self.session.flush()

    def item_basis(self, contract: Contract, contract_item: ContractItem, estimated: str, qty: str) -> None:
        AccrualBasisFactRepository(self.session).add(
            AccrualBasisFact(
                id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT_ITEM, contract_id=contract.id,
                contract_item_id=contract_item.id, quantity=Decimal(qty),
                estimated_cost=Decimal(estimated), basis="MANUAL_CONFIRMED",
                source_fragment_id=self.frag.id, created_at=NOW,
            )
        )
        self.session.flush()

    def accrual(self, contract_item: ContractItem, period: str, qty: str, estimated: str) -> None:
        AccrualRepository(self.session).add(
            Accrual(
                id=uuid.uuid4(), period=period, contract_item_id=contract_item.id,
                quantity=Decimal(qty), estimated_cost=Decimal(estimated), basis="MANUAL_CONFIRMED",
                status=AccrualStatus.ACTIVE, created_from_fact_id=uuid.uuid4(), created_at=NOW,
            )
        )
        self.session.flush()


def _build_app(tmp_path, seed_fn) -> tuple[TestClient, object, str]:
    from bel.web.app import create_app

    db_path = tmp_path / f"gate-{uuid.uuid4().hex[:8]}.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        contract_id = seed_fn(_Seed(session))
        session.commit()
    app = create_app(str(db_path))
    return TestClient(app), app, contract_id


def test_sales_invoice_never_reads_as_purchase_arrival(tmp_path):
    """Gate B: a confirmed SALES invoice must NOT suppress the contract's
    AccrualRequired and must never drive a reversal — the page agrees
    with the frozen engine."""

    def seed(s: _Seed) -> str:
        c = s.contract("PO-WEB-SALES")
        item = s.item(c, "60")
        invoice_item = s.invoice(item, "DIGITAL-WEB-SALES", InvoiceDirection.SALES, "2031-03-15", "60", "780.00")
        s.confirm(invoice_item, c)
        s.item_allocation(invoice_item, item, "60", "780.00")
        s.cost_recognition(c)
        s.item_basis(c, item, "624.00", "60")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    with app.state.session_factory() as session:
        preview = build_period_close_preview(session, WEB_PERIOD)
        assert [a.contract_id for a in preview.new_accrual_requirements] == [uuid.UUID(contract_id)]
        assert not preview.prior_accrual_reversals, "a SALES invoice never drives a reversal"

    html = client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}").text
    assert "Accrual Required" in html
    assert "销项" in html
    # No reversal rendered at all — guard the actual Projected State
    # wording (post-rename "部分红冲" alone would pass vacuously even if a
    # reversal were wrongly rendered).
    assert "红冲后" not in html

    page = client.get(f"/period-close?period={WEB_PERIOD}").text
    assert "Accrual Required" in page
    assert "PO-WEB-SALES" in page


def test_multiple_open_accruals_require_explicit_scope_in_web(tmp_path):
    """Gate C: two open Accruals on the same item + one available
    allocation -> MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE on the
    page, and NO monetary reversal is auto-chosen for either accrual."""

    def seed(s: _Seed) -> str:
        c = s.contract("PO-WEB-MULTI")
        item = s.item(c, "100")
        s.accrual(item, "2031-01", "100", "900.00")
        s.accrual(item, "2031-02", "100", "1000.00")
        invoice_item = s.invoice(item, "DIGITAL-WEB-MULTI", InvoiceDirection.PURCHASE, "2031-03-15", "35", "455.00")
        s.confirm(invoice_item, c)
        s.item_allocation(invoice_item, item, "35", "455.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    with app.state.session_factory() as session:
        preview = build_period_close_preview(session, WEB_PERIOD)
        assert not preview.prior_accrual_reversals, "no FIFO winner may be auto-chosen"
        assert any(
            b.blocker_type == MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE for b in preview.blockers
        )

    html = client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}").text
    assert MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE in html
    assert "存在多笔未冲销的历史暂估，无法判断本次到票归属哪一笔" in html
    # No reversal rendered at all (see Gate B comment above).
    assert "红冲后" not in html
