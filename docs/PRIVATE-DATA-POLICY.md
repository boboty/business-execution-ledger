# Private Data Policy

This policy is designed to keep the repository safe for public
development while private business data is used only through the
external private acceptance boundary. The policy is enforced by
tooling.

## The rules

**P01 — Private business data may be used locally for development and
acceptance testing.** It lives under `$BEL_PRIVATE_DATA_ROOT`, a
directory *outside* this repository, and is read directly from there
by local tooling (`tests/private_acceptance/runner.py`) and by hand
during development.

**P02 — Private business data MUST NEVER be committed to the
repository.** Not as a source file, not as a "temporary" branch, not as
an attachment to an issue/PR/commit message, not via Git LFS, not
base64-encoded, not inside an encrypted file checked into Git.

**P03 — This prohibition includes derived facts, not only source
files.** Specifically, none of the following may appear in
committed source, tests, fixtures, docs, commit messages, PR
descriptions, issues, or generated reports:

- exact amounts (contract/invoice/payment values, totals, balances)
- transaction/record counts (contract count, invoice count, row counts,
  "N eligible", "N ambiguous", etc.)
- source identifiers (contract numbers, invoice numbers, bank reference
  numbers, account numbers, source file names that reveal a company
  identity)
- source dates when linked to a sensitive business fact (a transaction date
  tied to a source amount, for example) — generic period labels like
  `2026-07` as a directory name are not themselves private
- counterparty names (suppliers, customers, banks)
- person names
- bank account/routing information
- acceptance outcomes or conclusions derived from non-public inputs

**P04 — Renaming entities while preserving source values is NOT
sufficient anonymization.** Replacing a counterparty's name with a
placeholder like "Company A" while keeping source amounts, contract
numbers, dates, and record counts still discloses a sensitive business
shape and is treated as a policy violation, not a mitigation.

**P05 — Committed test data must be independently synthetic.**
Fixtures under `fixtures/synthetic/` are constructed from the rules and
test scenarios they exist to exercise — not derived by editing,
subsetting, or rescaling a non-public file. See `fixtures/synthetic/scenarios.py`
for the current fixture set and its scenario coverage.

**P06 — Private acceptance logs must be redacted by default.**
`tests/private_acceptance/runner.py` prints only `SCENARIO_ID: PASS` or
`SCENARIO_ID: FAIL` to stdout — nothing else, ever. Full diagnostics are
written only under `$BEL_PRIVATE_DATA_ROOT/reports/<SCENARIO_ID>.json` —
never into the repository, never to stdout.

**P07 — Agent-generated artifacts are bound by this same policy.**
Commit messages, PR descriptions, issues, docs, test reports, and
completion reports produced by an AI coding agent (or a human) must
follow P01–P06 exactly like any other repository content. "It's just a
report, not source code" is not an exemption.

## Layout

```
$BEL_PRIVATE_DATA_ROOT/
  <period>/                    e.g. 2026-07
    contracts/                 private contract ledger workbook(s)
    invoices/                  private invoice ledger workbook(s)
    bank/                      private bank statement PDF(s)
    facts/                     private Close Fact Pack(s) for period close, e.g. phase2b-close-facts.json
    expected/                  private expected-results material
  reports/                     private-acceptance diagnostics

fixtures/synthetic/            independently constructed synthetic data — committed
tests/golden/synthetic-v1/     public golden suite against fixtures/synthetic/ — committed
tests/private_acceptance/      scenario-ID-only harness, reads $BEL_PRIVATE_DATA_ROOT — committed, no source values
data/private/                  legacy in-repo location, gitignored, kept empty — second layer of defense only
```

`data/private/` remains in `.gitignore` as a second layer of defense,
but no development or acceptance workflow depends on private files living
inside the repository tree.

## Enforcement

`tools/privacy_scan.py` (Path Guard, Local Denylist, Generic Guard,
Commit Message Guard) checks staged changes, all tracked files, and — on
demand — full Git history for policy violations. Git hooks
(`.githooks/pre-commit`, `.githooks/commit-msg`) run the staged/commit-message
checks automatically once installed (`git config core.hooksPath
.githooks`). CI runs the tracked-file and Generic Guard checks on every
push, independent of any local denylist.

See also `CLAUDE.md` / `AGENTS.md` for the corresponding hard rules
given to AI coding agents working in this repository.

## If uncertain

Treat any value you're unsure about as private. When in doubt, leave it
out of anything committed.
