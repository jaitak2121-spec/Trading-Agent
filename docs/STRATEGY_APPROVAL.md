# Strategy approval process

Current status: no strategy is approved for live capital. The MA strategy is an
operational paper baseline. The fixed development comparison approves none.

Before researching a new candidate, freeze its source/configuration hashes,
genuine dataset hashes, chronological development folds, transaction costs,
sampling/gap policy, benchmarks and acceptance criteria. Record the number of
candidates to make multiple testing visible. Candidate selection may use only
development folds. Feature/parameter changes require a new experiment identity.

Development approval requires the preregistered criteria, sufficient trades,
robustness across regimes, credible uncertainty, and cost/risk evaluation. A
positive classification metric is not a strategy result. Sparse or missing
validation folds cannot count as passing folds. Never change criteria after
looking at results. The current comparison had no selection/approval action;
its two empty folds and negative usable fold cannot support approval.

A selected candidate must be frozen before requesting separate authorization
for a fresh independent confirmation period. Never reuse the consumed V2 ML
confirmation, and never open Segment 7 as part of strategy development. The
new protocol must identify its own independent evidence and exact authorization.
Failure means reject/archive, not tune against confirmation and retry.

After independent confirmation, an operator may approve only a paper trial,
recording strategy/code/configuration/dataset/report hashes, limitations and
risk limits in an immutable approval record. Deployment remains a separate
deliberate action; a report cannot silently change the production strategy.
Forward paper monitoring must include costs, exposure, drawdown and incidents.
Regime or performance deterioration prompts a new research cycle, not automatic
weight updates or threshold changes.

No amount of backtest evidence alone authorizes real money. Live qualification
needs an independently reviewed exchange adapter, safe venue environment,
permissions, balance/order/position reconciliation, outage/restart tests and
explicit user activation. Those requirements remain separate from research.
