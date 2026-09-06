"""Same JSON contract on a migration-created disposable PostgreSQL schema."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from bel.application.tool_operations import ToolOperations
from bel.infrastructure.persistence.database import DatabaseRuntime
from bel.infrastructure.tool_host import create_tool_contract
from bel.tools import ToolContract
from tests.integration.test_tool_contract import call, counts, exercise, seed

pytestmark = pytest.mark.postgres


@pytest.fixture
def tool_pg(postgres_url, monkeypatch):
    monkeypatch.setenv("BEL_DATABASE_URL", postgres_url)
    engine = create_engine(postgres_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP SCHEMA public CASCADE")
        connection.exec_driver_sql("CREATE SCHEMA public")
    engine.dispose()
    command.upgrade(Config("alembic.ini"), "head")
    runtime = DatabaseRuntime(postgres_url)
    yield runtime
    runtime.engine.dispose()


def test_postgres_vertical_slice(tool_pg, postgres_url):
    with tool_pg.session_factory() as session:
        exercise(session)
    contract = create_tool_contract(postgres_url)
    assert call(contract, "invoice_work")["status"] == "OK"
    assert call(contract, "match_invoices")["status"] == "FORBIDDEN"


def test_concurrent_retries_do_not_duplicate_allocations(tool_pg):
    with tool_pg.session_factory() as session:
        seed(session)
    tool = ToolContract(ToolOperations(tool_pg.session_factory), allow_matching=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: call(tool, "match_invoices"), range(2)))
    assert all(r["status"] == "OK" for r in results)
    assert sum(r["data"]["auto_confirmed"] for r in results) == 1
    assert sum(r["data"]["already_matched_skipped"] for r in results) == 2
    with tool_pg.session_factory() as session:
        assert counts(session)[:2] == (1, 2)


def test_write_failure_rolls_back(tool_pg, monkeypatch):
    from bel.application import matching
    with tool_pg.session_factory() as session:
        seed(session)
        before = counts(session)
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic failure after pending allocation")
    monkeypatch.setattr(matching.EventRepository, "add", fail)
    tool = ToolContract(ToolOperations(tool_pg.session_factory), allow_matching=True)
    assert call(tool, "match_invoices")["status"] == "OUTCOME_UNKNOWN"
    with tool_pg.session_factory() as session:
        assert counts(session) == before
