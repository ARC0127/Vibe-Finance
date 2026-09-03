# 2026-09-03 盘前迟到调度：失败治理记录

本轮状态为 `FAILED_TIME_WINDOW_CLOSED`，失败代码 `FAILED_LATE_DISPATCH`，原因 `INVALID_TEMPORAL_PROVENANCE`。08:00 任务在北京时间 16:35:56 才收到调度；实际时钟核验为 16:36:29。08:00 证据截点、09:10 恢复检查和 09:30–09:35 开盘窗口均已过去。

没有创建当日盘前快照、盘前业务报告、订单、成交或金融心跳，也没有执行盘前 `validate/run`。没有使用收盘后信息回填 08:00 事实。本文件及同名 JSON 仅是不可变失败治理证据，不能作为 `READY`、`ORDER_SCHEDULED` 或成交证明。

## 核验结果

- 交易日：2026-09-03 为星期四，不在[上交所 2026 年休市安排](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)和[深交所 2026 年休市安排](https://www.szse.cn/disclosure/notice/general/t20251222_618087.html)的休市区间。此检查在窗口外完成，仅用于日历分类；深交所首次网页抓取超时，Windows HTTPS 重试返回 200。
- 经验校验：`experience-validate PASS`，3 条记录、3 项经验、0 条晋级规则、0 条 `PROPOSED_ONLY`。全部为 `OBSERVED / NON_BINDING`，未影响候选排序、仓位或订单。
- `experience_context` 账本聚合 SHA-256：`fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21`。策略 SHA-256：`05f1733eb7952fd7bdfad2ef931cd6a51016f2de34a4a871f3c82d0e122d5c0a`。完整结构保存在同名 JSON。
- 盘前快照：标准快照、09:10 恢复快照和标准盘前报告均不存在。未补建四类 ETF 的 history、双源价格或 `price_as_of`。配置要求覆盖 13 只场内标的（权益 10、黄金 1、债券 1、货币 1），并加入全部持仓与待单；本轮覆盖状态是 `NOT_REBUILT_TIME_WINDOW_CLOSED`，不是通过。
- 本轮新增订单 0、成交 0、预计新增名义金额 0 元。股票短名单和基金桶未在截点后重新研究为盘前结论；没有新建场外基金申请。

## 当前账本只读观察

以下是在 16:41:38 读取的本地状态，不是 08:00 已知事实或本任务的投资判断：

- 订单日志有 77 个事件；组合与日志中的待单 ID 一致。
- 既有 `PENDING_NEXT_OPEN` 为 `SELL 518880 × 100`，订单 ID `0e87aeb5b1654d9ea74ab2c77fe1d63d`，信号时间 2026-09-02 16:30。按信号价 8.902 元计算名义金额为 890.20 元。本轮没有重新确认其盘前身份、ST/退市或公司行为状态，也没有成交。
- `PENDING_NEXT_NAV=0`；6 个持仓、6 个经济暴露组。16:30 组合估值为 30,174.40 元，现金 9,274.50 元；累计成交 20 笔，最后成交日 2026-09-02。未计算本轮盘前风险或分散度投影。
- 订单、组合及心跳的观察时 SHA-256 见同名 JSON；三者在本任务 `update-readme` 前后均未变化。

## 文件与同步边界

收盘任务 `95b0f67d53e6419eac366d203ecc6f3f` 已生成的 2026-09-03 收盘输入、报告、事务，以及文档任务和订单就绪任务的文件均予保留；不纳入本次盘前同步，也不作为盘前证据。`update-readme` 已执行，前后内容 SHA-256 均为 `ae3a676373402b93f7c69129d95b10d954a4d6cb5bfba9550ee8fbc52f679564`，未改变其他任务的 README 内容。

仅通过 `scripts/sync_github.sh preopen-review FAILED_TIME_WINDOW_CLOSED` 同步本失败 JSON/Markdown 和脚本生成的自有清单；同步与测试结果由 `reports/automation-runs/preopen-review/` 的本轮清单记录。没有直接执行 Git add/commit/push，没有改策略或经验记录。

已过去的时间窗口不可通过代码修复。下一次自动重试点为 **2026-09-04 08:00 Asia/Shanghai**；仍须使用及时证据并遵守原风控门禁。

本项目仅做中国大陆股票和基金虚拟实验，不连接券商，不构成投资建议。DeepSeek 本轮调用 0 次、费用 0 元。
