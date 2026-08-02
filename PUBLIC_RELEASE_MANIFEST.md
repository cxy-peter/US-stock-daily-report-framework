# Public Release Summary

This repository contains the reusable research code, tests, synthetic examples
and methods for one personal U.S. stock daily report. It contains no real
portfolio, broker credential or private report, and it has no order endpoint.

## Included

- read-only broker/Flex parsing and reconciliation;
- portfolio, accepted-close, fee and cost controls;
- conclusion-first report components;
- global event and cross-asset transmission;
- political, prediction-market, volatility and option research;
- purged 1/5/20-session factor validation with costs and multiple-testing control;
- factor risk, dynamic exposure, attribution and allocation proposals;
- Linux and Windows regression tests with synthetic data.

## Kept private

- real holdings, cash, cost basis and P&L;
- Flex and data-provider credentials;
- private Issues, reports and runtime databases;
- owner decisions and actual trades.

## Important boundary

Research outputs may recommend `HOLD`, `ADD_REVIEW`, `TRIM_REVIEW`,
`BLOCK_ADD` or `PAUSE_AND_VERIFY`. They never submit an order or silently update
the confirmed ledger.

Implementation status and deeper controls are indexed in [`docs/README.md`](docs/README.md).
