---
name: maintain-daily-research-context
description: Preserve the complete requirement and implementation context of the U.S. stock daily-research project when reviewing, extending, debugging or deploying it. Use before any Codex/agent change to sources, models, reports, scheduling, portfolio actions, private inputs, tests or repository structure.
---

# Maintain Daily Research Context

## Purpose

Prevent requirement loss, documentation drift and false implementation claims.
The project has accumulated several generations of requirements. A module name,
class, test or design document does **not** prove that the module has current
data, is executed every day, appears in the report or affects a decision.

## Required read order

Before changing code, read in this order:

1. `requirements/DAILY_RESEARCH_REQUIREMENTS.yaml`;
2. `PROJECT_CONTRACT.yaml`;
3. `docs/PRODUCTION_INTEGRATION_AUDIT_V4.md`;
4. `docs/IMPLEMENTATION_STATUS.md`;
5. the affected implementation and tests;
6. the private deployment workflow and renderer when the change affects the live report.

Never start from an old PR description, README paragraph or chat summary when a
newer requirement-ledger entry exists.

## Four-state implementation test

For every requested capability, report all four states separately:

| State | Required evidence |
|---|---|
| `implemented_library` | typed code exists and compiles |
| `tested` | point-in-time and failure-path tests pass |
| `live_data_connected` | a production adapter supplies current, timestamped data |
| `daily_report_integrated` | the one user-visible report renders and uses the result |

A capability is production-complete only when all required states pass. Use
`private_input_required`, `blocked`, `degraded`, `not_due` or `no_data` instead
of silently promoting an incomplete state.

## Change workflow

1. Resolve the exact requirement IDs affected by the request.
2. Inspect the live path from collector/input through model, risk gate, action
   renderer and private Issue delivery.
3. Reuse existing tested modules. Do not create a second TPTI, Polymarket,
   manager-skill, factor, risk or accounting implementation when the canonical
   library already exists.
4. Add an adapter or orchestration bridge when code exists but is not supplied
   live data or not included in the daily report.
5. Preserve source timestamps, provenance, independence groups and source
   health. Missing evidence is never neutral.
6. Keep agent summaries, Reddit, Quora and Xiaohongshu as claims to verify.
   Agent-generated prose is never an original source. Social media has zero
   direct ADD/OPEN weight.
7. Update `requirements/DAILY_RESEARCH_REQUIREMENTS.yaml`, implementation audit,
   tests and private deployment pin in the same change set.
8. Run:

```bash
python -m compileall -q .
python scripts/check_public_privacy.py
python scripts/check_project_contract.py
python scripts/check_requirement_ledger.py
pytest -q
```

9. Inspect one synthetic report and one live/private report. Confirm that the
   conclusion appears first, the report date/time zone is correct, and no
   source failure is described as a neutral market view.

## Daily report contract

The target report answers, in order:

1. what to hold, add, trim or block for the next session;
2. why the conclusion changed or did not change;
3. current position state;
4. buy-side theses with variant perception, evidence, catalysts, horizon and
   invalidation;
5. factor and advanced-model diagnostics;
6. fund-company, financial-company and independently verified market views;
7. collapsed source health, tests and operating boundaries.

Do not show a recurring aggregate fee section. Estimate commission, slippage,
holding friction and tax-lot implications only for a security that has an
actual `ADD_REVIEW` or `TRIM_REVIEW` proposal. Unknown components remain
`UNKNOWN` and cannot be assumed to be zero.

## Context persistence rules

- Keep personal/private holdings, raw social exports, broker reports and paid
  research outside the public repository.
- Store changing requirements as IDs in the ledger, not only prose.
- Store the previous thesis scores or state in the private daily Issue metadata
  so the next report can distinguish new, stronger, weaker and unchanged views.
- A scheduling change must state local time, time zone, effective date and DST
  behavior.
- A manager record must be cut at manager/team changes; do not attribute a
  product shell's old return history to a new manager.
- Correlated headlines, syndicated articles, social reposts, Polymarket markets
  and option-surface variables remain grouped rather than counted repeatedly.

## External agent fallback

Architecture references such as FinRobot, TradingAgents, OpenBB agents and
ai-hedge-fund may inform role decomposition and report design. Their generated
conclusions enter only through the private opinion inbox with:

- generated/observed timestamp;
- agent/repository identity;
- claim and ticker/topic;
- original source URLs;
- direction, horizon and invalidation;
- independent verification result.

Without original URLs or corroboration, the item is context-only.

## Completion statement

When finishing a task, list:

- requirement IDs changed;
- code and tests added;
- live adapters activated;
- fields now present in the daily report;
- remaining `private_input_required`, `blocked` or roadmap items.

Never summarize “all modules are implemented” when some are still library-only
or depend on missing private/live inputs.
