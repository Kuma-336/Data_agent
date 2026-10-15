# Data Agent 基准测评计划

## 1. 目标

这套基准用于回答一个明确问题：同一版本的数据、用例、验收规则和模型配置下，Data Agent 改造前后的正确性、安全性、工具使用和成本是否发生了可解释的变化。

首版基准固定为 Northwind Baseline V1。它包含 150 条 PostgreSQL 数据分析任务，覆盖简单过滤、聚合、关联与排名、时间序列、业务指标、数据质量、报告和高级分析。V1 一旦用于对照实验，就不允许原地修改题目、参考 SQL 或验收口径；任何口径变化都必须生成 V2。

## 2. 可重复性契约

每次正式运行必须同时固定以下输入：

| 输入 | 固定方式 |
| --- | --- |
| 数据集 | Northwind SQL commit `cd0ef28d66369fbe177778e604e4be0f153c9e5c` |
| 数据哈希 | SHA-256 `0ee30c01ba282f7194f38bf7f99cd6be0470b7ee5f67d0f7ca41fb058d735e0c` |
| 用例文件 | `evals/northwind_baseline_v1.json` |
| Smoke 用例文件 | `evals/northwind_baseline_v1_smoke.json` |
| 用例锁文件 | `evals/northwind_baseline_v1.lock.json` |
| 数据库版本 | PostgreSQL 16 |
| 数据权限 | `northwind_reader`，默认事务只读 |
| 评测规则 | 严格行值匹配、忽略行顺序、必需工具、禁用工具、证据和工具预算 |
| 模型配置 | 每个 Eval Run 记录模型名称和 Token；正式对比时保持供应商、模型与采样参数一致 |

模型调用具有随机性，因此“重复”指实验条件和评分过程可复现，不代表每次模型输出逐字一致。重要版本至少运行三次，分别报告单次通过率、三次平均通过率和 stable pass，即三次均通过的案例比例。

## 3. 用例组成

| 类别 | 数量 | 主要验证点 |
| --- | ---: | --- |
| Simple filter | 32 | 过滤值和计数 |
| Aggregation | 32 | 分组、金额与聚合 |
| Join and ranking | 30 | 多表关联、Top N 和稳定排序 |
| Business metric | 19 | 指标定义、日期范围和 Decimal 语义 |
| Data quality | 16 | 空值、空表、重复值和边界条件 |
| Report | 10 | 查询证据、报告创建和文字结论 |
| Time series | 6 | 月度分桶和时间边界 |
| Advanced | 5 | 多阶段聚合和复杂业务逻辑 |
| 合计 | 150 |  |

难度分布为 easy 74、medium 59、hard 17。另提供由同一生成器派生的 12 条 Smoke Fixture，可在正式全量运行前验证数据源、模型和工具链是否可用。

## 4. 机器校验

生成或刷新 Fixture：

```powershell
python scripts/generate_northwind_eval_cases.py
```

检查已提交 Fixture 是否与生成器逐字节一致：

```powershell
python scripts/generate_northwind_eval_cases.py --check
```

同时检查本地 Northwind SQL：

```powershell
python scripts/generate_northwind_eval_cases.py --check --dataset backend/test-data/northwind/northwind.sql
```

该检查会验证 150 个连续且唯一的 Case Key、唯一名称与 Prompt、只读参考 SQL、验收字段一致性、Fixture 哈希和数据集哈希。任何失败都应阻断正式评测。

## 5. 准备隔离数据源

下载并验证固定版本的数据，然后启动隔离的 PostgreSQL：

```powershell
python scripts/bootstrap_northwind_eval.py --download --start
```

数据库连接参数如下：

```text
host: localhost
port: 55432
database: northwind
username: northwind_reader
password: northwind_reader
sslmode: disable
```

启动脚本会验证 `customers=91`、`orders=830`、`order_details=2155`，并确认评测角色的 `default_transaction_read_only=on`。正式评测不得使用管理员账号。

## 6. 导入与运行

1. 在 Data Agent 中创建上述只读连接，记录 Connection ID。
2. 调用 `POST /eval-suites/import`，Body 为：

```json
{
  "target_id": "连接 ID",
  "fixture": "evals/northwind_baseline_v1.json 解析后的 JSON 对象"
}
```

3. 导入接口会验证 Fixture，并保存 `fixture_hash`、数据集元数据、Case Key、类别、难度和完整验收规则。同一用户、同一 Fixture、同一数据源重复导入会返回原 Suite，不会再创建 150 条重复用例。
4. 调用 `POST /eval-suites/{suite_id}/runs`。该操作会产生真实模型费用。
5. 轮询 `GET /eval-runs/{eval_run_id}`，直到状态为 `completed` 或 `failed`。

评测器对每个案例同时检查：

- 最后一次 SQL 结果是否与参考 SQL 严格一致，行顺序除外。
- 所有必需工具是否出现。
- 是否调用禁用工具。
- 报告任务是否创建报告。
- 是否产生可追踪的查询证据。
- 工具调用次数是否未超过 Case 预算。

## 7. 标准运行顺序

正式实验按以下顺序执行：

1. Fixture 与数据哈希检查。
2. 后端自动化测试。
3. 12 Case smoke run；任何环境或鉴权失败都停止全量运行。
4. 150 Case full run。
5. 重要版本连续运行三次。
6. 导出每次 Run 的模型、Token、工具事件、失败详情和总体汇总。
7. 按 Case Key 对齐改造前后结果，禁止只比较总体通过率。

## 8. 报告指标

每次对比至少报告：

- 总通过率、按类别通过率、按难度通过率。
- stable pass、波动案例和连续失败案例。
- 数据正确率、工具契约通过率、证据通过率和预算通过率。
- P50 与 P95 延迟。
- 输入 Token、输出 Token、平均 Token 每 Case。
- 模型调用数、SQL 调用数、报告工具调用数和重试数。
- 失败分类与具体 Case Key。

正确性和安全性是硬门槛。不能用更低成本或更低延迟抵消正确性下降，也不能通过放宽 V1 的评分规则改善结果。

## 9. 当前边界

Northwind V1 是数据查询与报告基准，不覆盖尚未上线的 Intent Router、Memory V2 和 MCP V2。

V1 还存在两项已确认的评测盲区：业务口径被写入 prompt，导致 `semantics_wrong` 不可检测；acceptance 要求必须调用 `execute_sql` 并产生证据，导致 `should_have_asked` 不可表达且被判为失败。两项问题及解决方向登记于 [`open-issues.md`](./open-issues.md)（ISSUE-001、ISSUE-002）。在其关闭之前，V1 通过率不足以作为路线决策依据。

后续阶段应各自新增独立 Suite，并沿用相同的版本、哈希、导入、评分和锁文件机制，避免把不同性质的能力混成一个不可解释的总分。
