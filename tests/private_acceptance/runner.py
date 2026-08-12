"""Private acceptance harness — re-verifies the same criteria as the
public synthetic suite against real business data, entirely outside the
repository. See docs/PRIVATE-DATA-POLICY.md.

This file is a standalone script, not a pytest test module (pytest
never collects it — the name doesn't match test_*/*_test). It contains
no real entity names, amounts, counts, or identifiers: only scenario
IDs and comparison logic. All real data and all real expected results
are read from $BEL_PRIVATE_DATA_ROOT, never from this repository.

Usage:
    BEL_PRIVATE_DATA_ROOT=/path/to/private/data python tests/private_acceptance/runner.py --all
    BEL_PRIVATE_DATA_ROOT=/path/to/private/data python tests/private_acceptance/runner.py P2A_MATCHING

Default stdout is exactly "SCENARIO_ID: PASS" or "SCENARIO_ID: FAIL" per
scenario — nothing else, never a real value. The reason_code and full
diagnostics (which do contain real values) are written under
$BEL_PRIVATE_DATA_ROOT/reports/<SCENARIO_ID>.json, never printed and
never into the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from dataclasses import asdict, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bel.application.import_bank import import_bank_statement  # noqa: E402
from bel.application.import_contract_ledger import import_contract_ledger  # noqa: E402
from bel.application.import_invoices import import_invoices  # noqa: E402
from bel.application.matching import match_invoices, match_payments  # noqa: E402
from bel.domain.invoice import InvoiceDirection  # noqa: E402
from bel.infrastructure.persistence.database import make_engine, make_session_factory  # noqa: E402
from bel.infrastructure.persistence.models import (  # noqa: E402
    Base,
    ContractModel,
    InvoiceAllocationModel,
    InvoiceModel,
    MatchCaseModel,
    PaymentAllocationModel,
    PaymentModel,
)

PERIOD_DIR_RE = re.compile(r"^\d{4}-\d{2}$")

REASON_ROOT_NOT_SET = "PRIVATE_DATA_ROOT_NOT_SET"
REASON_ROOT_INSIDE_REPO = "PRIVATE_DATA_ROOT_INSIDE_REPO"
REASON_PERIOD_NOT_FOUND = "PERIOD_DIR_NOT_FOUND"
REASON_SOURCE_FILE_MISSING = "SOURCE_FILE_MISSING"
REASON_EXPECTED_FILE_MISSING = "EXPECTED_FILE_MISSING"
REASON_RESULT_MISMATCH = "RESULT_MISMATCH"
REASON_UNEXPECTED_ERROR = "UNEXPECTED_ERROR"


class AcceptanceError(Exception):
    def __init__(self, reason_code: str, diagnostic: dict[str, Any] | None = None):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.diagnostic = diagnostic or {}


def _new_session():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)()


def _one_file(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise AcceptanceError(
            REASON_SOURCE_FILE_MISSING,
            {"directory": str(directory), "pattern": pattern, "matches_found": len(matches)},
        )
    return matches[0]


def _load_expected(period_dir: Path, filename: str) -> dict[str, Any]:
    path = period_dir / "expected" / filename
    if not path.exists():
        raise AcceptanceError(REASON_EXPECTED_FILE_MISSING, {"path": str(path)})
    return json.loads(path.read_text())


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if hasattr(value, "hex") and not isinstance(value, (bytes, bytearray)):  # UUID
        return str(value)
    return value


def _is_within(path: Path, parent: Path) -> bool:
    """Return whether *path* is *parent* or one of its descendants."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_private_root() -> Path:
    raw = os.environ.get("BEL_PRIVATE_DATA_ROOT")
    if not raw:
        raise AcceptanceError(REASON_ROOT_NOT_SET)
    try:
        root = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AcceptanceError(REASON_PERIOD_NOT_FOUND, {"root": raw}) from exc
    repo_root = _repo_root()
    if not root.is_dir() or _is_within(root, repo_root):
        # Refuse to write real-data diagnostics (reports/) or read source
        # files from inside the repository tree, even if misconfigured.
        raise AcceptanceError(REASON_ROOT_INSIDE_REPO, {"root": str(root), "repo_root": str(repo_root)})
    return root


def resolve_period_dir(root: Path, period: str | None) -> Path:
    if period:
        candidate = root / period
        if not candidate.is_dir():
            raise AcceptanceError(REASON_PERIOD_NOT_FOUND, {"root": str(root), "period": period})
        return candidate

    periods = sorted(p.name for p in root.iterdir() if p.is_dir() and PERIOD_DIR_RE.match(p.name))
    if not periods:
        raise AcceptanceError(REASON_PERIOD_NOT_FOUND, {"root": str(root)})
    return root / periods[-1]


def _assert_equal(diagnostic: dict[str, Any], label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        diagnostic.setdefault("mismatches", []).append(
            {"field": label, "actual": _to_jsonable(actual), "expected": _to_jsonable(expected)}
        )


def _finish(diagnostic: dict[str, Any]) -> None:
    if diagnostic.get("mismatches"):
        raise AcceptanceError(REASON_RESULT_MISMATCH, diagnostic)


def run_p1_import(period_dir: Path) -> dict[str, Any]:
    session = _new_session()
    ledger_path = _one_file(period_dir / "contracts", "*.xlsx")
    baseline = _load_expected(period_dir, "contract-import-baseline.json")

    result = import_contract_ledger(session, ledger_path)
    diagnostic: dict[str, Any] = {"scenario": "P1_IMPORT"}

    _assert_equal(diagnostic, "is_reimport", result.is_reimport, False)
    _assert_equal(diagnostic, "sheets_detected", len(result.sheets), baseline["sheets_detected"])
    _assert_equal(diagnostic, "primary_sheet", result.primary_sheet, baseline["primary_sheet"])
    _assert_equal(diagnostic, "columns", result.primary_sheet_columns, baseline["columns"])
    _assert_equal(diagnostic, "business_rows", result.business_rows, baseline["business_rows"])
    _assert_equal(diagnostic, "blank_trailing_rows", result.blank_trailing_rows, baseline["blank_trailing_rows"])
    _assert_equal(diagnostic, "contracts_created", result.contracts_created, baseline["contracts_created"])
    _assert_equal(diagnostic, "contract_items_created", result.contract_items_created, baseline["contract_items_created"])
    _assert_equal(diagnostic, "distinct_sellers", result.distinct_sellers, baseline["distinct_sellers"])
    _assert_equal(diagnostic, "distinct_buyers", result.distinct_buyers, baseline["distinct_buyers"])
    _assert_equal(diagnostic, "distinct_owners", result.distinct_owners, baseline["distinct_owners"])
    _assert_equal(
        diagnostic, "distinct_customs_receivers", result.distinct_customs_receivers, baseline["distinct_customs_receivers"]
    )
    _assert_equal(
        diagnostic, "missing_export_contract_no", result.missing_export_contract_no, baseline["missing_export_contract_no"]
    )
    _assert_equal(diagnostic, "business_key_conflicts", len(result.business_key_conflicts), baseline["business_key_conflicts"])
    _assert_equal(diagnostic, "gross_amount_total", result.gross_amount_total, Decimal(baseline["gross_amount_total"]))

    duplicate_groups = len({c.contract_no for c in result.business_key_conflicts})
    _assert_equal(diagnostic, "duplicate_contract_no_groups", duplicate_groups, baseline["duplicate_contract_no_groups"])

    second = import_contract_ledger(session, ledger_path)
    _assert_equal(diagnostic, "reimport.is_reimport", second.is_reimport, True)
    _assert_equal(diagnostic, "reimport.contracts_created", second.contracts_created, 0)

    _finish(diagnostic)
    return diagnostic


def run_invoice_import(period_dir: Path) -> dict[str, Any]:
    session = _new_session()
    invoices_path = _one_file(period_dir / "invoices", "*.xlsx")
    baseline = _load_expected(period_dir, "invoice-import-baseline.json")

    result = import_invoices(session, invoices_path, InvoiceDirection.PURCHASE)
    diagnostic: dict[str, Any] = {"scenario": "P2A_INVOICE_IMPORT"}

    _assert_equal(diagnostic, "is_reimport", result.is_reimport, False)
    _assert_equal(diagnostic, "invoices_created", result.invoices_created, baseline["invoices_created"])
    _assert_equal(diagnostic, "invoice_items_created", result.invoice_items_created, baseline["invoice_items_created"])
    _assert_equal(diagnostic, "net_amount_total", result.net_amount_total, Decimal(baseline["net_amount_total"]))
    _assert_equal(diagnostic, "tax_amount_total", result.tax_amount_total, Decimal(baseline["tax_amount_total"]))
    _assert_equal(diagnostic, "gross_amount_total", result.gross_amount_total, Decimal(baseline["gross_amount_total"]))
    _assert_equal(diagnostic, "net_plus_tax_eq_gross", result.net_amount_total + result.tax_amount_total, result.gross_amount_total)

    second = import_invoices(session, invoices_path, InvoiceDirection.PURCHASE)
    _assert_equal(diagnostic, "reimport.is_reimport", second.is_reimport, True)
    _assert_equal(diagnostic, "reimport.invoices_created", second.invoices_created, 0)

    _finish(diagnostic)
    return diagnostic


def run_bank_import(period_dir: Path) -> dict[str, Any]:
    session = _new_session()
    bank_path = _one_file(period_dir / "bank", "*.pdf")
    baseline = _load_expected(period_dir, "bank-import-baseline.json")

    result = import_bank_statement(session, bank_path, "cmb")
    diagnostic: dict[str, Any] = {"scenario": "P2A_PAYMENT_IMPORT"}

    _assert_equal(diagnostic, "is_reimport", result.is_reimport, False)
    _assert_equal(diagnostic, "payments_created", result.payments_created, baseline["payments_created"])
    _assert_equal(diagnostic, "opening_balance", result.opening_balance, Decimal(baseline["opening_balance"]))
    _assert_equal(diagnostic, "total_in", result.total_in, Decimal(baseline["total_in"]))
    _assert_equal(diagnostic, "total_out", result.total_out, Decimal(baseline["total_out"]))
    _assert_equal(diagnostic, "closing_balance", result.closing_balance, Decimal(baseline["closing_balance"]))
    _assert_equal(
        diagnostic,
        "reconciliation",
        result.opening_balance + result.total_in - result.total_out,
        result.closing_balance,
    )

    second = import_bank_statement(session, bank_path, "cmb")
    _assert_equal(diagnostic, "reimport.is_reimport", second.is_reimport, True)
    _assert_equal(diagnostic, "reimport.payments_created", second.payments_created, 0)

    _finish(diagnostic)
    return diagnostic


def run_matching(period_dir: Path) -> dict[str, Any]:
    session = _new_session()
    ledger_path = _one_file(period_dir / "contracts", "*.xlsx")
    invoices_path = _one_file(period_dir / "invoices", "*.xlsx")
    bank_path = _one_file(period_dir / "bank", "*.pdf")
    baseline = _load_expected(period_dir, "matching-baseline.json")

    import_contract_ledger(session, ledger_path)
    import_invoices(session, invoices_path, InvoiceDirection.PURCHASE)
    import_bank_statement(session, bank_path, "cmb")

    inv_summary = match_invoices(session)
    pay_summary = match_payments(session)
    diagnostic: dict[str, Any] = {"scenario": "P2A_MATCHING"}

    inv_baseline = baseline["invoice_matching"]
    _assert_equal(diagnostic, "invoice.eligible_total", inv_summary.eligible_total, inv_baseline["eligible_total"])
    _assert_equal(diagnostic, "invoice.auto_confirmed", inv_summary.auto_confirmed, inv_baseline["auto_confirmed"])
    _assert_equal(
        diagnostic,
        "invoice.human_confirmation_required",
        inv_summary.human_confirmation_required,
        inv_baseline["human_confirmation_required"],
    )
    _assert_equal(diagnostic, "invoice.unmatched", inv_summary.unmatched, inv_baseline["unmatched_within_eligible"])
    _assert_equal(diagnostic, "invoice.capacity_exceeded", inv_summary.capacity_exceeded, inv_baseline.get("capacity_exceeded", 0))

    pay_baseline = baseline["payment_matching"]
    _assert_equal(diagnostic, "payment.eligible_total", pay_summary.eligible_total, pay_baseline["eligible_total"])
    _assert_equal(diagnostic, "payment.auto_confirmed", pay_summary.auto_confirmed, pay_baseline["auto_confirmed"])
    _assert_equal(
        diagnostic,
        "payment.human_confirmation_required",
        pay_summary.human_confirmation_required,
        pay_baseline["human_confirmation_required"],
    )
    _assert_equal(diagnostic, "payment.unmatched", pay_summary.unmatched, pay_baseline["unmatched_within_eligible"])
    _assert_equal(diagnostic, "payment.capacity_exceeded", pay_summary.capacity_exceeded, pay_baseline.get("capacity_exceeded", 0))

    invoice_match_case_count = session.query(MatchCaseModel).filter_by(subject_type="INVOICE").count()
    payment_match_case_count = session.query(MatchCaseModel).filter_by(subject_type="PAYMENT").count()
    _assert_equal(diagnostic, "invoice.match_case_count", invoice_match_case_count, inv_baseline["eligible_total"])
    _assert_equal(diagnostic, "payment.match_case_count", payment_match_case_count, pay_baseline["eligible_total"])

    ambiguous_amounts = {Decimal(a) for a in baseline["ambiguous_amount_clusters"]}
    invoice_allocation_amounts = {a.allocated_gross_amount for a in session.query(InvoiceAllocationModel).all()}
    payment_allocation_amounts = {a.allocated_amount for a in session.query(PaymentAllocationModel).all()}
    if not ambiguous_amounts.isdisjoint(invoice_allocation_amounts):
        diagnostic.setdefault("mismatches", []).append({"field": "invoice_ambiguous_amount_leaked_into_allocation"})
    if not ambiguous_amounts.isdisjoint(payment_allocation_amounts):
        diagnostic.setdefault("mismatches", []).append({"field": "payment_ambiguous_amount_leaked_into_allocation"})

    supplier_breakdown_path = period_dir / "expected" / "matching-supplier-breakdown.json"
    if supplier_breakdown_path.exists():
        breakdown = json.loads(supplier_breakdown_path.read_text())
        contract_counterparties = {c.counterparty for c in session.query(ContractModel).all()}

        for seller, expected in breakdown["invoice_eligibility_by_seller"].items():
            if seller not in contract_counterparties:
                diagnostic.setdefault("mismatches", []).append({"field": f"seller_not_a_contract_party:{seller}"})
                continue
            invoices = (
                session.query(InvoiceModel).filter_by(direction=InvoiceDirection.PURCHASE, seller=seller).all()
            )
            _assert_equal(diagnostic, f"seller_breakdown.{seller}.count", len(invoices), expected["count"])
            _assert_equal(
                diagnostic,
                f"seller_breakdown.{seller}.net",
                sum((i.net_amount for i in invoices), Decimal("0")),
                Decimal(expected["net"]),
            )
            _assert_equal(
                diagnostic,
                f"seller_breakdown.{seller}.tax",
                sum((i.tax_amount for i in invoices), Decimal("0")),
                Decimal(expected["tax"]),
            )
            _assert_equal(
                diagnostic,
                f"seller_breakdown.{seller}.gross",
                sum((i.gross_amount for i in invoices), Decimal("0")),
                Decimal(expected["gross"]),
            )

        for counterparty, expected in breakdown["payment_eligibility_by_counterparty"].items():
            if counterparty not in contract_counterparties:
                diagnostic.setdefault("mismatches", []).append({"field": f"counterparty_not_a_contract_party:{counterparty}"})
                continue
            payments = session.query(PaymentModel).filter_by(direction="OUT", counterparty=counterparty).all()
            _assert_equal(diagnostic, f"payment_breakdown.{counterparty}.count", len(payments), expected["count"])
            _assert_equal(
                diagnostic,
                f"payment_breakdown.{counterparty}.amount",
                sum((p.amount for p in payments), Decimal("0")),
                Decimal(expected["amount"]),
            )

    _finish(diagnostic)
    return diagnostic


SCENARIOS = {
    "P1_IMPORT": run_p1_import,
    "P2A_INVOICE_IMPORT": run_invoice_import,
    "P2A_PAYMENT_IMPORT": run_bank_import,
    "P2A_MATCHING": run_matching,
}


def _write_report(root: Path, scenario_id: str, diagnostic: dict[str, Any]) -> None:
    """Write diagnostics only through a verified, external reports directory.

    This intentionally suppresses all errors: the caller must preserve the
    one-line stdout contract even when a diagnostic cannot be safely written.
    """
    try:
        if scenario_id not in SCENARIOS:
            return
        repo_root = _repo_root()
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir() or _is_within(resolved_root, repo_root):
            return

        reports_dir = resolved_root / "reports"
        reports_dir.mkdir(exist_ok=True)
        resolved_reports_dir = reports_dir.resolve(strict=True)
        # A reports symlink may point outside the repository, but it still
        # violates the promised $BEL_PRIVATE_DATA_ROOT/reports/ location.
        if (
            not resolved_reports_dir.is_dir()
            or not _is_within(resolved_reports_dir, resolved_root)
            or _is_within(resolved_reports_dir, repo_root)
        ):
            return

        filename = f"{scenario_id}.json"
        path = resolved_reports_dir / filename
        # Resolve the final component as well: an existing report may itself
        # be a symlink into the repository or elsewhere.
        resolved_path = path.resolve(strict=False)
        if not _is_within(resolved_path, resolved_reports_dir) or _is_within(resolved_path, repo_root):
            return

        # Open the verified directory itself without following a symlink, then
        # create the report relative to that descriptor.  This closes the
        # check/write gap for a reports-directory symlink swap as well as for
        # the final report-file component.
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        reports_fd = os.open(resolved_reports_dir, directory_flags)

        # O_NOFOLLOW makes an attempted final-component symlink swap fail
        # rather than following it.  The verified directory descriptor keeps
        # all diagnostics under the external private root.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, 0o600, dir_fd=reports_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as report:
                json.dump(_to_jsonable(diagnostic), report, indent=2, ensure_ascii=False, default=str)
        finally:
            os.close(reports_fd)
    except Exception:  # noqa: BLE001 — reporting must never violate stdout contract
        return


def run_scenario(root: Path, period: str | None, scenario_id: str) -> bool:
    try:
        period_dir = resolve_period_dir(root, period)
        diagnostic = SCENARIOS[scenario_id](period_dir)
        _write_report(root, scenario_id, diagnostic)
        print(f"{scenario_id}: PASS")
        return True
    except AcceptanceError as exc:
        _write_report(root, scenario_id, {"scenario": scenario_id, "reason_code": exc.reason_code, **exc.diagnostic})
        print(f"{scenario_id}: FAIL")
        return False
    except Exception:  # noqa: BLE001 — any unexpected error must still redact stdout
        _write_report(
            root,
            scenario_id,
            {"scenario": scenario_id, "reason_code": REASON_UNEXPECTED_ERROR, "traceback": traceback.format_exc()},
        )
        print(f"{scenario_id}: FAIL")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenarios", nargs="*", choices=list(SCENARIOS) + [], help="Scenario IDs to run")
    parser.add_argument("--all", action="store_true", help="Run every scenario")
    parser.add_argument("--period", default=None, help="Period folder under the private root (default: latest)")
    args = parser.parse_args(argv)

    if not args.all and not args.scenarios:
        parser.error("specify one or more scenario IDs, or --all")

    try:
        root = resolve_private_root()
    except Exception:  # noqa: BLE001 — an unusable root has no safe report destination
        # No resolvable private root at all — nowhere safe to write a
        # report either, so stdout is still exactly PASS/FAIL, nothing more.
        for scenario_id in (list(SCENARIOS) if args.all else args.scenarios):
            print(f"{scenario_id}: FAIL")
        return 1

    scenario_ids = list(SCENARIOS) if args.all else args.scenarios
    all_passed = True
    for scenario_id in scenario_ids:
        passed = run_scenario(root, args.period, scenario_id)
        all_passed = all_passed and passed

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
