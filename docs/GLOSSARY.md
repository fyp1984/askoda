# 术语表

本文解释 Askoda 研发与验收过程中出现的缩写与专有名词。

**每条定义均以代码或设计文档为准**，括号内标注权威来源，便于自行核对：

- `gateway/` 下的实现
- `agents/state/batches.json`（批次状态机）
- `06-测试与验收/` 下的验收单

---

## 一、架构与组件

### MCP（Model Context Protocol）
AI Agent 调用外部工具的协议。在本项目中它是**网关与前端/BFF 之间的唯一通道**。

对外只暴露一个 streamable-http 端点（`/mcp`），共 **45 个工具**。一个 MCP 会话内不支持并发调用同一个 `Mcp-Session-Id`——并行发请求会导致 SSE 流互相截断进而挂死。

> 权威来源：`gateway/app.py`（`@mcp.tool` 共 45 处）、`bff/mcp_client.py`

### BFF（Backend for Frontend）
**给前端专用的后端层**。它坐在前端和 MCP 网关之间，只做三件事：聚合、转换、脱敏。

架构红线 R2 要求**前端只调自研 BFF**，不得直连任何后端。BFF 另有 R2 脱敏职责：把网关响应里的容器内部地址抹掉再交给前端。

> 权威来源：`bff/app.py`、`bff/sanitize.py`

### MDL（**M**anifest **D**ata **L**ayer / 语义模型文件）
一个 JSON 文件，描述"有哪些表、哪些列、表之间什么关系"。**它是 AI 唯一允许看见的数据结构**。

A 库 6 模型/29 字段/5 关系；B 库 8 模型/47 字段/10 关系。SQL 里引用了 MDL 闭集之外的表或列，会被规则 `UNMAPPED_OBJECT_REF` 直接阻断。

> 权威来源：`wren-docker/workspace/mdl.json`、`wren-docker-b/workspace/mdl.json`

### Wren / Wren Engine
**开源语义引擎**，项目的 L0 层基础设施。只负责"把 SQL 翻译成对底层数据源的实际查询"，不做语义理解、不做业务判断。

> 权威来源：`wren-docker/`、`gateway/wren.py`

### RAGFlow
**开源知识库引擎**，作为知识底座使用。它负责把文档切块、向量化、检索、返回带坐标的引用片段。

当前部署下 19380 端口是一个 nginx 反代，**其 backend 仍是旧绑定栈内的 RAGFlow 实例**——"独立栈"这个说法目前不成立。

> 权威来源：`gateway/knowledge.py`、`docker-compose.yml`

### TEI（Text Embeddings Inference）
HuggingFace 官方的 **embedding 模型推理服务**。把文本转成 1024 维向量。

本项目固定使用 **bge-m3**：库里已入库的向量都是它生成的，若换模型会让查询向量与库内向量空间不一致 → **召回静默变错**（比直接报错更危险）。

> 权威来源：`.models/README.md`、`tools/_archived/fix_tei_model.sh`

### 双库（A 库 / B 库）
| | A 库 | B 库 |
|---|---|---|
| 域名 | 电商域 | 零售会员域 |
| 治理程度 | **裸库**（描述覆盖 6.9%） | **已治理**（覆盖 100%） |
| 规模 | 6 模型 / 29 字段 / 5 关系 | 8 模型 / 48 字段 / 10 关系 |
| 语义词库 | 7 条 | 114 条 |

**A 库刻意不补描述**：它的低覆盖是为了验证"缺少元数据时系统会怎么表现"，**给 A 库补描述刷分属违规**。因此"业务口吻问 A 未命中知识库"是**预期行为**，不是缺陷。

> 权威来源：`03-架构与设计/`、`agents/state/batches.json` 的 A/B 库差异记录

---

## 二、需求理解

### 槽位（slot）
一次分析里被**单独填充的结构化字段**，共 7 个：`subject`（主体）、`granularity`（颗粒度）、`time`（时间）、`scope`（范围）、`fields`（输出字段）、`risks`（风险）、`contacts`（对接人）。

槽位填不出来时会显式标记为缺失（`无候选结论`），**不允许用推测值填坑**。

> 权威来源：`gateway/semantics.py`

### 颗粒度（granularity）
**"结果里的每一行，代表什么"**。这是本项目最容易被业务方忽略、却直接决定数字对不对的口径。

举例：同样一笔订单 100 元，若"一行=一笔订单"则是 100 元；若"一行=一个订单行"（一张订单含 3 个商品行）就是 300 元。**分母口径一变，比率立刻变**。

> 权威来源：知识库《01-指标口径说明书-零售会员域》

### 口径确认 / confirmation
当某个槽位**影响结果但系统无法自行判断**时，生成问题请业务方回答。答复不可覆盖，只能新增版本——保证事后能还原"当时为什么这么判"。

> 权威来源：知识库《04-需求受理与口径确认规程》

---

## 三、规则与判据

### 证据层级 P1–P9
每条结论都必须挂一个证据来源，等级越靠前越贴近业务原话：

| 等级 | 名称 | 来源 |
|---|---|---|
| P1 | 当前业务说明 | 需求单正文（title / description / expected_output） |
| P2 | 业务确认 | 已答复的确认记录 |
| P3 | 表样 | 需求单附件 |
| P4 | 历史案例 | 知识库·案例类文档 |
| P5 | 系统资料 | 知识库·口径/制度/表结构类文档 |
| P6 | 数据字典 | PG 里的 `table_docs` / `column_docs` / `business_glossary` |
| P7 | 表关系 | MDL `relationships` |
| P8 | 通用规则 | 内置规则集 |
| P9 | 模型推断 | LLM 推断（置信度最低） |

> 权威来源：`gateway/evidence.py` 第 40–51 行

### R1–R7（需求语义规则）
**问题还没变成 SQL 之前**就要查的规则，共 7 条（R5 专管"标记为待上架的口径不可使用"）。

> 权威来源：`gateway/semantics.py`

### REGISTRY（SQL 静态规则表）
**SQL 生成之后**要查的规则表，11 条，定义在 `gateway/rules.py`。当前**4 条标记为 `blocking`**（命中即阻断）：

| 规则 | 含义 | 阻断 |
|---|---|---|
| `JOIN_WITHOUT_CONDITION` | JOIN 必须带 ON/USING，禁止笛卡尔积 | ✅ |
| `ONE_TO_MANY_UNHANDLED` | 一对多 JOIN + 聚合必须去重或预聚合，否则金额被放大 | ✅ |
| `UNMAPPED_OBJECT_REF` | 引用的表/列必须在 MDL 闭集内 | ✅ |
| `ENUM_VALUE_INVALID` | 枚举列取值必须在声明值域内 | ✅ |
| `SELECT_STAR` | 禁止 SELECT * | — |
| `GROUP_BY_INCONSISTENT` | 非聚合列必须出现在 GROUP BY | — |
| `UNNECESSARY_DISTINCT` | 单表无聚合场景不该用 DISTINCT | — |
| `LOAD_DATE_SUBSTITUTION` | 禁止用装载日期代替业务日期 | — |
| `MULTI_VALUE_NO_ORDER` | 多值拼接必须指定 ORDER BY | — |
| `DANGLING_DENOMINATOR` | 比率类必须有明确分母 | — |
| `TIME_FIELD_SUSPECT` | 时间过滤应优先用业务日期列 | — |

> 权威来源：`gateway/rules.py` 第 1218 行起的 `REGISTRY`

### 分级阻断（D1 / D2）
两级拦截策略：

- **D1** = 不通过也不自动改写 SQL，直接把问题抛给人看（`review.revised_sql` 恒为 `None`）
- **D2** = L1/L2(blocking)/L3/L4 任一不通过即**阻断**执行；L2 其余规则只记警告

> 权威来源：`gateway/gates.py` 文件头设计说明

### 五层门禁 L1–L5
生成与审查**严格分离**的五道关卡：

| 层 | 做什么 |
|---|---|
| L1 | 写操作检测（`is_write`）——只读工具链下禁止任何写操作 |
| L2 | SQL 静态规则（即 REGISTRY 那 11 条） |
| L3 | SQLGlot 语法解析 |
| L4 | Wren `dry_run` 语义试跑（不真取数） |
| L5 | 结果断言（校验行数/样本是否合理） |

SQLFluff 与 Great Expectations 留了插槽但**默认关闭**，一期未实现。

> 权威来源：`gateway/gates.py` 文件头 + `load_sql_five_layer_gates()` 文档串

---

## 四、质量保障

### 门禁 G0–G3
统一质量检查的四个层级，入口 `tools/gate_all.py`：

| 层 | 做什么 | 特点 |
|---|---|---|
| **G0** | 语法 + 依赖基线静态自检 | 最快 |
| **G1** | 单元自证（`*_selftest.py`） | |
| **G2** | 独立复核（`*_check.py`） | |
| **G3** | 真实链路（探活 + `*_verify.py`） | 会写库 |

`--strict` 模式下**已知基线红也阻塞**。G3 会写业务库，日常只跑 `--layers G0,G1`。

> 权威来源：`tools/gate_all.py`

### 批次（B0–B5）
阶段 A 的实施单元，每个批次有明确的验收判据。状态记在 `agents/state/batches.json`（**唯一权威状态源**），验收台账由它自动生成。

| 批次 | 内容 |
|---|---|
| B0 | 统一门禁入口 + 环境自检 |
| B1 | Agent 驱动端到端验证 |
| B2a | 工具面补齐 41 → 45（只动 app.py） |
| B2b | 修 B1 实测的实现层缺陷（禁动 app.py） |
| B2c | 修 P1-8：R1 方向判据 |
| B3 | 最小前端 web/ + 自研 BFF bff/ |
| B4 | 知识底座复位（**未通过**） |
| B4b | 修 embedding 上游（P1-17） |
| B4c | embedding 迁至远端 + 主备切换 |

> 权威来源：`agents/state/batches.json`

### 架构五红线 R1–R5
不可逾越的架构约束，由 `tools/redline_guard.py` 机器判定：

| 红线 | 含义 |
|---|---|
| **R1** | 不使用开源组件的原生 UI |
| **R2** | 前端只调自研 BFF，不直连任何后端 |
| **R3** | 不 fork、不改开源上游源码 |
| **R4** | 不做 UI 补丁、不做 DOM 注入 |
| **R5** | API 契约版本化（统一 `/api/v1`） |

> 注意：**R1–R5（架构红线）与 P1–P9（证据层级）、R1–R7（语义规则）、G0–G3（门禁）是四套不同编号体系**，容易混淆。本项目历史上就因此出过错判。

> 权威来源：`tools/redline_guard.py`

### 六条提交红线
另一个层面上的硬约束（改开源上游 / 改工具入参名 / 改 D1 / 删数据 / 新增运行时依赖 / 需人工点信任），命中即停，由 `tools/redline_guard.py` 独立判定。

一期允许的运行时依赖**只有**：`fastmcp==4.0.10`、`starlette`、`psycopg[binary]`、`minio`、`sqlglot`。

> 权威来源：`tools/redline_guard.py`

---

## 五、数据与存储

### 元数据库
PostgreSQL 实例 `127.0.0.1:15434/assistant`，存**网关自身的运行元数据**——需求单、分析轮次、证据链、知识引用。

**刻意不放业务数据**：A/B 库是被分析的数据源，元数据库是网关自己的账。混在一起会让"换数据源 0 行代码"这条硬指标立刻失效。

主要表：`demand_requests`（需求单）、`demand_events`（流转事件）、`analysis_rounds`（分析轮次）、`knowledge_citations`（知识引用）、`table_docs` / `column_docs` / `business_glossary`（元数据字典）、`sql_runs`（取数记录）。

> 权威来源：`gateway/db.py` 文件头

### 知识引用（citations）
某次结论引用了哪些知识库的哪些片段。带**文档名 + chunk ID + 坐标**（`position=[[1,0,0,0,0]]`），保证可追溯到原文位置。

> 权威来源：`gateway/knowledge.py`、`knowledge_citations` 表

### 容器名对照
Compose 服务名与容器名不完全一致，容易找错：

| Compose 服务名 | 容器名 |
|---|---|
| `gateway` | `askoda` |
| `tei-embedding` | `askoda-tei-embedding` |

---

## 六、常用命令速查

```bash
PY=/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python
export PATH="$HOME/.orbstack/bin:$PATH"
cd /Users/FYP/Documents/WorkSpace/askoda

$PY tools/gate_all.py --layers G0,G1          # 日常门禁（快，不写库）
$PY tools/gate_all.py --strict                # 全量含真实链路（会写库）
$PY tools/m2_verify.py                       # M2 验收 22 项
$PY tools/r2_check.py                        # R2 脱敏门禁（打真实网关）
$PY tools/redline_guard.py                   # 六条红线判定
$PY tools/coord.py --status                  # 批次状态一览
$PY tools/embedding_failover.sh status       # embedding 主备状态
```

---

## 七、容易混淆的三组概念

**1. 四套编号体系**
`R1–R5` 架构红线 · `R1–R7` 语义规则 · `P1–P9` 证据层级 · `G0–G3` 门禁层。**同名不同义**，引用时务必写全称。

**2. 三个"拒绝"语义不同**
- **阻断（blocking）**：规则表里 `blocking: true`，命中即中止执行
- **提示（warning）**：记录但不中止
- **缺失标记**：槽位填不出时显式标为"无候选结论"，**不用推测值填坑**

**3. 三种"已验证"强度不同**
- **探活 200**：只说明服务在响应
- **判据全过**：说明该批次的验收项都满足
- **独立复核**：助手不看执行方自述，自己重跑一遍

**探活通过 ≠ 功能可用。** 2026-10-06 就出现过"探活 curl 200 但检索恒 0 条"的案例。