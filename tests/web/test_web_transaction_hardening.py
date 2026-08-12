"""Phase 2C.1 — SQLite transaction hardening regression suite.

Proactively covers the concurrency/transaction attacks an adversarial
gate would otherwise find one round at a time, per the frozen
architecture (Phase 2C.1 Round 2):

  - File SQLite is the concurrent Web runtime; concurrency acceptance uses
    temporary FILE databases only (:memory: has no concurrent Web
    guarantee).
  - Reads are always normal DEFERRED transactions; BEGIN IMMEDIATE is a
    WRITE TRANSACTION property held by the shared
    ``execute_manual_item_allocation`` -> ``serialized_write_transaction``
    boundary (Web and CLI).
  - Any write failure rolls back and leaves no Evidence/Allocation
    residue; a file-database busy condition is a controlled 503.

Attack coverage:
  A. long reader + POST        -> 201 or controlled 503, never 500
  B. two identical POSTs       -> at most one allocation, no 500, no orphan Evidence
  C. two different payloads    -> cumulative allocation <= capacity
  D. GET storm + POST          -> GETs readable, POST succeeds/controlled busy, no leaked lock
  E. POST under held writer    -> controlled 503 after busy_timeout, rollback, pool healthy
  F. injected failed COMMIT    -> rollback, no partial Evidence/Allocation, next write succeeds
  H. injected file runtime     -> GET and POST see the same DB
  I. fact-pack import + web allocation contention -> one waits/fails cleanly, no partial state
  J. concurrent read/write repetitions -> no busy residue, invariants preserved

All scenarios run against the independently-synthetic Phase 2B fixture.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from bel.application.allocate_invoice_item import execute_manual_item_allocation
from bel.application.import_close_facts import CloseFactPackError, import_close_facts
from bel.infrastructure.persistence.database import DatabaseRuntime, is_database_busy
from bel.infrastructure.persistence.models import (
    CostRecognitionFactModel,
    EvidenceDocumentModel,
    EvidenceFragmentModel,
    InvoiceItemAllocationModel,
    InvoiceItemModel,
)
from bel.infrastructure.persistence.repositories import (
    ContractRepository,
    InvoiceItemAllocationRepository,
    InvoiceItemRepository,
    InvoiceRepository,
)
from bel.web.app import create_app

WEB_PERIOD = "2031-03"
ROUNDS = 20  # unit/integration CI round count
NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Shared fixtures / helpers
# --------------------------------------------------------------------------


def seed_runtime(runtime: DatabaseRuntime, tmp_path: Path) -> Path:
    """Seed a file runtime with the synthetic Phase 2B fixture. Returns the
    Close Fact Pack path."""
    from fixtures.synthetic import scenarios
    from fixtures.synthetic.phase2b_close import (
        CLOSE_FACT_PACK,
        PHASE2B_CONTRACT_HEADERS,
        PHASE2B_CONTRACT_ROWS,
        PHASE2B_INVOICE_ROWS,
    )
    from bel.application.import_contract_ledger import import_contract_ledger
    from bel.application.import_invoices import import_invoices
    from bel.domain.invoice import InvoiceDirection
    from bel.infrastructure.persistence.models import Base
    from tests.conftest import write_invoice_workbook, write_ledger_workbook
    from tests.web.conftest import _confirm_contract_allocations

    contracts = tmp_path / "c.xlsx"
    invoices = tmp_path / "i.xlsx"
    facts = tmp_path / "f.json"
    write_ledger_workbook(contracts, PHASE2B_CONTRACT_HEADERS, PHASE2B_CONTRACT_ROWS)
    write_invoice_workbook(invoices, scenarios.BUYER, PHASE2B_INVOICE_ROWS)
    facts.write_text(json.dumps(CLOSE_FACT_PACK), encoding="utf-8")

    Base.metadata.create_all(runtime.engine)
    with runtime.session_factory() as session:
        import_contract_ledger(session, contracts)
        import_invoices(session, invoices, InvoiceDirection.PURCHASE)
        _confirm_contract_allocations(session)
        session.commit()
        import_close_facts(session, facts)
    return facts


@pytest.fixture
def file_runtime(tmp_path) -> DatabaseRuntime:
    return DatabaseRuntime(str(tmp_path / f"rt-{uuid.uuid4().hex[:8]}.db"), busy_timeout_ms=250)


@pytest.fixture
def seeded_file_ctx(file_runtime, tmp_path):
    seed_runtime(file_runtime, tmp_path)
    app = create_app(runtime=file_runtime)
    return TestClient(app), file_runtime, app


def _contract_id(runtime: DatabaseRuntime, contract_no: str) -> str:
    with runtime.session_factory() as session:
        return str(next(c for c in ContractRepository(session).list_all() if c.contract_no == contract_no).id)


def _invoice_item_id(runtime: DatabaseRuntime, external_key: str) -> uuid.UUID:
    with runtime.session_factory() as session:
        invoice = InvoiceRepository(session).find_by_external_key(external_key)
        item = InvoiceItemRepository(session).list_for_invoice(invoice.id)[0]
        return item.id


def _payload(contract_id: str, qty: str, net: str) -> dict:
    return {
        "invoice_external_key": "DIGITAL-CLOSE-006",
        "line_no": 1,
        "contract_id": contract_id,
        "source_item_key": "ITEM-A",
        "quantity": qty,
        "net_amount": net,
    }


def _counts(runtime: DatabaseRuntime) -> dict[str, int]:
    with runtime.session_factory() as session:
        return {
            "allocation": session.query(InvoiceItemAllocationModel).count(),
            "document": session.query(EvidenceDocumentModel).count(),
            "fragment": session.query(EvidenceFragmentModel).count(),
        }


def _line_quantity(runtime: DatabaseRuntime, invoice_item_id: uuid.UUID) -> Decimal:
    with runtime.session_factory() as session:
        return session.get(InvoiceItemModel, invoice_item_id).quantity


def _line_total(runtime: DatabaseRuntime, invoice_item_id: uuid.UUID) -> Decimal:
    with runtime.session_factory() as session:
        return session.execute(
            select(func.coalesce(func.sum(InvoiceItemAllocationModel.allocated_quantity), 0)).where(
                InvoiceItemAllocationModel.invoice_item_id == invoice_item_id
            )
        ).scalar()


def _hold_write_lock(runtime: DatabaseRuntime):
    """Open a session and acquire the SQLite write lock (BEGIN IMMEDIATE)
    WITHOUT committing — the same primitive ``serialized_write_transaction``
    uses, held for the duration of the test. Caller must rollback/close."""
    session = runtime.session_factory()
    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    return session


# --------------------------------------------------------------------------
# Attack drivers (shared by the committed CI suite; stress runs locally)
# --------------------------------------------------------------------------


def run_a_long_reader_plus_post(app, runtime, rounds: int) -> None:
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    for i in range(rounds):
        held = runtime.session_factory()
        held.execute(select(func.count()).select_from(InvoiceItemAllocationModel))
        response = client.post("/api/invoice-item-allocations", json=_payload(contract_id, "1", f"{30 + i}.00"))
        assert response.status_code in (201, 503), f"never 500, got {response.status_code}"
        held.execute(select(func.count()).select_from(InvoiceItemAllocationModel))  # reader still usable
        held.close()
    assert client.get(f"/period-close?period={WEB_PERIOD}").status_code == 200


def run_b_two_identical_posts(app, runtime, rounds: int) -> None:
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    for i in range(rounds):
        payload = _payload(contract_id, "1", f"{100 + i}.00")
        before = _counts(runtime)
        barrier = threading.Barrier(2)

        def _post(idx):
            barrier.wait()
            return TestClient(app).post("/api/invoice-item-allocations", json=payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(_post, (0, 1)))
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 400], f"round {i}: {statuses}"
        loser = next(r for r in responses if r.status_code == 400)
        assert "duplicate" in loser.json()["detail"].lower()
        after = _counts(runtime)
        assert after["allocation"] == before["allocation"] + 1, "at most one allocation"
        assert after["document"] == before["document"] + 1, "no orphan EvidenceDocument"
        assert after["fragment"] == before["fragment"] + 1, "no orphan EvidenceFragment"
    assert client.get(f"/period-close?period={WEB_PERIOD}").status_code == 200


def run_c_two_different_payloads_capacity(make_app: Callable[[], object], rounds: int) -> None:
    for _round in range(rounds):
        app = make_app()
        runtime = app.state.runtime
        contract_id = _contract_id(runtime, "PO-CLOSE-006")
        invoice_item_id = _invoice_item_id(runtime, "DIGITAL-CLOSE-006")
        line_quantity = _line_quantity(runtime, invoice_item_id)
        payloads = [_payload(contract_id, "30", "500.00"), _payload(contract_id, "30", "501.00")]
        barrier = threading.Barrier(2)

        def _post(args):
            idx, payload = args
            barrier.wait()
            return TestClient(app).post("/api/invoice-item-allocations", json=payload)

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(_post, [(0, payloads[0]), (1, payloads[1])]))
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 400], f"round {_round}: {statuses}"
        loser = next(r for r in responses if r.status_code == 400)
        assert "capacity" in loser.json()["detail"].lower()
        total = _line_total(runtime, invoice_item_id)
        assert total == Decimal("30"), f"round {_round}: cumulative must be 30, got {total}"
        assert total <= line_quantity, "cumulative allocated quantity must never exceed the line"


def run_d_get_storm_plus_post(app, runtime, rounds: int) -> None:
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    for i in range(rounds):
        barrier = threading.Barrier(3)

        def _get():
            barrier.wait()
            return TestClient(app).get(f"/period-close?period={WEB_PERIOD}").status_code

        def _post():
            barrier.wait()
            return TestClient(app).post("/api/invoice-item-allocations", json=_payload(contract_id, "1", f"{200 + i}.00")).status_code

        with ThreadPoolExecutor(max_workers=3) as executor:
            g1 = executor.submit(_get)
            g2 = executor.submit(_get)
            p = executor.submit(_post)
            assert g1.result() == 200, "GET must remain readable"
            assert g2.result() == 200, "GET must remain readable"
            assert p.result() in (201, 503), "POST succeeds or controlled busy"
    assert client.get(f"/period-close?period={WEB_PERIOD}").status_code == 200
    assert client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}").status_code == 200


def run_e_post_under_held_writer(app, runtime, rounds: int) -> None:
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    for i in range(rounds):
        holder = _hold_write_lock(runtime)
        response = client.post("/api/invoice-item-allocations", json=_payload(contract_id, "1", f"{300 + i}.00"))
        assert response.status_code == 503, f"controlled busy expected, got {response.status_code}"
        holder.rollback()
        holder.close()
        ok = client.post("/api/invoice-item-allocations", json=_payload(contract_id, "1", f"{400 + i}.00"))
        assert ok.status_code == 201, f"next write must succeed, got {ok.status_code}"
    assert client.get(f"/period-close?period={WEB_PERIOD}").status_code == 200


def run_f_injected_failed_commit(app, runtime) -> None:
    """An OperationalError is injected at the REAL commit (the shared
    boundary's ``session.commit``), AFTER evidence/allocation were
    flushed. The boundary must roll back, leaving zero residue, and the
    next write must succeed."""
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    before = _counts(runtime)

    with runtime.session_factory() as session:
        original_commit = session.commit

        def _failing_commit():
            raise OperationalError("statement", {}, Exception("database is locked"), "COMMIT")

        session.commit = _failing_commit
        try:
            execute_manual_item_allocation(
                session,
                invoice_external_key="DIGITAL-CLOSE-006",
                line_no=1,
                contract_id=uuid.UUID(contract_id),
                source_item_key="ITEM-A",
                quantity=Decimal("1"),
                net_amount=Decimal("900.00"),
            )
            raise AssertionError("expected OperationalError at commit")
        except OperationalError as exc:
            assert is_database_busy(exc)
        finally:
            session.commit = original_commit
            session.rollback()

    assert _counts(runtime) == before, "failed commit must leave zero partial rows"
    assert client.post("/api/invoice-item-allocations", json=_payload(contract_id, "1", "910.00")).status_code == 201


def run_h_injected_file_runtime_same_db(app, runtime) -> None:
    client = TestClient(app)
    assert "PO-CLOSE-001" in client.get(f"/period-close?period={WEB_PERIOD}").text, "GET must read the injected file DB"
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    response = client.post("/api/invoice-item-allocations", json=_payload(contract_id, "1", "610.00"))
    assert response.status_code == 201
    assert "已关联" in client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}").text, "GET must see the POST's write"
    with runtime.session_factory() as session:
        assert InvoiceItemAllocationRepository(session).count() == 4  # 3 from fact pack + 1


def run_i_fact_pack_import_contends(app, runtime, tmp_path: Path) -> None:
    client = TestClient(app)
    pack_path = tmp_path / "contend.json"
    pack_path.write_text(
        json.dumps(
            {
                "version": 1,
                "contract_items": [],
                "cost_recognition_facts": [
                    {
                        "contract_selector": {"contract_no": "PO-CLOSE-002", "counterparty": "SupplierCloseBeta"},
                        "recognition_date": "2031-02-28",
                        "basis": "MANUAL_CONFIRMED",
                    }
                ],
                "accrual_basis_facts": [],
                "historical_accrual_facts": [],
                "invoice_item_allocations": [],
                "accrual_reversals": [],
            }
        ),
        encoding="utf-8",
    )

    def _cost_fact_count() -> int:
        with runtime.session_factory() as session:
            return session.query(CostRecognitionFactModel).count()

    holder = _hold_write_lock(runtime)
    before = _cost_fact_count()
    with runtime.session_factory() as session:
        with pytest.raises(CloseFactPackError) as excinfo:
            import_close_facts(session, pack_path)
    assert "busy" in str(excinfo.value).lower(), "contention must fail cleanly"
    assert _cost_fact_count() == before, "no partial business state"
    holder.rollback()
    holder.close()

    with runtime.session_factory() as session:
        result = import_close_facts(session, pack_path)
    assert not result.is_reimport
    assert _cost_fact_count() == before + 1, "retry after the writer releases succeeds"


def run_j_concurrent_read_write(app, runtime, rounds: int) -> None:
    client = TestClient(app)
    contract_id = _contract_id(runtime, "PO-CLOSE-006")
    invoice_item_id = _invoice_item_id(runtime, "DIGITAL-CLOSE-006")
    line_quantity = _line_quantity(runtime, invoice_item_id)
    for i in range(rounds):
        barrier = threading.Barrier(3)

        def _get():
            barrier.wait()
            return TestClient(app).get(f"/period-close?period={WEB_PERIOD}").status_code

        def _post():
            barrier.wait()
            return TestClient(app).post("/api/invoice-item-allocations", json=_payload(contract_id, "1", f"{700 + i}.00")).status_code

        with ThreadPoolExecutor(max_workers=3) as executor:
            g1 = executor.submit(_get)
            g2 = executor.submit(_get)
            p = executor.submit(_post)
            assert g1.result() == 200
            assert g2.result() == 200
            assert p.result() in (201, 400, 503), "POST must be controlled"
    assert client.get(f"/period-close?period={WEB_PERIOD}").status_code == 200
    assert client.get(f"/contracts/{contract_id}?period={WEB_PERIOD}").status_code == 200
    total = _line_total(runtime, invoice_item_id)
    assert total <= line_quantity, f"capacity invariant violated: {total} > {line_quantity}"
    with runtime.session_factory() as session:
        allocations = InvoiceItemAllocationRepository(session).list_all()
        keys = [(a.invoice_item_id, a.contract_item_id, a.allocated_quantity, a.allocated_net_amount) for a in allocations]
        assert len(keys) == len(set(keys)), "no duplicate allocations"


# --------------------------------------------------------------------------
# Committed CI tests (20 rounds, temporary FILE SQLite)
# --------------------------------------------------------------------------


def test_a_long_reader_plus_post(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_a_long_reader_plus_post(app, runtime, ROUNDS)


def test_b_two_identical_posts(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_b_two_identical_posts(app, runtime, ROUNDS)


def test_c_two_different_payloads_capacity(web_app):
    run_c_two_different_payloads_capacity(lambda: web_app(with_payment=False), ROUNDS)


def test_d_get_storm_plus_post(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_d_get_storm_plus_post(app, runtime, ROUNDS)


def test_e_post_under_held_writer(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_e_post_under_held_writer(app, runtime, ROUNDS)


def test_f_injected_failed_commit_rolls_back(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_f_injected_failed_commit(app, runtime)


def test_h_injected_file_runtime_same_db(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_h_injected_file_runtime_same_db(app, runtime)


def test_i_fact_pack_import_contends_with_web_writer(seeded_file_ctx, tmp_path):
    _client, runtime, app = seeded_file_ctx
    run_i_fact_pack_import_contends(app, runtime, tmp_path)


def test_j_concurrent_read_write_repetitions(seeded_file_ctx):
    _client, runtime, app = seeded_file_ctx
    run_j_concurrent_read_write(app, runtime, ROUNDS)
