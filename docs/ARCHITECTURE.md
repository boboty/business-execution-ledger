# Architecture Principles (Frozen)

These five principles (A01–A05) are architecture law for this system.
Later implementation phases must not route around them. Any change to a
principle in this document requires a deliberate, explicit revision of
this document — never a silent contradiction in code.

## A01 — Agent is an operator, not the system

```
Agent Runtime
      ↓
Tool / Application API
      ↓
Application Service
      ↓
Business Domain
```

Forbidden:

```
Agent → Database
```

An Agent never talks to storage directly, and never bypasses the
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

## A03 — AI understands facts, rules decide state

AI (via the Agent) may:

- classify documents
- extract information
- propose candidate associations (matches)
- judge whether two subjects are likely the same entity
- explain an exception in natural language

AI may **not** make the final call on:

- whether to accrue (暂估)
- the accrual amount
- whether to reverse (红冲)
- whether an accrual is a duplicate
- period-close status

These are always executed by deterministic code. AI output that feeds
these decisions enters the system as a *proposal*, subject to the
confidence-state machine in A05 — never as a silent, final write.

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

## A05 — When uncertain, generate a Task

The system never silently guesses. Every fact or proposal that a rule or
an Agent produces carries a confidence state:

```
AUTO_CONFIRMED
PROPOSED
HUMAN_CONFIRMATION_REQUIRED
REJECTED
```

Anything below auto-confirmation threshold becomes a `Task / Exception`
for a human to resolve — it does not get silently applied, and it does
not get silently dropped.

---

## Agent Runtime Boundary (Frozen)

```
Business Core
       ↑
Application API / Tool Contract
       ↑
AgentRuntime Interface
      ↙  ↓  ↘
     Pi  Pydantic  OpenAI...
```

- **V1 first implementation:** Pi Agent Core
- **V1.1 second implementation:** PydanticAI

**Rule:** `Business Core` must never `import` Pi, PydanticAI, the OpenAI
Agents SDK, or any other agent-runtime package. The dependency direction
only ever points from an Agent Runtime toward the Application API — never
the reverse, and the Business Core has zero awareness that any particular
runtime exists.

This is what makes the runtime swappable later. When the Agent Runtime is
replaced:

- the database does not change
- the Domain does not change
- the Rules do not change
- Period Close does not change
- the Tool Contract, in principle, does not change

This must eventually be provable by an automated test (e.g., a
dependency/import check that fails the build if `Business Core` imports
an agent-runtime package, or a contract test that runs the same Tool
Contract suite against more than one runtime). Phase 0 freezes the
requirement; it does not implement the test.
