# MCP 工具面参考（48 个工具）

> 本文是 [Askoda](../README.md) 的**能力清单与操作手册**。
> 想快速跑起来看[快速开始](../README.md#快速开始)；要部署到机器上看[部署手册](DEPLOYMENT.md)。

48 个工具由网关通过**一个** streamable-http 端点暴露，按业务域分为九组。
**工具名与入参以 `gateway/app.py` 的 `@mcp.tool` 注册处为唯一事实源**，
下表与之逐条一致（自动核验：`python3 tools/mcp_acceptance_check.py`）。
返回字段的完整承诺与边界条件见项目文档工作空间的《MCP 工具契约与注册说明》。

---

## 分组速查

| # | 域 | 数 | 工具 |
|---|---|---|---|
| 1 | 健康与数据集 | 4 | `gateway_health` `datasets` `knowledge_health` `schema_version` |
| 2 | 兜底规划与 Wren 直通 | 5 | `plan` `wren_manifest` `wren_dry_run` `wren_query` `ask` |
| 3 | 需求受理 | 6 | `demand_create` `demand_get` `demand_list` `demand_summarize` `demand_set_status` `demand_similar_precheck` |
| 4 | 知识储备（检索 + 准入） | 7 | `knowledge_search` `knowledge_documents` `knowledge_retire` `knowledge_citation_list` `knowledge_upload` `knowledge_document_status` `knowledge_delete` |
| 5 | 元数据字典 | 3 | `metadata_lookup` `metadata_glossary` `metadata_collect` |
| 6 | 附件 | 3 | `attachment_put` `attachment_list` `attachment_url` |
| 7 | 语义分析与确认闭环 | 7 | `analysis_first_round` `analysis_rounds` `analysis_get` `analysis_evidence` `confirmation_generate` `confirmation_list` `confirmation_answer` |
| 8 | 结构化需求与上下文包 | 5 | `schema_scan` `requirement_structured` `requirement_get` `schema_candidates` `sql_context_pack` |
| 9 | 生成 · 门禁 · 执行 | 8 | `sql_review` `sql_plan` `sql_generate` `sql_execute_readonly` `sql_run_get` `sql_run_list` `sql_run_replay` `sql_optimize` |

合计 **48**。

> ⚠️ **工具面是按位契约**：修改工具的**入参名或顺序**会让已发布的调用方全部失效。
> 新增可以，**改名与改顺序不行**——这是六条红线之一。

---

#### 域 1 · 健康与数据集（4）

| 工具                 | 入参            | 返回要点                                                                                  | 用途                 |
| ------------------ | ------------- | ------------------------------------------------------------------------------------- | ------------------ |
| `gateway_health`   | —             | `{status, service, version, datasets[], components{meta_db, attachments, knowledge}}` | 全局探活；先跑它再谈别的       |
| `datasets`         | —             | `{datasets:[{key, label, 模型数, 字段数, 关系数}]}`                                            | 确认双库在册、选库          |
| `knowledge_health` | —             | `{ok, 可见数据集, 已解析文档数}`                                                                 | 知识底座可用性            |
| `schema_version`   | `dataset="B"` | `{schema_version, 表数, 字段数}`                                                           | 取最近一次 Schema 快照版本号 |

#### 域 2 · 兜底规划与 Wren 直通（5）

| 工具              | 入参                    | 返回要点                                             | 用途                      |
| --------------- | --------------------- | ------------------------------------------------ | ----------------------- |
| `plan`          | ★`nl`, `dataset="B"`  | `{blocked, intent, sql, objects, reason, steps}` | 确定性 NL→SQL（封闭世界，未命中即拒）  |
| `wren_manifest` | `dataset="B"`         | `{models[], relationships[]}`                    | 读 MDL 语义层清单             |
| `wren_dry_run`  | ★`sql`, `dataset="B"` | `{ok, message, plan?}`                           | 语义层闭集预演（门禁 L4）          |
| `wren_query`    | ★`sql`, `dataset="B"` | `{ok, row_count, columns, data, dtypes}`         | 只读执行（先过只读门禁，再过 dry_run） |
| `ask`           | ★`nl`, `dataset="B"`  | `{blocked, …, row_count, result}`                | 一句话问数：规划→门禁→只读执行        |

> `plan` / `ask` **只走确定性规划器**，不做语义分析与确认流转；后者是 `analysis_first_round` 的职责。

#### 域 3 · 需求受理（6）

| 工具                        | 入参                                                                                                                                                      | 返回要点                                                        | 用途   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ---- |
| `demand_create`           | ★`title` ★`business_context` ★`description` ★`expected_output` ★`contact`, `time_range=None`, `expected_finish_at=None`, `attachments=None`, `actor=""` | 需求单（**默认掩码版**）；四道校验不过 → `{ok:false, rejected:true, reason}` | 提交门户 |
| `demand_get`              | ★`demand_id`, `reveal=false`, `actor=""`                                                                                                                | 单据详情 + 流转事件；`reveal=true` 返回原文并写 `reveal_original` 留痕       | 查单   |
| `demand_list`             | `status=None`, `limit=20`, `offset=0`                                                                                                                   | 需求单列表（可按状态过滤）                                               | 看板   |
| `demand_summarize`        | `demand_id=None`                                                                                                                                        | 给单号=单汇总；不给=全局看板                                             | 管理   |
| `demand_set_status`       | ★`demand_id` ★`status`, `note=None`, `actor=""`                                                                                                         | 状态流转，不可覆盖、逐条留痕                                              | 回退闭环 |
| `demand_similar_precheck` | ★`title`, `description=""`, `top=5`, `threshold=0.55`                                                                                                   | 提交前查重：返回相似需求候选与相似度；独立入口、不写库                                 | 提交门户 |

状态取值：`待分析 / 分析中 / 待业务确认 / 待补充修改 / 待审核通过 / 已通过 / 已退回`。

#### 域 4 · 知识储备（7）

<p align="center">

| 工具                          | 入参                                                                                         | 返回要点                                                                                         | 用途        |
| --------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- | --------- |
| `knowledge_search`          | ★`question`, `top_k=5`, `threshold=0.1`, `vector_weight=0.7`, `demand_id=""`, `round_no=0` | 带来源引用：`document_name` / `chunk_id` / `positions`；传 `demand_id` 时把引用登记进 `knowledge_citations` | 取证        |
| `knowledge_documents`       | `limit=50`                                                                                 | 已入库文档（名称 / 解析状态 / 分块数 / 更新时间）                                                                | 知识储备      |
| `knowledge_upload`          | ★`filename`, ★`content_base64`, `content_type=""`, `actor=""`                              | `document_ids` + `parse_started`；**同名会返回 `duplicate=true` 并拒绝入库**                            | 知识准入 · 上传 |
| `knowledge_document_status` | `document_id=""`, `limit=50`                                                               | 逐份文档的 `run`（DONE/RUNNING/FAIL）+ `progress` + `chunk_count`                                   | 解析轮询      |
| `knowledge_delete`          | ★`document_id`, `actor=""`                                                                 | 撤库结果；只删 RAGFlow 文档，不碰需求单与附件                                                                  | 知识准入 · 撤库 |
| `knowledge_retire`          | ★`citation_id`, `reason=""`, `actor=""`                                                    | 把某条知识引用标记为已下架/已回退                                                                            | 引用留痕治理    |
| `knowledge_citation_list`   | `demand_id=""`, `include_retired=False`, `limit=50`                                        | 知识引用留痕记录（按时间倒序）                                                                              | 引用可追溯     |

</p>

> **检索侧两处实测**：`threshold` **必须 >0**（传 0 会被按 falsy 回退到 0.2）；  
> `vector_weight` 默认 0.7（官方 0.3 偏关键词，中文长问句易漏召回）。
>
> **口语化问句易 0 命中**：RAGFlow 对「坪效怎么算」这类疑问句常返回 0 条，  
> 需去掉疑问词改问「坪效 面积」才召回。这是底座特性，不是故障。
>
> **准入侧两处实测**：① 解析是**异步**的，上传后须轮询  
> `knowledge_document_status`到 `run=DONE` 才算可被检索命中；  
> ② **同名文件会被 RAGFlow 静默改名**（`x.md` → `x(1).md`），  
> 同一口径两版并存会让召回自相矛盾，故本工具**在入库前拦下同名**并返回  
> `duplicate=true`，前端据此提供「撤掉库中那份再重传」的一键入口。
>
> **部署前提**：本域依赖 RAGFlow 与 embedding 服务，
> 详见[《部署手册》关于知识库功能](DEPLOYMENT.md#关于知识库功能可选)
> 与[本机 embedding 启动](DEPLOYMENT.md#本机-embedding备用需要时才开)。

#### 域 5 · 元数据字典（3）

| 工具                  | 入参                                                                   | 返回要点                                                     | 用途    |
| ------------------- | -------------------------------------------------------------------- | -------------------------------------------------------- | ----- |
| `metadata_lookup`   | `table=None`, `dataset=None`, `keyword=None`, `include_hidden=false` | 表中文名 / 颗粒度 / 字段清单；`include_hidden=true` 带出未建模列并标"AI 不可见" | 数据字典  |
| `metadata_glossary` | `term=None`, `keyword=None`                                          | 业务口径词表（来源标 `mdl` / `manual`）                             | 数据字典  |
| `metadata_collect`  | `dataset="B"`, `engine="native"`                                     | 采集物理结构入字典 + 一致性自检                                        | 数据源接入 |

#### 域 6 · 附件（3）

| 工具                | 入参                                              | 返回要点                                                      | 用途     |
| ----------------- | ----------------------------------------------- | --------------------------------------------------------- | ------ |
| `attachment_put`  | ★`filename` ★`content_base64`, `demand_id=None` | `{ok, attachment}`；文本类（csv/txt/md/json）上传即敏感扫描，命中给 `risk` | 随单附件   |
| `attachment_list` | `demand_id=None`                                | `{ok, prefix, objects[]}`                                 | 列附件    |
| `attachment_url`  | ★`object_key`, `expires_seconds=3600`           | `{ok, url}` 临时下载链接                                        | 前端直连下载 |

#### 域 7 · 语义分析与确认闭环（7）

| 工具                      | 入参                                                               | 返回要点                                                                                                                                        | 用途    |
| ----------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `analysis_first_round`  | ★`demand_id`, `dataset="B"`, `actor=""`                          | 六槽位 / `evidence_chain` / `rule_check`(R1–R7) / `questions` / `dropped_questions` / `conflicts` / `degraded_sources` / `round_no`。**轮次只增不改** | 跑一轮分析 |
| `analysis_rounds`       | ★`demand_id`                                                     | 全部轮次元信息                                                                                                                                     | 看轮次   |
| `analysis_get`          | ★`demand_id`, `round_no=None`                                    | 某轮完整结果（默认最新）                                                                                                                                | 看结论   |
| `analysis_evidence`     | ★`demand_id`, `round_no=None`, `level=None`                      | 证据链，可按 P1–P9 过滤                                                                                                                             | 看证据   |
| `confirmation_generate` | ★`demand_id`, `dataset="B"`, `actor=""`                          | **只刷新待确认问题**，不新增轮次                                                                                                                          | 重问    |
| `confirmation_list`     | ★`demand_id`, `include_history=false`                            | 确认问答；`include_history=true` 回放全版本                                                                                                           | 看问答   |
| `confirmation_answer`   | ★`demand_id` ★`question_id` ★`answer`, `choice=None`, `actor=""` | 答复；**不覆盖历史**，插 `version+1` 并以 `supersedes` 指旧版                                                                                              | 业务答复  |

> 两类消费者别混发：`slots` 是**分析师视角**（含物理表名/字段名），`questions` 是**业务视角**（已过技术词闸门）。

#### 域 8 · 结构化需求与上下文包（5）

| 工具                       | 入参                            | 返回要点                                                                       | 用途    |
| ------------------------ | ----------------------------- | -------------------------------------------------------------------------- | ----- |
| `schema_scan`            | `dataset="B"`, `persist=true` | 快照 + 稳定版本号 `schema_version`（同库两次采集必须同值）                                    | 数据源接入 |
| `requirement_structured` | ★`demand_id`, `dataset="B"`   | 结构化技术需求对象 + `contract_ok` / `contract_errors`；版本化只增不改                      | 合成需求  |
| `requirement_get`        | ★`demand_id`, `version=None`  | 取某一版结构化需求（默认最新）                                                            | 取需求   |
| `schema_candidates`      | ★`demand_id`, `dataset="B"`   | 主题表 / 关联路径 / 时间字段候选；只在 MDL 闭集内产生，未命中给 `miss_reason`                        | 生成准备  |
| `sql_context_pack`       | ★`demand_id`, `dataset="B"`   | SQL 上下文包 + 溯源三件套 `schema_version` / `requirement_version` / `pack_version` | 生成准备  |

#### 域 9 · 生成 · 门禁 · 执行（8）

| 工具                     | 入参                                                  | 返回要点                                                                       | 用途      |
| ---------------------- | --------------------------------------------------- | -------------------------------------------------------------------------- | ------- |
| `sql_review`           | ★`sql`, `dataset="B"`                               | 五层门禁审查 `{status, layers[]}`：L1 语法 / L2 静态规则（分级阻断）/ L3 只读 / L4 语义 / L5 结果断言 | 门禁      |
| `sql_plan`             | ★`demand_id`, `dataset="B"`                         | 计划草稿（不含 SQL 正文）；选不出唯一解给候选集合并标「需人工审核」                                       | 生成第 1 段 |
| `sql_generate`         | ★`demand_id`, `dataset="B"`, `candidate_sql=None`   | SQL 初稿；`candidate_sql` 为空则回落确定性规划器；多条且差异过大时 `hold`                         | 生成第 2 段 |
| `sql_execute_readonly` | ★`demand_id`, `dataset="B"`, `sql=None`, `actor=""` | 先过五层门禁，全过才执行；每次写一条 `sql_runs`（溯源齐全，可回放）                                    | 执行      |
| `sql_optimize`         | ★`sql`, `dataset="B"`                               | SQL 静态等价重写（谓词下推 / 冗余 DISTINCT 移除 / CSE 保守跳过）；不能证明等价就不改                     | 优化      |
| `sql_run_get`          | ★`demand_id`                                        | 该需求全部运行记录（生成 SQL / 门禁结果 / 结果校验 / 溯源版本号）                                    | 联调交付    |
| `sql_run_list`         | `demand_id=""`, `dataset=""`, `limit=20`            | 跨需求执行留痕列表（时间倒序，可筛选）                                                        | 审计      |
| `sql_run_replay`       | ★`demand_id`, `version=0`                           | 按版本精确复原当时的输入 → pack_version → SQL → 审查 → 结果                                | 审计回放    |

> **L2 分级阻断**：4 条硬错误规则命中即**阻断** —— `JOIN_WITHOUT_CONDITION` / `ONE_TO_MANY_UNHANDLED` / `UNMAPPED_OBJECT_REF` / `ENUM_VALUE_INVALID`；其余 7 条只记警告、不阻断。
>
> **两套规则别混**：上述 L2 是 **SQL 静态规则**（`gateway/rules.py`，11 条，看 SQL AST）；而域 7 `analysis_first_round` 的 `rule_check`（R1–R7）是**需求语义规则**（`gateway/semantics.py`，看需求文本）。二者分层、独立演进、互不覆盖。

#### 典型操作动线（八步链路）

```
demand_create
 → analysis_first_round         # 六槽位 + 证据链 + R1–R7
   → confirmation_list          # 有没有要问业务的
     → confirmation_answer      # 业务答复（可多轮）
   → requirement_structured     # 合成结构化需求 + 契约校验
     → sql_context_pack         # 组装上下文包（带溯源三件套）
       → schema_candidates      # 主题表 / 关联 / 时间字段候选
       → sql_plan               # 计划草稿（无 SQL）
       → sql_generate           # SQL 初稿（candidate_sql 为空则兜底）
         → sql_review           # 五层门禁（可能被 L2/L3/L4 拦）
           → sql_execute_readonly   # 全过才执行 + 留痕
             → sql_run_get / sql_run_replay
```

任一步都可插 `knowledge_search(question, demand_id=…)` 把引用登记进 `knowledge_citations`。

#### 最小调用示例（终端直接验）

```bash
# 1) 探活：status=ok，双库都 ok
curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool

# 2) 握手 + 取工具面（应返回 48 个工具）
SID=$(curl -sD- -o/dev/null -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}' \
  | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}' | tr -d '\r')
curl -s -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' -o /dev/null
curl -s -X POST http://127.0.0.1:18080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H "mcp-session-id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -o '"name":"[a-z_]*"' | sort
```

> Agent 客户端（豆包 / WorkBuddy / Codex 等）不必手搓 HTTP：把 `http://127.0.0.1:18080/mcp` 注册为 MCP 服务后，直接按工具名调用即可。

---

### 5.10 MCP 操作指引与版本升级对照

<a id="mcp-ops"></a>

#### 5.10.1 接入前后自查（3 步）

1. `curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool` → `status` 应为 `ok`（知识库单独展示，不可用不拖垮整体，会如实进 `degraded`）；
2. 客户端 `tools/list` → 应为 **48 个**；
3. 冒烟：调 `datasets` 看双库（A / B）是否都在册。

#### 5.10.2 常用运维动作

```bash
docker compose ps                 # 看状态（wren-mcp-a/b、assistant-minio 无 healthcheck，只显示 Up 是正常的，见 5.2）
docker compose up -d --build      # 改过 gateway/ 后重建
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # 双探活（HTTP + MCP，含真调一次 gateway_health）
python3 tools/mcp_acceptance_check.py                                 # 工具面 48 个逐条核对
```

> ⚠️ 容器内代码平铺 `/app`，但 `tools/` 不在 build context —— 跑容器内脚本前需 `docker cp`；走 HTTP 的脚本**必须在宿主跑**，容器内会 connection refused。

#### 5.10.3 版本标识对照（升级时最易搞混的一张表）

| 版本标识                              | 出现在哪                            | 含义                                                         | 何时变                  |
| --------------------------------- | ------------------------------- | ---------------------------------------------------------- | -------------------- |
| `GATEWAY_VERSION`（当前 `0.3.0`）     | `gateway_health.version` / 启动日志 | **网关产品版本**                                                 | 每期里程碑                |
| `serverInfo.version`（当前 `4.0.10`） | MCP `initialize` 握手             | **FastMCP 库版本**（`FastMCP(name)` 未显式传版本，取库默认）               | 升级 `fastmcp` 依赖      |
| `rules_version`                   | `sql_review` / `sql_runs`       | L2 规则集内容哈希                                                 | 改 `gateway/rules.py` |
| `schema_version`                  | `schema_scan` / 上下文包            | 该库「表.列:类型」排序清单 sha256 前 8 位                                | 库结构变化                |
| `requirement_version`             | `requirement_*`                 | 结构化需求版本（只增不改）                                              | 每次合成                 |
| `pack_version`                    | `sql_context_pack`              | sha256(schema_version + requirement_version + 规则集版本) 前 8 位 | 上述任一变化               |
| 镜像标签 `askoda-gateway:0.3.0`       | `docker-compose.yml`            | **与 `GATEWAY_VERSION` 保持一致**                               | 每期里程碑                |

> 关键提醒：**MCP 握手报的 `4.0.10` 是 FastMCP 库版本，不是网关版本**；对外讲版本请以 `gateway_health.version`（当前 `0.3.0`）为准。

#### 5.10.4 升级操作清单（工具面变更必做）

改 `gateway/app.py`（增删 `@mcp.tool`）后，按序执行：

1. **同批更新文档**：项目文档工作空间《MCP 工具契约与注册说明》的逐工具契约与域计数 → 本文件的域计数表（分组速查）与逐域工具表；
2. **重建镜像**：`BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway && docker compose up -d`；
3. **核验工具面**：`tools/list` 的数量与清单须与文档一致（`python3 tools/mcp_acceptance_check.py`）；
4. **跑回归**：`python3 tools/gate_all.py`（G0–G3 四层门禁），确认基线不退化；
5. **同步镜像标签**与 `.env`（如涉及新环境变量）。

> 联动红点（改工具面会牵动这些断言 / 清单，出改单时要一并扫）：`tools/mcp_acceptance_check.py` 的**计数与清单断言**、契约文档的域计数表、本文件的域计数表。

---
