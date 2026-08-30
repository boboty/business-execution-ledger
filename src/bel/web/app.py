"""BEL minimal human workbench (Phase 2C).

Two working V1 pages — 月结工作台 and 合同360° — served by FastAPI on
top of the SAME Application Services the CLI uses. Web Routes only call
Application Services; they never build DB objects or re-implement
business rules.

Security posture (Phase 2C spec section 4):
  - dynamic HTML/JSON responses carry no-store / no-referrer / nosniff
    and a strict same-origin CSP header (no inline script, no inline
    style, no third-party CDN);
  - no CORS is enabled;
  - write operations accept same-origin JSON only;
  - there is deliberately NO file download endpoint.

Transaction posture (Phase 2C.1 Round 2):
  - one DatabaseRuntime identity is shared by every page and API;
  - reads are always normal DEFERRED transactions (never the write lock);
  - the write API shares the single command-level serialized write
    boundary (``execute_manual_item_allocation`` ->
    ``serialized_write_transaction`` -> BEGIN IMMEDIATE);
  - a SQLite busy error surfaces as a controlled 503, never a 500;
  - ``:memory:`` is rejected: the Web runtime requires a file database.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bel.infrastructure.persistence.database import DatabaseRuntime
from bel.infrastructure.persistence.schema_gate import assert_schema_at_head

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'",
}


def create_app(database_url: str | None = None, *, runtime: DatabaseRuntime | None = None) -> FastAPI:
    """Build the Phase 2C workbench app.

    Pass either ``database_url`` (the app builds its own
    ``DatabaseRuntime`` from it) or a pre-built ``runtime`` — the injected
    identity is used by BOTH the read-only pages and the write API, so
    tests can inject a file ``DatabaseRuntime`` and GET/POST see the same
    DB. In-memory SQLite runtimes are rejected: the Web runtime requires a
    file/server database with a real concurrent-write guarantee.

    Before serving, asserts the database's Alembic schema revision is at
    head (Part G) — PostgreSQL only; a SQLite runtime bypasses this gate
    by construction (test-only, unsupported for production — see
    ``schema_gate.py``).
    """
    if runtime is None:
        if database_url is None:
            raise ValueError("provide database_url or a DatabaseRuntime")
        runtime = DatabaseRuntime(database_url)
    if runtime.is_memory:
        raise ValueError(
            "the Web runtime requires a file/server database; SQLite in-memory ('sqlite://') is "
            "test-only and unsupported (it has no concurrent Web guarantee)"
        )
    assert_schema_at_head(runtime.engine)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.state.session_factory = runtime.session_factory
    app.state.database_url = runtime.database_url
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response

    from bel.web import routes

    app.include_router(routes.router)
    return app
