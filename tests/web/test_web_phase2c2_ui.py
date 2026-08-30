"""Phase 2C.2 UI regression suite (spec section 11).

Every scenario here is a hand-built, independently-synthetic database
(no value derived from a non-public file — docs/PRIVATE-DATA-POLICY.md).
These exercise the four-layer UI semantic (Fact / Current State /
Decision-Projected State / Blocker) and the presentation-only
composition (item-evidence placeholder, supplier grouping, blocker
business context) added on top of the frozen ``build_period_close_preview``
engine — none of these tests touch or extend the Rule Engine itself.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from bel.application.period_close import (
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE,
    build_period_close_preview,
)
from bel.application.period_close_workbench import get_period_close_workbench
from bel.domain.accrual import (
    Accrual,
    AccrualBasisFact,
    AccrualBasisScopeType,
    AccrualReversal,
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
from bel.infrastructure.persistence.models import (
    AccrualBasisFactModel,
    AccrualModel,
    AccrualReversalModel,
    BusinessEventModel,
    ContractItemModel,
    ContractModel,
    CostRecognitionFactModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    HistoricalAccrualFactModel,
    ImportRunModel,
    InvoiceAllocationModel,
    InvoiceItemAllocationModel,
    InvoiceItemModel,
    InvoiceModel,
    MatchCandidateModel,
    MatchCaseModel,
    PaymentAllocationModel,
    PaymentModel,
)
from bel.infrastructure.persistence.repositories import (
    AccrualBasisFactRepository,
    AccrualRepository,
    AccrualReversalRepository,
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
UI_PERIOD = "2031-03"


class _Seed:
    """Independently-synthetic repository builder for Phase 2C.2 UI
    scenarios — invented contracts/suppliers/amounts only, never derived
    from a non-public file."""

    def __init__(self, session) -> None:
        self.session = session
        ev = EvidenceRepository(session)
        doc = EvidenceDocument(
            id=uuid.uuid4(), file_name="synthetic-ui.xlsx", sha256=uuid.uuid4().hex,
            source_type="synthetic", imported_at=NOW,
        )
        ev.add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name="s1", row_number=1, locator_json=None, raw_data={}, created_at=NOW,
        )
        ev.add_fragment(frag)
        session.flush()
        self.frag = frag

    def contract(self, no: str, counterparty: str, gross: str = "1000.00") -> Contract:
        c = Contract(
            id=uuid.uuid4(), contract_no=no, contract_type=None, counterparty=counterparty,
            buyer="BuyerUI", gross_amount=Decimal(gross), currency="CNY", contract_date=None,
            current_source_fragment_id=self.frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(self.session).add(c)
        self.session.flush()
        return c

    def item(self, contract: Contract, qty: str, key: str = "ITEM-A", product_name: str | None = "UI Widget") -> ContractItem:
        i = ContractItem(
            id=uuid.uuid4(), contract_id=contract.id, source_item_key=key, sku=None,
            product_name=product_name, specification=None, quantity=Decimal(qty), unit="件",
            unit_price=None, gross_amount=None, tax_rate=None, net_amount=None,
            current_source_fragment_id=self.frag.id, created_at=NOW,
        )
        ContractItemRepository(self.session).add(i)
        self.session.flush()
        return i

    def invoice(self, external_key: str, counterparty: str, issue: str, qty: str, net: str) -> InvoiceItem:
        inv = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no=external_key, external_invoice_key=external_key,
            issue_date=date.fromisoformat(issue), seller=counterparty, buyer="BuyerUI",
            net_amount=Decimal(net), tax_amount=Decimal("0"), gross_amount=Decimal(net),
            invoice_status=None, source_fragment_id=self.frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(self.session).add(inv)
        self.session.flush()
        ii = InvoiceItem(
            id=uuid.uuid4(), invoice_id=inv.id, line_no=1, product_name="UI Widget",
            specification=None, unit="件", quantity=Decimal(qty), unit_price=None,
            net_amount=Decimal(net), tax_rate=None, tax_amount=Decimal("0"),
            gross_amount=Decimal(net), source_fragment_id=self.frag.id,
        )
        InvoiceItemRepository(self.session).add(ii)
        self.session.flush()
        return ii

    def confirm(self, invoice_item: InvoiceItem, contract: Contract, confirmation_type: str = ConfirmationType.AUTO_CONFIRMED) -> None:
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
                confirmation_type=confirmation_type, created_at=NOW,
            )
        )
        self.session.flush()

    def item_allocation(self, invoice_item: InvoiceItem, contract_item: ContractItem, qty: str, net: str, created_at=NOW) -> InvoiceItemAllocation:
        alloc = InvoiceItemAllocation(
            id=uuid.uuid4(), invoice_item_id=invoice_item.id, contract_item_id=contract_item.id,
            allocated_quantity=Decimal(qty), allocated_net_amount=Decimal(net),
            confirmation_type="MANUAL_CONFIRMED", source_fragment_id=self.frag.id, created_at=created_at,
        )
        InvoiceItemAllocationRepository(self.session).add(alloc)
        self.session.flush()
        return alloc

    def cost_recognition(self, contract: Contract, date_str: str = "2031-02-28") -> None:
        CostRecognitionFactRepository(self.session).add(
            CostRecognitionFact(
                id=uuid.uuid4(), contract_id=contract.id,
                recognition_date=date.fromisoformat(date_str), basis="MANUAL_CONFIRMED",
                source_fragment_id=self.frag.id, created_at=NOW,
            )
        )
        self.session.flush()

    def contract_basis(self, contract: Contract, estimated: str) -> None:
        """CONTRACT-scope AccrualBasisFact -> a Contract-level Candidate at
        preview time (no ContractItem exists for these — R007)."""
        AccrualBasisFactRepository(self.session).add(
            AccrualBasisFact(
                id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT, contract_id=contract.id,
                contract_item_id=None, quantity=None,
                estimated_cost=Decimal(estimated), basis="MANUAL_CONFIRMED",
                source_fragment_id=self.frag.id, created_at=NOW,
            )
        )
        self.session.flush()

    def item_basis(self, contract: Contract, item: ContractItem, estimated: str, qty: str) -> None:
        """CONTRACT_ITEM-scope AccrualBasisFact -> an AccrualRequired
        (R002) at preview time for this item."""
        AccrualBasisFactRepository(self.session).add(
            AccrualBasisFact(
                id=uuid.uuid4(), scope_type=AccrualBasisScopeType.CONTRACT_ITEM, contract_id=contract.id,
                contract_item_id=item.id, quantity=Decimal(qty),
                estimated_cost=Decimal(estimated), basis="MANUAL_CONFIRMED",
                source_fragment_id=self.frag.id, created_at=NOW,
            )
        )
        self.session.flush()

    def accrual(self, item: ContractItem, period: str, qty: str, estimated: str) -> Accrual:
        a = Accrual(
            id=uuid.uuid4(), period=period, contract_item_id=item.id,
            quantity=Decimal(qty), estimated_cost=Decimal(estimated), basis="MANUAL_CONFIRMED",
            status=AccrualStatus.ACTIVE, created_from_fact_id=uuid.uuid4(), created_at=NOW,
        )
        AccrualRepository(self.session).add(a)
        self.session.flush()
        return a

    def persisted_reversal(self, accrual: Accrual, allocation: InvoiceItemAllocation, qty: str, cost: str) -> None:
        """A reversal that ALREADY happened and was recorded (Current
        State), distinct from this period's Decision/Projected State."""
        AccrualReversalRepository(self.session).add(
            AccrualReversal(
                id=uuid.uuid4(), accrual_id=accrual.id, period=accrual.period,
                invoice_item_allocation_id=allocation.id,
                reversed_quantity=Decimal(qty), reversed_estimated_cost=Decimal(cost), created_at=NOW,
            )
        )
        self.session.flush()


def _build_app(tmp_path, seed_fn):
    from bel.web.app import create_app

    db_path = tmp_path / f"ui-{uuid.uuid4().hex[:8]}.db"
    engine = make_engine(f"sqlite:///{db_path}")
    from bel.infrastructure.persistence.models import Base

    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        result = seed_fn(_Seed(session))
        session.commit()
    app = create_app(f"sqlite:///{db_path}")
    return TestClient(app), app, result


_DB_MODELS = [
    AccrualBasisFactModel, AccrualModel, AccrualReversalModel, BusinessEventModel,
    ContractItemModel, ContractModel, CostRecognitionFactModel, EvidenceDocumentModel,
    EvidenceFragmentModel, HistoricalAccrualFactModel, ImportRunModel, InvoiceAllocationModel,
    InvoiceItemAllocationModel, InvoiceItemModel, InvoiceModel, MatchCandidateModel,
    MatchCaseModel, PaymentAllocationModel, PaymentModel,
]


def _db_counts(session_factory) -> dict[str, int]:
    with session_factory() as session:
        return {m.__tablename__: session.query(m).count() for m in _DB_MODELS}


# ---- A. Projected status semantic ----------------------------------------


def test_a_projected_status_never_reads_as_already_executed(tmp_path):
    """Period Close reversal must render 'Projected State' wording only;
    Contract360's persisted balance may legitimately say '已冲销'."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-A", "SupplierUiAlpha")
        item = s.item(c, "10")
        accrual = s.accrual(item, "2031-02", "10", "500.00")
        invoice_item = s.invoice("DIGITAL-UI-A", "SupplierUiAlpha", "2031-03-10", "10", "500.00")
        s.confirm(invoice_item, c)
        allocation = s.item_allocation(invoice_item, item, "10", "500.00")
        # Full persisted reversal — a Current State the Contract360 balance
        # panel may legitimately call "已冲销".
        s.persisted_reversal(accrual, allocation, "10", "500.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)

    # Period Close preview: the accrual is fully reversed already, so no
    # open accrual remains -> no reversal Decision this period. Assert the
    # page never emits the bare "已红冲"/"部分红冲" strings anywhere.
    page = client.get(f"/period-close?period={UI_PERIOD}").text
    assert "已红冲" not in page

    # Contract360: Current State is legitimately allowed to say "已冲销".
    detail = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text
    assert "当前暂估余额" in detail
    assert "已冲销" in detail
    assert "红冲后" not in detail  # no Projected State language for a Current State fact


def test_a_partial_reversal_uses_projected_wording(web_client):
    """The existing S2B-01 partial-reversal fixture must render the exact
    Projected State phrase, never the bare status word."""
    from tests.web.conftest import CLOSE_PERIOD_FIXTURE

    html = web_client.get(f"/period-close?period={CLOSE_PERIOD_FIXTURE}").text
    assert "红冲后：部分冲销" in html
    assert "已红冲" not in html
    assert "部分红冲" not in html


# ---- B. Missing product evidence ------------------------------------------


def test_b_missing_product_evidence_is_never_shown_as_a_product_name(tmp_path):
    """A ContractItem with no product_name must render the business
    placeholder — never its source_item_key masquerading as a product."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-B", "SupplierUiBeta")
        item = s.item(c, "10", key="ITEM-A", product_name=None)
        s.accrual(item, "2031-02", "10", "500.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text

    assert "未提供商品明细" in html
    assert "仅合同范围" in html
    # The technical key is still traceable, but only inside the
    # technical-detail rendering (mono, under an "Item Key" label) — never
    # as the primary business text.
    assert '<dt>Item Key</dt><dd class="mono">ITEM-A</dd>' in html


def test_b_accrual_balance_shows_evidence_note_not_item_key(tmp_path):
    def seed(s: _Seed):
        c = s.contract("PO-UI-B2", "SupplierUiBeta2")
        item = s.item(c, "10", key="ITEM-Z", product_name=None)
        s.accrual(item, "2031-02", "10", "500.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text
    assert "当前暂估余额" in html
    assert "当前暂估仅有合同范围证据" in html


def _section(html: str, start_marker: str, end_marker: str | None = None) -> str:
    """Slice page text between two headings so a per-path assertion can't
    pass by accident because the note happens to render somewhere else on
    the page."""
    start = html.index(start_marker)
    if end_marker is None:
        return html[start:]
    end = html.index(end_marker, start)
    return html[start:end]


def test_b_placeholder_evidence_covers_every_required_path(tmp_path):
    """Gate A remediation (Blocker 1): every one of the eight listed
    presentation paths must render BOTH the primary placeholder
    ("未提供商品明细") and the secondary evidence note ("当前暂估仅有合同
    范围证据" / "仅合同范围") for a ContractItem with no product_name —
    never just the primary label alone, and never the raw
    source_item_key standing in for it."""

    def seed(s: _Seed):
        # Path 1/2/3/8: reversal + its cost difference (Period Close 本期
        # 拟红冲 / 成本差异, and the same rows reused on Contract360's 本期
        # 业务判断).
        c_reversal = s.contract("PO-UI-B-REV", "SupplierUiBRev")
        item_reversal = s.item(c_reversal, "10", key="ITEM-REV", product_name=None)
        accrual_reversal = s.accrual(item_reversal, "2031-02", "10", "500.00")
        inv_reversal = s.invoice("DIGITAL-UI-B-REV", "SupplierUiBRev", "2031-03-10", "10", "500.00")
        s.confirm(inv_reversal, c_reversal)
        s.item_allocation(inv_reversal, item_reversal, "10", "500.00")

        # Path: new AccrualRequired (Period Close 新增正式暂估, Contract360
        # 本期业务判断's own accrual-required row).
        c_new = s.contract("PO-UI-B-NEW", "SupplierUiBNew")
        item_new = s.item(c_new, "20", key="ITEM-NEW", product_name=None)
        s.cost_recognition(c_new)
        s.item_basis(c_new, item_new, "800.00", "20")

        # Path: MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE blocker
        # (Period Close Blocker, Contract360 Blocker).
        c_blocker = s.contract("PO-UI-B-BLK", "SupplierUiBBlk", gross="2400.00")
        item_blocker = s.item(c_blocker, "200", key="ITEM-BLK", product_name=None)
        s.accrual(item_blocker, "2031-02", "200", "2400.00")
        inv_a = s.invoice("DIGITAL-UI-B-BLK-A", "SupplierUiBBlk", "2031-03-05", "35", "455.00")
        inv_b = s.invoice("DIGITAL-UI-B-BLK-B", "SupplierUiBBlk", "2031-03-06", "40", "480.00")
        s.confirm(inv_a, c_blocker)
        s.confirm(inv_b, c_blocker)
        s.item_allocation(inv_a, item_blocker, "35", "455.00")
        s.item_allocation(inv_b, item_blocker, "40", "480.00")

        return {
            "reversal": str(c_reversal.id),
            "new": str(c_new.id),
            "blocker": str(c_blocker.id),
        }

    client, app, ids = _build_app(tmp_path, seed)

    PRIMARY = "未提供商品明细"
    NOTE = "当前暂估仅有合同范围证据"

    # ---- Period Close page ----
    page = client.get(f"/period-close?period={UI_PERIOD}").text

    blockers_section = _section(page, "<h2>阻塞待处理</h2>", "<h2>本期拟红冲</h2>")
    assert PRIMARY in blockers_section and NOTE in blockers_section, "Period Close Blocker"

    reversals_section = _section(page, "<h2>本期拟红冲</h2>", "<h2>新增正式暂估</h2>")
    assert PRIMARY in reversals_section and NOTE in reversals_section, "Period Close 本期拟红冲"

    accruals_section = _section(page, "<h2>新增正式暂估</h2>", "<h2>成本差异</h2>")
    assert PRIMARY in accruals_section and NOTE in accruals_section, "Period Close 新增正式暂估"

    differences_section = _section(page, "<h2>成本差异</h2>")
    assert PRIMARY in differences_section and NOTE in differences_section, "Period Close 成本差异"

    # The blocker's raw key is reachable on Period Close too (added to its
    # own 技术详情 in this remediation); reversal/accrual rows carry no
    # source_item_key field in their Decision -> Fact trace at all, so
    # nothing to assert there — its traceability is via Contract360 below.
    assert "ITEM-BLK" in page
    tech_index = page.find("技术详情")
    blk_key_index = page.find("ITEM-BLK")
    assert tech_index != -1 and blk_key_index > tech_index

    # ---- Contract360: reversal contract (合同范围/商品明细, 本期业务判断) ----
    detail_reversal = client.get(f"/contracts/{ids['reversal']}?period={UI_PERIOD}").text
    scope_section = _section(detail_reversal, "<h2>合同范围 / 商品明细</h2>", "<h2>发票</h2>")
    assert PRIMARY in scope_section and ("仅合同范围" in scope_section or NOTE in scope_section), "Contract360 合同范围/商品明细"

    balance_section = _section(detail_reversal, "<h2>当前暂估余额</h2>", "<h2>本期业务判断")
    assert PRIMARY in balance_section and NOTE in balance_section, "Contract360 当前暂估余额"

    decisions_reversal_section = _section(detail_reversal, "<h2>本期业务判断")
    assert PRIMARY in decisions_reversal_section and NOTE in decisions_reversal_section, "Contract360 本期拟红冲/成本差异"

    # ---- Contract360: new-accrual contract ----
    detail_new = client.get(f"/contracts/{ids['new']}?period={UI_PERIOD}").text
    decisions_new_section = _section(detail_new, "<h2>本期业务判断")
    assert PRIMARY in decisions_new_section and NOTE in decisions_new_section, "Contract360 新增正式暂估"

    # ---- Contract360: blocker contract ----
    detail_blocker = client.get(f"/contracts/{ids['blocker']}?period={UI_PERIOD}").text
    decisions_blocker_section = _section(detail_blocker, "<h2>本期业务判断")
    assert PRIMARY in decisions_blocker_section and NOTE in decisions_blocker_section, "Contract360 Blocker"


# ---- C. Candidate supplier grouping ----------------------------------------


def test_c_candidates_group_by_supplier_without_changing_totals(tmp_path):
    """Multiple contract-level candidates: same supplier (x2), a different
    supplier, and a duplicated contract_no under two different
    counterparties. Grouping is presentation-only: group count, total
    candidate count, and cost sum must all agree with the raw preview."""

    def seed(s: _Seed):
        a1 = s.contract("PO-UI-C-A1", "SupplierUiGroupA")
        s.cost_recognition(a1)
        s.contract_basis(a1, "111.11")

        a2 = s.contract("PO-UI-C-A2", "SupplierUiGroupA")
        s.cost_recognition(a2)
        s.contract_basis(a2, "222.22")

        b1 = s.contract("PO-UI-C-B1", "SupplierUiGroupB")
        s.cost_recognition(b1)
        s.contract_basis(b1, "333.33")

        # Duplicate contract_no, two DIFFERENT counterparties — must stay
        # two distinct candidates, never merged/overwritten.
        d1 = s.contract("DUP-UI-C-001", "SupplierUiGroupC1")
        s.cost_recognition(d1)
        s.contract_basis(d1, "444.44")
        d2 = s.contract("DUP-UI-C-001", "SupplierUiGroupC2")
        s.cost_recognition(d2)
        s.contract_basis(d2, "555.55")

        return None

    client, app, _ = _build_app(tmp_path, seed)

    with app.state.session_factory() as session:
        preview = build_period_close_preview(session, UI_PERIOD)
        workbench = get_period_close_workbench(session, UI_PERIOD)

    assert len(preview.contract_level_candidates) == 5
    raw_total = sum((c.estimated_cost for c in preview.contract_level_candidates), Decimal("0"))
    assert raw_total == Decimal("1666.65")

    from bel.web import viewmodels

    candidate_rows = [viewmodels.CandidateRowVM(c, i) for i, c in enumerate(workbench.candidates)]
    groups = viewmodels._group_candidates_by_supplier(candidate_rows)

    # 4 distinct groups: SupplierUiGroupA, SupplierUiGroupB,
    # SupplierUiGroupC1, SupplierUiGroupC2 (duplicate contract_no does NOT
    # collapse the two different counterparties into one group).
    assert len(groups) == 4
    assert sum(g.contract_count for g in groups) == 5
    assert sum(g.estimated_cost_total for g in groups) == raw_total
    group_a = next(g for g in groups if g.counterparty == "SupplierUiGroupA")
    assert group_a.contract_count == 2
    assert group_a.estimated_cost_total == Decimal("333.33")

    html = client.get(f"/period-close?period={UI_PERIOD}").text
    assert html.count("DUP-UI-C-001") == 2, "the duplicated contract_no must appear once per distinct candidate"
    assert "SupplierUiGroupC1" in html and "SupplierUiGroupC2" in html


# ---- Gate A remediation: Candidate raw reason traceability ----------------


def test_candidate_raw_blocking_reason_is_traceable_not_primary(tmp_path):
    """A Contract-level Candidate's business text (missing_info) must be
    the primary visible content on BOTH Period Close's supplier-grouped
    candidate table and Contract360's 本期业务判断 candidate table — but
    the raw Decision reason code (MISSING_CONTRACT_ITEM_EVIDENCE) must
    still be reachable, positioned inside a 技术详情 detail, and never
    used as the primary status tag."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-CANDIDATE-REASON", "SupplierUiCandidateReason")
        s.cost_recognition(c)
        s.contract_basis(c, "246.80")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)

    BUSINESS_TEXT = "当前已确认到合同范围，但缺少商品明细证据，暂不能形成正式暂估。"
    RAW_CODE = "MISSING_CONTRACT_ITEM_EVIDENCE"
    STATUS_LABEL = "尚不能形成正式暂估"

    for page_url in (f"/period-close?period={UI_PERIOD}", f"/contracts/{contract_id}?period={UI_PERIOD}"):
        html = client.get(page_url).text

        assert BUSINESS_TEXT in html, f"business explanation missing on {page_url}"
        assert RAW_CODE in html, f"raw blocking_reason no longer traceable on {page_url}"

        # Structural check: the raw code must sit AFTER a 技术详情 marker,
        # not merely appear somewhere on the page (it could otherwise be
        # smuggled into the primary status tag).
        tech_index = html.find("技术详情")
        code_index = html.find(RAW_CODE)
        assert tech_index != -1 and code_index > tech_index, (
            f"{RAW_CODE} must be positioned inside a 技术详情 block on {page_url}"
        )

        # The visible status tag must be the readable label, never the raw
        # code standing in for it.
        assert f'tag-candidate">{STATUS_LABEL}</span>' in html, f"status tag was not the business label on {page_url}"
        assert f'tag-candidate">{RAW_CODE}</span>' not in html, f"raw code must never BE the primary status tag on {page_url}"


def test_other_technical_values_remain_business_first_with_raw_traceable(tmp_path):
    """Re-confirms the established Phase 2C.2 principle for the other
    named technical enums after this remediation: AUTO_CONFIRMED,
    HUMAN_CONFIRMED, and EXACT_COUNTERPARTY_AMOUNT_UNIQUE render a
    Chinese business label first, with the raw literal still reachable.
    MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE is already covered
    by test_d_multiple_item_allocations_blocker_shows_business_card."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-TECHVALS", "SupplierUiTechvals")
        item = s.item(c, "10")
        inv_auto = s.invoice("DIGITAL-UI-TECHVALS-AUTO", "SupplierUiTechvals", "2031-03-05", "10", "500.00")
        s.confirm(inv_auto, c, confirmation_type=ConfirmationType.AUTO_CONFIRMED)
        return str(c.id)

    def seed_human(s: _Seed):
        c = s.contract("PO-UI-TECHVALS-H", "SupplierUiTechvalsHuman")
        item = s.item(c, "10")
        inv_human = s.invoice("DIGITAL-UI-TECHVALS-HUMAN", "SupplierUiTechvalsHuman", "2031-03-06", "10", "500.00")
        s.confirm(inv_human, c, confirmation_type=ConfirmationType.HUMAN_CONFIRMED)
        return str(c.id)

    client_auto, _, contract_id_auto = _build_app(tmp_path, seed)
    client_human, _, contract_id_human = _build_app(tmp_path, seed_human)

    auto_html = client_auto.get(f"/contracts/{contract_id_auto}?period={UI_PERIOD}").text
    assert "系统确定性匹配" in auto_html
    assert "交易对手 + 金额唯一匹配" in auto_html
    assert "AUTO_CONFIRMED" in auto_html
    assert "EXACT_COUNTERPARTY_AMOUNT_UNIQUE" in auto_html
    tech_index = auto_html.find("技术详情")
    assert tech_index != -1 and auto_html.find("AUTO_CONFIRMED", tech_index) > tech_index

    human_html = client_human.get(f"/contracts/{contract_id_human}?period={UI_PERIOD}").text
    assert "人工确认" in human_html
    assert "HUMAN_CONFIRMED" in human_html
    tech_index = human_html.find("技术详情")
    assert tech_index != -1 and human_html.find("HUMAN_CONFIRMED", tech_index) > tech_index


# ---- D. Blocker presentation ----------------------------------------------


def test_d_multiple_item_allocations_blocker_shows_business_card(tmp_path):
    """Two qualifying InvoiceItemAllocations on one open Accrual -> the
    business-first blocker card (title/reason/next-step/contract link)
    must be visible, and the raw blocker code must be reachable only
    inside a 技术详情 detail."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-D", "SupplierUiDelta", gross="2400.00")
        item = s.item(c, "200")
        s.accrual(item, "2031-02", "200", "2400.00")
        inv_a = s.invoice("DIGITAL-UI-D-A", "SupplierUiDelta", "2031-03-05", "35", "455.00")
        inv_b = s.invoice("DIGITAL-UI-D-B", "SupplierUiDelta", "2031-03-06", "40", "480.00")
        s.confirm(inv_a, c)
        s.confirm(inv_b, c)
        s.item_allocation(inv_a, item, "35", "455.00")
        s.item_allocation(inv_b, item, "40", "480.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)

    with app.state.session_factory() as session:
        preview = build_period_close_preview(session, UI_PERIOD)
    blockers = [b for b in preview.blockers if b.blocker_type == MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE]
    assert len(blockers) == 1
    assert not preview.prior_accrual_reversals, "no allocation may be auto-chosen as the reversal source"

    html = client.get(f"/period-close?period={UI_PERIOD}").text
    # Business-first content, visible without expanding anything.
    assert "发票总额已确认，但明细范围不足以自动冲销" in html
    assert "下一步" in html
    assert "补充原始暂估的商品明细" in html
    assert f'/contracts/{contract_id}?period={UI_PERIOD}' in html
    assert "当前版本尚不支持在此直接确认冲销范围。" in html

    # The raw code is reachable, but only inside a 技术详情 block.
    tech_index = html.find("技术详情")
    code_index = html.find(MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE)
    assert tech_index != -1 and code_index != -1
    assert code_index > tech_index, "the raw blocker code must be positioned inside the 技术详情 block"

    # Known-facts context is real Facts, not a new judgment: both confirmed
    # invoices' keys and the historical accrual amount are visible.
    assert "DIGITAL-UI-D-A" in html and "DIGITAL-UI-D-B" in html
    assert "2400.00" in html  # historical estimated cost


# ---- F. GET zero-write -----------------------------------------------------


def test_f_grouping_and_blocker_context_queries_write_nothing(tmp_path):
    def seed(s: _Seed):
        c = s.contract("PO-UI-F", "SupplierUiFoxtrot", gross="2400.00")
        item = s.item(c, "200")
        s.accrual(item, "2031-02", "200", "2400.00")
        inv_a = s.invoice("DIGITAL-UI-F-A", "SupplierUiFoxtrot", "2031-03-05", "35", "455.00")
        inv_b = s.invoice("DIGITAL-UI-F-B", "SupplierUiFoxtrot", "2031-03-06", "40", "480.00")
        s.confirm(inv_a, c)
        s.confirm(inv_b, c)
        s.item_allocation(inv_a, item, "35", "455.00")
        s.item_allocation(inv_b, item, "40", "480.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    before = _db_counts(app.state.session_factory)
    r1 = client.get(f"/period-close?period={UI_PERIOD}")
    r2 = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}")
    assert r1.status_code == 200 and r2.status_code == 200
    after = _db_counts(app.state.session_factory)
    assert before == after, "grouping/blocker-context GETs must not write a single row"


# ---- G. Preview parity still holds after the item/context refactor -------


def test_g_workbench_decision_payloads_still_match_the_frozen_engine(tmp_path):
    def seed(s: _Seed):
        c = s.contract("PO-UI-G", "SupplierUiGolf", gross="2400.00")
        item = s.item(c, "200")
        s.accrual(item, "2031-02", "200", "2400.00")
        inv_a = s.invoice("DIGITAL-UI-G-A", "SupplierUiGolf", "2031-03-05", "35", "455.00")
        inv_b = s.invoice("DIGITAL-UI-G-B", "SupplierUiGolf", "2031-03-06", "40", "480.00")
        s.confirm(inv_a, c)
        s.confirm(inv_b, c)
        s.item_allocation(inv_a, item, "35", "455.00")
        s.item_allocation(inv_b, item, "40", "480.00")
        return None

    client, app, _ = _build_app(tmp_path, seed)
    with app.state.session_factory() as session:
        workbench = get_period_close_workbench(session, UI_PERIOD)
        preview = build_period_close_preview(session, UI_PERIOD)

    assert [b.blocker for b in workbench.blockers] == list(preview.blockers)
    assert workbench.summary == preview.summary


# ---- H. Human Acceptance Fix — invoice item scope wording, blocker wording,
# and Evidence business labels (spec section 10). None of these touch the
# Rule Engine, Domain, or InvoiceItemAllocation semantics — Presentation
# only, driven by the SAME already-persisted Facts (existence of an
# allocation, and whether its target ContractItem carries real
# product_name Evidence).


def test_h_a_contract_scope_allocation_without_product_evidence_uses_scope_wording(tmp_path):
    """An InvoiceItemAllocation exists, but its target ContractItem has no
    product_name -> the page may only claim scope attribution, never the
    stronger "confirmed to a contract item" wording, and never the
    collapsed legacy "已确认关联" that conflated the two."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-HA", "SupplierUiHotelAlpha")
        item = s.item(c, "10", product_name=None)
        inv = s.invoice("DIGITAL-UI-HA", "SupplierUiHotelAlpha", "2031-03-05", "10", "500.00")
        s.confirm(inv, c)
        s.item_allocation(inv, item, "10", "500.00")
        return str(c.id)

    client, _, contract_id = _build_app(tmp_path, seed)
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text
    assert "已归属本合同范围" in html
    assert "已确认关联" not in html
    assert "已确认到合同商品" not in html


def test_h_b_contract_scope_allocation_with_real_product_evidence_uses_confirmed_wording(tmp_path):
    """An InvoiceItemAllocation whose target ContractItem carries a real
    product_name -> the stronger business label is warranted."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-HB", "SupplierUiHotelBravo")
        item = s.item(c, "10", product_name="Synthetic Product")
        inv = s.invoice("DIGITAL-UI-HB", "SupplierUiHotelBravo", "2031-03-06", "10", "500.00")
        s.confirm(inv, c)
        s.item_allocation(inv, item, "10", "500.00")
        return str(c.id)

    client, _, contract_id = _build_app(tmp_path, seed)
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text
    assert "已确认到合同商品" in html


def test_h_c_unallocated_invoice_item_uses_unassigned_wording(tmp_path):
    """No InvoiceItemAllocation at all -> the unassigned wording, never
    the legacy "尚未关联合同范围"."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-HC", "SupplierUiHotelCharlie")
        s.item(c, "10", product_name="Synthetic Product C")
        inv = s.invoice("DIGITAL-UI-HC", "SupplierUiHotelCharlie", "2031-03-07", "10", "500.00")
        s.confirm(inv, c)
        return str(c.id)

    client, _, contract_id = _build_app(tmp_path, seed)
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text
    assert "尚未归属本合同范围" in html
    assert "尚未关联合同范围" not in html


def test_h_d_blocker_known_facts_use_scope_attribution_wording(tmp_path):
    """MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE's known-facts card
    must describe the allocation count as scope attribution, never imply
    it already resolved the reversal-scope question (spec section 5)."""

    def seed(s: _Seed):
        c = s.contract("PO-UI-HD", "SupplierUiHotelDelta", gross="2400.00")
        item = s.item(c, "200")
        s.accrual(item, "2031-02", "200", "2400.00")
        inv_a = s.invoice("DIGITAL-UI-HD-A", "SupplierUiHotelDelta", "2031-03-05", "35", "455.00")
        inv_b = s.invoice("DIGITAL-UI-HD-B", "SupplierUiHotelDelta", "2031-03-06", "40", "480.00")
        s.confirm(inv_a, c)
        s.confirm(inv_b, c)
        s.item_allocation(inv_a, item, "35", "455.00")
        s.item_allocation(inv_b, item, "40", "480.00")
        return str(c.id)

    client, app, contract_id = _build_app(tmp_path, seed)
    with app.state.session_factory() as session:
        preview = build_period_close_preview(session, UI_PERIOD)
    blockers = [b for b in preview.blockers if b.blocker_type == MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE]
    assert len(blockers) == 1

    for html in (
        client.get(f"/period-close?period={UI_PERIOD}").text,
        client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text,
    ):
        assert "已归属本合同范围的发票明细数量" in html
        assert "已关联发票明细数量" not in html


def test_h_e_evidence_business_labels_with_raw_technical_traceability(tmp_path):
    """Evidence's first layer must show a Chinese business label for a
    known category/source_type — never the raw technical literal in the
    primary row — while the raw literal stays reachable under 技术信息
    (spec section 7/8/10.E). Uses the SAME source_type literals the real
    importers write (import_contract_ledger.py, import_invoices.py,
    import_bank.py, import_close_facts.py)."""
    from bel.domain.payment import Payment
    from bel.domain.matching import PaymentAllocation
    from bel.infrastructure.persistence.models import Base
    from bel.infrastructure.persistence.repositories import (
        PaymentAllocationRepository,
        PaymentRepository,
    )
    from bel.web.app import create_app

    now = datetime.now(timezone.utc)
    db_path = tmp_path / f"ui-h-e-{uuid.uuid4().hex[:8]}.db"
    engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with make_session_factory(engine)() as session:
        ev = EvidenceRepository(session)

        def _doc_fragment(source_type: str, sheet: str) -> EvidenceFragment:
            doc = EvidenceDocument(
                id=uuid.uuid4(), file_name=f"{source_type}.dat", sha256=uuid.uuid4().hex,
                source_type=source_type, imported_at=now,
            )
            ev.add_document(doc)
            frag = EvidenceFragment(
                id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.EXCEL_ROW,
                sheet_name=sheet, row_number=1, locator_json=None, raw_data={}, created_at=now,
            )
            ev.add_fragment(frag)
            session.flush()
            return frag

        contract_frag = _doc_fragment("contract_ledger_xlsx", "contracts")
        item_frag = _doc_fragment("close_fact_pack_json", "facts")
        invoice_frag = _doc_fragment("invoice_ledger_xlsx", "invoices")
        payment_frag = _doc_fragment("cmb_bank_statement_pdf", "bank")

        contract = Contract(
            id=uuid.uuid4(), contract_no="PO-UI-HE", contract_type=None, counterparty="SupplierUiHotelEcho",
            buyer="BuyerUI", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
            current_source_fragment_id=contract_frag.id, created_at=now, updated_at=now,
        )
        ContractRepository(session).add(contract)
        session.flush()

        ContractItemRepository(session).add(
            ContractItem(
                id=uuid.uuid4(), contract_id=contract.id, source_item_key="ITEM-A", sku=None,
                product_name="UI Widget HE", specification=None, quantity=Decimal("10"), unit="件",
                unit_price=None, gross_amount=None, tax_rate=None, net_amount=None,
                current_source_fragment_id=item_frag.id, created_at=now,
            )
        )
        session.flush()

        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no="DIGITAL-UI-HE", external_invoice_key="DIGITAL-UI-HE",
            issue_date=date(2031, 3, 8), seller="SupplierUiHotelEcho", buyer="BuyerUI",
            net_amount=Decimal("500.00"), tax_amount=Decimal("0"), gross_amount=Decimal("500.00"),
            invoice_status=None, source_fragment_id=invoice_frag.id, created_at=now, updated_at=now,
        )
        InvoiceRepository(session).add(invoice)
        session.flush()
        invoice_match_case = MatchCase(
            id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
            created_at=now, resolved_at=now,
        )
        MatchCaseRepository(session).add(invoice_match_case)
        session.flush()
        InvoiceAllocationRepository(session).add(
            InvoiceAllocation(
                id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id,
                match_case_id=invoice_match_case.id, allocated_gross_amount=invoice.gross_amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
            )
        )
        session.flush()

        payment = Payment(
            id=uuid.uuid4(), transaction_date=date(2031, 3, 9), direction="OUT",
            amount=Decimal("500.00"), counterparty="SupplierUiHotelEcho", business_type="采购款",
            bank_reference="REF-UI-HE", description=None, running_balance=None,
            source_fragment_id=payment_frag.id, created_at=now,
        )
        PaymentRepository(session).add(payment)
        session.flush()
        payment_match_case = MatchCase(
            id=uuid.uuid4(), subject_type="PAYMENT", subject_id=payment.id,
            status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
            created_at=now, resolved_at=now,
        )
        MatchCaseRepository(session).add(payment_match_case)
        session.flush()
        PaymentAllocationRepository(session).add(
            PaymentAllocation(
                id=uuid.uuid4(), payment_id=payment.id, contract_id=contract.id,
                match_case_id=payment_match_case.id, allocated_amount=payment.amount,
                match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
            )
        )
        session.commit()
        contract_id = str(contract.id)

    client = TestClient(create_app(f"sqlite:///{db_path}"))
    html = client.get(f"/contracts/{contract_id}?period={UI_PERIOD}").text

    assert "合同证据" in html
    assert "合同台账 Excel" in html
    assert "发票证据" in html
    assert "发票台账 Excel" in html
    assert "付款证据" in html
    assert "银行流水 PDF" in html
    assert "月结事实包" in html  # CONTRACT_ITEM's close_fact_pack_json source

    raw_values = ("contract_ledger_xlsx", "invoice_ledger_xlsx", "cmb_bank_statement_pdf", "close_fact_pack_json")
    for raw in raw_values:
        assert raw in html, f"{raw} must remain traceable in technical detail"
    tech_index = html.find("技术信息")
    assert tech_index != -1
    for raw in raw_values:
        assert html.find(raw, tech_index) > tech_index, f"{raw} must sit inside a 技术信息 block, not the primary row"

    # Raw category codes (the Evidence dataclass's ``category`` field, e.g.
    # "CONTRACT") must remain traceable too, alongside source_type — not
    # only the source_type literal (Gate A-Human-Fix finding).
    raw_categories = ("CONTRACT", "CONTRACT_ITEM", "INVOICE", "PAYMENT")
    for raw in raw_categories:
        idx = html.find(raw, tech_index)
        assert idx > tech_index, f"raw category {raw} must sit inside a 技术信息 block"
