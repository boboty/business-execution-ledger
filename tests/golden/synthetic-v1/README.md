# Synthetic golden suite

Baseline JSON in this directory is computed from `fixtures/synthetic/scenarios.py`
and `fixtures/synthetic/bank_pdf.py` — independently constructed synthetic
contracts, invoices, and a bank statement, not derived from any real file.
See `docs/PRIVATE-DATA-POLICY.md`.

The Phase 2B close-engine golden (`test_period_close_baseline.py` +
`period-close-baseline.json`) is computed from
`fixtures/synthetic/phase2b_close.py` — independently constructed close
contracts, partial/full receipt invoices, and a Close Fact Pack. See
`docs/PHASE2B-DECISIONS.md`.

The equivalent runs against real business data (private, never committed)
are `tests/private_acceptance/runner.py`'s `P1_IMPORT`, `P2A_INVOICE_IMPORT`,
`P2A_PAYMENT_IMPORT`, `P2A_MATCHING`, and the `P2B_*` close-engine
scenarios.
