# 受治理的 Skill 记忆

## 目标

把已结束会话中的可复用流程沉淀为候选 Skill，并每周用 `skill-creator` 核对。候选不会自动安装、自动触发或获得金融操作权限。

```text
session end
    -> redact + deterministic extraction
    -> local candidate package
    -> weekly structural review
    -> skill-creator semantic review
    -> explicit approve / revise / reject
    -> manual installation only
```

## 两层边界

### 会话结束

`GovernedSkillMemoryProvider.on_session_end()` 或 CLI 生成：

```text
artifacts/skill-memory/candidates/<skill-name>--<source-hash>/
├── SKILL.md
├── agents/openai.yaml
└── references/provenance.json
```

规则：

- 原始会话不落盘，只保存规范化源载荷的 SHA-256；
- API key、token、private key 和常见 secret 赋值先脱敏；
- 明显提示注入行不进入 Skill；
- `allow_implicit_invocation: false`；
- provenance 固定两个生成文件的 SHA-256；
- 重跑相同会话保持幂等，篡改候选后失败关闭；
- 候选位于 `artifacts/`，不进入 `skill-memory-review` 的 Git 同步白名单。

推荐让会话宿主在结束时提供结构化 `skill_draft`：

```json
{
  "schema_version": 1,
  "session_id": "opaque-session-id",
  "ended_at": "2026-07-24T16:00:00+08:00",
  "messages": [
    {"role": "user", "content": "原始请求"},
    {"role": "assistant", "content": "执行结果"}
  ],
  "skill_draft": {
    "name": "review-virtual-order-evidence",
    "title": "Review Virtual Order Evidence",
    "description": "Audit a simulation-only evidence workflow.",
    "triggers": ["a repeatable evidence audit is requested"],
    "workflow": ["Read authoritative inputs.", "Bind conclusions to hashes."],
    "guardrails": ["Never connect a broker."],
    "validation": ["Run task-specific tests."]
  }
}
```

若没有 `skill_draft`，provider 只从 assistant 消息中确定性提取流程。没有可判定流程时返回 `NO_DECIDABLE_SKILL_MEMORY`，不会为了“有记忆”而制造空 Skill。

CLI：

```bash
python3 -m vibe_finance skill-memory-session-end \
  --session /path/to/session.json \
  --name review-virtual-order-evidence
```

## 每周核对

建议每周在 Asia/Shanghai 的受治理低峰窗口运行：

```bash
python3 -m vibe_finance skill-memory-review --date YYYY-MM-DD
```

输出位于：

```text
reports/skill-memory/reviews/YYYY-MM-DD/review-<input-hash>.json
reports/skill-memory/reviews/YYYY-MM-DD/review-<input-hash>.md
```

确定性预检覆盖：

- 文件集合、symlink 和 provenance 状态；
- `SKILL.md` frontmatter、工作流、护栏和验证段；
- `agents/openai.yaml` 的显式调用策略；
- 固定文件哈希、凭据残留和提示注入；
- 重复 Skill 名称与合并核对需求。

该报告中的 `skill_creator_semantic_review` 初始为 `PENDING`。定时 Codex 任务应再使用 `$skill-creator`：

1. 读取本文件和最新周审报告；
2. 对每个通过结构预检的候选运行 `skill-creator/scripts/quick_validate.py`；
3. 检查 description 是否能准确触发、正文是否简洁、流程是否可迁移；
4. 对同名候选给出 merge/revise/reject 建议；
5. 只报告建议，不复制到 Codex skills 目录，不修改金融状态；
6. 如需持久化周审报告，只使用：

```bash
scripts/sync_github.sh skill-memory-review <status>
```

## 激活

激活不属于自动流程。只有用户明确批准某个候选后，才能：

1. 重新检查 provenance 和当前文件哈希；
2. 处理重复、冲突和过期规则；
3. 用 `skill-creator` 修改并再次运行 `quick_validate.py`；
4. 复制到明确指定的 skills 目录；
5. 记录候选 ID、批准者、批准时间和最终文件哈希。

周审通过、结构校验通过或生成成功都不等于激活批准，也不构成金融或科学结果。
