"""Trusted composition root; never expose database configuration as tool input."""
from sqlalchemy.engine import make_url

from bel.application.tool_operations import ToolOperations
from bel.infrastructure.persistence.database import DatabaseRuntime
from bel.infrastructure.persistence.schema_gate import assert_schema_at_head
from bel.tools import ToolContract


def create_tool_contract(database_url: str, *, allow_matching: bool = False) -> ToolContract:
    """Host opt-in authorizes the entire procurement invoice matching batch.

    Does not load .env, initialize schema, start a server, or create an Agent.
    Host owns process lifetime; the runtime's pool closes on process exit.
    """
    if make_url(database_url).drivername != "postgresql+psycopg":
        raise ValueError("Tool runtime requires postgresql+psycopg")
    runtime = DatabaseRuntime(database_url)
    try:
        assert_schema_at_head(runtime.engine)
    except Exception:
        runtime.engine.dispose()
        raise
    return ToolContract(ToolOperations(runtime.session_factory), allow_matching=allow_matching)
