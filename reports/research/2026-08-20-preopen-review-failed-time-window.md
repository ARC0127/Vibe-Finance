# 2026-08-20 Preopen Review Failure Governance

- Status: `FAILED_TIME_WINDOW_CLOSED`
- Failure code: `FAILED_LATE_DISPATCH`
- Failure reason: `INVALID_TEMPORAL_PROVENANCE`
- Scheduled evidence cutoff: `2026-08-20T08:00:00+08:00`
- Actual dispatch: `2026-08-20T22:23:30.232+08:00`

The automation arrived after the 08:00 evidence window had closed. Information visible after that cutoff cannot reconstruct a time-valid 2026-08-20 preopen state. The business pipeline therefore remained stopped.

No `data/inbox/2026-08-20-preopen.json`, root preopen report, order, fill, or heartbeat was created by this run. The preopen validator and strategy runner were not called, and no post-cutoff market information was backfilled as preopen evidence.

The Shanghai Stock Exchange and Shenzhen Stock Exchange 2026 closure schedules classify 2026-08-20 as a trading day. This check was performed after the cutoff and is used only for calendar classification, not as preopen market evidence:

- [Shanghai Stock Exchange closed-day schedule](https://www.sse.com.cn/disclosure/dealinstruc/closed/)
- [Shenzhen Stock Exchange 2026 closure notice](https://www.szse.cn/disclosure/notice/general/t20251222_618087.html)

Experience-ledger validation passed with 3 records, 3 experiences, and 0 promoted rules. Aggregate ledger SHA-256: `fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21`. These non-binding observations did not affect ranking, sizing, or orders.

The pre-existing late transaction `00b679ea1b6045cfb5362ffc213c64ee` remains preserved as audit material and is bound to an `INVALID_TEMPORAL_PROVENANCE` invalidation record. It is not accepted as a timely snapshot, order report, or execution proof and was not modified by this run.

This artifact is governance-only and simulation-only. No broker connection or DeepSeek call was used. The next automatic retry point is `2026-08-21T08:00:00+08:00`.
