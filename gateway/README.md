# 服务端网关（M1 网关骨架 → M2 需求受理与知识底座 → M3 语义分析与确认闭环）

数据需求智能分析助手的服务端入口：对 Agent 暴露 MCP 工具，对内接入 Wren 语义层双库、
自有元数据库与附件对象存储，对下接入 RAGFlow 知识底座。

> **网关侧不内置 LLM。** 语义分析以「证据编排 + 确定性规则」落地，结论一律带来源与置信度；
> 需要自由生成的部分由**客户端 Agent** 承担。因此 M3 的验收与回放可在任意 Agent
> （豆包 / Codex / WorkBuddy）上跑，不存在"必须先选定某个模型"这类前置条件。

## 一、一键起停

在**项目根目录**（`数据需求智能分析助手/`）执行：

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
| 附件对象存储 | `127.0.0.1:19000`（S3）/ `19001`（控制台） | MinIO，桶 `demand-attachments` |
| 知识底座 | `http://127.0.0.1:19380/api/v1` | RAGFlow 检索接口（网关只做客户端） |

宿主机端口用 `18080` 而非 `8080`：`8080` 已被本项目 `demo-server.py` 长期占用。

## 三、MCP 工具契约（M1 7 个 + M2 14 个 + M3 7 个 = 28 个）

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
| `analysis_first_round` | `demand_id`, `dataset="B"` | 取证据（P1–P8）→ 六槽位结论 → 规则校验 → 生成待确认问题 → 落库。**轮次只增不改** |
| `analysis_rounds` | `demand_id` | 列出全部轮次：轮次号、证据数、问题数、主体、阻断项数 |
| `analysis_get` | `demand_id`, `round_no=None` | 取某一轮完整结果（默认最新一轮） |
| `analysis_evidence` | `demand_id`, `round_no`, `level` | 取证据链，可按优先级过滤（P1–P9） |
| `confirmation_generate` | `demand_id`, `dataset="B"` | **只刷新待确认问题**，不新增轮次（知识库/元数据更新后重问用） |
| `confirmation_list` | `demand_id`, `include_history=False` | 列确认问答；`include_history=True` 连历史版本一起给 |
| `confirmation_answer` | `demand_id`, `question_id`, `answer`, `choice` | 答复问题。**不覆盖历史**：`version+1` + `supersedes` 指向旧版 |

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

### 设计边界（尚未做）

- SQL 生成链路（语义层门禁之后）→ **M4**
- 五层门禁的 SQLGlot / SQLFluff / Great Expectations 三层 → **M5**（当前为只读门禁 + Wren dry_run 两层）
- 需求单的**对话式补全**（多轮追问缺失字段）→ 未排期；当前为一次性校验 + 提示

## 四、代码结构

| 文件 | 职责 |
|---|---|
| `app.py` | FastMCP 服务端：28 个工具、`/healthz` 路由、启动入口（启动时幂等建表） |
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
| `evidence.py` | **M3** 证据编排：P1–P8 阶梯、词表构建、命中匹配、冲突检测、R1–R7 规则集 |
| `semantics.py` | **M3** 语义分析：五个 Skill 的确定性落地（需求理解 / 颗粒度 / 时间口径 / 规则校验 / 问题生成），纯函数不碰库 |
| `analysis.py` | **M3** 编排与落库：组装、轮次与确认记录持久化、状态流转（写入纪律集中在这一层） |
| `healthcheck.py` | 双探活脚本（HTTP `/healthz` + MCP `initialize`/`tools/list`） |
| `Dockerfile` / `requirements.txt` | 镜像构建 |

## 五、探活与自证

```bash
# 在项目根目录
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # 双探活
python3 tools/m2_verify.py                                            # M2 端到端 22 用例
python3 tools/m3_verify.py                                            # M3 端到端 109 断言
python3 tools/masking_selftest.py                                     # 脱敏自证（离线）
python3 tools/knowledge_selftest.py                                   # 问句压缩自证（离线）
python3 tools/evidence_selftest.py                                    # 证据词表自证（近线，无元数据库则跳跃跳过）
python3 tools/semantics_selftest.py                                   # 语义闸门自证（离线）
```

- 退出码 `0` = 全过；`1` = 任一失败（`collect_metadata.py` 用 `2` 表示数据不一致）
- `--deep` 额外调用一次 `gateway_health` 工具，验证工具面真的可执行
- 四份 `*_selftest.py` 不依赖网络（`evidence_selftest.py` 的 B/C 组需元数据库），改动脱敏/检索/闸门后应随手复跑
- `m3_verify.py` 会在客户端**独立复扫一遍技术词**（技术词取自 `wren_manifest`，不是服务端内部清单）——
  自己验自己等于没验；跑完默认清理产生的需求单，加 `--keep` 保留现场

### 演示前重置环境（可选）

`tools/m2_verify.py`、`tools/m3_verify.py` 每跑一次都会往元数据库写测试需求单（M3 还会写分析轮次与
确认问答）、往对象存储写附件；跑过几轮后「需求看板」就全是测试单。演示前用重置工具擦净——
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
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` | `assistant-minio:9000` / `…` / `demand-attachments` | 附件存储 |
| `KNOWLEDGE_API_URL` | `http://host.docker.internal:19380/api/v1` | RAGFlow 检索接口 |
| `KNOWLEDGE_API_KEY` / `KNOWLEDGE_API_KEY_FILE` | 空 | 知识库凭据（二选一） |
| `KNOWLEDGE_DATASET_ID` | 空 | 目标数据集 |
| `KNOWLEDGE_TIMEOUT` | `30` | 检索超时（秒） |

> 容器访问宿主机上的 RAGFlow 走 `host.docker.internal`，编排里已加
> `extra_hosts: ["host.docker.internal:host-gateway"]`。

## 七、已知工程约束

1. **FastMCP 代理能力**：FastMCP 版本已升至 4.x，`create_proxy` 已移除。按《实施推进与验收方案》§12「待实测项 1」的降级条款，改用 POC 阶段已验证的手写 streamable-http client 作为 Wren 接入层——不依赖框架代理，Wren 会话完全可控。
2. **数据卷复用**：编排里的 `wren-pgdata` / `wren-pgdata-b` 为 external，指向单栈时代既有卷，历史数据与已部署 MDL 零迁移。M2 新增的 `assistant-pgdata` / `assistant-minio-data` 为本编排自有卷。
3. **单栈 compose 与本编排共用容器名与数据卷**（`wren-docker/`、`wren-docker-b/`），**不可同时起两套**；单栈仅作单库调试用。
4. **入口侧脱敏的作用域边界**：姓名规则靠"首字须为常见姓氏"降误判，因此**孤立人名（如只在句中出现的"李明"而无任何称谓/上下文）不会被识别**。这是刻意的取舍——误掩普通词的代价（需求单读不通）与漏掩的代价（敏感值落库）需要平衡，而真正敏感的手机号/身份证/银行卡/邮箱/地址是**格式驱动**、识别稳定的。详见 `knowledge/02-数据安全与敏感字段管理办法.md`。
5. **RAGFlow 表格型 chunk 无坐标**：从 markdown 表格切出的 chunk `positions` 为空。`knowledge._locator` 会如实标注为 `position=n/a(表格型chunk未返回坐标)`，下游用 `position_available` 分支处理，不要依赖 `positions` 恒存在。
6. **元数据两条采集路径**：`schemacrawler` 后端出结构（表/列/主键），`information_schema` 出类型名，每次采集自动交叉校验，不一致即退出码 2。不要单看一侧就认为字典正确。
7. **MDL 只在容器内可达**：`MDL_A_PATH` / `MDL_B_PATH` 默认是容器内路径（`/workspace-a/mdl.json`），宿主机直跑网关代码时读不到，P7（表关系）会失败。这不是缺陷，而是编排设计（MDL 以只读卷挂载）；要宿主机复现，把 `MDL_B_PATH` 指向 `wren-docker-b/workspace/mdl.json` 即可。**重要的是失败时不会静默**：`degraded_sources` 会报出 P7 缺失、R1 未生效、置信度压到 0.50 上限。
8. **镜像标签未随版本号更新**：编排里 `image: demand-assistant-gateway:0.1.0`，而网关实际版本 0.3.0。不影响运行（每次 `build` 都重新构建），但运维侧看标签会误判。已在 M3 验收单列为未决事项。
9. **网关启动时建表**：`db.init_schema()` 挂在 `main()` 里（幂等）。此前只有 `tools/collect_metadata.py` 会调它，导致「全新部署 + 首次调用」必然失败且要到第一次写库才暴露。元数据库未就绪时不阻断启动，只打警告——探活会把状态如实报成 `degraded`。
