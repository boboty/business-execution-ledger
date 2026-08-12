from bel.application.import_contract_ledger import import_contract_ledger
from bel.infrastructure.persistence.models import EvidenceDocumentModel, EvidenceFragmentModel

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_import_creates_one_document_and_one_fragment_per_row(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 100],
            [2, "C002", "SellerB", "BuyerX", 200],
            [3, None, None, None, None],  # blank trailing row
        ],
    )
    result = import_contract_ledger(db_session, path)

    documents = db_session.query(EvidenceDocumentModel).all()
    fragments = db_session.query(EvidenceFragmentModel).all()

    assert len(documents) == 1
    assert documents[0].id == result.evidence_document_id
    assert len(fragments) == 3  # all rows preserved as evidence, including the blank one
    assert {f.row_number for f in fragments} == {3, 4, 5}  # header is row 2, data starts row 3


def test_fragment_raw_data_matches_source_cells_exactly(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(HEADERS, [[1, "C001", "SellerA", "BuyerX", 123.45]])
    import_contract_ledger(db_session, path)

    fragment = db_session.query(EvidenceFragmentModel).one()
    assert fragment.raw_data == {"序号": 1, "合同编码": "C001", "卖方": "SellerA", "买方": "BuyerX", "金额": 123.45}
