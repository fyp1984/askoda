# Askoda · 数据需求智能分析助手

> **[English](README.en.md) | 中文**
>
> 业务用自然语言提数据需求，系统给出「口径明确、可解释、可审计」的 SQL。
> 每一次取数都留下完整可追溯的判断链。

---

<p align="center">
  <a href="#一项目定位">项目定位</a> ·
  <a href="#二核心特性">核心特性</a> ·
  <a href="#三架构与设计原则">架构</a> ·
  <a href="#四目录结构">目录</a> ·
  <a href="#五快速上手">快速上手</a> ·
  <a href="#六验证与自证">验证</a> ·
  <a href="#七里程碑与验收矩阵">里程碑</a> ·
  <a href="#八贡献指南">贡献</a> ·
  <a href="#九路线图">路线图</a> ·
  <a href="#十许可证">许可证</a>
</p>

---

## 一、项目定位

Askoda 面向 **高合规行业的数据取数场景**（金融、监管、能源、政府、大型制造等）。它的目标不是"把问题翻译成 SQL"这么简单，而是让每一次取数都留下可追溯的判断链：

> 这句话是怎么被理解的 → 用了哪张表 → 口径从哪里来 → 生成前后的每一道门禁拦了什么 → 谁在什么时候执行过 → 结果为什么被判定为可交付。

Askoda 不是"万能问数器"。对于**口径模糊、一对多放大、未建模字段、未确认指标**，它宁可**返回澄清问题或直接拒绝**，也不会用默认值静默兜底。这是它与市面上通用 AI-BI 工具最本质的区别。

**适用场景**
- 分析师需要反复与业务对口径的 SQL 生产场景
- 监管报送 / 内审 / 合规部门的取数留痕需求
- 指标口径复杂、历史数据治理不完善的大型企业
- 希望逐步落地"语义层 + 证据链"的数据中台建设

**不适用场景**
- 个人玩票 / 临时小数据集的探索式分析（直接用 ChatBI 更合适）
- 追求"一句话出图表"的产品演示（Askoda 会追问，不会秒出结果）

---

## 二、核心特性

| 特性 | 说明 |
|---|---|
| **需求受理与结构化** | 一句业务口吻的需求 → 结构化需求（指标/维度/过滤/颗粒度/时间窗），带 JSON Schema 契约校验 |
| **语义层约束生成** | 不做自由生成，先把候选对象收敛到语义层（MDL）可见范围；未命中即拒绝，不猜 |
| **五层 SQL 门禁** | L1 语法（sqlglot）→ L2 AST 静态规则 → L3 只读门禁 → L4 语义层 dry-plan → L5 结果断言 |
| **澄清问答闭环** | 口径不明时生成澄清问题、回填答案并留痕，不以默认值静默兜底 |
| **只读执行** | 生成的 SQL 一律经语义层执行，物理层不做写操作；`DELETE`/`INSERT`/多语句拼接在 L3 直接拦截 |
| **九字段留痕 + 审计回放** | 每次执行记录需求版本、语义层版本、规则版本、生成 SQL、门禁结论、执行人……可完整回放五段链条 |
| **双库可换** | 同一套代码接两套语义层（裸库 A / 已治理库 B），换库零代码改动 |
| **失败回退矩阵 F1–F5** | 分类明确失败归属（口径不清 / 缺元数据 / 候选冲突 / 规则拦截 / 执行异常），用于判定"该补什么" |
| **证据编排 + 确定性规则** | 网关侧**不内置 LLM**，以证据优先级（P1–P9）+ 规则集（R1–R7）落地语义分析，每条结论带来源与置信度 |
| **健壮性 & 并发安全** | 只读工具 3 次退避重试、超时分层配置（专用 > 通用 > 默认 60s）、`run_id` 同进程并发唯一、核心链路仅用局部变量 |
| **知识引用留痕** | 每条知识库引用写 `knowledge_citations` 表（KC- 前缀），审计回放时可精确追溯引用了哪一页哪一段 |
| **入口侧脱敏** | 需求提交时自动扫描 7 类敏感信息（姓名/手机/身份证/银行卡/邮箱/地址/金额）并分级脱敏，掩码版与原文版分离存储 |

---

## 三、架构与设计原则

### 3.1 总览

```
业务端（Agent / 客户端 · 豆包 / WorkBuddy / Codex 等任意）
        │
        ▼
自建统一前端 ──► FastAPI 网关 / BFF ──► MCP 网关（FastMCP 4.x, streamable-http）
                                              │
                        ┌─────────────────────┼─────────────────────┐
                        ▼                     ▼                     ▼
                  Wren 语义层 A         Wren 语义层 B         知识底座 / 元数据库
                  （电商域 裸库）      （零售会员域 已治理）   （RAGFlow 检索 / MinIO 附件）
```

### 3.2 三条铁律（架构底线）

1. **网关侧不内置 LLM**。
   语义分析走「证据编排 + 确定性规则」，每条结论带来源与置信度；需要自由生成的部分（如对话式补全、业务话术润色）**交由客户端 Agent**。
   因此本项目**不绑定任何具体模型**，M3 之后的验收与回放可在任意 Agent 上跑。

2. **不 fork、不改开源引擎源码**。
   定制一律走官方扩展点或外挂层。语义层用上游 Wren Engine Ibis 官方镜像、SQL 解析用 sqlglot 纯 Python 官方 API，绝不改 vendor 源码。

3. **第三方能力收口**。
   门禁 → `gateway/gates.py`、契约 → `gateway/contracts/`、规则 → `gateway/rules.py`、证据 → `gateway/evidence.py`。
   替换或升级任何一个底层能力只改一处，不改调用面。

### 3.3 取数判断链（五段链条 · 审计回放可复原）

```
① input           结构化需求（六槽位 + 四版本号：schema/requirement/pack/rules）
    │
    ▼
② sql             plan() 元数据计划（无 SQL 正文）→ generate() 生成 SQL → 立即门禁审查
    │
    ▼
③ review          L1–L5 五层门禁，每条规则命中记录 snippet + detail
    │
    ▼
④ result          只读执行结果（row_count / columns / 抽样数据 / 信号列）
    │
    ▼
⑤ knowledge       知识引用（KC- 前缀 + chunk_id + 文档坐标 + 原文片段）
```

任一环节失败即写入 `fallback.py` F1–F5 分类，不静默继续。

---

## 四、目录结构

```
askoda/
├── gateway/          网关服务端（MCP 工具面 41 个）：受理 / 分析 / 生成 / 门禁 / 执行 / 留痕
│   ├── contracts/    对外契约（结构化需求 JSON Schema 等）
│   ├── Dockerfile
│   ├── requirements.txt    仅依赖：fastmcp / psycopg / minio / sqlglot
│   ├── app.py        FastMCP 入口
│   ├── wren.py       Wren streamable-http 客户端（会话复用 + 超时 + 重试）
│   ├── db.py         网关自有元数据库：7 张表 DDL + query/query_one/execute 接口
│   ├── demand.py     需求受理：四类校验 + 状态机 + 事件留痕
│   ├── masking.py    入口侧脱敏：7 类规则 + 姓氏闸门
│   ├── knowledge.py  RAGFlow 客户端：引用解析 + 问句压缩回退
│   ├── metadata.py   元数据字典：MDL × 物理结构合流，"建表但不建模"标注
│   ├── collect.py    元数据采集：native / schemacrawler 双后端
│   ├── attachments.py  MinIO 附件：上传 / 列举 / 临时下载链接
│   ├── evidence.py   证据编排：P1–P9 阶梯 + 词表构建 + 冲突检测
│   ├── rules.py      L2 静态规则：R1–R7，纯函数模块
│   ├── gates.py      五层门禁编排：L1→L2→L3→L4→L5
│   ├── planner.py    确定性规划器兜底
│   ├── requirement.py  结构化需求校验与版本管理
│   ├── semantics.py  语义分析：六槽位确定性落地，纯函数不碰库
│   ├── analysis.py   分析编排与落库：轮次 / 确认问答持久化
│   ├── fallback.py   F1–F5 失败分类：纯函数、不抛异常
│   ├── planner.py / sqlgen.py / sqlpack.py  两段式 SQL 生成（plan → generate）
│   ├── sqlrun.py     只读执行 + 九字段留痕 + 列表筛选 + 审计回放接口
│   └── healthcheck.py  双探活脚本（HTTP /healthz + MCP initialize）
├── tools/            自证与独立复扫脚本（每个里程碑一套，含对抗用例）
│   ├── m4_verify.py / m5_verify.py / mcp_acceptance_check.py  …
│   ├── gates_selftest.py / rules_selftest.py / evidence_selftest.py …
│   ├── audit_verify.py（审计回放）/ robustness_verify.py（重试/超时/并发）
│   └── collect_metadata.py / reset_demo_data.py
├── poc-eval/         POC 评估与端到端演练（题库、回放、演练脚本、机器可读报告）
│   ├── poc_e2e_gateway.py    主线故事 + POC-1 题库 + POC-3 陷阱 + MDL 事实核对
│   └── e2e-gateway-report.json
├── knowledge/        知识底座样例文档
│   ├── 01-指标口径说明书-零售会员域.md
│   ├── 02-数据安全与敏感字段管理办法.md
│   ├── 03-表结构与颗粒度说明-零售会员域.md
│   └── 04-需求受理与口径确认规程.md
├── wren-docker/      模拟库 A 环境（电商域，6 模型 / 29 字段 / 5 关系）
├── wren-docker-b/    模拟库 B 环境（零售会员域，8 模型 / 47 字段 / 10 关系）
├── demo/             演示程序
│   ├── demo-server.py       本地实测服务（127.0.0.1:8080）
│   ├── 数据需求智能分析助手-V7.html  工作台 Demo 页
│   └── 语义层实测页面 + 品牌资产
├── docker-compose.yml      一键起停编排（gateway + 双库 + 元数据库 + MinIO，共 7 服务）
├── .env.example  环境变量样例
└── README.md         本文件
```

> 项目管理类文档（PRD、技术方案、作战计划、验收单、归档）**不在本仓库内**，独立维护在项目文档工作空间。
> 本仓库只保留工程文件与面向开发者的说明。

---

## 五、快速上手

### 5.1 前置依赖

- Docker（推荐 OrbStack / Docker Desktop 4.28+）
- Python 3.11+（仅本地跑工具脚本时用，服务端运行不需要）
- `docker compose` V2（`docker compose` 命令可用）

### 5.2 三步启动

```bash
# 1. 准备环境变量
cp .env.example .env

# 2. 一键起停全部服务（首次会构建 gateway 镜像，约 1–3 分钟）
docker compose up -d

# 3. 检查状态（7 个服务全部 healthy 才算就绪）
docker compose ps
```

改了 `gateway/` 代码后重新构建：

```bash
docker compose up -d --build
```

> ⚠️ macOS + OrbStack 沙箱下，`compose build` 需要把 buildx 配置目录重定位到仓库内（沙箱不允许写 `~/.docker/buildx`）：
> ```bash
> BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway
> ```

### 5.3 接入面

| 项 | 地址 | 说明 |
|---|---|---|
| **MCP 端点** | `http://127.0.0.1:18080/mcp` | streamable-http，Agent 接这里，PROTOCOL_VERSION=2025-03-26 |
| **健康检查** | `http://127.0.0.1:18080/healthz` | 各数据集与组件连通性，返回 `degraded` 时查看 `components` 字段 |
| **模拟库 A（电商）** | `127.0.0.1:9000`(MCP) / `15432`(PG) | 6 模型 / 29 字段 / 5 关系 |
| **模拟库 B（零售会员）** | `127.0.0.1:9002`(MCP) / `15433`(PG) | 8 模型 / 47 字段 / 10 关系 |
| **网关元数据库** | `127.0.0.1:15434`(PG) | user=assistant / 需求单、事件、元数据字典 |
| **附件对象存储** | `127.0.0.1:19000`(S3) / `19001`(控制台) | MinIO，桶 `demand-attachments` |
| **知识底座（可选）** | `http://127.0.0.1:19380/api/v1` | RAGFlow v0.26.4 检索接口，网关只做客户端 |

宿主机网关端口用 `18080` 而非 `8080`：`8080` 留给本地演示服务 `demo/demo-server.py`。

### 5.4 常见问题排错

| 现象 | 原因 | 修复 |
|---|---|---|
| `assistant-postgres` 启动后首次调用建表失败 | 网关启动时 `init_schema` 幂等建表，但首次 `up` 可能 PG 未 ready | `docker compose restart gateway`，健康检查会把 `degraded` 转回 `ok` |
| Wren MCP 返回 `503` / `connection refused` | Wren Engine 冷启动慢，首次需 30–60s | 等 1 分钟后重试，或 `docker compose logs wren-mcp-a` 看日志 |
| 知识库接口报 `host.docker.internal` 不可达 | RAGFlow 不在本机或未起来 | 在 `.env` 里把 `KNOWLEDGE_API_URL` 改为实际可达地址，或留空跳过（`degraded_sources` 会如实报告） |
| 数据卷 `wren-pgdata` 报 `external volume not found` | 新机器上没有单栈时代遗留卷 | 在 `.env` 里改为 `WREN_PG_VOLUME=askoda_wren-pgdata` 或删除 `external: true` 由 compose 自建 |
| `tools/*_verify.py` 报 ModuleNotFoundError | 脚本依赖网关内部模块，不能直接在宿主机跑 | 按脚本头部注释 `docker cp` 进容器 + `PYTHONPATH=/app` 执行 |

---

## 六、验证与自证

所有验证脚本遵循统一约定：
- **退出码 0 = 全绿；非 0 = 有断言失败；退出码 2 = 数据不一致**
- **不依赖外部 LLM**，可在离线环境独立复现
- **自己验自己 ≠ 验收**：关键链路（如 M3 技术词闸门）由脚本在客户端**独立复扫**，不信赖服务端内部清单

### 6.1 核心验证脚本（宿主机能跑的）

```bash
# 双探活（HTTP + MCP 全链路），加 --deep 额外真执行一次 gateway_health
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep

# MCP 工具面验收（41 个工具 + 真实调用比对，对照《实施推进与验收方案》）
python3 tools/mcp_acceptance_check.py

# 五层门禁自证（纯离线，不依赖数据库）
python3 tools/gates_selftest.py
python3 tools/rules_selftest.py

# 脱敏 / 检索 / 证据 / 语义 四门自证（基本都是纯离线）
python3 tools/masking_selftest.py
python3 tools/knowledge_selftest.py
python3 tools/evidence_selftest.py
python3 tools/semantics_selftest.py
```

### 6.2 需要容器内跑的脚本（依赖网关内部模块）

```bash
# 例：M5 门禁端到端 62 断言
docker cp tools/m5_verify.py demand-gateway:/app/tools/m5_verify.py
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/m5_verify.py

# M6-4 审计回放断言（含反查 dataset + payload 列）
docker cp tools/audit_verify.py demand-gateway:/app/tools/audit_verify.py
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/audit_verify.py

# M6-3 健壮性（超时退避重试 + 并发 20 次 run_id 唯一性）
docker cp tools/robustness_verify.py demand-gateway:/app/tools/
docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/robustness_verify.py
```

### 6.3 端到端演练

```bash
python3 poc-eval/poc_e2e_gateway.py
# 产出 poc-eval/e2e-gateway-report.json（机器可读，归档用）
```

覆盖：主线故事 12 条 + POC-1 题库 30 条（20 正向 / 10 拒绝）+ POC-3 陷阱 10 条 + MDL 事实核对 50 条。

### 6.4 演示前环境重置

```bash
# 默认 dry-run，不加 --apply 一个字都不改
python3 tools/reset_demo_data.py

# 清需求单 + 分析轮次 + 确认问答 + 知识库测试夹具
python3 tools/reset_demo_data.py --apply

# 连同附件对象一并清理
python3 tools/reset_demo_data.py --apply --purge-attachments
```

---

## 七、里程碑与验收矩阵

### 7.1 已完成里程碑（M1–M6）

| 里程碑 | 交付内容 | 验收脚本 | 结果（真实数值） |
|---|---|---|---|
| **M1** · 网关骨架 | MCP 7 工具 + Wren 双库接入 + 确定性 ask 兜底 | `mcp_acceptance_check.py` M1 段 | 7/7 ✅ |
| **M2** · 需求受理 | 需求 CRUD / 脱敏 / 元数据 / 附件 / 知识库 14 工具 | `m2_verify.py` | 22/22 ✅ |
| **M3** · 语义分析 + 确认闭环 | 证据编排 P1–P9 + 规则 R1–R7 + 7 工具 | `m3_verify.py` | 109/109 ✅ |
| **M4** · 两段式 SQL 生成 | plan() → generate() + 闭集自检 + pack_version 哈希 | `m4_verify.py` | 51/0 ✅（库 A） |
| **M5** · 五层门禁 + 纯规则库 | sqlglot + R1–R7 规范化 + 只读 + dry-plan + 结果断言 | `m5_verify.py` | 62/0 ✅（库 B） |
| **M6-1** · 规则版本指纹 | `rules_version` 含每条 `evaluate` 的 `source_sha8` | `m61r2_version_check.py` | ✅ |
| **M6-2** · 失败回退矩阵 F1–F5 | 分类纯函数 + F2 判据修正（按 `chosen_table` 空） | `m62_live_check.py` + `fallback_verify.py` | ✅ |
| **M6-3** · 健壮性（超时 / 重试 / 并发） | 超时三级配置 + READ_TOOLS 3 次退避 + run_id 唯一 | `robustness_verify.py` | 18/18 ✅ |
| **M6-4** · 审计留痕 + 回放 | actor 透传 + knowledge_citations + 列表按 dataset 反查 + 五段链条回放 | `audit_verify.py` | B 段 16/16 ✅ |

### 7.2 POC 题库验收

| 题库 | 正向准确率 | 拒绝覆盖率 | POC-3 陷阱拦截率 |
|---|---|---|---|
| POC-1（30 条） | 20/20 = 100% | 10/10 = 100% | — |
| POC-3（10 条） | — | — | 7/10 = 70%（规则补强进行中） |

### 7.3 技术债务清单（已知 · 非阻塞）

- [ ] `docker-compose.yml` 中 `image: demand-assistant-gateway:0.1.0` 标签未随代码版本号（当前 0.3.0）同步更新
- [ ] `.buildx` 目录未在 `.gitignore` 中声明（OrbStack 沙箱专用）
- [ ] POC-3 3/10 拦截漏项：R4 多值拼接顺序、R6 时间列后缀黑名单扩展、R7 未建模字段跨表引用
- [ ] 知识底座表格型 chunk 的 `positions` 恒空，需 RAGFlow 侧升级后对接精确坐标

---

## 八、贡献指南

### 8.1 提交规范（Conventional Commits + 署名）

```
<type>(<scope>): <subject>

<body>

Signed-off-by: 西北人 <fyp1984@yeah.net>
```

- `type` ∈ `feat / fix / docs / style / refactor / perf / test / chore / revert`
- `scope` = `gateway / tools / poc-eval / demo / wren-docker / wren-docker-b / docs`
- **每条提交必须带 `Signed-off-by`**（`git commit -s` 自动加）
- 一条提交只做一件事；代码与文档改动分离提交

### 8.2 代码约束（硬约束 · 过不了 CI）

| 编号 | 约束 | 验证方式 |
|---|---|---|
| HC-01 | 网关侧不内置任何 LLM 调用 | `grep -r "openai\|anthropic\|gemini\|llm\|chat_" gateway/` 必须空 |
| HC-02 | 禁止修改第三方开源源码 | — |
| HC-03 | 第三方依赖仅限 `fastmcp / psycopg / minio / sqlglot` | `gateway/requirements.txt` 行数不能增加（除了 bugfix 版本 pin） |
| HC-04 | 数据库访问必须走 `gateway/db.py` 的 `query/query_one/execute` | 自查调用点 |
| HC-05 | 规则文件 `gateway/rules.py` 必须纯函数，零 IO / 零 DB / 零网络 | 自查 import |
| HC-06 | 所有脚本必须 `ast.parse` 通过 | `python3 -c "import ast; ast.parse(open(f).read())"` 对每个 `.py` |
| HC-07 | 九字段留痕一行都不能缺 | `m5_verify.py` / `audit_verify.py` 断言 |
| HC-08 | actor 必须透传写入 `sql_runs.actor`，不许用默认值 | `audit_verify.py` B 段断言 |
| HC-09 | 筛选异常时严禁静默降级成全量 | `audit_verify.py` dataset 三态对照 |

### 8.3 验收新功能的三步法

1. **先写自证脚本**，放 `tools/mXX_<feature>_selftest.py`，全是断言；
2. **再改代码**，改完先跑 `ast.parse` 全量语法检查；
3. **最后跑 m4_verify / m5_verify 回归**，确保 51/0 / 62/0 的 baseline 不退化。

---

## 九、路线图（Roadmap）

### 短期（v0.4 · 已立项）

- [ ] **M6-5**：SQL 多候选差异度（F3 系数 0.35 阈值）+ hold 状态人工审核工作台
- [ ] **M6-6**：R4/R6/R7 规则补强，POC-3 陷阱拦截率目标从 70% → 90%
- [ ] **M7**：对话式需求补全（客户端 Agent 多轮追问，不回写网关核心逻辑）
- [ ] **M8**：指标口径词表在线管理 UI（当前走 knowledge 文档 + 元数据字典）

### 中期（v0.5 · 规划中）

- [ ] 语义层 MDL 在线 Editor + PR 式变更审批流
- [ ] SQL 审查不通过后的一期自动改写（治理指引 `how_to_fix` → 自动 patch）
- [ ] 数据集注册接口（当前走 `gateway/registry.py` 代码注册）
- [ ] FastMCP 5.x 升级 + `create_proxy` 官方能力回归（去掉手写 Wren 客户端）

### 长期（v1.0 · 概念阶段）

- [ ] 多租户 + 按 dataset 的细粒度 RBAC
- [ ] 指标血缘可视化（从需求 → SQL → 表/字段 → 口径文档的追溯图）
- [ ] 与主流 BI 工具（Metabase / Superset / Tableau）的导出对接
- [ ] 私有化部署一键安装包（helm chart + 离线安装包）

---

## 十、安全披露政策（Security）

**不要用 GitHub Issue 披露安全漏洞。**

发现以下类型问题请直接邮件给维护者 `<fyp1984@yeah.net>`，24 小时内响应：

- SQL 注入绕过（L3 只读门禁被破）
- 多语句拼接绕过 L2/L3
- 脱敏漏报（手机号/身份证/银行卡等真实敏感值未被 mask 落库）
- 附件存储权限绕过（可下载非本人 demand_id 的附件）
- 知识库凭证泄露到日志或返回体
- actor 伪造（非本人的执行记录写入了他人 actor 值）

邮件请附最小复现脚本 + 实际请求/响应体（打码敏感值后可）。

---

## 十一、许可证

© 2026 CheersAI · 西北人

本仓库代码采用 **MIT License** 授权，详见各文件头部；**文档部分采用 CC BY 4.0**，转载请注明来源。

第三方依赖许可证：
- [FastMCP](https://github.com/jlowin/fastmcp) — MIT
- [sqlglot](https://github.com/tobymao/sqlglot) — MIT
- [Wren Engine Ibis](https://github.com/Canner/wren-engine) — Apache-2.0
- [PostgreSQL](https://www.postgresql.org) — PostgreSQL License
- [MinIO](https://github.com/minio/minio) — AGPL-3.0（作为独立服务使用，不与本项目代码静态/动态链接）
