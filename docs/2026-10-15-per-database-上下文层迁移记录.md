# per-database 上下文层迁移记录

日期：2026-10-15
范围：将 nao 的三层上下文模型（schema / profiling / business rules）迁移至 Personal Data Agent，以持久化文件承载，权限随数据库归属。

## 变更目标

nao 不把库结构塞进系统提示，而是落成项目目录里的文件按需读取：表结构在 `columns.md`，列的真实值分布在 `profiling.md`，业务口径在 `RULES.md`。本次为 data agent 建立等价结构，并追加一项本项目特有的约束：**三类文件均按数据库分树存放，可查阅权限伴随该数据库的归属**。

迁移前的状态有三处缺口：

1. `DataSource.schema_cache` 只存表名（`table_schema` / `table_name`），**没有列信息**，模型必须猜列名。
2. 系统提示把 `schema_cache` 整体 JSON dump 进去并截断到 3000 字符，既浪费预算又可能丢列。
3. profiling 只覆盖上传文件（`profile_file`），PostgreSQL 连接没有画像；业务上下文 `ProjectContextDocument` 是 user 级的，与具体数据库无关联。

## 已实现内容

1. 新增 `backend/app/services/context_store.py`（133 行）。负责文件布局、权限闸门与路径安全，是唯一允许拼接上下文路径的位置。
2. 新增 `backend/app/services/context_sync.py`（261 行）。三种来源（PostgreSQL `information_schema`、上传文件、DuckDB 样本库）各自采样，但汇入**同一套渲染器和同一套 profiling**，因此同一份 `columns.md` 不论来源读起来一致。渲染函数为纯函数，可脱离数据库测试。
3. `profile_frame`（原 `analytics._profile_frame`）改为公开，并新增 `top_values`：低基数列的真实值与频次。该字段只对分类列生成，数值列与日期列不生成——数值已由 min/max/mean 概括，`top_values` 的用途是防止模型猜枚举字符串。
4. 系统提示不再内联 schema dump。改为给出每个数据库的**表索引**，并把该数据库的 `RULES.md` 原文内联（与 nao 一致：规则是常驻注入，表结构是按需读取）。
5. 新增两个按需读取工具：`list_table_context(target)` 与 `read_table_context(target, table, kind)`。`target` 沿用 `execute_sql` 既有词汇（`data` / `jaffle_shop` / 连接 id），模型不需要学第二套寻址方式。
6. `inspect_database` 改为返回表索引并指向 `read_table_context`，仅在尚未同步时回落到 `schema_cache`。
7. 系统提示新增两条无条件规则：不得假设列名（先读 `columns`）；对任何列值过滤前先读 `profiling` 并使用 `top_values` 中的真实值。另加一条口径声明规则：优先遵循该库的业务规则，规则未覆盖时声明所用定义，仅当不同口径会改变结论（而非仅改变数值）时才澄清。
8. 新增同步端点：`POST /connections/{id}/sync-context`（可带 `profile=false` 跳过画像）、`POST /files/{id}/sync-context`、`POST /sample-tables/jaffle-shop/sync-context`。文件上传时自动同步，且同步失败不影响上传成功。
9. 新增读写端点：`GET /context/databases`、`GET /context/{kind}/{asset_id}/tables`、`GET /context/{kind}/{asset_id}/tables/{table}/{document_kind}`、`GET|PUT /context/{kind}/{asset_id}/rules`。
10. 新增 `data_sources.context_synced_at` 字段及增量迁移。同步后 `schema_cache` 降级为轻量索引（`{"tables": [...], "stored_as": "context_files"}`），不再承载全量结构。
11. 抽出 `query.engine_for_source`，消除三处重复的连接构造逻辑，主机白名单检查统一在该函数内。
12. `config.py` 新增六个参数：`context_max_tables`(200)、`context_preview_rows`(10)、`context_profile_sample_rows`(50000)、`context_profile_top_values`(10)、`context_profile_max_distinct`(50)、`context_max_rules_chars`(50000)。

## 文件布局

```
{DATA_DIR}/context/user=<user_id>/<kind>=<asset_id>/
    RULES.md
    tables/<table>/columns.md
    tables/<table>/profiling.md
    tables/<table>/preview.md
```

`kind` 取 `connection`、`file` 或 `sample`。表目录名使用限定名（PostgreSQL 为 `schema.table`），上传文件固定为 `data`。

## 权限与数据边界

- 权限不依赖调用处的检查，而是结构性的：`ContextTarget` 只能由 `connection_target(source)` / `file_target(file)` / `sample_target(user_id)` 构造，前两者的入参必须是调用方**已按 `user_id` 查出的 ORM 行**。拿不到该行就无法构造 target，也就拼不出路径。
- 路径本身嵌入 `user=<user_id>`，不同用户的上下文在文件系统层面即分离。
- 段名白名单为 `^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,119}$`，拒绝 `..`、`/`、`\`、空串与隐藏文件；路径 resolve 后再做一次 `_assert_within` 包含性检查。文档种类限定在 `columns` / `profiling` / `preview` 三者之内。
- HTTP 侧由 `_context_target_for_user` 统一解析，内部复用既有的 `_source_for_user` / `_file_for_user`；Agent 侧由 `_context_target` 解析，且只接受**本轮已附加**的资源。
- 样本库 `jaffle_shop` 为全局只读，但每个用户在自己的目录下拥有独立的 `RULES.md`，可各自记录口径而互不影响。
- PostgreSQL 同步在只读事务内执行（`SET TRANSACTION READ ONLY`），标识符经双引号转义，采样行数以整型字面量注入，不接受外部字符串拼接。

## 与 ADR-0001 的关系

ADR-0001 §4.1 将「语义层 / metric layer」列为明确非目标，并要求范围变更须另立 ADR。本次变更的定位如下：

- `columns.md` 对应 ADR-0001 §3.1 中标注为「**修复**」的 A-01（PostgreSQL 列级 schema 缺失）。
- `profiling.md` 属同一性质的瓶颈摘除，服务于 C-1 的「不成为瓶颈」上限。
- `RULES.md` 为纯 markdown，由模型自行读取。**不含指标定义建模、measure/dimension 编译器或治理查询路径**，因此不构成 §4.1 所禁的语义层；其归属为 §3.2 列为自建的「Context 治理」与权限模型。

ISSUE-001 的修复前置条件因此具备：业务口径已有 per-database 的持久化落点，`must_read_context` 一类验收可依据 `read_table_context` 调用记录或内联规则判定。是否立 ADR-0002 固化该边界，尚未决定。

## 验证结论

结论：可提交。既有行为未被破坏，用户隔离与只读约束未被新增路径旁路。

自动化验证：`pytest -q` 结果为 `34 passed`，其中新增 9 条用例，覆盖——

- 上传后 schema 与 profiling 文档落盘，且 `top_values` 只出现在低基数列；
- 跨用户读取该库上下文、列出表、写入规则，均返回 404；
- `..`、`../secret`、`a/b`、`a\b`、空串、`.hidden` 等段名被拒绝，未知文档种类被拒绝；
- 规则按数据库往返读写，并反映在 `/context/databases` 的 `has_rules`；
- DuckDB 样本库同步后，`orders` 的画像含 `status` 的真实取值，`columns.md` 含 9 列与 99 行计数；
- PostgreSQL 形状的列定义渲染出主键、NOT NULL、默认值与注释；
- 系统提示内联规则与表索引，且不再含 `; schema=` 形式的结构 dump。

实测产出与 nao 的格式一致，例如 `orders/profiling.md` 中：

```
- {"name": "status", "dtype": "object", "null_count": 0, "distinct_count": 5,
   "sample_values": [...], "top_values": [{"value": "completed", "count": 67},
   {"value": "placed", "count": 13}, {"value": "shipped", "count": 13},
   {"value": "returned", "count": 4}, {"value": "return_pending", "count": 2}]}
```

过程中修正一处自身设计缺陷：初版对数值列也生成 `top_values`（`credit_card_amount` 列出 10 个浮点值），属无效膨胀，已限制为仅分类列，`orders` 的画像文档因此减半。

**未验证项**：`sync_connection_context` 的 PostgreSQL 路径未实际执行。本机 Docker 守护进程未运行，亦无可连的 PostgreSQL 实例；`information_schema` 查询仅通过语法编译与渲染器单测，未对真实库运行。验证方式为 `python scripts/bootstrap_northwind_eval.py --download --start` 后调用 `POST /connections/{id}/sync-context`。已为每张表加独立 try/except 与事务回滚，单表不可读不会中断整库同步，结果中以 `skipped` 返回。

## 提交与推送

- `1797cbb` — 收拢工作区中既有的未提交工作（Northwind 基准 150 条与锁文件、run/step 追踪、jaffle_shop 样本源、PDF 报告导出、`docs/agent-evolution/`）。
- `022b406` — 本次上下文层，11 个文件，+1023 / −28。

两者均已推送至 `origin/main`。

提交拆分按「是否被本次改动触碰」划线，而非按改动内容：`models.py`、`main.py`、`config.py`、`agent.py`、`analytics.py`、`query.py` 六个文件同时含两批改动（例如 `models.py` 的 86 行新增中仅 1 行属本次），按 hunk 拆分需交互式 staging，当前环境不支持。**后果：`1797cbb` 不是独立可运行的提交**——它带入了 `evals.py` / `runs.py` / `test_evals.py`，但支撑其的 `Eval*` 与 `AgentRun` 表定义落在 `022b406`。最终状态完整且测试通过，但 bisect 至 `1797cbb` 会 import 失败。

`.gitignore` 新增三条：`backend/.venv/`（为本机运行测试所建，因 Docker 不可用）、`backend/*.egg-info/`、`**/__pycache__/`。

## 后续风险与建议

1. **文档会过期。** `profiling.md` 带 `Computed at` 时间戳，但系统未对陈旧度做任何提示；schema 漂移后模型会按旧文档写 SQL 且不报错。建议后续在表索引中暴露同步时间，或对超期文档在提示中加标注。
2. **PostgreSQL 采样开销未实测。** 每表拉取最多 50000 行至进程内做画像，宽表或大表上的内存与耗时未经测量。必要时改为下推聚合（每表一条带位置别名的聚合查询），或下调 `context_profile_sample_rows`。
3. **`sample_values` 与 `top_values` 存在冗余。** 分类列同时携带两者，前者可考虑在有 `top_values` 时省略，以压缩文档体积。
4. **`RULES.md` 无结构校验，也无体积预算。** 当前仅截断至 `context_max_rules_chars`。它随每条消息注入，多库同时附加时会叠加占用；建议后续纳入统一的 context 预算核算。
5. **PostgreSQL 路径需真机验证后方可依赖**，见上节未验证项。
6. 是否以 ADR-0002 固化「per-database 上下文文件属 Context 治理、指标建模属非目标」的边界，待决定。不固化的风险是后续讨论再次摆回语义层方向，这正是 ADR-0001 §8 所列的「范围回爬」风险。
