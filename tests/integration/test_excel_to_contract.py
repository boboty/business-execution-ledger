from decimal import Decimal

from bel.application.import_contract_ledger import import_contract_ledger
from bel.infrastructure.persistence.models import ContractItemModel, ContractModel

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额"]


def test_import_creates_contracts_with_correct_fields(db_session, ledger_workbook_factory):
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "Buyer Co", 100.50],
            [2, "C002", "SellerB", "Buyer Co", 200],
        ],
    )
    result = import_contract_ledger(db_session, path)

    assert result.contracts_created == 2
    assert result.contract_items_created == 0

    contracts = db_session.query(ContractModel).order_by(ContractModel.contract_no).all()
    assert [c.contract_no for c in contracts] == ["C001", "C002"]
    assert contracts[0].counterparty == "SellerA"
    assert contracts[0].buyer == "Buyer Co"
    assert contracts[0].gross_amount == Decimal("100.50")
    assert contracts[0].currency == "CNY"
    assert contracts[0].contract_date is None  # no date evidence column exists — never inferred
    assert contracts[0].current_source_fragment_id is not None


def test_import_never_synthesizes_contract_items(db_session, ledger_workbook_factory):
    """Hard requirement: this ledger has no product/quantity columns, so
    ContractItem count must stay exactly zero after import. See
    section 9 of the Phase 1 spec — this is a BLOCKER-level check."""
    path = ledger_workbook_factory(
        HEADERS,
        [[i, f"C{i:03d}", "Seller", "Buyer", 100 + i] for i in range(1, 6)],
    )
    result = import_contract_ledger(db_session, path)

    assert result.contracts_created == 5
    assert result.contract_items_created == 0
    assert db_session.query(ContractItemModel).count() == 0
