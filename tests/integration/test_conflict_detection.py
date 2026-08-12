from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.list_exceptions import list_exceptions
from bel.domain.event import BusinessEventType
from bel.domain.exception import ExceptionStatus, ExceptionType
from bel.infrastructure.persistence.models import BusinessEventModel, ContractModel

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "外销合同编码"]


def test_duplicate_contract_no_creates_two_contracts_and_one_conflict(db_session, ledger_workbook_factory):
    # Shape reproduces a real conflict found in production data (same
    # contract_no, two different counterparties) — names/amounts here
    # are synthetic, not the real business identifiers. See
    # docs/PHASE1-DECISIONS.md.
    path = ledger_workbook_factory(
        HEADERS,
        [
            [100, "DUP-CONTRACT-001", "Seller Alpha Co", "Buyer Co", 33894.40, "EXPORT-A"],
            [101, "DUP-CONTRACT-001", "Seller Beta Co", "Buyer Co", 23763.20, "EXPORT-A"],
        ],
    )
    result = import_contract_ledger(db_session, path)

    # Never merged or updated in place — two distinct Contract rows.
    assert result.contracts_created == 2
    contracts = db_session.query(ContractModel).filter_by(contract_no="DUP-CONTRACT-001").all()
    assert len(contracts) == 2
    assert len({c.id for c in contracts}) == 2  # distinct UUIDs, never merged
    assert {c.counterparty for c in contracts} == {"Seller Alpha Co", "Seller Beta Co"}

    assert len(result.business_key_conflicts) == 1
    conflict = result.business_key_conflicts[0]
    assert conflict.contract_no == "DUP-CONTRACT-001"
    assert set(conflict.contract_ids) == {c.id for c in contracts}

    exceptions = list_exceptions(db_session, open_only=True)
    assert len(exceptions) == 1
    assert exceptions[0].exception_type == ExceptionType.BUSINESS_KEY_CONFLICT
    assert exceptions[0].status == ExceptionStatus.OPEN
    assert set(exceptions[0].detail["contract_ids"]) == {str(c.id) for c in contracts}

    events = db_session.query(BusinessEventModel).filter_by(
        event_type=BusinessEventType.BUSINESS_KEY_CONFLICT_DETECTED
    ).all()
    assert len(events) == 1


def test_no_conflict_when_contract_no_is_unique(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [[1, "C001", "SellerA", "BuyerX", 100, "EXPORT001"], [2, "C002", "SellerB", "BuyerX", 200, "EXPORT002"]],
    )
    result = import_contract_ledger(db_session, path)
    assert result.business_key_conflicts == []
    assert list_exceptions(db_session, open_only=True) == []
