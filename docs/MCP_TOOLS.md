# MCP 工具面参考（65 个工具）

> 本文是 [Askoda](../README.md) 的**能力清单与操作手册**。
> 想快速跑起来看[快速开始](../README.md#快速开始)；要部署到机器上看[部署手册](DEPLOYMENT.md)。

65 个工具由网关通过**一个** streamable-http 端点暴露，按业务域分为十一组。
**工具名与入参以 `gateway/app.py` 的 `@mcp.tool` 注册处为唯一事实源**，
下表与之逐条一致（自动核验：`python3 tools/mcp_acceptance_check.py`）。
返回字段的完整承诺与边界条件见项目文档工作空间的《MCP 工具契约与注册说明》。

### 返回约定（读之前先看这条，否则会误判失败）

| 情形 | 返回形态 | 怎么判 |
|---|---|---|
| 成功 | **资源对象本身**（如需求单、确认记录），**不强制带 `ok`** | 看有没有 `ok:false`；不要用 `ok is True` 判成功 |
| 业务拒绝（可纠正） | `{ok:false, error, ...}`，可能还带 `hint` / `guide` | `ok is False` |
| 异常 | `{ok:false, error:"<类名>: <原因>"}` | 同上 |

⚠️ 这个「成功不带 ok」是既有契约，不是遗漏：`demand_create` / `demand_set_status`
这类工具直接回资源对象，BFF 也按 `ok is false` 判拒绝。
**用 `if resp.get("ok")` 判成功的客户端会把成功当失败**——已登记在
`docs/OPEN_ITEMS.md` 第 17 条，若要做成对外 SDK，届时统一口径。

---

## 分组速查

| # | 域 | 数 | 工具 |
|---|---|---|---|
| 1 | 健康与数据集 | 8 | `gateway_health` `datasets` `dataset_register` `dataset_probe` `dataset_registry` `dataset_delete` `knowledge_health` `schema_version` |
| 2 | 兜底规划与 Wren 直通 | 5 | `plan` `wren_manifest` `wren_dry_run` `wren_query` `ask` |
| 3 | 需求受理 | 10 | `demand_create` `demand_get` `demand_list` `demand_summarize` `demand_set_status` `demand_clarify` `demand_return` `demand_supplement` `demand_purge` `demand_similar_precheck` |
| 4 | 知识储备（检索 + 准入） | 10 | `knowledge_search` `knowledge_documents` `knowledge_retire` `knowledge_citation_list` `knowledge_upload` `knowledge_document_status` `knowledge_delete` `knowledge_list_kbs` `knowledge_bind` `knowledge_settings` |
| 5 | 元数据字典 | 3 | `metadata_lookup` `metadata_glossary` `metadata_collect` |
| 6 | 附件 | 3 | `attachment_put` `attachment_list` `attachment_url` |
| 7 | 语义分析与确认闭环 | 7 | `analysis_first_round` `analysis_rounds` `analysis_get` `analysis_evidence` `confirmation_generate` `confirmation_list` `confirmation_answer` |
| 8 | 结构化需求与上下文包 | 5 | `schema_scan` `requirement_structured` `requirement_get` `schema_candidates` `sql_context_pack` |
| 9 | 生成 · 门禁 · 执行 | 8 | `sql_review` `sql_plan` `sql_generate` `sql_execute_readonly` `sql_run_get` `sql_run_list` `sql_run_replay` `sql_optimize` |
| 10 | 系统配置（大模型连接） | 3 | `llm_config_get` `llm_config_set` `llm_config_test` |
| 11 | 口径维护 | 3 | `metric_list` `metric_save` `metric_preview` |

合计 **65**（2026-11-01 用 `tools/list` 实测；M10/M11 新增的 14 个此前未进本表，已补齐）。

> ⚠️ **工具面是按位契约**：修改工具的**入参名或顺序**会让已发布的调用方全部失效。
> 新增可以，**改名与改顺序不行**——这是六条红线之一。

---

#### 域 1 · 健康与数据集（8）

| 工具                 | 入参            | 返回要点                                                                                  | 用途                 |
| ------------------ | ------------- | ------------------------------------------------------------------------------------- | ------------------ |
| `gateway_health`   | —             | `{status, service, version, datasets[], components{meta_db, attachments, knowledge}}` | 全局探活；先跑它再谈别的       |
| `datasets`         | —             | `{datasets:[{key, label, 模型数, 字段数, 关系数}]}`                                            | 确认双库在册、选库          |
| `dataset_register` | ★`key`, `mdl_json`/`mdl_path`, `engine`, `label`, `domain`, `enabled` | `{ok, dataset}`；MDL 缺 models 直接拒 | 登记新库（MDL 入库，不落文件） |
| `dataset_probe`    | ★`key`, `engine`, `dbname`, `mdl_json?` | `{ok, checks{连接/引擎/MDL 三判据分开报}}` | 登记前自检 |
| `dataset_registry` | — | `{items[], 每个含 key/label/models/fields}` | 已登记清单 |
| `dataset_delete`   | ★`key`, `actor` | `{ok}`；**只取消登记，不删业务库** | 下线一个库 |
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

#### 域 3 · 需求受理（10）

| 工具                        | 入参                                                                                                                                                      | 返回要点                                                        | 用途   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- | ---- |
| `demand_create`           | ★`title` ★`business_context` ★`description` ★`expected_output` ★`contact`, `time_range=None`, `expected_finish_at=None`, `attachments=None`, `actor=""` | 需求单（**默认掩码版**）；四道校验不过 → `{ok:false, rejected:true, reason}` | 提交门户 |
| `demand_get`              | ★`demand_id`, `reveal=false`, `actor=""`                                                                                                                | 单据详情 + 流转事件；`reveal=true` 返回原文并写 `reveal_original` 留痕       | 查单   |
| `demand_list`             | `status=None`, `limit=20`, `offset=0`                                                                                                                   | 需求单列表（可按状态过滤）                                               | 看板   |
| `demand_summarize`        | `demand_id=None`                                                                                                                                        | 给单号=单汇总；不给=全局看板                                             | 管理   |
| `demand_set_status`       | ★`demand_id` ★`status`, `note=None`, `actor=""`                                                                                                         | 状态流转，不可覆盖、逐条留痕                                              | 回退闭环 |
| `demand_clarify`          | ★`demand_id`, `actor=""`                                                                                                                                | 生成待确认问题清单（五槽位缺口）                                            | 读懂需求 |
| `demand_return`           | ★`demand_id` ★`reason`, `actor=""`                                                                                                                      | 退回业务方，原因留痕                                                      | 退回   |
| `demand_supplement`       | ★`demand_id` ★`content`, `actor=""`                                                                                                                     | 补录业务说明，写事件流                                                     | 补料   |
| `demand_purge`            | `demand_ids=[]`, `dry_run=true`, `actor=""`                                                                                                             | **先 dry-run 摊开各子表行数**，确认后才真删；默认不删                            | 清理测试数据 |
| `demand_similar_precheck` | ★`title`, `description=""`, `top=5`, `threshold=0.55`                                                                                                   | 提交前查重：返回相似需求候选与相似度；独立入口、不写库                                 | 提交门户 |

状态取值：`待分析 / 分析中 / 待业务确认 / 待补充修改 / 待审核通过 / 已通过 / 已退回`。

#### 域 4 · 知识储备（10）

<p align="center">

| 工具                          | 入参                                                                                         | 返回要点                                                                                         | 用途        |
| --------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------- | --------- |
| `knowledge_search`          | ★`question`, `top_k=5`, `threshold=0.1`, `vector_weight=0.7`, `demand_id=""`, `round_no=0` | 带来源引用：`document_name` / `chunk_id` / `positions`；传 `demand_id` 时把引用登记进 `knowledge_citations` | 取证        |
| `knowledge_documents`       | `limit=50`                                                                                 | 已入库文档（名称 / 解析状态 / 分块数 / 更新时间）                                                                | 知识储备      |
| `knowledge_upload`          | ★`filename`, ★`content_base64`, `content_type=""`, `actor=""`                              | `document_ids` + `parse_started` + `dedup_checked`；**同名返回 `duplicate=true`**；表格含超长单元格返回 `oversized=true`（附 `cells` 位置表）；`note` 汇总解析/防重/预检提示 | 知识准入 · 上传 |
| `knowledge_document_status` | `document_id=""`, `limit=50`                                                               | 逐份文档的 `run`（DONE/RUNNING/FAIL）+ `progress` + `chunk_count`                                   | 解析轮询      |
| `knowledge_delete`          | ★`document_id`, `actor=""`                                                                 | 撤库结果；只删 RAGFlow 文档，不碰需求单与附件                                                                  | 知识准入 · 撤库 |
| `knowledge_retire`          | ★`citation_id`, `reason=""`, `actor=""`                                                    | 把某条知识引用标记为已下架/已回退                                                                            | 引用留痕治理    |
| `knowledge_citation_list`   | `demand_id=""`, `include_retired=False`, `limit=50`                                        | 知识引用留痕记录（按时间倒序）                                                                              | 引用可追溯     |
| `knowledge_list_kbs`        | —                                                                                          | 可见知识库清单（名称 / 文档数 / 能力位）                                                                    | 绑定前先看有什么 |
| `knowledge_bind`            | ★`kb_ids=[]`, `actor=""`                                                                   | 绑定生效知识库；**三层作用域：底座可见 > 界面 > 单次显式**                                                          | 切换知识底座    |
| `knowledge_settings`        | —                                                                                          | 当前绑定、候选库、生效来源（`db`/`env`）                                                                  | 设置页读取     |

</p>

> **检索侧两处实测**：`threshold` **必须 >0**（传 0 会被按 falsy 回退到 0.2）；  
> `vector_weight` 默认 0.7（官方 0.3 偏关键词，中文长问句易漏召回）。
>
> **口语化问句易 0 命中**：RAGFlow 对「坪效怎么算」这类疑问句常返回 0 条，  
> 需去掉疑问词改问「坪效 面积」才召回。这是底座特性，不是故障。
>
> **准入侧三处实测**：① 解析是**异步**的，上传后须轮询  
> `knowledge_document_status`到 `run=DONE` 才算可被检索命中；  
> ② **同名文件会被 RAGFlow 静默改名**（`x.md` → `x(1).md`），  
> 同一口径两版并存会让召回自相矛盾，故本工具**在入库前拦下同名**并返回  
> `duplicate=true`，前端据此提供「撤掉库中那份再重传」的一键入口；  
> ③ **表格里超过 2048 字符的单元格会让 embedding 服务静默卡死**  
> （进程不退出、健康检查仍绿，但不再出量），整份文档最终解析失败且看不到原因。  
> 故 xlsx / csv 在入库前先扫一遍，命中即返回 `oversized=true` 并附 `cells` 位置表  
> （`表名!坐标` + 字符数），用户可直接定位去拆。`.xls` 是旧二进制格式扫不了，  
> 放行但在 `note` 里提示「建议另存为 .xlsx」。
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
| `attachment_url`  | ★`object_key`, `expires_seconds=3600`           | `{ok, url}` 临时下载链接；**url 的 host 是对象存储内网地址** | **仅限服务端**使用（浏览器请走 BFF 代理） |

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
| `sql_review`           | ★`sql`, `dataset="B"`                               | 门禁审查 `{status, layers[]}`：L-REQ 需求要素覆盖判定 / L1 语法 / L2 静态规则（分级阻断）/ L3 只读 / L4 语义 / L5 结果断言 | 门禁      |
| `sql_plan`             | ★`demand_id`, `dataset="B"`                         | 计划草稿（不含 SQL 正文）；选不出唯一解给候选集合并标「需人工审核」                                       | 生成第 1 段 |
| `sql_generate`         | ★`demand_id`, `dataset="B"`, `candidate_sql=None`   | SQL 初稿；`candidate_sql` 为空则回落确定性规划器；多条且差异过大时 `hold`                         | 生成第 2 段 |
| `sql_execute_readonly` | ★`demand_id`, `dataset="B"`, `sql=None`, `actor=""` | 先过门禁，全过才执行；每次写一条 `sql_runs`（溯源齐全，可回放）                                    | 执行      |
| `sql_optimize`         | ★`sql`, `dataset="B"`                               | SQL 静态等价重写（谓词下推 / 冗余 DISTINCT 移除 / CSE 保守跳过）；不能证明等价就不改                     | 优化      |
| `sql_run_get`          | ★`demand_id`                                        | 该需求全部运行记录（生成 SQL / 门禁结果 / 结果校验 / 溯源版本号）                                    | 联调交付    |
| `sql_run_list`         | `demand_id=""`, `dataset=""`, `limit=20`            | 跨需求执行留痕列表（时间倒序，可筛选）                                                        | 审计      |
| `sql_run_replay`       | ★`demand_id`, `version=0`                           | 按版本精确复原当时的输入 → pack_version → SQL → 审查 → 结果                                | 审计回放    |

> **L2 分级阻断**：4 条硬错误规则命中即**阻断** —— `JOIN_WITHOUT_CONDITION` / `ONE_TO_MANY_UNHANDLED` / `UNMAPPED_OBJECT_REF` / `ENUM_VALUE_INVALID`；其余 7 条只记警告、不阻断。
>
> **两套规则别混**：上述 L2 是 **SQL 静态规则**（`gateway/rules.py`，11 条，看 SQL AST）；而域 7 `analysis_first_round` 的 `rule_check`（R1–R7）是**需求语义规则**（`gateway/semantics.py`，看需求文本）。二者分层、独立演进、互不覆盖。

#### 域 10 · 系统配置 · 大模型连接（3）

| 工具                | 入参                                                                                                                                                                          | 返回要点                                                                                                       | 用途     |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------ |
| `llm_config_get`  | —                                                                                                                                                                           | `{source, effective, missing[], has_credential, credential_masked, providers[], recent_changes[]}`；**不回明文密钥** | 读配置与生效状态 |
| `llm_config_set`  | `provider=""`, `base_url=""`, `model=""`, `api_key=""`, `enabled=true`, `temperature=0`, `timeout=60`, `max_repair=1`, `clear_credential=false`, `reset=false`, `actor=""` | 部分更新；`api_key` 留空=不改，`clear_credential`=清空，`reset`=退回环境变量；校验不过返回 `{ok:false, errors[]}` 且**不落库**          | 保存并即时生效 |
| `llm_config_test` | `provider=""`, `base_url=""`, `model=""`, `api_key=""`, `timeout=0`（0=跟随配置）                                                                                                        | `{ok, latency_ms, notice, probe{reachable, model_count, model_listed}, used_saved_credential}`；**先探活再生成**，失败时可区分「地址不通」与「模型太慢」                 | 先测后存    |

> **配置优先级**：元数据库 `llm_settings` 有行 ⇒ 整行生效（界面保存的）；没有行 ⇒ `LLM_*` 环境变量。
> **不在字段级混合**——一半来自库、一半来自环境变量的组合没人能解释清楚。
> 未配置（或开关关闭）时 `sql_generate` 回落确定性规划器，行为与引入大模型之前**逐字一致**。
>
> **密钥只进不出**：三个工具的任何出参都不含明文密钥（只给 `credential_masked` 尾号）；
> 变更留痕写 `config_events`，只记「是否换过凭据」，不记密钥本身。
>
> **测试连接为什么分两段**：推理模型（如 `deepseek-r1:7b`）答一句「可用」也要 20–30 秒，
> 若只发一次生成请求，超时会把「模型慢」误报成「地址不通」（2026-11-01 实测踩过）。
> 故先 `GET {base}/models` 秒级确认地址/密钥/模型名，再发一次真实生成。

#### 域 11 · 口径维护（3）

| 工具               | 入参                                        | 返回要点                                                                 | 用途       |
| ---------------- | ----------------------------------------- | -------------------------------------------------------------------- | -------- |
| `metric_list`    | `dataset="C"`                             | `{version, views[{name, columns, comment, statement}], metrics[], models}` | 读当前生效口径 |
| `metric_save`    | ★`dataset`, `metrics{views,metrics}`, `dry_run=false`, `actor` | 校验 → **热部署引擎** → 落库；返回 `{ok, version, added[], removed[], persist_warning}`；校验不过返回 `problems[]` 逐条原因 | 保存并生效    |
| `metric_preview` | `dataset="C"`, ★`question`, `limit=30`     | `{metric, metric_label, sql, row_count, columns, rows}`；不建需求单、只读执行 | 改完试一句    |

> **口径的真源是库**（`dataset_registry.mdl_json` 的 `x_askoda_metrics`），不是仓库里的文件；
> `db/credit/04_metrics.json` 只是初始种子（重建环境时导入）。
> 为什么：网关规划器读的就是库，文件改了不跑脚本就**静默不生效**。
>
> **保存顺序不可反**：先部署引擎、成功后才落库。反过来会出现「库里是新口径、引擎还是旧的」，
> 页面显示改了、跑出来还是旧数——最难查的一种状态。
>
> ⚠️ **引擎启动读工作区 `mdl.json`**：热部署只改进程内存，所以保存后还要写回
> `/app/engine-workspace-c`（compose 里的 bind mount）。写不回去时保存照常成功，
> 但 `persist_warning` 会说明「引擎重启后会回滚」——这条必须显示给用户。

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
         → sql_review           # 门禁（可能被 L2/L3/L4 拦）
           → sql_execute_readonly   # 全过才执行 + 留痕
             → sql_run_get / sql_run_replay
```

任一步都可插 `knowledge_search(question, demand_id=…)` 把引用登记进 `knowledge_citations`。

#### 最小调用示例（终端直接验）

```bash
# 1) 探活：status=ok，双库都 ok
curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool

# 2) 握手 + 取工具面（应返回 65 个工具）
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
2. 客户端 `tools/list` → 应为 **65 个**；
3. 冒烟：调 `datasets` 看双库（A / B）是否都在册。

#### 5.10.2 常用运维动作

```bash
docker compose ps                 # 看状态（wren-mcp-a/b、assistant-minio 无 healthcheck，只显示 Up 是正常的，见 5.2）
docker compose up -d --build      # 改过 gateway/ 后重建
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # 双探活（HTTP + MCP，含真调一次 gateway_health）
python3 tools/mcp_acceptance_check.py                                 # 工具面 65 个逐条核对
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
