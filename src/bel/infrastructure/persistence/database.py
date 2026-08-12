from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def make_engine(db_path: str) -> Engine:
    """db_path: a filesystem path, or ':memory:' for an in-process SQLite DB."""
    url = f"sqlite:///{db_path}" if db_path != ":memory:" else "sqlite://"
    engine = create_engine(url, future=True)

    # SQLite ignores FOREIGN KEY declarations unless enabled per-connection —
    # without this, current_source_fragment_id etc. are documentation only,
    # not an enforced invariant. Must be set on every new DBAPI connection.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
