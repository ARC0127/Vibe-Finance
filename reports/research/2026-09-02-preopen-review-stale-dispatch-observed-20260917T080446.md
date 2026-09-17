# 2026-09-02 盘前过期触发：2026-09-17 失败治理观察

本次处理状态为 `FAILED_TIME_WINDOW_CLOSED / FAILED_LATE_DISPATCH`，原因 `STALE_TRIGGER_DATE_MISMATCH`。最新可见心跳标注 2026-09-02 08:01:36，实际时钟核验为 2026-09-17 08:04:46（Asia/Shanghai）。这是过期重复触发的失败治理记录，不是盘前快照、业务决策报告或成交证明；不改写原 9 月 2 日任务的状态。调度日期错位的根因为 `UNKNOWN`，不能从此观察推断调度器故障原因。

没有执行盘前 `validate/run`，没有新增金融快照、业务报告、订单、成交或心跳；新增订单 0 笔、名义金额 0 元。没有采集开盘后行情或用后续信息回填历史。未修改策略、经验、既有事务或无效证据。时间窗口不可安全恢复，因此没有通过放宽门禁制造成功。

## 证据与核验

- 日历治理复核：9 月 2 日为星期三，不在[上交所 2026 年休市安排](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)和[深交所 2026 年休市安排](https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html)的休市区间。9 月休市为 25–27 日。本检查只用于当前日历分类，不证明历史截点的市场状态。深交所网页及 Windows HTTPS 读取失败后，WSL HTTPS 读取成功；原文 SHA-256 和重试记录见同名 JSON。
- 经验校验 `PASS`：3 条记录、3 项经验、0 条晋级规则，均为 `OBSERVED / NON_BINDING`，未影响排序、仓位或订单。`experience_context` 聚合 SHA-256 为 `fd55fafbcf38eec9495ab512ed64a535a64d5dea85da5e9e1ef6c7e1b2e92e21`；当前策略 SHA-256 为 `05f1733eb7952fd7bdfad2ef931cd6a51016f2de34a4a871f3c82d0e122d5c0a`。这是当前治理校验，不替换历史经验上下文。
- 原 9 月 2 日盘前报告记录为 `ORDER_SCHEDULED`，运行 ID `65a2a24e639d48628a4b5cb514e5744b`。原 governed sync 提交为 `207f8543c2c47d13a5c0c604c89c626369467b37`；其清单中的盘前输入、JSON/Markdown 报告和 prepare/commit 事务共五个不可变产物的 SHA-256 全部匹配。历史报告状态及清单 PASS 仅按原记录披露，不将其解释为本轮成功。
- 当前 canonical 账本保留 9 月 2 日 09:32:23 的虚拟 `BUY 159928 ×100`，成交价 0.674 元、订单 ID `873ca05756ed43b689f7501efbe924fe`。这是已有历史事件，不是本轮新增或重放成交。

## 当前账本观察与边界

当前账本有 93 个事件，组合与 canonical 最新待单 ID 一致。按 9 月 16 日 22:30 估值，项目权益 29,828.30 元、现金 9,830.80 元，持仓 6 只、经济暴露组 6 个。已有唯一 `PENDING_NEXT_OPEN BUY 518880 ×100`，信号为 9 月 16 日 16:30、信号价 8.905 元，信号价名义金额 890.50 元、限价 9.17215 元。`PENDING_NEXT_NAV=0`。这些后续日期的状态不是 9 月 2 日 08:00 已知事实，也未由本轮确认 READY。

配置仍要求覆盖权益 ETF 10、黄金 ETF 1、债券 ETF 1、现金 ETF 1，并加入全部持仓与待单。本轮没有补建 history、双源封存价或 `price_as_of`，没有重新研究股票短名单和基金桶，也没有计算过期盘前风险投影；覆盖状态为 `NOT_REBUILT_TIME_WINDOW_CLOSED`。身份、ST/退市及公司行为在本次重复处理中的状态明确为 `UNVERIFIED`，不自行写成 CLEARED。

## 治理同步

`update-readme` 已完成且字节不变；订单、组合、金融心跳 SHA-256 也未变化，详见同名 JSON。运行开始时 `main`、HEAD 与 `origin/main` 均为 `314d5d110d1c9b9b58c906a49dcbd8e157293f07`。

仅运行 `scripts/sync_github.sh preopen-review FAILED_TIME_WINDOW_CLOSED`。预期载荷只有本失败 JSON/Markdown 和脚本生成的自有清单，不纳入任何金融状态、历史产物或其他任务的未跟踪文件。测试、同步结果及最终本地/远端 SHA 以本轮 governed manifest 和自动化 memory 的收尾记录为准。

下一计划盘前重试点为 **2026-09-18 08:00 Asia/Shanghai**，须由新的正确日期触发并采用对应及时证据；本条过期心跳不授权替建 9 月 17 日业务周期。

本项目仅做中国大陆股票和基金虚拟实验，不连接券商，不构成投资建议。DeepSeek 0 次、0 元。
