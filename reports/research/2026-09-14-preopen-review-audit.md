# 2026-09-14 盘前自治购买策略审计

- 状态：`PASS_WITH_PROVENANCE_LABEL_WARNING_AND_OPEN_RECHECK`
- 快照封存：`2026-09-14T09:09:57.586173+08:00`，早于 09:10 恢复截止。
- 决策运行：`3e0a318b2cfa46f38e591e9dbe55264b`；输入 SHA-256 `cc27f39792685827492be9e60b9938aa5b363bacd368d7fe4dddcab2d760b68e`。
- 经验账本：3 条经验、0 条晋级规则；聚合 SHA-256 `fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21`。
- 场内覆盖：equity_etf 10、gold_etf 1、bond_etf 1、cash_etf 1；每只 31 个带日期历史点。
- 实际价格边界由 `base_close=data/inbox/2026-09-11.json`、其 SHA-256 与双源批次 SHA-256 锁定。快照 `price_boundary` 以及两条新增证据的 `source_id` 仍带旧日期，是不可变快照中的非权威标签错误；URL、发布时间、as_of、原文哈希和证据包哈希锁定实际 2026-09-14/2026-09-11 证据，未覆盖原文件。

## 条件订单

- 保留 `PENDING_NEXT_OPEN BUY 518880 x100`，订单 `c9ffdb17b4034c00803afba695eee552`，信号 `DAILY_EXPLORATION_FALLBACK`。
- 信号价 ¥8.943；预计名义金额 ¥894.300，最低佣金 ¥5.0；限价 ¥9.21129。
- 本次新评估：`HOLD / NONE`；未覆盖既有规范订单。
- 证券身份、退市/终止风险及公司行为由上交所公告查询与华安 2026-09-11 历史 PCF 支持；实时状态仍为 `UNVERIFIED_PREOPEN`。
- 09:30–09:35 必须重新证明 TRADING、双源开盘价、非零成交量、限价与全部组合约束，才允许虚拟成交。

## 组合投影（非成交）

- 昨收信号价投影现金 ¥9257.100、可投资资产 ¥29781.200；现金比例 31.0837%，权益 ETF 59.9076%，最大单标的 18.4506%。
- 风险投影违规：无。

## 边界

- 动态股票短名单仅研究，不生成个股订单；场外基金 WATCH，PENDING_NEXT_NAV=0。
- DeepSeek 0 次调用、费用 ¥0；无券商连接、无真实订单、无虚拟成交。
