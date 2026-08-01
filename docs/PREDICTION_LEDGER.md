# Private Prediction Research Ledger

## Purpose

The prediction ledger turns a sanitized research signal into a result that can
be settled and audited later. It is a calibration component, not a portfolio or
order component. It has no broker, order, fill, cash or position mutation API.

The database belongs in the same owner-only, non-Git, non-cloud private runtime
boundary as the portfolio ledger. The public repository contains only the
engine and synthetic tests.

## Signal contract

Each signal records only derived metadata:

- first-observed timestamp and observation session;
- closed platform/source/regime IDs, controlled topic taxonomy ID and optional canonical ticker;
- direction, strength and probability;
- 1-, 5-, 20- and 60-session target dates plus the signed 60-session calendar path,
  calendar version, timezone and official observation-session close;
- a fully validated point-in-time `AcceptedClose` reference (callers cannot inject a
  bare price, as-of timestamp or lineage digest);
- market regime and model version;
- irreversible author/evidence digests and a rights attestation digest.

Raw posts, account names, handles, URLs, search queries, credentials and binary
floating-point values are rejected. A signal must exist before its outcome is
known; a future timestamp or accepted-close observation retrieved after the
settlement timestamp fails closed.

## Immutable event model

Signals, settlements, reversals and idempotency aliases are append-only canonical
JSON events in SQLite. A source-signal identity fingerprint prevents the same
sanitized evidence from becoming two calibration samples merely by changing a
timestamp, score, regime or reference-price lineage, while aliases permanently
bind every successful retry key. A SHA-256 chain and an HMAC-authenticated,
two-phase committed/pending checkpoint detect mutation, DB-only tail deletion,
sidecar-only rollback and ambiguous commits. Corrections append a reversal and, when appropriate, a newly
identified replacement event; rows are never updated or deleted through the
ledger API.
For the same source-signal fingerprint, an active event remains unique. After a
reversal, a replacement is accepted only with an explicit
`supersedes_signal_id` pointing to that reversed signal; the validator rejects
unlinked retries, active duplicates and branching replacement chains.

### Integrity threat-model boundary

The SQLite file and its colocated `.integrity.json` sidecar are one local trust
unit. This design intentionally fails closed if either member is missing,
modified or rolled back independently. It cannot detect a coordinated restore of
both files to the same older, once-valid snapshot: no colocated checksum can
provide an external monotonic notion of "latest." Backups must therefore keep the
pair atomic and owner-controlled. A production deployment that must detect
coordinated rollback needs a separate owner-protected monotonic anchor (for
example, a remote append-only checkpoint or signed monotonic counter); that
external anchor is not implemented by this offline module.

## Settlement and calibration

Only an `AcceptedClose` that passed the independent-source, finality, raw-price
and ledger-input gates may settle a horizon. The outcome contains:

- raw return and optional versioned factor-residual return;
- directional hit;
- maximum favorable and adverse excursion from supplied accepted-close path;
- Brier score for the stated direction probability.

Summaries group by platform, topic, **model version**, market regime and horizon.
`PredictionOutcome`, `CalibrationSummary`, and `PredictionWeightState` all carry
the model version. Outcome/calibration filters may select one version, while
`weight_state` requires it explicitly; samples from different versions can never
be pooled to satisfy minimum-sample, recent-window, decay, or quarantine gates.
Their `sample_scope` is explicit: default summaries are `live_only`; an explicit
backfill research query is labelled `includes_backfill`, and each returned
outcome exposes its `recording_mode` and `calibration_eligible` label. Backfills
can never be included in `weight_state`. Residual-return
Rank IC is reported only when enough non-missing, non-constant observations
exist; missing residuals remain missing rather than becoming zero.

The factor-residual evidence also persists its own factor-model version, but
this release does not yet split residual summary rows by that second version.
Do not mix factor-model versions inside one prediction-version calibration
cohort. Current `weight_state` uses hit rate and Brier only; residual means and
Rank IC do not change research weights. A factor-version-isolated summary is a
required follow-up before residual metrics may influence weighting.

The ledger revalidates the structural corporate-action gates on every accepted
close and currently accepts only `corporate_action_status=clear_none`. A
`reconciled` close fails closed because this module has no auditable split ratio,
distribution adjustment or total-return price contract. Production provider
adapters must still obtain and attest authoritative corporate-action evidence; a
future adjusted pipeline must persist that evidence and apply it consistently to
the full path before `reconciled` can be enabled.

Rolling hit rate, Brier score and hit-rate drift produce one of four research
states:

- `research_only`: insufficient settled samples;
- `active`: calibration gates pass;
- `decayed`: recent efficacy weakened;
- `quarantined`: failure or drift crossed the hard threshold.

Every state carries `automatic_trading_permitted = false`. The private daily
runtime adapter and scheduled horizon settlement remain separate work and must
preserve this boundary.
