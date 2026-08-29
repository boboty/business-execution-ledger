# Phase 2D.1-R0 Acceptance — Business Semantics Freeze

Public acceptance criteria for Phase 2D.1-R0. This is a design round:
there is no new behavior to test. Acceptance verifies that the four
semantic freezes are complete, internally consistent, consistent with
the code as it actually is, and sufficient to unblock the rounds they
claim to unblock — plus the standing boundary and privacy gates.

Every claim in [PHASE2D1-R0-DECISIONS.md](PHASE2D1-R0-DECISIONS.md)
cites the source file it rests on, so each row below is re-verifiable.

## Baseline

Verify against commit **`9d22fb783a3b476efa0a14160d33d628bf54594c`**
("phase D") — the completed Phase 2D.0 rebaseline, in sync with
`origin/main`. The release owner tags this commit `v0.1.2`; if that tag
now exists it must point at this commit. If it does not yet exist, use
the commit hash directly:

```bash
git diff 9d22fb7 --stat
git diff 9d22fb7 --name-only
```

## How to run

```bash
.venv/bin/pytest
.venv/bin/python tools/privacy_scan.py --tracked
.venv/bin/python tools/privacy_scan.py --history
.venv/bin/python tools/privacy_scan.py --staged
.venv/bin/python tools/privacy_scan.py --untracked
git diff --check
git diff --stat
git diff --name-only
git status
git ls-files --others --exclude-standard    # new R0 docs are UNTRACKED
```

There is no bare `python` on the reference machine — use
`.venv/bin/python`. **`git diff` does not show the new untracked R0
documents**; every diff-based check must be read together with the
untracked listing.

---

## Gate A — Fact Correction

- [ ] **Evidence immutability stated precisely, not overstated.** The
      document says immutability is enforced at the Domain and
      repository layer (frozen dataclasses, no update API) and states
      explicitly that **no database-level immutability constraint
      exists**, listing DB-level enforcement as a deferred item. It does
      not claim the database prevents an `UPDATE`.
- [ ] Every correction requires new Evidence (imported or
      `FragmentKind.MANUAL_FACT`); no correction path writes a Fact with
      no Evidence behind it; prior Evidence rows are never modified or
      deleted.
- [ ] **Correction ≠ business progression.** Supplement, correction and
      progression are distinguished, with the rule "correction applies
      only to an asserted Fact, never to derived state", and the
      invoice-arrival example classified as progression.
- [ ] **Cross-object authoritative graph is frozen.** The document
      rejects the new-row-per-version design, explains that it would
      leave `InvoiceItemAllocation.contract_item_id`,
      `Accrual.contract_item_id`, `AccrualBasisFact.contract_item_id`
      and every `contract_id` pointing at a superseded row, and freezes
      the identity-anchor + revision model in its place.
- [ ] **Identity vs provenance references are frozen as a general rule**
      with both categories enumerated: identity references
      (`contract_id`, `contract_item_id`, `invoice_id`, `payment_id`)
      resolve to current and are never re-pointed; provenance references
      (`source_fragment_id`, `created_from_fact_id`, a revision's own
      fragment) name the exact historical artifact and are **never**
      re-pointed. The document states that an Accrual keeping its
      pointer to the revision actually used is required, not a bug.
- [ ] **`revision_type` has a defined home and constraint** — a column
      on the revision row, `INITIAL | SUPPLEMENT | CORRECTION`, exactly
      one `INITIAL` per anchor, never stored on the anchor.
- [ ] **The Rule Engine stays untouched, by construction.** The document
      names `_contract_item_to_domain` as the existing assembly seam,
      and states as a hard R1 requirement that the repository returns a
      `ContractItem` dataclass of exactly the present shape so
      `period_close.py` and all other consumers see no change.
- [ ] **No `is_current` sprawl**, and no rule inspects timestamps or
      version numbers; current-revision resolution is defined once.
- [ ] **The unique constraint is addressed.** The document states
      `UniqueConstraint("contract_id", "source_item_key")` stays on the
      anchor unchanged and no partial index is required — the earlier
      draft's partial-index requirement is gone because the model
      changed.
- [ ] **Not one mechanism for everything.** Objects differentiated by
      who asserts: `ContractItem` / `Contract` / `Shipment` use anchor +
      revisions; manually-confirmed facts use whole-fact supersession;
      `Invoice` / `InvoiceItem` / `Payment` get none (external, corrected
      by their own instruments as progression); `Accrual` /
      `AccrualReversal` get none (derived, own reversal mechanism);
      Evidence never.
- [ ] **Recomputation is frozen end-to-end, not stopped at "generate a
      Task".** All five steps are present: identify affected records by
      provenance reference; generate a Task naming superseded revision,
      superseding revision and each affected record; the record **remains
      as-is and is visibly flagged as resting on a superseded
      revision**; resolution only via a new Fact or the record's own
      reversal mechanism, never by editing it in place; the Task closes
      when the condition no longer holds.
- [ ] **Silent cascade forbidden** and stated as such.
- [ ] **R015 untouched** — still `PROPOSED`, not promoted, trigger
      condition not restated.

## Gate B — Sales-side Party Semantics

- [ ] **Party roles frozen by business confirmation, and the earlier
      recommendation visibly corrected.** 卖方 → `Contract.counterparty`
      = domestic supplier (external); 买方 → `Contract.buyer` = **our own
      trading/export entity**. The document states plainly that the
      previous draft's recommendation was wrong.
- [ ] **`Contract.buyer` is prohibited as a sales-side customer key**,
      with the consequence named: it would attribute every sales invoice
      and customer receipt to our own company.
- [ ] `Contract` is identified as representing the **procurement leg
      only**, and purchase-side matching on `counterparty` is confirmed
      unchanged and still correct.
- [ ] **`M001` is not reused**, with the reason sharpened by the party
      freeze — `Contract.gross_amount` is the procurement value of a
      contract whose buyer is ourselves. The algorithm is tagged
      `REQUIRES BUSINESS RULE FREEZE`.
- [ ] **Ambiguity path** unchanged: explicit
      matched/ambiguous/unmatched, `MatchCase` in
      `HUMAN_CONFIRMATION_REQUIRED` with real candidate rows
      (`MatchCandidate` on the procurement leg, `SalesMatchCandidate` on
      the sales leg), non-unique business keys never grounds for merging,
      `_ContractResolver`'s "rejecting, not guessing", full Evidence
      traceability.

## Gate G — Sales Scope, Customer Identity and the Bridge

The central gate for this revision. It fails if any of the sales-side
model is left to the implementer.

### G1 — Sales scope object

- [ ] Three minimal models were evaluated and **one is frozen**, not
      left as a preference: distinct `SalesContract` +
      `ProcurementSalesLink`. The other two are recorded as rejected
      with reasons.
- [ ] The rejection of "reuse `Contract` with `contract_type`" cites the
      concrete code hazard — `match_invoices` iterates
      `contract_repo.list_all()` with no type filter, so sales rows in
      the same table would become purchase-invoice candidates — and the
      positional-party problem.
- [ ] The rejection of a neutral trade-case object cites the extra
      concept, the axis mismatch with the legacy ledger, and the
      order/case-lifecycle scope risk; and notes such a view can be
      *derived* from link connectivity without an entity.
- [ ] `SalesContract` is stated to be **the only place an external
      customer is expressed**.
- [ ] `sales_contract_no` is a business key in a **separate namespace**
      from `Contract.contract_no`, with conflicting customers producing
      `BusinessKeyConflict` (R004 pattern), never a merge.
- [ ] The minimum V1 field set is given, and CRM/order-style fields
      (address, contact, credit terms, price terms, payment terms,
      Incoterms, shipping terms) and **sales line items** are explicitly
      excluded.

### G2 — External customer identity

- [ ] The canonical source is **sales-side Evidence — the export sales
      contract document itself**.
- [ ] All three wrong sources are explicitly excluded with reasons:
      `Contract.buyer` (our own entity); a sales-scope reference number
      (a reference to a scope, not a party identity); a customs-receiving
      party on shipping material (an agent, not necessarily the
      customer).
- [ ] A `SalesContract` **may exist before its customer is known**, is
      then incapable of supporting outbound invoicing, and carries an
      unresolved-customer Task. The customer is supplied later as a
      `SUPPLEMENT` revision under the section 1.1 taxonomy — reusing the
      correction machinery rather than inventing a second mechanism. It
      is never guessed.

### G3 — The bridge

- [ ] **Who holds it** is frozen: a dedicated `ProcurementSalesLink`
      object; neither leg holds a foreign key to the other.
- [ ] **Cardinality** is frozen as many-to-many, with both directions
      addressed: one sales scope ← several procurement contracts
      (allowed); one procurement contract → several sales scopes
      (allowed structurally, but cost attribution is undecidable from
      the link alone → **Task**, no chosen attribution).
- [ ] **The link carries no amount and no quantity**, and **V1 performs
      no apportionment across the bridge**, with the reason stated: on a
      many-to-many edge any spread figure would be invented.
- [ ] **`ContractItem` does not participate** in the bridge, with the
      reason (no evidence source today; no first-stage judgment needs
      it).
- [ ] **Evidence trace** is defined: each link records the Evidence that
      established it — commonly the procurement ledger row carrying the
      sales-scope reference — and whether it was deterministic or
      human-confirmed.
- [ ] **Conflict handling** is tabulated for every named situation:
      missing sales-contract Evidence, conflicting customers under one
      scope number, a shipment implying an unestablished link, and a
      sales invoice matching several scopes.
- [ ] **Ledger aggregation direction** is frozen as the **procurement
      contract axis**, justified by correspondence with the legacy
      ledger it replaces and by what cutover reconciliation needs; linked
      sales scopes are enumerated, never summed across.

### G4 — Link correction: supersession and invalidation

- [ ] **A confirmed link can leave authoritative current state.** The
      document does not stop at "conflicts produce a Task": it freezes
      relationship-level supersession, so a link later shown wrong stops
      being current.
- [ ] **All four forbidden outcomes are named and ruled out**: deleting
      the old link; overwriting its endpoint in place; raising a Task but
      leaving the wrong link current forever; and leaving the replaced
      and replacing links both current.
- [ ] **Both cases are expressible** — replacement (`A → X` becomes
      `A → Y`) and **pure invalidation** (`A → X` simply does not exist,
      with no replacement) — and the document explains why a bare
      `supersedes_link_id` on a replacement row was rejected: it cannot
      express a retirement with no replacement.
- [ ] **A definite mechanism is recommended for R3a, not left open** —
      an append-only `ProcurementSalesLinkCorrection` record with
      `superseded_link_id`, a nullable `replacement_link_id`, and a
      required `source_fragment_id` — with the `AccrualReversal`
      precedent cited and the mutate-a-status-column alternative
      explicitly considered and declined.
- [ ] **History and Evidence remain.** The retired link's endpoints and
      its original Evidence are preserved permanently; the link row is
      never mutated and never deleted.
- [ ] **Current-link selection is deterministic and shared.**
      `current(link) ⟺ no correction record names it as
      superseded_link_id`, resolved in one place, on the
      `get_accrual_balance` / `is_open_accrual` precedent. Superseded and
      invalidated links are auditable but take no part in Ledger
      projection or business judgment, and **no rule inspects timestamps
      to decide which link is newer**.
- [ ] **Correction requires Evidence.** The correction record's
      `source_fragment_id` is required, and a human correction supplies
      manual Evidence — there is no path by which clicking "remove
      relationship" retires a confirmed link with no provenance.
- [ ] **Only human confirmation changes the authoritative relationship.**
      Corrective Evidence alone does not flip authority; the current link
      stands until confirmed, and a V1 correction record is always
      `HUMAN_CONFIRMED`.
- [ ] **Additive versus corrective is a human determination.** Because
      the bridge is many-to-many, several current links from one
      procurement contract are legitimate; the system never infers that
      new Evidence *replaces* rather than *adds*, and the ambiguous case
      becomes a Task.
- [ ] **No silent dual-current.** Replacement is written atomically —
      link and correction record in one transaction — and the invariant
      is stated: for any pair, **at most one link is current**.
- [ ] **The identity contradiction is resolved by two layers, and the
      document says so.** The relationship **business key**
      `(procurement_contract_id, sales_contract_id)` says *which*
      relationship; a `ProcurementSalesLink` row is one **assertion
      episode**. The frozen invariant is **at most one CURRENT episode
      per business key** — explicitly **not** "only one row for the pair
      for all history".
- [ ] A business key may hold **several episodes** over time, at most one
      current and the rest permanently retired.
- [ ] **The three creation actions are frozen and never inferred**:
      `ADD` (no current and no retired episode), `CORRECT / INVALIDATE`
      (targets a current episode), `REESTABLISH` (a retired episode
      exists, none current → new Evidence + explicit `HUMAN_CONFIRMED`).
- [ ] **`REESTABLISH` is stated to be distinct from resurrection**: it
      writes a **new** episode with its own provenance; the retired
      episode stays retired permanently and nothing historical is
      reopened, re-pointed or mutated.
- [ ] **Replay protection rests on per-episode provenance**, not on
      forbidding the pair: the same `source_fragment_id` for the same
      business key is idempotent; replaying historical Evidence **never
      produces a current episode**; only new Evidence plus an explicit
      `HUMAN_CONFIRMED` re-establishment creates a further episode.
- [ ] The earlier self-contradictory "the pair may never exist twice"
      rule is **gone**, not merely qualified.

#### Correction lineage

- [ ] **Only a current assertion may be corrected.** A retired episode is
      final — never corrected again, re-pointed, or reopened; a
      correction targeting one is rejected.
- [ ] **`superseded_link_id` is semantically unique** — an episode may be
      superseded at most once, so a correction chain cannot fork.
- [ ] **Duplicate vs conflicting submissions are distinguished**: the
      same correction resubmitted is **idempotent**; a *different*
      replacement for an already-corrected `superseded_link_id` is a
      **conflict → Task / reject**, writing no second correction and
      leaving the existing lineage unaltered.
- [ ] **The replacement-already-current boundary is settled**, leaving
      nothing to guess: if the replacement business key already has a
      current episode the correction **references it** (no duplicate
      episode); if it has none, a new confirmed replacement assertion is
      created **in the same transaction**.
- [ ] **The transactional obligation is stated as an obligation on R3a**,
      with all five ordered steps (verify still current; verify no
      existing correction; resolve or create the replacement; write the
      correction; commit) and the two storage-level invariants
      (`superseded_link_id` unique; one current episode per business
      key).
- [ ] **No document claims the codebase already serialises this class of
      write** — no link implementation exists yet, and the text says the
      invariants are R3a's to enforce.
- [ ] Correction history is **append-only: no delete, no endpoint
      overwrite, no lineage branching.**
- [ ] The correction applicability table in section 1.4 describes
      `ProcurementSalesLink` as **relationship-level supersession /
      invalidation with no in-place endpoint overwrite** — not as "not
      versioned, conflicts go to a Task" — and states that a Task is the
      work entry for ambiguity, not the correction semantics itself.

### G5 — Sales-side allocation and MatchCase reuse

- [ ] **Procurement allocation objects are untouched.**
      `InvoiceAllocation.contract_id`, `PaymentAllocation.contract_id`
      and `MatchCandidate.contract_id` keep their hard `contracts.id`
      foreign keys. They are **not** made polymorphic, not generalised
      into a superclass, and not re-pointed.
- [ ] **`SalesInvoiceAllocation` and `SalesPaymentAllocation` are frozen**
      with their minimum fields, targeting `sales_contract_id`.
- [ ] The separation is **structural**: a `SALES` invoice cannot be
      attributed by a procurement allocation, and an `IN` receipt cannot
      be either — because no such column exists, not because a rule
      forbids it.
- [ ] One subject may be allocated across **several** `SalesContract`s.
- [ ] R3b's first version may be **manual / human-confirmed only**, and
      that is stated as sufficient for the read models R4 needs.
- [ ] **`MatchCase` reuse is justified from code, not asserted**:
      no FK to `contracts`, no leg field, `subject_id` already
      documented as polymorphic, `subject_type` neutral between
      `INVOICE`/`PAYMENT`, `match_method` an unconstrained `String`, and
      `find_by_subject` unable to collide across legs because an invoice
      is either `PURCHASE` or `SALES` by its own field.
- [ ] **`SalesMatchCandidate` is a separate object**, justified by
      `MatchCandidateModel.contract_id` being a hard FK to
      `contracts.id`. **No generic polymorphic candidate framework** is
      introduced.
- [ ] **Both required R3b guards are named**, with the code evidence:
      (a) `confirm_match` has no direction or leg check and writes
      procurement allocations in both branches, so it must reject a
      sales-leg `MatchCase` — and this is stated as a **defensive
      rejection that does not alter M001 semantics**; (b)
      `list_match_cases` returns every case unfiltered, so leg-agnostic
      listings must not present a sales case as confirmable through the
      procurement path.

### G6 — Sales-side identity and idempotency

- [ ] **`SalesContract` identity is frozen as
      `(our_entity, sales_contract_no)`** — not left as TBD, and not
      assumed to be `sales_contract_no` alone.
- [ ] The choice is **justified from existing precedent**: this project
      already refuses to treat `Contract.contract_no` as globally
      unique, and `customer` cannot participate because it is allowed to
      be unknown when a scope is first learned.
- [ ] **`our_entity` provenance is constrained** — from the sales
      contract document, or from a procurement record asserting our
      entity and the sales-scope reference on the **same** evidence
      fragment (reading two fields off one record, not an inference).
      Never guessed. Later disagreement is a conflict, not a silent
      preference.
- [ ] **Null policy is complete**: `sales_contract_no` missing → no
      canonical anchor, Evidence + `Task`; `our_entity` missing → no
      silent anchor; `customer` missing → anchor **is** created with
      `customer` NULL plus a `Task`.
- [ ] **Conflict policy**: conflicting facts under one identity →
      `BusinessKeyConflict` / `Task`, never auto-merged.
- [ ] **`ProcurementSalesLink` identity is frozen in two layers**: the
      relationship business key
      `(procurement_contract_id, sales_contract_id)`, neither end empty,
      and one row per **confirmed assertion episode** — with **at most
      one current episode per business key** and history permitted to
      hold several.
- [ ] Within an episode the same supporting Evidence is **idempotent**;
      replaying historical Evidence **never produces a current episode**,
      so a retired assertion is not resurrected by a re-run.
- [ ] Conflicting Evidence produces a `Task` **as the work entry** and
      leaves the current assertion untouched — the Task is **not** the
      final correction mechanism. Only an explicit human-confirmed
      corrective action changes the authoritative current relationship,
      through relationship-level **supersession or invalidation**
      (Gate G4), after which the retired assertion remains auditable and
      non-current. Both replacement and pure invalidation are supported,
      and `superseded_link_id` is unique so lineage cannot fork.
- [ ] A retired business key returns to current only through new
      Evidence plus an explicit `HUMAN_CONFIRMED` **REESTABLISH**, which
      writes a **new** assertion episode rather than reviving the old
      one.
- [ ] **A link exists only for a confirmed relationship**, so the row
      needs no `OPEN`/`RESOLVED` workflow of its own — currency is
      derived from whether a correction record supersedes it;
      `confirmation_type` distinguishes `AUTO_CONFIRMED` from
      `HUMAN_CONFIRMED`.
- [ ] **Human confirmation still requires Evidence** — no link may exist
      whose only justification is that somebody clicked confirm.
- [ ] The bridge still carries **no amount, no quantity, no allocation
      ratio**, and is still never created automatically by a `Shipment`.
- [ ] Both new objects appear in the **R5 backfill identity table**
      alongside `Contract`, `ContractItem`, `Invoice`, `Payment` and
      `Shipment`, so backfill can be re-run safely.

### G7 — Association targets and R3 boundary

- [ ] `SALES` invoice → `SalesContract` via **`SalesInvoiceAllocation`**;
      **never** to a procurement `Contract`, and never through the
      procurement `InvoiceAllocation` table.
- [ ] `IN` receipt → `SalesContract` via **`SalesPaymentAllocation`** —
      an independent sales-side object that reuses the allocation
      *semantics and shape* but is physically and semantically separate.
      No document says `IN` receipts need no new structure, or that they
      use the procurement `PaymentAllocation`:

      ```
      procurement OUT payment  →  PaymentAllocation       → Contract
      sales-side IN receipt    →  SalesPaymentAllocation  → SalesContract
      ```
- [ ] Sales ambiguity candidates are **`SalesMatchCandidate`**, and
      `MatchCase` is reused unchanged where already frozen.
- [ ] No direct `Payment ↔ Invoice`; receipt granularity flagged as
      requiring a rule freeze.
- [ ] R3 is split into **R3a** (scope and bridge) and **R3b**
      (allocation), each with an explicit may-implement list.
- [ ] The may-not list is complete and explicit: no automatic amount
      matching; no cross-bridge apportionment; no `Payment ↔ Invoice`;
      no `Contract.buyer` as customer key; no scope reference treated as
      customer identity; no `Shipment` auto-creating a link; no
      sales-side item object; no invoicing eligibility judgment.

## Gate C — Shipment

- [ ] **One object and one canonical name.** `Shipment` is used
      consistently as the object name; "Shipment / Export" appears only
      as [DOMAIN.md](DOMAIN.md)'s concept heading. R2 is told not to
      introduce a second object or a second name.
- [ ] **Minimal field set**, each field purposeful, `source_fragment_id`
      required, and revision rows per the section 1.3 model.
- [ ] **No scope explosion** — customs workflow state, rebate fields,
      carrier/vessel/port/container, HS codes, tracking status and
      multi-leg itineraries excluded with a reason; `amount` excluded
      rather than added speculatively.
- [ ] **Contract cardinality** frozen: one contract → many shipments;
      one shipment → one contract in V1, with a cross-contract shipment
      producing a Task rather than a silent split; item-level optional;
      no-item-detail degrades to contract scope consistently with R007.
- [ ] Legacy export-contract-number kept as an intake anchor, explicitly
      not the canonical association, described only as a code fact.
- [ ] **The cost-recognition trace is frozen to exactly one model.** The
      document states `CostRecognitionFactModel` today carries only
      `contract_id`, `recognition_date`, `basis`, `source_fragment_id`
      and therefore cannot name a shipment; freezes a nullable
      `shipment_id` provenance FK as the single trace model; and
      **explicitly rejects the alternatives** (shared
      `source_fragment_id` — ambiguous when one fragment yields several
      shipments; a generic provenance link table — over-general) so an
      implementer has nothing to guess.
- [ ] **Auto-derivation is not assumed.** Creating a
      `CostRecognitionFact` still requires a human assertion;
      `shipment_id` records which shipment evidenced it, it does not
      create it. Whether a Shipment automatically implies cost
      recognition is `REQUIRES BUSINESS RULE FREEZE`, justified by the
      Phase 2B decision that the system does not decide which business
      behavior means cost recognition. `period_close.py` stays
      untouched.
- [ ] **`Shipment` is explicitly not the bridge.** R2 delivers the
      procurement `contract_id` only; R3a may add a nullable sales-side
      reference as an additive field; a `Shipment` **never creates a
      `ProcurementSalesLink` automatically** — an implied but
      unestablished link produces a Task; corroboration is not creation.
- [ ] **Shipment ≠ invoice eligibility**, and
      `Export completed → ready to invoice` is explicitly not frozen.
- [ ] **Shipped quantity is not frozen as invoiceable quantity.** The
      document states that V1 builds no sales-side item object and that
      a `Shipment` is an important *candidate* fact source for a future
      invoicing quantity — a design direction, explicitly **not** a
      frozen rule. What determines an invoiceable quantity belongs to
      Phase 2D.3's eligibility freeze, and neither R2 nor R3 may treat
      shipped quantity as settled invoicing input.

## Gate D — Backfill

- [ ] Legacy ledger is Evidence/migration-aid/reconciliation-input, not
      Golden Truth.
- [ ] The forbidden pattern (`legacy 已付款 → BEL PAID`) and the required
      pattern (Evidence → Facts → deterministic state) are both stated.
- [ ] **Three exhaustive outcomes** per legacy status column, with no
      fourth path by which a manual result silently becomes canonical;
      Evidence-only noted as already available because importers retain
      the full source row in `raw_data`.
- [ ] **Human-Confirmed Cutover Fact provenance constraints** complete:
      real manual Evidence with a distinguishing source type; never
      impersonating source-system Evidence, with a machine-readable
      basis; supersedable; never in public fixtures (P05).
- [ ] **The allowlist of expressible fact types is closed and
      justified.** Permitted: `ContractItem`, `HistoricalAccrualFact`,
      `CostRecognitionFact`, `AccrualBasisFact`, `InvoiceItemAllocation`
      — each listed with its **rule consumer**. Excluded: `Invoice`,
      `InvoiceItem`, `Payment` (external — must come from real
      documents), and `Accrual`, `AccrualReversal`, `InvoiceAllocation`,
      `PaymentAllocation`, `Shipment` (rule outputs / derived records).
- [ ] **The governing rule is stated**: the allowlist contains only fact
      types that are *inputs* to rules, never *outputs* — and the
      document explains that permitting a "confirmed" payment would let
      the legacy 已付款 column re-enter as a Fact under another name.
- [ ] Extending the allowlist requires a business decision recorded as a
      rule freeze, not an implementer's judgment.
- [ ] **Idempotency**: today's file-content-level `sha256` idempotency is
      named, its insufficiency explained, and business-identity keying
      frozen in its place. Identity is evaluated against current
      revision values.
- [ ] **Every fact type has a frozen identity, null policy and conflict
      policy** — `Contract`, `ContractItem`, `Invoice`, `Payment`,
      `Shipment`. No fact type is left "to be defined later".
- [ ] **`ContractItem.source_item_key` nullability is resolved**:
      required for all new intake and all backfill; pre-existing null
      rows are **not eligible** for identity matching and surface as
      unresolved rather than being guessed at.
- [ ] **`Payment` identity weakness is stated, not papered over** — no
      source-account field, `bank_reference` nullable and non-unique, so
      the frozen composite cannot separate same-date/amount/direction
      transactions on different accounts. The robust fix (a
      source-account field) is a named R5 migration, and until then
      every incomplete or colliding case produces a Task, never a silent
      merge.
- [ ] **Identity-bearing field correction is handled as
      re-identification**, always producing a Task and never applied
      silently, because it can merge or split business identities and
      affects every allocation and derived record on that anchor.
- [ ] Backfill must be re-runnable without duplicating Facts or
      resurrecting superseded revisions.
- [ ] **Cutover Baseline** defined and located under
      `$BEL_PRIVATE_DATA_ROOT/<period>/expected/`, with
      [PRIVATE-DATA-POLICY.md](PRIVATE-DATA-POLICY.md) unchanged.
- [ ] **Reconciliation outcomes** `MATCH` / `BEL_CORRECTED_LEGACY` /
      `UNRESOLVED`, gated at `UNRESOLVED = 0` meaning every difference
      adjudicated.
- [ ] **Reconciliation scope bounded** to first-stage authoritative
      conclusions, with in-scope and out-of-scope sets both listed and a
      missing counterpart explicitly not counted as a discrepancy.
- [ ] **Private boundary preserved** — scenario ID plus PASS/FAIL
      publicly; values, counts and mismatch detail only under
      `$BEL_PRIVATE_DATA_ROOT/reports/` (P06).

## Gate E — Implementation Readiness

R0's purpose is to let the next rounds start. This gate fails if a
load-bearing semantic is still ambiguous.

- [ ] A readiness verdict is given for **R1, R2, R3 and R5**, each with
      its condition named rather than implied.
- [ ] **R1 can start**: correction taxonomy, Evidence rule, anchor +
      revision model, identity/provenance reference rules, per-object
      applicability, and the full five-step recomputation policy are
      frozen; R1's migration and the repository-assembly requirement
      that keeps the Rule Engine untouched are both named.
- [ ] **R2 can start**: object, canonical name, fields, association
      cardinality, business identity, and the single cost-recognition
      trace model are frozen; both boundaries (auto cost recognition,
      invoice eligibility) are marked as not frozen.
- [ ] **All five rounds report READY**: R1, R2, R3a, R3b, and R5
      (design ready). Each entry names what is frozen for it, not merely
      that it is ready.
- [ ] **No round is blocked on an unanswered business question**, and
      the document says so explicitly — every item still tagged
      `REQUIRES BUSINESS RULE FREEZE` is scoped *outside* the ready
      rounds.
- [ ] R3b is scoped so that it can be built **without** the sales-side
      matching algorithm, which still requires a business rule freeze.
- [ ] **R5 design is ready** with identities, null and conflict policies
      and the cutover allowlist frozen; implementation blocked on the
      `Payment` source-account migration and on R1/R2/R3.
- [ ] Every deferred item appears in the consolidated list with its tag,
      and no gap is left implicit or described in vague language
      ("to be refined later", "basically covered").
- [ ] **Minimal-model discipline held.** No generic temporal database,
      event-sourcing framework, workflow engine, generic graph model,
      universal Fact superclass, MDM platform, logistics platform, or
      tax/export declaration engine. In particular the generic
      provenance link table is explicitly rejected in favour of one
      named relationship.
- [ ] **Existing precedent reused over invention** —
      `Accrual`/`AccrualReversal`'s additive-plus-derived pattern,
      `get_accrual_balance`'s single-shared-predicate pattern,
      `_contract_item_to_domain`'s assembly seam,
      `_ContractResolver`'s reject-don't-guess pattern,
      `HistoricalAccrualFact`'s go-live-fact pattern, and
      `PaymentAllocation`'s direction neutrality.

## Gate F — Boundaries, code integrity and privacy

- [ ] **`docs/ARCHITECTURE.md` and `docs/RULES.md` are byte-unchanged.**
      A01–A05 and R001–R015 are untouched, and no `PROPOSED` rule was
      promoted.
- [ ] **`docs/DOMAIN.md` changes are additive and authorised.** Exactly
      two new sections — `SalesContract` and `ProcurementSalesLink` —
      covering business semantics and relationships only, with no schema
      or implementation detail. No existing DOMAIN section was rewritten.
- [ ] **`docs/V1-SCOPE.md` changes match SCR-2D1R0-001's approved list**
      and nothing beyond it: section 2 object list, section 2.4 status,
      new section 2.5, section 3.1 correction, section 3 match types,
      section 5 Ledger columns.
- [ ] **`docs/PHASE2D0-DECISIONS.md` changes are additive only.**
      `git diff --numstat` shows insertions and **zero deletions**; both
      changes are forward clarifications that explicitly preserve the
      Phase 2D.0 conclusions they annotate.
- [ ] The SCR is recorded in
      [PHASE2D1-R0-DECISIONS.md](PHASE2D1-R0-DECISIONS.md) with its
      approval status, the four conflicting passages quoted, the applied
      change list, and the impact on Domain / Rules / Architecture /
      existing implementation / schema / roadmap / cutover.
- [ ] The SCR states accurately what existing implementation requires:
      procurement semantics unchanged and no contract-type filter needed
      in `matching.py` because the legs are physically separate, **plus**
      the one defensive guard R3b must add to `confirm_match`.
- [ ] **No document claims `DOMAIN.md` is unchanged.** The R0 decisions
      header states that DOMAIN *semantics* changed under approved
      SCR-2D1R0-001 while implementation and schema did not.
- [ ] **The correction applicability table covers the sales objects**:
      `SalesContract` reuses the same anchor + revision /
      `SUPPLEMENT` / `CORRECTION` model with **no second correction
      mechanism**, and `ProcurementSalesLink` is described as
      **relationship-level supersession / invalidation with no in-place
      endpoint overwrite** — not as "not versioned, conflicts go to a
      Task". It has no *attribute* revisions because it asserts a
      relationship rather than attributes, but the assertion itself is
      supersedable, and the table says a Task is the work entry for
      ambiguity, not the correction semantics.
- [ ] **`Shipment` business identity has exactly one status.** It is
      frozen in the R5 identity table and **does not** also appear in
      the deferred list.
- [ ] **No document implies the procurement allocation tables are
      reused for the sales leg.** Sales-side text says the allocation
      *semantics and shape* are reused, implemented by
      `SalesInvoiceAllocation` / `SalesPaymentAllocation`.
- [ ] **Sales-side multi-candidate wording names `SalesMatchCandidate`**,
      never the procurement `MatchCandidate`. References to
      `MatchCandidate` appear only where the procurement object is being
      described as unchanged.
- [ ] **The SCR's schema impact enumerates every new object**: R3a —
      `SalesContract` (+ revisions), `ProcurementSalesLink`,
      `ProcurementSalesLinkCorrection`, the nullable `Shipment`
      reference; R3b — `SalesInvoiceAllocation`,
      `SalesPaymentAllocation`, `SalesMatchCandidate`; and states
      **`MatchCase` is reused unchanged with no schema change**.
- [ ] Existing Phase 1 / 2A / 2B / 2C / 2C.2 decision and acceptance
      documents are unchanged. `README.md` is unchanged.
- [ ] No file under `src/`, `migrations/`, `tests/` or `fixtures/` is
      modified. No migration written, no schema altered, no Rule Engine
      logic changed, no UI/importer/matching implemented.
- [ ] No design routes around A01–A05: no Agent reaching storage, no
      prompt as business rule, no AI making a final
      accrual/reversal/close call, no finance/tax/ERP vocabulary in the
      Business Core, and uncertainty produces a Task.
- [ ] Every code claim in
      [PHASE2D1-R0-DECISIONS.md](PHASE2D1-R0-DECISIONS.md) is accurate
      at the baseline commit and cites its source — including the two
      emphasised findings (`current_source_fragment_id` is never updated
      and is not a supersession pointer; re-importing a revised workbook
      is not idempotent) and the `CostRecognitionFactModel` field list.
- [ ] `.venv/bin/pytest` passes with the same result as at the baseline
      commit.
- [ ] All four privacy scans report zero findings. The
      `BEL_PRIVACY_DENYLIST unset — Generic Guard only` line is the
      documented default, not a failure.
- [ ] No private name, contract number, amount, quantity, record count,
      ratio, coverage figure, or private acceptance finding appears
      anywhere. Ledger column-header constants (`合同编码`, `卖方`,
      `买方`, `金额`) are cited as public source-code facts only.
- [ ] The party-role freeze in section 2.1 is recorded as **generic
      business roles** (domestic supplier / our own trading entity) with
      no company name, entity name, or dataset-derived value attached.

## Explicitly out of scope (this round)

Implementing any of it: the revision model and its migration,
`ContractItem` maintenance, the `Shipment` object, sales-side matching,
backfill, reconciliation, or any rule listed as requiring a freeze. R0
freezes semantics only.
