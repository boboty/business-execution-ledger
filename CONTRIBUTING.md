# Contributing

Contributions are welcome. BEL is still early, so the most valuable contributions are usually small, testable improvements that preserve the system's architecture boundaries.

## Before you start

Read these first:

- `docs/ARCHITECTURE.md` — frozen architecture principles
- `docs/V1-SCOPE.md` — current scope and non-goals
- `docs/RULES.md` — numbered business rules
- `docs/PRIVATE-DATA-POLICY.md` — mandatory public-data boundary
- `docs/PERSISTENCE-MIGRATION-POLICY.md` — PostgreSQL runtime contract and migration immutability rules

## Ground rules

1. **Do not turn prompts into business rules.** Deterministic business decisions belong in code.
2. **Preserve Evidence → Fact → Decision traceability.** New outputs should remain defensible back to their source evidence.
3. **Do not silently guess.** Ambiguity should become an explicit proposal, task or exception.
4. **Keep the Business Core runtime-agnostic.** Agent frameworks belong outside the domain layer.
5. **Never contribute real business data.** Public tests and examples must use synthetic data.
6. **Never edit, delete, or rename a committed migration file.** A schema correction is always a new forward migration under `migrations/postgresql_versions/` — see `docs/PERSISTENCE-MIGRATION-POLICY.md`.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
git config core.hooksPath .githooks

# A local PostgreSQL 18 instance (BEL's V1 production target) is needed
# for anything touching the migration chain or PostgreSQL-marked tests —
# createdb, then either export BEL_DATABASE_URL directly:
export BEL_DATABASE_URL=postgresql+psycopg://localhost/bel_dev
# ...or copy .env.example to a repo-root .env and fill it in — both `bel`
# and `alembic` auto-load it from within this checkout (development
# convenience only, never a production mechanism; see
# docs/PERSISTENCE-MIGRATION-POLICY.md's "Runtime contract" section for
# the exact precedence rule).
.venv/bin/alembic upgrade head

.venv/bin/pytest   # PostgreSQL-marked tests auto-skip if BEL_DATABASE_URL isn't set
```

Before opening a pull request, run the test suite, the privacy scanner (`tools/privacy_scan.py --staged`), and the migration immutability checker (`tools/check_migration_immutability.py --staged`) — both also run automatically via `.githooks/pre-commit`. Prefer focused PRs with an explanation of the business rule or architecture principle affected.

For behavior changes that expose a gap in a frozen design document, record the implementation judgment explicitly rather than silently rewriting the design to fit the code.