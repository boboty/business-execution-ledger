"""SUPPLIER_INVOICE_REQUEST rule foundation (Phase 2D.3-F1b).

Formally establishes the supplier-direction preparation rule layer on
top of the F0 fact context. Primary axis: procurement ``Contract``.
The rule layer consumes ONLY already-confirmed / current Facts (via
``get_invoice_preparation_context``) and produces a Decision per
procurement Contract scope — the Fact -> Decision layering is
preserved: this module never writes, never mutates Facts, and never
re-derives "current" semantics.

Rule provenance lives in ``docs/PHASE2D3-RULE-FREEZE.md``. The frozen
rules implemented here, by ID:

- IP-P02 (``ACCOUNTANT_CONFIRMED``): the expected supplier PURCHASE
  invoice gross amount is the procurement Contract gross amount. A
  preparation amount — not an accounting value, not a tax calculation.
- IP-P03 (``ACCOUNTANT_CONFIRMED``): one procurement Contract is not
  expected to be split across multiple PURCHASE invoices. More than one
  currently-allocated PURCHASE invoice emits a deterministic violation
  blocker. Historical Facts are never deleted or mutated by this.
- IP-P04 (``ACCOUNTANT_CONFIRMED``): one supplier PURCHASE invoice must
  not cover multiple procurement Contracts. The complete F0 context's
  factual invoice -> contracts map is built and any invoice currently
  allocated to more than one procurement Contract emits a deterministic
  violation blocker. The invoice is never silently apportioned.
- IP-P05 (``ACCOUNTANT_CONFIRMED``): where item facts exist, supplier
  invoice product naming must match the confirmed procurement product
  naming — compared here as EXACT equality of the two confirmed product
  names. No fuzzy matching; no normalization beyond what the Domain
  already freezes (``bel.domain.normalize`` freezes COUNTERPARTY
  normalization only — no product-name normalization exists, so raw
  exact equality is used); no inference from HS / tax classification
  codes.
- IP-P01 (``ACCOUNTANT_CONFIRMED``): OUT payment facts are exposed as
  context only. Payment is NOT a gate: nothing here requires payment
  before a request, and no readiness is derived from payment.
- IP-P06 (``ACCOUNTANT_CONFIRMED``): no tax-rate inference. An actual
  PURCHASE InvoiceItem's ``tax_rate`` is reachable only as an existing
  Fact (through the exposed item associations); no requested,
  recommended, or inferred tax rate exists anywhere in this module.
- IP-P07 (``UNRESOLVED_SAFE_BLOCKER``): the quantity basis (contract /
  shipped / declared precedence) is NOT frozen — no requested quantity
  is calculated anywhere, and no such field exists in any DTO.

Decision status vocabulary (fact/preparation vocabulary, deliberately
NOT an eligibility vocabulary — no READY / ELIGIBLE / OVERDUE /
SHOULD_HAVE_INVOICED / PAYMENT_REQUIRED / TAX_RATE_RECOMMENDED member):

- ``PREPARATION_AMOUNT_DETERMINABLE`` — the IP-P02 expected amount could
  be determined from the Contract Fact (and no frozen check is in
  conflict or blocked on a missing Fact). This does NOT by itself mean
  "the supplier should invoice now"; it is a statement about fact
  completeness, nothing more.
- ``INSUFFICIENT_FACTS`` — the expected amount could NOT be determined,
  OR a comparison required by an existing association could not be
  performed because the compared Fact/value is absent (an explicit
  missing-fact blocker is emitted for each such gap).
- ``RULE_CONFLICT`` — a frozen rule is violated by the current Facts:
  IP-P03 / IP-P04 cardinality, an IP-P02 amount MISMATCH
  (``PURCHASE_INVOICE_AMOUNT_MISMATCH``), or an IP-P05 product-name
  MISMATCH (``PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH``).

Status precedence when several apply: ``RULE_CONFLICT`` >
``INSUFFICIENT_FACTS`` > ``PREPARATION_AMOUNT_DETERMINABLE``. A frozen
rule conflict is never masked by fact incompleteness; the violated
facts remain exposed on the decision either way.

Advisory / blocker separation (Phase 2D.3-F1d): a Decision carries
exactly two finding channels. ``blockers`` are hard findings — the
frozen-rule conflicts and missing compared Facts the status precedence
is derived from. ``advisories`` are explicit NON-BLOCKING findings:
they record a frozen accountant-confirmed rule consequence that is
factual context and never a gate (IP-P01: OUT payment Facts present;
IP-P06: an actual InvoiceItem tax_rate reachable as an existing Fact).
An advisory NEVER affects ``status`` — status is a function of blockers
alone, so a scope with advisories and no blockers is still
``PREPARATION_AMOUNT_DETERMINABLE``. Advisories follow the same
discipline as blockers: they state what the Facts are, never what
should be done about them (no "should"/"recommend"/"must pay").

Check results (exact, never tolerant): ``MATCH`` / ``MISMATCH`` /
``NOT_COMPARABLE_MISSING_FACT``. A MISMATCH means the confirmed Facts
conflict with a frozen accountant-confirmed rule (IP-P02 / IP-P05): it
emits the corresponding mismatch blocker and makes the scope decision
``RULE_CONFLICT``. It is never worded as "unpaid", "outstanding", or
"overdue". ``NOT_COMPARABLE_MISSING_FACT`` is NOT a rule conflict: it
emits an explicit missing-fact blocker and makes the scope decision at
least ``INSUFFICIENT_FACTS``. Amount comparisons reuse the existing
canonical semantics (M001 compares ``Invoice.gross_amount`` to
``Contract.gross_amount``; the confirmed allocation carries that same
amount) with exact ``Decimal`` equality — no tolerance is invented.

Strictly read-only: evaluation is a pure function of the F0 context.
The session entry point builds the context UNFILTERED — the IP-P04
mapping must span the complete F0 context, so an axis filter must never
blind it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from bel.application.invoice_preparation import (
    InvoicePreparationContext,
    SupplierScopeContext,
    SupplierScopeInvoiceAllocation,
    SupplierScopeInvoiceItemAllocation,
    SupplierScopePaymentAllocation,
    get_invoice_preparation_context,
)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class SupplierRequestDecisionStatus:
    """Fact/preparation vocabulary, deliberately NOT an eligibility
    vocabulary: there is no READY / ELIGIBLE / BLOCKED member, and none
    of the rejected business-judgment members (OVERDUE,
    SHOULD_HAVE_INVOICED, PAYMENT_REQUIRED, TAX_RATE_RECOMMENDED) exist.
    A status here states whether the frozen checks could be satisfied
    determinatively — expected amount determinable, facts insufficient,
    or a frozen rule in conflict — nothing more."""

    PREPARATION_AMOUNT_DETERMINABLE = "PREPARATION_AMOUNT_DETERMINABLE"
    INSUFFICIENT_FACTS = "INSUFFICIENT_FACTS"
    RULE_CONFLICT = "RULE_CONFLICT"


class SupplierRequestBlockerCode:
    """Explicit blocker codes. Violation codes state which frozen
    accountant-confirmed rule the current Facts conflict with
    (RULE_CONFLICT); missing-fact codes state which compared Fact/value
    could not be found (INSUFFICIENT_FACTS). Codes never state what
    should be done about it (that is the unfrozen business decision)."""

    # -- Missing-fact codes (never a rule conflict; status at least
    #    INSUFFICIENT_FACTS) --

    # IP-P02 missing fact: the Contract's gross amount is unknown, so no
    # expected purchase invoice gross amount can be prepared. (Today's
    # schema backstop
    # ck_contract_revisions_current_requires_amount_currency makes an
    # unknown current amount unreachable in storage; the rule stays
    # deterministic should the Domain ever carry an unknown amount.)
    MISSING_CONTRACT_GROSS_AMOUNT = "MISSING_CONTRACT_GROSS_AMOUNT"
    # IP-P02 comparison required by the confirmed association but the
    # PURCHASE Invoice Fact behind it is absent — the compared Fact does
    # not exist. (Unreachable in storage via the
    # invoice_allocations.invoice_id FK; deterministic over the F0
    # context's ``invoice is None`` shape.)
    MISSING_PURCHASE_INVOICE_FACT = "MISSING_PURCHASE_INVOICE_FACT"
    # IP-P05 comparison required by the item association but the
    # ContractItem's confirmed product name is absent.
    MISSING_CONTRACT_ITEM_PRODUCT_NAME = "MISSING_CONTRACT_ITEM_PRODUCT_NAME"
    # IP-P05 comparison required by the item association but the
    # InvoiceItem's confirmed product name is absent.
    MISSING_INVOICE_ITEM_PRODUCT_NAME = "MISSING_INVOICE_ITEM_PRODUCT_NAME"

    # -- Rule-violation codes (frozen accountant-confirmed rule
    #    conflicted by current Facts; status RULE_CONFLICT) --

    # IP-P03 violation: more than one PURCHASE invoice is currently
    # allocated to this procurement Contract ("must not be split"). No
    # historical Fact is deleted or mutated, and nothing is claimed to
    # be "overdue".
    MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT = "MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT"
    # IP-P04 violation: one PURCHASE invoice is currently allocated to
    # more than one procurement Contract. The invoice is never silently
    # apportioned.
    PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS = "PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS"
    # IP-P02 violation: the single associated PURCHASE invoice's gross
    # amount is not equal to the Contract gross amount (exact Decimal
    # comparison). A frozen-rule conflict — never worded as
    # "unpaid"/"outstanding"/"overdue".
    PURCHASE_INVOICE_AMOUNT_MISMATCH = "PURCHASE_INVOICE_AMOUNT_MISMATCH"
    # IP-P05 violation: an explicitly associated InvoiceItem and
    # ContractItem both have confirmed product names and they are not
    # exactly equal. A frozen-rule conflict — never worded as
    # "unpaid"/"outstanding"/"overdue".
    PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH = "PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH"


class SupplierRequestAdvisoryCode:
    """Explicit NON-BLOCKING finding codes (Phase 2D.3-F1d). An advisory
    records a frozen accountant-confirmed rule consequence that is
    factual context — it is emitted alongside the exposed Facts, never
    drives the decision status, and never states what should be done
    about it. ``status`` is derived from blockers only; an advisory code
    belongs to no blocker class (disjoint from
    ``RULE_VIOLATION_BLOCKER_CODES`` and ``MISSING_FACT_BLOCKER_CODES``,
    enforced by test)."""

    # IP-P01 advisory: the scope carries OUT payment Facts. Payment is
    # context, never a gate — the request is neither enabled nor blocked
    # by it, and no readiness is derived from it.
    OUT_PAYMENT_PRESENT_CONTEXT_ONLY = "OUT_PAYMENT_PRESENT_CONTEXT_ONLY"
    # IP-P06 advisory: an actual PURCHASE InvoiceItem's tax_rate is
    # reachable as an existing Fact. The Fact is displayed as it is; no
    # tax-rate recommendation or inference exists anywhere.
    EXISTING_INVOICE_ITEM_TAX_RATE_FACT = "EXISTING_INVOICE_ITEM_TAX_RATE_FACT"


# The advisory codes, defined once — exhaustive over
# SupplierRequestAdvisoryCode's members (enforced by test) and disjoint
# from both blocker classes, because an advisory never participates in
# the status precedence.
NON_BLOCKING_ADVISORY_CODES: frozenset[str] = frozenset(
    {
        SupplierRequestAdvisoryCode.OUT_PAYMENT_PRESENT_CONTEXT_ONLY,
        SupplierRequestAdvisoryCode.EXISTING_INVOICE_ITEM_TAX_RATE_FACT,
    }
)


# The two blocker classes, defined once — the decision status is derived
# from exactly this classification (RULE_CONFLICT > INSUFFICIENT_FACTS >
# PREPARATION_AMOUNT_DETERMINABLE). Exhaustive over
# SupplierRequestBlockerCode's members (enforced by test).
RULE_VIOLATION_BLOCKER_CODES: frozenset[str] = frozenset(
    {
        SupplierRequestBlockerCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT,
        SupplierRequestBlockerCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS,
        SupplierRequestBlockerCode.PURCHASE_INVOICE_AMOUNT_MISMATCH,
        SupplierRequestBlockerCode.PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH,
    }
)
MISSING_FACT_BLOCKER_CODES: frozenset[str] = frozenset(
    {
        SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT,
        SupplierRequestBlockerCode.MISSING_PURCHASE_INVOICE_FACT,
        SupplierRequestBlockerCode.MISSING_CONTRACT_ITEM_PRODUCT_NAME,
        SupplierRequestBlockerCode.MISSING_INVOICE_ITEM_PRODUCT_NAME,
    }
)


class SupplierRequestCheckOutcome:
    """Exact comparison outcomes — never tolerant, never a business
    judgment. MISMATCH is a factual outcome and is never worded as
    unpaid / outstanding / overdue."""

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_COMPARABLE_MISSING_FACT = "NOT_COMPARABLE_MISSING_FACT"


# The two check names this foundation freezes. Adding a check is a new
# rule and requires its own freeze.
AMOUNT_CONSISTENCY_CHECK_NAME = "PURCHASE_INVOICE_GROSS_AMOUNT_VS_CONTRACT_GROSS_AMOUNT"
ITEM_NAME_CONSISTENCY_CHECK_NAME = "INVOICE_ITEM_PRODUCT_NAME_VS_CONTRACT_ITEM_PRODUCT_NAME"


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierRequestBlocker:
    """One explicit blocker on a procurement Contract scope — a frozen
    rule the current Facts conflict with (IP-P02 / IP-P03 / IP-P04 /
    IP-P05 mismatches and cardinality), or a compared Fact/value a
    required comparison needs but that is absent."""

    code: str
    # The procurement Contract scope the blocker is emitted on.
    contract_id: uuid.UUID
    # PURCHASE invoice ids the blocker is about (empty when no invoice
    # is involved, e.g. the missing-contract-amount case).
    related_invoice_ids: tuple[uuid.UUID, ...] = ()
    # Procurement Contract ids involved (IP-P04: every contract the
    # offending invoice is currently allocated to, this scope's included).
    related_contract_ids: tuple[uuid.UUID, ...] = ()
    # ContractItem ids the blocker is about (missing/mismatched product
    # name on the contract side).
    related_contract_item_ids: tuple[uuid.UUID, ...] = ()
    # InvoiceItem ids the blocker is about (missing/mismatched product
    # name on the invoice side).
    related_invoice_item_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SupplierRequestAdvisory:
    """One explicit NON-BLOCKING finding on a procurement Contract scope
    (Phase 2D.3-F1d). An advisory records a frozen accountant-confirmed
    rule consequence that is factual context only (IP-P01 OUT payment
    Facts present; IP-P06 existing InvoiceItem tax_rate Fact displayed).
    Advisories NEVER affect the decision ``status`` — status is a
    function of blockers alone. Like blockers, an advisory states what
    the Facts are, never what should be done about it."""

    code: str
    # The procurement Contract scope the advisory is emitted on.
    contract_id: uuid.UUID
    # InvoiceItem ids the advisory is about (IP-P06: the items whose
    # existing tax_rate Fact is displayed).
    related_invoice_item_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SupplierRequestAmountCheck:
    """IP-P02 amount-consistency check result — the single associated
    PURCHASE invoice's gross amount against the Contract gross amount,
    exact ``Decimal`` equality (existing canonical M001 semantics; no
    tolerance invented). ``None`` compared amount(s) mean the Fact was
    missing — always ``NOT_COMPARABLE_MISSING_FACT``."""

    check_name: str
    contract_id: uuid.UUID
    invoice_id: uuid.UUID
    compared_invoice_gross_amount: Decimal | None
    contract_gross_amount: Decimal | None
    outcome: str


@dataclass(frozen=True)
class SupplierRequestItemNameCheck:
    """IP-P05 product-name check result — the InvoiceItem Fact's product
    name against its confirmed ContractItem's product name, EXACT
    equality. No fuzzy matching, no normalization beyond the Domain's
    frozen (counterparty-only) normalization, no inference from HS /
    tax classification codes."""

    check_name: str
    contract_id: uuid.UUID
    allocation_id: uuid.UUID
    contract_item_id: uuid.UUID
    invoice_item_id: uuid.UUID
    contract_product_name: str | None
    invoice_product_name: str | None
    outcome: str


@dataclass(frozen=True)
class PurchaseInvoiceContractAssociation:
    """One PURCHASE invoice association's current factual footprint —
    the invoice id and EVERY procurement Contract id it is currently
    allocated to across the complete F0 context. This is the Fact-level
    mapping IP-P04 is evaluated over (and the audit trail for each
    emitted IP-P04 blocker)."""

    invoice_id: uuid.UUID
    contract_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class SupplierInvoiceRequestDecision:
    """One procurement Contract scope's request-preparation Decision.
    Carries facts, the IP-P02 expected amount, check results, blockers
    and non-blocking advisories only — no readiness/eligibility field,
    no requested quantity, no tax-rate recommendation.

    The existing PURCHASE invoice / item / OUT payment associations are
    the F0 context's own frozen DTO entries, exposed verbatim (one
    source of truth): through them an actual InvoiceItem's ``tax_rate``
    is reachable as an existing Fact (IP-P06), and OUT payments are
    visible as context without gating anything (IP-P01)."""

    contract_id: uuid.UUID
    contract_no: str
    # The supplier — only from Contract.counterparty. None stays None
    # (unknown); supplier-name presence is NOT judged by this rule.
    supplier: str | None
    status: str
    # IP-P02 expected purchase invoice gross amount — the Contract's own
    # gross amount. ``None`` only when the Contract Fact's amount is
    # unknown (MISSING_CONTRACT_GROSS_AMOUNT blocker).
    expected_purchase_invoice_gross_amount: Decimal | None
    invoice_allocations: tuple[SupplierScopeInvoiceAllocation, ...]
    invoice_item_allocations: tuple[SupplierScopeInvoiceItemAllocation, ...]
    payment_allocations: tuple[SupplierScopePaymentAllocation, ...]
    amount_checks: tuple[SupplierRequestAmountCheck, ...] = ()
    item_name_checks: tuple[SupplierRequestItemNameCheck, ...] = ()
    blockers: tuple[SupplierRequestBlocker, ...] = ()
    # Explicit NON-BLOCKING findings (IP-P01 / IP-P06). Never affect
    # ``status`` — status is derived from blockers alone.
    advisories: tuple[SupplierRequestAdvisory, ...] = ()


@dataclass(frozen=True)
class SupplierInvoiceRequestReport:
    decisions: tuple[SupplierInvoiceRequestDecision, ...]
    # The complete factual PURCHASE invoice -> procurement Contract ids
    # mapping across the F0 context (IP-P04 input), deterministically
    # ordered (entries by invoice id; contract ids sorted).
    purchase_invoice_contract_map: tuple[PurchaseInvoiceContractAssociation, ...]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_supplier_invoice_request(session: Session) -> SupplierInvoiceRequestReport:
    """Evaluate the SUPPLIER_INVOICE_REQUEST rule foundation over the
    complete F0 fact context (unfiltered — the IP-P04 mapping must span
    every supplier scope, so an axis filter must never blind it).
    Strictly read-only."""
    context = get_invoice_preparation_context(session)
    return evaluate_supplier_invoice_request_from_context(context)


def evaluate_supplier_invoice_request_from_context(
    context: InvoicePreparationContext,
) -> SupplierInvoiceRequestReport:
    """Pure decision function over the F0 context — no session, no I/O,
    no mutation."""
    invoice_contract_map = _build_invoice_contract_map(context)
    return SupplierInvoiceRequestReport(
        decisions=tuple(
            _evaluate_scope(scope, invoice_contract_map) for scope in context.supplier_scopes
        ),
        purchase_invoice_contract_map=tuple(
            PurchaseInvoiceContractAssociation(
                invoice_id=invoice_id, contract_ids=tuple(sorted(contract_ids, key=str))
            )
            for invoice_id, contract_ids in sorted(invoice_contract_map.items(), key=lambda kv: str(kv[0]))
        ),
    )


def _build_invoice_contract_map(
    context: InvoicePreparationContext,
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """The current factual mapping from PURCHASE Invoice allocation id to
    procurement Contract ids, across the COMPLETE F0 context. Keyed by
    the allocation's invoice id (the association exists as a Fact even
    when the Invoice anchor itself is missing). First-seen contract
    order is deterministic (F0 scopes are ordered); the report re-sorts
    for full stability."""
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for scope in context.supplier_scopes:
        for entry in scope.invoice_allocations:
            contracts = mapping.setdefault(entry.allocation.invoice_id, [])
            if scope.contract.id not in contracts:
                contracts.append(scope.contract.id)
    return mapping


def _evaluate_scope(
    scope: SupplierScopeContext,
    invoice_contract_map: dict[uuid.UUID, list[uuid.UUID]],
) -> SupplierInvoiceRequestDecision:
    contract = scope.contract
    blockers: list[SupplierRequestBlocker] = []

    # A. IP-P02 — the expected purchase invoice gross amount is the
    # Contract's own gross amount. Unknown amount => explicit
    # missing-fact blocker; nothing is estimated or substituted.
    if contract.gross_amount is None:
        blockers.append(
            SupplierRequestBlocker(
                code=SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT,
                contract_id=contract.id,
            )
        )
        expected_amount: Decimal | None = None
    else:
        expected_amount = contract.gross_amount

    # B. IP-P03 — PURCHASE invoice cardinality on this Contract, from the
    # direction-isolated F0 associations. Zero is a factual state only:
    # nothing is claimed to be missing, late, or overdue. More than one
    # distinct PURCHASE invoice is a deterministic rule violation.
    invoice_ids_in_scope: list[uuid.UUID] = []
    for entry in scope.invoice_allocations:
        if entry.allocation.invoice_id not in invoice_ids_in_scope:
            invoice_ids_in_scope.append(entry.allocation.invoice_id)
    if len(invoice_ids_in_scope) > 1:
        blockers.append(
            SupplierRequestBlocker(
                code=SupplierRequestBlockerCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT,
                contract_id=contract.id,
                related_invoice_ids=tuple(invoice_ids_in_scope),
            )
        )

    # C. IP-P04 — one PURCHASE invoice must not cover multiple
    # procurement Contracts. Judged over the complete-context mapping,
    # surfaced on every involved scope. The invoice is never silently
    # apportioned and no historical Fact is touched.
    for invoice_id in invoice_ids_in_scope:
        involved = invoice_contract_map.get(invoice_id, ())
        if len(involved) > 1:
            blockers.append(
                SupplierRequestBlocker(
                    code=SupplierRequestBlockerCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS,
                    contract_id=contract.id,
                    related_invoice_ids=(invoice_id,),
                    related_contract_ids=tuple(sorted(involved, key=str)),
                )
            )

    # D. IP-P02 amount consistency — only where EXACTLY ONE PURCHASE
    # invoice is associated with this Contract. The compared amount is
    # the Invoice Fact's gross amount (the existing canonical semantics:
    # M001 matches on invoice.gross_amount == contract.gross_amount, and
    # the confirmed allocation carries that same amount). Exact Decimal
    # equality; no tolerance.
    #
    # MISMATCH conflicts with the frozen accountant-confirmed rule: the
    # PURCHASE_INVOICE_AMOUNT_MISMATCH blocker is emitted and the scope
    # becomes RULE_CONFLICT (never worded as "unpaid"/"outstanding"/
    # "overdue"). Where the comparison cannot be performed because the
    # compared Fact/value is absent, the check is
    # NOT_COMPARABLE_MISSING_FACT and an explicit missing-fact blocker
    # is emitted (the unknown Contract amount is already named by step
    # A's MISSING_CONTRACT_GROSS_AMOUNT — never duplicated here) — that
    # is fact incompleteness, NOT a rule conflict.
    amount_checks: list[SupplierRequestAmountCheck] = []
    if len(invoice_ids_in_scope) == 1:
        invoice_id = invoice_ids_in_scope[0]
        invoice_fact = next(
            (entry.invoice for entry in scope.invoice_allocations if entry.allocation.invoice_id == invoice_id),
            None,
        )
        if invoice_fact is None:
            blockers.append(
                SupplierRequestBlocker(
                    code=SupplierRequestBlockerCode.MISSING_PURCHASE_INVOICE_FACT,
                    contract_id=contract.id,
                    related_invoice_ids=(invoice_id,),
                )
            )
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=None,
                    contract_gross_amount=contract.gross_amount,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        elif contract.gross_amount is None:
            # Step A already emitted MISSING_CONTRACT_GROSS_AMOUNT for
            # this same absent value — the check result is recorded
            # without a duplicate blocker.
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=None,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        else:
            outcome = (
                SupplierRequestCheckOutcome.MATCH
                if invoice_fact.gross_amount == contract.gross_amount
                else SupplierRequestCheckOutcome.MISMATCH
            )
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    outcome=outcome,
                )
            )
            if outcome == SupplierRequestCheckOutcome.MISMATCH:
                blockers.append(
                    SupplierRequestBlocker(
                        code=SupplierRequestBlockerCode.PURCHASE_INVOICE_AMOUNT_MISMATCH,
                        contract_id=contract.id,
                        related_invoice_ids=(invoice_id,),
                    )
                )

    # E. IP-P05 item-name consistency — for every current item
    # association on this Contract's ContractItems (the F0 context
    # direction-isolates these to PURCHASE parents). EXACT equality of
    # the two confirmed product names. No fuzzy match, no normalization
    # beyond the Domain's frozen one (counterparty only), no
    # HS/tax-classification inference.
    #
    # MISMATCH conflicts with the frozen accountant-confirmed rule: the
    # PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH blocker is emitted per
    # conflicting association and the scope becomes RULE_CONFLICT
    # (never worded as "unpaid"/"outstanding"/"overdue"). A missing
    # name (or missing InvoiceItem Fact) is NOT_COMPARABLE_MISSING_FACT
    # — NOT a rule conflict; it collects an explicit missing-fact
    # blocker (deduplicated per absent side) and makes the scope at
    # least INSUFFICIENT_FACTS.
    item_by_id = {item.id: item for item in scope.items}
    item_name_checks: list[SupplierRequestItemNameCheck] = []
    missing_contract_item_name_ids: list[uuid.UUID] = []
    missing_invoice_item_name_ids: list[uuid.UUID] = []
    for entry in scope.invoice_item_allocations:
        contract_item = item_by_id.get(entry.allocation.contract_item_id)
        contract_name = contract_item.product_name if contract_item is not None else None
        invoice_name = entry.invoice_item.product_name if entry.invoice_item is not None else None
        if contract_name is None or invoice_name is None:
            outcome = SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
        else:
            outcome = (
                SupplierRequestCheckOutcome.MATCH
                if contract_name == invoice_name
                else SupplierRequestCheckOutcome.MISMATCH
            )
        item_name_checks.append(
            SupplierRequestItemNameCheck(
                check_name=ITEM_NAME_CONSISTENCY_CHECK_NAME,
                contract_id=contract.id,
                allocation_id=entry.allocation.id,
                contract_item_id=entry.allocation.contract_item_id,
                invoice_item_id=entry.allocation.invoice_item_id,
                contract_product_name=contract_name,
                invoice_product_name=invoice_name,
                outcome=outcome,
            )
        )
        if outcome == SupplierRequestCheckOutcome.MISMATCH:
            blockers.append(
                SupplierRequestBlocker(
                    code=SupplierRequestBlockerCode.PURCHASE_INVOICE_PRODUCT_NAME_MISMATCH,
                    contract_id=contract.id,
                    related_invoice_ids=(entry.invoice.id,) if entry.invoice is not None else (),
                    related_contract_item_ids=(entry.allocation.contract_item_id,),
                    related_invoice_item_ids=(entry.allocation.invoice_item_id,),
                )
            )
        elif outcome == SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT:
            if contract_name is None:
                missing_contract_item_name_ids.append(entry.allocation.contract_item_id)
            if invoice_name is None:
                missing_invoice_item_name_ids.append(entry.allocation.invoice_item_id)
    if missing_contract_item_name_ids:
        blockers.append(
            SupplierRequestBlocker(
                code=SupplierRequestBlockerCode.MISSING_CONTRACT_ITEM_PRODUCT_NAME,
                contract_id=contract.id,
                related_contract_item_ids=tuple(sorted(set(missing_contract_item_name_ids), key=str)),
            )
        )
    if missing_invoice_item_name_ids:
        blockers.append(
            SupplierRequestBlocker(
                code=SupplierRequestBlockerCode.MISSING_INVOICE_ITEM_PRODUCT_NAME,
                contract_id=contract.id,
                related_invoice_item_ids=tuple(sorted(set(missing_invoice_item_name_ids), key=str)),
            )
        )

    # F. IP-P01 — OUT payment associations are exposed as context only
    # and never gate the request. The advisory channel records the rule
    # consequence explicitly: a scope carrying OUT payment Facts gets an
    # OUT_PAYMENT_PRESENT_CONTEXT_ONLY advisory, and the status is
    # untouched — no readiness is derived from payment anywhere.
    # G. IP-P06 — no tax rate is produced or inferred anywhere in this
    # decision. An actual InvoiceItem's tax_rate is displayed only as
    # the existing Fact it is: reachable through
    # invoice_item_allocations AND named by an
    # EXISTING_INVOICE_ITEM_TAX_RATE_FACT advisory (no recommendation).
    # H. IP-P07 — no quantity is calculated anywhere; the quantity basis
    # is an unresolved safe blocker (docs/PHASE2D3-RULE-FREEZE.md).

    advisories: list[SupplierRequestAdvisory] = []
    if scope.payment_allocations:
        advisories.append(
            SupplierRequestAdvisory(
                code=SupplierRequestAdvisoryCode.OUT_PAYMENT_PRESENT_CONTEXT_ONLY,
                contract_id=contract.id,
                note="out payment Fact(s) exposed as context only; payment never gates a request (IP-P01)",
            )
        )
    tax_rate_invoice_item_ids: list[uuid.UUID] = []
    for entry in scope.invoice_item_allocations:
        if (
            entry.invoice_item is not None
            and entry.invoice_item.tax_rate is not None
            and entry.invoice_item.id not in tax_rate_invoice_item_ids
        ):
            tax_rate_invoice_item_ids.append(entry.invoice_item.id)
    if tax_rate_invoice_item_ids:
        advisories.append(
            SupplierRequestAdvisory(
                code=SupplierRequestAdvisoryCode.EXISTING_INVOICE_ITEM_TAX_RATE_FACT,
                contract_id=contract.id,
                related_invoice_item_ids=tuple(tax_rate_invoice_item_ids),
                note="existing InvoiceItem tax_rate displayed as the Fact it is; no inference, no recommendation (IP-P06)",
            )
        )

    # Status precedence: a frozen-rule conflict outranks fact
    # incompleteness, which outranks the clean determinable state. Every
    # blocker code belongs to exactly one of the two classes (enforced
    # by test), so the classification is total.
    if any(b.code in RULE_VIOLATION_BLOCKER_CODES for b in blockers):
        status = SupplierRequestDecisionStatus.RULE_CONFLICT
    elif any(b.code in MISSING_FACT_BLOCKER_CODES for b in blockers):
        status = SupplierRequestDecisionStatus.INSUFFICIENT_FACTS
    else:
        status = SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE

    return SupplierInvoiceRequestDecision(
        contract_id=contract.id,
        contract_no=contract.contract_no,
        supplier=contract.counterparty,
        status=status,
        expected_purchase_invoice_gross_amount=expected_amount,
        invoice_allocations=tuple(scope.invoice_allocations),
        invoice_item_allocations=tuple(scope.invoice_item_allocations),
        payment_allocations=tuple(scope.payment_allocations),
        amount_checks=tuple(amount_checks),
        item_name_checks=tuple(item_name_checks),
        blockers=tuple(blockers),
        advisories=tuple(advisories),
    )
