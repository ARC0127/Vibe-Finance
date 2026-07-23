# 来源与证据规则

机器可读的完整注册表位于 config/sources.json。本文件说明来源如何影响交易，而不是把网址数量当作研究质量。

## 分级

| 等级 | 典型来源 | 可用于 |
|---|---|---|
| A | 证监会、交易所、法定披露、人民银行、国家统计局、基金业协会、基金公司、指数公司 | 法定事实、公告、交易规则、净值/基金文件、宏观原始数据 |
| B | IMF、世界银行、OECD、BIS、权威研究机构和可追溯证券媒体 | 宏观情景、具名研究观点、新闻交叉验证 |
| C | 东方财富/天天基金、新浪财经、Investing.com | 发现线索、便利行情和第二/第三价格核验，不能单独决定交易 |

价格、净值和公司行为优先使用同口径 A 级来源；至少两个独立来源一致才进入自动门禁。二手文章若引用原始公告，应回到公告本身。

## 核心中国来源

- [中国证监会](https://www.csrc.gov.cn/)：监管规则、行政许可与处罚。
- [上海证券交易所](https://www.sse.com.cn/)、[上证基金网](https://etf.sse.com.cn/)及[基金披露](https://etf.sse.com.cn/disclosure/)：交易日历、公告、ETF 信息。
- [深圳证券交易所](https://www.szse.cn/)与[北京证券交易所](https://www.bse.cn/)：上市证券和规则。
- [中国证券投资基金业协会](https://www.amac.org.cn/sjtj/tjbg/)：公募基金行业统计。
- [中国人民银行](https://www.pbc.gov.cn/)、[国家统计局](https://www.stats.gov.cn/)、[财政部](https://www.mof.gov.cn/)、[国家外汇管理局](https://www.safe.gov.cn/)：宏观与政策原始材料。
- [中证指数](https://www.csindex.com.cn/)、[国证指数](https://www.cnindex.com.cn/)、[中国债券信息网](https://indices.chinabond.com.cn/cbweb-mn/indices/single_index_query?locale=zh_CN)：指数方法、样本与债券指数。
- 基金管理人包括：[华夏基金](https://www.chinaamc.com/)、[易方达基金](https://www.efunds.com.cn/)、[南方基金](https://www.nffund.com/)、[银华基金](https://www.yhfund.com.cn/)、[华泰柏瑞基金](https://www.huatai-pb.com/)、[博时基金](https://www.bosera.com/)、[招商基金](https://www.cmfchina.com/)等；完整清单见 `config/sources.json`。

## 天天基金的专门地位

[天天基金](https://fund.eastmoney.com/)是基金研究的重要 C 级渠道。每只基金优先从其[数据中心](https://fund.eastmoney.com/data/)、[排行](https://fund.eastmoney.com/data/fundranking.html)、[筛选](https://fund.eastmoney.com/data/fundguide.aspx)和单基金页面发现净值、申赎、费率、规模、经理、持仓、分红和公告。

天天基金页面自身说明数据来自东方财富 Choice、需要核实且过往业绩不预示未来。因此：

- 机器门禁要求基金出现 `eastmoney` 交叉验证，同时至少有基金公司或交易所原始来源；
- 排行只用于发现，不能单独触发 BUY；
- 净值更新时间、同类口径和份额类别必须一致；
- 基金持仓按披露日处理，不能当作实时数据；
- 完整规则见 `docs/TIANTIAN_FUND.md`。

## 国际与专家渠道

- [IMF 中国](https://www.imf.org/en/Countries/CHN)、[世界银行中国](https://www.worldbank.org/en/country/china)、[OECD 中国](https://www.oecd.org/china/)、[BIS](https://www.bis.org/)用于跨机构宏观情景。
- [中国金融四十人论坛](https://www.cf40.org.cn/)与[北京大学国家发展研究院](https://nsd.pku.edu.cn/)用于追踪具名研究者的持续观点。
- 例如[黄益平的机构主页](https://nsd.pku.edu.cn/szdw/qzjs/1c3a4a26b717457494a80b8ebc9a46ef.htm)可确认身份和研究领域；其观点仍必须与原始数据及反方证据并列。
- [证券时报](https://www.stcn.com/)、[中国证券报](https://www.cs.com.cn/)、[上海证券报](https://www.cnstock.com/)、[新华财经](https://www.cnfin.com/)用于盘后事实与政策传播交叉验证。

“权威专家”不是权威价格源。观点字段必须保存作者、机构、发布日期、原文、主题、方向、时间尺度、利益冲突提示和反方材料；不能把知名度或情绪热度直接转换成仓位。

## 冲突与失败回退

1. 比较是否为同一证券、时间点、复权方式、币种、净值/市价和收益口径。
2. 同口径冲突时采用更高等级且发布时间更接近截点的来源。
3. 仍无法解释则标记 SOURCE_CONFLICT，阻止该资产新订单。
4. 访问失败不得用模型猜测数值。记录失败，保留上一次不可变快照并因陈旧门禁降级。
5. 不绕过登录、验证码、反爬、付费墙或许可限制。

## 当前采集边界

初始 v0.1 测试公共聚合行情端点时曾遇到 TLS/访问限制。策略 v0.3.1 的 `capture-open` 仅在虚拟实验中自动读取腾讯与新浪公开行情响应，用于逐只交叉核验开盘价、上一收盘价、源内时间戳和非零成交量；响应 URL、访问时间与 SHA-256 写入不可变快照。二者仍为 C 级来源，不能替代交易所/基金管理人对代码身份、停复牌、基金属性和公司行为的一级证据。任一访问失败、时间戳不在09:30–09:35、字段冲突或盘前一级证据未闭环均阻止虚拟成交；不绕过访问限制，也不连接行情付费服务或券商。
