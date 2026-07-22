# 动态策略实验完整性审计（2026-07-21）

状态：`PARTIALLY_COMPLETED`

证据边界：本报告只确认代码、确定性夹具和 `TEST_SYNTHETIC_MECHANICAL_ONLY` 回放的机械性质。它不确认任何真实市场收益、策略排序、最大回撤或晋升资格。

## 三个竞争性假设

1. `H1 / B3-MG-ERC`：先用 B1 的双窗口绝对动量筛选合格资产，再在合格子集上运行 B2 的 shrinkage ERC；这可能在共同成本下提高长期净终值。当前仅实现和验证机械规则，收益假设为 `UNKNOWN`。
2. `H2 / B4-Net-Wealth-Expert-Mixture`：用截至信号时点的 B1/B2 影子组合净财富动态混合两者；可能适应策略有效性变化，但状态、成本和防前视复杂度更高。当前为设计候选，未实现。
3. `H3 / B5-Agreement-Min-Committee`：逐资产取 B1/B2 目标权重的较小值并保留剩余现金；可作为结构性保守负对照。当前为设计候选，未实现。

B3 优先占用下一候选槽位的依据是零新增数值超参数、复用现有 B1/B2 组件并有强 golden invariant；这不是“B3 更赚钱”的证据。

## Claim → Evidence coverage

| Claim | 当前证据 | 判定 |
|---|---|---|
| 冻结 B0 的来源、策略和纯决策适配器可重放 | 历史源哈希核验、fixture 烟测及 Save→Reload 测试 | `PASS_MECHANICAL_ONLY` |
| B1/B2 数学与共同 caps 按 manifest 实现 | 单元测试覆盖窗口、shrinkage ERC、caps、漂移 fail-closed | `PASS_MECHANICAL_ONLY` |
| B3 精确复用 B1 资格与 B2 子集 ERC，且无新增数值超参 | 预注册提案；全合格等于 B2、全不合格、单资产和子集 ERC 测试 | `PASS_MECHANICAL_ONLY` |
| B0/B1/B2/B3 能跨两个合成周期完成 signal→plan→next-open execution→state→Save→Reload | 10/25/50 bps 确定性闭环测试；输入双哈希；未来 open 隔离；T+1、成本、持仓成本和 realized PnL 传递 | `PASS_TEST_SYNTHETIC_MECHANICAL_ONLY` |
| 机械工件可以形成真实收益指标或策略排名 | runner 强制 `metrics/ranking/strategy_ranking=None`，`promotion_authorized=false` | `REFUTED_BY_DESIGN` |
| B3 在 10 bps 和 25 bps 下长期净终值严格高于 B1/B2 | 无许可合格纵向点时面板，无 walk-forward 结果 | `UNKNOWN` |
| 当前可以进行非封存 walk-forward 与一次性 sealed OOS | 权威日历、可信 loader、公司行动链、总收益构造和完整指数序列未取得 | `NOT_EVALUABLE` |
| 当前可以按风险约束选择候选 | 最大回撤预算尚未冻结 | `UNKNOWN` |

## 已闭合的机械链路

- Entrypoint：`run_synthetic_mechanical_evaluation(input, output_path=...)`。
- Run：B0/B1/B2/B3 共用点时信号、下一交易日开盘执行和 10/25/50 bps 成本场景。
- Save：工件原子写入、不可覆盖、自哈希。
- Reload/Eval：重新读取输入字节/规范哈希、重跑并比较工件哈希；这里只评估闭环一致性，不计算收益。
- 反事实隔离：改变未来 open 会改变 execution，但不得改变同周期 signal/plan。
- 状态：现金、数量、T+1 可卖数量、平均成本、最后买入日和累计 realized PnL 连续传递。

## 缺失实验与协议缺口

| 优先级 | 缺口 | 为什么阻断 | 接受标准 |
|---|---|---|---|
| P0 | 许可覆盖的 2016+ 个共同交易日点时面板 | 没有真实 walk-forward 输入 | 原始 raw open/close、逐事件时间、原文哈希、许可快照、公司行动和可重算 total return 全部通过受保护 loader |
| P0 | 权威交易日历与完整修订链 | 不能证明 t→下一交易日，也不能用工作日推断 | 每日 session 状态、开闭市时刻、authority、observed/published 时间、revision 和 raw hash 完整 |
| P0 | 最大回撤预算预注册 | 即使有内部排序也不能形成风险合格选择 | 在读取候选绩效前由用户冻结数值和违反后的 fail-closed 行为 |
| P0 | 可信 loader 与来源独立性门禁 | 两个交付渠道不等于两个独立真相源 | verifier 从 authority/upstream truth group/delivery channel/许可/内容哈希导出 readiness，不能手工翻转布尔量 |
| P1 | 非封存 walk-forward 反驳实验 | B3 长期净终值主张仍为 UNKNOWN | 冻结代码、数据和规则后，按 manifest 运行 4 个非重叠 test 段；保存逐 fold 净终值、成本、回撤和协议失败码 |
| P1 | 10/25 bps B3 主判据 | 缺少相对 B1/B2 的可判定证据 | `G_B3` 在 10bps 与 25bps 均严格高于 B1/B2；任一不满足即反驳主假设，不改口径 |
| P1 | B3 无前视、排列、缺窗和保存重放测试 | 当前 golden tests 尚未覆盖全部不变量 | 未来数据扰动不改变当前信号；资产排列等价；缺窗/manifest 漂移 fail closed；Save→Reload 完全一致 |
| P2 | B4 状态机与 B5 负对照 | 不能判断 B3 增量来自动态调配还是仅现金暴露 | 先机械闭环，再与同一冻结面板、成本和 folds 比较；不得为 B4 添加事后超参 |
| P2 | 一次性 sealed OOS 和回滚重放 | 内部 test 不能充当独立验证 | 仅对预选单一候选开启一次；失败后不读取同一尾部选择 runner-up；回滚工件可重放 |

## 风险排序与下一步

1. `NEED_USER_AUTHORITY`：官方上交所历史日 K 产品当前公开价格为约人民币 10,000 元/年，远高于项目剩余 100 元研究预算，并且申请、审批和签约会产生外部义务。本轮不得采购、注册或下载。
2. 在不新增外部权限的范围内，继续补 B3 的未来扰动、排列、缺窗与 artifact reload 测试，并把可信 loader schema/gates 固化为仅设计、不伪造数据。
3. 只有 P0 数据、日历、许可、loader 和回撤预算全部冻结后，才能启动非封存 walk-forward；在此之前所有候选维持 `PROPOSED_ONLY`。
4. 任何机械测试通过都不得改写为“收益验证”“策略更优”或“可晋升”。

## 当前决策

- 保留 B0 为冻结回滚基线。
- B1/B2/B3 仅处于机械可执行、不可评价收益的候选状态。
- 不进行策略排序，不修改真实/虚拟生产持仓，不连接券商，不触发 Git 同步。
- 下一轮的真实收益探索取决于新的数据/许可/回撤预算授权；在此之前继续做可回滚、零成本的机械与治理闭环。
