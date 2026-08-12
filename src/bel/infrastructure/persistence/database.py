"""Database engines, WAL/busy configuration, DatabaseRuntime identity, and
the serialized write transaction boundary (Phase 2C.1 transaction
hardening).

Frozen architecture (Phase 2C.1 Round 2):
  - File SQLite is the production/concurrent Web runtime; ``:memory:`` is
    test-only and has NO concurrent Web guarantee (``bel web --db :memory:``
    is rejected).
  - BEGIN IMMEDIATE is a WRITE TRANSACTION property, never a global Engine
    property. Read operations are always normal DEFERRED reads.
  - All manual InvoiceItemAllocation writers (Web, CLI, future Agent) share
    ONE command-level serialized transaction boundary:
    ``serialized_write_transaction`` acquires the SQLite write lock before
    any read, and owns commit/rollback.
  - Every connection runs ``PRAGMA journal_mode=WAL`` (writers never block
    readers and vice versa) and a ``PRAGMA busy_timeout`` so a second
    writer fails with a controlled busy error after the timeout.
  - Any write failure rolls back and leaves no Evidence/Allocation residue.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

DEFAULT_BUSY_TIMEOUT_MS = 5000

# SQLite "database is locked" family — surfaced as a controlled 503.
_BUSY_FRAGMENTS = ("database is locked", "database table is locked", "database schema is locked")


def is_database_busy(exc: BaseException) -> bool:
    """True when *exc* (or its cause chain) is a SQLite busy/lock error —
    the signal that a concurrent writer held the write lock past the
    busy timeout."""
    text = str(exc).lower()
    if any(fragment in text for fragment in _BUSY_FRAGMENTS):
        return True
    if exc.__cause__ is not None and exc.__cause__ is not exc:
        return is_database_busy(exc.__cause__)
    return False


def make_engine(db_path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> Engine:
    """Build a DEFERRED SQLite engine. Transactions are plain DEFERRED
    reads/writes unless a write explicitly enters
    ``serialized_write_transaction``. ``:memory:`` uses a single shared
    StaticPool connection (sequential/test use only — no concurrent Web
    guarantee)."""
    url = f"sqlite:///{db_path}" if db_path != ":memory:" else "sqlite://"
    kwargs: dict = {"future": True}
    connect_args: dict = {}
    if db_path == ":memory:":
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


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def serialized_write_transaction(session: Session) -> Iterator[Session]:
    """The single serialized write boundary shared by every manual
    InvoiceItemAllocation writer (Web, CLI, future Agent).

    On enter it executes ``BEGIN IMMEDIATE`` as the FIRST statement of the
    transaction, so the SQLite write lock is acquired before any read and
    concurrent read-check-then-write flows (capacity / duplicate checks)
    serialize. On success it commits; on ANY failure it rolls back — a
    failed write can never leave Evidence/Allocation residue.

    ``session`` must be fresh (no statement has run yet); ``BEGIN
    IMMEDIATE`` cannot start while a transaction is already open.
    """
    connection = session.connection()
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise


class DatabaseRuntime:
    """One logical database identity.

    Owns a single DEFERRED engine and its session factory. Reads always use
    normal DEFERRED transactions; writes go through
    ``serialized_write_transaction`` (BEGIN IMMEDIATE per transaction).
    ``:memory:`` runtimes are test-only and must never back the Web app.
    """

    def __init__(self, db_path: str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> None:
        self.db_path = db_path
        self.is_memory = db_path == ":memory:"
        self.engine = make_engine(db_path, busy_timeout_ms=busy_timeout_ms)
        self.session_factory = make_session_factory(self.engine)
