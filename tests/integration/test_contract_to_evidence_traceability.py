from bel.application.get_contract import get_contract
from bel.application.import_contract_ledger import import_contract_ledger

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "备注"]


def test_contract_traces_back_to_its_exact_workbook_sheet_row(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 100, "note1"],
            [2, "C002", "SellerB", "BuyerX", 200, "note2"],
        ],
        filename="traceability.xlsx",
    )
    result = import_contract_ledger(db_session, path)

    from bel.infrastructure.persistence.models import ContractModel

    c002 = db_session.query(ContractModel).filter_by(contract_no="C002").one()

    trace = get_contract(db_session, c002.id)
    assert trace is not None
    assert trace.contract.contract_no == "C002"
    assert trace.fragment.sheet_name == "报关出口购销合同"
    assert trace.fragment.row_number == 4  # header row 2, C001 at row 3, C002 at row 4
    assert trace.fragment.raw_data["备注"] == "note2"
    assert trace.document.id == result.evidence_document_id
    assert trace.document.file_name == "traceability.xlsx"


def test_get_contract_returns_none_for_unknown_id(db_session):
    import uuid

    assert get_contract(db_session, uuid.uuid4()) is None
