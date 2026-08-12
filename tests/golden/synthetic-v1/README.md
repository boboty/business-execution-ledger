# Synthetic golden suite

Baseline JSON in this directory is computed from `fixtures/synthetic/scenarios.py`
and `fixtures/synthetic/bank_pdf.py` — independently constructed synthetic
contracts, invoices, and a bank statement, not derived from any real file.
See `docs/PRIVATE-DATA-POLICY.md`.

The equivalent runs against real business data (private, never committed)
are `tests/private_acceptance/runner.py`'s `P1_IMPORT`, `P2A_INVOICE_IMPORT`,
`P2A_PAYMENT_IMPORT`, and `P2A_MATCHING` scenarios.
