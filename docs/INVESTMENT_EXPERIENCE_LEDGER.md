# 投资经验账本

## 定位

投资经验账本是 Vibe Finance 的项目级长期记忆。它保存已经发生的观察、待检验假设、支持证据、反例、适用范围、验证状态和可能的策略影响。新对话、盘前任务和反思任务读取同一组 Git 版本化记录，不依赖某个对话的临时上下文。

账本不拥有修改策略的权限。经验记录中的建议只有同时满足闭环成交样本、走前检验、独立样本外检验、回滚重放和受保护静态门禁后，才能被标记为 `PROMOTED_RULE`。实际运行仍以 `config/strategy.json` 为准。

## 存储模型

每次反思在自己的不可变进化目录中写一个记录：

```text
reports/evolution/<run-id>/experience.json
```

同一经验可以追加新版本。新版本使用相同的 `experience_id`，把 `revision` 加一，并在 `previous_record_sha256` 中引用上一版。历史文件不得覆盖、重命名或删除。

记录至少包含：

- `observation`：已经观察到的现象；
- `hypothesis`：可证伪假设及反证条件；
- `evidence`：支持证据的仓库相对路径、SHA-256 和对应主张；
- `counterevidence`：反例或削弱结论的证据；
- `counterevidence_search`：反例搜索状态和范围；
- `scope`：市场、资产类型、持有期、市场状态和排除项；
- `validation`：`OBSERVED`、`HYPOTHESIS`、`TESTING`、`SUPPORTED` 或 `INVALIDATED`，以及闭环样本数、指标和限制；
- `strategy_impact`：`NONE` 或 `PROPOSED_ONLY`，包括目标参数、候选改动和回滚条件；
- `record_sha256`：记录正文的规范化哈希。

经验文件不能自行声明 `PROMOTED_RULE`。晋级状态由读取器根据同目录 `gate.json`、当前 `MODE_LOCK.json`、受保护 verifier 哈希、闭环样本数和当前策略哈希重新计算。

## 反思任务写入

反思任务先在仓库外准备草稿，再写入唯一的新运行目录：

```bash
python3 -m vibe_finance experience-record \
  --draft /tmp/vibe-experience-draft.json \
  --output reports/evolution/<run-id>/experience.json
```

若该经验同时提出策略改动，`proposal.json` 必须加入：

```json
{
  "experience": {
    "path": "experience.json",
    "sha256": "<experience.json 文件 SHA-256>"
  }
}
```

`evolution-gate` 会重新校验经验格式、引用证据和文件哈希。反思任务只能生成经验、proposal、candidate 和评估证据，不能修改历史记录、交易账本、代码、测试或 live 策略。

## 盘前任务读取

`python3 -m vibe_finance run --mode preopen` 自动加载并验证经验账本，把以下内容写入盘前 JSON 和 Markdown：

- 账本聚合 SHA-256；
- 经验数量和不可变版本数量；
- 最新观察与验证状态；
- `PROPOSED_ONLY` 数量；
- 已通过全部门禁且与当前策略哈希一致的规则数量。

未晋级经验只进入复核区，不改变候选排序、仓位、订单或成交。账本损坏、证据哈希漂移、修订链断裂或伪造门禁会让盘前任务失败关闭。

## 校验

```bash
python3 -m vibe_finance experience-validate
```

`PASS` 只证明记录格式、证据哈希、修订链和晋级计算一致。它不证明某项投资假设有效，也不等于策略已经升级。

## 当前晋级条件

一条经验成为策略规则必须同时满足：

1. 最新版本的 `validation.status` 为 `SUPPORTED`；
2. 至少 20 个由订单事件链和哈希 provenance 重放得到的 eligible closed round trips；
3. 走前、独立样本外和回滚重放均由受保护 evaluator 标记为 `VERIFIED`；
4. 同目录门禁结论为 `ACCEPTED`；
5. 门禁固定该经验文件、候选策略和 `MODE_LOCK.json` 的 SHA-256；
6. 当前 `config/strategy.json` 与已接受候选策略哈希一致。

当前仓库尚无受信 evaluator 的 `ACCEPTED` 路径，因此已有经验只能保持观察或 `PROPOSED_ONLY`。

本账本只服务于中国大陆股票和基金的虚拟实验，不连接真实交易，也不构成投资建议。
