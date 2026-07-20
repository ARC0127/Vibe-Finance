# Vibe Finance 本地文档与日志索引

最近整理截点：2026-07-20 16:35:27 CST。

本索引只提供当前入口和历史定位，不替代原始文件。历史报告、输入、订单与账本遵循不可覆盖原则；需要更正时新增带时间戳的记录。

## 当前规范

- [项目入口](../README.md)
- [主研究 Prompt](../MASTER_PROMPT.md)
- [模式锁](../MODE_LOCK.md) / [机器模式锁](../MODE_LOCK.json)
- [定时设计](AUTOMATION.md)
- [股票与基金策略异同](STOCK_FUND_STRATEGY.md)
- [天天基金使用规范](TIANTIAN_FUND.md)
- [来源与证据规则](SOURCES.md)
- [DeepSeek 成本审计](DEEPSEEK_COSTS.md)
- [GitHub 自动同步与自主进化证据链](GITHUB_AUTOMATION.md)
- [参考项目](REFERENCE_PROJECTS.md)

## 机器配置

- [策略配置](../config/strategy.json)
- [股票与基金研究池](../config/universe.json)
- [来源注册表](../config/sources.json)

## 当前账本与审计

- [虚拟组合账本](../data/ledger/portfolio.json)
- [流水线心跳](../data/ledger/heartbeat.json)
- [DeepSeek 调用成本日志](../data/ledger/api_costs.jsonl)

`data/ledger/orders.jsonl` 当前不存在，因为还没有产生虚拟订单；这不是解析失败。后续首次订单由流水线创建。

## 点时研究数据

- [2026-07-19 收盘输入](../data/inbox/2026-07-19.json)
- [2026-07-20 收盘输入](../data/inbox/2026-07-20.json)
- [2026-07-20 午间研究快照](../data/research/2026-07-20-midday.json)

## 决策与研究报告

- [2026-07-19 短周期报告](../reports/daily/2026-07-19-short.md) / [机器记录](../reports/daily/2026-07-19-short.json)
- [2026-07-19 长周期报告](../reports/daily/2026-07-19-long.md) / [机器记录](../reports/daily/2026-07-19-long.json)
- [2026-07-20 短周期报告](../reports/daily/2026-07-20-short.md) / [机器记录](../reports/daily/2026-07-20-short.json)
- [2026-07-19 完整研究分析](../reports/research/2026-07-19-analysis.md)
- [2026-07-20 盘前及午间追加分析](../reports/research/2026-07-20-preopen.md)

2026-07-20 盘前报告包含 09:25 主报告以及 12:22、12:25 的追加段落，其中“7 项测试”和 v0.1 回滚基线属于当时截点事实。当前规范和测试状态以 README、配置和最新整理报告为准；不回写历史报告。

## 整理运行

- [2026-07-20 16:35 整理报告](../reports/document-log/2026-07-20-1635.md) / [完整机器清单](../reports/document-log/2026-07-20-1635.json)

## 尚未产生的目录

以下目录要等相应事件首次发生后创建，不应预先伪造空报告：

- `reports/execution/`：股票或场内 ETF 开盘结算；
- `reports/funds/`：场外基金净值订单或结算；
- `reports/evolution/`：反思进化候选或策略升级；
- `reports/monitor/`：启用 GitHub 每任务审计清单后的活动监测记录。
