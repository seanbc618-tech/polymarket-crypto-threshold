# 项目进展说明：这几天到底在干什么

**更新时间：** 2026-07-28
**项目：** `polymarket-crypto-threshold`
**当前性质：** Research prototype
**实盘状态：** **NO-GO**

> **2026-07-29 更新：** 本文记录的 30-label 等待已经结束，后续 frozen
> training、五个合法 OOS labels、combined replay、fixed-holdout calibration
> 和机械验收也已完成。Phase 2 只读研究证据闭环已通过，但 Polymarket
> midpoint 在全部三项 OOS 指标上优于 raw/calibrated 模型，因此没有模型
> edge 或实盘依据。最终证据见
> `docs/PHASE2-FINAL-ACCEPTANCE-2026-07-29.md`。

## 先说结论

今天真正结束的是：

> Phase 2 的“至少 30 个合格训练标签”数据等待。

2026-07-28 的 VPS 严格只读检查结果是：

```text
Replay plan READY
family=daily_threshold
eligible_items=3922
eligible_unique_labels=34/30
training_cutoff=2026-07-27T16:13:16.984047+00:00
training_cutoff_label=label:88dce179-e282-434a-9323-cdcbb08f8b30
```

北京时间的训练截止点是 **2026-07-28 00:13:16**。

上一次检查只有 `2572` 个可重放条目和 `25/30` 个合格唯一标签。等待期间，数据增长为：

- 可重放条目：`2572 -> 3922`，增加 `1350`
- 合格唯一标签：`25 -> 34`，增加 `9`
- 训练数据门槛：从 `PENDING` 变成 **`READY`**

本次检查使用只读入口。复核结果为：

```text
query_only=1
replay_datasets=0
calibration_runs=0
```

也就是说，我们只是确认数据已经够了，尚未在正在采集的数据库里创建 replay 或 calibration。

但整个 Phase 2 还不能在这一刻写成“最终验收通过”。剩下的是冻结训练集、建立独立 OOS、计算指标并运行最终机械验收。这些是接下来的离线工作，不是再重跑 72 小时。

## 为什么等了这么久

这几天不是在等一个进程单纯熬时间，而是在等真实市场自然产生不可伪造的时序证据。

### 1. 72 小时和 30 个标签验证的是两件不同的事

72 小时连续 Shadow Monitoring 验证的是系统工程：

- 服务能否持续运行
- 数据是否持续新鲜
- WebSocket 断线后 REST 是否独立可用
- 周期是否连续
- 数据库、WAL、备份和 schema 是否稳定
- 是否始终没有订单、成交、仓位或签名突变

30 个唯一结算标签验证的是研究数据：

- 预测发生在结算之前
- 预测使用的原始输入完整存在
- 市场后来真实结算
- 预测和标签能通过精确 ID 与时间顺序对应
- 同一个市场的一千次快照不能冒充一千个独立样本

所以，跑完 72 小时并不自动得到 30 个可训练标签；有 30 条原始 settlement 记录，也不等于有 30 个可重放训练标签。

### 2. 为什么之前有 31 个 raw labels 仍然不能开始

2026-07-27 的审计发现：

- 原始 Daily settlement labels：`31`
- 真正 replay-eligible 的唯一标签：`25`
- 不能使用的标签：`6`

那 6 个标签没有匹配到结算前的合格 `analyzed` 决策和完整 replay 输入。硬把它们算进去会制造训练证据，也会破坏 no-lookahead，因此系统按设计拒绝了它们。

### 3. 为什么不能拿 3922 个 snapshots 直接训练

`eligible_items=3922` 表示有 3922 个可以审计的决策快照，不代表有 3922 次独立实验。很多快照来自同一个最终只产生一个结果的市场。

训练门槛按 `eligible_unique_labels` 计算，目的就是防止把重复观察当成独立样本，制造虚假的样本量和指标置信度。

## 这些天具体完成了什么

### 2026-07-23：Phase 2 本地闭环和五小时 Smoke

- 完成真实 Gamma discovery、CLOB 盘口、Binance 结算源和 Coinbase sanity check 的只读分析闭环。
- 完成原始 payload 先落库、精确 signal input 链接、不可变 settlement label 和 replay manifest。
- 完成 Polymarket WebSocket 可选加速层与 Binance Reference Price Stream。
- 本地代理环境下跑完约五小时 Shadow Smoke。
- 记录 86 个连续周期、8588 个通过 schema monitor 的外部 payload，以及 150 个 Binance closed 1m ticks。
- 建立本地 replay，但它只有 5 个唯一结算标签，因此没有假装存在有效 OOS。
- 完成只读 Dashboard 和安全钱包配置边界；私钥仍与运行时交易路径断开。

### 2026-07-24 至 2026-07-27：VPS 连续 Shadow 验证

- 将只读系统部署到香港 VPS，使用直连网络，不需要本地代理。
- 修复 VPS 主机时间不同步问题，并让服务等待 NTP 同步后启动。
- 从干净 evidence DB 开始正式连续运行。
- Daily bounded service 自然运行完成，没有人为延长或中途重启。
- 最终得到 `72.956423` 个进程可归属小时和 `1353` 个周期。
- 最大周期起点间隔约 `215.808` 秒，最大周期耗时约 `35.797` 秒。
- REST fallback、schema drift、paper ledger、Binance closed tick 和 reconnect 证据通过。
- 最终数据库通过 integrity check、外键检查和禁止交易表检查。
- 制作 WAL 一致、逐表游标一致的最终备份；原始 evidence DB 保持不变。

这段 72 小时不是为了证明模型赚钱，而是为了证明系统能长期、只读、可恢复地采集真实证据。

### 2026-07-25：增加 5m/15m Up/Down 研究线

根据项目需求，增加了独立的短周期研究线：

- 资产：BTC、ETH、SOL、XRP、DOGE、BNB、HYPE
- 周期：5 分钟和 15 分钟
- 结算源：Polymarket/Chainlink crypto window
- 独立配置、独立数据库、独立 systemd service

这条研究线没有与 Daily BTC/ETH threshold 数据混合，也没有拿来填 Daily Phase 2 的 30 个标签。

### 2026-07-26：修复 settlement 调度和重复数据增长

长时间运行暴露出两个真实运行问题：

- pending settlement 可能被新任务挤压，形成 retry starvation
- 相同的未完成 resolution payload 可能重复增长

完成的修复包括：

- retry 与 never-attempted candidate 公平轮换
- `5m / 15m / 1h / 6h` pending backoff
- settlement semantic fingerprint 去重
- 保留旧历史数据用于审计，不偷偷清理或重写
- 增加长期调度、backoff 和公平性测试

### 2026-07-27：修复 Up/Down 权威边界

运行证据发现 9 个历史市场、83 条 signal rows 使用了窗口边界后一秒左右的 RTDS tick，和最终 `priceToBeat` 不完全一致。

我们没有放宽容差把错误数据“洗白”，而是：

- 将 Polymarket public crypto-window 的 immutable `openPrice` 设为模型权威边界
- RTDS 只保留为当前价格和波动率来源
- settlement 要求 window endpoint、Gamma 和最终 outcome 共同一致
- 历史 83 条 mismatch rows 永久排除在 short replay 之外
- 新实现继续要求精确边界，不允许用 ppm 或时间容差蒙混过关

### 2026-07-27：建立 Forward Collector 和冻结训练规则

72 小时 Daily evidence 已经证明运行连续性，但标签数不足，所以没有重启已完成的 bounded service，而是：

- 从最终 WAL 一致备份建立独立 forward working copy
- 将 working copy 迁移到 schema v5
- 启动 bounded forward label collector
- 保留原始 72 小时 evidence 和最终备份不变
- 增加只读 `replay-plan`
- 增加严格的前 30 个合格标签选择和确定性 cutoff
- 增加 `replay-manifest-v3`
- 增加 `fixed-holdout-calibration-v3`
- 禁止在 OOS 阶段重新拟合训练 histogram
- 将 training dataset hash、标签列表、cutoff、item identity 和时间戳绑定到 combined replay

最新完整本地质量门是：

```text
242 passed
Ruff clean
mypy clean over 53 source files
git diff --check clean
```

这些实现以 `6002bc4` 部署，部署没有重启 Forward 或 Up/Down 服务。

### 2026-07-28：训练数据门槛终于通过

今天的只读 `replay-plan` 首次确认：

```text
eligible_unique_labels=34/30
status=READY
```

这意味着“继续等训练标签”的阶段正式结束。

## 现在项目到底到什么程度

| 项目 | 当前状态 | 说明 |
|---|---|---|
| Phase 0 真相修复 | 完成 | 过期、时间、概率区间、doctor、交易模式均 fail-closed |
| Phase 1 真实只读分析闭环 | 完成 | Gamma -> CLOB -> provider -> ask VWAP -> net EV -> signal |
| Phase 2 软件实现 | 完成 | replay、calibration、streams、shadow、paper ledger、acceptance checker |
| 本地五小时 Smoke | 完成 | 代理下验证 Binance stream 与 REST 独立路径 |
| VPS 72 小时连续 Shadow | 完成 | 72.956 小时，服务自然结束，证据和最终备份已冻结 |
| Binance reconnect / REST fallback | 完成 | 真实部署网络证据已记录 |
| 无真实交易突变检查 | 完成 | 没有 signer、BUY/SELL、orders、fills、positions |
| 30 个训练标签门槛 | **完成** | 2026-07-28 达到 `34/30 READY` |
| 冻结 30-label training replay | 待执行 | 必须在 WAL 一致的独立快照上构建 |
| 独立 OOS replay 和 calibration | 待执行 | 不允许将后来的标签重新加入训练 |
| Phase 2 最终机械验收和报告 | 待执行 | 需要真实 OOS 指标后才能给结论 |
| Phase 3 | 未开始 | 当前阶段仍禁止真实交易 |

一句话概括：

> 系统工程和训练数据收集已经完成；Phase 2 现在进入最后的离线冻结、OOS 评估和验收阶段。

## 接下来还剩什么

下一步按这个顺序执行：

1. 对 forward DB 创建 WAL 一致的独立快照。
2. 在快照上用最早的 30 个合格唯一标签构建 frozen training replay。
3. 对 replay manifest、hash、输入链接、标签列表和 cutoff 做 100% verify。
4. 检查训练截止点之后是否已经存在合法的 OOS decision/label。
5. 如果 OOS 已存在，构建绑定 frozen training dataset 的 combined replay。
6. 运行 fixed-holdout calibration，训练 histogram 在整个 OOS 期间保持不变。
7. 发布 raw、calibrated、Polymarket baseline 的 Brier、log loss 和 ECE。
8. 运行 `phase2-acceptance`，再做一次人工项目审查并形成最终报告。

冻结和验证本身是离线计算，不需要再等 72 小时。唯一可能继续需要自然时间的是：如果当前数据库里还没有训练 cutoff 之后产生并完成结算的合法 OOS 样本，就必须让 forward collector 再收集一批，不能用训练期数据冒充 OOS。

## 现在还不能宣称什么

即使 `34/30 READY`，现在也不能宣称：

- Phase 2 已最终验收
- 模型已经证明优于 Polymarket
- paper PnL 能代表未来利润
- 系统可以接真实资金
- Phase 3 或自动交易已经获准

必须等 OOS 指标出来以后，才能判断模型有没有可测量的预测价值。即使 Phase 2 通过，也只代表只读研究闭环的证据完整，不自动授权实盘。

## 安全边界

截至这份说明：

- 没有执行真实 BUY/SELL
- 没有签名或发送订单
- 没有 authenticated reconciliation
- 没有读取或要求真实私钥
- 没有订单、成交或仓位真相表
- WebSocket 只提供行情和重分析 hint
- 最终可执行深度仍以 token-specific REST orderbook 为准
- `TRADING_DISABLED` 和 Live `NO-GO` 继续生效

这份文档只保存在本地工作树，不上传 VPS，也不提交 Git。
