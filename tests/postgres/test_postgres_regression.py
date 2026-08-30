"""PostgreSQL regression smoke suite (Phase 2D.1-P, Part H + concurrency).

Not a rewrite of the ~40 existing SQLite-backed test files — those cover
business logic exhaustively already and are dialect-independent by
construction (they exercise the SAME application-service functions this
file does). This suite exists to prove two things the SQLite suite
cannot: (1) the same functions genuinely work against a real PostgreSQL
schema built from the new baseline migration (dialect portability —
UUID/JSON/Numeric/Date, partial indexes, the trigger), and (2) the
concurrency invariants the Phase 2D.1-P advisory-lock work is meant to
preserve actually hold under PostgreSQL's weaker default isolation,
including against writers that bypass the application layer entirely.

Requires a real PostgreSQL database via BEL_DATABASE_URL — skipped
automatically otherwise (see tests/conftest.py's ``postgres`` marker
handling). Every test rebuilds the schema from empty via
``Base.metadata.create_all`` (structurally identical to what the
baseline migration creates — migration-driven creation itself is
verified separately by test_migration.py) so each test starts clean and
tests never interfere with each other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, text

from bel.application.contract_360 import get_contract_360
from bel.application.contract_business_ledger import ContractLedgerFilters, get_contract_business_ledger
from bel.application.contract_ledger_export import (
    export_contract_business_ledger_csv,
    export_contract_business_ledger_xlsx,
)
from bel.application.contract_item_facts import (
    execute_create_contract_item_fact,
    execute_supplement_contract_item_fact,
    get_contract_item_history,
)
from bel.application.cutover_reconciliation import build_contract_execution_snapshot
from bel.application.import_bank import import_bank_statement
from bel.application.import_close_facts import import_close_facts
from bel.application.import_contract_ledger import import_contract_ledger
from bel.application.import_invoices import import_invoices
from bel.application.matching import match_invoices, match_payments
from bel.application.period_close_workbench import get_period_close_workbench
from bel.application.procurement_sales_link import add_procurement_sales_link
from bel.application.sales_contract_facts import create_sales_contract_fact
from bel.application.sales_matching import confirm_sales_invoice_match, propose_sales_invoice_match
from bel.domain.contract import Contract
from bel.domain.evidence import EvidenceDocument, EvidenceFragment, FragmentKind
from bel.domain.invoice import Invoice, InvoiceDirection
from bel.domain.matching import ConfirmationType
from bel.domain.payment import Payment, PaymentDirection
from bel.domain.procurement_sales_link import ConfirmationType as LinkConfirmationType
from bel.infrastructure.persistence.database import DatabaseRuntime, is_database_busy, make_engine, make_session_factory
from bel.infrastructure.persistence.models import Base
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    EvidenceRepository,
    InvoiceAllocationRepository,
    InvoiceRepository,
    MatchCaseRepository,
    PaymentAllocationRepository,
    PaymentRepository,
    SalesInvoiceAllocationRepository,
)
from fixtures.synthetic import scenarios
from fixtures.synthetic.bank_pdf import build_cmb_bank_statement_pdf
from fixtures.synthetic.phase2b_close import (
    CLOSE_FACT_PACK,
    PHASE2B_CONTRACT_HEADERS,
    PHASE2B_CONTRACT_ROWS,
    PHASE2B_INVOICE_ROWS,
)
from tests.conftest import write_invoice_workbook, write_ledger_workbook
from tests.web.conftest import _confirm_contract_allocations

NOW = datetime.now(timezone.utc)
REPO_ROOT = Path(__file__).parent.parent.parent

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_runtime(postgres_url):
    """A fresh PostgreSQL schema, rebuilt from empty for this one test."""
    engine = create_engine(postgres_url, future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()

    runtime = DatabaseRuntime(postgres_url)
    Base.metadata.create_all(runtime.engine)
    yield runtime
    runtime.engine.dispose()


def _make_fragment(session):
    doc = EvidenceDocument(
        id=uuid.uuid4(), file_name="x", sha256=uuid.uuid4().hex + uuid.uuid4().hex, source_type="t", imported_at=NOW
    )
    evidence_repo = EvidenceRepository(session)
    evidence_repo.add_document(doc)
    frag = EvidenceFragment(
        id=uuid.uuid4(),
        evidence_document_id=doc.id,
        fragment_kind=FragmentKind.MANUAL_FACT,
        sheet_name=None,
        row_number=None,
        locator_json={},
        raw_data={},
        created_at=NOW,
    )
    evidence_repo.add_fragment(frag)
    session.flush()
    return frag


# ---------------------------------------------------------------------------
# Part H — functional regression against real PostgreSQL
# ---------------------------------------------------------------------------


def test_contract_ledger_invoice_bank_import_and_reads(pg_runtime, tmp_path):
    with pg_runtime.session_factory() as session:
        contracts_xlsx = tmp_path / "contracts.xlsx"
        invoices_xlsx = tmp_path / "invoices.xlsx"
        write_ledger_workbook(contracts_xlsx, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
        write_invoice_workbook(invoices_xlsx, scenarios.BUYER, PHASE2B_INVOICE_ROWS)

        ledger_result = import_contract_ledger(session, contracts_xlsx)
        assert ledger_result.contracts_created > 0
        invoice_result = import_invoices(session, invoices_xlsx, InvoiceDirection.PURCHASE)
        assert invoice_result.invoices_created > 0
        session.commit()

        pdf_path = tmp_path / "bank.pdf"
        build_cmb_bank_statement_pdf(pdf_path, scenarios.OPENING_BALANCE, scenarios.PAYMENT_TRANSACTIONS)
        bank_result = import_bank_statement(session, pdf_path, profile="cmb", source_account_id="ACC-PG")
        assert bank_result.payments_created > 0
        session.commit()

        contracts = ContractRepository(session).list_all()
        assert len(contracts) > 0
        invoices = InvoiceRepository(session).list_all()
        assert len(invoices) > 0
        payments = PaymentRepository(session).list_all()
        assert len(payments) > 0
        assert all(p.source_account_id == "ACC-PG" for p in payments)


def test_contract_item_current_revision_read_and_history(pg_runtime):
    with pg_runtime.session_factory() as session:
        frag = _make_fragment(session)
        contract = ContractRepository(session)
        from bel.domain.contract import Contract

        c = Contract(
            id=uuid.uuid4(), contract_no="C-PG-1", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=date(2026, 1, 1),
            current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        contract.add(c)
        session.flush()

        created = execute_create_contract_item_fact(
            session, contract_id=c.id, source_item_key="ITEM-PG-1", fields={"product_name": "Widget"}
        )
        session.commit()
        initial_revision_id = get_contract_item_history(session, created.item.id)[0].id
        supplemented = execute_supplement_contract_item_fact(
            session,
            contract_item_id=created.item.id,
            based_on_revision_id=initial_revision_id,
            fields={"quantity": Decimal("5")},
        )
        session.commit()

        history = get_contract_item_history(session, created.item.id)
        assert len(history) == 2
        assert supplemented.item.quantity == Decimal("5")


def test_procurement_sales_link_and_whole_fact_supersession(pg_runtime):
    with pg_runtime.session_factory() as session:
        frag = _make_fragment(session)
        from bel.domain.contract import Contract

        contract = Contract(
            id=uuid.uuid4(), contract_no="C-PG-2", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=date(2026, 1, 1),
            current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.flush()

        sc_result = create_sales_contract_fact(
            session, our_entity="Our Entity", sales_contract_no="SC-PG-1", fields={"customer": "Cust"},
            source_fragment_id=frag.id, created_at=NOW,
        )
        session.commit()

        link_result = add_procurement_sales_link(
            session, procurement_contract_id=contract.id, sales_contract_id=sc_result.sales_contract.id,
            source_fragment_id=frag.id, confirmation_type=LinkConfirmationType.AUTO_CONFIRMED, created_at=NOW,
        )
        session.commit()
        assert link_result.link is not None


def test_contract_business_ledger_contract_360_and_period_close_workbench(pg_runtime, tmp_path):
    with pg_runtime.session_factory() as session:
        contracts_xlsx = tmp_path / "contracts.xlsx"
        invoices_xlsx = tmp_path / "invoices.xlsx"
        facts_json = tmp_path / "close-facts.json"
        write_ledger_workbook(contracts_xlsx, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
        write_invoice_workbook(invoices_xlsx, scenarios.BUYER, PHASE2B_INVOICE_ROWS)
        facts_json.write_text(json.dumps(CLOSE_FACT_PACK))

        import_contract_ledger(session, contracts_xlsx)
        import_invoices(session, invoices_xlsx, InvoiceDirection.PURCHASE)
        _confirm_contract_allocations(session)
        session.commit()
        import_close_facts(session, facts_json)
        session.commit()

        ledger = get_contract_business_ledger(session, ContractLedgerFilters())
        assert len(ledger.rows) > 0

        csv_bytes = export_contract_business_ledger_csv(ledger)
        assert len(csv_bytes) > 0
        xlsx_bytes = export_contract_business_ledger_xlsx(ledger)
        assert len(xlsx_bytes) > 0

        first_contract_id = ledger.rows[0].contract.id
        contract_360 = get_contract_360(session, first_contract_id, period="2031-03")
        assert contract_360 is not None

        workbench = get_period_close_workbench(session, "2031-03")
        assert workbench is not None


def test_cutover_reconciliation_snapshot_builds_against_postgres(pg_runtime, tmp_path):
    with pg_runtime.session_factory() as session:
        contracts_xlsx = tmp_path / "contracts.xlsx"
        write_ledger_workbook(contracts_xlsx, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
        import_contract_ledger(session, contracts_xlsx)
        session.commit()

        snapshot = build_contract_execution_snapshot(session)
        assert isinstance(snapshot, dict)


# ---------------------------------------------------------------------------
# Concurrency — through the application layer (serialized_write_transaction /
# acquire_serialization_lock)
# ---------------------------------------------------------------------------


def _seed_sales_invoice(postgres_url, tmp_path):
    engine = make_engine(postgres_url)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)
        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.SALES, invoice_type=None, invoice_no=None,
            digital_invoice_no=None, external_invoice_key=f"SINV-{uuid.uuid4().hex[:8]}", issue_date=date(2026, 1, 10),
            seller="Our Entity", buyer="Customer", net_amount=Decimal("100.00"), tax_amount=Decimal("0"),
            gross_amount=Decimal("100.00"), invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(session).add(invoice)
        session.flush()
        sc = create_sales_contract_fact(
            session, our_entity="Our Entity", sales_contract_no=f"SC-{uuid.uuid4().hex[:8]}", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        session.commit()
        proposal = propose_sales_invoice_match(session, invoice_id=invoice.id, sales_contract_ids=[sc.id], created_at=NOW)
        session.commit()
        return invoice.id, sc.id, proposal.match_case.id
    engine.dispose()


def test_concurrent_sales_invoice_confirmation_exactly_one_wins(pg_runtime, tmp_path):
    """Real threads, real independent connections — proves
    acquire_serialization_lock actually serializes the capacity-check
    race under PostgreSQL, not just under SQLite's implicit whole-DB
    lock."""
    invoice_id, sc_id, match_case_id = _seed_sales_invoice(pg_runtime.database_url, tmp_path)

    results: list[object] = []
    barrier = threading.Barrier(2)

    def _attempt():
        engine = make_engine(pg_runtime.database_url)
        session_factory = make_session_factory(engine)
        barrier.wait()
        try:
            with session_factory() as session:
                result = confirm_sales_invoice_match(
                    session, match_case_id=match_case_id, allocations=[(sc_id, Decimal("100.00"))], created_at=NOW,
                )
                session.commit()
                results.append(result)
        except Exception as exc:  # noqa: BLE001 — collecting whichever exception the loser raises
            results.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if not isinstance(r, Exception)]
    # Exactly one confirmation writes fresh (created=True); a legitimate
    # idempotent replay of the SAME payload is also acceptable — never two
    # independently-created allocation sets.
    created_count = sum(1 for r in successes if getattr(r, "created", False))
    assert created_count == 1, f"expected exactly one winner, got results={results}"

    engine = make_engine(pg_runtime.database_url)
    with make_session_factory(engine)() as session:
        allocations = SalesInvoiceAllocationRepository(session).list_for_invoice(invoice_id)
        assert sum(a.allocated_gross_amount for a in allocations) == Decimal("100.00")
    engine.dispose()


# ---------------------------------------------------------------------------
# Adversarial — bypassing the application layer entirely (direct INSERTs),
# proving the trigger's OWN business-key-scoped advisory lock, independent
# of serialized_write_transaction / acquire_serialization_lock.
# ---------------------------------------------------------------------------


def test_procurement_sales_link_trigger_survives_raw_concurrent_inserts(pg_runtime):
    """Two independent psycopg connections racing a direct INSERT into
    procurement_sales_links for the SAME business key — no
    serialized_write_transaction, no ORM, no application layer at all.
    Exactly one row may survive as current; the trigger's own
    pg_advisory_xact_lock is what must catch the race."""
    with pg_runtime.session_factory() as session:
        frag = _make_fragment(session)
        from bel.domain.contract import Contract

        contract = Contract(
            id=uuid.uuid4(), contract_no="C-PG-RACE", contract_type=None, counterparty="Supplier",
            buyer="Buyer", gross_amount=Decimal("1000.00"), currency="CNY", contract_date=date(2026, 1, 1),
            current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.flush()
        sc = create_sales_contract_fact(
            session, our_entity="Our Entity", sales_contract_no="SC-PG-RACE", fields={},
            source_fragment_id=frag.id, created_at=NOW,
        ).sales_contract
        session.commit()
        contract_id, sales_contract_id, fragment_id = contract.id, sc.id, frag.id

    dsn = pg_runtime.database_url.replace("postgresql+psycopg://", "postgresql://")
    results: list[object] = []
    barrier = threading.Barrier(2)

    def _raw_insert():
        conn = psycopg.connect(dsn)
        try:
            barrier.wait()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO procurement_sales_links "
                    "(id, procurement_contract_id, sales_contract_id, source_fragment_id, confirmation_type, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, now())",
                    (str(uuid.uuid4()), str(contract_id), str(sales_contract_id), str(fragment_id), "HUMAN_CONFIRMED"),
                )
            conn.commit()
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            results.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=_raw_insert) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r == "ok"]
    assert len(successes) == 1, f"expected exactly one raw insert to survive, got results={results}"

    with pg_runtime.session_factory() as session:
        rows = session.execute(
            text(
                "SELECT count(*) FROM procurement_sales_links WHERE procurement_contract_id = :p "
                "AND sales_contract_id = :s"
            ),
            {"p": str(contract_id), "s": str(sales_contract_id)},
        ).scalar_one()
        assert rows == 1


# ---------------------------------------------------------------------------
# BLOCKER 2 — procurement M001 batch matching (match_invoices / match_payments)
# concurrency, real PostgreSQL sessions, no mocked locking.
# ---------------------------------------------------------------------------


def _seed_m001_contract(database_url: str, *, gross_amount: Decimal, counterparty: str) -> uuid.UUID:
    engine = make_engine(database_url)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)
        contract = Contract(
            id=uuid.uuid4(), contract_no=f"C-M001-{uuid.uuid4().hex[:8]}", contract_type=None,
            counterparty=counterparty, buyer="Our Entity", gross_amount=gross_amount, currency="CNY",
            contract_date=date(2026, 1, 1), current_source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        ContractRepository(session).add(contract)
        session.commit()
        contract_id = contract.id
    engine.dispose()
    return contract_id


def test_concurrent_match_invoices_against_the_same_eligible_invoice_exactly_one_wins(pg_runtime):
    """BLOCKER 2 required test A: two independent PostgreSQL sessions
    concurrently run the full match_invoices() batch pass against the
    SAME single eligible PURCHASE invoice (a unique-candidate match —
    M001's EXACT_COUNTERPARTY_AMOUNT_UNIQUE case). MatchCaseModel has no
    DB-level uniqueness on (subject_type, subject_id) by design (a
    subject may legitimately have several MatchCases across different
    Contracts) — so without acquire_serialization_lock() covering the
    whole read-check-write pass, both sessions could observe "not yet
    matched" and both write an authoritative MatchCase + InvoiceAllocation
    for the same invoice, silently doubling its allocated amount."""
    counterparty = f"Supplier-{uuid.uuid4().hex[:8]}"
    gross_amount = Decimal("500.00")
    contract_id = _seed_m001_contract(pg_runtime.database_url, gross_amount=gross_amount, counterparty=counterparty)

    engine = make_engine(pg_runtime.database_url)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)
        invoice = Invoice(
            id=uuid.uuid4(), direction=InvoiceDirection.PURCHASE, invoice_type=None, invoice_no=None,
            digital_invoice_no=None, external_invoice_key=f"PINV-{uuid.uuid4().hex[:8]}", issue_date=date(2026, 1, 10),
            seller=counterparty, buyer="Our Entity", net_amount=gross_amount, tax_amount=Decimal("0"),
            gross_amount=gross_amount, invoice_status=None, source_fragment_id=frag.id, created_at=NOW, updated_at=NOW,
        )
        InvoiceRepository(session).add(invoice)
        session.commit()
        invoice_id = invoice.id
    engine.dispose()

    results: list[object] = []
    barrier = threading.Barrier(2)

    def _attempt():
        thread_engine = make_engine(pg_runtime.database_url)
        session_factory = make_session_factory(thread_engine)
        barrier.wait()
        try:
            with session_factory() as session:
                results.append(match_invoices(session))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            thread_engine.dispose()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"no raw exception expected, got: {errors}"
    auto_confirmed_runs = sum(1 for r in results if r.auto_confirmed == 1)
    already_matched_runs = sum(1 for r in results if r.already_matched_skipped == 1)
    assert auto_confirmed_runs == 1, f"expected exactly one winner, got summaries={results}"
    assert already_matched_runs == 1, f"expected exactly one deterministic skip, got summaries={results}"

    with pg_runtime.session_factory() as session:
        match_cases = [
            mc for mc in MatchCaseRepository(session).list_all() if mc.subject_id == invoice_id
        ]
        assert len(match_cases) == 1, f"expected exactly one MatchCase, found {len(match_cases)} — residue from a lost race"
        allocations = InvoiceAllocationRepository(session).list_for_contract(contract_id)
        assert len(allocations) == 1
        assert allocations[0].allocated_gross_amount == gross_amount  # never doubled, never a capacity overflow


def test_concurrent_match_payments_against_the_same_eligible_payment_exactly_one_wins(pg_runtime):
    """BLOCKER 2 required test B: the OUT-payment twin of the invoice
    test above — see its docstring for the race being closed."""
    counterparty = f"Supplier-{uuid.uuid4().hex[:8]}"
    amount = Decimal("300.00")
    contract_id = _seed_m001_contract(pg_runtime.database_url, gross_amount=amount, counterparty=counterparty)

    engine = make_engine(pg_runtime.database_url)
    with make_session_factory(engine)() as session:
        frag = _make_fragment(session)
        payment = Payment(
            id=uuid.uuid4(), transaction_date=date(2026, 1, 15), direction=PaymentDirection.OUT, amount=amount,
            counterparty=counterparty, business_type=None, bank_reference=None, description=None,
            running_balance=None, source_fragment_id=frag.id, created_at=NOW, source_account_id=None,
        )
        PaymentRepository(session).add(payment)
        session.commit()
        payment_id = payment.id
    engine.dispose()

    results: list[object] = []
    barrier = threading.Barrier(2)

    def _attempt():
        thread_engine = make_engine(pg_runtime.database_url)
        session_factory = make_session_factory(thread_engine)
        barrier.wait()
        try:
            with session_factory() as session:
                results.append(match_payments(session))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)
        finally:
            thread_engine.dispose()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [r for r in results if isinstance(r, Exception)]
    assert not errors, f"no raw exception expected, got: {errors}"
    auto_confirmed_runs = sum(1 for r in results if r.auto_confirmed == 1)
    already_matched_runs = sum(1 for r in results if r.already_matched_skipped == 1)
    assert auto_confirmed_runs == 1, f"expected exactly one winner, got summaries={results}"
    assert already_matched_runs == 1, f"expected exactly one deterministic skip, got summaries={results}"

    with pg_runtime.session_factory() as session:
        match_cases = [mc for mc in MatchCaseRepository(session).list_all() if mc.subject_id == payment_id]
        assert len(match_cases) == 1
        allocations = PaymentAllocationRepository(session).list_for_contract(contract_id)
        assert len(allocations) == 1
        assert allocations[0].allocated_amount == amount


# ---------------------------------------------------------------------------
# BLOCKER 3 — advisory lock timeout must surface as a controlled busy error,
# never a raw traceback, through the real `bel` CLI entry point.
# ---------------------------------------------------------------------------


def _hold_write_lock_and_block(dsn: str, ready: threading.Event, release: threading.Event) -> None:
    """Acquire BEL's global write-serialization advisory lock
    (database.py's _WRITE_LOCK_KEY) on a raw connection and hold it,
    uncommitted, until told to release — simulating a concurrent writer
    that another real production command must wait behind."""
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (0x4245_4C00,))
        ready.set()
        release.wait(timeout=30)
    finally:
        conn.rollback()
        conn.close()


def _stamp_alembic_head(engine) -> None:
    """These two tests invoke the real `bel` CLI as a subprocess, which
    enforces the Part G schema-at-head gate — but `pg_runtime` builds its
    schema via Base.metadata.create_all() (structurally identical, much
    faster than a real `alembic upgrade head` for the other 9 tests in
    this module that never shell out to the CLI), so it carries no
    alembic_version row. Recording one here satisfies the gate's
    precondition for these two subprocess tests without slowing down the
    rest of the module — not a stamp-as-repair (M5): the schema really is
    the head schema, this only makes the tracking row agree with a fact
    that's already true."""
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("DELETE FROM alembic_version")
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('f5796c006707')")


def _run_bel_cli(database_url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "bel.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "BEL_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_match_run_reports_controlled_busy_error_under_lock_timeout(pg_runtime):
    """BLOCKER 3: `bel match run` invokes BOTH match_invoices() and
    match_payments(), each now acquiring the global advisory lock as
    their first action. With another session holding that lock, `match
    run` must reach the configured lock_timeout and report BEL's
    controlled busy message — never an opaque SQLAlchemy/psycopg
    traceback, never a credential leak, and no partial write."""
    _stamp_alembic_head(pg_runtime.engine)
    dsn = pg_runtime.database_url.replace("postgresql+psycopg://", "postgresql://")
    ready = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_write_lock_and_block, args=(dsn, ready, release))
    holder.start()
    assert ready.wait(timeout=10), "lock holder never confirmed it acquired the lock"
    try:
        result = _run_bel_cli(pg_runtime.database_url, "match", "run")
    finally:
        release.set()
        holder.join(timeout=10)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "database is busy; retry when the other write completes" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "OperationalError" not in result.stdout
    for stream in (result.stdout, result.stderr):
        assert pg_runtime.database_url not in stream
        parsed_user = pg_runtime.engine.url.username
        if parsed_user:
            assert parsed_user not in stream or "user" not in stream.lower()

    with pg_runtime.session_factory() as session:
        assert MatchCaseRepository(session).list_all() == []  # zero partial writes


def test_sales_match_propose_reports_controlled_busy_error_under_lock_timeout(pg_runtime):
    """BLOCKER 3, sales path: `bel sales-match invoice propose` calls
    propose_sales_invoice_match(), which now acquires the same global
    advisory lock as its first action — before even validating the
    invoice/candidate ids exist. A held lock must still produce BEL's
    controlled busy message, not a traceback, regardless of whether the
    referenced ids are real."""
    _stamp_alembic_head(pg_runtime.engine)
    dsn = pg_runtime.database_url.replace("postgresql+psycopg://", "postgresql://")
    ready = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=_hold_write_lock_and_block, args=(dsn, ready, release))
    holder.start()
    assert ready.wait(timeout=10), "lock holder never confirmed it acquired the lock"
    try:
        result = _run_bel_cli(
            pg_runtime.database_url,
            "sales-match", "invoice", "propose",
            "--invoice", str(uuid.uuid4()),
            "--sales-contract", str(uuid.uuid4()),
        )
    finally:
        release.set()
        holder.join(timeout=10)

    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "database is busy; retry when the other write completes" in result.stdout
    assert "Traceback" not in result.stdout
    assert "Traceback" not in result.stderr
    assert "not found" not in result.stdout  # must fail on the LOCK, not on invoice lookup

    with pg_runtime.session_factory() as session:
        assert MatchCaseRepository(session).list_all() == []  # zero partial writes
