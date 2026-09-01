# Phase 2D.4-F0 Decisions — Unresolved Work Semantics & Product Freeze

Phase 2D.4-F0 is a **design / documentation / inventory** slice. It
freezes the semantics and product contract of the Exception & Task Center
(异常与任务中心) **before** the global Center surface is built. No Center UI
is implemented, no new exception producer is added, and no R009–R015 rule
is implemented or promoted. `docs/DOMAIN.md`, `docs/RULES.md`,
`docs/ARCHITECTURE.md` and the migration chain are untouched.

The central problem this document resolves:

> BEL already has several forms of authoritative unresolved work. They
> are **not** the same storage object and must not be falsely unified
> into one mutable "Task". The Center provides **one landing surface**,
> not **one fake domain object**.

The acceptance contract for the whole phase is frozen separately in
`docs/PHASE2D4-ACCEPTANCE.md`. This document records the judgment calls.

---

## 1. Inventory of current unresolved-work sources (code reality)

Every producer below is **implemented** at the base commit
(`035340b4c2f5737fab7855e99fdcb249e246f779`). The inventory is organized
by the four storage/derivation classes the Center must distinguish.

### A. Persisted `TaskException` — `task_exceptions` table

Storage: `TaskExceptionModel` (`src/bel/infrastructure/persistence/models.py`
lines 644–652); JSON `detail`, `String` status, no `UniqueConstraint` and
no dedupe constraint — dedupe is **per-producer and application-level**
where it exists (a scan of `ExceptionRepository.list_open()` filtered on
a detail-key subset), never a database guarantee. Some producers raise a
fresh task on every occurrence (§1A table notes).

| ExceptionType | Producer | `detail` scope-bearing keys | scope / anchor | auto-resolution |
|---|---|---|---|---|
| `BusinessKeyConflict` | `bel.application.import_contract_ledger` (`import_contract_ledger.py:172`); sales leg `bel.application.sales_contract_facts` (`sales_contract_facts.py:382`) | `contract_no`, `contract_ids` (list) / `sales_contract_id` | → one or more procurement `contract_id`s, or a `sales_contract_id` | none |
| `AllocationCapacityExceeded` | `bel.application.matching` M001 pass (`matching.py:242`) | `subject_type`, `subject_id`, `contract_id`, amounts | → `contract_id` | none |
| `ContractItemFactSuperseded` | `bel.application.contract_item_facts` (`contract_item_facts.py:516`) | `contract_item_id`, `superseded_revision_id`, `superseding_revision_id`, `dependents` | → `contract_item_id` | none |
| `ShipmentFactSuperseded` | `bel.application.shipment_facts` (`shipment_facts.py:662`) | `shipment_id`, `superseded_revision_id`, `superseding_revision_id`, `dependents` | → `shipment_id` | none |
| `ShipmentIdentityIncomplete` | `bel.application.shipment_facts` (`shipment_facts.py:325`) | `contract_id`, `execution_date`, `source_fragment_id`, `fields` | → `contract_id` only; **no Shipment anchor** | none (domain action exists — §5.1, `SUPPLY_FACT` §6 — task not auto-closed) |
| `ShipmentIdentityConflict` | `bel.application.shipment_facts` (`shipment_facts.py:405`) | `shipment_id` (existing anchor), both `source_fragment_id`s, both assertions | → `shipment_id` | none |
| `SalesContractIdentityIncomplete` | `bel.application.sales_contract_facts` (`sales_contract_facts.py:327`) | `source_fragment_id`, `missing_our_entity`, `missing_sales_contract_no` | **no anchor of any kind** | none |
| `SalesContractCustomerUnresolved` | `bel.application.sales_contract_facts` (`sales_contract_facts.py:240`) | `sales_contract_id` | → `sales_contract_id` | **yes** — customer SUPPLEMENT closes it (§5.2) |
| `ProcurementSalesLinkUnconfirmed` | `bel.application.procurement_sales_link` (`procurement_sales_link.py:342`) | `procurement_contract_id`, `sales_contract_id`, `source_fragment_id` | → both contract ids; **no link row** | none |
| `ProcurementSalesLinkMultipleScopes` | `bel.application.procurement_sales_link` (`procurement_sales_link.py:198`) | `procurement_contract_id`, `sales_contract_ids` (list) | → procurement Contract **plus all structured SalesContract ids in `sales_contract_ids`** (repeatable scopes: one authoritative task, multiple trace/navigation scopes) | none |
| `ProcurementSalesLinkCorrectionConflict` | `bel.application.procurement_sales_link` (`procurement_sales_link.py:221`) | `superseded_link_id`, `conflicting_source_fragment_id` | → link id | none |
| `BackfillIdentityIncomplete` | `bel.application.cutover_backfill` `_find_or_create_backfill_task` (`cutover_backfill.py:177`) | `fact_type`, `identity_key`, `+ extra` (`missing_contract_no`, `missing_counterparty`, …) | **no anchor id ever stored**; contract linkage, where any, lives only inside `identity_key` text | none |
| `BackfillIdentityAmbiguous` | same | `fact_type`, `identity_key`, `+ extra` (`matches`, `reason`, …) | no anchor id; `identity_key` names the ambiguous candidate set | none |
| `BackfillConflict` | same | `fact_type`, `identity_key`, `+ extra` (`reason`) | no anchor id | none |

Idempotency reality (per type):

- **No dedup — a fresh task is raised on every occurrence:**
  procurement `BusinessKeyConflict` (every import run that maps one
  `contract_no` to >1 id, `import_contract_ledger.py:169–178`),
  `AllocationCapacityExceeded` (every M001 pass that hits capacity,
  `matching.py:241–260`), `ContractItemFactSuperseded`
  (`contract_item_facts.py:512–526`), `ShipmentFactSuperseded`
  (`shipment_facts.py:658–672`).
- **Application-level dedup — an existing matching OPEN task is reused**
  (scan of `ExceptionRepository.list_open()`, each keyed on a detail-key
  subset): `ShipmentIdentityIncomplete` by `source_fragment_id`;
  `ShipmentIdentityConflict` by `(shipment_id, conflicting_source_fragment_id)`;
  `SalesContractIdentityIncomplete` by `source_fragment_id`;
  `SalesContractCustomerUnresolved` by `sales_contract_id`;
  sales-leg `BusinessKeyConflict` by
  `(sales_contract_id, conflicting_source_fragment_id)`;
  `ProcurementSalesLinkUnconfirmed` by
  `(procurement_contract_id, sales_contract_id, source_fragment_id)`;
  `ProcurementSalesLinkMultipleScopes` by `procurement_contract_id`;
  `ProcurementSalesLinkCorrectionConflict` by
  `(superseded_link_id, conflicting_source_fragment_id)`;
  the three backfill types by `(exception_type, identity_key)`
  (`cutover_backfill.py:169–173`).

Because `ShipmentIdentityIncomplete` / `ShipmentIdentityConflict` / the
backfill types are idempotent AND have no resolution path, a replay of
the same Evidence reuses the OPEN task instead of piling up duplicates.

Declared-but-unproduced: `PROCUREMENT_SALES_LINK_CONFLICT`
(`src/bel/domain/exception.py:72`) has **no creation site** in code today —
the link-conflict family actually produced is
UNCONFIRMED / MULTIPLE_SCOPES / CORRECTION_CONFLICT. It remains a valid
enum literal for a future producer; it is not a current source.

### B. Persisted `MatchCase` requiring human confirmation — `match_cases` + candidates

Storage: `MatchCaseModel` (`models.py:778–805`) + real candidate rows
(`MatchCandidateModel`, `SalesMatchCandidateModel` — not JSON). Status
`String`, no expiry/TTL, `REJECTED` is declared
(`src/bel/domain/matching.py:94`) but **never assigned anywhere**.

- **Procurement leg** (M001): `HUMAN_CONFIRMATION_REQUIRED` when an
  eligible subject has >1 candidate, or a unique candidate would exceed
  contract capacity (`matching.py:183`, `matching.py:227`). A persisted
  `AllocationCapacityExceeded` Task accompanies the latter.
- **Sales leg** (`MANUAL_SALES_SCOPE`): `propose_sales_invoice_match` /
  `propose_sales_payment_match` create `HUMAN_CONFIRMATION_REQUIRED`
  cases with one `SalesMatchCandidate` per proposed sales scope
  (`sales_matching.py:223–231`, `sales_matching.py:276–284`).
- **Resolution** (§5.1): `confirm_match` (procurement) /
  `confirm_sales_invoice_match` / `confirm_sales_payment_match` (sales)
  → `RESOLVED` + allocation(s) with `ConfirmationType.HUMAN_CONFIRMED`.
- `MatchCaseStatus.UNMATCHED` (0 candidates, `matching.py:165`) is a
  persisted matching state. It is **not** surfaced as Center unresolved
  work today (the existing projection filters to
  `HUMAN_CONFIRMATION_REQUIRED`). Whether UNMATCHED becomes a Center
  source is the R009/R010 question and stays deferred (§9).

### C. Computed blocker / unresolved Decision — Period Close

`bel.application.period_close` (`period_close.py`) is a stateless
preview: every Decision and blocker is recomputed from current facts for
the **requested period** on each run. Nothing is persisted (§7).

Computed blockers (`CloseBlocker`, `period_close.py:136–141`):
`ITEM_MATCH_REQUIRED_FOR_REVERSAL`,
`MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE`,
`MULTIPLE_ITEM_ALLOCATIONS_REQUIRE_EXPLICIT_SCOPE`,
`MISSING_ACCRUAL_BASIS` — plus contract-level `AccrualCandidate` with
`blocking_reason=MISSING_CONTRACT_ITEM_EVIDENCE`. `MISSING_ACCRUAL_BASIS`
is explicitly **not** the PROPOSED R011 (`period_close.py:20–21`).

### D. Management advisory — computed, never persisted

Phase 2D.3 advisories are in-memory DTOs recomputed on every evaluation;
**none is a `TaskException` and none is ever written**
(invoice-prep modules contain no repository `.add()`; the export module
states "export performs zero database writes",
`invoice_preparation_export.py:31–37`).

- Sales (`SalesInvoiceAdvisoryCode`): `SALES_INVOICE_AMOUNT_DEVIATION`,
  `SALES_INVOICE_CURRENCY_DEVIATION` (`sales_invoice_preparation.py:190,195`).
- Supplier (`SupplierRequestAdvisoryCode`):
  `PURCHASE_INVOICE_AMOUNT_DEVIATION`, `PURCHASE_INVOICE_CURRENCY_DEVIATION`,
  `PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION`,
  `MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT`,
  `PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS`,
  `SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED`
  (`supplier_invoice_request.py:204,210,214,193,198,226`).

Note on `MISSING_CONTRACT_GROSS_AMOUNT` (`SupplierRequestBlockerCode`,
`supplier_invoice_request.py:176`): this is a **hard missing-fact blocker
that drives the supplier decision `status`** — it is **not** a management
advisory and **not** in the advisory list above. It is a
**locally-computed, scope-scoped invoice-preparation blocker** (one per
procurement Contract, recomputed each evaluation, never persisted).
Frozen choice: it is **excluded** from the F1 Center taxonomy (§2) with
this rationale — the Center's computed source type `COMPUTED_BLOCKER` is
defined only for period-scoped Period Close blockers (§8); this blocker
is scope-scoped to the invoice-preparation surface, has no Center
resolution semantic, and the fix is a Contract fact action (`Contract`
`gross_amount` supplement). It stays on the Workbench's own read model,
the same way its advisory siblings do. It is not silently relabelled as
an advisory — it is a computed blocker that is deliberately outside the
Center's source types.

The docstring on `SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED` says it
"Disappears on recomputation once a confirmed PURCHASE invoice is
associated. No Task is persisted (a later stage may promote this to a
Task workflow)" (`supplier_invoice_request.py:223–226`). That promotion is
exactly what §9 gates behind a future business rule.

The Invoice Preparation Workbench already surfaces existing unresolved
work (from `_collect_unresolved_work`, see §10) as an
`UNRESOLVED_WORK` attention category, **structurally distinct** from
`MANAGEMENT_ADVISORY` (`invoice_preparation_export.py:68–70`) — the
advisory/Task distinction is already real in the product.

---

## 2. Source taxonomy (frozen)

The Center uses a neutral source vocabulary:

- `TASK_EXCEPTION` — a persisted `TaskException` (class A).
- `MATCH_CASE` — a persisted `MatchCase` in
  `HUMAN_CONFIRMATION_REQUIRED` (class B).
- `COMPUTED_BLOCKER` — a Period Close blocker/decision recomputed for a
  requested period (class C).

`ADVISORY` is **not** an unresolved-work source type. Class D advisories
(`SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED`,
`PURCHASE_INVOICE_AMOUNT_DEVIATION`,
`SALES_INVOICE_AMOUNT_DEVIATION`, and every other Phase 2D.3 advisory)
remain management advisories and are **not** automatically promoted into
Center Tasks. Promoting an advisory into a Task requires an explicit
future business rule (§9). The taxonomy is the vocabulary of the
projection, not a new storage object.

`COMPUTED_BLOCKER` is defined **only** for period-scoped Period Close
blockers/decisions (§8). Scope-scoped computed blockers from other
surfaces are excluded from the Center taxonomy by explicit choice:
`MISSING_CONTRACT_GROSS_AMOUNT` (§1D) stays on the Invoice Preparation
Workbench's own read model — it is a hard missing-fact blocker on that
surface, not a period-scoped Center source, and no Center action exists
for it. This does not relabel it as an advisory; it is a computed
blocker that is deliberately outside the Center's source types.

## 3. One neutral unresolved-work contract (frozen)

The future Center read model is a single presentation DTO,
`UnresolvedWorkItem`, with the following frozen shape. Every field is
optional; a source that has no value for a field leaves it **explicitly
`None`**.

| Field | Meaning | `TASK_EXCEPTION` | `MATCH_CASE` | `COMPUTED_BLOCKER` |
|---|---|---|---|---|
| `source_type` | one of the three taxonomy values | `TASK_EXCEPTION` | `MATCH_CASE` | `COMPUTED_BLOCKER` |
| `source_id` | the authoritative identity of the item (§4): a persisted source object's own id for `TASK_EXCEPTION` / `MATCH_CASE`; a **stable deterministic key** for `COMPUTED_BLOCKER` — no persisted object, no random UUID (§8) | task `id` | match-case `id` | deterministic key (§8) |
| `code` | machine code of the specific finding | `exception_type` | `match_method` (M001 / MANUAL_SALES_SCOPE) | `blocker_type` |
| `status` | current state | `ExceptionStatus` | `MatchCaseStatus` | "present for requested period" (constant) |
| `summary` | existing summary / deterministic presentation text | existing `summary` | derived | derived |
| `created_at` | creation time | `created_at` | `created_at` | `None` (period-scoped, not an event) |
| `scope_type` / `scope_id` | the structured business scope the item maps to (§4); **repeatable** — a `MATCH_CASE` with several candidates traces to each, exactly as `_collect_unresolved_work` does today | from `detail`/lookup | candidate scope(s) | contract / contract_item |
| `procurement_contract_id`, `sales_contract_id`, `invoice_id`, `payment_id`, `shipment_id`, `match_case_id` | trace/navigation ids | per producer `detail` | subject + candidate scopes | per blocker |
| `resolution_route` | guidance where the issue is corrected (§6) | §6 | `CONFIRM_MATCH` | `REVIEW_ONLY` |
| `provenance` | producer identifier where truthfully available — trace metadata, never identity (§3.1) | producer **module** where deterministically available; may be `None` when the persisted source does not distinguish the producer | producer **module** (`bel.application.matching` / `bel.application.sales_matching`) | `bel.application.period_close` |

Rules:
- **Never infer a Contract/scope by parsing `summary` text.** The existing
  `_collect_unresolved_work` (`contract_business_ledger.py:206–290`) is
  the canonical precedent: every association resolves through a structured
  FK/typed `detail` key or an explicit repository lookup by id — never
  `summary`.
- **Missing scope stays explicit `None`**, never guessed and never
  defaulted.
- **Computed items have no source object.** A `COMPUTED_BLOCKER` has
  `created_at = None`, a constant `status`, and a `source_id` that is a
  deterministic, collision-free key (§8) — it is never the id of a
  persisted row, because none exists.

### 3.1 Provenance contract (frozen)

`provenance` is auxiliary trace metadata: **where the unresolved-work
item's producer is truthfully available**. It is never part of the item's
identity (identity remains `(source_type, source_id)`, §4) and never
changes what an item is.

- **Module-level provenance is sufficient and is the normal F1/F2
  contract** — e.g. `bel.application.matching`,
  `bel.application.shipment_facts`,
  `bel.application.sales_contract_facts`,
  `bel.application.procurement_sales_link`,
  `bel.application.cutover_backfill`,
  `bel.application.period_close`.
- **Function-level provenance MAY be used only where it is explicitly
  available, unambiguous, and intentionally stable.** It is not
  manufactured: a persisted `TaskException` row does not store which
  producer function created it.
- **`provenance` MAY be `None`** where the authoritative persisted source
  does not distinguish the producer. Example: `BusinessKeyConflict` can be
  produced by either `bel.application.import_contract_ledger` or
  `bel.application.sales_contract_facts`, and the persisted row does not
  record which — F1 truthfully leaves provenance `None` for such rows
  rather than guessing.
- **Never infer provenance from `summary` text, and never guess a
  producer function.**
- `COMPUTED_BLOCKER` provenance is always the constant
  `bel.application.period_close` (§8).

## 4. Scope / identity rule (frozen)

The global Center **does not require every item to map to a Contract** —
a deliberate departure from the Contract Business Ledger's
`_collect_unresolved_work`, which is contract-scoped by construction and
therefore drops genuinely unmappable items (its comment on
`SalesContractIdentityIncomplete`, `contract_business_ledger.py:269–270`).

Frozen identity:

> **global unresolved-work identity = `(source_type, source_id)`**

Business scope identifiers (`procurement_contract_id`,
`sales_contract_id`, `invoice_id`, …) are **trace / navigation fields,
not the identity of the unresolved-work item**. No text parsing.

The identity formula holds for all three source types. For
`TASK_EXCEPTION` and `MATCH_CASE`, `source_id` is the persisted object's
own UUID. For `COMPUTED_BLOCKER` there is **no persisted source object**:
`source_id` is a deterministic normalized key (§8) that is stable across
recomputes and runs, free of random UUIDs, and collision-free — the
identity formula `(source_type, source_id)` still identifies the item
within a given Center view.

Items that must remain visible globally despite having **no** Contract
anchor (each already exists in code):

- `SalesContractIdentityIncomplete` — no anchor is created
  (`sales_contract_facts.py:327`); identity is the task id.
- Backfill identity issues — a backfill task's `detail` never stores an
  anchor id; `Invoice`, `Payment` and `SalesContract` backfill tasks have
  no contract reference at all, and the reconciliation snapshot keys an
  OPEN backfill task by **its own persisted id** — "the task's OWN
  canonical identity, not a stand-in for a missing business identity"
  (`cutover_reconciliation.py:406–415`).
- `ShipmentIdentityIncomplete` — no Shipment anchor exists
  (`shipment_facts.py:325`); scope is `contract_id` only.

## 5. Lifecycle — no generic RESOLVE (frozen)

Frozen lifecycle principle:

```
unresolved work
   -> user / future Agent performs an ALLOWED DOMAIN ACTION
   -> underlying Fact / relationship / confirmation changes
   -> producer condition is recomputed
   -> unresolved work disappears or source object becomes resolved
```

The Center is **not** allowed to implement a universal
`POST /exceptions/{id}/resolve` that blindly sets `status = RESOLVED`. A
generic "mark resolved" button is forbidden for V1 unless a source
already has a specifically frozen resolution semantic. The Center is a
read model; it never writes status.

### 5.1 Existing producer-specific resolutions that are preserved

- **MatchCase** `HUMAN_CONFIRMATION_REQUIRED` → `RESOLVED`: the only
  resolution is `confirm_match` / `confirm_sales_invoice_match` /
  `confirm_sales_payment_match`, each of which also writes the
  authoritative allocation (`matching.py:410–517`, `sales_matching.py:312–512`).
- **`SalesContractCustomerUnresolved`** → `RESOLVED`: a SUPPLEMENT that
  fills in `customer` closes the OPEN task via
  `ExceptionRepository.update_status`
  (`sales_contract_facts.py:250–260`, `:459–460`). This is the **only**
  automatic `TaskException` resolution in the codebase today.
- **Shipment identity confirmation**: `create_shipment_fact` with
  `identity_confirmed=True` creates the Shipment anchor for a
  `SHIPMENT_IDENTITY_INCOMPLETE` situation
  (`shipment_facts.py:346–376`). The domain action exists and is a
  legitimate fix; it does **not** auto-close the Task.

### 5.2 What is NOT generalized

The `SalesContractCustomerUnresolved` supplement-close loop is
**not** a template for a universal workflow. No other TaskException type
has an automatic transition, and none is invented here. A
`ContractItemFactSuperseded` / `ShipmentFactSuperseded` task is raised so
a human decides what, if anything, must be redone — the correction that
created it is not itself a "resolve". Backfill tasks stay OPEN and block
reconciliation until a human resolves the underlying identity
(`cutover_reconciliation.py:402–415`).

## 6. Resolution route (frozen presentation concept)

`resolution_route` tells the user **where** the underlying issue should
be corrected/confirmed. It is navigation/action guidance, not business
truth, and it is defined only for **real, currently available domain
actions**. No generic enum is invented.

Frozen route vocabulary:

| route | meaning | items carrying it today | real capability |
|---|---|---|---|
| `CONFIRM_MATCH` | confirm the match proposal | `MATCH_CASE` (procurement + sales) | `bel match confirm` / `bel sales-match invoice confirm` / `bel sales-match payment confirm` |
| `CONFIRM_RELATIONSHIP` | supplement/confirm the customer relationship | `SalesContractCustomerUnresolved` | `bel sales-contract supplement ... --customer` (auto-closes) |
| `SUPPLY_FACT` | supply/confirm the missing fact that would create an anchor | `ShipmentIdentityIncomplete` | `bel shipment create --confirm-incomplete-identity` |
| `REVIEW_ONLY` | no Center action can resolve; review underlying facts | every other source | none safe today |

Any source whose resolution cannot be expressed by a safe existing action
gets `REVIEW_ONLY` — the default, not an error. A new route may be added
in a later slice only when a corresponding real, safe domain action
exists; the Center may then deep-link to it. `CONFIRM_MATCH` is the only
route whose action fully resolves the source object today; the other two
name a fix that changes the underlying facts (only the customer route
also closes the Task).

## 7. Persisted vs computed (frozen, visibly)

The three sources keep separate storage semantics; the Center only
aggregates them in a read projection. Nothing is copied into a new
storage object.

- `TASK_EXCEPTION` — has a storage lifecycle (`OPEN`/`RESOLVED`).
- `MATCH_CASE` — independent persisted matching lifecycle.
- `COMPUTED_BLOCKER` — computed for a requested period; **never
  persisted merely to make the Center easier**.

**Do not create a new `unresolved_work` table** to copy the three
together (acceptance invariant, `docs/PHASE2D4-ACCEPTANCE.md`).

## 8. Period Close blockers (frozen product answer)

Period Close blockers are **period-dependent**. Frozen V1 contract:

- The Center's **global view** (no period) aggregates persisted
  unresolved sources only: `TASK_EXCEPTION` + `MATCH_CASE` (§3).
- An **optional period parameter** adds the currently computed
  `COMPUTED_BLOCKER`s for that period, e.g. `/exceptions?period=2026-08`.
- Without a requested period there is **no** timeless global set of
  period-close blockers; the Center does not pretend one exists.
- A blocker snapshot is **never persisted**; the next recompute reflects
  the current facts.

The `source_id` of a computed blocker is a **stable, deterministic,
collision-free normalized key** — never a random UUID and never a
persisted id. It covers, in fixed canonical order: the `period`, the
`blocker_type`, and **every scope id the blocker carries** — for
`CloseBlocker` that is `contract_id`, and where present `contract_item_id`,
`accrual_id`, or the full `accrual_ids` tuple ordered canonically (sorted
by `str(uuid)`, since `MULTIPLE_OPEN_ACCRUALS_REQUIRE_EXPLICIT_SCOPE`
names several). The key is a pure function of these fields, so the same
facts recompute the same item across runs and views; it exists only
inside the Center projection and is never stored (§7). Consistent with
§4: computed blockers are ephemeral and are never the object of a
resolve action.

## 9. Duplication / correlation (frozen)

The Center does **not** deduplicate different authoritative source
objects merely because their text looks similar. No fuzzy merge, no
summary-text equality, no Agent semantic deduplication. A `TaskException`,
a `MatchCase` and a Period Close blocker may refer to the same business
context while remaining separate Center items. If future correlation is
useful, it is projection metadata only — it never changes identity.

Idempotency that *does* exist is producer-level and structural (a
producer reusing an existing OPEN task by a detail-key subset, §1) — the
Center neither adds nor removes it.

## 10. Rule producer boundary (frozen)

R009 `InvoiceUnmatched`, R010 `PaymentUnmatched`, R011 `EvidenceMissing`,
R012 `AmountMismatch` — and R013–R015 — remain **PROPOSED** in
`docs/RULES.md`. Phase 2D.4-F0:

- does **not** promote any of them to CONFIRMED;
- does **not** implement them;
- does **not** create `TaskException` rows for them.

The Exception & Task Center can ship over the **existing** authoritative
unresolved work (§1) without waiting for R009–R012. Additional producers
are added rule-by-rule, each after a business rule freeze.

`_collect_unresolved_work` (`contract_business_ledger.py:206–290`),
already reused by the Contract Business Ledger and the Invoice
Preparation Workbench (`invoice_preparation.py:233`), is the **precedent
for the structured scope-resolution technique** (§3's first rule) — it is
**not** the Center projection. It is contract-scoped by construction and
drops genuinely unmappable items (e.g. `SalesContractIdentityIncomplete`,
`contract_business_ledger.py:269–270`), it carries no Period Close
blockers, and its DTO has no status/scope fields. F1 must therefore build
a **new global aggregation** over the full §1 inventory that (a) preserves
unmappable items (§4), (b) keeps `(source_type, source_id)` identity, and
(c) adds period-scoped computed blockers when a period is requested (§8) —
reusing `_collect_unresolved_work`'s structured-resolution discipline,
never its contract-scoped drop behavior.

## 11. Product shape for F1 / F2 (frozen)

- **F1** — read-only Exception & Task Center: the global neutral
  unresolved-work projection (`UnresolvedWorkItem`, §3), filters (§12),
  Web surface. No writes.
- **F2** — Exception & Task Data Product: the **same** neutral projection,
  XLSX / CSV, Web + CLI (mirroring the Contract Ledger and Invoice
  Preparation export pattern: one Application projection shared
  byte-for-byte by both transports).

No new generic Task workflow is required to complete F1/F2. Source-specific
action links may be added later **only** where a safe, frozen resolution
semantic exists (§6) — never as a generic "resolve" control.

## 12. Center filters (frozen)

Minimal useful F1 filters:

- `status` / open-only
- `source_type` (`TASK_EXCEPTION` / `MATCH_CASE` / `COMPUTED_BLOCKER`)
- `code` / type (e.g. `exception_type`, `blocker_type`)
- procurement contract
- sales contract
- `period` (enables §8 computed blockers)

Not added: `priority`, `assignee`, `SLA`, `due date`, `department`,
`workflow state` — no existing Fact supports them. This is not Jira.

## 13. F0 boundary (frozen)

F0 requires:

- **no schema migration**;
- **no Domain model change** unless a documentation/code-reality bug
  absolutely requires one (none was found — this slice is documentation
  only);
- no generic workflow abstraction prebuilt;
- no Center UI;
- no new exception producers;
- no R009–R012 implementation.

The deliverables are `docs/PHASE2D4-DECISIONS.md` and
`docs/PHASE2D4-ACCEPTANCE.md`, plus the code-reality corrections in
`ROADMAP.md` (Phase 2D.4) and `docs/V1-SCOPE.md` (section 5.2).

## 14. Cross-checks

- All inventory claims verified against source at the stated line numbers.
- No R009–R015 provenance changed.
- No generic `RESOLVE` introduced.
- No new storage table.
- No schema/migration change.
- Private-data policy: this document contains no source-derived values.
