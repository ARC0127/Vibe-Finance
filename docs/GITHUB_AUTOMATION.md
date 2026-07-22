# GitHub 自动同步与自主进化证据链

## 当前状态

- 远端：`origin` 已配置为 `ARC0127/Vibe-Finance`。
- 本地 Git 作者信息：已配置。
- GitHub CLI：2.96.0，安装在 `/home/arc/.local/bin/gh`，官方 SHA-256 校验通过。
- 认证账户：`ARC0127`；配置文件权限为 0600，Git HTTPS 凭据助手已由 `gh auth setup-git` 配置。
- 远端核验：仓库允许为 `PUBLIC` 或 `PRIVATE`；当前公开仓库下账户权限为 `ADMIN`，非交互 `git ls-remote` 已通过。
- 同步分支：用户已明确授权由本项目独占维护仓库；9 个业务定时在自身校验通过后立即提交并推送 `main`，不再设置 02:00 集中任务或每日提交次数限制。
- WMI 状态：外部安全流程已由用户宣告终止；2026-07-22 13:47（Asia/Shanghai）按明确授权恢复唯一 `.git`，禁用标记已不存在。本地与 GitHub `main` 均指向 `876daa47b66ce374fee1122ec99ce8607a5bba32`。
- 历史迁移：[Draft PR #1](https://github.com/ARC0127/Vibe-Finance/pull/1)只记录从自动化分支切换到直接维护 `main` 前的配置历史，迁移完成后关闭，不再追加定时提交。
- Codex 全局能力：同一 WSL 用户运行的其他 Codex 项目可以使用 `gh` 和 GitHub 凭据，但不会自动获得 Vibe Finance 的推送策略。

不得把 Personal Access Token、API key 或密码写入仓库、自动化 Prompt、脚本参数、报告或日志。优先使用 GitHub CLI 的系统凭据存储完成认证。

## 启用所需条件

基础条件已完成：安装并认证 GitHub CLI、确认仓库为 PUBLIC 且当前账户权限为 ADMIN、验证本地仓库与 GitHub `main` 一致，以及用户对直接维护 `main` 的明确授权。每次业务任务同步仍必须：

1. 只在本地 `main` 与 `origin/main` 完全一致时开始同步；发现远端漂移立即停止，不自动 rebase 或覆盖；
2. 使用项目同步互斥锁，避免多个业务任务并发提交；
3. 通过密钥扫描、JSON/JSONL 解析、`git diff --check` 和测试后才允许提交；
4. 排除 WMI 隔离目录、环境文件和私钥，禁止无保护的 `git add -A`；
5. 只提交当前任务及其闭环依赖的明确文件，不混入仓库外内容。

## 运行清单与每日提交合同

每次定时运行先完成自身工作并验证本地产物，然后写出一个不可覆盖的运行清单。清单至少包含：

- `task_id`、任务名称、北京时间开始/结束、运行状态；
- 输入文件及 SHA-256、数据截点、来源等级；
- 新增/修改文件列表，不包含密钥或 Prompt 原文；
- 账本与策略版本、是否生成虚拟订单；
- 测试/校验命令和退出状态；
- DeepSeek 调用次数、成本与剩余预算；
- 对反思任务额外记录由静态 verifier 计算的 `PROPOSED_ONLY / REJECTED / NOT_APPLICABLE`、gate SHA、派生样本数、样本窗口和回滚基线；调用方状态不拥有进化结论权。

任务提交信息：

```text
automation(<task_id>): <YYYY-MM-DD HH:mm CST> <status>
```

每个业务定时对应自己的即时提交；没有变化时记录 `NO_CHANGES`，不创建空提交。推送失败不得回滚或删除本地产物，后续运行应继续处理未持久化内容，不得伪造成功运行。

## 自主进化专用门禁

GitHub 是证据链，不是放宽策略门禁的理由。反思任务不能直接修改 `config/strategy.json`。未来若开放由 verifier 执行的 promotion，仍必须同时满足：

- 至少 20 个从哈希链合法订单状态机重放、仓位精确回零且具有哈希绑定 provenance 的 eligible completed round trips；
- 预注册假设、训练/验证/独立样本外区间完整；
- 计入费用与压力测试后优于当前版本；
- 最大回撤不恶化；
- 全部测试通过；
- 旧策略、变更理由和回滚基线可追溯。

不满足时只提交 `reports/evolution/` 中的 `PROPOSED_ONLY` 记录，不得修改线上策略。当前可信 WF/OOS evaluator registry 尚不存在，因此 `ACCEPTED` 路径被代码明确禁用；不能用手写 JSON、环境变量或同步状态绕过。以后若实现可信 evaluator，promotion 仍只能由受保护 verifier 执行，且策略改动、测试证据和回滚文件必须处于同一个提交中。

当前门禁入口：

```bash
python3 -m vibe_finance evolution-gate \
  --proposal reports/evolution/<run-id>/proposal.json \
  --baseline-ref <full-40-character-ancestor-commit> \
  --output reports/evolution/<run-id>/gate.json
```

`proposal.json` 不得含 `decision` 或 `rollback_base`。候选与证据路径必须位于同一新建 run 目录，包含 SHA-256，且不能使用绝对路径、`..` 或符号链接逃逸。

订单完整性由 `config/ledger_legacy_anchor.json` 与 `vibe_finance/evolution.py` 共同执行：现有四行是 Git 锚定只读 legacy prefix，未来第一条 v2 事件从 sequence 5 接续；每次验证还要求 `HEAD` 中已提交账本是工作账本的 canonical 前缀，因而不能通过整体重算 v2 后缀改写已提交历史。新事件通过 side lock、单次 append 和 `fsync` 写入；合法成交还必须有唯一的先前 pending、字段一致与有效 fill 时间。组合持仓与成交数必须能从事件重放得到，修改 `filled_trade_count` 或直接伪造 `FILLED` 均不能增加验收样本。portfolio 与多事件 append 的跨文件事务仍未实现，当前不主张崩溃/并发一致性。

## 安全同步顺序

1. 获取项目同步互斥锁。
2. 确认当前位于 `main`，且本地历史与 `origin/main` 一致；远端漂移时失败关闭。
3. 执行密钥扫描、JSON/JSONL 校验、`git diff --check` 和完整测试。
4. 只暂存当前任务及闭环依赖的明确项目文件，排除环境文件与私钥。
5. 创建任务提交并直接推送 `main`；服务器拒绝时保留本地产物，不强推。
6. 核验 `origin/main` 与本地提交 SHA 一致。

业务任务统一入口：

```bash
scripts/sync_github.sh <task-id> <status>
```

脚本要求唯一仓库根、`main`、安全远端历史、凭据扫描、任务 allowlist 和测试通过。常规定时使用各自 task-id；人工确认本期全部工作均属同一发布范围时，可使用 `current-period-release`。脚本不允许 force push、自动 rebase、删除或改写历史报告。

## 各任务的预期 GitHub 产物

| 任务 | 主要产物 |
|---|---|
| 活动监测 | `reports/monitor/` 只读检查结果，不刷新金融心跳 |
| 08:00 盘前复核 | `reports/preopen/` 或研究附录 |
| 09:10 订单就绪检查 | 补单/失败证据、盘前报告与账本 |
| 09:35 开盘结算 | `reports/execution/`、订单审计与账本 |
| 16:30 收盘分析 | `data/inbox/`、`reports/daily/`、订单与账本 |
| 22:30 基金净值 | 基金输入、`reports/funds/`、订单与账本 |
| 周六反思进化 | 仅新增 `reports/evolution/<run-id>/`；可信 evaluator 未实现前禁止策略 promotion |
| 周日长周期 | 周报、归因和来源质量汇总 |
| 23:10 文档整理 | `reports/document-log/` 与 `docs/DOCUMENT_LOG_INDEX.md` |
