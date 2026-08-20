# 2026-08-20 Daily Order Guard Failure Governance

- Status: `FAILED_TIME_WINDOW_CLOSED`
- Failure reason: `INVALID_TEMPORAL_PROVENANCE`
- Scheduled guard cutoff: `2026-08-20T09:10:00+08:00`
- Actual correction time: `2026-08-20T22:28:00+08:00`

The 2026-08-20 guard artifacts generated after 22:25 Asia/Shanghai are not valid 09:10 preopen evidence. They must not be represented as timely `READY` or `ORDER_SCHEDULED` evidence.

Revoked paths:

- `data/inbox/2026-08-20-0910-preopen.json`
- `reports/preopen/guard/2026-08-20-preopen.json`
- `reports/preopen/guard/2026-08-20-preopen.md`
- `data/ledger/transactions/00b679ea1b6045cfb5362ffc213c64ee/prepare.json`
- `data/ledger/transactions/00b679ea1b6045cfb5362ffc213c64ee/commit.json`

Baseline reference for the rollback decision: `e1c975e972faecfd7bbd8cca7a056b527a10061e`.

This evidence is governance-only. It is not a market snapshot, order report, execution proof, or investment signal. No broker connection was used. DeepSeek usage was 0 calls and CNY 0.0.
