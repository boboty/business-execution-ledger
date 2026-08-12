# Contributing

Contributions are welcome. BEL is still early, so the most valuable contributions are usually small, testable improvements that preserve the system's architecture boundaries.

## Before you start

Read these first:

- `docs/ARCHITECTURE.md` — frozen architecture principles
- `docs/V1-SCOPE.md` — current scope and non-goals
- `docs/RULES.md` — numbered business rules
- `docs/PRIVATE-DATA-POLICY.md` — mandatory public-data boundary

## Ground rules

1. **Do not turn prompts into business rules.** Deterministic business decisions belong in code.
2. **Preserve Evidence → Fact → Decision traceability.** New outputs should remain defensible back to their source evidence.
3. **Do not silently guess.** Ambiguity should become an explicit proposal, task or exception.
4. **Keep the Business Core runtime-agnostic.** Agent frameworks belong outside the domain layer.
5. **Never contribute real business data.** Public tests and examples must use synthetic data.

## Development

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
git config core.hooksPath .githooks
.venv/bin/alembic upgrade head
.venv/bin/pytest
```

Before opening a pull request, run the test suite and the privacy scanner. Prefer focused PRs with an explanation of the business rule or architecture principle affected.

For behavior changes that expose a gap in a frozen design document, record the implementation judgment explicitly rather than silently rewriting the design to fit the code.