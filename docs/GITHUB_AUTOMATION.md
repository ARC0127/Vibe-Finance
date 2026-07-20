# GitHub 自动同步与自主进化证据链

## 当前状态

- 远端：`origin` 已配置为 `ARC0127/Vibe-Finance`。
- 本地 Git 作者信息：已配置。
- GitHub CLI：2.96.0，安装在 `/home/arc/.local/bin/gh`，官方 SHA-256 校验通过。
- 认证账户：`ARC0127`；配置文件权限为 0600，Git HTTPS 凭据助手已由 `gh auth setup-git` 配置。
- 远端核验：仓库为 `PRIVATE`，当前账户权限为 `ADMIN`，非交互 `git ls-remote` 已通过。
- 同步分支：用户已明确授权由本项目独占维护仓库；8 个定时直接提交并推送 `main`。
- 历史迁移：[Draft PR #1](https://github.com/ARC0127/Vibe-Finance/pull/1)只记录从自动化分支切换到直接维护 `main` 前的配置历史，迁移完成后关闭，不再追加定时提交。
- Codex 全局能力：同一 WSL 用户运行的其他 Codex 项目可以使用 `gh` 和 GitHub 凭据，但不会自动获得 Vibe Finance 的推送策略。

不得把 Personal Access Token、API key 或密码写入仓库、自动化 Prompt、脚本参数、报告或日志。优先使用 GitHub CLI 的系统凭据存储完成认证。

## 启用所需条件

以下条件现已完成：安装并认证 GitHub CLI、确认 private/ADMIN、验证非交互 Git，以及用户对直接维护 `main` 的明确授权。所有运行仍必须：

1. 只在本地 `main` 与 `origin/main` 完全一致时开始同步；发现远端漂移立即停止，不自动 rebase 或覆盖；
2. 使用同一个互斥锁，避免 08:00、09:35、活动监测或人工运行并发提交；
3. 通过密钥扫描、JSON/JSONL 解析、`git diff --check` 和测试后才允许提交；
4. 只暂存当前任务 allowlist 内的文件，禁止无条件 `git add -A`。

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
2. 确认当前位于 `main`，本地 HEAD 与 `origin/main` 一致，且没有不属于本次任务的工作区变化。
3. 执行密钥扫描、JSON/JSONL 校验和相关测试。
4. 只暂存本次运行清单列出的 allowlist 文件，禁止无条件 `git add -A`。
5. 创建带任务身份的提交。
6. 创建带任务身份的本地提交并直接推送 `main`；服务器拒绝时保留本地提交，不强推。
7. 核验 `origin/main` 与本地提交 SHA 一致。

项目统一入口：

```bash
scripts/sync_github.sh <task-id> <status> [--dry-run]
```

脚本支持八个固定 task-id，使用 `$HOME/.cache/vibe-finance/github-sync.lock` 跨任务互斥，并在每次提交前生成 `reports/automation-runs/<task-id>/` 机器审计清单。脚本不允许 force push、自动 rebase、删除或从非 `main` 分支同步。

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
