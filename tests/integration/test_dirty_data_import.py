import datetime

from bel.application.import_contract_ledger import import_contract_ledger
from bel.infrastructure.persistence.models import EvidenceFragmentModel

HEADERS = ["序号", "合同编码", "卖方", "买方", "金额", "是否付款", "进项发票入账月", "退税到账日期", "备注"]


def test_dirty_ledger_data_imports_without_error_and_stays_traceable(db_session, ledger_workbook_factory):
    """Exercises independently constructed dirty-data shapes in one small
    workbook: a column mixing '是' strings with date cells (是否付款), a
    column mixing dates with free-text labels (进项发票入账月), a
    malformed date-like string (退税到账日期), and a stray number in a
    text column (备注). None of this may raise, and none of it may be
    silently reinterpreted. See spec section 8 and section 15."""
    path = ledger_workbook_factory(
        HEADERS,
        [
            [1, "C001", "SellerA", "BuyerX", 100, "是", "5月暂估入账", "205/8/14", 4.2953],
            [2, "C002", "SellerB", "BuyerX", 200, datetime.datetime(2026, 7, 6), "6月暂估入账", None, "已执行完"],
        ],
    )

    result = import_contract_ledger(db_session, path)  # must not raise

    assert result.contracts_created == 2

    fragments = {f.row_number: f for f in db_session.query(EvidenceFragmentModel).all()}
    row3, row4 = fragments[3], fragments[4]

    assert row3.raw_data["是否付款"] == "是"
    assert row3.raw_data["进项发票入账月"] == "5月暂估入账"
    assert row3.raw_data["退税到账日期"] == "205/8/14"  # malformed string, kept as-is — not parsed as a date
    assert row3.raw_data["备注"] == 4.2953  # stray number, kept as a number, not coerced to text

    assert row4.raw_data["是否付款"] == "2026-07-06"  # date cell serialized to ISO — not to a bool
    assert row4.raw_data["进项发票入账月"] == "6月暂估入账"
    assert row4.raw_data["备注"] == "已执行完"

    # None of the dirty columns leak into canonical Contract fields.
    from bel.infrastructure.persistence.models import ContractModel

    c001 = db_session.query(ContractModel).filter_by(contract_no="C001").one()
    assert c001.counterparty == "SellerA"
    assert c001.gross_amount == 100
