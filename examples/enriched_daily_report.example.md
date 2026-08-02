# 美股增强研究日报 · 2026-01-02

> **SIMULATION ONLY** — 全部标的、分数与事件均为合成示例；不含真实账户数据，不连接券商。

## 今日结论

**今日没有需要调整的持仓：继续持有。**

- 全球研究状态：`completed`
- IBKR/Flex 状态：`not_connected_in_public_example`
- 主动新增风险：`RESEARCH_GATE_REQUIRED`
- 自动交易：`false`

## 全球市场与主观叙事因子

- 状态：`healthy`；独立加权来源组：4。
- 全球叙事风险预算乘数：97.0%。
- Quora/搜索摘要：`context_only / direct_weight=0`。
- Reddit/社区：`one_correlated_group / independent_trade=false`。

| 主题 | 合成状态 | 主要传导 |
|---|---:|---|
| oil_supply | +0.34 | XLE/USO 正向；VOO/QQQM/MU 负向 |
| memory_hbm_demand | +0.41 | MU/SMH 正向 |
| semiconductor_export_controls | -0.08 | 当前未形成独立确认 |
| rates_inflation | +0.12 | TLT/长久期科技承压 |

| 来源角色 | 状态 | 用途 |
|---|---|---|
| 发行人 primary newsroom | healthy | HBM/产能/合作事实 |
| 国际媒体 | healthy | 石油与地缘事件独立解释 |
| 韩国地区媒体 | partial | 当地半导体政策与供应链 |
| Reddit | healthy | 拥挤与分歧，不作一级证据 |
| Quora/search | context_only | 发现线索，零直接权重 |

## 滚动回归与因子有效性

- 模型：`walk_forward_ridge:synthetic-example`
- 训练—测试：严格先后；OOS 记录按 5 日 horizon 去重叠。
- OOS 净年化：+4.2%；年化波动：8.8%；Sharpe：0.48。
- 换手与 5 bps 成本已扣除。

| 因子 | 方向校正 OOS IC | 系数一致性 | 状态 | 有效权重 |
|---|---:|---:|---|---:|
| memory_relative_21 | +0.08 | 70% | active | 39% |
| market_momentum_63 | +0.04 | 62% | active | 19% |
| oil_relative_21 | +0.01 | 54% | watch | 3% |
| rates_relative_21 | -0.03 | 48% | quarantined | 0% |

## 定投与仓位边界

| 标的 | 配置计划 | 模型状态 | 券商确认 |
|---|---:|---|---|
| DEMO_MU | $20 | KEEP_BASE | UNKNOWN |
| DEMO_QQQM | $20 | KEEP_BASE | UNKNOWN |
| DEMO_SMH | $20 | KEEP_BASE | UNKNOWN |
| DEMO_VOO | $20 | KEEP_BASE | UNKNOWN |
| DEMO_SCHD | $20 | KEEP_BASE | UNKNOWN |

- 配置计划、模型建议与券商成交必须分开。
- 全球媒体、KOL、社区、回归或优化器均不能下单。
- 缺失的账户、价格、税务、IPS 或成本信息保持 `UNKNOWN`。
