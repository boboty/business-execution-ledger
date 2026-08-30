# Persistence & Migration Policy

Frozen by Phase 2D.1-P (PostgreSQL Runtime Baseline & Migration
Discipline), inserted between Phase 2D.1 and Phase 2D.2. This document,
`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, and `migrations/README`
carry the same hard rules — kept in sync deliberately.

## Why this exists

BEL's runtime persistence was SQLite-only through Phase 2D.1. Before BEL
begins carrying non-disposable business data, this phase moved the
production/runtime contract to PostgreSQL while preserving every
persistence invariant closed in prior Phase 2D.1 rounds. SQLite remains
only as an explicit test-only convenience — it has no active Alembic
chain and no concurrent-Web guarantee.

## Two historical markers — never conflate them

**LEGACY MIGRATION FREEZE ANCHOR** — commit
`b94f572528e25a620bf1a78bd2e26d12547b0212`. Every file under
`migrations/versions/*.py` exactly as it exists at this commit is frozen
byte-for-byte forever. This is the pre-2D.1-P SQLite-era migration
chain — it is no longer wired into any active Alembic tooling (see
"The rebaseline" below) and is kept in the repository purely as inert
historical reference.

**POSTGRESQL MIGRATION EPOCH COMMIT** — `86e572b246641762c257084bbc08764b874a13db`.
The commit that first adds the verified, tested PostgreSQL baseline
(`migrations/postgresql_versions/f5796c006707_postgresql_baseline.py`).
Every file under `migrations/postgresql_versions/*.py` is immutable from
its own first commit forward — the baseline at this commit, and every
later revision at whichever commit adds it.

Pre-2D.1-P history may contain migrations that were edited after their
own earlier commits — that predates this policy and is not a violation;
it is one of the reasons this policy exists now. M1 (below) is enforced
mechanically from each chain's own anchor forward, never retroactively.

## The rebaseline — a one-time, non-repeatable exception

Investigation during Phase 2D.1-P found that
`migrations/versions/f1a2b3c4d5e6_procurement_sales_link.py` executes a
raw SQLite-only trigger statement
(`CREATE TRIGGER ... WHEN EXISTS(...) BEGIN ... RAISE(ABORT, ...) END`).
PostgreSQL has no equivalent syntax — triggers require a separate
`FUNCTION`, with no inline `BEGIN...END` SQL body. Confirmed empirically
against a real PostgreSQL 17 instance:

```
ERROR:  syntax error at or near "EXISTS"
LINE 4: WHEN EXISTS (
             ^
```

This is a hard failure inside the frozen file's own execution — no
forward-only migration can repair it, because the failure happens before
any later migration would even run. Per the CRITICAL MIGRATION RULE this
situation requires an explicit human decision rather than a unilateral
fix, editing the frozen file, or silently squashing history.

**Decision:** `migrations/versions/` stays byte-for-byte untouched,
forever. A brand-new PostgreSQL-only chain starts fresh in
`migrations/postgresql_versions/`, with a single baseline revision
(`down_revision = None`) that re-expresses the exact Phase 2D.1 schema
and every storage invariant using PostgreSQL-native constructs — not a
business-schema redesign. `alembic.ini`'s `version_locations` points
only at the new chain; the old chain is never discovered by Alembic
tooling again.

This was viable specifically because no authoritative business data
existed yet and `bel.db` was always disposable (see "Rebuild path"
below) — **this exception must not recur** once the PostgreSQL
Migration Epoch is established. Any future SQLite-vs-PostgreSQL
incompatibility in a *new* migration is an authoring bug to fix before
that migration's first commit, not grounds for another rebaseline.

Two further portability findings were fixed by authoring the new
baseline correctly from the start (no further exception needed):

- 4 of the old migrations create partial unique indexes via
  `sqlite_where=` with no `postgresql_where=` counterpart — SQLAlchemy
  silently drops an unrecognized dialect kwarg, so replaying them as-is
  on Postgres would have silently produced **full** unique indexes,
  breaking the one-current/one-initial revision invariant. The new
  baseline's indexes (and the corresponding `models.py` `Index()`
  definitions) carry both `sqlite_where=` and `postgresql_where=`.
- 4 repository call sites depend on deferred FK checking for a
  retire-then-insert write pattern (`PRAGMA defer_foreign_keys = ON` on
  SQLite). PostgreSQL needs the FK declared `DEFERRABLE INITIALLY
  DEFERRED` at the schema level — added to the 4 affected
  `superseded_by_revision_id` columns in `models.py` (not only in the
  migration), so `alembic check` reports clean because the live schema
  and the model metadata genuinely agree.

A third, independent finding — not a schema issue — surfaced while
verifying the new baseline: SQLAlchemy's PostgreSQL/psycopg dialect does
not populate a sane `rowcount` for `INSERT ... FROM SELECT` statements
(confirmed empirically: it reports `-1` even when a row was genuinely
inserted). Every atomic conditional-insert primitive in
`repositories.py` that used `result.rowcount == 1` to detect a lost race
now uses `.returning(<pk column>)` and checks whether a row came back —
portable across both SQLite and PostgreSQL, and a real correctness fix
independent of the Postgres migration itself (it would have silently
made every sales-match proposal/confirmation and procurement-sales-link
write believe it always lost its race under PostgreSQL).

## M1 — Committed migrations are immutable

Once a file under `migrations/versions/` or `migrations/postgresql_versions/`
appears in a Git commit:

- never modify it
- never delete it
- never rename it
- never reuse its revision ID

Before its FIRST commit, editing is allowed. After that commit,
correction requires a NEW migration.

## M2 — Forward-only repair

Wrong migration `A` is repaired as `A -> B_fix`. Never rewrite `A`.

## M3 — Alembic is the schema authority

Runtime code must not create or mutate production schema with
`Base.metadata.create_all()`, ad-hoc `ALTER TABLE`, startup
auto-patching, or handwritten recovery logic. Schema change = model
change + new Alembic revision + tests. (`Base.metadata.create_all()`
remains the correct tool for SQLite test-only fixtures — that is not
"production schema".)

## M4 — One head

Normal development leaves exactly one Alembic head under
`migrations/postgresql_versions/`. Parallel migration branches must be
reconciled explicitly before merge.

## M5 — No stamp-as-repair

`alembic stamp head` must never be used to conceal schema drift. Stamp
is not a migration.

## M6 — Migration testing

Every schema change must verify at least fresh PostgreSQL DB -> `alembic
upgrade head`, and previous head -> new head. When downgrade is
supported: new head -> previous revision -> new head.

## M7 — Model / database consistency

On a migrated PostgreSQL database, `alembic check` must report no
pending model/schema operations.

## M8 — Production data is never reset to fix a migration

Development databases may be disposable (as `bel.db` always was, and as
a PostgreSQL dev database remains until real business data accumulates).
Once BEL begins carrying authoritative business facts, migration bugs
must be repaired forward. Dropping production data is never a migration
strategy.

## M9 — Database revision is checked before serving traffic

Web startup and CLI initialization refuse a database whose Alembic
revision is not the single expected head — see
`src/bel/infrastructure/persistence/schema_gate.py`. This is a
PostgreSQL-only gate: SQLite has no active Alembic chain, so an
injected/explicit SQLite runtime bypasses it by construction — deliberate
and unsupported for production. It is a revision CHECK, never an
automatic migration; BEL never silently migrates a production database
at startup.

## M10 — PostgreSQL defines production persistence semantics

Passing an SQLite test is not proof of a PostgreSQL persistence
invariant. Concurrency, migration, constraint and transactional
acceptance must run on PostgreSQL — see `tests/postgres/` (marked
`@pytest.mark.postgres`, auto-skipped unless `BEL_DATABASE_URL` points at
a real PostgreSQL database) and `tests/integration/test_migration.py`.

## Mechanical enforcement

`tools/check_migration_immutability.py` — forward-enforcing from the two
markers above, never retroactive. `--staged` (wired into
`.githooks/pre-commit` alongside `privacy_scan.py`, neither replacing the
other) compares the git index against HEAD. `--history` (CI's
`postgresql-gate` job) inspects `merge-base(origin/main, HEAD)..HEAD`
commit-by-commit — never full repo history — catching a
commit-N-adds/commit-N+1-modifies pattern a final PR diff alone would
hide as one clean added file.

## Runtime contract

**PostgreSQL 18 is the BEL V1 production/runtime target.** No older or
newer major version is documented as a supported production baseline —
there is no compatibility matrix, and none is planned. SQLite remains
explicitly a test-only convenience, never a production runtime. (Earlier
Phase 2D.1-P investigation and verification used PostgreSQL 16 and 17
locally and in CI before the target was frozen at 18 — that history is
left in commit messages and this document's own "rebaseline" section
rather than erased; it does not change the frozen target.)

Canonical `BEL_DATABASE_URL` forms:

- `postgresql+psycopg://user:password@host:5432/bel` — production, the
  only chain `alembic upgrade head` runs against
- `sqlite:///path/to/file.db` / `sqlite://` (in-memory) — test
  convenience only, no active Alembic chain, no concurrent-Web guarantee

`BEL_DATABASE_URL` is BEL's ONE runtime configuration contract, with no
second protocol (no `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_PASSWORD`/`DB_NAME`
reconstructed internally) and no fallback: Alembic (`migrations/env.py`)
requires it explicitly for every operation that needs to know a
database and fails fast with a controlled `MissingDatabaseUrlError` if
it's unset — it never falls back to `alembic.ini`'s own `sqlalchemy.url`
key, which is a deliberately unresolvable placeholder, never a usable
local/default connection string. CLI: `--database-url` or the
`BEL_DATABASE_URL` environment variable, required. Web: same, via
`create_app(database_url=...)`.

**Local development `.env` loading** (`src/bel/infrastructure/env_bootstrap.py`)
is a SOURCE-CHECKOUT DEVELOPMENT convenience only, never a second
runtime configuration protocol or a general production discovery
mechanism. A repo-root `.env` (gitignored, never committed — see
`.env.example` for the placeholder template) containing
`BEL_DATABASE_URL` is auto-loaded by both the CLI and Alembic
(`migrations/env.py`), via the SAME shared helper so the two paths
cannot drift, before either reads the variable. Precedence, highest
first:

1. an explicit `--database-url` CLI flag (Click's own native
   flag-over-envvar behavior, untouched by this mechanism)
2. an already-exported process `BEL_DATABASE_URL`
3. this repo's `.env` file
4. the controlled `MissingDatabaseUrlError` / CLI "missing option" error

`.env`'s value NEVER overrides an already-set process variable
(`python-dotenv`'s `load_dotenv(..., override=False)`). Discovery is
anchored to `env_bootstrap.py`'s own installed location (walking up to
find a directory carrying BOTH `pyproject.toml` and `alembic.ini`
together — not a generic "search upward from cwd for any `.env`"), so it
is cwd-independent and structurally cannot mistake an unrelated parent
directory for a BEL checkout: a packaged/production install carries
neither marker, so the loader silently no-ops and production/CI continue
to require `BEL_DATABASE_URL` injected by their own deployment
environment exactly as before this mechanism existed.

Concurrency: `serialized_write_transaction` (SQLite: `BEGIN IMMEDIATE`;
PostgreSQL: `SET LOCAL lock_timeout` + `pg_advisory_xact_lock` on one
reserved global key) is the shared write-serialization boundary for
writers whose session is guaranteed fresh. `acquire_serialization_lock`
is the equivalent for writers whose established calling convention
cannot guarantee that (no-ops on SQLite, since its own file-level
locking already covers them implicitly). The `ProcurementSalesLink`
one-current-episode trigger additionally takes its own
business-key-scoped `pg_advisory_xact_lock` inside the trigger function
itself, so it stays correct even against a write that bypasses the
application layer entirely.

## Rebuild path

`bel.db` and any disposable PostgreSQL dev database are never a source
of truth. To rebuild:

```bash
createdb bel
BEL_DATABASE_URL=postgresql+psycopg://localhost/bel alembic upgrade head
BEL_DATABASE_URL=postgresql+psycopg://localhost/bel bel import-contract-ledger <path>
# ... re-import original Excel / Evidence, run backfill if applicable
BEL_DATABASE_URL=postgresql+psycopg://localhost/bel bel web
```

Source Excel / Evidence is the rebuild input — never the old SQLite
file. No SQLite -> PostgreSQL data migrator exists or is planned; this is
intentional (Part I of the Phase 2D.1-P brief).
