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
  be determined from the Contract Fact. This does NOT by itself mean
  "the supplier should invoice now"; it is a statement about fact
  completeness, nothing more.
- ``INSUFFICIENT_FACTS`` — the expected amount could NOT be determined
  (the Contract's gross amount is unknown).
- ``RULE_CONFLICT`` — a frozen rule (IP-P03 / IP-P04) is violated by the
  current Facts.

Status precedence when several apply: ``RULE_CONFLICT`` >
``INSUFFICIENT_FACTS`` > ``PREPARATION_AMOUNT_DETERMINABLE``. A frozen
rule violation is never masked by fact incompleteness; the violated
facts remain exposed on the decision either way.

Check results (exact, never tolerant): ``MATCH`` / ``MISMATCH`` /
``NOT_COMPARABLE_MISSING_FACT``. An amount or product-name MISMATCH is
a factual comparison outcome — it is never called "unpaid",
"outstanding", or "overdue", and it does not by itself change the
decision status. Amount comparisons reuse the existing canonical
semantics (M001 compares ``Invoice.gross_amount`` to
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
    A status here states whether the IP-P02 expected amount could be
    determined, or that a frozen rule is violated — nothing more."""

    PREPARATION_AMOUNT_DETERMINABLE = "PREPARATION_AMOUNT_DETERMINABLE"
    INSUFFICIENT_FACTS = "INSUFFICIENT_FACTS"
    RULE_CONFLICT = "RULE_CONFLICT"


class SupplierRequestBlockerCode:
    """Explicit blocker codes. Violation codes state which frozen rule
    the current Facts conflict with; the missing-fact code states which
    Fact could not be found. Codes never state what should be done about
    it (that is the unfrozen business decision)."""

    # IP-P02 missing fact: the Contract's gross amount is unknown, so no
    # expected purchase invoice gross amount can be prepared. (Today's
    # schema backstop
    # ck_contract_revisions_current_requires_amount_currency makes an
    # unknown current amount unreachable in storage; the rule stays
    # deterministic should the Domain ever carry an unknown amount.)
    MISSING_CONTRACT_GROSS_AMOUNT = "MISSING_CONTRACT_GROSS_AMOUNT"
    # IP-P03 violation: more than one PURCHASE invoice is currently
    # allocated to this procurement Contract. Factual state only — no
    # historical Fact is deleted or mutated, and nothing is claimed to
    # be "overdue".
    MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT = "MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT"
    # IP-P04 violation: one PURCHASE invoice is currently allocated to
    # more than one procurement Contract. The invoice is never silently
    # apportioned.
    PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS = "PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS"


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
    """One explicit blocker on a procurement Contract scope — a missing
    Fact (IP-P02) or a frozen-rule violation (IP-P03 / IP-P04)."""

    code: str
    # The procurement Contract scope the blocker is emitted on.
    contract_id: uuid.UUID
    # PURCHASE invoice ids the blocker is about (empty for the
    # missing-contract-amount case).
    related_invoice_ids: tuple[uuid.UUID, ...] = ()
    # Procurement Contract ids involved (IP-P04: every contract the
    # offending invoice is currently allocated to, this scope's included).
    related_contract_ids: tuple[uuid.UUID, ...] = ()
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
    Carries facts, the IP-P02 expected amount, check results and
    blockers only — no readiness/eligibility field, no requested
    quantity, no tax-rate recommendation.

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
    # equality; no tolerance. A dangling association (no Invoice Fact)
    # is NOT_COMPARABLE_MISSING_FACT, never a guessed amount.
    amount_checks: list[SupplierRequestAmountCheck] = []
    if len(invoice_ids_in_scope) == 1:
        invoice_id = invoice_ids_in_scope[0]
        invoice_fact = next(
            (entry.invoice for entry in scope.invoice_allocations if entry.allocation.invoice_id == invoice_id),
            None,
        )
        if invoice_fact is None or contract.gross_amount is None:
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount if invoice_fact is not None else None,
                    contract_gross_amount=contract.gross_amount,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        else:
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    outcome=(
                        SupplierRequestCheckOutcome.MATCH
                        if invoice_fact.gross_amount == contract.gross_amount
                        else SupplierRequestCheckOutcome.MISMATCH
                    ),
                )
            )

    # E. IP-P05 item-name consistency — for every current item
    # association on this Contract's ContractItems (the F0 context
    # direction-isolates these to PURCHASE parents). EXACT equality of
    # the two confirmed product names; a missing name or missing
    # InvoiceItem Fact is NOT_COMPARABLE_MISSING_FACT. No fuzzy match,
    # no normalization beyond the Domain's frozen one (counterparty
    # only), no HS/tax-classification inference.
    item_by_id = {item.id: item for item in scope.items}
    item_name_checks: list[SupplierRequestItemNameCheck] = []
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

    # F. IP-P01 — OUT payment associations are exposed as context only
    # (below). They never gate the request and never change the status:
    # no readiness is derived from payment anywhere above or below.
    # G. IP-P06 — no tax rate is produced anywhere in this decision; an
    # actual InvoiceItem's tax_rate is reachable only through the
    # exposed invoice_item_allocations Facts.
    # H. IP-P07 — no quantity is calculated anywhere; the quantity basis
    # is an unresolved safe blocker (docs/PHASE2D3-RULE-FREEZE.md).

    if any(
        b.code
        in (
            SupplierRequestBlockerCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT,
            SupplierRequestBlockerCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS,
        )
        for b in blockers
    ):
        status = SupplierRequestDecisionStatus.RULE_CONFLICT
    elif expected_amount is None:
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
    )
