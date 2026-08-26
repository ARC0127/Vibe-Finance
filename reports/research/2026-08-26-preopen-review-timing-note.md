# 2026-08-26 Preopen Review Timing Note

- Status: `PASS_WITH_TIMING_NOTE`
- Automation dispatch: `2026-08-26T08:02:04.283+08:00`
- Evidence cutoff: `2026-08-26T08:00:00+08:00`
- Immutable snapshot creation: `2026-08-26T08:11:10+08:00`
- Snapshot: `data/inbox/2026-08-26-preopen.json`
- Snapshot SHA-256: `894f525a45d02d0728a024843e65d775d02322dff6cab503c5f19485e34f6983`
- Pipeline run: `ba18ad9b42c34f03a2403b1ce317dd67`
- Pipeline result: `PASS / ORDER_SCHEDULED`

The automation was dispatched at 08:02, but the immutable snapshot was finalized at 08:11, after the 09:10 order-readiness checkpoint and before the market opened. The snapshot field `provenance.recovery_window=AUTHORIZED_PREOPEN_RECOVERY_BEFORE_09_10` is therefore not an accurate creation-time classification. The authoritative timestamp is `provenance.created_at`. The immutable snapshot is preserved and is not rewritten.

The evidence cutoff remains 08:00. All listed-asset prices and histories come from the sealed 2026-08-25 close. No information published after 08:00 and no 2026-08-26 opening or intraday price was used. Official pages retrieved after 08:00 were used only to verify documents whose publication dates were already before the cutoff. The retained `PENDING_NEXT_OPEN BUY 159928 x3600` order was created by the 2026-08-25 close run; this review created no new order and no fill.

`159928` remains `UNVERIFIED_PREOPEN` for live trading and suspension. A virtual fill is permitted only after the 09:30-09:35 task proves `TRADING` and a compliant dual-source opening price. This project is simulation-only, does not connect to a broker and is not investment advice.
