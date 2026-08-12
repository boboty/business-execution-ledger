"""Contract 360° web tests.

The page must show the contract, its items, the confirmed invoices
(with the item-allocation state), the explicitly-allocated payments,
the accrual balances (derived via the shared domain function), the
current-period close judgment (the SAME engine, filtered — never a
second rule set), and the aggregated evidence. All GET, strictly
read-only.
"""

from __future__ import annotations

from tests.web.conftest import CLOSE_PERIOD_FIXTURE


def _db_counts(session_factory) -> dict[str, int]:
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

    models = [
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
    ]
    with session_factory() as session:
        return {m.__tablename__: session.query(m).count() for m in models}


def test_contract_header(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "合同360°" in html
    assert "合同信息" in html
    assert "PO-CLOSE-001" in html
    assert "SupplierCloseAlpha" in html
    assert "1300.00" in html
    assert "CNY" in html


def test_contract_items_shown(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "合同商品" in html
    assert "ITEM-A" in html
    assert "Alpha Widget" in html
    assert "当前暂估状态" in html


def test_invoice_area_with_manual_allocation_state(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "发票" in html
    assert "DIGITAL-CLOSE-001" in html
    assert "进项" in html
    assert "匹配方式" in html
    assert "AUTO_CONFIRMED" in html
    # line 1 is already linked to a contract item via the fact pack
    assert "已关联" in html
    # invoice item table columns
    assert "行号" in html
    assert "未税金额" in html


def test_unallocated_invoice_item_offers_manual_allocation_form(web_client, contract_id_by_no):
    """PO-CLOSE-006 is contract-confirmed but has no item match — the page
    must offer an explicit, non-preselected allocation form."""
    contract_id = contract_id_by_no["PO-CLOSE-006"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "未关联" in html
    assert "关联合同明细" in html
    assert "请选择合同商品" in html  # never preselected
    assert "原发票数量" in html
    assert "DIGITAL-CLOSE-006" in html


def test_payment_area_shows_only_allocated_payments(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "付款" in html
    assert "付款/收款" in html
    assert "455.00" in html
    assert "SupplierCloseAlpha" in html
    assert "AUTO_CONFIRMED" in html


def test_accrual_balance_area(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "暂估余额" in html
    assert "2031-02" in html  # source period
    assert "100" in html  # original quantity
    assert "1200.00" in html  # original estimated cost
    assert "剩余数量" in html
    assert "未红冲" in html  # ACTIVE (no committed reversals)


def test_current_period_decisions_filtered_to_contract(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "当前期间业务判断" in html
    # S2B-01 partial reversal judgment for THIS contract
    assert "到票数量" in html
    assert "部分红冲" in html


def test_contract_with_blocker_shows_its_blocker(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-006"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "ITEM_MATCH_REQUIRED_FOR_REVERSAL" in html
    assert "已确认到票，但尚未确认发票明细对应哪个合同商品" in html


def test_evidence_aggregation(web_client, contract_id_by_no):
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    html = web_client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "证据" in html
    assert "合同 Evidence" in html
    assert "合同商品 Evidence" in html
    assert "发票 Evidence" in html
    assert "付款 Evidence" in html
    assert "历史暂估事实 Evidence" in html
    assert "人工明细关联 Evidence" in html
    assert "元数据" in html
    assert "close_fact_pack_json" in html  # source type of fact-pack evidence


def test_contract_360_get_is_zero_write(app_for_client, contract_id_by_no):
    client, app = app_for_client
    contract_id = contract_id_by_no["PO-CLOSE-001"]
    before = _db_counts(app.state.session_factory)
    response = client.get(f"/contracts/{contract_id}?period={CLOSE_PERIOD_FIXTURE}")
    assert response.status_code == 200
    after = _db_counts(app.state.session_factory)
    assert before == after, "GET /contracts/{id} must not write a single row"


def _build_many_to_many_contract_ctx(tmp_path):
    """Build the Domain's many-to-many attack: one Invoice confirmed to TWO
    Contracts, with the item allocation belonging to Contract B only."""
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal

    from fastapi.testclient import TestClient

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
    from bel.domain.accrual import InvoiceItemAllocation
    from bel.infrastructure.persistence.database import make_engine, make_session_factory
    from bel.infrastructure.persistence.models import Base
    from bel.infrastructure.persistence.repositories import (
        ContractItemRepository,
        ContractRepository,
        EvidenceRepository,
        InvoiceAllocationRepository,
        InvoiceItemAllocationRepository,
        InvoiceItemRepository,
        InvoiceRepository,
        MatchCaseRepository,
    )
    from bel.web.app import create_app

    now = datetime.now(timezone.utc)
    db_path = tmp_path / f"m2m-{uuid.uuid4().hex[:8]}.db"
    engine = make_engine(str(db_path))
    Base.metadata.create_all(engine)

    with make_session_factory(engine)() as session:
        ev = EvidenceRepository(session)
        doc = EvidenceDocument(
            id=uuid.uuid4(), file_name="synthetic.xlsx", sha256=uuid.uuid4().hex,
            source_type="synthetic", imported_at=now,
        )
        ev.add_document(doc)
        frag = EvidenceFragment(
            id=uuid.uuid4(), evidence_document_id=doc.id, fragment_kind=FragmentKind.EXCEL_ROW,
            sheet_name="s1", row_number=1, locator_json=None, raw_data={}, created_at=now,
        )
        ev.add_fragment(frag)
        session.flush()

        def _contract(no: str) -> Contract:
            c = Contract(
                id=uuid.uuid4(), contract_no=no, contract_type=None, counterparty="SupplierM2M",
                buyer="BuyerM2M", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=None,
                current_source_fragment_id=frag.id, created_at=now, updated_at=now,
            )
            ContractRepository(session).add(c)
            session.flush()
            return c

        def _item(contract: Contract, key: str) -> ContractItem:
            i = ContractItem(
                id=uuid.uuid4(), contract_id=contract.id, source_item_key=key, sku=None,
                product_name=f"Item {key}", specification=None, quantity=Decimal("100"), unit="件",
                unit_price=None, gross_amount=None, tax_rate=None, net_amount=None,
                current_source_fragment_id=frag.id, created_at=now,
            )
            ContractItemRepository(session).add(i)
            session.flush()
            return i

        contract_a = _contract("PO-M2M-A")
        contract_b = _contract("PO-M2M-B")
        item_a = _item(contract_a, "ITEM-A")
        item_b = _item(contract_b, "ITEM-B")

        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no="DIGITAL-M2M-001", external_invoice_key="DIGITAL-M2M-001",
            issue_date=__import__("datetime").date(2031, 3, 15), seller="SupplierM2M", buyer="BuyerM2M",
            net_amount=Decimal("1000.00"), tax_amount=Decimal("0"), gross_amount=Decimal("1000.00"),
            invoice_status=None, source_fragment_id=frag.id, created_at=now, updated_at=now,
        )
        InvoiceRepository(session).add(invoice)
        session.flush()
        line1 = InvoiceItem(
            id=uuid.uuid4(), invoice_id=invoice.id, line_no=1, product_name="M2M Widget",
            specification=None, unit="件", quantity=Decimal("50"), unit_price=None,
            net_amount=Decimal("1000.00"), tax_rate=None, tax_amount=Decimal("0"),
            gross_amount=Decimal("1000.00"), source_fragment_id=frag.id,
        )
        InvoiceItemRepository(session).add(line1)
        session.flush()

        def _confirm(contract: Contract) -> None:
            match_case = MatchCase(
                id=uuid.uuid4(), subject_type="INVOICE", subject_id=invoice.id,
                status=MatchCaseStatus.AUTO_CONFIRMED, match_method=MatchMethod.M001,
                created_at=now, resolved_at=now,
            )
            MatchCaseRepository(session).add(match_case)
            session.flush()
            InvoiceAllocationRepository(session).add(
                InvoiceAllocation(
                    id=uuid.uuid4(), invoice_id=invoice.id, contract_id=contract.id,
                    match_case_id=match_case.id, allocated_gross_amount=invoice.gross_amount,
                    match_method=AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_UNIQUE,
                    confirmation_type=ConfirmationType.AUTO_CONFIRMED, created_at=now,
                )
            )
            session.flush()

        # Invoice X confirmed to BOTH contracts (Domain: Invoice ↔ Contract
        # is many-to-many).
        _confirm(contract_a)
        _confirm(contract_b)
        # The item allocation belongs to Contract B's item ONLY.
        InvoiceItemAllocationRepository(session).add(
            InvoiceItemAllocation(
                id=uuid.uuid4(), invoice_item_id=line1.id, contract_item_id=item_b.id,
                allocated_quantity=Decimal("10"), allocated_net_amount=Decimal("200.00"),
                confirmation_type="MANUAL_CONFIRMED", source_fragment_id=frag.id, created_at=now,
            )
        )
        session.commit()
        ids = {"A": str(contract_a.id), "B": str(contract_b.id)}

    return TestClient(create_app(str(db_path))), ids


def test_contract360_item_allocation_is_scoped_to_own_contract(tmp_path):
    """Domain many-to-many attack: an Invoice confirmed to Contracts A and
    B, with the item allocation owned by Contract B. Contract A must see
    line 1 as 未关联 with the manual-allocation form; Contract B sees
    已关联 without it. The allocation must never leak across contracts."""
    client, ids = _build_many_to_many_contract_ctx(tmp_path)

    page_a = client.get(f"/contracts/{ids['A']}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "DIGITAL-M2M-001" in page_a
    assert "未关联" in page_a, "Contract A must show line 1 as unlinked"
    assert "请选择合同商品" in page_a, "Contract A must still offer the manual-allocation form"

    page_b = client.get(f"/contracts/{ids['B']}?period={CLOSE_PERIOD_FIXTURE}").text
    assert "已关联" in page_b, "Contract B must show line 1 as linked"
    assert "请选择合同商品" not in page_b, "Contract B must NOT offer the allocation form"
    assert "未关联" not in page_b
