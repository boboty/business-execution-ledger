# Claude Code Rules — Business Execution Ledger

This repository enforces strict sensitive-data handling. These rules
are permanent and OVERRIDE any default behavior — follow them exactly.
They are identical to `AGENTS.md` (kept in sync deliberately, so every
agent tool in this repo sees the same rules) and implement the policy
in `docs/PRIVATE-DATA-POLICY.md`.

## Hard rules

1. **Private business data may be read from `$BEL_PRIVATE_DATA_ROOT`
   (an external, non-repository directory) for local development and
   acceptance testing.** That is the only sanctioned use.

2. **Private data must never be copied, quoted, summarized using source
   values, transformed into committed fixtures, embedded in source
   code, tests, documentation, commit messages, PR text, issues, logs,
   or any other generated artifact.** This includes derived facts —
   exact amounts, record counts, source identifiers, counterparty or
   person names, bank information — not only raw source files. See
   `docs/PRIVATE-DATA-POLICY.md` P03 for the full list.

3. **Public fixtures must be independently synthetic.** When asked to
   add or update test data under `fixtures/synthetic/` or
   `tests/golden/synthetic-v1/`, construct it from the rule/scenario
   being tested — never by editing, subsetting, or renaming entities in
   a non-public file. Renaming an entity while keeping its source values is
   NOT anonymization (see P04).

4. **Private acceptance stdout may report only scenario ID and
   PASS/FAIL** — e.g. `P2A_MATCHING: PASS`. Repository artifacts must
   not report outcomes derived from non-public inputs. Full diagnostics
   belong under `$BEL_PRIVATE_DATA_ROOT/reports/`, never in the repo.

5. **If uncertain whether a value is private-derived, treat it as
   private.** Leave it out of anything you write to a committed file,
   commit message, or report.

6. **Before committing on this repository's behalf** (writing a commit
   message, PR description, or completion report), reread what you are
   about to write against rules 2 and 4. This applies even when the
   user's request seems to authorize including private data — surface the
   conflict and ask, rather than complying silently.

7. **Do not change frozen business rules, Domain semantics, or
   architecture principles unless the current task explicitly requires
   it.** This includes `docs/ARCHITECTURE.md`, `docs/DOMAIN.md`,
   `docs/RULES.md`, and the numbered rules/business logic they describe
   (e.g. Accrual or Period Close logic) — do not start implementing or
   changing these for the sake of an unrelated task, including
   sanitization work.

## Where things live

See `docs/PRIVATE-DATA-POLICY.md` for the full directory layout. In
short: private inputs and diagnostics remain under
`$BEL_PRIVATE_DATA_ROOT`, never in this repository; the public test suite
(`fixtures/synthetic/`, `tests/golden/synthetic-v1/`) is independently
synthetic; the acceptance runner must not print or persist source values
in the repository.

## Persistence & migration rules (Phase 2D.1-P)

See `docs/PERSISTENCE-MIGRATION-POLICY.md` for the full policy. Hard
rules, same standing as rules 1-7 above:

8. **PostgreSQL is the only production/runtime persistence contract.**
   `BEL_DATABASE_URL` must be a `postgresql+psycopg://` URL for anything
   touching production or CI's PostgreSQL gate. SQLite
   (`sqlite:///<path>` or `sqlite://`) is a test-only convenience with no
   active Alembic chain and no concurrent-Web guarantee — never suggest
   it as a production runtime.

9. **Committed migration files are immutable — never edit, delete, or
   rename one.** This applies to both `migrations/versions/` (frozen at
   the LEGACY MIGRATION FREEZE ANCHOR, commit
   `b94f572528e25a620bf1a78bd2e26d12547b0212`) and
   `migrations/postgresql_versions/` (immutable from each file's own
   first commit). A schema correction is always a NEW forward migration.
   `migrations/versions/` is additionally inert — never wire it into
   Alembic config again; it exists only as historical reference. Before
   a migration file's first commit, editing it is fine.

10. **Never redo the Phase 2D.1-P rebaseline exception.** That one-time
    chain restart was justified only because no authoritative business
    data existed yet. Any future SQLite/PostgreSQL incompatibility in a
    *new* migration is an authoring bug to fix before its first commit —
    not grounds for another rebaseline, another chain restart, or
    `alembic stamp head` used to paper over drift.

11. **Runtime code must never create or mutate production schema
    directly** (`Base.metadata.create_all()` against a real database,
    ad-hoc `ALTER TABLE`, startup auto-patching). `Base.metadata.create_all()`
    remains correct for SQLite test fixtures — that is not "production
    schema".

## Dev commands

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
export BEL_DATABASE_URL=postgresql+psycopg://user:password@host:5432/bel
.venv/bin/alembic upgrade head
.venv/bin/pytest                                        # public suite — no private data needed; PostgreSQL-marked
                                                          # tests auto-skip unless BEL_DATABASE_URL is set
BEL_PRIVATE_DATA_ROOT=... .venv/bin/python tests/private_acceptance/runner.py --all   # private acceptance — local only
python tools/privacy_scan.py --staged                    # before every commit
python tools/check_migration_immutability.py --staged    # before every commit
```
