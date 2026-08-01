# Pro Research Suite

## Purpose

The Pro suite adds a compact research-enrichment layer without modifying the
existing private accounting ledger or adding broker execution. It consumes an
owner-only portfolio snapshot and point-in-time research inputs, then produces
exactly one JSON/Markdown report.

```text
private accounting or portfolio snapshot
+ accepted-close/source health
+ aligned asset/factor returns
+ Trump policy events
+ resolved Polymarket events
+ authorized Social Heat aggregate
+ optional manager/fund returns
-> bounded research models
-> effective risk budget
-> HOLD / RISK_REBALANCE / PAUSE_AND_VERIFY
-> one pro_daily_report.json + one Chinese Markdown report
```

## Implemented models

### Trump Policy Transmission Index

TPTI weights policy events by source authority, implementation stage,
magnitude, confidence, horizon/recency and documented asset sensitivity. It
does not score raw media mention volume. Its total decision-score contribution
is capped at 5%, positive risk expansion is capped at 2%, and it cannot create a
trade by itself.

### Polymarket settlement event study

The event study freezes the last market probability observed before the
configured embargo, normally 24 hours before resolution. It then evaluates
1/5/20/60-session returns after resolution. Post-resolution probabilities are
never backfilled into the predictor. Until a group reaches the configured
point-in-time sample threshold it remains `research_only`.

### Barra-inspired public proxy

The factor-risk module is not commercial MSCI Barra. It uses ridge exposures, a
shrunk factor covariance matrix and asset-specific residual variance to estimate
portfolio factor exposures, systematic/specific risk, factor and asset risk
contributions, effective factor count and a downside-only concentration
multiplier.

### Kalman dynamic exposure

The state-space model estimates time-varying alpha and factor betas. It is
explicitly labelled return-inferred and must not be represented as disclosed
holdings.

### Manager skill and fragility

The manager module separates return evidence from portfolio survivability. It
calculates factor alpha, alpha t-statistic, residual-bootstrap skill probability,
Treynor-Mazuy and Henriksson-Merton timing, up/down capture, rolling-alpha
persistence and tracking error. Copy-trade permission also requires acceptable
leverage, top-10 concentration, liquidity days, prime-broker concentration,
manager tenure and fund age.

## Effective risk budget

```text
effective risk budget
= objective market multiplier
× Barra concentration multiplier
× Kalman exposure multiplier
× Trump policy multiplier
× Polymarket calibration multiplier
× Social Heat downside multiplier
× prediction-calibration multiplier
× live-data confidence
```

Missing optional sources reduce only their own coverage. Missing or stale
portfolio, accepted-close or factor inputs block new risk rather than being
interpreted as neutral.

## DCA boundary

The private configuration may contain five tickers at USD 20 each. The public
example is synthetic. Report fields distinguish configured daily amount,
model-proposed amount and broker-confirmed execution, which this suite never
invents. The suite has no order endpoint.

## Run the deterministic demonstration

```bash
python run_pro_daily.py \
  --config examples/pro_daily_config.example.yaml \
  --out-dir out_pro_demo
```

Outputs:

```text
out_pro_demo/pro_daily_report.json
out_pro_demo/pro_daily_report.md
```

## Private use

Copy `examples/pro_daily_private_template.yaml` outside every Git worktree and
cloud-sync directory, rename it to `*.private.yaml`, and point it to local JSON
and CSV files. Actual holdings, DCA tickers, costs, returns and research records
must remain private.
