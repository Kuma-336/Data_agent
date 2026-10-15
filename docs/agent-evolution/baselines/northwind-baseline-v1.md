# Northwind Baseline V1

- 状态：Completed
- 执行日期：2026-09-19
- Eval Suite：`Northwind Baseline V1`
- Suite ID：`6ce5d16c-c82d-45a0-b55d-ca9c7fcef0f4`
- Eval Run ID：`24f39896-55c5-4be5-932f-f6a6699624e3`
- 数据源：`Northwind Baseline`
- Connection ID：`980ff43a-e972-47f1-a392-a6f867884e0c`
- 模型：`deepseek-chat`
- Case Fixture：[`evals/northwind_baseline_v1.json`](../../../evals/northwind_baseline_v1.json)

## 1. 结论

本次基线共执行 150 个唯一案例，120 个通过、30 个失败，总通过率为 **80.0%**。

简单过滤达到 100%，聚合达到 93.8%，说明 Data Agent 已能稳定处理明确、低歧义的数据查询。主要薄弱点集中在：

1. PostgreSQL `real` 金额字段的计算顺序导致 0.01 精度差异。
2. 模型没有严格遵守输出常量字段的契约。
3. 对无数据的承运商仍生成了跨承运商统计，说明值域检查不足。
4. 报告工具对不规范 `blocks` 缺乏输入校验，报告案例通过率只有 30%。
5. 报告任务产生大量重试，平均 Token 明显高于普通查询。

这份报告是改造前的对照组。后续不得修改本 Fixture 的参考 SQL 和验收口径来提高分数；如需改变评测规则，应创建 V2 Fixture 并保留 V1 结果。

## 2. 数据集与可重复性

| 项目 | 值 |
| --- | --- |
| 数据集 | Northwind PostgreSQL |
| 来源 | `https://github.com/pthom/northwind_psql` |
| Commit | `cd0ef28d66369fbe177778e604e4be0f153c9e5c` |
| SQL SHA-256 | `0ee30c01ba282f7194f38bf7f99cd6be0470b7ee5f67d0f7ca41fb058d735e0c` |
| Fixture SHA-256 | `61a7e349110fd0ad42ff10b458e1f0d0d0702a859572be3667a2c7dd677554a7` |
| 数据表 | 14 |
| 订单 | 830 |
| 订单明细 | 2,155 |
| 数据库角色 | `northwind_reader` |
| 默认事务 | Read only |

所有 150 条参考 SQL 在运行前均通过 Data Agent SQL Guard，并使用只读账号执行成功。参考查询最多返回 22 行。

可重复运行的固定步骤、数据启动方式、Fixture 自校验、导入 API 和三次稳定性口径见 [`evaluation-plan.md`](../evaluation-plan.md)。Fixture 的类别分布、难度分布、Smoke Case 和哈希由 [`northwind_baseline_v1.lock.json`](../../../evals/northwind_baseline_v1.lock.json) 固化。

## 3. 总体指标

| 指标 | 结果 |
| --- | ---: |
| 总案例 | 150 |
| 通过 | 120 |
| 失败 | 30 |
| 通过率 | 80.0% |
| 输入 Token | 1,825,122 |
| 输出 Token | 101,284 |
| 总 Token | 1,926,406 |
| 平均 Token / Case | 12,843 |
| 模型调用 | 426 |
| 平均模型调用 / Case | 2.84 |
| 工具调用 | 292 |
| 平均工具调用 / Case | 1.95 |

以上 Token 仅统计 150 个唯一正式案例，不包含 8 Case 烟雾运行的 100,994 Token，也不包含重复投递事故的约 228,679 Token。本轮评测相关的可归因总用量约为 2,256,079 Token。

## 4. 按类别统计

| 类别 | 通过 | 总数 | 通过率 | Token | 平均 Token / Case |
| --- | ---: | ---: | ---: | ---: | ---: |
| Simple filter | 32 | 32 | 100.0% | 213,086 | 6,659 |
| Aggregation | 30 | 32 | 93.8% | 259,373 | 8,105 |
| Join and ranking | 24 | 30 | 80.0% | 385,077 | 12,836 |
| Business metric | 15 | 19 | 78.9% | 314,260 | 16,540 |
| Advanced | 4 | 5 | 80.0% | 62,078 | 12,416 |
| Time series | 4 | 6 | 66.7% | 99,725 | 16,621 |
| Data quality | 8 | 16 | 50.0% | 148,827 | 9,302 |
| Report | 3 | 10 | 30.0% | 443,980 | 44,398 |

## 5. 按难度统计

| 难度 | 通过 | 总数 | 通过率 | Token |
| --- | ---: | ---: | ---: | ---: |
| Easy | 66 | 74 | 89.2% | 579,288 |
| Medium | 45 | 59 | 76.3% | 804,997 |
| Hard | 9 | 17 | 52.9% | 542,121 |

## 6. 工具使用

| 工具 | 调用次数 |
| --- | ---: |
| `execute_sql` | 180 |
| `inspect_database` | 77 |
| `create_report` | 26 |
| `display_chart` | 6 |
| `load_skill` | 2 |
| `read_query_result` | 1 |

观察：

- 150 个案例产生了 180 次 SQL 调用，说明部分案例发生 SQL 修正或重复分析。
- 77 次 `inspect_database` 表明复杂请求通常会主动检查 Schema。
- 10 个报告案例调用了 26 次 `create_report`，存在明显的失败重试与 Token 放大。

## 7. 失败分类

| 失败类型 | 数量 | 占全部案例 | 说明 |
| --- | ---: | ---: | --- |
| 金额精度差异 | 12 | 8.0% | 结果通常只差 0.01 |
| 输出常量/标签不符合契约 | 8 | 5.3% | 例如要求 `region`，实际返回 `customers.region` |
| 查询语义或结果行数错误 | 3 | 2.0% | 承运商没有订单时模型忽略过滤条件 |
| Report block 类型异常 | 6 | 4.0% | `'str' object has no attribute 'get'` |
| Report 参数 JSON 解析失败 | 1 | 0.7% | 模型生成的工具参数 JSON 不合法 |
| 合计 | 30 | 20.0% |  |

### 7.1 金额精度差异

失败案例：

```text
NW055 NW057 NW072 NW073 NW074 NW075
NW077 NW078 NW095 NW096 NW115 NW149
```

参考 SQL 在乘法前把 `unit_price` 和 `discount` 转为 `numeric`：

```sql
od.unit_price::numeric * od.quantity * (1 - od.discount::numeric)
```

Agent 常生成：

```sql
ROUND(SUM(od.unit_price * od.quantity * (1 - od.discount))::numeric, 2)
```

Northwind 对应列为 `real`。第二种写法先进行浮点运算，最后才转 `numeric`，导致部分汇总出现 0.01 差异。这是实际数据分析风险，不应简单通过放宽所有评测容差掩盖。

### 7.2 输出契约不匹配

失败案例：

```text
NW120 NW122 NW127 NW128 NW129 NW130 NW131 NW132
```

查询统计值通常正确，但常量标签与 Prompt 指定值不同，例如：

```text
期望：field_name = region
实际：field_name = customers.region
```

这说明工具结果需要结构化输出契约，或者评测器应将“业务数值正确”和“输出契约正确”拆成两个指标。

### 7.3 承运商值域与空结果处理

失败案例：

```text
NW104 NW105 NW108
```

`Alliance Shippers`、`DHL` 等承运商在数据中没有对应订单。正确结果为空。模型部分情况下忽略指定承运商过滤，转而统计其他有订单的承运商。

后续应要求 Agent 在聚合前验证筛选值是否存在，并正确保留空结果，而不是主动替换成“最接近”的有效值。

### 7.4 报告工具健壮性

失败案例：

```text
NW136 NW137 NW140 NW141 NW142 NW143 NW145
```

其中 6 个案例因为 `blocks` 中出现字符串而触发：

```text
'str' object has no attribute 'get'
```

另一个案例的工具参数不是合法 JSON。报告服务应在进入持久化逻辑前使用严格 Schema 校验，并把错误作为可重试工具结果返回给 Agent，而不是让整次 Run 异常终止。

## 8. 运行事故与修复

首次完整运行中，Dramatiq 的 at-least-once 投递导致同一个 Eval Run 被两个 Worker 重复执行。发现时产生了 32 条重复结果和约 228,679 个额外 Token。

处理结果：

1. 立即停止 Worker，阻止额外费用。
2. 每个 Case 保留最早结果，删除 32 条重复 `EvalCaseResult`。
3. 将 Run 重置为 queued，只续跑 8 个缺失案例。
4. Worker 使用原子 `queued → running` 更新领取任务。
5. `run_suite` 恢复时跳过已有 Case。
6. 新增恢复测试。
7. 完整后端测试结果：21 passed。

烟雾运行产生 100,994 Token，重复运行产生约 228,679 Token；两者均不计入第 3～5 节的正式基线指标。

## 9. 当前能力边界

### 稳定能力

- 单表过滤和计数。
- 明确的分类聚合。
- 常规 Join 和 Top N。
- Schema 检查与只读 SQL 执行。
- 大多数中等难度业务指标。

### 需要优先改进

1. 报告工具输入 Schema 和错误恢复。
2. 金额字段的 Decimal 计算规范。
3. 工具输出契约遵循。
4. 空结果和值域检查。
5. 报告任务的调用次数和 Token 控制。

## 10. 建议的改进顺序

### P0：报告工具边界

- 为 Report Block 建立 Pydantic 判别联合类型。
- 禁止字符串或未知 Block 直接进入 `save_report`。
- 将 JSON/Schema 错误返回为 `retryable` 工具结果。
- 限制一次报告任务的 `create_report` 重试次数。

### P0：金额计算规范

- 在业务上下文中定义标准净销售额公式。
- 在 SQL 生成指令中要求浮点金额先转换为 `numeric`。
- 考虑把金额字段映射为 Decimal 语义。
- Eval 同时保留严格匹配和容差匹配，但发布 Gate 仍使用严格业务口径。

### P1：输出契约

- 为工具增加 Output Schema。
- 将业务值正确性与展示标签正确性分别计分。
- 对用户明确指定的别名和常量字段增加验证。

### P1：值域与空结果

- 执行聚合前验证筛选维度值。
- 空结果必须原样返回，不得替换成其他实体。
- 为“没有数据”增加专门 Eval。

### P2：成本优化

- 在 Context Builder 中按需注入 Schema。
- 缓存同一 Run 中的 Schema 检查。
- 报告任务先完成数据查询和验证，再一次性创建报告。
- 为模型调用、工具调用和 Token 设置分阶段预算。

## 11. 基线限制

- 每个 Case 只执行一次，尚未计算 pass@3 或 stable-pass。
- 当前 `data_match` 使用严格行值匹配并忽略行顺序。
- 模型具有随机性，同一 Fixture 重跑可能得到不同结果。
- 评测主要覆盖 PostgreSQL 数据分析，尚未覆盖文件上传、Memory、Intent Router 和 MCP 端到端。
- 报告案例同时评估数据查询和报告创建，无法仅凭总分区分内容质量。
- 本次运行发生过队列重复投递，已修复；唯一结果指标不包含重复 Run，但实际 API 账单包含重复 Token。

## 12. 下一次基线

完成 P0 修复后，使用完全相同的 Fixture 创建 `Northwind Baseline V1.1` Run，并比较：

- 总通过率是否高于 80%。
- 报告通过率是否从 30% 提升到至少 80%。
- 金额精度失败是否清零。
- 平均模型调用是否低于 2.84。
- 报告平均 Token 是否显著低于 44,398。
- 是否仍存在重复执行或无界重试。
