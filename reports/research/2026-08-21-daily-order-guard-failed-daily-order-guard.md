# 2026-08-21 Daily Order Guard Failure

- Status: `FAILED_DAILY_ORDER_GUARD`
- Automation: `vibe-finance-9`
- Timezone: `Asia/Shanghai`
- Final ledger reread: `2026-08-21T13:41:30+08:00`
- Simulation only: true
- Broker connection: not used
- DeepSeek: 0 calls, CNY 0 spent

## Finding

The 08:00 preopen snapshot and report were timely and complete before the later intraday recovery:

- `data/inbox/2026-08-21-preopen.json`
  - SHA-256: `7e096611ca884d4345aed938142c966440ee6119c743a6e90594acbb46cec54b`
- `reports/preopen/2026-08-21-preopen.json`
  - SHA-256: `39fd864b126faa29380fde518cafaaf97e0bca384b5deb977f4fa16db23d971e`
  - run_id: `f631de4d826044fabfc518a5992b7034`
  - as_of: `2026-08-21T08:00:00+08:00`
  - daily_execution_status: `ORDER_SCHEDULED`

Before the intraday recovery, the ledger had one qualifying `PENDING_NEXT_OPEN` order before the 09:10 cutoff:

- order_id: `6041f9ad80fb401b9dd940051fc6ee1b`
- symbol: `510880`
- side: `SELL`
- quantity: `100`
- signal_as_of: `2026-08-18T09:09:59+08:00`
- signal_type: `DAILY_REBALANCE_TRIM`

The final ledger reread after the governed `open-settlement` recovery commit showed:

- `pending_orders`: `0`
- cash_cny: `9643.8`
- positions_count: `6`

Under the daily-order-guard contract, a normal trading day that ends the guard with no `PENDING_NEXT_OPEN` must be reported as `FAILED_DAILY_ORDER_GUARD`. The previously synced daily-order-guard `READY` manifest is retained only as historical evidence of the pre-intraday check and is not the final outcome of this owner run.

## Governed History

- Daily-order-guard READY manifest: `reports/automation-runs/daily-order-guard/20260821T133851+0800.json`
  - SHA-256: `b031865b10e1d368db6936e095fcdc5ae596fe3d4e430ae400bd38d9d4de229d`
  - commit: `a8bc4c342f9865c626431fdc7cf57c0e67256463`
- Open-settlement recovery manifest: `reports/automation-runs/open-settlement/20260821T134028+0800.json`
  - SHA-256: `714b5bb12260b8d3cdb353e2ec9a6ea01b876a3b8e7235d05eed473ebcccaaad`
  - commit: `9cc51f2d0874c75a4c3bd5fefa78f0571cb38d81`
  - status: `PASS_INTRADAY_RECOVERY`
  - report: `reports/execution/2026-08-21-intraday.json`
  - report SHA-256: `d00389105e862e883419ebce82b7e0337aefb536ed30dd7945639eced1930573`
  - run_id: `18b09951aec14c97874aaebcd780184f`

## Constraints

- No `2026-08-21-0910-preopen` recovery input was generated during this correction.
- No order, portfolio, heartbeat, README, strategy, or broker-facing state was modified by this failure evidence.
- No `NO_TRADE` success substitution was used.
