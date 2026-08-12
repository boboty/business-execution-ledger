from bel.application.import_contract_ledger import import_contract_ledger
from bel.infrastructure.persistence.models import (
    ContractModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    ImportRunModel,
)

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_reimporting_the_same_file_creates_zero_additional_facts(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [[i, f"C{i:03d}", "Seller", "Buyer", 100 + i] for i in range(1, 6)],
    )

    first = import_contract_ledger(db_session, path)
    assert first.is_reimport is False
    assert first.contracts_created == 5

    second = import_contract_ledger(db_session, path)
    assert second.is_reimport is True
    assert second.contracts_created == 0
    assert second.evidence_document_id == first.evidence_document_id

    assert db_session.query(ContractModel).count() == 5  # not 10
    assert db_session.query(EvidenceDocumentModel).count() == 1  # not 2
    assert db_session.query(EvidenceFragmentModel).count() == 5  # not 10

    # An audit trail entry is fine — it's just not a new business fact.
    assert db_session.query(ImportRunModel).count() == 2
    runs = db_session.query(ImportRunModel).order_by(ImportRunModel.is_reimport).all()
    assert [r.is_reimport for r in runs] == [False, True]
