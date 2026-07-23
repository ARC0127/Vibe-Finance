# 自动化设计

所有时间使用 `Asia/Shanghai`。Codex 自动化负责唤醒研究任务；Python 流水线负责数据校验、信号排序、订单、费用、账本和不可覆盖报告。

## 工作日 08:00：盘前决策

- 核验交易日、隔夜公告、停复牌、公司行为、基金状态和已有订单。
- 使用最近封存的收盘价格与历史，补充截至08:00已公开的事件信息。
- 创建 `data/inbox/YYYY-MM-DD-preopen.json`，`market_state` 必须为 `preopen`。
- 先运行 `validate`，再运行：

```bash
python3 -m vibe_finance run \
  --input data/inbox/YYYY-MM-DD-preopen.json \
  --mode preopen \
  --report-dir reports/preopen
```

- 有收盘订单时复核并保留或取消；没有订单时，策略必须生成常规信号或小仓位每日探索订单。
- 盘前订单可以在当天09:35结算，但必须满足 `signal_as_of < fill_as_of`。
- `UNVERIFIED_PREOPEN` 只表示盘前尚无实时成交证明，不等同于确认停牌。对双源收盘价通过、无公司行为冲突的场内 ETF，可以生成条件订单；09:35 必须重新证明 `TRADING` 并通过双源开盘价校验，否则取消。

## 工作日 09:10：订单就绪检查

- 这是开盘结算前的恢复任务，不采集开盘后价格。
- 检查正常交易日是否至少存在一笔 `PENDING_NEXT_OPEN`。
- 如果08:00任务失败或没有订单，重新构建盘前快照并运行同一 `preopen` 流水线。
- 已有合规订单时只报告 `READY`，禁止重复下单。
- 09:10之后仍无订单时返回失败，触发通知。

## 工作日 09:30–09:35：双源封存与开盘虚拟成交

- 任务在09:29启动只读预备；09:30后先运行封存命令，再做文档审计或结算，防止自动化准备工作耗尽价格窗口。
- 只结算此前形成的虚拟订单，不使用开盘结果反向制造信号。
- `capture-open` 只接受09:30:00至09:35:00源内时间戳，并独占创建输出；窗口外、文件已存在、任一标的缺源、价格冲突、时间偏移或零成交量均失败且不写文件。
- 场内快照覆盖 `config/strategy.json:data_collection.daily_snapshot_asset_types` 指定的全部研究池标的，当前包括权益、黄金、国债与现金ETF；持仓和 `PENDING_NEXT_OPEN` 标的无条件并入。
- 自 `daily_snapshot_coverage_effective_date` 起，正常交易日快照缺少上述任一资产类型时，`validate` 直接失败；不能以“无信号”为由省略防御资产。
- 聚合行情只证明双源价格与实际成交活动。代码身份、基金属性和公司行为状态必须从更早的不可变盘前快照继承；未标记为 `CLEARED` 或 `NO_UNADJUSTED_ACTION_FOUND_AT_CUTOFF` 的待单不得成交。

```bash
python3 -m vibe_finance capture-open \
  --base-snapshot data/inbox/YYYY-MM-DD-preopen.json \
  --output data/inbox/YYYY-MM-DD-open.json
python3 -m vibe_finance validate --input data/inbox/YYYY-MM-DD-open.json
python3 -m vibe_finance settle-open \
  --input data/inbox/YYYY-MM-DD-open.json
```

- 正常交易日成交少于一笔时，流水线返回 `FAILED_DAILY_TRADE` 并记录 `DAILY_TRADE_REQUIREMENT_MISSED`。
- 价格缺失或冲突、超过限价、资金不足、持仓不足、停牌或未处理公司行为会取消订单。系统不伪造成交。
- 股票ETF按T+1处理；同日买入份额不能同日卖出。

## 工作日 16:30：收盘分析

- 保存不可覆盖的 `data/inbox/YYYY-MM-DD.json`。
- 对趋势延续、受控回撤、防御、退出和每日探索信号进行统一排序。
- 股票和场内ETF订单进入 `PENDING_NEXT_OPEN`；场外基金只形成研究判断。
- 当天已经成交后，仍要为下一交易日准备订单。

## 工作日 22:30：场外基金净值

- 优先检查基金公司或法定披露，并强制用天天基金交叉核验净值日期、申赎状态、费率、规模、经理、持仓与公告。
- 新申购登记为 `PENDING_NEXT_NAV`。只有信号日之后公开并完成双源核验的确认净值才能结算。
- 场外基金订单不进入09:30–09:35开盘任务。

## 每6小时：活动监测

- 运行 `python3 -m vibe_finance status`。
- 只读检查心跳、最近报告、账本、待执行订单和DeepSeek预算。
- 监测任务不能刷新金融心跳，也不能创建订单。

## 周六 20:30：反思与策略进化

- 分别统计最近5、20和60个交易日的收益、回撤、费用、成交偏差与取消原因。
- 分离趋势、回撤、防御、股票、场内基金和场外基金的贡献。
- 参数升级至少需要20笔完成交易，并通过走前和未参与调参的样本外检验。
- 未通过的修改保留为 `PROPOSED_ONLY`，不能直接改变线上策略。

## 周日 20:00：长期复盘

- 汇总一周订单、持仓、费用、来源失败和组合暴露。
- 比较策略版本与基准，记录可证伪假设和下一周重点。
- 权威 Markdown/JSON 周报通过校验后，若 `Default templates` 与 Spreadsheets 能力可用，使用 `Analytics Dashboard` 模板生成 `artifacts/weekly-dashboard/YYYY-Www-vibe-finance.xlsx`。
- Excel 只用于展示，不参与信号、订单或账本计算；模板不可用时记录 `TEMPLATE_EXPORT_SKIPPED`，不得让周复盘失败。
- `Investment Banking` 插件不用于本项目的公开股票或基金决策；详细边界见 `docs/PLUGIN_POLICY.md`。

## 每日 23:10：文档与日志

- 更新索引、校验JSON/JSONL、检查Markdown链接、孤立文件和同日重复版本。
- 运行 `python3 -m vibe_finance update-readme`，同时替换 README 中带标记的公开账本、每日市场策略、执行结果和滚动五日计划；数据来自正式账本、最新不可变报告及其 SHA-256 匹配快照，不得沿用首日静态文案。
- 不删除、移动或覆盖历史金融产物。

## GitHub 同步

每个业务任务完成本地产物和校验后，立即运行对应的 `scripts/sync_github.sh <task-id> <status>`，不再等待 02:00，也不设置每日提交次数限制。脚本负责互斥、凭据扫描、JSON/JSONL 解析、测试、任务 allowlist、提交、推送和远端 SHA 核验。

仓库由本项目独占维护，用户授权直接更新 `main`。公开仓库可以同步，但任何凭据命中、远端历史冲突、测试失败或账本不一致仍必须失败关闭；不得 force push、自动 rebase、覆盖历史金融报告或上传密钥。

## 高频状态

高频任务保持关闭。只有许可明确的点时数据、滑点与成交模型、走前回测、独立样本外结果全部通过后，才允许单独评估高频实验。

## 内容边界

自动化只处理公开金融数据与虚拟实验产物。不进行社会议题分析，不生成或上传政治相关内容。所有报告必须明确：本项目不连接真实交易，不构成任何投资建议。
