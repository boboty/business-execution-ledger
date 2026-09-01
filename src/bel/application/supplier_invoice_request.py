"""SUPPLIER_INVOICE_REQUEST rule foundation (Phase 2D.3-F1b, re-leveled
in Phase 2D.3-F1d).

Formally establishes the supplier-direction preparation rule layer on
top of the F0 fact context. Primary axis: procurement ``Contract``.
The rule layer consumes ONLY already-confirmed / current Facts (via
``get_invoice_preparation_context``) and produces a Decision per
procurement Contract scope — the Fact -> Decision layering is
preserved: this module never writes, never mutates Facts, and never
re-derives "current" semantics.

The Invoice Preparation Workbench is FACT CONTROL + MANAGEMENT
REMINDERS, NOT a workflow approval engine. Its job: Facts ->
deterministic comparison -> management reminder / review signal. A
legitimate real-world business state is NEVER turned into a conflict
merely because it departs from the company's preferred management
pattern.

Rule provenance lives in ``docs/PHASE2D3-RULE-FREEZE.md``. The frozen
rules implemented here, by ID:

- IP-P02 (``ACCOUNTANT_CONFIRMED``): the expected supplier PURCHASE
  invoice gross amount is the procurement Contract gross amount, with
  the Contract's own currency as the expected currency
  (``expected_purchase_invoice_currency``, Phase 2D.3-F1e). A
  preparation amount — not an accounting value, not a tax calculation.
  The amount comparison is CURRENCY-SAFE (Phase 2D.3-F1e): it is
  evaluated ONLY when the Contract amount AND currency AND the Invoice
  amount AND currency are all explicit — no FX, no implicit
  same-currency assumption, no CNY/USD default. A single associated
  invoice whose gross amount differs from the reference (same explicit
  currency) is a management-review DEVIATION advisory
  (``PURCHASE_INVOICE_AMOUNT_DEVIATION``); both explicit currencies
  present but different is a currency deviation
  (``PURCHASE_INVOICE_CURRENCY_DEVIATION``); either way the invoice Fact
  stays valid and nothing is a rule conflict.
- IP-P03 (``ACCOUNTANT_CONFIRMED``): one procurement Contract is not
  expected to be split across multiple PURCHASE invoices. More than one
  confirmed PURCHASE invoice is a management review signal
  (``MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT`` ADVISORY) — the split is
  legitimate business state, and every Fact stays preserved. An
  allocation record whose Invoice Fact is missing (``invoice is None``)
  is NOT a confirmed PURCHASE invoice and never counts toward this.
- IP-P04 (``ACCOUNTANT_CONFIRMED``): one supplier PURCHASE invoice must
  not cover multiple procurement Contracts. An M:N association is a
  management review signal (``PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS``
  ADVISORY); the invoice is never silently apportioned, and the M:N
  relationship is not a business error.
- IP-P05 (``ACCOUNTANT_CONFIRMED``): where item facts exist, supplier
  invoice product naming should match the confirmed procurement product
  naming — compared here as EXACT equality of the two confirmed product
  names. An unequal pair is a management-review DEVIATION advisory
  (``PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION``), never a conflict.
- IP-P09 (``ACCOUNTANT_CONFIRMED``): paid but no PURCHASE invoice yet is
  a management follow-up (``SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED``
  ADVISORY; 已付款，尚未收到对应进项发票，建议催供应商开票) — emitted when
  at least one confirmed OUT Payment Fact is allocated and NO confirmed
  PURCHASE Invoice Fact is associated, and gone on recomputation once one
  is. Only confirmed Facts count: ``payment is None`` never counts as a
  confirmed OUT payment, and ``invoice is None`` never counts as
  "invoice already received".
- IP-P01 (``ACCOUNTANT_CONFIRMED``): OUT payment facts are exposed as
  context only. Payment is CONTEXT — NOT a gate, and no status/advisory
  derives from payment ordering.
- IP-P06 (``ACCOUNTANT_CONFIRMED``): no tax-rate inference. An actual
  PURCHASE InvoiceItem's ``tax_rate`` is CONTEXT — reachable only as the
  existing Fact it is; no advisory is emitted for its presence.
- IP-P07 (``UNRESOLVED``): the quantity basis (contract / shipped /
  declared precedence) is NOT frozen — no requested quantity is
  calculated anywhere, and no such field exists in any DTO.

Decision status vocabulary (fact/preparation vocabulary, deliberately
NOT an eligibility vocabulary — no READY / ELIGIBLE / OVERDUE /
SHOULD_HAVE_INVOICED / PAYMENT_REQUIRED / TAX_RATE_RECOMMENDED member):

- ``PREPARATION_AMOUNT_DETERMINABLE`` — the IP-P02 expected amount could
  be determined from the Contract Fact (and no genuinely-required data
  is missing). This does NOT by itself mean "the supplier should invoice
  now"; it is a statement about fact completeness, nothing more.
- ``INSUFFICIENT_FACTS`` — the genuinely-required preparation data (the
  Contract gross amount) is absent. Only genuine data incompleteness
  blocks; a comparison that cannot be performed because an optional
  compared Fact is absent is a ``NOT_COMPARABLE_MISSING_FACT`` check
  result and never changes status.

Blockers / advisories (Phase 2D.3-F1d re-leveling): a Decision carries
exactly two finding channels. ``blockers`` are the hard findings — the
genuinely-required data absent — and the decision ``status`` is derived
from them alone (currently exactly one blocker code exists:
``MISSING_CONTRACT_GROSS_AMOUNT``). ``advisories`` are explicit
NON-BLOCKING management reminders / review signals (IP-P09 follow-up;
IP-P02 / IP-P05 deviation; IP-P03 / IP-P04 cardinality); they NEVER
affect ``status``, so a scope with advisories and no blockers is still
``PREPARATION_AMOUNT_DETERMINABLE``, and an advisory coexisting with a
blocker leaves the blocker's status intact. The P03 / P04 / P09
advisories are computed over CONFIRMED Facts ONLY — an allocation
record whose ``invoice is None`` / ``payment is None`` is factual
context (still exposed on the decision) but is never promoted into
confirmed Invoice/Payment Fact semantics.

Check results (exact, never tolerant): ``MATCH`` / ``DEVIATION`` /
``NOT_COMPARABLE_MISSING_FACT`` / (Phase 2D.3-F1e)
``NOT_COMPARABLE_CURRENCY_MISMATCH``. A DEVIATION means the confirmed
Facts differ from the preferred reference (IP-P02 / IP-P05): it emits
the corresponding ADVISORY and never changes status — never worded as
"unpaid", "outstanding", or "overdue". ``NOT_COMPARABLE_MISSING_FACT``
and ``NOT_COMPARABLE_CURRENCY_MISMATCH`` are check results only: a
comparison that cannot be performed because a compared Fact/value is
absent — or because the two explicit currencies differ — is an optional
management comparison and does NOT make the decision
``INSUFFICIENT_FACTS`` (only ``MISSING_CONTRACT_GROSS_AMOUNT`` — the
primary preparation value — does). Amount comparisons reuse the existing
canonical semantics (M001 compares ``Invoice.gross_amount`` to
``Contract.gross_amount``; the confirmed allocation carries that same
amount) with exact ``Decimal`` equality — no tolerance is invented —
and are made ONLY under an explicit comparable currency (no implicit
same-currency assumption, no FX).

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
from bel.domain.invoice import InvoiceDirection
from bel.domain.payment import PaymentDirection

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class SupplierRequestDecisionStatus:
    """Fact/preparation vocabulary, deliberately NOT an eligibility
    vocabulary: there is no READY / ELIGIBLE / BLOCKED member, and none
    of the rejected business-judgment members (OVERDUE,
    SHOULD_HAVE_INVOICED, PAYMENT_REQUIRED, TAX_RATE_RECOMMENDED) exist.
    A status here states whether the genuinely-required preparation data
    could be determined — nothing more."""

    PREPARATION_AMOUNT_DETERMINABLE = "PREPARATION_AMOUNT_DETERMINABLE"
    INSUFFICIENT_FACTS = "INSUFFICIENT_FACTS"


class SupplierRequestBlockerCode:
    """Explicit blocker codes — the genuinely-required data that could
    not be found (INSUFFICIENT_FACTS). There is exactly one today. Codes
    never state what should be done about it (that is the unfrozen
    business decision)."""

    # IP-P02 missing fact: the Contract's gross amount is unknown, so no
    # expected purchase invoice gross amount can be prepared. (Today's
    # schema backstop
    # ck_contract_revisions_current_requires_amount_currency makes an
    # unknown current amount unreachable in storage; the rule stays
    # deterministic should the Domain ever carry an unknown amount.)
    MISSING_CONTRACT_GROSS_AMOUNT = "MISSING_CONTRACT_GROSS_AMOUNT"


class SupplierRequestAdvisoryCode:
    """Explicit NON-BLOCKING management finding codes (Phase 2D.3-F1d
    re-leveling). An advisory records a frozen accountant-confirmed rule
    consequence that is a management reminder / review signal — a
    legitimate business state management should review or follow up. It
    never drives ``status``, never blocks invoice preparation, and is
    recomputed from current Facts on every evaluation (a finding
    disappears as soon as the Facts change)."""

    # IP-P03 advisory: more than one confirmed PURCHASE invoice is
    # currently allocated to this procurement Contract (a dangling
    # allocation whose Invoice Fact is missing never counts). A
    # management review signal, NOT a violation — the split is legitimate
    # business state and every Fact stays preserved.
    MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT = "MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT"
    # IP-P04 advisory: one confirmed PURCHASE invoice is currently
    # allocated to more than one procurement Contract (an M:N
    # association). Not a violation, and the invoice is never silently
    # apportioned.
    PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS = "PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS"
    # IP-P02 deviation: the single associated PURCHASE invoice's gross
    # amount differs from the Contract gross amount (exact Decimal
    # comparison, same explicit currency). The invoice Fact stays valid;
    # this is a management review signal on the preparation amount
    # reference.
    PURCHASE_INVOICE_AMOUNT_DEVIATION = "PURCHASE_INVOICE_AMOUNT_DEVIATION"
    # IP-P02 currency deviation (Phase 2D.3-F1e): the single associated
    # PURCHASE invoice's explicit currency differs from the Contract's
    # explicit currency — the amount comparison is NOT performed (no FX,
    # no amount deviation is implied). A management review signal, never
    # a conflict; both explicit currencies are the Facts.
    PURCHASE_INVOICE_CURRENCY_DEVIATION = "PURCHASE_INVOICE_CURRENCY_DEVIATION"
    # IP-P05 deviation: an explicitly associated InvoiceItem and
    # ContractItem both have confirmed product names and they are not
    # exactly equal. A management review signal, not a violation.
    PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION = "PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION"
    # IP-P09 follow-up: at least one confirmed OUT Payment Fact is
    # currently allocated but NO confirmed PURCHASE Invoice Fact is
    # associated — paid, no invoice, recommend supplier invoice follow-up
    # (已付款，尚未收到对应进项发票，建议催供应商开票). A dangling payment
    # allocation (payment=None) never counts as a confirmed OUT payment,
    # and a dangling invoice allocation (invoice=None) never counts as
    # "invoice already received". Not overdue, not a rule conflict, not a
    # payment-required gate, not a chronology finding. Disappears on
    # recomputation once a confirmed PURCHASE invoice is associated. No
    # Task is persisted (a later stage may promote this to a Task
    # workflow).
    SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED = "SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED"


# The advisory codes, defined once — exhaustive over
# SupplierRequestAdvisoryCode's members (enforced by test) and disjoint
# from the blocker class, because an advisory never participates in the
# status derivation.
NON_BLOCKING_ADVISORY_CODES: frozenset[str] = frozenset(
    {
        SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT,
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS,
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION,
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_CURRENCY_DEVIATION,
        SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION,
        SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED,
    }
)


# The blocker class, defined once — the decision status is derived from
# exactly this classification (INSUFFICIENT_FACTS when a member is
# present, otherwise PREPARATION_AMOUNT_DETERMINABLE). Exhaustive over
# SupplierRequestBlockerCode's members (enforced by test). A code never
# overlaps the advisory codes (enforced by test).
MISSING_FACT_BLOCKER_CODES: frozenset[str] = frozenset(
    {
        SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT,
    }
)


class SupplierRequestCheckOutcome:
    """Exact comparison outcomes — never tolerant, never a business
    judgment. DEVIATION is a factual outcome and is never worded as
    unpaid / outstanding / overdue.

    Phase 2D.3-F1e (docs/PHASE2D3-RULE-FREEZE.md IP-P02): the amount
    comparison is currency-safe — it is performed ONLY when both
    currencies are explicit and exactly comparable, so a hidden
    same-currency assumption is never made. ``NOT_COMPARABLE_MISSING_FACT``
    covers any required amount/currency Fact that is absent;
    ``NOT_COMPARABLE_CURRENCY_MISMATCH`` is a NEW outcome: both explicit
    currencies present but different (no amount comparison is even
    attempted, and no amount deviation is implied). No FX conversion."""

    MATCH = "MATCH"
    DEVIATION = "DEVIATION"
    NOT_COMPARABLE_MISSING_FACT = "NOT_COMPARABLE_MISSING_FACT"
    NOT_COMPARABLE_CURRENCY_MISMATCH = "NOT_COMPARABLE_CURRENCY_MISMATCH"


# The two check names this foundation freezes. Adding a check is a new
# rule and requires its own freeze.
AMOUNT_CONSISTENCY_CHECK_NAME = "PURCHASE_INVOICE_GROSS_AMOUNT_VS_CONTRACT_GROSS_AMOUNT"
ITEM_NAME_CONSISTENCY_CHECK_NAME = "INVOICE_ITEM_PRODUCT_NAME_VS_CONTRACT_ITEM_PRODUCT_NAME"


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupplierRequestBlocker:
    """One explicit blocker on a procurement Contract scope — the
    genuinely-required preparation data that is absent
    (``MISSING_CONTRACT_GROSS_AMOUNT``)."""

    code: str
    # The procurement Contract scope the blocker is emitted on.
    contract_id: uuid.UUID
    # PURCHASE invoice ids the blocker is about (empty when no invoice
    # is involved, e.g. the missing-contract-amount case).
    related_invoice_ids: tuple[uuid.UUID, ...] = ()
    # Procurement Contract ids involved.
    related_contract_ids: tuple[uuid.UUID, ...] = ()
    # ContractItem ids the blocker is about.
    related_contract_item_ids: tuple[uuid.UUID, ...] = ()
    # InvoiceItem ids the blocker is about.
    related_invoice_item_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SupplierRequestAdvisory:
    """One explicit NON-BLOCKING management finding on a procurement
    Contract scope (Phase 2D.3-F1d re-leveling). An advisory records a
    frozen accountant-confirmed rule consequence that is a management
    reminder / review signal — legitimate business state worth a review
    or follow-up. Advisories NEVER affect the decision ``status`` —
    status is a function of blockers alone — and are recomputed from
    current Facts on every evaluation."""

    code: str
    # The procurement Contract scope the advisory is emitted on.
    contract_id: uuid.UUID
    # PURCHASE invoice ids the advisory is about (IP-P03 cardinality,
    # IP-P04 spanning, IP-P02 / IP-P05 deviation).
    related_invoice_ids: tuple[uuid.UUID, ...] = ()
    # Procurement Contract ids involved (IP-P04: every contract the
    # offending invoice is currently allocated to, this scope's included).
    related_contract_ids: tuple[uuid.UUID, ...] = ()
    # InvoiceItem ids the advisory is about (IP-P05 deviation).
    related_invoice_item_ids: tuple[uuid.UUID, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SupplierRequestAmountCheck:
    """IP-P02 amount-consistency check result — the single associated
    PURCHASE invoice's gross amount against the Contract gross amount,
    exact ``Decimal`` equality (existing canonical M001 semantics; no
    tolerance invented), evaluated ONLY when both currencies are
    explicit and exactly comparable (Phase 2D.3-F1e). ``None`` compared
    amount/currency value(s) mean the Fact was missing — always
    ``NOT_COMPARABLE_CURRENCY_MISMATCH`` when both currencies are present
    but different, otherwise ``NOT_COMPARABLE_MISSING_FACT``. Both are
    check results only (never a blocker, never a status change)."""

    check_name: str
    contract_id: uuid.UUID
    invoice_id: uuid.UUID
    compared_invoice_gross_amount: Decimal | None
    contract_gross_amount: Decimal | None
    # Phase 2D.3-F1e — the compared explicit currencies, so the monetary
    # scope of the check is explicit (no hidden same-currency assumption).
    compared_invoice_currency: str | None
    contract_currency: str | None
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
    """One confirmed PURCHASE Invoice Fact's current footprint — the
    invoice id and EVERY procurement Contract id it is currently
    allocated to across the complete F0 context. Only associations whose
    Invoice Fact exists (and is PURCHASE) are promoted into this map; a
    dangling association (``invoice is None``) is never a confirmed
    invoice and is excluded. This is the Fact-level mapping IP-P04 is
    evaluated over (and the audit trail for each emitted IP-P04
    advisory)."""

    invoice_id: uuid.UUID
    contract_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True)
class SupplierInvoiceRequestDecision:
    """One procurement Contract scope's request-preparation Decision.
    Carries facts, the IP-P02 expected amount, check results, blockers
    and non-blocking management advisories only — no readiness/
    eligibility field, no requested quantity, no tax-rate
    recommendation.

    The existing PURCHASE invoice / item / OUT payment associations are
    the F0 context's own frozen DTO entries, exposed verbatim (one
    source of truth): through them an actual InvoiceItem's ``tax_rate``
    is reachable as an existing Fact (IP-P06, CONTEXT), and OUT payments
    are visible as context without gating anything (IP-P01)."""

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
    # IP-P02 expected purchase invoice currency (Phase 2D.3-F1e) — the
    # Contract's own currency, the explicit monetary scope of the expected
    # amount. ``None`` exactly when the expected amount is itself unknown
    # (the pair moves together: no reference currency is presented for an
    # amount that cannot be prepared).
    expected_purchase_invoice_currency: str | None
    invoice_allocations: tuple[SupplierScopeInvoiceAllocation, ...]
    invoice_item_allocations: tuple[SupplierScopeInvoiceItemAllocation, ...]
    payment_allocations: tuple[SupplierScopePaymentAllocation, ...]
    amount_checks: tuple[SupplierRequestAmountCheck, ...] = ()
    item_name_checks: tuple[SupplierRequestItemNameCheck, ...] = ()
    blockers: tuple[SupplierRequestBlocker, ...] = ()
    # Explicit NON-BLOCKING management findings (IP-P09 / IP-P02 /
    # IP-P05 deviation, IP-P03 / IP-P04 cardinality). Never affect
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
    """The confirmed PURCHASE Invoice -> procurement Contract ids mapping
    across the COMPLETE F0 context (the IP-P04 input). Only associations
    whose Invoice Fact EXISTS and is direction PURCHASE are promoted into
    this confirmed map: an allocation record with ``invoice is None`` is
    factual context, NOT a confirmed Invoice Fact, so it is excluded here
    (it stays visible on the decision's ``invoice_allocations``).
    First-seen contract order is deterministic (F0 scopes are ordered);
    the report re-sorts for full stability."""
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for scope in context.supplier_scopes:
        for entry in scope.invoice_allocations:
            if entry.invoice is None or entry.invoice.direction != InvoiceDirection.PURCHASE:
                continue
            contracts = mapping.setdefault(entry.invoice.id, [])
            if scope.contract.id not in contracts:
                contracts.append(scope.contract.id)
    return mapping


def _evaluate_scope(
    scope: SupplierScopeContext,
    invoice_contract_map: dict[uuid.UUID, list[uuid.UUID]],
) -> SupplierInvoiceRequestDecision:
    contract = scope.contract
    blockers: list[SupplierRequestBlocker] = []
    advisories: list[SupplierRequestAdvisory] = []

    # A. IP-P02 — the expected purchase invoice gross amount is the
    # Contract's own gross amount. Unknown amount => explicit
    # missing-fact blocker (the ONLY blocker this rule layer emits:
    # genuinely-required preparation data absent); nothing is estimated
    # or substituted.
    if contract.gross_amount is None:
        blockers.append(
            SupplierRequestBlocker(
                code=SupplierRequestBlockerCode.MISSING_CONTRACT_GROSS_AMOUNT,
                contract_id=contract.id,
            )
        )
        expected_amount: Decimal | None = None
        # Phase 2D.3-F1e: the reference currency is the Contract's own —
        # exposed only when the expected amount itself is determinable
        # (the pair moves together; no reference currency is presented
        # for an amount that cannot be prepared).
        expected_currency: str | None = None
    else:
        expected_amount = contract.gross_amount
        expected_currency = contract.currency

    # B. IP-P03 — PURCHASE invoice cardinality on this Contract, from the
    # F0 associations (which preserve every association, missing or
    # wrong-direction, as context). Zero is a factual state only:
    # nothing is claimed to be missing, late, or overdue. More than one
    # distinct CONFIRMED PURCHASE invoice is a management review signal —
    # a split is legitimate business state, so this is an ADVISORY, never
    # a conflict, and every Fact stays preserved. The count is over
    # confirmed PURCHASE Invoice Facts ONLY: an allocation record whose
    # Invoice Fact is missing (``invoice is None``) — or present with a
    # direction that is not PURCHASE — is NOT a confirmed
    # invoice and never contributes (Codex Pre-Gate BLOCKER 1).
    #
    # ``invoice_ids_in_scope`` (raw allocation ids) is kept separately
    # for the step-D amount check, which must stay NOT_COMPARABLE when a
    # single association's Invoice Fact is absent.
    invoice_ids_in_scope: list[uuid.UUID] = []
    confirmed_invoice_ids: list[uuid.UUID] = []
    for entry in scope.invoice_allocations:
        if entry.allocation.invoice_id not in invoice_ids_in_scope:
            invoice_ids_in_scope.append(entry.allocation.invoice_id)
        if (
            entry.invoice is not None
            and entry.invoice.direction == InvoiceDirection.PURCHASE
            and entry.invoice.id not in confirmed_invoice_ids
        ):
            confirmed_invoice_ids.append(entry.invoice.id)
    if len(confirmed_invoice_ids) > 1:
        advisories.append(
            SupplierRequestAdvisory(
                code=SupplierRequestAdvisoryCode.MULTIPLE_PURCHASE_INVOICES_ON_CONTRACT,
                contract_id=contract.id,
                related_invoice_ids=tuple(confirmed_invoice_ids),
                note="multiple confirmed PURCHASE invoices on one Contract — management review, not a violation (IP-P03)",
            )
        )

    # C. IP-P04 — one PURCHASE invoice must not cover multiple
    # procurement Contracts. Judged over the complete-context CONFIRMED
    # mapping, surfaced on every involved scope. An M:N relationship is
    # not a business error: this is an ADVISORY, and the invoice is never
    # silently apportioned and no historical Fact is touched. Only
    # confirmed PURCHASE Invoice Facts participate — a dangling
    # association (``invoice is None``) never contributes to the spanning
    # check (Codex Pre-Gate BLOCKER 1).
    for invoice_id in confirmed_invoice_ids:
        involved = invoice_contract_map.get(invoice_id, ())
        if len(involved) > 1:
            advisories.append(
                SupplierRequestAdvisory(
                    code=SupplierRequestAdvisoryCode.PURCHASE_INVOICE_SPANS_MULTIPLE_CONTRACTS,
                    contract_id=contract.id,
                    related_invoice_ids=(invoice_id,),
                    related_contract_ids=tuple(sorted(involved, key=str)),
                    note="one confirmed PURCHASE invoice spans multiple Contracts — M:N association, never apportioned (IP-P04)",
                )
            )

    # D. IP-P02 amount consistency — only where EXACTLY ONE PURCHASE
    # invoice is associated with this Contract. The compared amount is
    # the Invoice Fact's gross amount (the existing canonical semantics:
    # M001 matches on invoice.gross_amount == contract.gross_amount, and
    # the confirmed allocation carries that same amount). Exact Decimal
    # equality; no tolerance.
    #
    # Phase 2D.3-F1e — the amount comparison is CURRENCY-SAFE: it is
    # evaluated ONLY when the Contract amount AND currency AND the Invoice
    # amount AND currency are all explicit. No FX conversion, no implicit
    # same-currency assumption, no CNY/USD default, and no inference from
    # buyer/seller/country or a contract currency. Outcomes:
    #
    #   - any required amount/currency Fact absent ->
    #     NOT_COMPARABLE_MISSING_FACT (a check result ONLY — never a
    #     blocker, never a status change; the unknown Contract amount is
    #     already named by step A's MISSING_CONTRACT_GROSS_AMOUNT — never
    #     duplicated here; a missing Invoice currency is an optional
    #     management comparison that simply cannot be made and never
    #     blocks the Decision);
    #   - both explicit currencies present but different ->
    #     NOT_COMPARABLE_CURRENCY_MISMATCH + PURCHASE_INVOICE_CURRENCY_DEVIATION
    #     ADVISORY (no amount comparison is attempted, no amount deviation
    #     is implied);
    #   - same explicit currency: MATCH on exact amount equality, else
    #     DEVIATION + PURCHASE_INVOICE_AMOUNT_DEVIATION ADVISORY.
    #
    # A DEVIATION (either channel) is a management-review ADVISORY — the
    # invoice Fact stays valid, never a rule conflict, never worded as
    # "unpaid"/"outstanding"/"overdue".
    amount_checks: list[SupplierRequestAmountCheck] = []
    if len(invoice_ids_in_scope) == 1:
        invoice_id = invoice_ids_in_scope[0]
        invoice_fact = next(
            (entry.invoice for entry in scope.invoice_allocations if entry.allocation.invoice_id == invoice_id),
            None,
        )
        if invoice_fact is None or invoice_fact.direction != InvoiceDirection.PURCHASE:
            # The single association's Invoice Fact is missing OR present
            # with a direction that is not PURCHASE — neither is a
            # CONFIRMED PURCHASE invoice for this surface, so the amount
            # comparison stays NOT_COMPARABLE_MISSING_FACT (Final Gate: an
            # association existing is NOT proof of a confirmed Fact; a
            # wrong-direction Fact never participates as a confirmed
            # comparison candidate). The association itself remains visible
            # as context on the decision.
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount if invoice_fact else None,
                    contract_gross_amount=contract.gross_amount,
                    compared_invoice_currency=invoice_fact.currency if invoice_fact else None,
                    contract_currency=contract.currency,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        elif contract.gross_amount is None or contract.currency is None:
            # Step A already emitted MISSING_CONTRACT_GROSS_AMOUNT for an
            # absent amount — the check result is recorded without a
            # duplicate blocker. An absent Contract currency is the same
            # class of missing Fact: the amount comparison cannot be
            # performed and nothing is inferred (F1e).
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    compared_invoice_currency=invoice_fact.currency,
                    contract_currency=contract.currency,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        elif invoice_fact.currency is None:
            # The Invoice Fact carries no explicit currency — the
            # comparison is NOT_COMPARABLE_MISSING_FACT, never MATCH/
            # DEVIATION under an implicit same-currency assumption, and
            # never a blocker: the optional management comparison just
            # cannot be made (the preparation reference remains
            # determinable from Contract.gross_amount + Contract.currency).
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    compared_invoice_currency=None,
                    contract_currency=contract.currency,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT,
                )
            )
        elif invoice_fact.currency != contract.currency:
            # Both currencies explicit but different — a currency
            # deviation (ADVISORY), never an amount comparison, never a
            # conflict, no FX conversion.
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    compared_invoice_currency=invoice_fact.currency,
                    contract_currency=contract.currency,
                    outcome=SupplierRequestCheckOutcome.NOT_COMPARABLE_CURRENCY_MISMATCH,
                )
            )
            advisories.append(
                SupplierRequestAdvisory(
                    code=SupplierRequestAdvisoryCode.PURCHASE_INVOICE_CURRENCY_DEVIATION,
                    contract_id=contract.id,
                    related_invoice_ids=(invoice_id,),
                    note="PURCHASE invoice explicit currency differs from the Contract reference — amount not compared, management review (IP-P02)",
                )
            )
        else:
            outcome = (
                SupplierRequestCheckOutcome.MATCH
                if invoice_fact.gross_amount == contract.gross_amount
                else SupplierRequestCheckOutcome.DEVIATION
            )
            amount_checks.append(
                SupplierRequestAmountCheck(
                    check_name=AMOUNT_CONSISTENCY_CHECK_NAME,
                    contract_id=contract.id,
                    invoice_id=invoice_id,
                    compared_invoice_gross_amount=invoice_fact.gross_amount,
                    contract_gross_amount=contract.gross_amount,
                    compared_invoice_currency=invoice_fact.currency,
                    contract_currency=contract.currency,
                    outcome=outcome,
                )
            )
            if outcome == SupplierRequestCheckOutcome.DEVIATION:
                advisories.append(
                    SupplierRequestAdvisory(
                        code=SupplierRequestAdvisoryCode.PURCHASE_INVOICE_AMOUNT_DEVIATION,
                        contract_id=contract.id,
                        related_invoice_ids=(invoice_id,),
                        note="PURCHASE invoice gross amount deviates from the Contract reference — management review (IP-P02)",
                    )
                )

    # E. IP-P05 item-name consistency — for every current item
    # association on this Contract's ContractItems. A confirmed comparison
    # candidate requires the InvoiceItem Fact AND a parent PURCHASE Invoice
    # (F0 preserves missing/wrong-direction associations; they are
    # NOT_COMPARABLE_MISSING_FACT here, never a MATCH/DEVIATION against a
    # wrong-direction Fact). EXACT equality of the two confirmed product
    # names. No fuzzy match, no normalization beyond the Domain's frozen
    # one (counterparty only), no HS/tax-classification inference.
    #
    # A DEVIATION is a management-review ADVISORY per conflicting
    # invoice (the invoice Fact stays valid — never a rule conflict). A
    # missing name (or missing InvoiceItem Fact) is
    # NOT_COMPARABLE_MISSING_FACT — a check result ONLY: the optional
    # management comparison is unavailable and does NOT block
    # preparation.
    item_by_id = {item.id: item for item in scope.items}
    item_name_checks: list[SupplierRequestItemNameCheck] = []
    deviation_items_by_invoice: dict[uuid.UUID, list[uuid.UUID]] = {}
    for entry in scope.invoice_item_allocations:
        contract_item = item_by_id.get(entry.allocation.contract_item_id)
        contract_name = contract_item.product_name if contract_item is not None else None
        # Final Gate: the item-name comparison candidate is CONFIRMED only
        # when the InvoiceItem Fact exists AND its parent Invoice is a
        # confirmed PURCHASE invoice. A missing InvoiceItem, a missing
        # parent Invoice, or a parent Invoice whose direction is not
        # PURCHASE is an incomplete association (NOT_COMPARABLE_MISSING_FACT)
        # — never a MATCH/DEVIATION against a wrong-direction Fact, and
        # never a blocker. The association itself stays visible as context.
        confirmed_candidate = (
            entry.invoice is not None
            and entry.invoice.direction == InvoiceDirection.PURCHASE
            and entry.invoice_item is not None
        )
        invoice_name = entry.invoice_item.product_name if confirmed_candidate else None
        if not confirmed_candidate or contract_name is None or invoice_name is None:
            outcome = SupplierRequestCheckOutcome.NOT_COMPARABLE_MISSING_FACT
        else:
            outcome = (
                SupplierRequestCheckOutcome.MATCH
                if contract_name == invoice_name
                else SupplierRequestCheckOutcome.DEVIATION
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
        if outcome == SupplierRequestCheckOutcome.DEVIATION:
            invoice_id = entry.invoice_item.invoice_id
            deviation_items_by_invoice.setdefault(invoice_id, []).append(entry.allocation.invoice_item_id)
    for invoice_id, invoice_item_ids in deviation_items_by_invoice.items():
        advisories.append(
            SupplierRequestAdvisory(
                code=SupplierRequestAdvisoryCode.PURCHASE_INVOICE_PRODUCT_NAME_DEVIATION,
                contract_id=contract.id,
                related_invoice_ids=(invoice_id,),
                related_invoice_item_ids=tuple(sorted(set(invoice_item_ids), key=str)),
                note="invoice product name deviates from the contract product name — management review (IP-P05)",
            )
        )

    # F. IP-P09 — paid but no PURCHASE invoice yet is a management
    # follow-up. At least one confirmed OUT Payment Fact currently
    # allocated AND no confirmed PURCHASE Invoice Fact associated yet =>
    # SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED. Only confirmed Facts count
    # (Codex Pre-Gate BLOCKER 1): a dangling payment allocation
    # (``payment is None``) never counts as a confirmed OUT payment, and
    # a dangling invoice allocation (``invoice is None``) never counts as
    # "invoice already received". Not overdue, not a rule conflict, not
    # payment-required, not an eligibility gate, not a chronology
    # finding. The advisory disappears on recomputation as soon as a
    # confirmed PURCHASE invoice is associated, and no Task is persisted
    # (a later stage may promote this to a Task workflow).
    # G. IP-P01 — OUT payment associations are exposed as context only
    # and never gate the request. Payment is CONTEXT: no status/advisory
    # derives from payment ordering, and there is no payment-presence
    # advisory — the only payment-derived finding is the IP-P09 follow-up
    # above (confirmed OUT payment + no confirmed PURCHASE invoice),
    # never a payment-state signal.
    # H. IP-P06 — no tax rate is produced or inferred anywhere in this
    # decision. An actual InvoiceItem's tax_rate is CONTEXT, displayed
    # only as the existing Fact it is, reachable through
    # invoice_item_allocations — no advisory is emitted for its presence.
    # I. IP-P07 — no quantity is calculated anywhere; the quantity basis
    # is unresolved (docs/PHASE2D3-RULE-FREEZE.md).
    confirmed_out_payment_ids: list[uuid.UUID] = []
    for entry in scope.payment_allocations:
        if (
            entry.payment is not None
            and entry.payment.direction == PaymentDirection.OUT
            and entry.payment.id not in confirmed_out_payment_ids
        ):
            confirmed_out_payment_ids.append(entry.payment.id)
    if confirmed_out_payment_ids and not confirmed_invoice_ids:
        advisories.append(
            SupplierRequestAdvisory(
                code=SupplierRequestAdvisoryCode.SUPPLIER_INVOICE_FOLLOW_UP_RECOMMENDED,
                contract_id=contract.id,
                note="paid, but no confirmed PURCHASE invoice associated yet — recommend supplier invoice follow-up "
                "(已付款，尚未收到对应进项发票，建议催供应商开票) (IP-P09)",
            )
        )

    # Status: derived from the blocker class alone (INSUFFICIENT_FACTS
    # when genuinely-required data is absent, otherwise
    # PREPARATION_AMOUNT_DETERMINABLE). Advisories never participate.
    # Every blocker code belongs to MISSING_FACT_BLOCKER_CODES (enforced
    # by test), so the classification is total.
    if any(b.code in MISSING_FACT_BLOCKER_CODES for b in blockers):
        status = SupplierRequestDecisionStatus.INSUFFICIENT_FACTS
    else:
        status = SupplierRequestDecisionStatus.PREPARATION_AMOUNT_DETERMINABLE

    return SupplierInvoiceRequestDecision(
        contract_id=contract.id,
        contract_no=contract.contract_no,
        supplier=contract.counterparty,
        status=status,
        expected_purchase_invoice_gross_amount=expected_amount,
        expected_purchase_invoice_currency=expected_currency,
        invoice_allocations=tuple(scope.invoice_allocations),
        invoice_item_allocations=tuple(scope.invoice_item_allocations),
        payment_allocations=tuple(scope.payment_allocations),
        amount_checks=tuple(amount_checks),
        item_name_checks=tuple(item_name_checks),
        blockers=tuple(blockers),
        advisories=tuple(advisories),
    )
