# Daily Order Guard Failure Evidence - 2026-09-03

- Status: `FAILED_TIME_WINDOW_CLOSED`
- Failure reason: `TIME_WINDOW_CLOSED_AND_SAME_DAY_PREOPEN_EVIDENCE_MISSING`
- Scheduled guard cutoff: `2026-09-03T09:10:00+08:00`
- Actual owner run time: `2026-09-03T16:37:53+08:00`
- Simulation only: `true`
- Broker connection: `NOT_USED`
- DeepSeek: 0 calls, CNY 0.00 spent

The daily-order-guard owner ran after both the 09:10 preopen guard cutoff and the 09:30-09:35 opening proof window. No same-day 08:00 preopen input or report was present:

- `data/inbox/2026-09-03-preopen.json`: missing
- `reports/preopen/2026-09-03-preopen.json`: missing
- `reports/preopen/2026-09-03-preopen.md`: missing

Because the time window was already closed, this run did not generate `data/inbox/2026-09-03-0910-preopen.json` and did not run a replacement preopen pipeline. Creating those business artifacts at 16:37 Asia/Shanghai would have invalid temporal provenance.

Current ledger reread still showed one existing `PENDING_NEXT_OPEN` virtual order from the 2026-09-02 close-analysis cycle:

- Order: `0e87aeb5b1654d9ea74ab2c77fe1d63d`
- Symbol: `518880`
- Side/quantity: `SELL 100`
- Signal time: `2026-09-02T16:30:00+08:00`
- Signal type: `DAILY_WEAKNESS_ROTATION`
- Portfolio SHA-256: `5d8e873e80b8547c9eec846dba81e5df3f176c14383b7a20d91b2b9e079870cf`
- Orders SHA-256: `7c5181437989a1da3d5a10ec1a7fb11e5cd873b1d70f7af23cab616087db2e97`

This evidence preserves the failure boundary: the existing pending order is retained as ledger state, but this run does not claim a timely 2026-09-03 09:10 readiness check. No order, portfolio, heartbeat, strategy, broker-facing state, or opening-price artifact was modified.
