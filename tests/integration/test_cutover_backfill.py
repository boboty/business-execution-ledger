"""Phase 2D.1-R5 — identity-aware backfill (docs/ROADMAP.md 2D.1-R5,
docs/PHASE2D1-R0-DECISIONS.md section 4).

Covers the test matrix from the R5 spec section 54: exact-plan rerun,
revised-bytes-same-content rerun, business conflicts, missing/ambiguous
identity, and the no-resurrection bridge invariant. Uses the same
synthetic workbook factories as the rest of the suite
(tests/conftest.py) — no private data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from bel.adapters.pdf.cmb_bank_statement import ParsedBankTransaction
from bel.application.cutover_backfill import (
    backfill_contract_items,
    backfill_contracts,
    backfill_invoices,
    backfill_payment_transactions,
    backfill_procurement_sales_links,
    backfill_sales_contracts,
    backfill_shipments,
)
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.infrastructure.persistence.database import make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceRepository,
    PaymentRepository,
    ProcurementSalesLinkRepository,
    SalesContractRepository,
    ShipmentRepository,
)

NOW = datetime.now(timezone.utc)

CONTRACT_HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "外销合同编码"]
INVOICE_HEADERS_ROW = [
    "凭证模板", "凭证字号", "发票票种", "开票日期", "发票号码", "数电发票号码", "销方名称",
    "商品名称（明细）", "规格型号（明细）", "单位（明细）", "数量（明细）", "单价（明细）", "金额（明细）",
    "税率（%）（明细）", "税额（明细）", "价税合计（明细）", "发票状态", "发票金额", "发票税额", "发票价税合计",
]


@pytest.fixture
def db_session():
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        yield session


def _write_ledger(path: Path, rows: list[list]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "报关出口购销合同"
    ws.append(["Title"])
    ws.append(CONTRACT_HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)


def _write_invoices(path: Path, buyer: str, rows: list[list]) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "sheet1"
    ws.append([])
    ws.append([buyer])
    ws.append(INVOICE_HEADERS_ROW)
    for row in rows:
        ws.append(row)
    wb.save(path)


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


# ---------------------------------------------------------------------------
# A/B/C/D/E — Contract backfill
# ---------------------------------------------------------------------------


def test_a_exact_plan_rerun_no_duplicate(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    first = backfill_contracts(db_session, path)
    second = backfill_contracts(db_session, path)
    assert first.created == 1
    assert second.created == 0
    assert second.replay_or_corroborating == 0  # is_reimport short-circuit — same bytes
    assert len(ContractRepository(db_session).list_all()) == 1


def test_b_revised_bytes_same_business_content_no_duplicate(db_session, tmp_path):
    path1 = tmp_path / "ledger1.xlsx"
    path2 = tmp_path / "ledger2.xlsx"
    _write_ledger(path1, [[1, "C001", "SellerA", "BuyerX", 100]])
    # Different file bytes (the 序号/sequence column, which is never
    # promoted to a canonical field, differs) but identical business
    # content — this is the R0 bug plain file-sha idempotency cannot
    # solve (spec section 26).
    _write_ledger(path2, [[99, "C001", "SellerA", "BuyerX", 100]])
    first = backfill_contracts(db_session, path1)
    second = backfill_contracts(db_session, path2)
    assert first.created == 1
    assert second.created == 0
    assert second.replay_or_corroborating == 1  # corroborating — different fragment, same content
    assert len(ContractRepository(db_session).list_all()) == 1


def test_c_revised_value_conflict_produces_task_never_overwrites(db_session, tmp_path):
    path1 = tmp_path / "ledger1.xlsx"
    path2 = tmp_path / "ledger2.xlsx"
    _write_ledger(path1, [[1, "C001", "SellerA", "BuyerX", 100]])
    _write_ledger(path2, [[1, "C001", "SellerA", "BuyerX", 999]])  # different amount
    backfill_contracts(db_session, path1)
    second = backfill_contracts(db_session, path2)
    assert len(second.tasks) == 1
    assert second.tasks[0].kind == "CONFLICT"
    current = ContractRepository(db_session).find_by_contract_no("C001")[0]
    assert current.gross_amount == Decimal("100.00")  # untouched — "latest wins" is forbidden


def test_d_missing_identity_produces_task(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", None, "BuyerX", 100]])  # counterparty missing
    result = backfill_contracts(db_session, path)
    assert result.created == 0
    assert result.tasks[0].kind == "IDENTITY_INCOMPLETE"
    assert ContractRepository(db_session).list_all() == []


def test_e_duplicate_identity_ambiguity_never_guesses(db_session, tmp_path):
    frag = _make_fragment(db_session)
    from bel.domain.contract import Contract

    c1 = Contract(
        id=uuid.uuid4(), contract_no="C-DUP", contract_type=None, counterparty="Sup", buyer=None,
        gross_amount=Decimal("1"), currency="CNY", contract_date=None, current_source_fragment_id=frag.id,
        created_at=NOW, updated_at=NOW,
    )
    c2 = Contract(
        id=uuid.uuid4(), contract_no="C-DUP", contract_type=None, counterparty="Sup", buyer=None,
        gross_amount=Decimal("2"), currency="CNY", contract_date=None, current_source_fragment_id=frag.id,
        created_at=NOW, updated_at=NOW,
    )
    ContractRepository(db_session).add(c1)
    ContractRepository(db_session).add(c2)
    db_session.flush()

    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C-DUP", "Sup", "BuyerX", 3]])
    result = backfill_contracts(db_session, path)
    assert result.tasks[0].kind == "IDENTITY_AMBIGUOUS"
    assert len(ContractRepository(db_session).list_all()) == 2  # no merge, no third row


# ---------------------------------------------------------------------------
# F/G — ContractItem backfill
# ---------------------------------------------------------------------------


def test_f_contract_item_exact_replay(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    entries = [{"contract_no": "C001", "counterparty": "SellerA", "source_item_key": "ITEM-1", "fields": {"product_name": "Widget"}}]
    first = backfill_contract_items(db_session, entries, source_fragment_id=frag.id)
    second = backfill_contract_items(db_session, entries, source_fragment_id=frag.id)
    assert first.created == 1
    assert second.replay_or_corroborating == 1


def test_g_contract_item_missing_source_item_key_produces_task(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    entries = [{"contract_no": "C001", "counterparty": "SellerA", "source_item_key": None, "fields": {}}]
    result = backfill_contract_items(db_session, entries, source_fragment_id=frag.id)
    assert result.tasks[0].kind == "IDENTITY_INCOMPLETE"


# ---------------------------------------------------------------------------
# H/I/J — Invoice backfill
# ---------------------------------------------------------------------------


def test_h_invoice_exact_replay_by_external_key(db_session, tmp_path):
    path1 = tmp_path / "inv1.xlsx"
    path2 = tmp_path / "inv2.xlsx"
    row1 = ["T", "V1", "增值税专用发票", date(2026, 1, 5), "INV-1", "DIGITAL-1", "Seller", "Widget", None, "PCS", 10, 10, 100, 0, 0, 100, None, 100, 0, 100]
    # V2 differs (an Evidence-only field, never promoted to a canonical
    # Invoice field) — same real-file-revision scenario as test_b.
    row2 = ["T", "V2", "增值税专用发票", date(2026, 1, 5), "INV-1", "DIGITAL-1", "Seller", "Widget", None, "PCS", 10, 10, 100, 0, 0, 100, None, 100, 0, 100]
    _write_invoices(path1, "BuyerX", [row1])
    _write_invoices(path2, "BuyerX", [row2])
    first = backfill_invoices(db_session, path1, "PURCHASE")
    second = backfill_invoices(db_session, path2, "PURCHASE")
    assert first.created == 1
    assert second.replay_or_corroborating == 1
    assert len(InvoiceRepository(db_session).list_all()) == 1


def test_i_invoice_missing_external_key_produces_task(db_session, tmp_path):
    path = tmp_path / "inv.xlsx"
    row = ["T", "V1", "增值税专用发票", date(2026, 1, 5), "INV-1", None, "Seller", "Widget", None, "PCS", 10, 10, 100, 0, 0, 100, None, 100, 0, 100]
    _write_invoices(path, "BuyerX", [row])
    result = backfill_invoices(db_session, path, "PURCHASE")
    assert result.created == 0
    assert result.tasks[0].kind == "IDENTITY_INCOMPLETE"


def test_j_invoice_same_key_conflicting_content_produces_task(db_session, tmp_path):
    path1 = tmp_path / "inv1.xlsx"
    path2 = tmp_path / "inv2.xlsx"
    row1 = ["T", "V1", "增值税专用发票", date(2026, 1, 5), "INV-1", "DIGITAL-1", "Seller", "Widget", None, "PCS", 10, 10, 100, 0, 0, 100, None, 100, 0, 100]
    row2 = ["T", "V1", "增值税专用发票", date(2026, 1, 5), "INV-1", "DIGITAL-1", "Seller", "Widget", None, "PCS", 10, 10, 200, 0, 0, 200, None, 200, 0, 200]
    _write_invoices(path1, "BuyerX", [row1])
    _write_invoices(path2, "BuyerX", [row2])
    backfill_invoices(db_session, path1, "PURCHASE")
    second = backfill_invoices(db_session, path2, "PURCHASE")
    assert second.tasks[0].kind == "CONFLICT"
    existing = InvoiceRepository(db_session).find_by_external_key("DIGITAL-1")
    assert existing.net_amount == Decimal("100")  # untouched


# ---------------------------------------------------------------------------
# K/L/M/N/O — Payment backfill
# ---------------------------------------------------------------------------


def _txn(index=0, amount=Decimal("100.00"), bank_reference="REF-1", counterparty="Cust"):
    return ParsedBankTransaction(
        page_index=0, transaction_index=index, raw_data={"n": index}, transaction_date=date(2026, 1, 5),
        business_type="转账", bank_reference=bank_reference, description="desc", signed_amount=amount,
        running_balance=Decimal("1000.00"), counterparty=counterparty,
    )


def test_k_payment_exact_replay(db_session):
    doc_id = uuid.uuid4()
    EvidenceRepository(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="a" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    first = backfill_payment_transactions(db_session, [_txn()], source_account_id="ACC-1", document_id=doc_id)
    second = backfill_payment_transactions(db_session, [_txn()], source_account_id="ACC-1", document_id=doc_id)
    assert first.created == 1
    assert second.replay_or_corroborating == 1
    assert len(PaymentRepository(db_session).list_all()) == 1


def test_l_different_source_accounts_are_two_distinct_payments(db_session):
    doc_id = uuid.uuid4()
    EvidenceRepository(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="b" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    backfill_payment_transactions(db_session, [_txn()], source_account_id="ACC-1", document_id=doc_id)
    result = backfill_payment_transactions(db_session, [_txn()], source_account_id="ACC-2", document_id=doc_id)
    assert result.created == 1  # same date/direction/amount/bank_reference, DIFFERENT account -> two Payments
    assert len(PaymentRepository(db_session).list_all()) == 2


def test_m_missing_source_account_produces_task_never_silent_dedup(db_session):
    doc_id = uuid.uuid4()
    EvidenceRepository(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="c" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    result = backfill_payment_transactions(db_session, [_txn()], source_account_id=None, document_id=doc_id)
    assert result.created == 0
    assert result.tasks[0].kind == "IDENTITY_INCOMPLETE"
    assert result.tasks[0].detail["missing_source_account_id"] is True


def test_n_missing_bank_reference_produces_task(db_session):
    doc_id = uuid.uuid4()
    EvidenceRepository(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="d" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    result = backfill_payment_transactions(
        db_session, [_txn(bank_reference=None)], source_account_id="ACC-1", document_id=doc_id
    )
    assert result.tasks[0].detail["missing_bank_reference"] is True


def test_o_same_identity_conflicting_evidence_produces_task(db_session):
    doc_id = uuid.uuid4()
    EvidenceRepository(db_session).add_document(
        EvidenceDocument(id=doc_id, file_name="x", sha256="e" * 64, source_type="cmb_bank_statement_pdf", imported_at=NOW)
    )
    db_session.flush()
    backfill_payment_transactions(db_session, [_txn(counterparty="Cust A")], source_account_id="ACC-1", document_id=doc_id)
    result = backfill_payment_transactions(
        db_session, [_txn(counterparty="Cust B")], source_account_id="ACC-1", document_id=doc_id
    )
    assert result.tasks[0].kind == "CONFLICT"
    assert len(PaymentRepository(db_session).list_all()) == 1  # existing untouched, no second row


# ---------------------------------------------------------------------------
# P/Q — Shipment backfill (delegates to R2's create_shipment_fact)
# ---------------------------------------------------------------------------


def test_p_shipment_complete_identity_replay(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    entries = [{"contract_no": "C001", "counterparty": "SellerA", "external_reference": "EXP-1", "execution_date": date(2026, 1, 10), "quantity": Decimal("5")}]
    first = backfill_shipments(db_session, entries, source_fragment_id=frag.id)
    second = backfill_shipments(db_session, entries, source_fragment_id=frag.id)
    assert first.created == 1
    assert second.replay_or_corroborating == 1


def test_q_shipment_incomplete_identity_never_silently_deduped(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    entries = [{"contract_no": "C001", "counterparty": "SellerA", "external_reference": None, "execution_date": date(2026, 1, 10), "quantity": Decimal("5")}]
    result = backfill_shipments(db_session, entries, source_fragment_id=frag.id)
    assert result.created == 0
    assert result.tasks[0].kind == "CONFLICT"  # ShipmentIdentityIncomplete surfaces as a Task, no Shipment created
    assert ShipmentRepository(db_session).list_all() == []


# ---------------------------------------------------------------------------
# R/S/T — SalesContract backfill
# ---------------------------------------------------------------------------


def test_r_sales_contract_missing_our_entity_rejected(db_session):
    frag = _make_fragment(db_session)
    entries = [{"our_entity": None, "sales_contract_no": "SC-1", "fields": {}}]
    result = backfill_sales_contracts(db_session, entries, source_fragment_id=frag.id)
    assert result.tasks[0].kind == "CONFLICT"
    assert SalesContractRepository(db_session).list_all() == []


def test_s_sales_contract_missing_sales_contract_no_rejected(db_session):
    frag = _make_fragment(db_session)
    entries = [{"our_entity": "Entity A", "sales_contract_no": None, "fields": {}}]
    result = backfill_sales_contracts(db_session, entries, source_fragment_id=frag.id)
    assert result.tasks[0].kind == "CONFLICT"


def test_t_sales_contract_customer_null_still_creates_anchor(db_session):
    frag = _make_fragment(db_session)
    entries = [{"our_entity": "Entity A", "sales_contract_no": "SC-1", "fields": {}}]
    result = backfill_sales_contracts(db_session, entries, source_fragment_id=frag.id)
    assert result.created == 1
    sc = SalesContractRepository(db_session).list_all()[0]
    assert sc.customer is None


# ---------------------------------------------------------------------------
# V/W/X — ProcurementSalesLink backfill (replay, no-resurrection, reestablish)
# ---------------------------------------------------------------------------


def test_v_link_current_replay(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    backfill_sales_contracts(db_session, [{"our_entity": "Entity A", "sales_contract_no": "SC-1", "fields": {}}], source_fragment_id=frag.id)

    entries = [{"contract_no": "C001", "counterparty": "SellerA", "sales_our_entity": "Entity A", "sales_contract_no": "SC-1"}]
    first = backfill_procurement_sales_links(db_session, entries, source_fragment_id=frag.id)
    second = backfill_procurement_sales_links(db_session, entries, source_fragment_id=frag.id)
    assert first.created == 1
    assert second.replay_or_corroborating == 1


def test_w_historical_link_replay_never_resurrects(db_session, tmp_path):
    """Create P->S episode E1, invalidate it, then rerun backfill with
    the SAME historical Evidence — the retired episode must NOT become
    current again (section 16/46, HARD)."""
    from bel.application.procurement_sales_link import correct_procurement_sales_link
    from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType

    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    backfill_sales_contracts(db_session, [{"our_entity": "Entity A", "sales_contract_no": "SC-1", "fields": {}}], source_fragment_id=frag.id)

    entries = [{"contract_no": "C001", "counterparty": "SellerA", "sales_our_entity": "Entity A", "sales_contract_no": "SC-1"}]
    first = backfill_procurement_sales_links(db_session, entries, source_fragment_id=frag.id)
    assert first.created == 1

    contract = ContractRepository(db_session).find_by_contract_no("C001")[0]
    sales_contract = SalesContractRepository(db_session).list_all()[0]
    link = ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id)

    invalidate_frag = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=link.id, source_fragment_id=invalidate_frag.id,
        confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )

    # Rerun with the SAME (frag.id) historical Evidence.
    rerun = backfill_procurement_sales_links(db_session, entries, source_fragment_id=frag.id)
    assert ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id) is None
    # add_procurement_sales_link never resurrects — a replay against a
    # retired-only business key is either a no-op or an explicit new
    # ADD attempt rejected by the link module's own semantics; either
    # way, no current episode may result from this rerun.


def test_x_explicit_reestablish_still_works_outside_backfill(db_session, tmp_path):
    from bel.application.procurement_sales_link import (
        correct_procurement_sales_link,
        reestablish_procurement_sales_link,
    )
    from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType

    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100]])
    backfill_contracts(db_session, path)
    frag = _make_fragment(db_session)
    backfill_sales_contracts(db_session, [{"our_entity": "Entity A", "sales_contract_no": "SC-1", "fields": {}}], source_fragment_id=frag.id)
    entries = [{"contract_no": "C001", "counterparty": "SellerA", "sales_our_entity": "Entity A", "sales_contract_no": "SC-1"}]
    backfill_procurement_sales_links(db_session, entries, source_fragment_id=frag.id)

    contract = ContractRepository(db_session).find_by_contract_no("C001")[0]
    sales_contract = SalesContractRepository(db_session).list_all()[0]
    link = ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id)
    invalidate_frag = _make_fragment(db_session)
    correct_procurement_sales_link(
        db_session, superseded_link_id=link.id, source_fragment_id=invalidate_frag.id,
        confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )

    new_frag = _make_fragment(db_session)
    reestablish_procurement_sales_link(
        db_session, procurement_contract_id=contract.id, sales_contract_id=sales_contract.id,
        source_fragment_id=new_frag.id, confirmation_type=LinkConfirmationType.HUMAN_CONFIRMED, created_at=NOW,
    )
    assert ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sales_contract.id) is not None


# ---------------------------------------------------------------------------
# 47 — backfill never reads expected/ as a Fact source (structural check)
# ---------------------------------------------------------------------------


def test_backfill_module_never_touches_private_root_resolution():
    import inspect

    import bel.application.cutover_backfill as module

    source = inspect.getsource(module)
    assert "os.environ" not in source
    assert "resolve_private_root" not in source
    assert "import os" not in source


# ---------------------------------------------------------------------------
# Gate-fix — frozen legacy-ledger sales-scope basis (section 2)
# ---------------------------------------------------------------------------


def test_sales_scope_basis_established_from_same_row(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100, "SC-BASIS-1"]])
    backfill_contracts(db_session, path)

    contract = ContractRepository(db_session).find_by_contract_no("C001")[0]
    sales_contracts = SalesContractRepository(db_session).list_all()
    assert len(sales_contracts) == 1
    sc = sales_contracts[0]
    assert sc.our_entity == "BuyerX"  # 买方 on the SAME row
    assert sc.sales_contract_no == "SC-BASIS-1"  # 外销合同编码 on the SAME row
    assert sc.customer is None  # never inferred
    assert ProcurementSalesLinkRepository(db_session).get_current_link(contract.id, sc.id) is not None


def test_sales_scope_basis_absent_when_export_code_missing(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100, None]])
    backfill_contracts(db_session, path)
    assert SalesContractRepository(db_session).list_all() == []


def test_sales_scope_basis_absent_when_buyer_missing(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", None, 100, "SC-BASIS-2"]])
    backfill_contracts(db_session, path)
    assert SalesContractRepository(db_session).list_all() == []


def test_sales_scope_basis_never_stitches_across_rows(db_session, tmp_path):
    """Row 1 supplies our_entity (买方) but no export code; row 2 supplies
    an export code but has a DIFFERENT buyer — neither row alone
    satisfies both halves, and the two must never be combined."""
    path = tmp_path / "ledger.xlsx"
    _write_ledger(
        path,
        [
            [1, "C001", "SellerA", "BuyerX", 100, None],
            [2, "C002", "SellerB", "BuyerY", 200, "SC-BASIS-3"],
        ],
    )
    backfill_contracts(db_session, path)
    sales_contracts = SalesContractRepository(db_session).list_all()
    assert len(sales_contracts) == 1
    assert sales_contracts[0].our_entity == "BuyerY"  # only row 2's OWN pair, never row 1's buyer


def test_sales_scope_basis_reruns_idempotently(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", "SellerA", "BuyerX", 100, "SC-BASIS-4"]])
    backfill_contracts(db_session, path)
    path2 = tmp_path / "ledger2.xlsx"
    _write_ledger(path2, [[99, "C001", "SellerA", "BuyerX", 100, "SC-BASIS-4"]])
    backfill_contracts(db_session, path2)  # revised bytes, same business content
    assert len(SalesContractRepository(db_session).list_all()) == 1


# ---------------------------------------------------------------------------
# Gate-fix — persistent, idempotent OPEN TaskException (section 1)
# ---------------------------------------------------------------------------


def test_identity_incomplete_task_is_persisted_open(db_session, tmp_path):
    path = tmp_path / "ledger.xlsx"
    _write_ledger(path, [[1, "C001", None, "BuyerX", 100, None]])
    result = backfill_contracts(db_session, path)
    assert len(result.tasks) == 1
    task_id = result.tasks[0].task_exception_id

    from bel.domain.exception import ExceptionStatus, ExceptionType
    from bel.infrastructure.persistence.repositories import ExceptionRepository

    persisted = [e for e in ExceptionRepository(db_session).list_all() if e.id == task_id]
    assert len(persisted) == 1
    assert persisted[0].status == ExceptionStatus.OPEN
    assert persisted[0].exception_type == ExceptionType.BACKFILL_IDENTITY_INCOMPLETE
    assert persisted[0].detail["identity_key"]  # structured, not a parsed summary


def test_identity_incomplete_task_rerun_reuses_same_open_task(db_session, tmp_path):
    """A rerun hitting the SAME underlying identity problem must reuse
    the existing OPEN TaskException, never create a duplicate."""
    path1 = tmp_path / "ledger1.xlsx"
    path2 = tmp_path / "ledger2.xlsx"
    _write_ledger(path1, [[1, "C001", None, "BuyerX", 100, None]])
    _write_ledger(path2, [[99, "C001", None, "BuyerX", 100, None]])  # revised bytes, same problem

    from bel.domain.exception import ExceptionType
    from bel.infrastructure.persistence.repositories import ExceptionRepository

    result1 = backfill_contracts(db_session, path1)
    result2 = backfill_contracts(db_session, path2)
    assert result1.tasks[0].task_exception_id == result2.tasks[0].task_exception_id
    open_tasks = [
        e for e in ExceptionRepository(db_session).list_open() if e.exception_type == ExceptionType.BACKFILL_IDENTITY_INCOMPLETE
    ]
    assert len(open_tasks) == 1


def test_conflict_task_is_persisted_open(db_session, tmp_path):
    path1 = tmp_path / "ledger1.xlsx"
    path2 = tmp_path / "ledger2.xlsx"
    _write_ledger(path1, [[1, "C001", "SellerA", "BuyerX", 100, None]])
    _write_ledger(path2, [[1, "C001", "SellerA", "BuyerX", 999, None]])
    backfill_contracts(db_session, path1)
    result = backfill_contracts(db_session, path2)

    from bel.domain.exception import ExceptionStatus, ExceptionType
    from bel.infrastructure.persistence.repositories import ExceptionRepository

    task_id = result.tasks[0].task_exception_id
    persisted = ExceptionRepository(db_session).list_all()
    match = next(e for e in persisted if e.id == task_id)
    assert match.status == ExceptionStatus.OPEN
    assert match.exception_type == ExceptionType.BACKFILL_CONFLICT
