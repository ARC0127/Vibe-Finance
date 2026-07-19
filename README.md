# Vibe Finance

Vibe Finance 是一个只研究中国大陆可获得信息、只执行虚拟交易的股票与基金决策实验。项目从 30,000 元虚拟本金开始，保存点时证据、决策、待成交订单、成交、费用与复盘；它不连接券商，也不承诺收益。

## 当前状态

截至 2026-07-19（周日），最新市场截点是 2026-07-17 收盘。上证、深证成指、创业板和沪深 300 同日显著下跌，且候选 ETF 尚无 20 个不可变历史快照，因此短、长周期均为 NO_TRADE。组合为：

- 可投资现金：29,900 元
- DeepSeek 研究预算：100 元
- 股票/基金持仓：0 元
- 项目权益：30,000 元
- DeepSeek 实际调用：0 次，成本 0 元

现金不是遗漏的投资决定；在数据或风险门禁失败时，零仓位是系统允许的主动决策。

## 一条命令

使用 Python 3.10+，无第三方运行依赖：

~~~bash
python3 -m vibe_finance run --input data/inbox/YYYY-MM-DD.json --mode short
~~~

常用检查：

~~~bash
python3 -m vibe_finance validate --input data/inbox/2026-07-19.json
python3 -m vibe_finance status
python3 -m unittest discover -s tests -v
~~~

运行顺序固定为：校验点时输入 → 结算以前生成的下一开盘虚拟订单 → 计算风险/趋势门禁 → 生成新订单 → 原子更新账本和心跳 → 写入不可覆盖的 Markdown/JSON 报告。

## 数据与成交纪律

- 每个数值必须带 as_of、来源标识和质量说明。
- 价格至少由 2 个来源交叉核验；策略输入至少积累 20 个历史点。
- 收盘后信号只能在更晚的交易日开盘模拟成交，禁止同一收盘价回填成交。
- 休市、快照陈旧、市场冲击、价格冲突或历史不足都会 fail closed。
- 旧日报和决策 JSON 不可由常规运行覆盖；修正必须生成新记录。
- 专家观点只提供情景与反证，不直接触发 BUY/SELL。
- 目前没有稳定、许可明确的自动行情 API，因此每日任务负责用权威网页生成点时输入；无法核实时维持 NO_TRADE。

## 调度

- 短周期：交易日收盘后运行，生成下一可交易时点的虚拟决策。
- 长周期：每周运行，做组合归因、来源可靠性复核和规则版本审查。
- 活动监测：每 6 小时只读检查心跳和报告新鲜度。
- 高频周期：当前不创建、不启用。只有许可明确的点时数据、成交/滑点模型、走前回测、样本外通过和再次人工确认全部满足后才允许创建。

## DeepSeek 成本与密钥

用户声明 DeepSeek 余额为 100 元，本项目将其从可投资现金中隔离。未通过 API 核验余额，也未调用 API。每次真实调用都必须用 record-api-cost 记录模型、用途、输入/输出 token 和人民币成本，且累计不得超过 100 元。

密钥不得进入仓库、报告、日志或自动化提示。由于密钥曾出现在对话中，建议在 DeepSeek 控制台轮换后，仅通过本机环境变量注入。

## 仓库地图

- [主研究 Prompt](MASTER_PROMPT.md)
- [模式锁](MODE_LOCK.md)
- [来源与证据规则](docs/SOURCES.md)
- [参考项目与架构取舍](docs/REFERENCE_PROJECTS.md)
- [DeepSeek 成本与使用审计](docs/DEEPSEEK_COSTS.md)
- [定时设计](docs/AUTOMATION.md)
- [策略配置](config/strategy.json)
- [来源注册表](config/sources.json)
- [今日完整分析](reports/research/2026-07-19-analysis.md)
- [虚拟账本](data/ledger/portfolio.json)
- [流水线](vibe_finance/pipeline.py)
- [测试](tests/test_pipeline.py)

## 风险边界

这是个人研究与代理能力训练项目，不是真实投资建议。模拟结果不能证明实盘收益能力；税费、流动性、涨跌停、停牌、基金申赎、净值发布时间、数据许可和行为偏差仍可能令实盘结果显著更差。
