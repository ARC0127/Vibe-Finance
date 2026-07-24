# 参考项目与架构取舍

这些项目用于学习工程模式，不表示其策略适合当前 30,000 元中国大陆虚拟组合。

| 项目 | 可吸收能力 | 当前不直接采用的部分 |
|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 数据、模型、回测、组合、执行分层，滚动研究和高频示例 | v0.1 没有合格点时数据和训练样本，不启动重型 ML/HFT |
| [FinRL](https://github.com/AI4Finance-Foundation/FinRL) | 训练—验证—交易分层、研究型强化学习基线、中国数据适配思路 | RL 容易在短样本和不稳定数据上过拟合，暂不用于下单 |
| [RQAlpha](https://github.com/ricequant/rqalpha) | A 股事件驱动、交易日和订单语义 | 当前先保留无第三方依赖的小型可审计账本 |
| [vn.py](https://github.com/vnpy/vnpy) | 事件驱动、模拟账户、行情与交易接口隔离 | 不连接真实网关；高频和实盘组件越界 |
| [LEAN](https://github.com/QuantConnect/Lean) | 数据无关的事件引擎、组合和成交模型 | 对首轮个人实验过重，且中国数据许可仍未解决 |
| [vectorbt](https://github.com/polakowo/vectorbt) | 快速大规模回测、参数扫描和走前研究 | 等不可变历史积累后再用于离线研究，避免先调参后补数据 |
| [AkShare](https://akshare.akfamily.xyz/introduction.html) | 丰富的中国公开数据适配器和研究便利性 | 接口可能随网页变化；须逐接口核验许可、字段和时间点语义 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot) | 多代理金融研究、文本与市场信息编排 | 语言模型结论不能替代价格数据、成交规则和样本外证据 |
| [DojoAgents](https://github.com/Alpha-Dojo/DojoAgents) | Task/Pipeline 契约、结构化工具结果、事件关联、证据包与循环护栏；会话记忆采用隔离候选与每周人工核对变体 | 不引入未充分验证的 sandbox、直接激活的会话记忆、force 绕过、多代理自治或券商能力；详见 [2026-07-24 调研](../reports/research/2026-07-24-dojoagents-benchmark.md) |

## 本项目当前架构

Vibe Finance 选择四个最小但关键的层：

1. 不可变点时输入：每天保留当时可见的数据和 SHA-256；
2. 确定性风控/信号：相同输入与策略版本应产生相同建议；
3. 事件式虚拟成交：收盘信号只能在后续开盘成交；
4. 本地账本与复盘：现金、持仓、费用、API 成本、订单和心跳可审计。

未来引入 Qlib、vectorbt 或 RQAlpha 的前提不是“功能更多”，而是至少已有足够历史、清晰数据许可、公司行为处理和可复现基准。模型复杂度必须晚于数据质量。

## 反模式

- 从展示页面抓到一个价格就自动成交；
- 把大模型情绪当作买卖信号；
- 同一批数据反复调参并宣称样本外；
- 用收盘后新闻按当天收盘价成交；
- 忽略手续费、最小佣金、涨跌停、停牌和基金净值延迟；
- 用监测任务刷新自己的心跳，从而掩盖主流程已经停止。
