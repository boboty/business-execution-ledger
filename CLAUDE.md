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

7. **Do not start Phase 2B, implement Accrual or Period Close logic, or
   change business rules for the sake of sanitization** unless the user
   explicitly asks for that specific work in this session.

## Where things live

See `docs/PRIVATE-DATA-POLICY.md` for the full directory layout. In
short: private inputs and diagnostics remain under
`$BEL_PRIVATE_DATA_ROOT`, never in this repository; the public test suite
(`fixtures/synthetic/`, `tests/golden/synthetic-v1/`) is independently
synthetic; the acceptance runner must not print or persist source values
in the repository.

## Dev commands

```bash
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/pytest                                        # public suite — no private data needed
BEL_PRIVATE_DATA_ROOT=... .venv/bin/python tests/private_acceptance/runner.py --all   # private acceptance — local only
python tools/privacy_scan.py --staged                    # before every commit
```
