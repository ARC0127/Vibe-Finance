# DojoAgents 调研与 Vibe Finance 借鉴评估

- 调研日期：2026-07-24（Asia/Shanghai）
- 对象：[Alpha-Dojo/DojoAgents](https://github.com/Alpha-Dojo/DojoAgents)
- 源码快照：[`e674e7e97eaa2edb119d58cdbee582c5b04e6328`](https://github.com/Alpha-Dojo/DojoAgents/commit/e674e7e97eaa2edb119d58cdbee582c5b04e6328)
- 包快照：[PyPI `dojoagents` 0.1.8](https://pypi.org/project/dojoagents/0.1.8/)
- 评估边界：工程架构与治理模式；不评估、背书或复用其投资策略

## 结论

DojoAgents 最值得 Vibe Finance 吸收的不是“多代理炒股”，而是把开放式 Agent 行为收束为可审计工作流的几个接口：

1. 任务契约、产物规格、输出校验、流水线前置检查；
2. 工具结果中的稳定调用标识、耗时、截断、产物和资源变更；
3. 以 `run_id + seq + call_id` 关联事件流；
4. 将原始会话、长期记忆和导出工件分开；
5. 对重复失败和无进展工具调用设置循环护栏；
6. 研究证据先固化为 evidence pack，再由综合阶段按 `evidence_ids` 引用。

但它不能作为金融正确性或安全性的外部证据。当前项目年轻，未发现针对 DojoAgents 本身的同行评审论文、公开金融绩效基准或独立安全评估。README、演示和项目热度只能证明功能展示与社区关注，不能证明投资收益、行情点时正确性或真实隔离强度。

本轮先吸收第 1 项的核心模式，随后补充了受治理的会话 Skill 记忆实现。两者都按 Vibe Finance 的要求加强：无依赖引入、无 `--force` 绕过、真实券商接口显式禁止、运行写入边界与同步白名单分离，并且自动生成的 Skill 只能进入本地候选区，不能自动激活。

## 一、DojoAgents 的实际架构

### 1. 通用 Agent 层

DojoAgents 将对话循环、工具执行、上下文压缩、护栏、事件和展示器拆开。`ToolResult` 不只返回文本，还携带 `call_id`、`ok`、`latency_ms`、`truncated`、`data`、`artifacts`、`resource_changes` 和元数据。前端可据 `resource_changes` 刷新受影响资源，而不是根据工具名猜测副作用。

证据：

- [`ToolResult`](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/agent/models.py)
- [`AgentEvent` 与事件汇聚](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/agent/events.py)
- [工具契约文档](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/docs/site/en/reference/tool-contracts.md)

### 2. 金融任务层

它没有把所有金融规则塞进通用 Agent Loop，而是用 Task/Pipeline 层描述可重复流程。`TaskContract` 包含任务版本、harness、输入、输出、所需工具、触发方式、约束和下游；产物可声明 JSON Schema；流水线推进前先验证上一步输出。

`output_validation.py` 还会拒绝空输出、占位符和只描述“将来要做什么”的元输出。这一点适合阻止 Agent 把“计划完成”伪装成“产物完成”。

证据：

- [`TaskContract`、`PipelineSpec`](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/tasks/models.py)
- [输出校验](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/tasks/output_validation.py)
- [任务流水线](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/tasks/pipeline.py)
- [Tasks and Pipelines 文档](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/docs/site/en/user-guide/tasks-and-pipelines.md)

### 3. 证据到综合

其 Theme Deep Dive 流程先产生 evidence pack，再由综合阶段生成 Driver、Impact、Risk，并要求条目绑定 `evidence_ids`；定量贡献者也要与证据包中的排序一致。这个结构比“让模型读一堆网页后直接给结论”更可审计。

可借鉴点是“证据对象先稳定、综合结论后生成”，不是其具体市场观点。

### 4. 会话、记忆与运行事件

DojoAgents 区分 session 后端、turn sidecar、memory sidecar 和导出工件，并为运行事件提供稳定标识。这适用于恢复长任务、关联工具开始/结束以及定位压缩前后的上下文状态。

其 Guardrail 会识别：

- 相同工具调用的重复失败；
- 相同只读调用连续返回同一结果、没有进展；
- 达到阈值后从警告升级为阻断。

这类护栏适合未来放在 Vibe Finance 的 Agent 编排层，不应替代金融状态机、交易日历和证据门禁。

## 二、需要警惕的边界

### 1. “Sandbox”名称强于当前策略实现

README 将 sandbox 描述为安全隔离环境，但在所审计快照中，`SandboxPolicy.check_tool()` 直接返回 `None`。这说明至少在这个策略抽象层，`allowed_roots`、`allow_network` 和 `allowed_commands` 没有形成工具级强制检查。

证据：[sandbox policy 源码](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/tools/sandbox.py)

因此：

- 不引入通用代码执行工具；
- 不把“配置了 sandbox”当成隔离已验证；
- 不用它替代 Vibe Finance 的路径白名单、原子事务、不可变证据和同步门禁。

### 2. 自动把完整会话写成 Skill 风险较高

`SkillSummaryMemoryProvider.on_session_end()` 会拼接消息内容并写入生成的 `SKILL.md`。在该模块中没有看到人工批准、证据筛选、敏感信息清理或来源可信度门。

证据：[skill summary memory 源码](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/memory/skill_summary.py)

对 Vibe Finance，直接复制该实现会放大提示注入持久化和错误流程固化风险。会话内容不能自动升级为治理规则；任何长期规则都应经过单独审阅，并绑定来源与版本。

本项目因此采用“两层记忆”：

1. 会话结束时自动脱敏并生成本地候选 Skill，不保存原始会话，不允许隐式触发；
2. 每周定时进行结构、哈希、凭据、提示注入、重复名称和 `skill-creator` 语义核对；
3. 定时任务只给出 approve/revise/reject 建议，实际安装仍需用户显式批准。

实现说明见 [`docs/SKILL_MEMORY.md`](../../docs/SKILL_MEMORY.md)。

### 3. `--force` 不适用于硬金融门禁

DojoAgents 的 pipeline preflight 支持 `force` 绕过。它对一般工作流调试可能有用，但交易日、点时证据、真实券商禁用、账本一致性和同步治理属于不可由调用者放宽的硬约束。

证据：[preflight 源码](https://github.com/Alpha-Dojo/DojoAgents/blob/e674e7e97eaa2edb119d58cdbee582c5b04e6328/dojoagents/tasks/preflight.py)

本轮契约明确要求 `caller_force_bypass=forbid` 且每个任务 `allow_force_bypass=false`。

### 4. 上下文压缩不能成为金融事实来源

0.8 阈值压缩、token ledger 和会话摘要对长对话有用，但摘要可能丢失时间戳、证据 ID、`UNKNOWN`/`PENDING` 和否定条件。它只能服务交互上下文，不能替代账本、订单状态、原始行情快照或哈希绑定报告。

### 5. 多代理、定时器和消息网关暂不引入

这些能力扩大自主运行范围、成本和攻击面，却不直接提高点时证据质量。当前 Vibe Finance 的主要瓶颈是数据有效性、样本外评估和真实开盘证据，而不是代理数量。

## 三、与 Vibe Finance 的能力对照

| 维度 | DojoAgents | Vibe Finance | 判断 |
|---|---|---|---|
| Agent 编排 | 通用循环、工具、事件、压缩、并发较完整 | 以确定性 CLI 和受治理自动化为主 | 借鉴接口，不引入运行时 |
| 任务契约 | Task/Pipeline/Schema 较系统 | 规则分散在 CLI、脚本、文档和测试 | 本轮补齐机器可读契约 |
| 输出真实性 | 拒绝占位符并验证 JSON/JSONL schema | 关键输入和事务已有专用验证 | 可在未来扩展为每类报告 schema |
| 证据引用 | evidence pack + `evidence_ids` | 已有点时输入和 SHA，但部分报告仍偏全局证据列表 | 下一阶段高价值项 |
| 状态一致性 | session/resource change/event 驱动 | 原子 prepare/commit、账本投影与恢复更强 | 保留 Vibe 实现 |
| 安全边界 | 有配置和 guardrail；所审计 sandbox policy 未强制 | 仿真锁、无券商、路径白名单、同步门禁 | 不替换现有门禁 |
| 长期记忆 | 支持自动生成 skill | 自动生成隔离候选；每周核对；人工激活 | 采用受治理变体 |
| 金融有效性 | 产品功能与演示为主 | 明确区分机械验证、仿真和金融结果 | 继续以独立评估为准 |

## 四、吸收路线

### 已吸收

1. 新增机器可读任务契约注册表，覆盖现有 10 个治理任务。
2. 分离 `runtime_write_roots` 与 `sync_allowlist`，防止把“允许同步”误解为“允许任务写入”。
3. 每个任务声明副作用类别、验收检查、约束和禁止强制绕过。
4. 注册表全局锁定：
   - `simulation_only=true`
   - `real_broker_integration=forbid`
   - `caller_force_bypass=forbid`
   - `timezone=Asia/Shanghai`
5. 新增只读审计命令，将契约任务 ID 和白名单逐项绑定到 `scripts/sync_github.sh`。
6. 新增失败关闭测试：券商开关、force bypass、路径逃逸、未知任务和同步白名单漂移都会拒绝。
7. 新增 `GovernedSkillMemoryProvider`，可在会话结束时自动生成脱敏、哈希绑定、不可隐式触发的候选 Skill。
8. 新增每周 Skill 记忆审计，候选留在本地 `artifacts/`，Git 只允许同步审核报告。

验证命令：

```bash
python3 -m vibe_finance task-contracts
python3 -m vibe_finance task-contracts --task-id activity-monitor
python3 -m unittest tests.test_task_contracts -v
```

`PASS` 的含义仅为：契约结构有效，且与当前同步白名单一致。它不代表行情有效、任务已经执行、订单已经成交、策略有收益或安全隔离已经得到证明。

### 下一阶段建议

优先级从高到低：

1. 为新生成的研究/决策报告建立独立 `evidence_registry`，为证据分配稳定 ID，并要求每条结论引用 ID；历史工件保持不变。
2. 为报告类型补充 JSON Schema 和占位符拒绝规则，但只把结构通过称为 structural pass。
3. 给未来 Agent 工具结果增加 `call_id`、`latency_ms`、`truncated`、`artifacts` 和明确的 `resource_changes`。
4. 统一自动化 `run_id + seq` 事件日志，方便恢复与审计，不将其混入金融账本。
5. 在 Agent 层加入重复失败/无进展检测；硬金融门禁仍由确定性代码执行。

### 明确不吸收

- 真实券商连接或任何实盘执行能力；
- 通用代码执行 sandbox；
- 未经隔离、脱敏和审核，直接把 session 写入可激活 Skill；
- 可绕过硬门禁的 `--force`；
- 用上下文摘要替代原始证据；
- 把多代理辩论、仪表盘或 demo 输出当作金融绩效证据；
- 在当前数据条件下引入盘中高频或持续自治运行。

## 五、来源、许可与不确定性

- DojoAgents 仓库标注 Apache-2.0；本轮没有复制其源码，只采用通用工程模式。
- PyPI 0.1.8 页面显示 Python 要求为 `>=3.11`；Vibe Finance 当前保持 Python `>=3.10` 且零依赖，因此没有安装该包。
- PyPI 页面未标示 Trusted Publishing；这不是恶意证据，但进一步支持“不直接引入依赖、只借鉴模式”的决定。
- 未发现 DojoAgents 自身的同行评审论文、公开投资绩效基准或独立安全审计。后续若出现论文、审计报告或稳定版本，应重新评估上述结论。
