# Data Agent 待解决问题登记

- 状态：两条已登记问题均为 Deferred（见 [ADR-0001](./adr/0001-project-goal-harness-capability.md)）
- 创建日期：2026-10-15
- 范围：评测盲区与其对应的 Agent 能力缺口
- 关联文档：[`evaluation-plan.md`](./evaluation-plan.md)、[`data-agent-evolution-rfc.md`](./data-agent-evolution-rfc.md)

## 0. 本文定位

本文登记**已确认存在、尚未解决**的问题。每条给出现象、证据、影响和验收标准，但不规定实施时间。

ID 一经分配不再复用，关闭时保留条目并标注结论，便于后续对照。

当前登记两条严重问题，均属同一性质：**Data Agent 最主要的两类失败模式，现有评测体系无法检测，其中一类还会被判为失败。**

---

## 1. 问题分类前提：两类沉默失败

在描述问题之前先固定术语。两类的分界线是**该问题是否存在唯一正确答案**。

| 分类 | 存在唯一正确答案 | Agent 的错误行为 |
| --- | --- | --- |
| `semantics_wrong` | 是 | 表、列、语法均正确，但业务口径错误 |
| `should_have_asked` | 否 | 擅自选定一种口径，且未向用户声明 |

两类的共同危险性在于**沉默**：

- `schema_wrong` 会触发 `column does not exist`。
- `dialect_error` 会触发解析失败。
- 这两类**不产生任何错误信号**，返回一个格式完整、数值合理的答案。

且现有 `verify_run`（[`backend/app/services/runs.py`](../../backend/app/services/runs.py)）只检查三件事：是否产生 `query_id`、是否有工具报错、报告是否含 markdown 叙述。两类失败在这三项上**全部通过**，`status` 判定为 `pass`。

### 1.1 `semantics_wrong` 的口径轴

以 Northwind 为例，同一个问题至少存在以下独立决策点：

| 口径轴 | Northwind 中的具体表现 |
| --- | --- |
| 时间字段 | `order_date` / `shipped_date` / `required_date` |
| 时间边界 | `BETWEEN` 闭区间 vs `>= AND <` 半开区间；财年起点 |
| 聚合粒度 | 按订单 `AVG` vs 按明细行 `AVG` |
| 金额定义 | 是否含 `discount`；是否含 `freight`；行级折扣 vs 整单折扣 |
| 过滤范围 | 取消与退货订单是否计入；`discontinued` 产品是否计入 |
| null 语义 | `shipped_date IS NULL` 视为未发货还是直接排除 |
| 去重键 | 客户数按 `customer_id` 还是 `company_name` |

### 1.2 `should_have_asked` 的判定细则

**过度澄清同样是失败。** 判定标准不是"是否模糊"，而是：

> 不同的合理口径是否导致**定性结论不同**？
>
> - 导致（排名第一的对象发生变化）→ 应澄清
> - 不导致（方向一致，仅数值差异）→ **不应澄清**；应选定默认口径并显式声明

据此，**声明口径可替代大多数澄清**：

- "按总销售额口径，最好的客户是 X" → 用户可自行判断口径是否符合预期，流程不中断
- "你需要哪种口径？" → 中断流程，且用户往往同样未想清楚

目标行为：**能声明即声明；仅当不同口径导致结论相反时强制澄清。**

---

## 2. ISSUE-001：评测集无法检测 `semantics_wrong`

**严重程度**：高
**状态**：Deferred（依 [ADR-0001](./adr/0001-project-goal-harness-capability.md) §4，修复路径指向语义层，当前目标下不实施；条目保留以记录判断过程）
**影响组件**：`evals/northwind_baseline_v1.json`、`docs/agent-evolution/evaluation-plan.md`

### 2.1 现象

Northwind Baseline V1 的 150 条用例中，业务口径被直接写入 prompt，因此口径选择不构成被测内容。模型只需按题面书写 SQL。

### 2.2 证据

NW101（`category: business_metric`，`difficulty: hard`）的 prompt：

> 计算 1996 年平均订单折后净销售额。**先按订单汇总，再求平均。**只返回 order_year 和两位小数的 avg_order_value。

其 `reference_sql` 隐含四个口径决策：折后为行级打折、按 `order_date` 半开区间、先按订单汇总再平均、不含 `freight`。其中**最易出错的"聚合粒度"已作为题面给出**。

同类情况见 NW104：

> 计算 Alliance Shippers **已发货**订单从下单到发货的平均天数。

"已发货"把 `shipped_date IS NOT NULL` 这一 null 语义决策写入了 prompt。

### 2.3 影响

- 口径一旦进入题面，`semantics_wrong` 被消除，实际被测能力退化为 SQL 书写正确性。
- 用例构成中 `simple_filter`(32) + `aggregation`(32) + `join_ranking`(30) = 94/150 本身即为口径明确题型。
- **后果：基线通过率偏乐观，无法预测生产表现。**

### 2.4 解决方向

新增用例类别 `metric_semantics`：prompt 删除全部口径提示，口径定义移入 `ProjectContextDocument` fixture，验收追加"必须读取语义层"。

```yaml
case_key: NWS001
category: metric_semantics
prompt: "1996 年平均订单金额是多少？"
context_fixture: metrics/avg_order_value.md
reference_sql: <与 NW101 相同>
acceptance:
  data_match: exact_rows_ignore_order
  must_read_context: true
```

此类用例验证的不是"能否写出 SQL"，而是**能否正确读取并应用已声明的业务口径** —— 即语义层与评测的接合点。

### 2.5 验收标准

- `metric_semantics` 类别至少覆盖 19 条现有 `business_metric` 对应的无提示版本。
- 验收能区分"读取了语义层并用对"与"未读取但恰好猜对"。
- V1 的 150 条用例不被原地修改（遵循 `evaluation-plan.md` §1 的 V1 冻结约定），新用例进入 V2 或独立 Suite。

### 2.6 约束

`evaluation-plan.md` §1 规定 V1 用于对照实验后不得原地修改题目、参考 SQL 或验收口径。因此本问题的修复**必须以新 Suite 形式交付**，不得改写 V1。

---

## 3. ISSUE-002：评测集在结构上惩罚 `should_have_asked`

**严重程度**：高
**状态**：Deferred（依 [ADR-0001](./adr/0001-project-goal-harness-capability.md) §4，修复路径指向语义层与口径声明能力，当前目标下不实施；条目保留以记录判断过程）
**影响组件**：`evals/northwind_baseline_v1.json`、`backend/app/services/evals.py`

### 3.1 现象

现有 case schema 无法表达"正确行为是不产生 SQL"。一个正确选择了澄清的 Agent，在现有评测下得分**更低**。

### 3.2 证据

每条用例的 acceptance 结构固定为：

```json
"acceptance": {
  "data_match": "exact_rows_ignore_order",
  "required_tools": ["execute_sql"],
  "evidence_required": true
}
```

三项要求同时成立：唯一正确行集、必须调用 `execute_sql`、必须产生可追踪证据。而 `should_have_asked` 的正确行为是调用 `clarify` 且不产生 SQL —— 在此 acceptance 下必然判负。

### 3.3 影响

- `should_have_asked` 在现有体系中**不可表达**，无法度量，无法设门槛。
- 更严重的是方向错误：**评测正在惩罚正确行为。** 若以该基线作为优化目标，会系统性地把 Agent 推向"遇到歧义就猜"。
- 与 RFC §6.3「低置信度或缺少资源时应进入 `clarify`，而不是猜测」的设计目标直接冲突。

### 3.4 解决方向

为 `evals.py` 新增 `mode: rubric` 判定分支，允许一道题存在两种正确答案（澄清 或 声明口径）。现有 `exact_rows_ignore_order` 逻辑不改动。

```yaml
case_key: NWA001
category: ambiguous_metric
prompt: "我们最好的客户是谁？"
context_fixture: none
acceptance:
  mode: rubric
  accept_if_any:
    - tool_called: clarify
      question_covers: [销售额, 订单数, 利润]
    - answer_declares_metric: true
      and_sql_matches_declaration: true
  reject_if:
    - silent_assumption: true
```

注意 ISSUE-001 与 ISSUE-002 存在对称性：**同一个业务问题，语义层中定义了口径时属于 `metric_semantics`，未定义时属于 `ambiguous_metric`。** 同一批问题可同时服务两类检测，差异仅在 fixture 是否提供定义。

### 3.5 验收标准

- `evals.py` 支持 `mode: rubric`，且不改变现有 `exact_rows_ignore_order` 用例的判定结果。
- `ambiguous_metric` 类别不少于 10 条，覆盖指标未定义、时间基准未定义、过滤范围未定义三种歧义来源。
- 判定能区分三种行为：澄清、声明口径后作答、静默假设。前两者通过，第三者失败。
- 对"过度澄清"同样设负向用例：口径明确的问题若调用 `clarify`，应判为失败。

### 3.6 依赖

本问题的修复需要 Agent 侧具备**结构化口径声明**能力，否则 `answer_declares_metric` 无法可靠判定。当前 system prompt（[`backend/app/services/agent.py`](../../backend/app/services/agent.py) 的 `build_system_prompt`）仅以散文要求 "Explain conclusions, caveats, and the SQL used"，无结构化强制。该能力缺口应在修复前或同期解决。

---

## 4. 两条问题的共同结论

ISSUE-001 与 ISSUE-002 指向同一个系统性判断：

> **现有评测体系度量的是 schema linking 与 SQL 书写正确性；而 Data Agent 的主要失败来源是语义接地与不确定性处理。**

因此在这两条关闭之前：

- 基于 V1 通过率做出的任何路线决策（框架选型、编排重构、沙箱）都缺乏有效依据。
- RFC Phase 0 的"退出条件"应追加一项：评测集能够表达并检测上述两类失败。

建议将本节结论回写至 `evaluation-plan.md` §9「当前边界」。

---

## 附录：本次审查发现的其他问题（未展开）

以下问题已确认存在，但不在本文范围，另行登记。列此仅为避免丢失。

| 编号 | 问题 | 位置 |
| --- | --- | --- |
| A-01 | PostgreSQL 连接的 `schema_cache` 只含表名不含列名，模型须猜列名 | `backend/app/main.py:197` |
| A-02 | `validate_readonly_sql` 对三条路径统一按 `postgres` 方言解析，但两条实际执行 DuckDB | `backend/app/services/sql_guard.py:17` |
| A-03 | 工具错误返回的 `retryable` 标记无任何代码读取，模型会反复重试不可恢复错误 | `backend/app/services/agent.py:133` |
| A-04 | 压缩边界用 `len(messages)` 反推下标，与 `db_messages` 三处不对齐 | `backend/app/services/memory_manager.py:48` |
| A-05 | `file_schema` 按整体 JSON 字符串硬截 4000 字符，宽表会截断 `columns` 并产生非法 JSON | `backend/app/services/agent.py:39` |
| A-06 | `verify_run` 只检查形式（有无 `query_id`、有无报错），不检查结论与证据是否一致 | `backend/app/services/runs.py` |
| A-07 | 已含 `LIMIT` 的查询不校验其大小；危险函数为正则黑名单而非 AST 白名单 | `backend/app/services/sql_guard.py:25-27` |
