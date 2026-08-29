# Phase 2D.0 Decisions

Phase 2D.0 — V1 Product Rebaseline. Documentation-only: no `src/`,
`migrations/`, `tests/`, or `fixtures/` file was changed, and
`docs/ARCHITECTURE.md`, `docs/DOMAIN.md`, `docs/RULES.md`, and every
existing Phase 1/2A/2B/2C/2C.2 decisions and acceptance document are
byte-identical to before this phase. Historical phase documents are
history and are not retroactively rewritten.

This document records the judgment calls behind the rewritten
[V1-SCOPE.md](V1-SCOPE.md), [ROADMAP.md](../ROADMAP.md), and
[README.md](../README.md), and — the core of this round — a **Code
Reality** section stating what the codebase actually does today.

## Why this phase was re-done against code, not against documents

An earlier pass at this rebaseline described several capabilities from
`V1-SCOPE.md` and `DOMAIN.md` rather than from the code. Those documents
freeze *concepts*; a concept being frozen is not evidence that an
implementation exists. Reading the two together produced overstatements
in three places — sales-side capability, `ContractItem` intake, and
`Shipment / Export` — each of which changed the plan once checked.
Everything in the Code Reality section below is stated with the source
location that supports it, so any claim can be re-verified rather than
trusted.

## Why the Definition of Done changed from a technical-phase checklist to a product outcome

The prior framing ("V1 is done when Phase 2A/2B/2C ship") measures
internal progress, not whether BEL replaced anything. The system this
project displaces is a manually-maintained contract ledger spreadsheet:
staff read it, update it by hand, and treat its cells as the truth about
contract execution. A phase checklist can be fully "complete" while that
spreadsheet is still what people trust. Reframing the Definition of Done
as "BEL is the System of Record instead of Excel" ties every remaining
phase to a checkable product outcome. It does not change what already
shipped (Phase 2A–2C.2 are unaffected); it changes what "done" means
next.

## Why cutover — not feature completion — is the real Definition of Done

A system that only handles business occurring after go-live is not a
System of Record; it is a second system running beside the old one. If
the historical ledger and its Evidence never enter BEL, staff must keep
consulting Excel, and Excel remains authoritative in practice no matter
how many surfaces BEL ships. Legacy backfill and a cutover
reconciliation are therefore first-class first-stage scope
([V1-SCOPE.md](V1-SCOPE.md) section 7), not follow-up chores.

The cutover infrastructure lands in Phase 2D.1-R5 and the **final
first-stage cutover acceptance runs after Phase 2D.4** — the two are
deliberately separate. A complete switch also depends on the 2D.2
accrual Data Product, the 2D.3 outbound-invoicing judgment and Data
Product, and the 2D.4 exception loop, none of which exist at R5. R5
delivers the mechanism and rehearses it against the contract-execution
fact layer; it does not declare the cutover.

## Why the legacy spreadsheet cannot be the acceptance baseline

The obvious cutover test — assert `BEL result == current Excel value` —
is wrong, and adopting it would bake the old system's defects into the
new one's acceptance criteria. A manually maintained ledger can contain
manual errors, stale state, incomplete information, contradictory
information, and results with no Evidence behind them. Being the
incumbent does not make it Golden Truth.

Acceptance is therefore against a **Cutover Baseline** built from the
legacy ledger *plus* source Evidence *plus* a business-confirmed
interpretation, with reconciliation outcomes that distinguish `MATCH`,
`BEL_CORRECTED_LEGACY`, and `UNRESOLVED`. `BEL_CORRECTED_LEGACY` exists
precisely because BEL disagreeing with the spreadsheet is sometimes the
system working correctly. The bar is `UNRESOLVED = 0` — every
discrepancy adjudicated — not universal agreement with Excel.

The Cutover Baseline is private expected-results material and lives
under `$BEL_PRIVATE_DATA_ROOT/<period>/expected/`, which
[PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) already defines for
exactly that purpose; no policy change is needed. Public output is
limited to `P2D_CUTOVER_RECONCILIATION: PASS`-style scenario/verdict
lines (P06).

## Why `ContractItem` completeness is the first-stage critical path

`ContractItem` has no ordinary business intake path today (Code Reality
A). That is not a UI inconvenience — R007 is already frozen: without
determinable product and quantity, only a contract-level candidate may
be formed, never a formal item-level `Accrual`. So formal accrual data,
partial reversal (R006), item-level invoice attribution, outbound
invoice quantity preparation, and the Contract Business Ledger's product
scope are all gated on it. Sequencing anything else first produces
surfaces whose dominant output is "cannot determine". It is therefore
Phase 2D.1's first implementation round.

It is deliberately **not** specified as CRUD. The required semantic is
human-supplied/confirmed Evidence → `ContractItem` Fact → deterministic
recompute, which keeps A02's `Decision → Fact → Evidence` chain intact
instead of letting a form write a canonical Fact with no Evidence
behind it.

## Why Fact Maintenance is not the same as editing derived state

In a "replace Excel" project the temptation is to let a user edit a
status cell directly, mirroring how the spreadsheet works. That would
reintroduce Excel's core failure mode inside BEL: a status a human can
overwrite reduces to whatever that human believed at that moment and
stops being derived, auditable state. Users supply Evidence, Facts,
human confirmations of Facts, and lawful corrections of Facts. States
like 待暂估/待红冲/可开票/已完成/已匹配/异常已解决 are always computed
from confirmed Facts by the rules in [RULES.md](RULES.md).

This is also why fact correction must be modeled rather than improvised:
"editing" must never be implemented by overwriting immutable `Evidence`
or by mutating a canonical Fact without an audit trail. The semantics
are unfrozen today (Code Reality E) and are the first item of Phase
2D.1-R0.

## Why `Shipment / Export` must enter Phase 2D.1

The legacy contract ledger is organized around export business. If BEL
cannot canonically express whether the goods a contract covers actually
shipped, it cannot replace that ledger — the Contract Business Ledger
would be missing a column that the spreadsheet it replaces has, and
cutover reconciliation would have nothing to reconcile on that
dimension. Since the implementation is entirely absent (Code Reality D),
it must be built, not merely surfaced.

Deliberately **not** frozen here: `Export completed → automatically
invoice eligible`. Export execution is one candidate input to a future
eligibility rule, not the rule itself. Freezing that link now would be
inventing an invoicing rule under cover of a shipping feature.

## Why the sales-side gap is a semantics freeze, not a parameter change

It would be convenient to describe closing the sales-side gap as
"parameterize the existing matching pass for `SALES`/`IN`". That is
wrong and this document explicitly rejects it. The purchase side
associates a supplier/seller to a contract's `counterparty`; the sales
side must associate a customer/buyer to a contract's `buyer`, and which
business amount and which relationship govern that association are not
settled questions. Reusing the purchase side's counterparty/amount
assumptions would silently invent a business relationship rule. The
semantics are frozen in Phase 2D.1-R0 before implementation in
Phase 2D.1-R3.

## Why the Outbound Invoicing Workbench was added, ahead of Business Cockpit

The prior V1 scope covered contract facts, matching and period close but
had no surface for getting sales invoices out — so BEL could not answer
"what can we bill now, and why", which is part of what the legacy ledger
is used for today. It is ahead of Business Cockpit because it closes a
hole in the core business loop, whereas a cockpit only summarizes a loop
that must already be closed.

A Sales Invoice Fact existing is not an invoicing eligibility Decision;
conflating the two would put an undeclared rule into a page. The
eligibility rule is frozen with the business before 2D.3.

## Why Business Cockpit is deferred

A dashboard's value depends on a settled, trustworthy business loop
underneath it. That loop is not closed: the Contract Business Ledger,
fact maintenance, the accrual Data Product, outbound invoicing, the
exception centre and cutover are all outstanding. Building a cockpit
over incomplete facts risks a confident-looking summary of data BEL does
not actually have, plus rework once the real surfaces land. Deferred to
after the five core surfaces and cutover — not cancelled, and not to be
pulled forward.

## Why Agent Runtime stays deferred

`ARCHITECTURE.md`'s Agent Runtime Boundary and the prior roadmap already
sequenced Agent Runtime after V1. Phase 2D.0 keeps that order and makes
the reason explicit: `Agent operates the system. Agent is not the
system.` (A01) presumes a system worth operating. Introducing a runtime
before the five surfaces and the cutover exist would mean building an
operator for a system that cannot yet answer its own Definition-of-Done
questions.

**Reading note on `ARCHITECTURE.md`'s version labels.** The frozen
[ARCHITECTURE.md](ARCHITECTURE.md) says "**V1 first implementation:** Pi
Agent Core" and "**V1.1 second implementation:** PydanticAI". Those
labels date from Phase 0 and name *Agent Runtime* milestones — which
runtime is implemented first, and which second, whenever that work
happens. They are not a statement that the product V1 frozen in
[V1-SCOPE.md](V1-SCOPE.md) includes an Agent Runtime; V1-SCOPE.md's
non-goals exclude Agent/LLM integration, and [ROADMAP.md](../ROADMAP.md)
sequences Agent Runtime after the cutover gate. Phase 2D.0 does not
modify `ARCHITECTURE.md` — it is frozen, and the boundary it states is
unchanged and still correct. Renaming those labels, if ever wanted, is a
deliberate revision of `ARCHITECTURE.md` in its own right, never a
silent edit folded into another phase.

## Confirmation: no Domain, Rule Engine, schema, or application code was changed

`docs/DOMAIN.md`, `docs/RULES.md`, `docs/ARCHITECTURE.md`, everything
under `src/`, `migrations/`, `tests/`, `fixtures/`, and every existing
`docs/PHASE1-*`, `docs/PHASE2A-*`, `docs/PHASE2B-*`, `docs/PHASE2C-*`
and `docs/PHASE2C2-*` document are unchanged. This phase touched only
`README.md`, `ROADMAP.md`, `CLAUDE.md`, `AGENTS.md`,
`docs/V1-SCOPE.md`, and the two `docs/PHASE2D0-*.md` documents. How each
of these claims is checked is set out in
[PHASE2D0-ACCEPTANCE.md](PHASE2D0-ACCEPTANCE.md).

---

# Code Reality / Impact Analysis

What the codebase does **today**, verified by reading it. Each capability
carries two independent statuses, because implementation state and
decision state are different axes and collapsing them into one label
loses information:

**Implementation Status** — `IMPLEMENTED` · `PARTIALLY IMPLEMENTED` ·
`NOT IMPLEMENTED`

**Decision Status** — `FROZEN` · `DEFERRED IMPLEMENTATION DECISION` ·
`REQUIRES BUSINESS RULE FREEZE` ·
`REQUIRES RELATIONSHIP / SEMANTIC FREEZE`

Vague phrasing — "basically supported", "to be refined later", "the
foundation exists" — is not used anywhere below, because it hides
implementation state instead of reporting it.

## Summary

| # | Capability | Implementation Status | Decision Status |
|---|---|---|---|
| A | Contract / ContractItem | PARTIALLY IMPLEMENTED | DEFERRED IMPLEMENTATION DECISION |
| B | Purchase-side matching | IMPLEMENTED | FROZEN |
| C | Sales-side association | PARTIALLY IMPLEMENTED | REQUIRES RELATIONSHIP / SEMANTIC FREEZE + REQUIRES BUSINESS RULE FREEZE |
| D | Shipment / Export | NOT IMPLEMENTED | Minimal semantics conceptually scoped; implementation details DEFERRED IMPLEMENTATION DECISION |
| E | Fact correction / supersession | NOT IMPLEMENTED | DEFERRED IMPLEMENTATION DECISION |
| F | Outbound invoicing | NOT IMPLEMENTED | REQUIRES BUSINESS RULE FREEZE |
| G | Task / Exception | PARTIALLY IMPLEMENTED | Existing producers FROZEN; R009–R012 REQUIRE BUSINESS RULE FREEZE |
| H | Data Products / export | NOT IMPLEMENTED | DEFERRED IMPLEMENTATION DECISION |
| I | Backfill / Cutover | NOT IMPLEMENTED | REQUIRES BUSINESS RULE FREEZE (cutover/backfill semantics) |

## A. Contract / ContractItem — PARTIALLY IMPLEMENTED · DEFERRED IMPLEMENTATION DECISION

`Contract` is implemented and populated: the contract-ledger Excel
adapter promotes contract-header fields — including both `counterparty`
and `buyer` — into canonical `Contract` facts
(`src/bel/adapters/excel/contract_ledger.py`,
`src/bel/application/import_contract_ledger.py`). `ContractItem` exists
as a Domain object (`src/bel/domain/contract.py`).

What is missing is `ContractItem` **intake**. The contract-ledger
importer structurally creates none — its result reports
`contract_items_created=0` unconditionally
(`src/bel/application/import_contract_ledger.py`). The only code path
that constructs a `ContractItem` is the Close Fact Pack importer
(`src/bel/application/import_close_facts.py`), a human-authored file.
There is no everyday business path — no web entry, no ledger-derived
items.

Consequence: `ContractItem` completeness is a first-stage critical path,
because R007 caps everything without item-level product/quantity at a
contract-level candidate. See [V1-SCOPE.md](V1-SCOPE.md) section 2.2.
Phase 2D.1-R1.

A second, separate gap on the query side: `search_contracts_by_no`
(`src/bel/application/search_contracts.py`) looks up by `contract_no`
and returns a flat list. No cross-contract listing, filtering, or
summarization query exists. That is Application-layer work, not a Domain
change — Phase 2D.1-R4.

## B. Purchase-side matching — IMPLEMENTED · FROZEN

`match_invoices` and `match_payments`
(`src/bel/application/matching.py`) implement deterministic association
with explicit matched / ambiguous / unmatched outcomes, and
`ContractItem ↔ InvoiceItem` allocation is implemented as a confirmed
manual write (Phase 2C). This path is working and is the reference
implementation the sales side must **not** be assumed to mirror.

## C. Sales-side association — PARTIALLY IMPLEMENTED · REQUIRES RELATIONSHIP / SEMANTIC FREEZE + REQUIRES BUSINESS RULE FREEZE

`InvoiceDirection.SALES` (`src/bel/domain/invoice.py`) and
`PaymentDirection.IN` (`src/bel/domain/payment.py`) exist, and the
invoice importer accepts `--direction sales`, so sales-side raw facts
can be imported and stored.

They are never associated with a `Contract`. `match_invoices` filters to
`InvoiceDirection.PURCHASE` and `match_payments` filters to
`PaymentDirection.OUT` (`src/bel/application/matching.py`). Sales
invoices and incoming receipts therefore enter BEL and stop there.

The accurate statement of this gap: **sales-side raw facts can exist and
be imported; the sales-side Contract association / matching pipeline is
not connected.** An earlier draft of this document described it as
"fields exist, only the semantics are unfrozen", which understated it —
the pipeline itself is absent.

This limits the Contract Business Ledger's sales-invoice state and
incoming-receipt state, the Outbound Invoicing Workbench's "does a
corresponding Sales Invoice Fact already exist" judgment, and sales-side
execution state generally.

It must **not** be planned as "parameterize matching for SALES/IN". The
purchase side associates supplier/seller to `Contract.counterparty`; the
sales side must associate customer/buyer to `Contract.buyer` (the field
exists and is populated by the ledger importer). Which business amount
and which relationship govern that association are unsettled. Semantics
freeze in Phase 2D.1-R0; implementation in Phase 2D.1-R3.

## D. Shipment / Export — NOT IMPLEMENTED · semantics conceptually scoped, details DEFERRED

`Shipment / Export` is frozen in [DOMAIN.md](DOMAIN.md) and
`Contract ↔ Export` is listed in [V1-SCOPE.md](V1-SCOPE.md) section 3,
but a repository-wide search finds no `Shipment` or `Export` domain
object, persistence model, adapter, or matching pipeline anywhere under
`src/`. Of the four V1 match types, this is the only one with no
implementation at all.

One concrete intake anchor already exists in code and is currently
discarded: the contract-ledger importer reads an export-contract-number
column and uses it **only** to compute a completeness statistic
(`missing_export_contract_no` in
`src/bel/application/import_contract_ledger.py`, surfaced as a CLI
count). The value is never promoted to a Fact and never used for any
association. The legacy ledger already carries an export linkage that
BEL reads and throws away.

Because the legacy ledger is organized around export business, this must
enter Phase 2D.1-R2 as a minimal vertical slice: domain object, Evidence
trace, intake path, `Contract` association, and Ledger projection.

`Export completed → automatically invoice eligible` is explicitly **not**
frozen — see F.

## E. Fact correction / supersession — NOT IMPLEMENTED · DEFERRED IMPLEMENTATION DECISION

A repository-wide search finds no supersession, correction, revision, or
"replaced-by" mechanism in the Domain or Application layers. Creating an
independent second Fact is the only thing possible today, with no link
back to what it corrects.

`Evidence` immutability ([DOMAIN.md](DOMAIN.md)) is not in question and
is preserved; what is missing is the lawful way to express "this Fact
supersedes that one" with an audit trail. Both `ContractItem`
maintenance (A) and backfill (I) depend on it, which is why it is the
first item of Phase 2D.1-R0. Phase 2D.0 names the gap and does not
design it.

## F. Outbound invoicing — NOT IMPLEMENTED · REQUIRES BUSINESS RULE FREEZE

No invoicing-eligibility or invoice-preparation logic exists anywhere in
the codebase, and no numbered rule in [RULES.md](RULES.md) produces such
an output.

The distinction that must not be blurred: **a Sales Invoice Fact
existing is not an invoicing eligibility Decision.** Storing a sales
invoice records what happened; deciding that a business scope is ready
to be invoiced is a rule, and that rule is not frozen.

Before Phase 2D.3 the business must confirm what combination of facts
means *not eligible*, *ready for invoice preparation*, *already
invoiced*, and *blocked / unresolved*. No rule may be invented ahead of
that. Prerequisites: A (ContractItem), C (sales-side), D
(Shipment/Export).

## G. Task / Exception — PARTIALLY IMPLEMENTED · existing producers FROZEN, R009–R012 REQUIRE FREEZE

Authoritative, deterministic unresolved work **already exists today**.
It comes in two structurally different forms, which the Exception Center
must not conflate:

**Persisted records** — rows that exist in the database until something
changes them:

- `ExceptionType.BUSINESS_KEY_CONFLICT`, written as a `TaskException` by
  the contract-ledger importer
  (`src/bel/application/import_contract_ledger.py`, R004)
- `ExceptionType.ALLOCATION_CAPACITY_EXCEEDED`, written as a
  `TaskException` by matching (`src/bel/application/matching.py`)
- `MatchCase` in `MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED`
  (`src/bel/domain/matching.py`, persisted via `MatchCaseModel` in
  `src/bel/infrastructure/persistence/models.py`)

**Deterministically recomputed results** — not persisted at all:

- Period Close blockers (`CloseBlocker`,
  `src/bel/application/period_close.py`). `build_period_close_preview`
  is a strict read-only preview that runs under `session.no_autoflush`
  and performs no write; each blocker is a DTO field recomputed from
  current Facts on every run and rendered by
  `src/bel/application/period_close_workbench.py`. No `TaskException`
  row is created for a blocker, and none should be inferred to exist.

The Exception Center's infrastructure can carry both forms without
waiting for any new rule freeze, but they are not interchangeable: a
persisted `TaskException` has an identity and an `OPEN`/`RESOLVED`
status that survives between runs, whereas a blocker exists only as long
as the facts that produce it do — it disappears on its own when new
Facts resolve the condition, which is exactly the closed loop
[V1-SCOPE.md](V1-SCOPE.md) section 5.2 describes. How the Center
presents a recomputed condition alongside a persisted record, without
inventing a fake persisted identity for the former, is a
`DEFERRED IMPLEMENTATION DECISION` for Phase 2D.4.

What is missing: `TaskException` (`src/bel/domain/exception.py`) models
only two `exception_type` values and a two-state `OPEN`/`RESOLVED`
status, with no modeled link from a resolution back to the recompute
that should make the condition disappear. And R009 `InvoiceUnmatched`,
R010 `PaymentUnmatched`, R011 `EvidenceMissing`, R012 `AmountMismatch`
are still `PROPOSED` in [RULES.md](RULES.md) — each requires a business
rule freeze before becoming an authoritative exception producer.

Two symmetrical errors to avoid: saying Phase 2D.4 depends entirely on
R009–R012 (it does not — real unresolved work exists now), and treating
R009–R012 as already official system rules (they are not).

## H. Data Products / export — NOT IMPLEMENTED · DEFERRED IMPLEMENTATION DECISION

The Period Close preview exists and is mature, and Contract 360 and the
period-close workbench already assemble multi-repository,
presentation-ready DTOs (`src/bel/application/contract_360.py`,
`src/bel/application/period_close_workbench.py`) — reusable query
composition that the exports should extend rather than replace.

But no export capability exists anywhere: no Application service and no
CLI command produces a file. Every Application service returns
Domain/DTO objects to a caller. Whether exports are a new
Application-layer boundary or a thin serialization step over existing
query composition is deferred to the phase building each export.
Assignments are in [V1-SCOPE.md](V1-SCOPE.md) section 6.

## I. Backfill / Cutover — NOT IMPLEMENTED · REQUIRES BUSINESS RULE FREEZE

No backfill, cutover, or cutover-reconciliation capability exists. (The
one "Reconciliation" string in `src/bel/cli.py` is bank-statement
balance checking — opening + in − out = closing — and is unrelated.)

The private acceptance harness (`tests/private_acceptance/runner.py`)
already provides the right shape for a future cutover scenario: it emits
only `SCENARIO_ID: PASS|FAIL` to stdout and writes diagnostics under
`$BEL_PRIVATE_DATA_ROOT/reports/`. A cutover reconciliation scenario
should reuse that harness rather than invent a new reporting path.

Backfill is required before BEL can be declared System of Record, and
the cutover/backfill semantic rules — including how a backfilled fact
relates to fact correction (E) — are frozen in Phase 2D.1-R0 and built
in Phase 2D.1-R5. The final gate runs after Phase 2D.4.

## What is Domain evolution vs. Application/Web implementation

| Gap | Kind |
|---|---|
| ContractItem intake path | Domain-adjacent Application work + correction semantics (E) |
| Cross-contract query/listing composition | Application/Web implementation |
| Sales-side relationship semantics | Semantic freeze, then Application implementation |
| `Shipment`/`Export` domain + persistence + adapter + matching | Domain evolution |
| Fact correction/supersession semantics | Domain evolution |
| Outbound invoicing eligibility rule | Business rule freeze, then implementation |
| Task/Exception type coverage + lifecycle | Domain evolution |
| Data Product/export boundary | Application/Web implementation |
| Backfill / cutover reconciliation | New capability + private acceptance scenario |
| Period Close / Contract 360 query composition | Already exists, reusable |
