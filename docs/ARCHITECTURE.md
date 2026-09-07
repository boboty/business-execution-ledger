# Architecture Principles (Frozen)

These five principles (A01–A05) are architecture law for this system.
Later implementation phases must not route around them. Any change to a
principle in this document requires a deliberate, explicit revision of
this document — never a silent contradiction in code.

## A01 — Agent is an operator, not the system

```
Operator / Agent Runtime
      ↓
Tool / Application API
      ↓
Application Service
      ↓
Business Domain
```

Forbidden:

```
Operator / Agent → Database
```

An Operator never talks to storage directly, and never bypasses the
Application API to reach the Business Domain. Just as important: a
**Prompt must never become a business rule**. If a decision needs to be
made consistently and defensibly, it is code — not instructions to a
model.

## A02 — Evidence ≠ Fact ≠ Decision

Three distinct layers, always kept separate and always traceable from one
to the next. (Illustrative example below — placeholder values, not a real
contract/counterparty record.)

```
采购合同PDF
    ↓
Evidence

供应商=SupplierExample
金额=1000.00
商品=示例商品
    ↓
Fact

截至月末未到票
    ↓
Rule

AccrualRequired
    ↓
Decision / Business Output
```

Every Decision must be traceable back to the Fact(s) it was derived from,
and every Fact must be traceable back to the Evidence it was extracted
from:

```
Decision → Fact → Evidence
```

This traceability is a system requirement, not a nice-to-have — it is
what makes a business state defensible when questioned later.

## A03 — AI interprets; deterministic rules decide state

AI capability is **outside the Business Core**. It may appear in a future
Intake module or in an Operator / Agent Runtime, but the Business Core
must not depend on a model provider, model SDK, prompt template, or agent
framework.

AI may:

- classify or interpret external business material
- extract and normalize candidate information
- propose candidate associations (matches)
- judge whether two subjects are likely the same entity
- explain an exception in natural language

AI may **not** make the final call on:

- whether to accrue (暂估)
- the accrual amount
- whether to reverse (红冲)
- whether an accrual is a duplicate
- period-close status
- any other authoritative business state governed by deterministic rules

A model response is never authoritative merely because it is structured or
confident. Any AI-derived candidate that is eventually admitted into BEL
must cross an explicit validation / confirmation boundary. Deterministic
business rules continue to consume governed Facts, not prompt-owned
judgments.

## A04 — Canonical Business Model

The domain model never contains finance/tax/ERP vocabulary. Specifically
forbidden inside the Business Core's domain language:

```
1405
220299
借方
贷方
凭证号
退税申报状态
(or any specific finance-software field name)
```

These belong to future Adapters, which translate the system's canonical
vocabulary into a consumer's vocabulary. Internally, the system speaks
only its own business vocabulary, for example:

```
AccrualRequired
cost_amount
contract_item
period
reason
evidence
```

A future Finance Adapter is responsible for translating, e.g.,
`AccrualRequired` + `cost_amount` into an accounting entry. The Business
Core never knows that translation exists.

## A05 — When uncertain, do not silently guess

Uncertainty must remain explicit. Where a proposal / confirmation workflow
exists, it may use states such as:

```
AUTO_CONFIRMED
PROPOSED
HUMAN_CONFIRMATION_REQUIRED
REJECTED
```

A result that is not authorized for automatic application must not be
silently promoted into authoritative state. It is either left as a
non-authoritative finding/proposal, routed into the existing domain-specific
human-work mechanism, or rejected according to that capability's frozen
semantics.

This principle does **not** require every advisory, missing value, computed
blocker, or future AI result to be persisted as one generic Task. BEL keeps
distinct unresolved-work semantics distinct rather than inventing a universal
workflow object.

---

## Operator Runtime Boundary

The stable architecture boundary is the Application / Tool Contract, not a
particular runtime framework and not a prematurely frozen `AgentRuntime`
interface.

```
Business Core
       ↑
Application API / Tool Contract
       ↑
Operator / Runtime
```

**Frozen rule:** `Business Core` must never import Pi, PydanticAI, the OpenAI
Agents SDK, or any other agent-runtime package. The dependency direction only
ever points from an Operator / Runtime toward the Application API — never the
reverse.

Framework brand, implementation language, process count, transport choice and
runtime ordering are **implementation decisions, not architecture law**.
Pi remains a candidate for the first restricted operator runtime, but it is not
hard-coded into the Business Core or into a mandatory framework sequence.

The current post-Tool-Contract sequence is deliberately narrower:

1. close the single-response consistency gap in operator-facing reads;
2. establish an executable delegation boundary with a trusted host;
3. prove that boundary with a model-free independent client over a private JSON
   transport;
4. freeze authorization, retry/reconciliation and stop/residual-work semantics;
5. only then attach the first restricted Operator / Agent Runtime;
6. later use a second real consumer/runtime to prove substitutability before
   extracting any broader common runtime abstraction.

When the Operator / Runtime is replaced:

- the database does not change
- the Domain does not change
- the Rules do not change
- Period Close does not change
- the Tool Contract, in principle, does not change

This substitutability must eventually be demonstrated by automated boundary
and contract tests, not assumed from an interface name.

## Future Intelligent Intake Boundary (Deferred)

BEL does **not** currently embed a semantic-understanding or semantic-
normalization layer inside the Business Core.

For local development and current private use, new Excel/PDF/source material
may continue to be understood and normalized outside BEL with Codex-assisted
preprocessing, human review of material ambiguities, and then the existing
BEL import/backfill paths. This is a development/operations workflow, not a
productized Intake subsystem.

If repeated source onboarding, multi-user operation, or ongoing normalization
cost later justifies productization, introduce a separate `bel-intake`
capability outside the Core:

```
Raw business material
        ↓
future bel-intake
(parse / interpret / normalize / propose)
        ↓
BEL Intake Contract
        ↓
Application Service / Business Core
```

The future Intake module may own parsers, OCR/model providers, prompts,
normalization strategies and proposal/evaluation machinery. It does **not** own
authoritative business state, and its output does not become a Fact merely by
being produced by a model. The stable seam to protect is the business input
contract BEL is willing to validate and admit — not a model-specific semantic
API.

The long-term responsibility split is therefore:

> **Intake interprets. Core decides. Operator acts.**

## Core Completion calculation boundary

Invoice preparation uses current confirmed contract Facts as the authority:
procurement quantities come from ContractItems; sales quantity comes from
SalesContract. Shipment/customs Facts are independent consistency context,
never fallback values or a path for synthesizing a contract Fact.

Sales preparation receives an explicit invoice month and traceable confirmed
SAFE daily USD/CNY inputs. Core chooses the latest valid publication no later
than the month's natural first day and computes expected CNY deterministically.
It does not access a rate website or maintain a holiday calendar. Missing or
ambiguous inputs remain visible. USD amount, rate, publication date and note
data remain structured Application outputs; Web/CLI/export only present them.

Tax classification code reuse is scoped to the Evidence-backed current
ContractItem revision. The existing Fact supplement/correction path is the
confirmation boundary. No catalogue, inferred category matching or generic
workflow is introduced.
