# 美股日报格式示例 · 2026-01-02

> **SIMULATION ONLY** — 以下结构、标的和结论均为合成示例；不含真实账户快照，系统不自动交易。

## 1. 今日结论

**继续持有；DEMO_MU/DEMO_SMH 的中期 HBM 逻辑仍在，但油价与利率风险不支持追涨。今天无强制减仓，新增仓位仅进入人工复核。**

| 标的 | 今日动作 | 解释 | 反证/触发条件 |
|---|---|---|---|
| DEMO_MU | HOLD / ADD_REVIEW | HBM 需求与 memory relative strength 为正，但仓位和费用需复核 | 存储价格转弱、出口限制升级或仓位上限触发即取消加仓 |
| DEMO_SMH | HOLD | 半导体行业趋势仍正，未形成两组证据支持加码 | SMH 相对 SPY 转负且 5/20 日因子失效 |
| DEMO_QQQM | HOLD | 大盘趋势尚可，但利率压力压制高久期资产 | 实际利率继续上升则收紧风险预算 |
| DEMO_VOO | HOLD | 作为核心宽基维持 | 市场广度和信用同时恶化时复核 |
| DEMO_SCHD | HOLD | 防御和现金流角色有效 | 组合防御暴露超上限时再平衡 |

## 2. 仓位展示示例

| 标的 | 市值占位符 | 组合权重占位符 | 成本占位符 | 盈亏占位符 | 动作 |
|---|---|---|---|---|---|
| DEMO_MU | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | HOLD / ADD_REVIEW |
| DEMO_SMH | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | HOLD |
| DEMO_QQQM | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | HOLD |
| DEMO_VOO | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | HOLD |
| DEMO_SCHD | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | HOLD |

## 3. 费用与损耗

| 项目 | 本期金额/比率 | 处理 |
|---|---|---|
| 实际交易佣金 | SYNTHETIC | 从券商事实读取 |
| 股息预扣税 | SYNTHETIC | 单列，不与投资收益混合 |
| 利息与其他费用 | SYNTHETIC | 单列 |
| 回测换手成本 | SYNTHETIC | 从 OOS 净收益中扣除 |
| 滑点 | UNKNOWN | 不假设为 0 |
| ETF 费率与最终税务 | UNKNOWN | 需产品/税务数据后核验 |

## 4. 今日核心论点

- **论点：HBM/存储需求仍支持 DEMO_MU 与 DEMO_SMH。** 反证是存储供给过剩、价格下行或出口限制显著升级。
- **论点：石油供应风险提高通胀与风险溢价。** 反证是停火、运输恢复和油价回落。
- **论点：长久期科技不适合在利率压力上升时追涨。** 反证是实际收益率下降并得到市场广度确认。

## 5. 因子有效性

- 采用 1/5/20 日 purged walk-forward；训练和测试之间按 horizon 清洗并设置 embargo。
- 标准化仅使用训练期；OOS 按 horizon 去重叠。
- 已扣换手与交易成本，并对同一因子库做 Benjamini–Hochberg 多重检验控制。
- 每日追加新样本；因子定义只在月度评审或数据定义变化时修改。

| 因子 | 多周期状态 | 中位方向 IC | 最优 q 值 | 稳健度 | 有效权重 |
|---|---|---|---|---|---|
| memory_relative_21 | active | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |
| market_momentum_63 | active | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |
| semis_relative_21 | watch | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |
| rates_relative_21 | quarantined | SYNTHETIC | SYNTHETIC | SYNTHETIC | 0 |

| Horizon | 状态 | OOS 样本 | 净年化 | Sharpe | PSR | 最大回撤 | 成本拖累 |
|---|---|---|---|---|---|---|---|
| 1 | active | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |
| 5 | active | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |
| 20 | research_only | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC | SYNTHETIC |

## 6. 事件与跨资产传导

| 来源 | 论点 | 主要影响 |
|---|---|---|
| 国际媒体 | 原油运输风险上升 | XLE/USO 正向；QQQM/MU 风险预算下调 |
| SK hynix 官方来源 | HBM 产能与合作继续扩张 | MU/SMH 中期需求背景正向 |
| 韩国媒体 | 当地半导体投资和政策支持 | 背景正向，但不单独触发交易 |
| Reddit | 社区情绪偏多 | 只用于拥挤与分歧，不作独立证据 |
| Quora/search | 出现估值争论 | 零直接权重，仅作线索 |

<details><summary>数据源、测试与运行状态</summary>

- 公共框架测试：PASS
- 私有部署测试：PASS
- IBKR 状态：not_connected_in_public_example
- 自动执行：false
- 缺失数据：保持 UNKNOWN

</details>
