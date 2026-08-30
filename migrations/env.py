import os
from logging.config import fileConfig

from sqlalchemy import create_engine
from sqlalchemy import pool

from alembic import context

from bel.infrastructure.env_bootstrap import load_local_dotenv
from bel.infrastructure.persistence.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Development convenience only (see env_bootstrap.py) — populates
# BEL_DATABASE_URL (and nothing else) from a source-checkout .env file
# BEFORE it's read below, without ever overriding an already-exported
# process environment variable. A no-op outside a real BEL source
# checkout, so production/CI behavior (which always injects
# BEL_DATABASE_URL directly) is unaffected.
load_local_dotenv()

# BEL_DATABASE_URL is BEL's ONE runtime configuration contract (see
# docs/PERSISTENCE-MIGRATION-POLICY.md) — read directly from the
# environment into a plain local variable, and NEVER round-tripped
# through alembic.ini's ConfigParser-backed Config object below.
# `Config.set_main_option`/`get_section` apply ConfigParser's
# BasicInterpolation to stored values, which treats a bare `%` as the
# start of a `%(name)s` reference — a URL-encoded password containing
# `%40`, `%25`, etc. (entirely valid, common for special characters)
# raises `ValueError: invalid interpolation syntax` the moment it's
# stored, before any connection is even attempted. Reading the URL
# straight from `os.environ` and handing it directly to
# `create_engine()`/`context.configure(url=...)` sidesteps that parser
# entirely — no decoding, no rebuilding, no parallel credential parser;
# the string SQLAlchemy receives is byte-for-byte what BEL_DATABASE_URL
# contains.
DATABASE_URL = os.environ.get("BEL_DATABASE_URL")

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


class MissingDatabaseUrlError(RuntimeError):
    """BEL_DATABASE_URL is BEL's ONE runtime configuration contract (see
    docs/PERSISTENCE-MIGRATION-POLICY.md) — every Alembic operation that
    needs to know a database (its dialect for offline SQL rendering, or
    a live connection for online execution) requires it explicitly.
    alembic.ini's own ``sqlalchemy.url`` is a non-runtime placeholder
    only (see the ini file) and is never consulted as a fallback here:
    an accidentally-missing environment variable must fail loudly, never
    silently connect to and migrate whatever placeholder/local database
    alembic.ini happens to name."""


def _require_database_url() -> str:
    if not DATABASE_URL:
        raise MissingDatabaseUrlError("BEL_DATABASE_URL is required for Alembic database operations")
    return DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=_require_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = create_engine(_require_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
