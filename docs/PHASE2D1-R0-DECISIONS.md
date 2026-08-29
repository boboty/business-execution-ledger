# Phase 2D.1-R0 Decisions — Business Semantics Freeze

Phase 2D.1-R0 freezes the four groups of business semantics that Phase
2D.1-R1/R2/R3a/R3b and 2D.1-R5 must implement against.

**Design and documentation only:** no `src/`, `migrations/`, `tests/`,
or `fixtures/` file is changed, no schema is altered, no migration is
written, and no Rule Engine logic is touched.
`docs/ARCHITECTURE.md` and `docs/RULES.md` are unchanged — A01–A05 and
R001–R015 are untouched.

**`docs/DOMAIN.md` semantics changed under approved SCR-2D1R0-001**
(see the SPEC_CHANGE_REQUEST section): two objects were added,
`SalesContract` and `ProcurementSalesLink`, covering business meaning
and relationships only. **No implementation or schema was changed** —
the additions describe what those objects must be able to express, not
how they are stored, exactly as the rest of that document does.

## Baseline

This round is designed against **commit
`9d22fb783a3b476efa0a14160d33d628bf54594c`** ("phase D"), the commit
that carries the completed Phase 2D.0 rebaseline and is in sync with
`origin/main`. The release owner tags this commit `v0.1.2`; at the time
of writing, that tag had not yet been created, so the commit hash above
is the authoritative baseline reference for this document and for
[PHASE2D1-R0-ACCEPTANCE.md](PHASE2D1-R0-ACCEPTANCE.md).

Each freeze is tagged:

- **FROZEN** — settled by this document; R1/R2/R3/R5 implement it as written
- **DEFERRED IMPLEMENTATION DECISION** — shape settled, mechanism left to the implementing round
- **REQUIRES BUSINESS RULE FREEZE** — needs a business-owner decision before it can be designed

## Code baseline this design is built on

| Observation | Source |
|---|---|
| `EvidenceDocument` / `EvidenceFragment` are `@dataclass(frozen=True)`; `raw_data` documented as never overwritten | `src/bel/domain/evidence.py` |
| `FragmentKind.MANUAL_FACT` already exists for human confirmations | `src/bel/domain/evidence.py` |
| Reversal is additive, balance derived — "forbids mutating the Accrual with a bare `reversed_amount += x` because that loses history" | `src/bel/domain/accrual.py` |
| One shared predicate resolves derived state; rules never re-implement it | `get_accrual_balance` / `is_open_accrual` |
| Repositories already assemble Domain dataclasses from models — `_contract_item_to_domain` | `src/bel/infrastructure/persistence/repositories.py` |
| Import idempotency is **file-content level only** — "Idempotent on file content" via `sha256` | `src/bel/application/import_contract_ledger.py` |
| `ContractItem` unique on `(contract_id, source_item_key)`; `Invoice.external_invoice_key` unique; `EvidenceDocument.sha256` unique | `models.py` |
| `Contract.contract_no` deliberately **not** unique → `BusinessKeyConflict` | `models.py`, R004 |
| `Payment` has **no** uniqueness constraint; `bank_reference` nullable; no source-account field | `models.py` |
| `CostRecognitionFactModel` carries only `contract_id`, `recognition_date`, `basis`, `source_fragment_id` | `models.py` |
| Fact Pack resolves a contract by `(contract_no, counterparty)`, rejecting on 0 or >1 — "rejecting, not guessing" | `_ContractResolver`, `import_close_facts.py` |
| Ledger party columns `COUNTERPARTY_HEADER = "卖方"` / `BUYER_HEADER = "买方"`, both imported | `src/bel/adapters/excel/contract_ledger.py` |
| `PaymentAllocation` carries `payment_id` + `contract_id` + amount, no direction assumption | `models.py`, `src/bel/domain/matching.py` |
| Matching filters to `PURCHASE` invoices and `OUT` payments | `src/bel/application/matching.py` |
| `CostRecognitionBasis.EXPORT_EXECUTION_CONFIRMED` exists, but "Phase 2B does NOT decide which business behavior means cost recognition — the Fact Pack states it explicitly and the Rule Engine only consumes the fact" | `src/bel/domain/accrual.py` |
| `HistoricalAccrualFact` is a go-live business-state fact "NOT claimed to have been computed by this system" | `src/bel/domain/accrual.py` |
| Period close is a strict read-only preview under `session.no_autoflush`, no writes | `src/bel/application/period_close.py` |

Two findings shape several decisions below:

1. **`current_source_fragment_id` is not a supersession pointer.**
   `Contract` and `ContractItem` both carry that field, but no code path
   updates it after creation. The `current_` prefix names an intent that
   was never implemented; nothing may assume it tracks a latest version.
2. **Re-importing a *revised* workbook is not idempotent.** Idempotency
   keys on file bytes, so a corrected ledger re-creates every contract as
   a new row, surfacing as `BusinessKeyConflict`. Fact correction
   (section 1) and backfill idempotency (section 4) are two faces of one
   missing capability: business-identity resolution.

---

# 1. Fact Correction / Supersession

## 1.1 Three situations that never share one operation — FROZEN

| Case | Situation | Treatment |
|---|---|---|
| **A. Supplement** | A previously unknown attribute becomes known | New revision, `SUPPLEMENT` |
| **B. Correction** | A previously asserted value was wrong | New revision, `CORRECTION` |
| **C. Business progression** | A new business event happened | **Not a correction.** A new, independent Fact of its own type |

Governing rule:

> **Correction applies only to a Fact that was asserted. It never applies
> to derived state.**

"No invoice received" was never a stored Fact — it is derived from the
absence of invoice Facts. When the invoice arrives nothing is corrected;
a new `Invoice` Fact enters and state recomputes. Modelling case C as an
update would reintroduce the editable-status failure mode
[V1-SCOPE.md](V1-SCOPE.md) section 2.1 forbids.

## 1.2 Evidence is always preserved — FROZEN

Every correction requires new Evidence — an imported document/row, or a
human confirmation recorded as `FragmentKind.MANUAL_FACT`. No correction
path writes a Fact with no Evidence behind it. Prior `EvidenceDocument`
and `EvidenceFragment` rows are never modified and never deleted.

**Enforcement status, stated precisely.** Immutability is today enforced
at the Domain and repository layer: both types are
`@dataclass(frozen=True)`, `raw_data` is documented as never
overwritten, and no repository exposes an update API for either. There
is **no database-level immutability constraint** — nothing at the schema
level prevents an `UPDATE`. R1 must not add a write path, and adding
database-level enforcement (a trigger or equivalent) is a named
**DEFERRED IMPLEMENTATION DECISION**, not something this document claims
already exists.

## 1.3 Identity anchor + revisions — FROZEN

The naive design — supersede a Fact by writing a *new row* carrying the
same business identity — is **rejected**, because it conflates row
identity with logical identity. Every existing foreign key
(`InvoiceItemAllocation.contract_item_id`, `Accrual.contract_item_id`,
`AccrualBasisFact.contract_item_id`, every `contract_id`) points at a
row UUID. Superseding by new row would leave all of them pointing at a
superseded row, and there would be no defined way to resolve them to the
authoritative current graph.

**Frozen model: the identity row is stable and never superseded; the
versioned business values live in revision rows.**

```
ContractItem                      ← stable logical identity. All FKs point here.
  id                                 never superseded, never deleted
  contract_id
  source_item_key                    business identity (see 4.4)
  created_at

ContractItemRevision              ← versioned assertions
  id
  contract_item_id      ──────────►  the anchor above
  revision_type                      INITIAL | SUPPLEMENT | CORRECTION
  <business value fields>            sku, product_name, quantity, ...
  source_fragment_id                 Evidence for THIS revision
  superseded_by_revision_id          nullable
  created_at
```

Current values are the one revision where
`superseded_by_revision_id IS NULL`. The same shape applies to
`Contract` and to `Shipment` (section 3).

Why this shape:

- **Every existing foreign key stays valid and unambiguous.** Nothing
  needs re-pointing, because identity references never pointed at a
  version in the first place.
- **It matches the codebase's own precedent.** `Accrual` is never
  mutated; `AccrualReversal` rows accumulate; the balance is derived by
  one shared function. This is the same pattern applied to attributes.
- **The Rule Engine is untouched.** The repository layer already
  assembles Domain dataclasses from models (`_contract_item_to_domain`).
  It absorbs the anchor + current-revision join and returns a
  `ContractItem` dataclass of **exactly the present shape**, so
  `period_close.py` and every other consumer see no change. This is a
  hard requirement on R1, not an aspiration.
- **The unique constraint survives unchanged.**
  `UniqueConstraint("contract_id", "source_item_key")` stays on the
  anchor, where it is still exactly right. No partial index is needed.
- **No `is_current` sprawl**, and no rule inspects timestamps or version
  numbers to decide what is current — current-revision resolution is
  defined once, in the repository layer.
- **`revision_type` has a defined home**: it is a column on the revision
  row, constrained to `INITIAL | SUPPLEMENT | CORRECTION`, with exactly
  one `INITIAL` revision per anchor. It is never stored on the anchor,
  because it describes a transition, not an identity.

### Identity references vs provenance references — FROZEN

This distinction governs every foreign key in the system after
supersession exists:

| Kind | Examples | Rule on correction |
|---|---|---|
| **Identity reference** — *which business thing* | `contract_id`, `contract_item_id`, `invoice_id`, `payment_id` | Points at the stable anchor. Always resolves to current values. **Never re-pointed**, because it never named a version |
| **Provenance reference** — *which exact artifact was used* | `source_fragment_id`, `created_from_fact_id`, `current_source_fragment_id`, the revision's own `source_fragment_id` | Points at the exact historical artifact. **Never re-pointed, ever.** An Accrual created from revision v1 keeps pointing at v1 — that is required for defensibility, not a bug |

An `Accrual` whose basis was corrected therefore still traces to the
revision actually used at the time, while queries asking "what is this
contract item now" resolve through the anchor to the current revision.
Both questions have exactly one answer, and they are different
questions.

### Migration scope — DEFERRED IMPLEMENTATION DECISION (R1)

R1 owns: creating the revision tables, moving existing business values
into one `INITIAL` revision per anchor, and updating the repository
assembly. The Domain dataclass shape and every consumer of it stay
unchanged.

## 1.4 Which objects get a correction mechanism — FROZEN

The dividing line is **who asserts the fact**:

| Object | Correction | Reasoning |
|---|---|---|
| `ContractItem` | **Yes — anchor + revisions (R1)** | Human-entered, no everyday intake path today, highest exposure, and every item-level judgment depends on it |
| `Contract` | **Yes — anchor + revisions (R1)** | Ledger header values get mis-keyed and the ledger is revised; without this a revised workbook duplicates contracts instead of correcting them |
| `Shipment` | **Yes — designed in from R2** | Human/document-entered, same exposure |
| `SalesContract` | **Yes — anchor + revisions (R3a)** | Human/document-entered from sales evidence, and `customer` is expected to arrive *after* the anchor exists. It reuses the **same** model as `ContractItem` and `Contract` — `SUPPLEMENT` for the customer arriving later, `CORRECTION` for a wrong value — and no second correction mechanism is created for the sales leg |
| `ProcurementSalesLink` | **Yes — relationship-level supersession / invalidation; no in-place endpoint overwrite** | A link asserts that a relationship exists, and that assertion can later be shown wrong. It does not need the attribute-revision model, because it has no asserted attributes to version — it needs **relationship** supersession: the confirmed link becomes non-current, its endpoints and Evidence are preserved, and a replacement becomes current if one exists. See 2.4. A `Task` is the *work entry* for ambiguity or conflict, not the correction semantics itself |
| Manually confirmed facts — `CostRecognitionFact`, `AccrualBasisFact`, `HistoricalAccrualFact`, `InvoiceItemAllocation` | **Yes, by whole-fact supersession** | These are single assertions with no independent identity of their own; they are superseded as a unit rather than versioned attribute-by-attribute. Mechanism detail is a **DEFERRED IMPLEMENTATION DECISION** for R1 |
| `Invoice`, `InvoiceItem`, `Payment` | **No** | External facts. The issuing system corrects itself through its own instruments (credit note, reversing entry), which arrive as **case C progression** — not as corrections of BEL's record |
| `Accrual`, `AccrualReversal` | **No** | Derived/executed records with their own additive reversal mechanism. Corrected by correcting the basis Facts and recomputing — never edited |
| `Evidence*` | **Never** | Immutable (A02) |

A bad *import* of an external fact — wrong direction flag, wrong file,
wrong profile — is an import-level remediation problem, not a Fact
correction problem. **DEFERRED IMPLEMENTATION DECISION**; folding it in
would give external append-only facts an editing path they must not
have.

## 1.5 Recomputation after correction — FROZEN

> Correcting a Fact must never require a human to manually delete stale
> derived state.

**Stateless decisions recompute for free.** `build_period_close_preview`
is a strict read-only preview running under `session.no_autoflush` that
writes nothing; every blocker, candidate and difference is recomputed
from current Facts on each run. Once current-revision resolution lives
in the repository, correction propagates across the whole period-close
surface with no invalidation logic.

**Persisted derived records do not self-update** — `Accrual`,
`AccrualReversal`, `InvoiceAllocation`, `PaymentAllocation`,
`TaskException`.

Frozen handling, in full:

1. When a revision is superseded, any persisted derived record whose
   **provenance reference** names the superseded revision is identified.
2. A **Task** is generated (A05) naming the superseded revision, the
   superseding revision, and each affected derived record. Silent
   cascade is forbidden: a data-entry fix must never rewrite an executed
   business record with no human in the loop.
3. Until that Task is resolved, the affected derived record **remains
   as-is and is visibly flagged as resting on a superseded revision.**
   It is not silently trusted and it is not silently discarded.
4. Resolution happens only through **an allowed action that produces a
   new Fact, or that reverses the derived record through that record's
   own mechanism** — never by editing the derived record in place. A
   corrected quantity does not edit an `Accrual`; it reverses it via
   `AccrualReversal` and a new `Accrual` is created from the corrected
   basis.
5. The Task closes when the condition that produced it no longer holds —
   the same closed loop [V1-SCOPE.md](V1-SCOPE.md) section 5.2 describes.

Which derived records are scanned, and the exact Task payload, are a
**DEFERRED IMPLEMENTATION DECISION** for R1. The response *policy* above
is frozen.

**R015 is untouched** — still `PROPOSED` in [RULES.md](RULES.md). This
freeze is directionally consistent with it and does not promote it or
restate its trigger condition.

---

# 2. Sales-side Relationship / Matching Semantics

## 2.1 Party roles — FROZEN by business confirmation

The recommendation carried in the previous draft of this document was
**wrong**, and is corrected here. Business confirmation:

```
卖方  (COUNTERPARTY_HEADER → Contract.counterparty)
      = the domestic supplier                          ← external party

买方  (BUYER_HEADER        → Contract.buyer)
      = our own trading / export entity                ← OURSELVES
```

Frozen consequences:

- **`Contract.buyer` is our own entity. It must never be used as the
  sales-side customer key** — for a `SALES` invoice, an `IN` payment, or
  anything else. Doing so would attribute every sales invoice and every
  customer receipt to our own company.
- The `Contract` rows BEL holds today are **procurement-leg contracts**.
  Purchase-side matching keying on `counterparty` is correct and stays
  unchanged.
- `counterparty` and `buyer` are **positional parties of the contract as
  written** (its seller and its buyer), not fixed business roles. Which
  side is external depends on which leg the contract represents. No code
  may assume `counterparty` is always the external party.
- **The sales-side customer has no canonical source in BEL today.** It
  must be established as an independent fact source from sales-side
  Evidence — the 外销合同 (export sales contract) and export/shipment
  material.
- The legacy export-contract-number column is an **anchor for locating**
  the sales-side business relationship. It does **not** itself carry
  customer identity and must not be treated as if it did.

This is now settled and R3 is no longer blocked on *which column*. It
is blocked on a larger gap this answer exposed — see 2.6.

## 2.2 Sales-side scope: a separate `SalesContract` — FROZEN

Because `Contract.buyer` is our own entity, **BEL held no representation
of the sales leg at all.** Three minimal models were evaluated; the
business owner approved the second.

| | Model | Verdict |
|---|---|---|
| 1 | Reuse `Contract`, discriminated by `contract_type` | Rejected |
| 2 | **Distinct `SalesContract` + `ProcurementSalesLink` bridge** | **Approved** |
| 3 | Neutral trade-case object owning both legs | Rejected |

### Why model 2

- **It removes a concrete regression hazard in shipped code.**
  `match_invoices` iterates `contract_repo.list_all()` with no type
  filter (`src/bel/application/matching.py`). Putting sales contracts in
  the same table would silently expose them as candidates for
  purchase-invoice matching, forcing a change to an already-accepted
  Phase 2A path. Physical separation needs no filter and carries no
  regression risk.
- **It removes positional party semantics.** Under model 1 the external
  party would sit in `counterparty` for a procurement contract and in
  `buyer` for a sales contract — one table, two readings, discriminated
  by a field the ledger importer currently writes as a single hardcoded
  constant. `SalesContract.customer` is a named field with one meaning.
- **The spec-change cost was already incurred.** Correcting the
  `Contract.buyer` conflict required SCR-2D1R0-001 regardless, so adding
  match types to the same SCR is incremental — which was model 1's only
  real advantage.
- **Model 1's remaining advantage does not exist.** Reusing `Contract`
  would have inherited `ContractItem` as sales line items, but V1 builds
  no sales-side item object at all (2.4), so nothing is inherited.

### Why not model 3

A neutral object owning both legs adds a third concept to hold a
customer that belongs to the sales contract; it introduces an axis that
does not match the legacy ledger (one row per procurement contract),
which would weaken cutover reconciliation; and it is the entry point for
order/case lifecycle machinery that the first stage explicitly excludes.
If a cross-leg business-case view is ever needed, it can be *derived*
from the connected components of `ProcurementSalesLink` without an
entity.

### Frozen semantics

- `Contract` represents the **procurement leg only**;
  `Contract.counterparty` is the external supplier and `Contract.buyer`
  is our own entity.
- `SalesContract` carries the sales-side business scope and is **the
  only place an external customer is expressed**.
- `SalesContract.sales_contract_no` is a **business key in a separate
  namespace** from `Contract.contract_no`; the two never share
  uniqueness. Conflicting customers under one `sales_contract_no`
  surface as `BusinessKeyConflict` (R004 pattern), never merged.
- Minimum V1 fields — `our_entity`, `sales_contract_no`, `customer`
  (nullable), `currency`, `gross_amount`, `contract_date`, plus the
  Evidence trace and the anchor + revision structure of section 1.3.
  Explicitly excluded: customer address/contact, credit terms, price
  terms, payment terms, Incoterms, shipping terms, and sales line items
  — no first-stage rule consumes any of them.
- **Business identity is `(our_entity, sales_contract_no)`** — see 4.4
  for the null and conflict policy. `customer` deliberately takes no
  part in identity, because it is allowed to be unknown when a scope is
  first learned (2.3); an anchor may not depend on a value that legally
  arrives later.

## 2.3 External customer identity — FROZEN

The canonical identity of the external customer comes from **sales-side
Evidence: the export sales contract document itself**, ingested like any
other evidence.

Three tempting sources are explicitly **excluded**:

| Rejected source | Why |
|---|---|
| `Contract.buyer` | Our own entity (2.1). Using it attributes sales business to ourselves |
| The sales-scope reference number carried on procurement evidence | A **reference to a scope**, not an identity of a party. It says which sales contract, not who the customer is |
| A customs-receiving party named on shipping material | A declaration/forwarding agent, not necessarily the buying customer. Must not be assumed equivalent |

**A `SalesContract` may legitimately exist before its customer is
known.** A sales scope is often first learned from a reference carried
on procurement evidence, before the sales contract document has been
ingested. Such a scope is created with no customer and is **not capable
of supporting outbound invoicing**; the customer is supplied later from
sales-side Evidence as a `SUPPLEMENT` revision under the section 1.1
taxonomy — reusing the correction machinery already frozen in this
document rather than inventing a second mechanism. Until then the scope
carries an unresolved-customer `Task`. It is never guessed.

## 2.4 The canonical bridge: `ProcurementSalesLink` — FROZEN

The bridge is frozen explicitly rather than left to "relate them through
Shipment later".

Minimum V1 fields:

```
id
procurement_contract_id
sales_contract_id
source_fragment_id
confirmation_type          AUTO_CONFIRMED | HUMAN_CONFIRMED
created_at
```

| Question | Frozen answer |
|---|---|
| **Who holds the bridge?** | A dedicated object, `ProcurementSalesLink`. Neither leg holds a foreign key to the other |
| **When does a link exist?** | **Only for a confirmed relationship.** Evidence that merely suggests a pairing produces a candidate for human confirmation and, unresolved, a `Task` — it does not create a link |
| **Does the link need a workflow state?** | **No.** Because an unconfirmed relationship is never written, there is no `OPEN`/`RESOLVED` lifecycle on the link itself. `confirmation_type` records *how* it was confirmed, not whether |
| **Business identity** | `(procurement_contract_id, sales_contract_id)`. Neither end may be empty. See the two-layer identity below |
| **Conflicting Evidence** | Never overwrites and never silently re-points a current link. It produces a `Task`, and the current assertion is unchanged until a human confirms |
| **Corrective Evidence** | Once human-confirmed, retires the current assertion through an append-only correction record — replacement or pure invalidation. It never deletes or edits the original row |

### Two-layer identity: business key vs assertion episode — FROZEN

An earlier draft of this document stated both that a pair which had ever
existed could never be created again, and that a retired pair could be
legitimately re-established with new Evidence and human confirmation.
**Those two statements contradict each other.** The contradiction came
from collapsing two different things into one identity, and is resolved
by separating them:

```
Relationship business key   =  (procurement_contract_id, sales_contract_id)
                               WHICH business relationship this is

ProcurementSalesLink row    =  one confirmed assertion episode
                               one occasion on which that relationship
                               was confirmed to hold
```

**Frozen invariant:**

```
at most ONE CURRENT assertion episode per relationship business key
```

and **not** "only one row for the pair for all of history". A business
key may accumulate **several** episodes over time — at most one of them
current, the rest permanently retired. This is what makes history
preservation, replay safety, and legitimate re-establishment
simultaneously satisfiable.

### The three creation actions — FROZEN, never inferred

Which action applies is an explicit determination, never something the
system derives from the shape of the data:

| Action | Precondition | Effect |
|---|---|---|
| **ADD** | The business key has **no current and no retired** episode | New Evidence, confirmed → create a current assertion episode |
| **CORRECT / INVALIDATE** | The business key has a **current** episode | Corrective Evidence + human confirmation → append a correction record; the current assertion is retired. A replacement may or may not exist |
| **REESTABLISH** | The business key has a **retired** episode and **no current** one | New Evidence + **explicit `HUMAN_CONFIRMED` re-establishment** → create a **new** assertion episode |

**`REESTABLISH` is not resurrection.**

```
re-establish  ≠  resurrect the old row
```

The retired episode stays retired permanently, with its own endpoints,
its own Evidence and its own correction record intact. Re-establishment
writes a *new* episode with its *own* provenance; nothing historical is
reopened, re-pointed or mutated.

### Replay protection — FROZEN

Protection rests on **per-episode provenance**, not on forbidding the
pair outright:

- **The same `source_fragment_id` for the same business key is
  idempotent.** Re-running an import that already produced an episode
  produces nothing — no duplicate, no new episode.
- **Replaying historical Evidence never produces a current episode.**
  Evidence that originally created an episode which has since been
  retired cannot, by being processed again, make that business key
  current. A backfill re-run is therefore always safe.
- **Only new Evidence plus an explicit `HUMAN_CONFIRMED` re-establishment
  action creates a further episode** for a business key that already has
  a retired one. There is no automatic path.

This replaces the earlier, self-contradictory "the pair may never exist
twice" rule.
| **Cardinality** | **Many-to-many.** One sales scope may be supplied by several procurement contracts; one procurement contract may supply several sales scopes |
| **Does it carry amounts or quantities?** | **No.** It expresses a relationship, never a proportion |
| **Does `ContractItem` participate?** | **No** in V1. Which purchased item fulfils which sold line has no evidence source today, and no first-stage judgment needs it |
| **What is `Shipment`'s role?** | **Not the bridge.** An execution fact that may *corroborate* a link. It never creates one — see section 3.6 |
| **Evidence trace** | `source_fragment_id` is the direct Evidence for the confirmed relationship — commonly a sales-scope reference carried on a procurement ledger row. **A human confirmation is not exempt:** it must supply its own manual Evidence, so no link can exist whose only justification is that somebody clicked confirm |
| **Ledger aggregation direction** | The **procurement contract** axis, matching the legacy ledger it replaces (one row per procurement contract, with the sales-scope reference as a column on that row). Keeping the same axis is what gives cutover reconciliation a row-for-row correspondence. A sales-side view may follow later as a secondary projection |

### Relationship correction: supersession and invalidation — FROZEN

Confirming a link is a canonical Fact assertion, so it must be
correctable when later Evidence shows it wrong. Four outcomes are
forbidden outright:

```
DELETE the old link                                       ✗ loses history
UPDATE old_link.sales_contract_id = the new one           ✗ in-place endpoint overwrite
raise a Task but leave the wrong link current forever     ✗ never converges
leave the replaced and replacing links both current       ✗ silent dual-current
```

Frozen principle:

```
confirmed relationship
        +
later corrective Evidence
        ↓
the old relationship becomes non-current
its endpoints and its original Evidence remain, permanently
a replacement relationship becomes current, if one exists
```

**Both outcomes must be expressible**, which is why a bare
`supersedes_link_id` on a replacement row is rejected — it cannot
express a retirement with no replacement:

| Case | Meaning |
|---|---|
| **Replacement** | `A → X` was wrong; the real relationship is `A → Y` |
| **Pure invalidation** | `A → X` was wrong; there is no replacement — the relationship simply does not exist |

**Recommended mechanism for R3a — an append-only correction record.**
The link row itself is never mutated:

```
ProcurementSalesLinkCorrection
  id
  superseded_link_id        → the link being retired          required
  replacement_link_id       → the link replacing it           NULL = pure invalidation
  source_fragment_id        Evidence for the correction       required
  confirmation_type
  created_at
```

This follows the precedent already established in this codebase:
`AccrualReversal` retires part of an `Accrual` by appending a row rather
than mutating it, and the resulting state is derived. The alternative —
a `status` plus `superseded_by` column mutated on the link row — was
considered and not recommended: it rewrites a row that asserts a
business fact, and it needs a second nullable field to express
invalidation anyway. R3a implements the record above; this is a
recommendation, not an open question.

**Deterministic current-link selection — FROZEN.**

```
current(link)  ⟺  no correction record names it as superseded_link_id
```

Resolved in **one shared place** in the repository layer, exactly as
`get_accrual_balance` / `is_open_accrual` are the single shared
predicates for accrual state. The Contract Business Ledger, downstream
rules and every projection consume **current confirmed links only**.
Superseded and invalidated links stay fully auditable but take no part
in current Ledger projection or in any business judgment. No rule or
application code ever inspects timestamps to decide which link is newer.

**Correction requires Evidence — FROZEN.** `source_fragment_id` on the
correction record is required. A human correction supplies its own
manual Evidence (`FragmentKind.MANUAL_FACT`), so there is no path by
which "a user clicked remove relationship" retires a confirmed link with
no provenance — the same rule the link itself is subject to.

**Only a human confirmation changes the authoritative current
relationship — FROZEN.** Corrective Evidence alone does not flip
authority. The system already held a confirmed answer, so overturning it
is precisely A05's "when uncertain, generate a Task" case: conflicting
Evidence produces a `Task` and leaves the current link untouched until a
human confirms. In V1 a correction record is therefore always
`HUMAN_CONFIRMED`.

**Additive versus corrective is a human determination — FROZEN.**
Because the bridge is many-to-many, one procurement contract having
current links to several sales scopes is **legitimate**, not a conflict.
So when new Evidence names a different pair, the system cannot infer
whether it *adds* a relationship or *replaces* an existing one. It never
guesses: the ambiguous case becomes a `Task`, and a human decides.

**Replacement is atomic — FROZEN.** Writing the replacement assertion
and the correction record that retires the old one happens in a single
transaction, so no observable state exists in which both are current.
No link implementation exists yet, so this is stated as an obligation,
not an existing property: **R3a must enforce these correction and
current-state invariants transactionally.**

### Correction lineage invariants — FROZEN

Without these, a correction chain can fork: two corrections retiring the
same assertion toward different replacements, leaving no determinable
current state.

**Only a current assertion may be corrected.** A retired assertion is
final: it can never be corrected again, re-pointed, or reopened. A
correction targeting a retired episode is rejected.

**One correction per assertion episode.**
`ProcurementSalesLinkCorrection.superseded_link_id` is **semantically
unique** — an assertion episode may be superseded at most once:

| Submission | Outcome |
|---|---|
| The same correction submitted again (same superseded episode, same replacement) | **Idempotent** — no second correction record |
| A *different* replacement for a `superseded_link_id` that already has a correction | **Conflict → `Task` / reject.** No second correction record is written, and the existing lineage is not altered |

**Replacement that is already current.** So an implementer has nothing
to guess:

- If the replacement business key **already has a current episode**, the
  correction **references that existing episode**. No duplicate
  replacement episode is created — doing so would violate the
  one-current-episode invariant.
- If the replacement business key has **no current episode**, a new
  confirmed replacement assertion is created **in the same transaction**
  as the correction.

**Transactional obligation on R3a.** No link implementation exists yet,
so this is a requirement on the implementing round, not a property to be
assumed. Within one transaction R3a must:

1. verify the target assertion is still **current**;
2. verify **no correction already names it** as `superseded_link_id`;
3. resolve the replacement — reference an existing current episode, or
   create one;
4. write the correction record;
5. commit.

and it must enforce, at the storage level, both

```
superseded_link_id                      unique
one CURRENT assertion episode           per relationship business key
```

**Invariants restated:** for any relationship business key, **at most
one assertion episode is current** at any time; retired episodes
accumulate behind it permanently; the correction history is append-only
with **no delete, no endpoint overwrite, and no lineage branching**.

**No cross-bridge apportionment in V1 — FROZEN.** On a many-to-many edge
there is no basis for spreading an amount or a quantity without an
explicit allocation fact. Any figure produced that way would be
invented, so V1 performs none: linked sales scopes are enumerated on a
procurement row, never summed across the bridge. Cross-leg apportionment
requires its own confirmed business rule and is not in V1.

### Cardinality and conflict handling — FROZEN

| Situation | V1 behaviour |
|---|---|
| One sales scope ← several procurement contracts | **Allowed.** Normal for consolidated export business. The Ledger shows the scope on each procurement row; figures are never summed across |
| One procurement contract → several sales scopes | **Allowed structurally**, but ambiguous: which sales scope that contract's cost serves is undecidable from the link alone. Produces a `Task`; the system does not choose an attribution |
| Scope reference present, no sales-contract Evidence | `SalesContract` exists with no customer; unresolved-scope `Task`; outbound invoicing blocked for that scope |
| Same `sales_contract_no`, conflicting customers | `BusinessKeyConflict` (R004 pattern). Never auto-merged |
| A `Shipment` implies a link that does not exist | `Task`. The link is never created silently (3.6) |
| A sales invoice matches more than one `SalesContract` | `MatchCase` in `HUMAN_CONFIRMATION_REQUIRED` with real `SalesMatchCandidate` rows |

The exception types these require are additions to `TaskException`,
which [V1-SCOPE.md](V1-SCOPE.md) section 5.2 already anticipates the
Exception Center carrying. Their exact codes and payloads are a
**DEFERRED IMPLEMENTATION DECISION** for R3a.

## 2.5 What a Sales Invoice associates to — FROZEN

```
SALES Invoice  ──(allocation)──►  SalesContract
```

reusing the allocation **semantics and shape**, implemented by the
separate sales-side allocation object `SalesInvoiceAllocation` (2.7) —
never by the procurement `InvoiceAllocation` table. Contract-level in V1;
many-to-many via allocation, consistent with [DOMAIN.md](DOMAIN.md)'s
rule that an invoice-to-contract relationship is never a single foreign
key. A sales invoice is **never** allocated to a procurement `Contract`.

## 2.6 What an incoming Payment associates to — FROZEN

Frozen: **`IN` receipt → `SalesContract` via `SalesPaymentAllocation`
is the V1 canonical association.**

The sales-side object reuses the **allocation semantics and shape** of
the procurement one — subject, target, match case, allocated amount,
confirmation type — but it is a **physically and semantically separate
object** (2.7). The procurement `PaymentAllocation` remains bound to
`OUT` payments attributed to a procurement `Contract`, with its hard
`contracts.id` foreign key unchanged; it is never used for an `IN`
receipt, and no sales-side attribution is expressible through it:

```
procurement OUT payment  →  PaymentAllocation        → Contract
sales-side IN receipt    →  SalesPaymentAllocation   → SalesContract
```

The bank-grain fact stays its own `Payment` row and attribution stays a
separate object, per [DOMAIN.md](DOMAIN.md). No direct
`Payment ↔ Invoice` association.

Open — **`REQUIRES BUSINESS RULE FREEZE`**: whether scope-level receipt
attribution suffices or receipts must be tracked against specific sales
invoices. `Payment ↔ Invoice` is not among [V1-SCOPE.md](V1-SCOPE.md)
section 3's match types, so adding it would be a scope change. R3b
implements scope-level only.

## 2.7 Sales-side allocation objects — FROZEN by physical separation

The procurement allocation objects are **not** generalised, made
polymorphic, or re-pointed. Their foreign keys are hard references to
`contracts.id` and stay that way:

```
InvoiceAllocation.contract_id   → procurement Contract   unchanged
PaymentAllocation.contract_id   → procurement Contract   unchanged
MatchCandidate.contract_id      → procurement Contract   unchanged
```

The sales leg gets its own objects instead. This is the same physical
separation that justified a separate `SalesContract` (2.2): a structure
that cannot express the wrong relationship is safer than one that must
be disciplined into not doing so.

**`SalesInvoiceAllocation`**

```
id
invoice_id                 → a SALES Invoice
sales_contract_id          → SalesContract
match_case_id
allocated_gross_amount
confirmation_type
created_at
```

**`SalesPaymentAllocation`**

```
id
payment_id                 → an IN Payment
sales_contract_id          → SalesContract
match_case_id
allocated_amount
confirmation_type
created_at
```

Frozen properties:

- Existing procurement `InvoiceAllocation` / `PaymentAllocation`
  semantics are **completely unchanged**.
- A `SALES` invoice can never be attributed by a procurement
  allocation, and an `IN` receipt can never be attributed by a
  procurement allocation — structurally, not by convention.
- One subject **may** be allocated across **several** `SalesContract`s,
  exactly as the procurement side allows across contracts. Allocation
  remains the place where a one-to-many business attribution is
  expressed.
- R3b's first version may implement **manual / human-confirmed
  allocation only.** That is sufficient to deliver the read models R4
  needs.
- Any **automatic** sales matching algorithm remains
  `REQUIRES BUSINESS RULE FREEZE` (2.8) — R3b is deliberately scoped to
  be complete without it.

### `MatchCase` reuse — FROZEN as reuse, with two named guards

`MatchCase` is **reused** for the sales leg; a separate
`SalesMatchCandidate` is added:

```
SalesMatchCandidate
  id
  match_case_id
  sales_contract_id
  created_at
```

Code evidence that reuse is safe:

- `MatchCaseModel` has **no foreign key to `contracts`** and no
  leg/direction field. `subject_id` is already deliberately polymorphic
  — its own docstring says so — and `subject_type` is `INVOICE` /
  `PAYMENT`, which is neutral between purchase and sales.
- `match_method` is an unconstrained `String`, so a sales method value
  needs no schema change.
- The per-subject dedup key `find_by_subject(subject_type, subject_id)`
  **cannot collide across legs**: an `Invoice` is either `PURCHASE` or
  `SALES` by its own field, and `match_invoices` filters to `PURCHASE`,
  so one subject belongs to exactly one leg and therefore to at most one
  `MatchCase`.
- `MatchCandidateModel.contract_id` is a hard FK to `contracts.id`,
  confirming it can only ever name a procurement contract — hence a
  separate candidate object rather than a polymorphic one. No generic
  polymorphic candidate framework is introduced.

Two guards R3b **must** implement, found by reading the confirmation
path rather than assumed:

1. **`confirm_match` must reject a sales-leg `MatchCase`.**
   `confirm_match` (`src/bel/application/matching.py`) takes a
   `contract_id`, loads it through `ContractRepository`, and writes a
   procurement `InvoiceAllocation` or `PaymentAllocation` in both
   branches, with **no direction or leg check anywhere**. Left as is, a
   human confirming a sales `MatchCase` through `bel match confirm`
   would attribute a `SALES` invoice to a procurement contract — the
   exact outcome this design forbids. The guard is a defensive rejection
   only; it does **not** alter M001 semantics or any procurement
   behaviour.
2. **Leg-agnostic listings must not present a sales case as confirmable
   through the procurement path.** `list_match_cases`
   (`src/bel/application/list_matches.py`) returns every `MatchCase`
   with no filter, and the CLI surfaces it directly.

## 2.8 Amount semantics — REQUIRES BUSINESS RULE FREEZE

`M001` / `EXACT_COUNTERPARTY_AMOUNT_UNIQUE` — exact party, exact amount,
unique candidate — **may not be reused on the sales side.** The party
freeze sharpens why: `Contract.gross_amount` is the **procurement** value
of a contract whose buyer is ourselves. It is not the sales value, and
an amount-equality rule built on it would match wrongly and silently.

Frozen: **no sales-side amount matching rule exists.** The relationship
model is frozen; the algorithm deciding *which* sales scope an invoice
or receipt belongs to awaits a business rule freeze. R3b implements the
association model and the human-confirmation path without it.

## 2.9 Ambiguity handling — FROZEN

Inherited unchanged: explicit matched / ambiguous / unmatched outcomes,
never a silent best guess; ambiguity produces a `MatchCase` in
`HUMAN_CONFIRMATION_REQUIRED` with real candidate rows — `MatchCandidate`
on the procurement leg, `SalesMatchCandidate` on the sales leg; business
keys remain non-unique and multiple candidates are never grounds for
merging; the `_ContractResolver` precedent governs ("rejecting, not
guessing"); full traceability to the originating `EvidenceFragment` on
every sales-side Fact, link and allocation.

## 2.10 R3 implementation boundary — FROZEN

R3 is split into two rounds.

**R3a — Sales Scope & Procurement-Sales Bridge** may implement:
`SalesContract` intake from sales-side Evidence (anchor + revisions,
customer allowed to start unknown); customer supplied later as a
`SUPPLEMENT` revision; `ProcurementSalesLink`, with the procurement
ledger's sales-scope reference column as a backfill basis and that
row's fragment as the link Evidence; an optional nullable sales-side
reference on `Shipment`; and the unresolved-scope / unconfirmed-link
Tasks.

**R3b — Sales-side Allocation** may implement: `SALES` invoice →
`SalesContract` allocation; `IN` receipt → `SalesContract` allocation;
the human-confirmation path; and the read models R4 needs for
sales-invoice and receipt state.

**Neither round may:** apply any automatic amount-based sales matching
rule (2.7); apportion any amount or quantity across the bridge (2.4);
create a `Payment ↔ Invoice` association (2.6); use `Contract.buyer` as
a customer key (2.1); treat a sales-scope reference number as a customer
identity (2.3); let a `Shipment` create a link automatically (3.6);
build a sales-side item object (2.2); or make any outbound-invoicing
eligibility judgment, which is Phase 2D.3's.

# 3. Shipment — Minimal Semantics

## 3.1 One object, and one name — FROZEN

V1 models **a single object, named `Shipment`.** [DOMAIN.md](DOMAIN.md)
already treats "Shipment / Export" as one concept under one heading, so
no frozen document needs changing; "Shipment / Export" is that
document's concept heading, while `Shipment` is the canonical object
name used everywhere in implementation. R2 must not introduce a second
object or a second name.

The one business fact it expresses:

> Goods for a given contract (and, where known, a given item scope) have
> actually shipped/exported, to a degree sufficient to serve as a basis
> for business execution judgments.

## 3.2 Minimal canonical fields — FROZEN

```
id                          stable identity anchor (section 1.3 applies)
contract_id                 required — the association (3.3)
contract_item_id            optional — item scope where known
execution_date              when the goods left
quantity                    optional — required for item-level use
external_reference          optional — declaration/booking reference as recorded
source_fragment_id          required — Evidence trace, never nullable
created_at
```

with revision rows per section 1.3 carrying the correctable values.

**Deliberately excluded from V1**, because no first-stage decision
consumes them: customs declaration workflow state, any tax-rebate field,
carrier/vessel/port/container, HS codes, logistics tracking status, and
multi-leg itineraries. `amount` is excluded rather than added
speculatively; it is addable when a frozen rule needs it. Per
[DOMAIN.md](DOMAIN.md) this object records export *execution*, not tax
treatment.

## 3.3 Contract ↔ Shipment association — FROZEN

| Question | V1 answer |
|---|---|
| One `Contract` → many `Shipment`s? | **Yes.** Partial and repeated shipment is normal |
| One `Shipment` → many `Contract`s? | **No** in V1. A single `contract_id`. A shipment genuinely spanning contracts is an explicit unresolved case producing a Task — never a silent split |
| Item-level required? | **No — optional.** `contract_item_id` nullable |
| No item detail? | Contract-scope fact. Supports contract-level judgments only, never item-level accrual — consistent with R007 |

The legacy export-contract-number column is an **intake anchor**, used to
find and associate export evidence. It is not the canonical association;
`contract_id` is.

## 3.4 Shipment → cost recognition trace — FROZEN

`CostRecognitionFactModel` today carries only `contract_id`,
`recognition_date`, `basis` and `source_fragment_id`. It therefore
**cannot express which shipment evidenced it**, and R2 must not leave
that to the implementer.

**Frozen trace model: an explicit provenance reference.**
`CostRecognitionFact` gains a nullable `shipment_id` foreign key naming
the `Shipment` anchor that evidenced this cost recognition. It is a
**provenance reference** under 1.3 — recorded once and never re-pointed.
Adding the column is an R2 migration.

Alternatives explicitly rejected, so they are not re-litigated:

- **Shared `source_fragment_id`** (infer the link when both cite the
  same fragment) — rejected: one fragment can yield several shipments,
  so the inference is ambiguous exactly where it matters.
- **A generic provenance link table** — rejected: over-general for a
  single named relationship, and it would invite an unbounded Fact-graph
  design this round is meant to avoid.

**What this does not change:** creating a `CostRecognitionFact` still
requires a human assertion. `shipment_id` records *which* shipment
evidenced it; it does not create it. Phase 2B deliberately decided the
system "does NOT decide which business behavior means cost recognition",
and auto-deriving cost recognition from a shipment would reverse that
frozen decision by the back door. Whether a `Shipment` Fact
automatically implies cost recognition is **`REQUIRES BUSINESS RULE
FREEZE`**; until answered, a human assertion remains the only thing that
creates a `CostRecognitionFact`. Because the Rule Engine consumes rather
than derives the fact, `period_close.py` stays untouched throughout.

## 3.5 `Shipment` is not the procurement/sales bridge — FROZEN

The bridge is `ProcurementSalesLink` (2.4). A `Shipment` is an execution
fact, not a relationship object, and this must be stated explicitly
because an implementer would otherwise reach for it naturally — a
shipment plausibly touches both legs.

Frozen:

- R2 delivers `Shipment` with its **procurement `contract_id` only**.
  The cost-recognition path (3.4) is procurement-side and complete
  without any sales-side reference.
- R3a may add a **nullable sales-side reference** to `Shipment`. That is
  an additive field, not a redesign, and R2 does not anticipate it.
- A `Shipment` **never creates a `ProcurementSalesLink` automatically.**
  A shipment implying a link that has not been established produces a
  `Task`; the relationship is confirmed, not inferred.
- A shipment may **corroborate** an existing link. Corroboration is not
  creation.

## 3.6 Shipment is not invoice eligibility — FROZEN

```
Shipment Fact   ≠   Invoice eligibility Decision
```

A shipment is a candidate *input* to a future eligibility rule. R0 does
**not** freeze `Export completed → ready to invoice`, and R2 may not
encode it. Eligibility is frozen separately before Phase 2D.3.

**The same caution applies to quantity.** V1 builds no sales-side item
object (2.2), and a `Shipment` is an important candidate fact source for
the quantity a future outbound invoice would prepare. That is a design
direction, **not a frozen rule.** R0 explicitly does **not** freeze
`shipped quantity = invoiceable quantity`; what actually determines an
invoiceable quantity belongs to Phase 2D.3's invoicing eligibility
freeze, together with the rest of the eligibility question. Nothing in
R2 or R3 may treat shipped quantity as settled invoicing input.

---

# 4. Legacy Backfill / Cutover Semantics

## 4.1 The role of the legacy ledger — FROZEN

Evidence / source, migration aid, and reconciliation input. **Not Golden
Truth.** Restates [V1-SCOPE.md](V1-SCOPE.md) section 7.1.

## 4.2 Backfill imports Facts, never derived state — FROZEN

Forbidden:

```
legacy column "已付款"  →  BEL state = PAID
```

Required:

```
legacy Evidence  →  canonical Fact(s)  →  deterministic state
```

Every legacy column expressing a *derived business status* resolves to
exactly one of three outcomes, and no others:

1. **Facts are recoverable** — import the underlying Facts and let the
   rules derive the state. The status column itself is never imported.
2. **Facts are not recoverable, but business confirms the state** — a
   Human-Confirmed Cutover Fact (4.3), explicitly labelled, and only of
   an allowed type.
3. **Neither** — **Evidence only.** Preserved in the fragment's
   `raw_data`, promoting to nothing.

Outcome 3 is free today: importers already retain the complete source
row in `EvidenceFragment.raw_data`, so an unpromotable column stays
fully preserved and traceable without becoming a Fact.

## 4.3 Human-Confirmed Cutover Facts — FROZEN, with a closed allowlist

Some historical business cannot have its original evidence
reconstructed. This is **allowed** as a named, migration-specific Fact
source, generalising a precedent that already exists rather than
inventing one: `HistoricalAccrualFact` is documented as a go-live
business-state fact "NOT claimed to have been computed by this system" —
precisely a human-confirmed cutover fact for accruals.

**Provenance constraints** (all frozen): backed by real Evidence of the
human confirmation (`FragmentKind.MANUAL_FACT`) carrying a distinct
source type identifying it as cutover baseline material; never
impersonating original source-system Evidence, with a machine-readable
basis so any consumer can tell a confirmed-at-cutover fact from an
evidenced one; supersedable by later real Evidence through section 1;
and never entering public fixtures (P05).

**Closed allowlist of expressible fact types.** A cutover fact may be
created only for these, each of which is a rule *input*:

| Fact type | Rule consumer |
|---|---|
| `ContractItem` (anchor + `INITIAL` revision) | R006, R007 — item scope |
| `HistoricalAccrualFact` | R001, R003 |
| `CostRecognitionFact` | R002 |
| `AccrualBasisFact` | R002, scoped by R007 |
| `InvoiceItemAllocation` | R001, R006 |

**Explicitly not allowed**, and this is the guard against repackaging
derived state:

- `Invoice`, `InvoiceItem`, `Payment` — external facts. They come from
  real source documents or not at all. Permitting a "confirmed" payment
  would let the legacy 已付款 column re-enter as a Fact by another name,
  which is exactly what 4.2 forbids.
- `Accrual`, `AccrualReversal`, `InvoiceAllocation`, `PaymentAllocation`
  — these are rule *outputs* and derived records. A cutover fact may
  never express a rule output.
- `Shipment` — an export execution either has evidence or is unresolved.

The governing rule: **the allowlist contains only fact types that are
inputs to rules, never outputs of rules.** Extending it requires a
business decision recorded as a rule freeze, not an implementer's
judgment.

## 4.4 Business identity per fact type — FROZEN

Today's idempotency is **file-content level only** (`sha256`), which is
necessary and insufficient: the same business fact arriving from a
revised or differently-scoped file is created twice. Frozen principle:
**backfill idempotency keys on a declared business identity per fact
type, not on the source file hash.**

Identity is always evaluated against **current revision values** (1.3).

| Fact type | Business identity | Null policy | Conflict policy |
|---|---|---|---|
| `Contract` | `(contract_no, counterparty)` — the selector `_ContractResolver` already uses | Both required. Either missing → not backfillable; Evidence only | `contract_no` is deliberately non-unique, so >1 match is legitimate → **Task, never a guess** (R004 / existing resolver behaviour) |
| `ContractItem` | `(contract_id, source_item_key)` | `source_item_key` **required** for all new intake and all backfill — the Fact Pack already enforces this. Pre-existing null rows are **not eligible** for identity matching and surface as unresolved rather than being guessed at | Duplicate key → resolve to the existing anchor (existing skip behaviour) |
| `Invoice` | `external_invoice_key` | Required for backfill. Null → not dedupable → **Task** | Unique constraint enforces |
| `Payment` | `(transaction_date, direction, amount, bank_reference)` | `bank_reference` null → identity **incomplete** → **Task, never silent dedup** | Same key, different Evidence → **Task** |
| `Shipment` | `(contract_id, external_reference, execution_date)` | `external_reference` null → identity incomplete → requires human confirmation | Same key, different Evidence → **Task** |
| `SalesContract` | `(our_entity, sales_contract_no)` | `sales_contract_no` missing → **no canonical anchor may be created**; the material stays Evidence plus a `Task`. `our_entity` missing → likewise **no silent anchor**. `customer` missing → **anchor is created**, `customer` NULL, with a `Task` | Conflicting business facts under one identity → `BusinessKeyConflict` / `Task`. Never auto-merged |
| `ProcurementSalesLink` | **Relationship business key** `(procurement_contract_id, sales_contract_id)`; each row is one **assertion episode** | Neither end may be empty | **At most one current episode per business key**, history may hold several. Same `source_fragment_id` for the same key → **idempotent**, nothing written. Replaying historical Evidence → **never produces a current episode**, so a re-run cannot revive a retired relationship. Evidence conflicting with a current episode → **Task**; never overwrite, never re-point. A retired key becomes current again only through an explicit `HUMAN_CONFIRMED` **REESTABLISH** with new Evidence, which writes a **new** episode and leaves the retired one retired |

**Why `(our_entity, sales_contract_no)` and not `sales_contract_no`
alone.** This project already refuses to assume a contract number is
globally unique — `Contract.contract_no` is deliberately non-unique in
the schema, with duplicates surfacing as `BusinessKeyConflict` rather
than merging. Applying the same discipline, a sales contract number
alone is not a safe anchor. The procurement identity pairs the number
with the *other* party (`counterparty`); the sales leg cannot mirror
that, because `customer` is allowed to be unknown when a scope is first
learned (2.3) and an identity may never depend on a value that legally
arrives later. `our_entity` is the party that **is** known at that
moment, which makes it the correct second element.

`our_entity` must come from Evidence, never from a guess. Two legitimate
sources: the sales contract document itself, which names our
contracting party; or a procurement record that asserts our entity and
the sales-scope reference **on the same evidence fragment** — reading
two fields off one record is not an inference. Where both later exist
and disagree, that is a conflict under the policy above, not a silent
preference for one.

**`Payment` identity is known to be weak, and this is stated rather than
papered over.** `Payment` has no source-account field, and
`bank_reference` is nullable and non-unique, so the composite above
cannot distinguish two genuinely different transactions that share date,
direction and amount on different accounts. The robust fix is to add a
source-account identifier to `Payment` — a named **R5 migration** and a
**DEFERRED IMPLEMENTATION DECISION**. Until it exists, the composite
above is the frozen key and every incomplete or colliding case produces
a Task instead of a silent merge.

**Identity-bearing fields are correctable, and that is a special case.**
Correcting `contract_no` or `counterparty` on a `Contract`, or
`source_item_key` on a `ContractItem`, is **re-identification, not a
plain correction**: it changes which business thing the anchor denotes.
Frozen: such a correction always produces a **Task** and is never
applied silently, because it can merge or split business identities and
therefore affects every allocation and derived record hanging off that
anchor.

Backfill must be safely re-runnable: re-running must not duplicate Facts
and must not resurrect superseded revisions. The deduplication
*algorithm* is deferred; the identities, null policies and conflict
policies above are frozen.

## 4.5 Cutover Baseline — FROZEN

```
Legacy data  +  Source Evidence  +  Business-confirmed interpretation
                          =
                   Cutover Baseline
```

Private acceptance material, living under
`$BEL_PRIVATE_DATA_ROOT/<period>/expected/`, the existing home for
private expected-results material. No change to
[PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) required.

## 4.6 Reconciliation scope and outcomes — FROZEN

Outcomes unchanged: `MATCH` · `BEL_CORRECTED_LEGACY` · `UNRESOLVED`,
gated at `UNRESOLVED = 0`, meaning every difference has been
adjudicated — not that BEL agrees with the spreadsheet everywhere.

**Reconciliation targets first-stage authoritative business conclusions,
not every legacy cell.** A legacy column with no BEL counterpart is not
automatically a discrepancy; in many cases it is a deliberate scope
decision.

In scope: contract execution state, accrual and period-close
conclusions, outbound invoicing preparation conclusions, and
unresolved-work/exception conclusions.

Out of scope: presentational columns, free-text notes, working columns,
and anything deliberately excluded by frozen V1 scope (for example
rebate-domain fields, per [V1-SCOPE.md](V1-SCOPE.md) section 6.1).

Which specific conclusions are compared, and their tolerances, is a
**DEFERRED IMPLEMENTATION DECISION** for R5. Public output remains
scenario ID plus PASS/FAIL; all values, counts and mismatch detail stay
under `$BEL_PRIVATE_DATA_ROOT/reports/` (P06).

---

# Readiness assessment

| Round | Ready? | What is frozen for it |
|---|---|---|
| **R1** — ContractItem Fact Maintenance | **YES** | Correction taxonomy (1.1), Evidence rule (1.2), anchor + revision model with identity/provenance reference rules (1.3), per-object applicability (1.4), five-step recomputation policy (1.5). R1 owns the revision-table migration and the repository assembly that keeps the Rule Engine untouched |
| **R2** — Shipment slice | **YES** | Object, canonical name, minimal fields (3.2), association cardinality (3.3), the single cost-recognition trace model (3.4), business identity (4.4). R2 keeps the procurement `contract_id` only, is not the bridge (3.5), must not auto-derive cost recognition, and must not treat shipped quantity as invoicing input (3.6) |
| **R3a** — Sales Scope & Bridge | **YES** | `SalesContract` semantics and minimum fields (2.2); external customer identity sourcing and the customer-arrives-later path (2.3); `ProcurementSalesLink` fields, confirmation semantics, Evidence trace, identity and idempotency/conflict policy (2.4); **relationship supersession and invalidation, with a recommended append-only correction record, deterministic current-link selection, atomic replacement and the no-resurrection rule** (2.4); `SalesContract` identity, null and conflict policy (4.4); `SalesContract` correction reuse via the same anchor + revision model (1.4) |
| **R3b** — Sales-side Allocation | **YES** | `SalesInvoiceAllocation` and `SalesPaymentAllocation` minimum semantics, `MatchCase` reuse with `SalesMatchCandidate`, and the two required guards (2.7); association targets (2.5, 2.6); manual/human-confirmed path sufficient for the first version; the automatic-algorithm boundary (2.8). R3b is deliberately scoped to be complete **without** the deferred sales matching algorithm |
| **R5** — Backfill | **DESIGN READY** | Legacy role (4.1), Facts-not-derived-state rule (4.2), the closed cutover-fact allowlist (4.3), and a complete identity/null/conflict policy for **every** fact type including `SalesContract` and `ProcurementSalesLink` (4.4), Cutover Baseline (4.5), reconciliation outcomes and scope (4.6). Implementation additionally needs the `Payment` source-account migration and R1/R2/R3a/R3b to land |

No round in this table is blocked on an unanswered business question.
The items still marked `REQUIRES BUSINESS RULE FREEZE` below are all
scoped **outside** the rounds above — each round is designed to be
deliverable without them.

# Consolidated deferred items

**DEFERRED IMPLEMENTATION DECISION** — revision-table migration and
repository assembly (R1) · supersession mechanism for the
manually-confirmed fact family (R1) · which derived records are scanned
on supersession and the Task payload (R1) · database-level Evidence
immutability enforcement · bad-import remediation for external facts ·
exception codes and payloads for the sales-scope and link Tasks (R3a) ·
the two `confirm_match` / listing guards' implementation detail (R3b) ·
`Payment` source-account field (R5 migration) · backfill deduplication algorithm (R5) · reconciliation
comparison set and tolerances (R5)

**REQUIRES BUSINESS RULE FREEZE** — sales-side amount/matching algorithm
(2.8) · receipt granularity, scope-level vs invoice-level (2.6) ·
cross-bridge apportionment of amounts or quantities (2.4) · whether a
`Shipment` automatically implies cost recognition (3.4) · outbound
invoicing eligibility, including what determines an invoiceable quantity
(3.6, Phase 2D.3)

# SPEC_CHANGE_REQUEST

## SCR-2D1R0-001 — Sales-side party semantics and sales scope object

**Status: APPROVED** by the business owner during Phase 2D.1-R0, and
applied under that approval. Raised because business confirmation of the
party roles (2.1) contradicted text already frozen in
`docs/V1-SCOPE.md` and `docs/PHASE2D0-DECISIONS.md`.

### Conflict

Four passages instructed the sales side to associate a customer to
`Contract.buyer`:

1. `V1-SCOPE.md` section 3.1 — "the sales side must associate a
   customer/buyer to a contract's `buyer`"
2. `V1-SCOPE.md` section 5 item 1 — the Ledger column list
   "counterparty/supplier, buyer/customer"
3. `PHASE2D0-DECISIONS.md`, "Why the sales-side gap is a semantics
   freeze" — same instruction
4. `PHASE2D0-DECISIONS.md` Code Reality item C — "the sales side must
   associate customer/buyer to `Contract.buyer`"

`Contract.buyer` is our own trading/export entity. Implementing any of
these would attribute every sales invoice and customer receipt to our
own company — a silent, systematic error in authoritative business
state. The conflict is factual, not editorial.

### Changes applied under approval

| # | Document | Change |
|---|---|---|
| 1 | `V1-SCOPE.md` 3.1 | Corrected: procurement association unchanged; `Contract.buyer` identified as our own entity and prohibited as a customer key; the earlier statement marked wrong; customer sourced from `SalesContract` |
| 2 | `V1-SCOPE.md` 2 | Object list gains `SalesContract` and `ProcurementSalesLink`; `Contract` annotated as the procurement leg |
| 3 | `V1-SCOPE.md` 3 | Match types gain `SalesContract ↔ Invoice`, `SalesContract ↔ Payment`, `Contract ↔ SalesContract` |
| 4 | `V1-SCOPE.md` 5 item 1 | Ledger columns separate our own contracting entity from the external customer; the procurement axis and the no-summing-across-the-bridge rule are stated |
| 5 | `V1-SCOPE.md` 2.4 | Correction semantics status updated from "not frozen" to frozen in R0, with a forward reference |
| 6 | `V1-SCOPE.md` 2.5 | **New section** — sales-side scope and bridge semantics |
| 7 | `PHASE2D0-DECISIONS.md` | **Two additive forward clarifications only.** No historical conclusion rewritten: the Phase 2D.0 finding that the sales-side pipeline does not exist stands, and its judgment that this was a semantics freeze rather than a parameter change stands and was understated |
| 8 | `DOMAIN.md` | **New sections** for `SalesContract` (including `our_entity` and the `(our_entity, sales_contract_no)` identity) and `ProcurementSalesLink` (including `source_fragment_id`, `confirmation_type`, and the confirmed-only / idempotent-pair semantics), plus a statement that sales-side allocation objects are physically separate from procurement ones. Business semantics and relationships only — no schema or implementation detail |

### Impact

- **Domain** — two new objects. `Contract`, `ContractItem`, `Invoice`,
  `Payment`, `Accrual` and every existing semantic are unchanged.
- **Rules** — `RULES.md` **unchanged**; R001–R015 untouched. Procurement
  `M001` unchanged. No numbered rule is added for the sales side, whose
  algorithm still awaits a freeze.
- **Architecture** — `ARCHITECTURE.md` **unchanged**; A01–A05 intact.
- **Existing implementation** — procurement semantics are **unchanged**:
  `InvoiceAllocation`, `PaymentAllocation` and `MatchCandidate` keep
  their hard `contracts.id` foreign keys and are not generalised;
  `matching.py`'s M001 pass needs no contract-type filter because the
  legs are physically separate tables; `period_close.py` is unaffected.
  The one addition R3b must make is **defensive, not semantic**: a
  rejection guard in `confirm_match`, which today has no leg check and
  would otherwise let a human attribute a `SALES` invoice to a
  procurement contract (2.7).
- **Schema** — R3a adds `SalesContract` (with its revision table),
  `ProcurementSalesLink`, and `ProcurementSalesLinkCorrection`, plus a
  nullable sales-side reference on `Shipment`. R3b adds
  `SalesInvoiceAllocation`, `SalesPaymentAllocation` and
  `SalesMatchCandidate`. **`MatchCase` is reused unchanged — no schema
  change to it.** No existing column on any object is altered, and the
  procurement `InvoiceAllocation`, `PaymentAllocation` and
  `MatchCandidate` tables are untouched.
- **Roadmap** — R3 split into R3a (scope and bridge) and R3b
  (allocation).
- **Cutover** — net positive: the procurement ledger's sales-scope
  reference column, previously read only for a completeness statistic,
  becomes the backfill basis for the bridge.

### Not requested

No change to `ARCHITECTURE.md` or `RULES.md` was needed or made.
