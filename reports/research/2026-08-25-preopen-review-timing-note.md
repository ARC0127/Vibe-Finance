# 2026-08-25 Preopen Review Timing Note

- Status: `PASS_WITH_TIMING_NOTE`
- Automation dispatch: `2026-08-25T08:01:02.898+08:00`
- Evidence cutoff: `2026-08-25T08:00:00+08:00`
- Immutable snapshot creation: `2026-08-25T08:12:22+08:00`
- Snapshot: `data/inbox/2026-08-25-preopen.json`
- Snapshot SHA-256: `eb67b39dc9833fc9aa145e158c9421414b5cb86c81f219379638b12fa8f4d0fb`
- Pipeline run: `be7d14dc042e41a983bba5128cce9b7e`
- Pipeline result: `PASS / ORDER_SCHEDULED`

The automation was dispatched at 08:01, but the immutable snapshot was finalized at 08:12, after the 09:10 order-readiness checkpoint and before the market opened. The snapshot field `provenance.recovery_window=AUTHORIZED_PREOPEN_RECOVERY_BEFORE_09_10` is therefore not an accurate creation-time classification. The authoritative timestamp is `provenance.created_at`. The immutable snapshot is preserved and is not rewritten.

The evidence cutoff remains 08:00. All listed-asset prices and histories come from the sealed 2026-08-24 close. No 2026-08-25 opening, intraday or post-08:00 market/event data was used. The retained `PENDING_NEXT_OPEN SELL 588000 x1300` order was created by the 2026-08-24 close run; this review created no new order and no fill.

`588000` remains `UNVERIFIED_PREOPEN`. A virtual fill is permitted only after the 09:30-09:35 task proves `TRADING` and a compliant dual-source opening price. This project is simulation-only, does not connect to a broker and is not investment advice.
