"""Database engines, dialect-specific runtime configuration,
``DatabaseRuntime`` identity, and the serialized write transaction
boundary (Phase 2C.1 transaction hardening; re-based onto PostgreSQL in
Phase 2D.1-P).

Frozen architecture:
  - PostgreSQL (``postgresql+psycopg://...``) is the production/concurrent
    runtime for Web and CLI. SQLite (``sqlite:///<path>`` or the in-memory
    ``sqlite://``) is a test-only convenience — it has no active Alembic
    chain and no concurrent-Web guarantee (``bel web`` rejects it).
  - The write-transaction boundary is a WRITE TRANSACTION property, never
    a global Engine property. Read operations are always normal
    (DEFERRED/READ COMMITTED) reads.
  - All manual writers that depend on serializing a read-check-then-write
    sequence (Web, CLI, future Agent) share ONE command-level serialized
    transaction boundary: ``serialized_write_transaction``. On SQLite this
    is ``BEGIN IMMEDIATE`` — SQLite's own whole-database write lock. On
    PostgreSQL, which has no equivalent implicit global writer lock under
    READ COMMITTED, it is a transaction-scoped ``pg_advisory_xact_lock``
    acquired (with a bounded ``lock_timeout``) before any read, restoring
    the same "only one write flow's read-check-write sequence proceeds at
    a time" guarantee.
  - Every SQLite connection still runs ``PRAGMA journal_mode=WAL`` and a
    ``PRAGMA busy_timeout``; neither has a PostgreSQL equivalent at the
    engine level (PostgreSQL's own MVCC means readers never block writers
    or vice versa, and ``lock_timeout`` is set per-transaction instead).
  - Any write failure rolls back and leaves no Evidence/Allocation residue
    on either dialect.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_BUSY_TIMEOUT_MS = 5000

# SQLite "database is locked" family — surfaced as a controlled 503.
_SQLITE_BUSY_FRAGMENTS = ("database is locked", "database table is locked", "database schema is locked")

# PostgreSQL SQLSTATE 55P03 (lock_not_available) — raised when
# ``SET LOCAL lock_timeout`` expires waiting on ``pg_advisory_xact_lock``
# inside ``serialized_write_transaction``. The message text is a stable
# fallback for drivers/wrappers that don't surface ``.sqlstate`` cleanly.
_POSTGRES_LOCK_TIMEOUT_SQLSTATE = "55P03"
_POSTGRES_BUSY_FRAGMENTS = ("lock timeout", "lock_not_available", "could not obtain lock")

# One reserved, well-documented global advisory-lock key. Every manual
# writer that enters ``serialized_write_transaction`` contends for this
# SAME key — that is deliberate: it reproduces SQLite's implicit
# whole-database single-writer serialization (see module docstring),
# not a per-resource lock. Chosen small and with a zero high byte so it
# is trivially within PostgreSQL's signed-bigint range for
# ``pg_advisory_xact_lock``. Unrelated to the per-business-key advisory
# lock taken inside the ``procurement_sales_links`` trigger function
# (see the PostgreSQL baseline migration) — different key space, by
# design never expected to collide, and even a collision would only
# cost extra (harmless) waiting.
_WRITE_LOCK_KEY = 0x4245_4C00  # "BEL\0" as bytes, read as a bigint


def is_database_busy(exc: BaseException) -> bool:
    """True when *exc* (or its cause chain) is a busy/lock-contention
    error — the signal that a concurrent writer held the write lock past
    the timeout. Recognizes both SQLite's busy/locked family and
    PostgreSQL's ``lock_timeout`` (SQLSTATE 55P03) from
    ``serialized_write_transaction``."""
    text_lower = str(exc).lower()
    if any(fragment in text_lower for fragment in _SQLITE_BUSY_FRAGMENTS):
        return True
    if any(fragment in text_lower for fragment in _POSTGRES_BUSY_FRAGMENTS):
        return True
    orig = getattr(exc, "orig", None)
    if orig is not None and getattr(orig, "sqlstate", None) == _POSTGRES_LOCK_TIMEOUT_SQLSTATE:
        return True
    if exc.__cause__ is not None and exc.__cause__ is not exc:
        return is_database_busy(exc.__cause__)
    return False


def make_engine(database_url: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> Engine:
    """Build an engine for *database_url*, dispatching on its dialect.

    Canonical URL forms — no other input is accepted:
      - ``postgresql+psycopg://user:password@host:5432/dbname`` (production)
      - ``sqlite:///<path>`` (SQLite file, test convenience)
      - ``sqlite://`` (SQLite in-memory, test convenience — a single
        shared ``StaticPool`` connection, sequential/test use only, no
        concurrent-Web guarantee)

    SQLite gets the existing PRAGMA connect-time configuration
    (foreign_keys, busy_timeout, WAL) unchanged. PostgreSQL gets a plain
    engine — WAL/busy_timeout have no PostgreSQL equivalent at the engine
    level; ``lock_timeout`` is set per-transaction instead, inside
    ``serialized_write_transaction``.
    """
    url = make_url(database_url)
    dialect = url.get_backend_name()

    if dialect == "sqlite":
        is_memory = not url.database or url.database == ":memory:"
        kwargs: dict = {"future": True}
        connect_args: dict = {}
        if is_memory:
            connect_args["check_same_thread"] = False
            kwargs["poolclass"] = StaticPool
        engine = create_engine(url, connect_args=connect_args, **kwargs)

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            # FK enforcement is documentation-only unless enabled per connection.
            cursor.execute("PRAGMA foreign_keys=ON")
            # Wait up to busy_timeout for a held write lock, then fail loudly.
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            # WAL: writers never block readers (and vice versa); no-op on
            # :memory:. Persistent once set on a file database.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    if dialect == "postgresql":
        return create_engine(url, future=True)

    raise ValueError(
        f"unsupported database dialect {dialect!r} from {database_url!r} — only postgresql+psycopg:// "
        "(production) and sqlite:// / sqlite:///<path> (test convenience) are accepted"
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def serialized_write_transaction(session: Session) -> Iterator[Session]:
    """The single serialized write boundary shared by every manual writer
    (Web, CLI, future Agent) whose correctness depends on a read-check-
    then-write sequence serializing against a concurrent identical one.

    On enter it acquires the write lock as the FIRST statement of the
    transaction, so a concurrent read-check-then-write flow (capacity /
    duplicate checks) serializes against this one. On success it commits;
    on ANY failure it rolls back — a failed write can never leave
    Evidence/Allocation residue.

    ``session`` must be fresh (no statement has run yet) — the lock
    acquisition must be the first statement of the transaction on both
    dialects (SQLite's ``BEGIN IMMEDIATE`` cannot start while a
    transaction is already open; PostgreSQL's ``SET LOCAL lock_timeout``
    only scopes to the transaction it starts).

    Dialect dispatch:
      - SQLite: ``BEGIN IMMEDIATE`` — SQLite's own whole-database write
        lock (unchanged from Phase 2C.1).
      - PostgreSQL: ``SET LOCAL lock_timeout`` then
        ``pg_advisory_xact_lock(_WRITE_LOCK_KEY)`` — transaction-scoped,
        released automatically on commit or rollback. A losing writer
        blocks up to ``DEFAULT_BUSY_TIMEOUT_MS`` then raises a controlled
        lock-timeout error (SQLSTATE 55P03), recognized by
        ``is_database_busy`` exactly like SQLite's busy error.
    """
    dialect = session.get_bind().dialect.name
    connection = session.connection()
    if dialect == "postgresql":
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{int(DEFAULT_BUSY_TIMEOUT_MS)}ms'")
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _WRITE_LOCK_KEY})
    else:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise


def acquire_serialization_lock(session: Session) -> None:
    """Acquire the SAME shared write-serialization lock as
    ``serialized_write_transaction``, but as a plain call rather than a
    transaction-owning context manager — for writers whose established
    calling convention does not guarantee a fresh session (callers may
    already have run reads/flushes on it, e.g. ``bel.application.matching
    .confirm_match`` and ``bel.application.sales_matching``'s propose/
    confirm functions, both called from tests — and often from each
    other's setup helpers — against one shared, already-active session).

    No-ops on SQLite: unlike PostgreSQL, SQLite's file-level locking
    already gives these callers the same protection implicitly (SQLite
    only ever has one writer at a time regardless of how many prior
    statements ran on the session), which is exactly why none of them
    needed ``BEGIN IMMEDIATE`` before — ``BEGIN IMMEDIATE`` couldn't be
    retrofitted here anyway (SQLite requires it to be a transaction's
    literal first statement, which these callers cannot promise).

    On PostgreSQL — which has no such implicit whole-database guarantee
    under READ COMMITTED — acquires the same ``_WRITE_LOCK_KEY`` advisory
    lock ``serialized_write_transaction`` uses, which works mid-transaction
    (no "first statement" requirement), closing the same class of race
    for these callers too. Callers keep their own commit/rollback
    responsibility; this does not manage the transaction."""
    if session.get_bind().dialect.name != "postgresql":
        return
    connection = session.connection()
    connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{int(DEFAULT_BUSY_TIMEOUT_MS)}ms'")
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _WRITE_LOCK_KEY})


class DatabaseRuntime:
    """One logical database identity.

    Owns a single engine and its session factory. Reads always use normal
    (DEFERRED / READ COMMITTED) transactions; writes that need it go
    through ``serialized_write_transaction``. SQLite in-memory runtimes
    (``is_memory``) are test-only and must never back the Web app.
    """

    def __init__(self, database_url: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        url = make_url(database_url)
        self.database_url = database_url
        self.dialect = url.get_backend_name()
        self.is_memory = self.dialect == "sqlite" and (not url.database or url.database == ":memory:")
        self.engine = make_engine(database_url, busy_timeout_ms=busy_timeout_ms)
        self.session_factory = make_session_factory(self.engine)
