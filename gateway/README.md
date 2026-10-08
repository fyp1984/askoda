# 服务端网关（MCP 工具面 48 个 · 双库语义层 · 五层门禁 · 只读执行 · 留痕回放）

数据需求智能分析助手的服务端入口：对 Agent 暴露 MCP 工具，对内接入 Wren 语义层双库、
自有元数据库与附件对象存储，对下接入 RAGFlow 知识底座。

> **网关侧不内置 LLM。** 语义分析以「证据编排 + 确定性规则」落地，结论一律带来源与置信度；
> 需要自由生成的部分由**客户端 Agent** 承担。因此 M3 的验收与回放可在任意 Agent
> （豆包 / Codex / WorkBuddy）上跑，不存在"必须先选定某个模型"这类前置条件。

## 一、一键起停

在**仓库根目录**（`askoda/`）执行：

```bash
docker compose up -d          # 起停全部：gateway + 元数据库 + 附件存储 + 双库（7 服务）
docker compose ps             # 查看状态
docker compose down           # 停栈（保留数据卷）
docker compose down && docker compose up -d      # 可复现性验证
```

首次 `up` 或改过 `gateway/` 代码后，加 `--build`：

```bash
docker compose up -d --build
```

> ⚠️ 本机 OrbStack 环境下，`compose build` 需要把 buildx 的配置目录重定位到工作区内（沙箱不允许写 `~/.docker/buildx`）：
> ```bash
> BUILDX_CONFIG="$PWD/.buildx" /Users/FYP/.orbstack/bin/docker compose build gateway
> ```

### 宿主机运维工具（元数据采集 CLI）

`tools/collect_metadata.py` 跑在宿主机（不是容器内）：因为 `schemacrawler` 后端需要宿主机
的 `docker` 命令，而 `native` 后端需要 `psycopg`——两者都不在网关容器里齐备。
它使用隔离虚拟环境，用法见该脚本头部注释。

## 二、接入面

| 项 | 地址 | 说明 |
|---|---|---|
| MCP 端点 | `http://127.0.0.1:18080/mcp` | streamable-http，Agent 接这里 |
| 健康检查 | `http://127.0.0.1:18080/healthz` | HTTP 200 + 各数据集与组件连通性 |
| 模拟库 A | `127.0.0.1:9000`（MCP）/ `15432`（PG） | 电商域，6 表 |
| 模拟库 B | `127.0.0.1:9002`（MCP）/ `15433`（PG） | 零售会员域，8 表 |
| 网关元数据库 | `127.0.0.1:15434`（PG） | 需求单 / 事件 / 元数据字典（网关自有） |
| 附件对象存储 | `127.0.0.1:19000`（S3）/ `19001`（控制台） | MinIO，桶 `askoda-attachments` |
| 知识底座 | `http://127.0.0.1:19380/api/v1` | RAGFlow 检索接口（网关只做客户端） |

宿主机端口用 `18080` 而非 `8080`：`8080` 已被本项目 `demo-server.py` 长期占用。

## 三、MCP 工具面（48 个 · 九域）

> 按落地批次分列：**M1–M3 第一批（28 个）** + **M4–M6 第二批（13 个）** + **后续补齐（7 个）**，合计 48 个。
> 逐条入参默认值与返回要点，以 [MCP 工具参考](../docs/MCP_TOOLS.md) 与项目文档《MCP 工具契约与注册说明》为事实源（`app.py` 增删工具须同批更新）。

### M1 · 语义层与问数

| 工具 | 入参 | 返回 | 说明 |
|---|---|---|---|
| `gateway_health` | — | `{status, service, version, datasets[], components{}}` | 网关自检 + 数据集与组件连通性 |
| `datasets` | — | `{datasets[]}` | 列出已注册数据集与语义层规模 |
| `plan` | `nl`, `dataset=B` | `{blocked, intent, sql, objects, reason, steps}` | 确定性 NL→SQL 兜底（封闭世界，未命中即拒） |
| `wren_manifest` | `dataset=B` | `{models[], relationships[]}` | 读取 MDL 语义层清单 |
| `wren_dry_run` | `sql`, `dataset=B` | `{ok, message}` | 语义层门禁预演 |
| `wren_query` | `sql`, `dataset=B` | `{ok, row_count, columns, data, dtypes}` | 只读执行（先过只读门禁，再过 dry_run） |
| `ask` | `nl`, `dataset=B` | `{blocked, intent, sql, dry_run, result,...}` | 一句话问数：规划 → 门禁 → 只读执行 |

### M2 · 需求受理

| 工具 | 入参 | 说明 |
|---|---|---|
| `demand_create` | `payload` | 提交需求：四类校验 → 敏感扫描 → 掩码落库 → 进入 `待分析` |
| `demand_get` | `demand_id`, `reveal=False` | 默认返回**掩码版**；`reveal=True` 取原文并写 `reveal_original` 留痕 |
| `demand_list` | `status=None`, `limit` | 需求列表 |
| `demand_set_status` | `demand_id`, `status` | 流转状态（7 态），每次变更写事件 |
| `demand_summarize` | `demand_id=None` | 看板汇总：状态分布 + 敏感命中统计 |

### M2 · 知识检索与元数据

| 工具 | 入参 | 说明 |
|---|---|---|
| `knowledge_search` | `question`, `top_k=5`, `threshold=0.1`, `vector_weight=0.7` | 引用式检索，返回带文档名 + chunk_id + 坐标的引用；**零命中自动去疑问词重试** |
| `knowledge_health` | — | 知识库连通性 + 数据集 + 文档数 |
| `knowledge_documents` | `limit` | 数据集内文档清单与解析状态 |
| `metadata_lookup` | `table`, `dataset`, `keyword`, `include_hidden=False` | 按表/关键词查中文名、颗粒度、口径；默认**排除未建模列** |
| `metadata_glossary` | — | 业务口径词表 |
| `metadata_collect` | `dataset="B"`, `engine="native"` | 触发元数据采集（`native` / `schemacrawler`） |

### M2 · 附件

| 工具 | 入参 | 说明 |
|---|---|---|
| `attachment_put` | `filename`, `content_base64`, `demand_id=None` | 上传附件；文本类附件自动扫敏感信息并给风险等级 |
| `attachment_list` | `demand_id` | 列出随单附件 |
| `attachment_url` | `object_key` | 生成临时下载链接 |

### M3 · 语义分析与确认闭环

| 工具 | 入参 | 说明 |
|---|---|---|
| `analysis_first_round` | `demand_id`, `dataset="B"` | 取证据（P1–P9）→ 六槽位结论 → 规则校验 → 生成待确认问题 → 落库。**轮次只增不改** |
| `analysis_rounds` | `demand_id` | 列出全部轮次：轮次号、证据数、问题数、主体、阻断项数 |
| `analysis_get` | `demand_id`, `round_no=None` | 取某一轮完整结果（默认最新一轮） |
| `analysis_evidence` | `demand_id`, `round_no`, `level` | 取证据链，可按优先级过滤（P1–P9） |
| `confirmation_generate` | `demand_id`, `dataset="B"` | **只刷新待确认问题**，不新增轮次（知识库/元数据更新后重问用） |
| `confirmation_list` | `demand_id`, `include_history=False` | 列确认问答；`include_history=True` 连历史版本一起给 |
| `confirmation_answer` | `demand_id`, `question_id`, `answer`, `choice` | 答复问题。**不覆盖历史**：`version+1` + `supersedes` 指向旧版 |

### M4 · Schema 快照与结构化需求（6）

| 工具 | 入参 | 说明 |
|---|---|---|
| `schema_version` | `dataset="B"` | 取最近一次 Schema 快照版本号 |
| `schema_scan` | `dataset="B"`, `persist=true` | 采集 Schema 快照 + 稳定版本号（同库两次采集必须同值） |
| `requirement_structured` | `demand_id`, `dataset="B"` | 合成结构化技术需求对象 + 契约校验（版本只增不改） |
| `requirement_get` | `demand_id`, `version=None` | 取某一版结构化需求（默认最新） |
| `schema_candidates` | `demand_id`, `dataset="B"` | 主题表 / 关联路径 / 时间字段候选（仅在 MDL 闭集内产生） |
| `sql_context_pack` | `demand_id`, `dataset="B"` | SQL 上下文包 + 溯源三件套（schema / requirement / pack） |

### M5 · 生成 · 门禁 · 执行（4）

| 工具 | 入参 | 说明 |
|---|---|---|
| `sql_review` | `sql`, `dataset="B"` | 五层门禁审查（L1 语法 / L2 静态规则·分级阻断 / L3 只读 / L4 语义 dry-plan / L5 结果断言） |
| `sql_plan` | `demand_id`, `dataset="B"` | 计划草稿（不含 SQL 正文）；选不出唯一解给候选集合并标「需人工审核」 |
| `sql_generate` | `demand_id`, `dataset="B"`, `candidate_sql=None` | SQL 初稿；`candidate_sql` 为空则回退确定性规划器 |
| `sql_execute_readonly` | `demand_id`, `dataset="B"`, `sql=None`, `actor=""` | 全过五层门禁才执行，每次写一条 `sql_runs`（溯源齐全，可回放） |

### M6 · 留痕 · 回放（3）

| 工具 | 入参 | 说明 |
|---|---|---|
| `sql_run_get` | `demand_id` | 该需求全部运行记录（生成 SQL / 门禁结果 / 结果校验 / 溯源版本号） |
| `sql_run_list` | `demand_id=""`, `dataset=""`, `limit=20` | 跨需求执行留痕列表（时间倒序，可筛选） |
| `sql_run_replay` | `demand_id`, `version=0` | 按版本精确复原「输入 → pack_version → SQL → 审查 → 结果」 |

### 后续补齐 · 知识治理与工具完善（7）

| 工具 | 入参 | 说明 |
|---|---|---|
| `demand_similar_precheck` | `title`, `description=""`, `top=5`, `threshold=0.55` | 提交前查重：返回相似需求候选与相似度；独立入口，**不写库** |
| `knowledge_retire` | `citation_id`, `reason=""`, `actor=""` | 把某条知识引用标记为已下架 / 已回退（引用留痕治理） |
| `knowledge_citation_list` | `demand_id=""`, `include_retired=False`, `limit=50` | 知识引用留痕记录（按时间倒序，可筛选） |
| `knowledge_upload` | `filename`, `content_base64`, `content_type=""`, `actor=""` | 知识准入：上传；**同名文件返回 `duplicate=true` 并拒绝入库** |
| `knowledge_document_status` | `document_id=""`, `limit=50` | 知识准入：解析轮询（`run` DONE/RUNNING/FAIL + `progress` + `chunk_count`） |
| `knowledge_delete` | `document_id`, `actor=""` | 知识准入：撤库（只删知识底座文档，**不碰需求单与附件**） |
| `sql_optimize` | `sql`, `dataset="B"` | SQL 静态等价重写（谓词下推 / 冗余 DISTINCT 移除 / CSE 保守跳过）；**不能证明等价就不改** |

#### 两类消费者的边界（勿混发）

`analysis_first_round` 的返回里有两组面向不同人的内容：

- `slots` 是**分析师视角**——会出现物理表名、字段名、主键（如"一行 = 一条 `ads_member_repurchase_di` 记录"），
  目的是让分析师核对口径，**不要直接转给业务**；
- `questions` 是**业务视角**——已过技术词闸门，只含业务能作答的话，可以直接展示给业务。

#### 证据源降级（`degraded_sources`）

某个证据来源取不到时，`degraded_sources` 会显式列出它，并说明影响哪些判定。
**这里非空绝不等于"没问题"**：例如 P7（表关系）缺失时，R1（一对多必须明确处理方式）实际未生效。
关键来源缺失时整体置信度会被压到 0.50 上限。

分级判据是：该来源支撑的判定是否 **blocking** 级。是则缺失本身即 blocking；
仅支撑参考性判断的（历史案例、表样）记为 warning。

### 原「设计边界（尚未做）」的现状

- SQL 生成链路（语义层门禁之后）→ **M4 已落地**（plan → generate 两段式）
- 五层门禁 → **M5 已落地**（L1 语法 / L2 静态规则·分级阻断 / L3 只读 / L4 语义 dry-plan / L5 结果断言）；SQLFluff / Great Expectations 留插槽、默认关闭
- 需求单的**对话式补全**（多轮追问缺失字段）→ 由**客户端 Agent** 承担（网关不内置 LLM）；网关侧为一次性校验 + 澄清问答闭环（`confirmation_*`）

## 四、代码结构

| 文件 | 职责 |
|---|---|
| `app.py` | FastMCP 服务端：48 个工具、`/healthz` 路由、启动入口（启动时幂等建表） |
| `wren.py` | Wren MCP 客户端（streamable-http，会话复用 + 失效重握手） |
| `registry.py` | 数据集注册表（Wren 端点 + MDL 路径 + PG DSN），**换库只改注册项** |
| `planner.py` | 确定性规划器兜底（A/B 两套意图库）+ 只读门禁 |
| `db.py` | 网关自有元数据库：7 张表 DDL + 线程内连接复用 |
| `demand.py` | 需求受理：四类校验、状态机、事件留痕 |
| `masking.py` | 入口侧脱敏：7 类规则 + 姓氏闸门 + 区间去重 |
| `knowledge.py` | RAGFlow 检索客户端：引用解析 + 问句压缩回退 |
| `metadata.py` | 元数据字典：MDL 语义 × 物理结构合流，"建表但不建模"标注 |
| `collect.py` | 元数据采集：`native` / `schemacrawler` 双后端 + 交叉校验 |
| `attachments.py` | MinIO 附件：上传、列举、临时链接、文本风险扫描 |
| `evidence.py` | **M3** 证据编排：P1–P9 阶梯、词表构建、命中匹配、冲突检测、R1–R7 规则集 |
| `semantics.py` | **M3** 语义分析：五个 Skill 的确定性落地（需求理解 / 颗粒度 / 时间口径 / 规则校验 / 问题生成），纯函数不碰库 |
| `analysis.py` | **M3** 编排与落库：组装、轮次与确认记录持久化、状态流转（写入纪律集中在这一层） |
| `healthcheck.py` | 双探活脚本（HTTP `/healthz` + MCP `initialize`/`tools/list`） |
| `Dockerfile` / `requirements.txt` | 镜像构建 |

## 五、探活与自证

```bash
# 在仓库根目录
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # 双探活
python3 tools/mcp_acceptance_check.py                                 # MCP 工具面 48 个逐条核对
python3 tools/gate_all.py --layers G0,G1                              # 静态 + 单元门禁（快跑，不写库）

# 离线自证（改动脱敏 / 检索 / 闸门后随手复跑）
python3 tools/gates_selftest.py                                       # 五层门禁自证
python3 tools/rules_selftest.py                                       # 规则库自证
python3 tools/masking_selftest.py                                     # 脱敏自证
python3 tools/knowledge_selftest.py                                   # 问句压缩自证
python3 tools/evidence_selftest.py                                    # 证据词表自证（近线，无元数据库则跳跃跳过）
python3 tools/semantics_selftest.py                                   # 语义闸门自证
```

- 退出码 `0` = 全过；`1` = 任一失败（`collect_metadata.py` 用 `2` 表示数据不一致）
- `--deep` 额外调用一次 `gateway_health` 工具，验证工具面真的可执行
- `*_selftest.py` 基本不依赖网络（`evidence_selftest.py` 的 B/C 组需元数据库）
- 完整四层门禁（G0 静态 / G1 单元 / G2 独立复核 / G3 真实链路）见仓库根 `README.md` §6.2

### 演示前重置环境（可选）

端到端演练与门禁 G3 每跑一次都会往元数据库写测试需求单、往对象存储写附件；跑过几轮后「需求看板」就全是测试单。演示前用重置工具擦净——
**默认 dry-run，不加 `--apply` 一个字都不改**。

该工具需宿主机具备 `psycopg` / `minio`，用隔离虚拟环境运行：

```bash
VENV=/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python
$VENV tools/reset_demo_data.py                              # 只看会删什么
$VENV tools/reset_demo_data.py --apply                      # 清需求单 + 分析轮次 + 确认问答 + 知识库测试夹具
$VENV tools/reset_demo_data.py --apply --purge-attachments  # 连同附件对象一并清理
```

> 附件清理按**全量列举 + 归属二分**做，不是按 `demand_id` 前缀扫——需求单删除后其附件前缀
> 就对不上任何现存单号了，只扫前缀会留下永久无主对象（实测踩到过 6 个）。

> M3 的 `analysis_rounds` / `confirmations` 都是**以 `demand_id` 关联的派生数据**，必须先于
> `demand_requests` 删除。漏删会留下指向已删需求单的孤儿行，既让表规模统计失真，也让下一轮
> 验收从脏状态起跑（实测踩到过：留下 5 条轮次 + 10 条确认记录）。工具现已在复核环节自查孤儿数。

## 六、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_HOST` / `GATEWAY_PORT` | `0.0.0.0` / `8080` | 容器内监听地址 |
| `GATEWAY_MCP_PATH` | `/mcp` | MCP 端点路径 |
| `WREN_A_URL` / `WREN_B_URL` | 容器内 `http://wren-mcp-a:9000` | Wren MCP 端点 |
| `MDL_A_PATH` / `MDL_B_PATH` | `/workspace-a/mdl.json` 等 | MDL 语义层文件 |
| `WREN_PG_A_DSN` / `WREN_PG_B_DSN` | `postgresql://test:test@wren-postgres-a:5432/test` 等 | 元数据采集直连 DSN |
| `ASSISTANT_DB_DSN` | `postgresql://assistant:assistant@assistant-postgres:5432/assistant` | 网关自有元数据库 |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` | `assistant-minio:9000` / `…` / `askoda-attachments` | 附件存储 |
| `KNOWLEDGE_API_URL` | `http://host.docker.internal:19380/api/v1` | RAGFlow 检索接口 |
| `KNOWLEDGE_API_KEY` / `KNOWLEDGE_API_KEY_FILE` | 空 | 知识库凭据（二选一） |
| `KNOWLEDGE_DATASET_ID` | 空 | 目标数据集 |
| `KNOWLEDGE_TIMEOUT` | `30` | 检索超时（秒） |

> 容器访问宿主机上的 RAGFlow 走 `host.docker.internal`，编排里已加
> `extra_hosts: ["host.docker.internal:host-gateway"]`。

## 七、已知工程约束

1. **FastMCP 代理能力**：FastMCP 版本已升至 4.x，`create_proxy` 已移除。按《实施推进与验收方案》§12「待实测项 1」的降级条款，改用 POC 阶段已验证的手写 streamable-http client 作为 Wren 接入层——不依赖框架代理，Wren 会话完全可控。
2. **数据卷复用**：编排里的 `wren-pgdata` / `wren-pgdata-b` 为 external，指向单栈时代既有卷，历史数据与已部署 MDL 零迁移。M2 新增的 `askoda-pgdata` / `askoda-minio-data` 为本编排自有卷（卷名在 `volumes:` 里显式 `name:` 钉住，与工程名一致，换目录名 / 项目名不影响数据）。
3. **单栈 compose 与本编排共用容器名与数据卷**（`wren-docker/`、`wren-docker-b/`），**不可同时起两套**；单栈仅作单库调试用。
4. **入口侧脱敏的作用域边界**：姓名规则靠"首字须为常见姓氏"降误判，因此**孤立人名（如只在句中出现的"李明"而无任何称谓/上下文）不会被识别**。这是刻意的取舍——误掩普通词的代价（需求单读不通）与漏掩的代价（敏感值落库）需要平衡，而真正敏感的手机号/身份证/银行卡/邮箱/地址是**格式驱动**、识别稳定的。详见 `knowledge/02-数据安全与敏感字段管理办法.md`。
5. **RAGFlow 表格型 chunk 无坐标**：从 markdown 表格切出的 chunk `positions` 为空。`knowledge._locator` 会如实标注为 `position=n/a(表格型chunk未返回坐标)`，下游用 `position_available` 分支处理，不要依赖 `positions` 恒存在。
6. **元数据两条采集路径**：`schemacrawler` 后端出结构（表/列/主键），`information_schema` 出类型名，每次采集自动交叉校验，不一致即退出码 2。不要单看一侧就认为字典正确。
7. **MDL 只在容器内可达**：`MDL_A_PATH` / `MDL_B_PATH` 默认是容器内路径（`/workspace-a/mdl.json`），宿主机直跑网关代码时读不到，P7（表关系）会失败。这不是缺陷，而是编排设计（MDL 以只读卷挂载）；要宿主机复现，把 `MDL_B_PATH` 指向 `wren-docker-b/workspace/mdl.json` 即可。**重要的是失败时不会静默**：`degraded_sources` 会报出 P7 缺失、R1 未生效、置信度压到 0.50 上限。
8. **镜像标签与版本号同步**：编排里 `image: askoda-gateway:0.3.0`，与 `gateway/app.py` 的 `GATEWAY_VERSION` 一致。**升版本号时两处要一起改**，否则运维侧看标签会误判。
9. **网关启动时建表**：`db.init_schema()` 挂在 `main()` 里（幂等）。此前只有 `tools/collect_metadata.py` 会调它，导致「全新部署 + 首次调用」必然失败且要到第一次写库才暴露。元数据库未就绪时不阻断启动，只打警告——探活会把状态如实报成 `degraded`。
