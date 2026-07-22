# B0/B1/B2 点时评估数据获取路线（仅调研，未采购）

- 日期：2026-07-21
- 状态：`NEED_USER_AUTHORITY`
- 成本：0 元；本轮没有订阅、注册、抓取或调用付费接口。
- 用途边界：仅为纯虚拟策略回溯、走前验证和模拟成交准备数据；不连接券商，不产生真实订单。

## 已确认的官方路线

1. 上证所信息网络有限公司的“上证智能数据”官方页面明确说明：服务包含实时、历史和参考数据；历史数据可用于历史行情回溯、策略模型验证和交易模拟测试。产品说明书列出 Level-1 日 K 数据的 `OpenPx`、`LastPx`、成交量、成交额与 `TradingDay` 等字段，覆盖本项目沪市 ETF 的 raw open/close 和交易状态需求。
   - https://www.sseinfo.com/services/sseai/aidata/
   - https://bsp.sseinfo.com/admin/static/public/2024-06-07/0017f8fa1c1a47cb997d433c6d05fc11/%E4%B8%8A%E6%B5%B7%E8%AF%81%E5%88%B8%E4%BA%A4%E6%98%93%E6%89%80%E5%8E%86%E5%8F%B2%E6%95%B0%E6%8D%AE%E6%8E%A5%E5%8F%A3%E8%AF%B4%E6%98%8E%E4%B9%A6.pdf

   官方产品价格页当前列示：单购日 K 线为 10,000 元/年，并需要申请、审核和签约。该费用是现有 100 元研究基础设施预算的 100 倍，因此不得在没有用户新增预算与许可授权时采购。
   - https://www.sseinfo.com/services/cpfwjg/
   - https://bsp.sseinfo.com/business/?id=1703682586115227650

2. 深交所投资者页面提供基金历史行情入口；深交所法律声明允许在遵守法律与声明前提下为非商业目的浏览、下载网站内容，但禁止未经书面许可的牟利性传播。该声明尚不足以证明自动化批量抓取、长期本地存储、派生数据共享或本项目具体评估方式均获许可，因此当前仍标记为 `LICENSE_SCOPE_UNKNOWN`。
   - https://investor.szse.cn/fund/marketdata/trade/index.html
   - https://www.szse.cn/application/laws/

## 采购或授权前必须书面确认

- 是否允许本地研究评估、长期存储、重复回放和生成不对外分发的派生指标。
- 能否提供至少 2016 个共同交易日的 raw open/close、交易状态、交易日历、修订/更正记录和数据可用时间。
- ETF 分红、拆分、份额折算等公司行动的公告时间、观察时间和生效日能否点时重放。
- 历史文件和每日增量是否有不可变内容哈希、版本号或可追溯下载凭证。
- 许可是否覆盖本项目的四个候选 ETF：510300、510880、512100、518880。
- 价格复核的两个交付通道如何定义独立性；同一交易所上游经两个转售商转发，不应被误称为两个独立价格真相来源。
- 费用、调用限制、停止方式和数据删除义务。

## 当前决定

在上述许可和成本得到明确授权前：

- 不采购、不抓取、不回填历史数据；
- `config/evaluation/trading-calendar.json` 保持 `NOT_ACQUIRED`；
- production readiness 保持 `NOT_EVALUABLE`；
- 只允许标记为 `TEST_ONLY` 的合成数据验证数学和会计机械；
- 不生成 B0/B1/B2 收益、排名或晋级结论。

## 2026-07-21 authority 审计补充

- 四个共同候选 `510300/510880/512100/518880` 均为沪市场内 ETF；深交所历史页面不能作为它们的权威行情补集。
- 上交所是成交价的唯一一级 truth authority。腾讯、新浪、东方财富等两个交付通道即使数值一致，也不能被称为两个独立价格真相来源；当前 `required_price_sources=2` 必须进一步区分 `authority/upstream_truth_group` 与 `delivery_channel`。
- 免费公开页面不足以提供 2016 个共同交易日、可重放修订链、完整公司行动及明确的长期本地存储许可；今天没有 evaluation-ready 数据可以 `ACQUIRE_NOW`。
- 可立即且零成本获取的仅是官方产品字段、申请流程、报价和条款说明；这些只能用于 schema 与采购决策，不能充当历史行情数据许可。

Trusted loader 在接入任何真实数据前还必须增加：逐源原始值和 raw blob 哈希、`event_at/published_at/observed_at/retrieved_at`、authority 与 delivery channel 双层独立性、许可的 storage/replay/derived-metrics 范围、公司行动修订链、total-return 可重算输入集合哈希，以及 sealed-OOS inventory。`origin=canonical` 和 `snapshot_sha256` 均不得由输入文件自证。
