# Phase 2D.4 Acceptance — Exception & Task Center

Public acceptance criteria for Phase 2D.4. The phase is built on the
semantics frozen in `docs/PHASE2D4-DECISIONS.md`; this document freezes
the invariants that F1 (read-only Center) and F2 (Exception & Task Data
Product) must satisfy, and the F0 boundary that precedes them.

**F0 is this phase's first slice: a design / documentation / inventory
freeze.** It implements nothing. Its acceptance is: the frozen
semantics documents exist, are internally consistent, match code reality,
and change no code.

## How to run — F0 (documentation only)

```bash
python tools/privacy_scan.py --staged                    # must PASS
python tools/check_migration_immutability.py --staged    # must PASS
git diff --check                                         # must be clean
```

No full pytest run is required for a documentation-only slice unless code
changed (this slice does not). If any later F-slice changes code, the
full public suite becomes mandatory again.

## F0 frozen invariants

These invariants bind the whole phase and are inherited unchanged by
F1/F2. They are the product contract of the Center.

1. **No generic RESOLVE action.** The Center exposes no universal
   `POST /exceptions/{id}/resolve` / "mark resolved" that blindly flips a
   status. A `TaskException` or `MatchCase` may only transition through a
   source-specific, already-frozen resolution semantic
   (`PHASE2D4-DECISIONS.md` §5). The only such semantics that exist today
   are `confirm_match` / `confirm_sales_invoice_match` /
   `confirm_sales_payment_match` (MatchCase → `RESOLVED` + allocation) and
   the `SalesContractCustomerUnresolved` close on a customer SUPPLEMENT.
   A `TaskException` with no frozen resolution stays OPEN and is presented
   with `resolution_route = REVIEW_ONLY`.

2. **No new unresolved-work storage table.** The Center aggregates
   `TaskException`, `MatchCase` and (for a requested period) computed
   Period Close blockers in a **read projection** only. No
   `unresolved_work` table (or equivalent) is created to copy the three
   together. Storage semantics stay separate: `TASK_EXCEPTION` persisted,
   `MATCH_CASE` persisted, `COMPUTED_BLOCKER` computed.

3. **No R009–R012 implementation.** R009 `InvoiceUnmatched`, R010
   `PaymentUnmatched`, R011 `EvidenceMissing`, R012 `AmountMismatch` (and
   R013–R015) stay PROPOSED in `docs/RULES.md`. The Center ships over the
   existing authoritative unresolved work without them. No `TaskException`
   row is created for any of them, and their provenance is unchanged.

4. **No advisory → Task promotion.** Phase 2D.3 management advisories
   (`SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED`,
   `PURCHASE_INVOICE_AMOUNT_DEVIATION`,
   `SALES_INVOICE_AMOUNT_DEVIATION`, and every other advisory code) remain
   management advisories — computed, recomputed from current facts, never
   persisted. The Center does not list them as unresolved work. Promoting
   an advisory into a Task requires an explicit future business rule.

5. **Persisted vs computed distinction preserved.** The Center never
   renders a computed blocker as a persisted `TaskException`, and never
   mutates a persisted source as if it were a derived view.

6. **Period blocker never persisted.** Period Close blockers are computed
   for a requested period and never snapshot to storage. The Center's
   global view (no `period` parameter) contains only persisted unresolved
   sources; `period` adds the current computed blockers for that period.
   Without a requested period, no timeless blocker set is presented.

7. **Unmappable work stays visible globally.** An item with no Contract
   anchor — e.g. `SalesContractIdentityIncomplete`, backfill identity
   issues, `ShipmentIdentityIncomplete` — still appears in the Center.
   The Center does not require every item to map to a Contract.

8. **No summary-text parsing for scope.** Scope/identity is resolved only
   from structured fields (`detail` keys, repository lookups by id).
   `summary` text is never parsed to infer a Contract or any scope.

9. **Canonical source trace preserved.** Every Center item carries its
   authoritative `(source_type, source_id)` identity plus the producer
   where available; `code`, `status`, `created_at` come from the source
   object, never fabricated. Business scope ids are trace/navigation
   fields, not the item's identity.

10. **Center is a read model first.** F1 is strictly read-only. Nothing
    in the Center writes facts, relationships, confirmations, or statuses.

11. **Export uses the same neutral projection.** F2's XLSX/CSV Data
    Product is built from the same `UnresolvedWorkItem` projection that
    powers the F1 page, shared byte-for-byte by Web and CLI — never a
    second projection that can drift.

## F0 documentation acceptance

12. `docs/PHASE2D4-DECISIONS.md` inventories every currently implemented
    unresolved-work producer and classifies each as
    `TASK_EXCEPTION` / `MATCH_CASE` / `COMPUTED_BLOCKER` / management
    advisory, with the `detail` scope keys that make each mappable (or
    deliberately unmappable).
13. The frozen global identity `(source_type, source_id)`, the lifecycle
    (no generic resolve), the period-handling rule, and the
    `resolution_route` policy (real capabilities only, `REVIEW_ONLY`
    default) are stated.
14. `ROADMAP.md` Phase 2D.4 and `docs/V1-SCOPE.md` section 5.2 reflect
    current code reality: the producer inventory is not the stale
    two-type statement, and `TaskException != all unresolved work` is
    stated. History is not rewritten.
15. The two documents are internally consistent with each other, with
    `docs/V1-SCOPE.md`, `docs/ROADMAP.md`, `docs/DOMAIN.md` (Task /
    Exception) and with the source.

## Explicitly out of scope (F0, and until explicitly re-opened)

- The Center UI / Web surface (F1).
- The Exception & Task Data Product (F2).
- Any new exception producer.
- Implementing or promoting R009–R015.
- Promoting any management advisory into a Task.
- A generic Task workflow or generic resolve control.
- Any schema/migration change; a schema correction is always a new
  forward migration (`docs/PERSISTENCE-MIGRATION-POLICY.md`).

## Private real-data acceptance (procedure only)

Real-data review runs the same public checks against
`$BEL_PRIVATE_DATA_ROOT`-sourced data; private acceptance output reports
scenario ID + PASS/FAIL only (`docs/PRIVATE-DATA-POLICY.md`). No
repository artifact records source-derived values. This documentation
freeze introduces no new scenario; it only freezes semantics for the
F1/F2 slices that will run them.
