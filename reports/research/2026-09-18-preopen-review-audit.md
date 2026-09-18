# 2026-09-18 盘前复核：执行风险阻塞

`FAILED_PREOPEN_RISK_REVIEW`。生产 CLI 输入校验和运行返回 PASS，保留 `ORDER_SCHEDULED`，但独立组合复核未通过。不可把该状态解读为 READY 或允许成交。原快照、报告、事务与订单均保留，不覆盖。

## 数据与条件订单

- 证据截点 `2026-09-18T08:00:00+08:00`，封存时间 `2026-09-18T08:05:17.190340+08:00`。输入 SHA-256 `8a52c55eee1c7ff0c8203b3eb5c2933ecedfc25eb782153f03ab774d7cb50d8c`。
- 官方沪深 2026 日历共同确认本日为交易日。价格仅来自 9 月 16 日封存及腾讯/新浪原始双源批次。9 月 17 日收盘缺失，不回填。
- 全部 13 只场内 ETF：权益 10、黄金 1、债券 1、现金 1。每只 34 个对齐日期历史点，最大价格年龄 39.7642 小时，低于现行 120 小时时效门禁。
- 保留既有 `PENDING_NEXT_OPEN BUY 518880 ×100`，订单 `54bcdb6c851f4c1c9b363a5efcab26d2`。信号价名义金额 ¥890.50、最低佣金 ¥5.00、限价 ¥9.17215。本轮新增订单 0、成交 0，canonical 仍为 93 个事件。
- 新盘前建议 `ADD / COLD_START_DEFENSIVE`。证券身份、ST/终止风险及公司行为复核由上交所完整公告查询与华安 9 月 17 日历史 PCF 支持，实时状态仍为 `UNVERIFIED_PREOPEN`。新来源日期标签均对应本轮或真实证据日期，不沿用 09-10/09-11 标签。

## 风险阻塞

- 当前小盘成长桶 20.22114%，已超过配置 20%。按封存价买入黄金并扣 ¥5.00 后为 20.22454%。这不是成交结果。
- 同一假设下现金 ¥8935.30 / 30.0616%，权益 ETF 60.9505%，最大单标的 18.3694%，黄金桶 8.9879%。持仓及暴露组为 6/6。其他这些门禁通过，不能抵消小盘成长桶超限。
- 静态代码核验：`vibe_finance/pipeline.py` 的 `_settle_pending` 买入分支检查限价和现金，没有执行期 `bucket_weight_caps` 重检。生成订单时的分配上限不能证明以后成交时仍合规。本轮没有新增测试行情、模拟成交或改动生产代码。
- `config/task_contracts.json` 的 preopen-review 写入/同步根不包含生产代码和测试。安全修复需要受治理代码发布权限/owner，不能改策略、绕过任务 allowlist，或把未执行的修复写成已完成。
- 09:30–09:35 只有新鲜双源开盘价、TRADING、非零成交量、限价、证券/公司行为证据和全部执行期组合门禁都得到证明，才可虚拟成交。超限未解除或 enforcing guard 未验证时必须硬阻断。

## 经验、研究与收尾

- `experience_context` 聚合 SHA-256 `fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21`。3 项 OBSERVED/NON_BINDING 经验、0 晋级规则。快照和盘前 JSON/Markdown 上下文一致，经验没有影响排序、仓位或订单。
- 股票短名单是已封存 WATCH 样本的复核，不是新完成的 30 只动态筛选。目标仍为 30，直接订单合格数 0，缺失财报、ST、调整后双源史等门禁不猜测。
- 复核宽基、红利、成长、行业、黄金、债券、现金及场外桶。最新 9 月 17 日基金快照为 WATCH，净值双源、申赎、费率/规模/经理/持仓/公司行为及同日收盘等门禁未闭合。PENDING_NEXT_NAV=0，不新建场外申请，不按排行榜追买。
- DeepSeek 0 次、费用 0 元。不连接券商。本项目仅做中国大陆股票和基金虚拟实验，不构成投资建议。
- 只用 `scripts/sync_github.sh preopen-review FAILED_PREOPEN_RISK_REVIEW` 做治理同步。测试/manifest/远端 SHA 以自动化 memory 收尾记录为准。
- 下一复核：今日 09:10 订单就绪检查及 09:30–09:35 严格开盘风险门禁。下一计划盘前为 9 月 21 日 08:00（Asia/Shanghai）。

## 可重放审计摘要

```json
{
  "status": "FAILED_PREOPEN_RISK_REVIEW",
  "pipeline_status": "PASS",
  "daily_execution_status": "ORDER_SCHEDULED",
  "run_id": "747a1df04ae54023afb08a4fc31b00b9",
  "input_sha256": "8a52c55eee1c7ff0c8203b3eb5c2933ecedfc25eb782153f03ab774d7cb50d8c",
  "created_at": "2026-09-18T08:05:17.190340+08:00",
  "coverage": {
    "equity_etf": 10,
    "gold_etf": 1,
    "bond_etf": 1,
    "cash_etf": 1
  },
  "history_points_each": 34,
  "missing_sealed_close_sessions": [
    "2026-09-17"
  ],
  "max_price_age_hours": 39.76416666666667,
  "experience_ledger_sha256": "fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21",
  "experience_count": 3,
  "promoted_rule_count": 0,
  "canonical_event_count": 93,
  "orders_sha256": "b40e83f60caf6f47d506736b5264d138ebfc6b3a4e73ee41b48b2764cb159e48",
  "pending_next_open": 1,
  "pending_next_nav": 0,
  "new_orders": 0,
  "fills": 0,
  "order": {
    "id": "54bcdb6c851f4c1c9b363a5efcab26d2",
    "symbol": "518880",
    "side": "BUY",
    "quantity": 100,
    "signal_notional_cny": 890.5,
    "estimated_fee_cny": 5.0,
    "limit_price": 9.17215,
    "fresh_action": "ADD",
    "fresh_signal": "COLD_START_DEFENSIVE"
  },
  "projection_not_fill": {
    "cash_cny": 8935.3,
    "investable_cny": 29723.3,
    "cash_weight": 0.3006160150454357,
    "equity_weight": 0.609505001127062,
    "max_position_weight": 0.18369427351606316,
    "bucket_weights": {
      "industry_equity": 0.07630377515282623,
      "core_equity": 0.18369427351606316,
      "small_growth_equity": 0.20224537652279526,
      "dividend_equity": 0.1472615759353773,
      "gold": 0.08987898382750233
    },
    "bucket_violations": {
      "small_growth_equity": {
        "weight": 0.20224537652279526,
        "cap": 0.2
      }
    },
    "current_small_growth_weight": 0.20221136089181016
  },
  "blockers": [
    "PREEXISTING_SMALL_GROWTH_BUCKET_ABOVE_20_PERCENT",
    "SETTLEMENT_PATH_NO_EXECUTION_TIME_BUCKET_CAP_RECHECK",
    "PREOPEN_TASK_CONTRACT_EXCLUDES_PRODUCTION_CODE_RELEASE"
  ],
  "decision_boundary": "ORDER_SCHEDULED is a pending-state count, not READY or authorization to fill. Must fail closed if fresh opening projection violates any current cap. Code repair needs the governed code-release owner/authority, not a preopen allowlist bypass.",
  "verified_at": "2026-09-18T08:09:16.287474+08:00",
  "next_checks": [
    "2026-09-18 09:10 order guard",
    "2026-09-18 09:30-09:35 fresh-price and full risk proof; no fill without a verified enforcing guard",
    "2026-09-21 08:00 next scheduled preopen"
  ]
}
```
