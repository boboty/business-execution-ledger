# Phase 2A Decisions

Judgment calls made while implementing Phase 2A. Per the Phase 2A spec
section 32: **no Phase 0 document was modified.** `DOMAIN.md`'s existing
Invoice/Payment sections (many-to-many via matching, bank-grain
preservation) already anticipated everything built here — no
`SPEC_CHANGE_REQUEST` was needed against Phase 0. This file plus
`docs/PHASE1-DECISIONS.md` are where Phase 2A-specific field/behavior
choices are recorded instead.

## Eligibility scoping (post-review fix — was a genuine BLOCKER-severity bug)

The first working version of `match_invoices`/`match_payments` ran M001
over **every** PURCHASE invoice / OUT payment, with no explicit
eligibility concept. That could create a formal `UNMATCHED` `MatchCase`
for an invoice or payment whose counterparty was never a party to any
contract — exactly the "ContractNotFound"-style noise spec section 14
forbids for payments, and the same protection is required for invoices.
The earlier aggregate assertion `auto + human == eligible` could not
distinguish correctly scoped eligibility from extra `UNMATCHED` rows.

Fixed by adding an explicit eligibility gate in `_run_match_pass`,
applied *before* Pass 1's candidate computation: a subject is eligible
iff `normalize_counterparty(subject's counterparty)` is in the set of
normalized contract counterparties — **counterparty membership only,
never amount**. Subjects that fail this check are filtered out before
they ever reach candidate computation and never become a `MatchCase` of
any status, including `UNMATCHED`. `MatchRunSummary` now reports
`eligible_total` and `out_of_scope` explicitly, with total persisted
`MatchCase` rows per subject_type equal to `eligible_total` exactly —
zero extra. The synthetic golden tests assert `unmatched` and
`eligible_total` directly against their synthetic baseline instead of
deriving them from a sum, and a unit test
(`test_out_of_scope_counterparty_gets_no_match_case_at_all`) asserts
zero `MatchCase` rows exist for an out-of-scope subject.

## Technology choices

- **pdfplumber**, not pypdf/PyPDF2, for the bank statement PDF.
  pdfplumber exposes per-word `(x0, top)` positions; pypdf's flat
  text-stream extraction does not. The adapter reconstructs transaction
  rows deterministically from those positions rather than relying on a
  drawn table grid or pdfplumber's own table-finder.
- `compute_sha256` moved to `bel/adapters/common.py` (used identically by
  all three importers now); re-exported from `contract_ledger.py` so
  Phase 1's import site didn't need to change.

## EvidenceFragment upgrade (fragment_kind / locator_json)

`sheet_name`/`row_number` made nullable via `batch_alter_table` (SQLite
can't `ALTER COLUMN` without a table rebuild); `fragment_kind` added
`NOT NULL` with a `server_default='EXCEL_ROW'` that backfills existing
Phase 1 rows during the rebuild, then the default is dropped so future
inserts must set it explicitly. Verified against a database seeded with
a pre-migration row via raw SQL, not just against an empty one — the
backfill actually runs, not merely declared. `PDF_TRANSACTION` fragments
use `locator_json={"page": ..., "transaction_index": ...}` (0-based,
sequential across the whole document) and leave `sheet_name`/`row_number`
null, exactly as spec section 4 sketches.

## Two real bugs the FK-enforcement lesson from Phase 1 repeated

Phase 1 already established that SQLite's `PRAGMA foreign_keys=ON`
(`database.py`) means SQLAlchemy's flush does **not** auto-order INSERTs
across mapped classes without an ORM `relationship()` — so a fragment
must be flushed before the contract referencing it. Phase 2A hit the
*same* class of bug twice more, independently:

1. `MatchCandidate`/`Allocation` rows reference a `MatchCase.id` created
   in the same pass — fixed with explicit `session.flush()` calls
   immediately after each `match_case_repo.add(...)` in
   `_run_match_pass`, before anything referencing that id.
2. `InvoiceItem` rows reference `Invoice.id` — fixed by splitting
   `import_invoices()` into three explicit passes (fragments -> flush ->
   invoices -> flush -> items) instead of interleaving.

Both were caught by writing the matching-engine tests immediately after
the feature, not deferred to the golden run — the golden run alone
would **not** have caught the second one, because idempotency skip logic
means a partially-broken importer can still look "done."

## A third bug: `match_invoices`/`match_payments` never committed

The first working version of the matching engine ran correctly (all
values computed correctly in memory, `MatchRunSummary` counts correct)
but never called `session.commit()` — so every `bel match run` silently
discarded all `MatchCase`/`MatchCandidate`/`Allocation`/`BusinessEvent`
rows the moment the session closed. This is exactly the kind of bug a
purely golden-number-driven check would miss if the golden test used the
*same* session that the CLI does, and exactly why the CLI smoke-test
(`bel match run` against a real `bel.db` file, then a fresh `sqlite3`
connection to inspect it) mattered as its own check, independent of the
pytest fixtures. Fixed by adding `session.commit()` at the end of
`_run_match_pass`; `confirm_match()` deliberately does *not* commit
internally (see below).

## Two-phase matching engine — how "no sequence guessing" is structural, not just tested

`match_invoices`/`match_payments` (via the shared `_run_match_pass`)
compute every subject's candidate `Contract` set in one pure pass over a
single static contract snapshot (`_find_candidate_contract_ids` reads
only its arguments, mutates nothing), *before* writing anything. Pass 2
turns each already-fixed candidate set into an outcome. Because pass 1
never consumes or mutates the contract set, no subject's evaluation can
depend on another subject's processing order — Excel row order, invoice
date order, and bank statement order are all structurally irrelevant to
who ends up ambiguous. Verified directly:
`test_no_sequence_guessing_two_contracts_two_invoices_same_amount` (two
same-amount contracts, two same-amount/same-counterparty invoices —
both land in `HUMAN_CONFIRMATION_REQUIRED` with both contracts as
candidates, zero allocations).

**The one place processing order can still matter:** two *genuinely
unique*-candidate subjects racing for the same contract's remaining
capacity (section 24). This is not the forbidden kind of guessing — both
subjects were unambiguously identified; the conflict is a real
resource constraint (total allocated can't exceed contract gross), and
the loser gets an auditable `ALLOCATION_CAPACITY_EXCEEDED`
`TaskException` plus a `HUMAN_CONFIRMATION_REQUIRED` MatchCase, never a
silent wrong allocation. Subjects are processed in a deterministic but
business-meaningless order (`sorted by str(id)`) so this edge case is at
least reproducible run-to-run. This path is exercised only by the
synthetic `test_allocation_capacity_exceeded_blocks_second_unique_match`,
not tuned against any particular dataset.

## SUPERSEDED: "multiple equivalent candidates => HUMAN_CONFIRMATION_REQUIRED"

The former rule above — several EXACTLY equivalent candidates (same
counterparty + same amount) must land in `HUMAN_CONFIRMATION_REQUIRED`
because sequence guessing was forbidden — is **superseded by later
business-owner confirmation** for procurement. The historical text above
is kept on purpose as the record of what Phase 2A originally decided.

New frozen procurement rule — *explicit, then chronological, then human*:

1. **Explicit relationship wins.** A subject that already has an
   authoritative MatchCase/Allocation (AUTO_CONFIRMED, a human-confirmed
   `RESOLVED` case, an `HUMAN_CONFIRMATION_REQUIRED` case, ...) is never
   reconsidered, reassigned, or duplicated.
2. **Otherwise chronological allocation.** When a procurement Invoice /
   OUT Payment can correspond to several equivalent Contracts (same
   counterparty + same amount), BEL does NOT ask a human merely for that
   reason. Subjects are processed in business chronological order
   (invoice `issue_date` ASC / OUT payment `transaction_date` ASC), and
   each subject allocates to the EARLIEST candidate Contract
   (`contract_date` ASC) that still has sufficient remaining capacity.
   Within a same date a deterministic stable tie-break is used — a real
   business/source identifier first (`contract_no`, invoice
   `external_invoice_key`/`digital_invoice_no`/`invoice_no` tried in that
   order — a blank/whitespace-only value does not count as usable and
   falls through to the next one, exactly like `NULL` would — payment
   `bank_reference`), UUID only as the final tie-breaker.

   **Dates are required ONLY when chronology is actually needed to choose
   between multiple otherwise-valid possibilities.** Chronology uses real
   business dates; a missing date is never replaced by technical ordering
   (`NULL`-first/last, `created_at`, import order, `contract_no`, or UUID
   used *as if* it were a date). Specifically:

   - a subject with exactly ONE valid candidate Contract (or whose multiple
     original candidates are narrowed by capacity to exactly ONE that can
     accept it) allocates normally even when `contract_date` /
     `issue_date` is NULL — chronology is not needed when no real choice
     remains;
   - if MORE than one valid candidate remains AND choosing requires the
     chronological fallback, EVERY competing candidate must have a real
     `contract_date`; if any competing candidate has `contract_date = NULL`,
     BEL cannot truthfully determine the earliest Contract and the case is
     `HUMAN_CONFIRMATION_REQUIRED` (no allocation);
   - likewise an Invoice needing subject chronology with `issue_date = NULL`
     is never given a fabricated ordering — it stays
     `HUMAN_CONFIRMATION_REQUIRED` (OUT Payment `transaction_date` is
     domain-required and unchanged).

   **"Effective uniqueness" must be established independently of any
   chronological allocation created in the same unresolved cohort this
   run — never manufactured by processing order.** Unresolved subjects
   sharing the same normalized counterparty + exact amount share the same
   static candidate Contract pool and are therefore a *cohort* competing
   for that pool. A missing date sorted last is not evidence of anything:
   letting a dated cohort member consume capacity first and then treating
   the narrowed leftover as "the one remaining candidate" for an undated
   sibling would fabricate a chronology the source data never established
   — that "effective uniqueness" was CREATED by this run's own undefined
   ordering, not by the business facts. Concretely: for a cohort of 2+
   unresolved subjects, chronological fallback allocation runs for the
   WHOLE cohort this pass only when it is well-defined for every member —
   every competing subject has a real business date AND every Contract
   still able to accept one of them, by pre-existing *pre-run*
   authoritative capacity alone, has a real `contract_date`. If either
   is missing anywhere in the cohort, NONE of its members are
   chronologically allocated this run — not even the dated ones — and
   each stays `HUMAN_CONFIRMATION_REQUIRED` with the full static candidate
   list; dated members are never processed first merely to consume
   capacity and narrow what an undated sibling sees. A single unresolved
   subject is never subject to this cohort check (nothing else in this
   run could have manufactured its uniqueness), and capacity already
   consumed by a pre-existing authoritative decision (a prior run, or a
   human confirmation) is unaffected by this rule — it can still make a
   cohort, or a lone subject, genuinely and independently unique.

   The allocation carries
   `AllocationMatchMethod.EXACT_COUNTERPARTY_AMOUNT_CHRONOLOGICAL` and the
   MatchCase is `AUTO_CONFIRMED` — meaning BEL deterministically applied
   the confirmed business rule, NOT that source Evidence explicitly proved
   that exact one-to-one historical relationship. Once a Contract's
   capacity is consumed, the next chronological subject naturally advances
   to the next chronological available Contract.
3. **Human review only when the deterministic rule cannot resolve** — e.g.
   no candidate has sufficient remaining capacity (the existing
   `HUMAN_CONFIRMATION_REQUIRED` + `ALLOCATION_CAPACITY_EXCEEDED`
   protection path, never a silent over-allocation), zero valid Contract
   correspondence (`UNMATCHED`), chronology unavailable because a competing
   date is missing, or conflicting explicit information. HCR is NOT raised
   merely because two or more same-counterparty / same-amount Contracts
   exist when their dates are all present and the chronological rule can
   decide.

Why: accounting does not require arbitrary atom-by-atom manual
confirmation when equivalent transactions can be deterministically
allocated without changing the business result.

The former "no sequence guessing" tests and the synthetic golden baseline
were updated to the confirmed rule (e.g. the former two-invoice /
two-contract HCR test is now
`test_two_contracts_two_invoices_chronological_allocation`), and the
matching module now processes subjects and candidates in business
chronological order instead of `sorted by str(id)`.

## `MatchCaseStatus` for the capacity-exceeded outcome

Spec section 25's status enum (`AUTO_CONFIRMED` /
`HUMAN_CONFIRMATION_REQUIRED` / `RESOLVED` / `REJECTED` / `UNMATCHED`)
has no dedicated "unique candidate but capacity-blocked" state. Chosen:
`HUMAN_CONFIRMATION_REQUIRED` (a human needs to look at it — same
category as ambiguity, different cause), with the *reason* captured
precisely by the accompanying `ALLOCATION_CAPACITY_EXCEEDED`
`TaskException`, not by inventing a new status value.

## Every AUTO_CONFIRMED match still gets a MatchCandidate row

Spec section 18 only explicitly requires `MatchCandidate` rows for the
ambiguous case. Phase 2A creates one for the unique-candidate case too
(and for the capacity-exceeded case). This isn't required by the letter
of the spec, but it makes `MatchCase -> MatchCandidate` traceability
uniform across every outcome instead of a special case, and costs one
extra row per confirmed match.

## `InvoiceAllocation`/`PaymentAllocation.match_case_id` added beyond the spec's minimum field list

Spec section 23 lists Allocation fields without `match_case_id`, but
section 27 requires `Allocation -> MatchCase -> Fact -> Evidence`
traceability explicitly. Without a stored `match_case_id`, that
traceability would depend on `MatchCase.subject_id` being unique per
subject (true today, under M001 only) rather than being a real,
independent, always-correct link. Added the FK; per section 32 this is
"落实已经冻结的概念成字段," not an architecture change.

## Matching idempotency

Re-running `bel match invoices` / `bel match payments` skips any subject
that already has a `MatchCase` (any status) — reported as
`already_matched_skipped`. This does not re-evaluate `REJECTED` cases;
re-matching after rejection is out of scope for Phase 2A (no rule for
"try again with different criteria" exists yet).

## Invoice-ledger parsing

- **Business-row detection**: a row is an invoice header iff
  `digital_invoice_no` or `invoice_no` is present (both blank on
  continuation rows) — presence of a seller name is expected to
  correlate exactly with presence of one of these two fields; a
  mismatch would indicate a parsing assumption is wrong.
- **`buyer`** is read once from cell A2 (the workbook's own title/header
  cell) and applied as a constant to every Invoice in the import,
  exactly like Phase 1's `contract_type` constant.
- **Blank `tax_rate`/`tax_amount`** (both invoice-level and item-level)
  parse to `Decimal("0")`, not `null` — a blank-tax row is expected to
  have `net_amount == gross_amount` (tax-exempt line items), so treating
  blank as zero is the correct accounting reading, not a guess. Cross-checking
  `net + tax == gross` at the aggregate level in the golden/acceptance
  tests guards against this silently masking a real parsing failure.
- **`external_invoice_key`** = `digital_invoice_no`. The
  `invoice_code + invoice_no` fallback spec section 10 mentions for
  future ticket types is not implemented; the column is nullable+unique
  so it can be added later without a schema change.

## CMB bank statement PDF parsing

- **The adapter does not assume fixed cross-page column positions.**
  Fixed-pixel column ranges were abandoned in favor of
  **content-based field detection**: the two purely-numeric decimal
  tokens in a row (`-?[\d,]+\.\d{2}`) are always amount-then-balance
  regardless of their x-position; `business_type` is the leftmost
  remaining word; `bank_reference` is a lone long digit token right
  after it, if present; everything else before the amount is
  `description`, everything after balance is `counterparty`.
- **Row banding**: a transaction's text can wrap across multiple
  physical PDF lines. Rows are therefore assigned to bands by proximity
  to the nearest date anchor (midpoint between consecutive anchors),
  not by "next date starts a new row."
- **Page footers** ("第N页/共M页...") bleed into the last transaction's
  band unless excluded — the footer's own vertical position is found and
  used as that page's bottom boundary.
- **`bank_reference`/`description` split is best-effort**, not
  guaranteed-correct on every row. It's exact for rows shaped like
  `business_type + bill_no + description`. Rows whose reference number
  itself wraps across lines and partially overlaps the `bank_reference`
  detection window can produce an imperfect split between
  `bank_reference` and `description`. This does **not** affect
  `transaction_date`, `direction`, `amount`, or `counterparty` — the
  fields M001 and the reconciliation golden/acceptance tests depend on
  — which the acceptance tests check exactly for every transaction
  (full running-balance chain continuity check, not just aggregate
  IN/OUT totals).
- **No OCR** — the PDF's text layer is used throughout.

## Synthetic fixture boundary

All committed fixtures and their baselines are independently constructed
from the rules and scenarios they exercise. Sensitive inputs, source
identifiers, and derived values do not belong in the repository; see
`docs/PRIVATE-DATA-POLICY.md`.

## Data file layout

See `docs/PRIVATE-DATA-POLICY.md` for the external-data layout.
sha256-based idempotency is content-based, so renaming/moving a source
file is harmless to already-imported state.
