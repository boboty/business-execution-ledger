"""Local development ``.env`` bootstrap (Phase 2D.1-P, post-Gate dev UX
fix).

A SOURCE-CHECKOUT DEVELOPMENT convenience only — never a general
production configuration discovery mechanism. ``BEL_DATABASE_URL``
remains BEL's ONE authoritative database runtime configuration value
(see docs/PERSISTENCE-MIGRATION-POLICY.md); ``.env`` is only ever one
development-time way to populate that single environment variable, never
a second protocol (no ``DB_HOST``/``DB_PORT``/``DB_USER``/``DB_PASSWORD``/
``DB_NAME``, no URL reconstruction).

Precedence (enforced by composing two independently-correct mechanisms,
never reimplemented here):
  - Click's own native behavior: an explicit ``--database-url`` flag
    always wins over its ``envvar=`` fallback — unaffected by this
    module.
  - ``python-dotenv``'s ``load_dotenv(..., override=False)``: populates
    ``os.environ`` only for keys not already set, so an already-exported
    process ``BEL_DATABASE_URL`` is never overwritten by a ``.env``
    value.

Composed, the effective order is: explicit CLI flag > pre-existing
process env var > source-checkout ``.env`` value > controlled
missing-config error (unchanged from before this module existed).

Safety: this module never prints, logs, or returns the URL/credentials
it loads, and never decodes or rebuilds ``BEL_DATABASE_URL`` — it only
decides *whether* to call ``load_dotenv`` and *where* to point it.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

# Both markers must be present together in the SAME candidate directory —
# not a generic "some .env exists somewhere above cwd" search. This is
# deliberately more than schema_gate.py's single-marker alembic.ini walk:
# a production wheel installed into site-packages, or a plain `pip
# install .` env, must never accidentally treat some unrelated parent
# directory (a user's home directory, a venv root, /) as BEL's own
# source checkout merely because it happens to contain a same-named
# file. Two independent, BEL-specific structural markers co-occurring is
# strong enough evidence without parsing file contents (which would be
# overengineering for a dev-convenience feature).
_REPO_ROOT_MARKERS = ("pyproject.toml", "alembic.ini")


def _find_repo_root() -> Path | None:
    """Walk up from this file looking for a directory that has ALL of
    ``_REPO_ROOT_MARKERS`` — i.e. a real BEL source checkout, never an
    installed package's site-packages location (which carries neither).
    Returns ``None`` (never raises) if no such directory is found."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if all((parent / marker).is_file() for marker in _REPO_ROOT_MARKERS):
            return parent
    return None


def load_local_dotenv(path: Path | str | None = None) -> None:
    """Populate ``os.environ`` from a source-checkout ``.env`` file, if
    one can be found — a silent no-op otherwise (missing file, or no BEL
    source checkout root at all, e.g. a packaged/production install).
    Never overrides an already-set environment variable
    (``override=False``): a real process ``BEL_DATABASE_URL`` always
    wins over anything in ``.env``.

    *path* is an explicit override for tests only — never a second
    runtime configuration protocol; production/CI code paths always use
    the default (repo-root discovery), and CI has no ``.env`` file to
    find regardless.
    """
    if path is not None:
        load_dotenv(dotenv_path=Path(path), override=False)
        return

    repo_root = _find_repo_root()
    if repo_root is None:
        return

    dotenv_path = repo_root / ".env"
    if not dotenv_path.is_file():
        return

    load_dotenv(dotenv_path=dotenv_path, override=False)
