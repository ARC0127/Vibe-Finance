# Frozen B0 Adapter Audit — 2026-07-21

Status: `PARTIALLY_COMPLETED`

This audit is simulation-only. It does not establish strategy returns, a B0/B1/B2 ranking, walk-forward performance, independent OOS performance, or promotion authority.

## Verified source identity

- Git commit: `081a45c95d078281733100479d6239e8f4eb9429`
- Strategy SHA-256: `8bd96245c81534623618660b99708009e95f400f2368a9a646b54f7155b8a59d`
- Pipeline SHA-256: `188da8ec280dcf52eb73406eccb47a5ab9a35c80bdfe8e488e316ec3a6c516f0`
- Universe SHA-256: `23a8197aa3e9166949f669070318422861d77e3ff3d8af1da12be3ebaf7d29b7`
- Sources registry SHA-256: `a82bbe176be533f889fcd2807626ce6d17078543515660cf023efbb00d45f073`
- Frozen source identity SHA-256: `a8b9a7b2cfcf328412497c935063d8b9fe7f3cbc49ab0aff0fe955bf42ad8cbe`

## Competing hypotheses and result

1. The historical commit cannot execute under the current runtime. Rejected: its own 21 tests passed in an extracted archive, and the isolated fixture replay entered the historical execution graph.
2. The historical schema is executable but cannot support a fair common evaluation. Retained: the commit fixtures contain no trusted next-open sequence, and the operational fixture universe is wider than the four-ETF B0/B1/B2 comparison universe.
3. UUID, wall-clock, path, or environment nondeterminism prevents replay. Rejected for the adapter: the clock and UUID stream are fixed, paths are normalized, the child environment is allowlisted, source/input trees are read-only during execution, and two runs produced the same behavior hash.

## Closed-loop evidence

- Historical fixture behavior SHA-256: `3684049a2f87761ff31ed751135e7fcef82ad68cced63fbe612949600a5c9217`
- Immutable smoke artifact: `reports/evolution/2026-07-21-b0-historical-fixture-smoke-v2.json`
- Artifact SHA-256: `aa0f1efd842160094904ac65226aec32ffd3b590ab29c27d143b01be04313815`
- Fresh-process verification: `VERIFIED_FROZEN_B0_HISTORICAL_FIXTURE_SMOKE`
- Historical 2026-07-19 fixture: zero orders.
- Historical 2026-07-20 fixture: `510300 BUY 1200` and `512100 BUY 800`; zero fills because no next-open fixture was supplied.
- Production `portfolio.json`, `orders.jsonl`, and `heartbeat.json` remained unchanged during each isolated replay.

The frozen decision adapter skips historical settlement, report writing, and heartbeat updates. A synthetic four-ETF golden signal produced fixed quantities of `510300 BUY 600` and `512100 BUY 200`. Under two independently bound raw-open observations of CNY 10.00 and common 10 bps slippage, the common executor produced:

- fill prices: CNY 10.01 and CNY 10.01;
- commission: CNY 10.00;
- slippage: CNY 8.00;
- after cash: CNY 21,882.00;
- `510300` after quantity / average cost: `600 / 10.018333`;
- `512100` after quantity: `200`.

Raw-open limits are checked before common slippage. B0 fixed quantities bypass the B1/B2 3 percentage-point and CNY 2,500 rebalance gates, but retain common source, calendar, commission, slippage, cash, lot-size, and T+1 checks.

## Regression matrix

| Variant | Result | Evidence boundary |
|---|---|---|
| Source identity check | PASS | Commit and four source files are hash-bound |
| Historical fixture run twice | PASS | Deterministic behavior, not performance |
| Save → reload → replay | PASS | Artifact and current input hashes reverified |
| Artifact/input tamper | INVALID | Fails closed before accepting replay |
| Four-ETF frozen decision → common next-open execution | PASS | Synthetic mechanical golden only |
| Raw open above frozen buy limit | CANCELLED | Limit checked before slippage |
| Full repository tests | 83/83 PASS | Structural/mechanical regression only |
| Static evolution gate | `PASS_STATIC_PROPOSED_ONLY_GATE` | Promotion remains disabled |

## Remaining P0

The trusted point-in-time loader and official trading calendar are still unavailable. The loader must materialize, for every signal date, the four-ETF total-return history rescaled to the signal-date raw close, adjacent total-return simple daily returns, a broad-index daily return, independently sourced next-open observations, processed corporate actions, and the bound multi-day state. Until the complete walk-forward and sealed independent-OOS run is saved and replayed, all metrics and ranking remain `null`, and the strategy remains `PROPOSED_ONLY`.
