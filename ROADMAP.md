# Roadmap

Business Execution Ledger (BEL) is being built as a deterministic business execution layer for agentic systems. The roadmap is intentionally capability-driven rather than feature-volume-driven: each phase should make more business work safely delegable without moving authoritative business state into prompts.

## Current — V0.1 / Phase 2B

- Traceable Evidence → Fact → Decision chain
- Contract, invoice, payment, allocation and accrual domain objects
- Deterministic matching with explicit ambiguous/unmatched outcomes
- Read-only period-close preview with numbered business rules
- Synthetic golden tests and privacy guards for a public repository

## Next — V1

- Complete the frozen V1 business scope
- Implement the five user-facing work surfaces defined in `docs/V1-SCOPE.md`
- Expand exception/task handling so uncertain cases become explicit work instead of silent guesses
- Add more evidence adapters while preserving raw-source traceability
- Strengthen rule-level explainability: every business state should name the rule and facts that produced it

## Agent Runtime

- Introduce the first Agent Runtime through the Application API / Tool Contract boundary
- Keep the Business Core independent of any specific model or agent framework
- Add automated architecture checks that fail if the Business Core imports an agent runtime
- Prove runtime substitutability with contract tests against more than one runtime

## Ecosystem

- Define stable Tool / MCP contracts for external agent runtimes
- Add adapters for downstream finance, tax, ERP and analytics consumers without leaking their vocabulary into the Business Core
- Publish reusable synthetic business scenarios for testing agentic business systems
- Improve contributor documentation and issue templates as outside contributors arrive

## What will not change

The core boundary is deliberate: agents may understand evidence, propose associations, explain exceptions and operate tools; authoritative business facts, business states and close decisions remain governed by structured data and deterministic rules.

See `docs/ARCHITECTURE.md` for the frozen architecture principles.