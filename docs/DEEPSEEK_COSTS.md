# DeepSeek 成本与使用审计

截点：2026-07-19。价格会变化，实际调用前必须重新核验[官方定价页](https://api-docs.deepseek.com/quick_start/pricing?article_id=article_1779470751466_8)。

## 当前官方标价

单位为美元/百万 token：

| 模型 | 缓存命中输入 | 缓存未命中输入 | 输出 |
|---|---:|---:|---:|
| DeepSeek V4 Flash | 0.0028 | 0.14 | 0.28 |
| DeepSeek V4 Pro | 0.003625 | 0.435 | 0.87 |

官方页面同时提示旧版 deepseek-chat/deepseek-reasoner 将于 2026-07-24 15:59 UTC 停用。成本计算为各类 token 数除以一百万后乘对应单价；人民币记账使用真实账单的结算金额或调用时可追溯汇率，不预先虚构汇率。

## 本项目实际使用

- 用户声明账户配置或余额：100 元，尚未通过 API 核验。
- Vibe Finance 实际调用：0 次。
- 输入 token：0。
- 输出 token：0。
- 已记账成本：0 元。
- 研究预算剩余：100 元。
- 可投资现金：29,900 元。
- 密钥未写入仓库、报告、日志或自动化。

[余额查询 API](https://api-docs.deepseek.com/zh-cn/api/get-user-balance)需要携带密钥。本轮没有把聊天中的密钥传入工具或命令，避免其出现在调用记录中。DeepSeek 控制台 Usage 页面可按月导出用量；官方 FAQ 说明导出的 amount 数据可用于按 API key 查看成本。未来如需核验，先在本机安全环境变量中配置轮换后的密钥，再执行不回显密钥的余额与用量检查。

## 强制记账

每次真实调用后必须运行：

~~~bash
python3 -m vibe_finance record-api-cost \
  --amount-cny 实际人民币成本 \
  --model 实际模型 \
  --purpose 可审计用途 \
  --input-tokens 实际输入数 \
  --output-tokens 实际输出数
~~~

命令会追加 data/ledger/api_costs.jsonl、增加实际调用次数并扣减 100 元研究预算。超过预算的调用会被拒绝。API 日志不保存 prompt 原文或密钥。

## 安全建议

聊天中出现过密钥，因此应在 DeepSeek 控制台轮换。新密钥只放在本机密钥管理或环境变量中，不放入 .env 提交、源码、报告、自动化 Prompt 或截图。
