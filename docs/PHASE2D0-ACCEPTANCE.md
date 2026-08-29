# Phase 2D.0 Acceptance

Public acceptance criteria for Phase 2D.0 (V1 Product Rebaseline).
Documentation-only phase — there is no application, database, or web
behavior to test. Acceptance is document consistency, **consistency with
actual code reality**, boundary preservation, and privacy, verified by
reading the diff and running the existing tooling to confirm it is
untouched by this phase.

## How to run

```bash
.venv/bin/pytest                       # public suite must still pass, unchanged by this phase
.venv/bin/python tools/privacy_scan.py --tracked
.venv/bin/python tools/privacy_scan.py --history
.venv/bin/python tools/privacy_scan.py --staged
.venv/bin/python tools/privacy_scan.py --untracked
git status
git diff --stat
git diff --name-only
git diff v0.1.1 --
git ls-files --others --exclude-standard      # the two Phase 2D.0 docs are UNTRACKED
```

Two notes on running these:

- There is no `python` on the reference machine; use `.venv/bin/python`
  as above.
- **`git diff` does not show the two new Phase 2D.0 documents.**
  `docs/PHASE2D0-DECISIONS.md` and `docs/PHASE2D0-ACCEPTANCE.md` are
  untracked in this phase (nothing is committed), so every diff-based
  check below must be read together with
  `git ls-files --others --exclude-standard`, and the untracked files
  reviewed directly. `privacy_scan.py --untracked` covers them for
  privacy; diff integrity and content review do not, unless the
  untracked listing is checked explicitly.

## Checklist

### 1. CODE REALITY CONSISTENCY

The central gate for this phase: **no changed document may overstate
what the code does.** Each claim below is re-verifiable from the source
location the documents cite.

- [ ] **Sales-side matching.** No document says or implies that sales
      invoices or incoming receipts are associated with a `Contract`
      today. [V1-SCOPE.md](V1-SCOPE.md) section 3.1 and
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) item C both state
      that `match_invoices` filters to `InvoiceDirection.PURCHASE` and
      `match_payments` filters to `PaymentDirection.OUT`, and that the
      accurate framing is "sales-side raw facts can be imported; the
      Contract association / matching pipeline is not connected."
- [ ] No document describes closing that gap as "parameterizing the
      existing matching pass for SALES/IN". Both documents state the
      purchase side keys on `Contract.counterparty` while the sales side
      must key on `Contract.buyer`, and that the governing amount and
      relationship are unsettled.
- [ ] **ContractItem intake.** No document claims `ContractItem` has an
      ordinary business intake path. Both state that the contract-ledger
      importer reports `contract_items_created=0` unconditionally and
      that the Close Fact Pack importer is the only construction path.
- [ ] **Shipment / Export.** No document claims this is largely present
      or needs only a page. Both state there is no domain object,
      persistence model, adapter, or matching pipeline anywhere under
      `src/`, and that it is the only V1 match type with zero
      implementation.
- [ ] The export-contract-number finding is stated as a code fact only —
      the contract-ledger importer reads that column solely to compute
      `missing_export_contract_no` and never promotes it to a Fact or an
      association. No private dataset result, count, ratio, or coverage
      figure accompanies it.
- [ ] **Task producers.** No document claims R009–R012 are official
      rules, and none claims Phase 2D.4 depends entirely on them. Both
      state that `BUSINESS_KEY_CONFLICT`,
      `ALLOCATION_CAPACITY_EXCEEDED`, `MatchCaseStatus.HUMAN_CONFIRMATION_REQUIRED`
      and period-close blockers are authoritative unresolved work that
      exists today.
- [ ] **Persisted vs. recomputed unresolved work.** No document claims
      Period Close blockers are persisted. `CloseBlocker` is a
      recomputed read-only preview DTO —
      `build_period_close_preview` runs under `session.no_autoflush` and
      writes nothing — while `TaskException`
      (`BUSINESS_KEY_CONFLICT`, `ALLOCATION_CAPACITY_EXCEEDED`) and
      `MatchCase` in `HUMAN_CONFIRMATION_REQUIRED` are persisted rows.
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) item G separates
      the two forms and states no `TaskException` row is created for a
      blocker.
- [ ] **Data Products.** No document claims any export exists. Both
      state no Application service or CLI command produces a file.
- [ ] **Backfill / cutover.** No document claims a backfill, cutover, or
      cutover-reconciliation capability exists.
- [ ] No changed document uses vague status language ("basically
      supported", "foundation exists", "to be refined later") in place
      of an implementation status.
- [ ] Every capability in
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md)'s Code Reality
      section carries **both** an Implementation Status
      (`IMPLEMENTED` / `PARTIALLY IMPLEMENTED` / `NOT IMPLEMENTED`) and
      a Decision Status (`FROZEN` / `DEFERRED IMPLEMENTATION DECISION` /
      `REQUIRES BUSINESS RULE FREEZE` /
      `REQUIRES RELATIONSHIP / SEMANTIC FREEZE`), and the two are not
      collapsed into a single label. Items A–I are all present.

### 2. CRITICAL PATH CONSISTENCY

- [ ] [ROADMAP.md](../ROADMAP.md) sequences `ContractItem` Fact
      Maintenance (2D.1-R1), Shipment/Export (2D.1-R2), and the
      sales-side association foundation (2D.1-R3) **before** the
      Contract Business Ledger (2D.1-R4) and before Outbound Invoicing
      (2D.3).
- [ ] Phase 2D.1-R0 (Business Semantics Freeze) precedes every 2D.1
      implementation round and covers fact correction/supersession,
      sales-side relationship/matching semantics, Shipment/Export
      minimal semantics, and cutover/backfill semantic rules.
- [ ] [V1-SCOPE.md](V1-SCOPE.md) section 2.2 states `ContractItem`
      completeness is a first-stage critical path and gives the R007
      reason, and describes the intake as
      Evidence → Fact → deterministic recompute rather than CRUD.
- [ ] [V1-SCOPE.md](V1-SCOPE.md) section 5 item 1 forbids the Contract
      Business Ledger from displaying fabricated data for facts BEL does
      not hold, and names its prerequisites.
- [ ] `Export completed → automatically invoice eligible` is **not**
      frozen anywhere; export execution is described only as a candidate
      input to a future eligibility rule.

### 3. CUTOVER STRATEGY

- [ ] The first-stage Definition of Done includes backfill, private
      reconciliation, and a cutover gate — not only shipped surfaces.
      [V1-SCOPE.md](V1-SCOPE.md) section 7 and
      [ROADMAP.md](../ROADMAP.md) agree.
- [ ] Phase 2D.1-R5 is described as cutover **infrastructure /
      rehearsal** (backfill mechanism, Cutover Baseline, reconciliation
      harness, verified first against the contract-execution fact
      layer), and is explicitly **not** the final gate.
- [ ] The `FIRST-STAGE CUTOVER GATE` sits **after** Phase 2D.4 in
      [ROADMAP.md](../ROADMAP.md), with its dependency on the 2D.2
      accrual Data Product, the 2D.3 invoicing judgment/Data Product,
      and the 2D.4 exception loop stated.
- [ ] The gate's conditions are listed: required fact flows operational,
      first-stage surfaces operational, Data Products operational,
      backfill complete, private cutover reconciliation PASS, unresolved
      cutover discrepancy = 0.
- [ ] Passing the gate is what makes BEL the System of Record and demotes
      legacy Excel to a read-only reference / Data Product.

### 4. LEGACY IS NOT GOLDEN TRUTH

- [ ] No document defines cutover acceptance as `BEL result == current
      Excel value`.
- [ ] [V1-SCOPE.md](V1-SCOPE.md) section 7.1 states the legacy
      spreadsheet may contain manual errors, stale state, incomplete
      information, contradictory information, and results with no
      Evidence, and therefore is not Golden Truth.
- [ ] The Cutover Baseline is defined as legacy Excel **plus** source
      Evidence **plus** business-confirmed interpretation.
- [ ] Reconciliation distinguishes at least `MATCH`,
      `BEL_CORRECTED_LEGACY`, and `UNRESOLVED`, and the bar is
      `UNRESOLVED = 0` (every discrepancy adjudicated), not universal
      agreement with Excel.
- [ ] Public runner output is limited to a scenario ID and PASS/FAIL
      (e.g. `P2D_CUTOVER_RECONCILIATION: PASS`); values, counts, names,
      amounts, records and mismatch details go only to
      `$BEL_PRIVATE_DATA_ROOT/reports/`.
- [ ] The Cutover Baseline is located at
      `$BEL_PRIVATE_DATA_ROOT/<period>/expected/`, matching the existing
      layout in [PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) — and
      `PRIVATE-DATA-POLICY.md` itself is unchanged by this phase.

### 5. RULE FREEZE BOUNDARIES

- [ ] Outbound invoicing eligibility is marked
      `REQUIRES BUSINESS RULE FREEZE` in
      [V1-SCOPE.md](V1-SCOPE.md) section 5.1,
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) item F, and
      [ROADMAP.md](../ROADMAP.md)'s Phase 2D.3 entry; none of them
      states or implies an eligibility rule has been decided.
- [ ] The four questions the business must answer before 2D.3 are named:
      *not eligible*, *ready for invoice preparation*, *already
      invoiced*, *blocked / unresolved*.
- [ ] "A Sales Invoice Fact existing is not an invoicing eligibility
      Decision" appears explicitly.
- [ ] R009–R012 are marked as requiring per-rule business freeze before
      becoming authoritative exception producers, in both
      [V1-SCOPE.md](V1-SCOPE.md) section 5.2 and
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) item G.
- [ ] Sales-side relationship semantics are marked
      `REQUIRES RELATIONSHIP / SEMANTIC FREEZE` and assigned to Phase
      2D.1-R0 before implementation in 2D.1-R3.

### 6. Product scope consistency

- [ ] [V1-SCOPE.md](V1-SCOPE.md) states the "replace the Excel contract
      ledger as System of Record" Definition of Done (section 0) and is
      the single source for it; `README.md` and `ROADMAP.md` reference
      it rather than restating it in conflicting words.
- [ ] Section 0's six Definition-of-Done capabilities each carry a
      truthful status. Capability 2 (period-close judgment at any point
      in time) is marked as an implemented capability whose first-stage
      coverage is **not** yet cutover-complete — it is not presented as
      a met Definition of Done.
- [ ] The five core work surfaces in [V1-SCOPE.md](V1-SCOPE.md) section
      5 (Contract Business Ledger, Contract 360, Period-Close Workbench,
      Outbound Invoicing Workbench, Exception & Task Center) match
      [ROADMAP.md](../ROADMAP.md) and `README.md`, and each carries its
      real implementation status.
- [ ] Business Cockpit is deferred, not cancelled, and sequenced after
      cutover — consistent across
      [V1-SCOPE.md](V1-SCOPE.md) section 8,
      [ROADMAP.md](../ROADMAP.md), and
      [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md).
- [ ] Business Fact Maintenance is a capability underlying the five
      surfaces, not a sixth page.
- [ ] Excel is stated consistently as import / export / cutover-backfill
      source / downstream handoff / human-readable data product, and
      never as System of Record.
- [ ] All four Data Products are assigned a phase (Contract Business
      Ledger export → 2D.1-R4, Period Close export → 2D.2, Invoice
      Preparation export → 2D.3, Exception export → 2D.4).
- [ ] [V1-SCOPE.md](V1-SCOPE.md) section 6.1 states both halves of the
      tax-rebate boundary: V1 **may** maintain canonical business facts
      a downstream rebate consumer would read, and **does not** add a
      rebate declaration status, calculation flow, tax-authority
      interface, or RebateX vocabulary to the Business Core.
- [ ] Section 1's evidence-source list is labelled a scope freeze, not
      an implementation-status claim, with actual importer status given
      in section 2.1.

### 7. Architecture boundary consistency

- [ ] [docs/ARCHITECTURE.md](ARCHITECTURE.md) is byte-identical to
      before this phase (`git diff docs/ARCHITECTURE.md` is empty).
- [ ] No changed document contradicts A01–A05: none describes an Agent
      writing directly to storage, a prompt acting as a business rule,
      an AI making a final accrual/reversal/duplicate/close-status call,
      finance/tax/ERP vocabulary entering the Business Core's domain
      language, or a rule silently guessing instead of producing a Task.
- [ ] The A02 object grouping in [V1-SCOPE.md](V1-SCOPE.md) section 2
      does not call `Evidence`, `BusinessEvent`, or `Task / Exception` a
      Fact: `Evidence` is immutable source material, `BusinessEvent` is
      history, `Task / Exception` is an unresolved-work projection that
      is never a source of business truth, and `Accrual` is a derived
      record whose `created_from` traces to confirmed Facts.
- [ ] Section 2.1 preserves "users maintain Facts, never derived state",
      naming 待暂估/待红冲/可开票/已完成/已匹配/异常已解决 as computed,
      and states that users legitimately supply Evidence, Facts, human
      confirmations, and lawful corrections.
- [ ] Section 2.4 preserves `Evidence` immutability and forbids
      implementing correction by overwriting Evidence or silently
      mutating a canonical Fact without an audit trail.
- [ ] [V1-SCOPE.md](V1-SCOPE.md) section 5 item 3 preserves the Period
      Close Workbench's Phase 2C.2 semantics: a read-only rehearsal
      keeping the four-layer Fact / Current State / Projected Decision /
      Blocker distinction, answerable at period end **or any other point
      in time**.
- [ ] Section 4 and section 5 item 3 both state BEL produces no
      accounting voucher, debit/credit entry, finance subject code,
      posting, or tax-accounting logic.
- [ ] The non-goals section still excludes Agent/LLM integration, MCP,
      accounting entries, tax-rebate logic, ERP concepts, generic
      workflow DSL, event sourcing, microservice split, and
      RBAC/enterprise IAM.
- [ ] [ROADMAP.md](../ROADMAP.md)'s post-cutover order is Business
      Cockpit → Application Tool Contract → Agent Runtime → runtime
      substitutability → MCP/ecosystem.
- [ ] [PHASE2D0-DECISIONS.md](PHASE2D0-DECISIONS.md) explains that
      `ARCHITECTURE.md`'s "V1 first implementation: Pi Agent Core" label
      names an Agent Runtime milestone, not the product V1 scope.

### 8. No code / rule / domain change

- [ ] `git diff --name-only` against `2c06e57` touches only `README.md`,
      `ROADMAP.md`, `CLAUDE.md`, `AGENTS.md`, `docs/V1-SCOPE.md`,
      `docs/PHASE2D0-DECISIONS.md`, `docs/PHASE2D0-ACCEPTANCE.md`. No
      path under `src/`, `migrations/`, `tests/`, or `fixtures/`
      appears.
- [ ] `git diff docs/RULES.md`, `git diff docs/DOMAIN.md`,
      `git diff docs/ARCHITECTURE.md`,
      `git diff src/bel/application/period_close.py`,
      `git diff src/bel/domain/`, and `git diff migrations/` are all
      empty.
- [ ] Existing Phase 1 / 2A / 2B / 2C / 2C.2 decision and acceptance
      documents are unchanged — history is not retroactively rewritten.
- [ ] No changed document adds a rule number, restates an existing
      rule's trigger condition differently, or promotes a `PROPOSED`
      rule to `CONFIRMED`.
- [ ] `.venv/bin/pytest` passes with the same results as on `2c06e57`.

### 9. Privacy

- [ ] `--tracked`, `--history`, `--staged`, `--untracked` privacy scans
      each report zero findings.
- [ ] The scans printing `no local denylist configured
      (BEL_PRIVACY_DENYLIST unset) — Generic Guard only` is the
      documented default, not a failure: the denylist is optional and
      kept outside this repository (`tools/privacy_scan.py`'s docstring
      — "Skipped silently if unset — never required for CI"), and
      [PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) states CI runs
      the tracked-file and Generic Guard checks independent of any local
      denylist.
- [ ] No changed document contains a real company/counterparty name,
      contract number, amount, quantity, record count, coverage ratio,
      or any other private-derived value (P03/P04). Every example is
      generic business semantics or independently synthetic.
- [ ] Statements about `ContractItem` intake and the export-contract
      column describe **code capability only** — never how complete, how
      frequent, or how covered any private dataset is.
- [ ] The cutover design keeps private material outside the repository:
      Baseline under `$BEL_PRIVATE_DATA_ROOT/<period>/expected/`,
      diagnostics under `$BEL_PRIVATE_DATA_ROOT/reports/`, public output
      limited to scenario ID + PASS/FAIL (P06).

### 10. Documentation cross-reference consistency

- [ ] Every cross-link in the two `docs/PHASE2D0-*.md` files resolves to
      a real file at the stated relative path.
- [ ] Section references used by other documents match
      [V1-SCOPE.md](V1-SCOPE.md)'s actual numbering (0, 1, 2, 2.1–2.4,
      3, 3.1, 4, 5, 5.1, 5.2, 6, 6.1, 7, 7.1, 7.2, 8).
- [ ] `README.md`'s Documentation section links the Phase 2C.2 and Phase
      2D.0 decision/acceptance documents.
- [ ] `README.md` describes only shipped capability as current, and
      states that anything absent from "Current capabilities" is not
      built.
- [ ] `CLAUDE.md` and `AGENTS.md`'s "Hard rules" sections (rules 1–7)
      are identical to each other, and no privacy rule was weakened.
- [ ] Rule 7 in both files states a durable guard against changing
      frozen business rules, Domain semantics, or architecture
      principles outside an explicit task request — it no longer names a
      specific past phase, and the rest of the agent instructions were
      not restructured.

## Explicitly out of scope (this phase)

Implementing anything named above: ContractItem Fact Maintenance,
Shipment/Export, sales-side association, the Contract Business Ledger,
fact correction/supersession semantics, backfill/cutover reconciliation,
any Data Product export, the Outbound Invoicing Workbench, any invoicing
eligibility rule, the Exception & Task Center or any Task lifecycle
extension, Business Cockpit, and Agent Runtime. All are named as future
work, not built here.
