# GitHub 自动同步与自主进化证据链

## 当前状态

- 远端：`origin` 已配置为 `ARC0127/Vibe-Finance`。
- 本地 Git 作者信息：已配置。
- GitHub CLI：`MISSING`。
- HTTPS 凭据：`MISSING_OR_UNAVAILABLE`；只读 `git ls-remote origin HEAD` 无法认证。
- 自动推送：`BLOCKED_NOT_ENABLED`。在认证和私有仓库状态核验完成前，8 个定时任务只写本地不可变产物。

不得把 Personal Access Token、API key 或密码写入仓库、自动化 Prompt、脚本参数、报告或日志。优先使用 GitHub CLI 的系统凭据存储完成认证。

## 启用所需条件

1. 安装 GitHub CLI，并在当前运行自动化的同一用户/环境中执行 `gh auth login`。
2. `gh auth status` 成功，且账户对 `ARC0127/Vibe-Finance` 有 push 权限。
3. 核验远端仓库为 private；本项目不应因自动化而变为公开仓库。
4. `git ls-remote origin HEAD` 成功，确认定时任务的非交互环境也能读取凭据。
5. 建立 `codex/vibe-finance-automation` 持久分支，并以 Draft PR 汇总到默认分支；不得让无人审查的任务直接改写 `main`。
6. 为所有 Git 操作使用同一个互斥锁，避免 08:00、09:35、活动监测或人工运行并发提交。

## 每个定时任务的提交合同

每次定时运行先完成自身工作并验证本地产物，然后写出一个不可覆盖的运行清单。清单至少包含：

- `task_id`、任务名称、北京时间开始/结束、运行状态；
- 输入文件及 SHA-256、数据截点、来源等级；
- 新增/修改文件列表，不包含密钥或 Prompt 原文；
- 账本与策略版本、是否生成虚拟订单；
- 测试/校验命令和退出状态；
- DeepSeek 调用次数、成本与剩余预算；
- 对反思任务额外记录 `PROPOSED_ONLY / ACCEPTED / REJECTED`、样本窗口、样本外结果和 `rollback_base`。

建议提交信息：

```text
automation(<task_id>): <YYYY-MM-DD HH:mm CST> <status>
```

一个定时对应一个提交；任务没有业务变化时仍可提交只含运行清单的审计记录。推送失败不得回滚或删除本地产物，下次同步应重试同一个提交，而不是伪造新的成功运行。

## 自主进化专用门禁

GitHub 是证据链，不是放宽策略门禁的理由。反思任务修改 `config/strategy.json` 前仍必须满足：

- 至少 20 笔已完成虚拟交易；
- 预注册假设、训练/验证/独立样本外区间完整；
- 计入费用与压力测试后优于当前版本；
- 最大回撤不恶化；
- 全部测试通过；
- 旧策略、变更理由和回滚基线可追溯。

不满足时只提交 `reports/evolution/` 中的 `PROPOSED_ONLY` 记录，不得修改线上策略。通过时策略改动、测试证据和回滚文件必须处于同一个提交中。

## 安全同步顺序

1. 获取跨任务互斥锁。
2. 确认当前位于自动化分支，且没有不属于本次任务的工作区变化。
3. 执行密钥扫描、JSON/JSONL 校验和相关测试。
4. 只暂存本次运行清单列出的 allowlist 文件，禁止无条件 `git add -A`。
5. 创建带任务身份的提交。
6. 先拉取并安全整合远端自动化分支；发生冲突立即停止并保留本地提交。
7. 推送自动化分支，核验远端提交 SHA。
8. 更新一个长期 Draft PR；不得为每次运行重复创建 PR。

## 各任务的预期 GitHub 产物

| 任务 | 主要产物 |
|---|---|
| 活动监测 | `reports/monitor/` 只读检查结果，不刷新金融心跳 |
| 08:00 盘前复核 | `reports/preopen/` 或研究附录 |
| 09:35 开盘结算 | `reports/execution/`、订单审计与账本 |
| 16:30 收盘分析 | `data/inbox/`、`reports/daily/`、订单与账本 |
| 22:30 基金净值 | 基金输入、`reports/funds/`、订单与账本 |
| 周六反思进化 | `reports/evolution/`，以及通过门禁后的版本化策略 |
| 周日长周期 | 周报、归因和来源质量汇总 |
| 23:10 文档整理 | `reports/document-log/` 与 `docs/DOCUMENT_LOG_INDEX.md` |

