# Askoda · 数据需求智能分析助手

> **[English](README.en.md) | 中文**

<p align="center">
  <b>以自然语言与业务意图驱动的企业级数据分析与检索工具</b><br/>
  本体驱动 · Ontology-Driven Intent-to-Analytics
</p>

**一句话**：业务用一句自然语言提问，Askoda 给出**口径明确、可解释、可审计**的结果 —— 每一次取数都留下完整可追溯的判断链。

它的完整链路是四段：

> **① 知识底座（前置）** → **② 需求理解** → **③ 受控执行（安全 + 准确度门禁）** → **④ 合规审计（后置）**

与通用「意图 → SQL」工具的根本差别在**首尾两段**：**问数之前先建知识底座，取数之后再做合规审计** —— 正是这两段，决定了它能否在企业级真实生产里落地。

---

<p align="center">
  <a href="#一项目定位">项目定位</a> ·
  <a href="#ontology">本体驱动</a> ·
  <a href="#二核心特性">核心特性</a> ·
  <a href="#三架构与设计原则">架构</a> ·
  <a href="#四目录结构">目录</a> ·
  <a href="#五快速上手">快速上手</a> ·
  <a href="docs/GLOSSARY.md">术语表</a> ·
  <a href="#mcp-tools">MCP 能力视图</a> ·
  <a href="#mcp-ops">MCP 操作与升级</a> ·
  <a href="#六验证与自证">验证</a> ·
  <a href="#七里程碑与验收矩阵">里程碑</a> ·
  <a href="#八贡献指南">贡献</a> ·
  <a href="#九路线图roadmap">路线图</a> ·
  <a href="#十一许可证">许可证</a>
</p>

---

## 一、项目定位

Askoda 是**以自然语言与业务意图为驱动的企业级数据分析与检索工具**，本体论驱动。它面向的**不只是**高合规取数场景（金融、监管、能源、政府、大型制造等），而是**企业级真实生产中的通用数据分析与检索需求**。

它与市面上常见「意图 → SQL」工具的根本区别在于：**它在需求分析之前先建知识底座，在数据提取之后再做合规审计**——正是这**首尾两段**，决定了它能否在企业级真实生产里落地。

> 一次提问的完整判断链：这句话是怎么被理解的 → 用了哪张表 → 口径从哪里来 → 生成前后每一道门禁拦了什么 → 谁在什么时候执行过 → 结果为什么被判定为可交付。

### 📖 术语说明

本文及项目文档中出现的缩写（如 **MDL**、**BFF**、**MCP**、**RAGFlow**、**TEI**、**Wren**）与专有名词（证据层级 P1–P9、语义规则 R1–R7、静态规则 REGISTRY、门禁 G0–G3、架构红线 R1–R5），统一收敛在：

**→ [术语表 · docs/GLOSSARY.md](docs/GLOSSARY.md)**

> ⚠️ **注意本项目有四套编号体系，同名不同义**：`R1–R5` 是架构红线、`R1–R7` 是需求语义规则、`P1–P9` 是证据层级、`G0–G3` 是门禁层。引用时请写全称，避免混淆。

Askoda 不是"万能问数器"。对于**口径模糊、一对多放大、未建模字段、未确认指标**，它宁可**返回澄清问题或直接拒绝**，也不会用默认值静默兜底。这是它与市面上通用 AI-BI 工具最本质的区别。

### 1.1 三层主线：事实 → 事理 → 行动

<a id="ontology"></a>

Askoda 的整体研发遵循 **本体论驱动的数据管理**理念（理论参考：《本体驱动的 AI 数据管理》，编写组 著，机械工业出版社，2026 年 5 月第 1 版，ISBN 978-7-111-81075-9）。一次取数被拆成自下而上的三层：

| 层 | 承载什么 | 一句话 |
|---|---|---|
| **① 事实层 · Facts** | 业务需求受理 · 元数据采集与字典 · 附件归档 · Schema 快照 · 入口脱敏 | 把原始需求与元数据**归档治理**成事实 |
| **② 事理层 · 本体知识库** | MDL 语义层 · 知识检索 · 证据编排 · 业务规则集 · 确认问答 | 把业务语义与专家知识**萃取沉淀**成本体知识库 |
| **③ 行动层** | 两段式生成 · 五层门禁 · 只读执行 · 九字段留痕 · 审计回放 | 由一句自然语言**受控生成**查询、给出分析 |

三层是**有向依赖，不可跳级**：事实层没治理干净，事理层无从萃取；事理层口径没定，行动层不允许生成。这正是 Askoda「宁可追问、不做默认值兜底」的根因。

### 1.2 本体六要素落在哪

| 本体要素 | 在 Askoda 里的载体 |
|---|---|
| 类 Class | MDL 模型（模拟库 A 6 个 / B 8 个） |
| 实例 Instance | 只读执行返回的行记录（仅放行 `SELECT` / `WITH`） |
| 属性 Property | MDL 字段 + 元数据字典（业务含义 / 别名；未建模字段显式标"AI 不可见"） |
| 关系 Relation | MDL `relationships`（A 库 5 组 / B 库 10 组） |
| 约束 Constraint | 枚举值域 · 颗粒度 · 字段可见性 · L3 只读边界 |
| 规则 Rule | L2 静态规则集（11 条，其中 4 条命中即阻断）+ L4 语义层 dry-plan |

### 1.3 最小切面：不建全量，先跑通一片

本体是**长出来的**，不是一次性建出来的。Askoda 按「点 → 线 → 面」滚动推进，每一片当场可用、当场可验：

| 切面 | 含义 | 本项目实测刻度 |
|---|---|---|
| **点** | 一张表 + 一个口径 | 单表建模 ≤ 2 人天（维表 0.5 / 汇总表 1 / 明细宽表 1.5–2） |
| **线** | 一组强关联表 + 协同口径 + 关联方向 | 交易域链式（订单 → 明细 → 退款）一次打通，L2 规则可判 JOIN 方向 |
| **面** | 一个数据域 = 一整套可版本化的 MDL | 模拟库 A / B 双面并存，**换库代码零改动（diffs = 0）**；一个完整面 ≈ 8 表 · 47 字段 · 9.5 人天 |

先切哪里？优先选**语义复杂、规则明确、合规压力大**的真实痛点；个人探索式分析、"一句话出图"的演示场景不适用。

### 1.4 边界（重要）

本版本「行动层」生成的是 **SQL**，语义层用 **MDL** 承载；**「本体原生查询（图查询 / Cypher）」是路线图方向，尚未实现**。Askoda 采用本体论的思想与分层（事实 / 事理 / 行动、最小切面），但**并未**引入 RDF/OWL 本体库或 SPARQL / Cypher 引擎。任何对外材料请勿把路线图写成现状。

**适用场景**
- **业务经营 / 业务需求分析人员**直接用自然语言提问、拿结果（权限到位时）
- 分析师需要反复与业务对口径的查询生产场景
- 监管报送 / 内审 / 合规部门的取数与留痕需求
- 指标口径复杂、历史数据治理不完善的大型企业
- 希望逐步落地"知识底座 + 语义层 + 证据链"的数据中台建设

**不适用场景**
- 个人玩票 / 临时小数据集的探索式分析（直接用 ChatBI 更合适）
- 追求"一句话出图表"的产品演示（Askoda 会追问，不会秒出结果）

### 1.5 完整链路与差异化

一次「问数」被拆成四段，**首段与末段是 Askoda 与普通「意图→SQL」工具的分水岭**（普通工具通常只做中间两段）：

| 段 | 做什么 | 关键构件 |
|---|---|---|
| **① 知识底座（前置）** | 知识归集 → 语义分析 → 知识管理，沉淀为可版本化的本体知识库 | 事实层（需求 / 元数据 / 附件 / Schema / 脱敏）+ 事理层（MDL 语义层 / 知识检索 / 规则集） |
| **② 需求理解** | 自然语言 / 业务意图 → 结构化需求 + 证据编排 + 澄清闭环 | `requirement_structured` / `analysis_*` / `confirmation_*` |
| **③ 受控执行** | 两段式生成 → 五层门禁（**安全 + 准确度检查**）→ 只读执行 | `sql_plan` / `sql_generate` / `sql_review` / `sql_execute_readonly` |
| **④ 合规审计（后置）** | 九字段留痕 + 审计回放 + 知识引用溯源（**事后审计补强**） | `sql_run_*` / `knowledge_citations` |

| 维度 | 通用「意图→SQL」工具 | Askoda |
|---|---|---|
| 起点 | 直接生成 SQL | 先建**知识底座** |
| 语义来源 | 靠大模型现场猜 | 靠**本体知识库**确定性对齐 |
| 生成后 | 基本不校验 | **五层门禁**（语法 / 静态规则 / 只读 / 语义预演 / 结果断言） |
| 出错时 | 静默给出"能跑但可能错"的数字 | **追问或拒绝**，不默认兜底 |
| 留痕 / 合规 | 通常无 | **可整链回放 + 事后审计补强** |
| 适用 | 演示 / 个人探索 | 企业级**真实生产** |

**开放对象**：权限管控到位时按角色分权——既可开放给技术与数据开发人员，也可开放给业务经营、业务需求分析人员。

---

## 二、核心特性

| 特性 | 说明 |
|---|---|
| **本体驱动的三层主线** | 事实（需求 / 元数据归档治理）→ 事理（MDL 语义层 + 专家知识萃取）→ 行动（受控生成查询）；三层有向依赖、不可跳级 |
| **知识底座（前置）** | 需求分析之前先做知识归集 → 语义分析 → 知识管理，沉淀为可版本化的本体知识库（MDL + 元数据字典 + 规则集） |
| **合规审计补强（后置）** | 九字段留痕 + 审计回放 + 知识引用溯源（`knowledge_citations`），面向合规的事后审计可整链复原 |
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
├── gateway/          网关服务端（MCP 工具面 45 个）：受理 / 分析 / 生成 / 门禁 / 执行 / 留痕
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
│   ├── rules.py      L2 静态规则：11 条（4 条阻断 / 7 条警告），纯函数模块
│   ├── gates.py      五层门禁编排：L1→L2→L3→L4→L5
│   ├── planner.py    确定性规划器兜底
│   ├── requirement.py  结构化需求校验与版本管理
│   ├── semantics.py  语义分析：六槽位确定性落地，纯函数不碰库
│   ├── analysis.py   分析编排与落库：轮次 / 确认问答持久化
│   ├── fallback.py   F1–F5 失败分类：纯函数、不抛异常
│   ├── sqlgen.py / sqlpack.py   两段式 SQL 生成（plan → generate）
│   ├── sqlrun.py     只读执行 + 九字段留痕 + 列表筛选 + 审计回放接口
│   └── healthcheck.py  双探活脚本（HTTP /healthz + MCP initialize）
├── tools/            门禁、自证与运维工具（长期随仓库分发）
│   ├── gate_all.py   统一门禁入口（G0–G3 四层，自动发现同目录脚本）
│   ├── redline_guard.py  六条红线机器守卫
│   ├── mcp_acceptance_check.py  工具面逐条核验（走真实 MCP 协议）
│   ├── *_selftest.py  离线自证（gates / rules / masking / knowledge / evidence / semantics）
│   └── collect_metadata.py / ingest_knowledge.py / reset_demo_data.py / embedding_failover.sh
├── poc-eval/         POC 评估与端到端演练（题库与演练脚本）
│   ├── poc_e2e_gateway.py    主线故事 + POC-1 题库 + POC-3 陷阱 + MDL 事实核对
│   └── poc_eval.py / bank-b.json / rule_upgrade_dryrun.py
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
| **MCP 端点** | `http://127.0.0.1:18080/mcp` | streamable-http，Agent 接这里（FastMCP 4.x，协议版本随客户端协商） |
| **健康检查** | `http://127.0.0.1:18080/healthz` | 各数据集与组件连通性，返回 `degraded` 时查看 `components` 字段 |
| **模拟库 A（电商）** | `127.0.0.1:9000`(MCP) / `15432`(PG) | 6 模型 / 29 字段 / 5 关系 |
| **模拟库 B（零售会员）** | `127.0.0.1:9002`(MCP) / `15433`(PG) | 8 模型 / 47 字段 / 10 关系 |
| **网关元数据库** | `127.0.0.1:15434`(PG) | user=assistant / 需求单、事件、元数据字典 |
| **附件对象存储** | `127.0.0.1:19000`(S3) / `19001`(控制台) | MinIO，桶 `askoda-attachments` |
| **知识底座（可选）** | `http://127.0.0.1:19380/api/v1` | RAGFlow v0.26.4 检索接口，网关只做客户端 |

宿主机网关端口用 `18080` 而非 `8080`：`8080` 留给本地演示服务 `demo/demo-server.py`。

### 5.4 常见问题排错

| 现象 | 原因 | 修复 |
|---|---|---|
| `assistant-postgres` 启动后首次调用建表失败 | 网关启动时 `init_schema` 幂等建表，但首次 `up` 可能 PG 未 ready | `docker compose restart gateway`，健康检查会把 `degraded` 转回 `ok` |
| Wren MCP 返回 `503` / `connection refused` | Wren Engine 冷启动慢，首次需 30–60s | 等 1 分钟后重试，或 `docker compose logs wren-mcp-a` 看日志 |
| 知识库接口报 `host.docker.internal` 不可达 | RAGFlow 不在本机或未起来 | 在 `.env` 里把 `KNOWLEDGE_API_URL` 改为实际可达地址，或留空跳过（`degraded_sources` 会如实报告） |
| 数据卷 `wren-pgdata` 报 `external volume not found` | 新机器上没有单栈时代遗留卷 | 在 `.env` 里改为 `WREN_PG_VOLUME=askoda_wren-pgdata` 或删除 `external: true` 由 compose 自建 |
| `tools/*_selftest.py` 报 ModuleNotFoundError | 部分自证脚本依赖网关内部模块 | 纯离线自证（`gates` / `rules` / `masking` / `knowledge` / `evidence` / `semantics`）可在宿主直跑；确实需要内部模块的按脚本头部注释 `docker cp` 进容器 + `PYTHONPATH=/app` 执行 |

---

### 5.5 MCP 能力视图（45 个工具 · 九个域）

<a id="mcp-tools"></a>

网关通过**一个** streamable-http 端点对外暴露 **45 个工具**，按业务域分为九组。**工具名与入参以 `gateway/app.py` 的 `@mcp.tool` 注册处为唯一事实源**，下表与之逐条一致（自动核验见 `tools/mcp_acceptance_check.py`）。返回字段的完整承诺与边界条件见项目文档工作空间的《MCP 工具契约与注册说明》。

| # | 域 | 数 | 工具 |
|---|---|---|---|
| 1 | 健康与数据集 | 4 | `gateway_health` `datasets` `knowledge_health` `schema_version` |
| 2 | 兜底规划与 Wren 直通 | 5 | `plan` `wren_manifest` `wren_dry_run` `wren_query` `ask` |
| 3 | 需求受理 | 6 | `demand_create` `demand_get` `demand_list` `demand_summarize` `demand_set_status` `demand_similar_precheck` |
| 4 | 知识检索 | 4 | `knowledge_search` `knowledge_documents` `knowledge_retire` `knowledge_citation_list` |
| 5 | 元数据字典 | 3 | `metadata_lookup` `metadata_glossary` `metadata_collect` |
| 6 | 附件 | 3 | `attachment_put` `attachment_list` `attachment_url` |
| 7 | 语义分析与确认闭环 | 7 | `analysis_first_round` `analysis_rounds` `analysis_get` `analysis_evidence` `confirmation_generate` `confirmation_list` `confirmation_answer` |
| 8 | 结构化需求与上下文包 | 5 | `schema_scan` `requirement_structured` `requirement_get` `schema_candidates` `sql_context_pack` |
| 9 | 生成 · 门禁 · 执行 | 8 | `sql_review` `sql_plan` `sql_generate` `sql_execute_readonly` `sql_run_get` `sql_run_list` `sql_run_replay` `sql_optimize` |

合计 **4+5+6+4+3+3+7+5+8 = 45**。

> 约定：入参列 `名: 类型 = 默认`，标 ★ 为必填；`dataset` 多数默认 `B`（裸库 A 用于验证换库）。所有输入输出均为 JSON。

#### 域 1 · 健康与数据集（4）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `gateway_health` | — | `{status, service, version, datasets[], components{meta_db, attachments, knowledge}}` | 全局探活；先跑它再谈别的 |
| `datasets` | — | `{datasets:[{key, label, 模型数, 字段数, 关系数}]}` | 确认双库在册、选库 |
| `knowledge_health` | — | `{ok, 可见数据集, 已解析文档数}` | 知识底座可用性 |
| `schema_version` | `dataset="B"` | `{schema_version, 表数, 字段数}` | 取最近一次 Schema 快照版本号 |

#### 域 2 · 兜底规划与 Wren 直通（5）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `plan` | ★`nl`, `dataset="B"` | `{blocked, intent, sql, objects, reason, steps}` | 确定性 NL→SQL（封闭世界，未命中即拒） |
| `wren_manifest` | `dataset="B"` | `{models[], relationships[]}` | 读 MDL 语义层清单 |
| `wren_dry_run` | ★`sql`, `dataset="B"` | `{ok, message, plan?}` | 语义层闭集预演（门禁 L4） |
| `wren_query` | ★`sql`, `dataset="B"` | `{ok, row_count, columns, data, dtypes}` | 只读执行（先过只读门禁，再过 dry_run） |
| `ask` | ★`nl`, `dataset="B"` | `{blocked, …, row_count, result}` | 一句话问数：规划→门禁→只读执行 |

> `plan` / `ask` **只走确定性规划器**，不做语义分析与确认流转；后者是 `analysis_first_round` 的职责。

#### 域 3 · 需求受理（5）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `demand_create` | ★`title` ★`business_context` ★`description` ★`expected_output` ★`contact`, `time_range=None`, `expected_finish_at=None`, `attachments=None`, `actor=""` | 需求单（**默认掩码版**）；四道校验不过 → `{ok:false, rejected:true, reason}` | 提交门户 |
| `demand_get` | ★`demand_id`, `reveal=false`, `actor=""` | 单据详情 + 流转事件；`reveal=true` 返回原文并写 `reveal_original` 留痕 | 查单 |
| `demand_list` | `status=None`, `limit=20`, `offset=0` | 需求单列表（可按状态过滤） | 看板 |
| `demand_summarize` | `demand_id=None` | 给单号=单汇总；不给=全局看板 | 管理 |
| `demand_set_status` | ★`demand_id` ★`status`, `note=None`, `actor=""` | 状态流转，不可覆盖、逐条留痕 | 回退闭环 |

状态取值：`待分析 / 分析中 / 待业务确认 / 待补充修改 / 待审核通过 / 已通过 / 已退回`。

#### 域 4 · 知识检索（2）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `knowledge_search` | ★`question`, `top_k=5`, `threshold=0.1`, `vector_weight=0.7`, `demand_id=""`, `round_no=0` | 带来源引用：`document_name` / `chunk_id` / `positions`；传 `demand_id` 时把引用登记进 `knowledge_citations` | 取证 |
| `knowledge_documents` | `limit=50` | 已入库文档（名称 / 解析状态 / 分块数） | 知识储备 |

> 实测两处：`threshold` **必须 >0**（传 0 会被按 falsy 回退到 0.2）；`vector_weight` 默认 0.7（官方 0.3 偏关键词，中文长问句易漏召回）。

#### 域 5 · 元数据字典（3）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `metadata_lookup` | `table=None`, `dataset=None`, `keyword=None`, `include_hidden=false` | 表中文名 / 颗粒度 / 字段清单；`include_hidden=true` 带出未建模列并标"AI 不可见" | 数据字典 |
| `metadata_glossary` | `term=None`, `keyword=None` | 业务口径词表（来源标 `mdl` / `manual`） | 数据字典 |
| `metadata_collect` | `dataset="B"`, `engine="native"` | 采集物理结构入字典 + 一致性自检 | 数据源接入 |

#### 域 6 · 附件（3）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `attachment_put` | ★`filename` ★`content_base64`, `demand_id=None` | `{ok, attachment}`；文本类（csv/txt/md/json）上传即敏感扫描，命中给 `risk` | 随单附件 |
| `attachment_list` | `demand_id=None` | `{ok, prefix, objects[]}` | 列附件 |
| `attachment_url` | ★`object_key`, `expires_seconds=3600` | `{ok, url}` 临时下载链接 | 前端直连下载 |

#### 域 7 · 语义分析与确认闭环（7）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `analysis_first_round` | ★`demand_id`, `dataset="B"`, `actor=""` | 六槽位 / `evidence_chain` / `rule_check`(R1–R7) / `questions` / `dropped_questions` / `conflicts` / `degraded_sources` / `round_no`。**轮次只增不改** | 跑一轮分析 |
| `analysis_rounds` | ★`demand_id` | 全部轮次元信息 | 看轮次 |
| `analysis_get` | ★`demand_id`, `round_no=None` | 某轮完整结果（默认最新） | 看结论 |
| `analysis_evidence` | ★`demand_id`, `round_no=None`, `level=None` | 证据链，可按 P1–P9 过滤 | 看证据 |
| `confirmation_generate` | ★`demand_id`, `dataset="B"`, `actor=""` | **只刷新待确认问题**，不新增轮次 | 重问 |
| `confirmation_list` | ★`demand_id`, `include_history=false` | 确认问答；`include_history=true` 回放全版本 | 看问答 |
| `confirmation_answer` | ★`demand_id` ★`question_id` ★`answer`, `choice=None`, `actor=""` | 答复；**不覆盖历史**，插 `version+1` 并以 `supersedes` 指旧版 | 业务答复 |

> 两类消费者别混发：`slots` 是**分析师视角**（含物理表名/字段名），`questions` 是**业务视角**（已过技术词闸门）。

#### 域 8 · 结构化需求与上下文包（5）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `schema_scan` | `dataset="B"`, `persist=true` | 快照 + 稳定版本号 `schema_version`（同库两次采集必须同值） | 数据源接入 |
| `requirement_structured` | ★`demand_id`, `dataset="B"` | 结构化技术需求对象 + `contract_ok` / `contract_errors`；版本化只增不改 | 合成需求 |
| `requirement_get` | ★`demand_id`, `version=None` | 取某一版结构化需求（默认最新） | 取需求 |
| `schema_candidates` | ★`demand_id`, `dataset="B"` | 主题表 / 关联路径 / 时间字段候选；只在 MDL 闭集内产生，未命中给 `miss_reason` | 生成准备 |
| `sql_context_pack` | ★`demand_id`, `dataset="B"` | SQL 上下文包 + 溯源三件套 `schema_version` / `requirement_version` / `pack_version` | 生成准备 |

#### 域 9 · 生成 · 门禁 · 执行（7）

| 工具 | 入参 | 返回要点 | 用途 |
|---|---|---|---|
| `sql_review` | ★`sql`, `dataset="B"` | 五层门禁审查 `{status, layers[]}`：L1 语法 / L2 静态规则（分级阻断）/ L3 只读 / L4 语义 / L5 结果断言 | 门禁 |
| `sql_plan` | ★`demand_id`, `dataset="B"` | 计划草稿（不含 SQL 正文）；选不出唯一解给候选集合并标「需人工审核」 | 生成第 1 段 |
| `sql_generate` | ★`demand_id`, `dataset="B"`, `candidate_sql=None` | SQL 初稿；`candidate_sql` 为空则回落确定性规划器；多条且差异过大时 `hold` | 生成第 2 段 |
| `sql_execute_readonly` | ★`demand_id`, `dataset="B"`, `sql=None`, `actor=""` | 先过五层门禁，全过才执行；每次写一条 `sql_runs`（溯源齐全，可回放） | 执行 |
| `sql_run_get` | ★`demand_id` | 该需求全部运行记录（生成 SQL / 门禁结果 / 结果校验 / 溯源版本号） | 联调交付 |
| `sql_run_list` | `demand_id=""`, `dataset=""`, `limit=20` | 跨需求执行留痕列表（时间倒序，可筛选） | 审计 |
| `sql_run_replay` | ★`demand_id`, `version=0` | 按版本精确复原当时的输入 → pack_version → SQL → 审查 → 结果 | 审计回放 |

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

# 2) 握手 + 取工具面（应返回 45 个工具）
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

### 5.6 MCP 操作指引与版本升级对照

<a id="mcp-ops"></a>

#### 5.6.1 接入前后自查（3 步）

1. `curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool` → `status` 应为 `ok`（知识库单独展示，不可用不拖垮整体，会如实进 `degraded`）；
2. 客户端 `tools/list` → 应为 **45 个**；
3. 冒烟：调 `datasets` 看双库（A / B）是否都在册。

#### 5.6.2 常用运维动作

```bash
docker compose ps                 # 7 服务是否全 healthy
docker compose up -d --build      # 改过 gateway/ 后重建
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep   # 双探活（HTTP + MCP，含真调一次 gateway_health）
python3 tools/mcp_acceptance_check.py                                 # 工具面 45 个逐条核对
```

> ⚠️ 容器内代码平铺 `/app`，但 `tools/` 不在 build context —— 跑容器内脚本前需 `docker cp`；走 HTTP 的脚本**必须在宿主跑**，容器内会 connection refused。

#### 5.6.3 版本标识对照（升级时最易搞混的一张表）

| 版本标识 | 出现在哪 | 含义 | 何时变 |
|---|---|---|---|
| `GATEWAY_VERSION`（当前 `0.3.0`） | `gateway_health.version` / 启动日志 | **网关产品版本** | 每期里程碑 |
| `serverInfo.version`（当前 `4.0.10`） | MCP `initialize` 握手 | **FastMCP 库版本**（`FastMCP(name)` 未显式传版本，取库默认） | 升级 `fastmcp` 依赖 |
| `rules_version` | `sql_review` / `sql_runs` | L2 规则集内容哈希 | 改 `gateway/rules.py` |
| `schema_version` | `schema_scan` / 上下文包 | 该库「表.列:类型」排序清单 sha256 前 8 位 | 库结构变化 |
| `requirement_version` | `requirement_*` | 结构化需求版本（只增不改） | 每次合成 |
| `pack_version` | `sql_context_pack` | sha256(schema_version + requirement_version + 规则集版本) 前 8 位 | 上述任一变化 |
| 镜像标签 `askoda-gateway:0.3.0` | `docker-compose.yml` | **与 `GATEWAY_VERSION` 保持一致** | 每期里程碑 |

> 关键提醒：**MCP 握手报的 `4.0.10` 是 FastMCP 库版本，不是网关版本**；对外讲版本请以 `gateway_health.version`（当前 `0.3.0`）为准。

#### 5.6.4 升级操作清单（工具面变更必做）

改 `gateway/app.py`（增删 `@mcp.tool`）后，按序执行：

1. **同批更新文档**：项目文档工作空间《MCP 工具契约与注册说明》的逐工具契约与域计数 → 本 README 的双语域计数表与逐域工具表；
2. **重建镜像**：`BUILDX_CONFIG="$PWD/.buildx" docker compose build gateway && docker compose up -d`；
3. **核验工具面**：`tools/list` 的数量与清单须与文档一致（`python3 tools/mcp_acceptance_check.py`）；
4. **跑回归**：`python3 tools/gate_all.py`（G0–G3 四层门禁），确认基线不退化；
5. **同步镜像标签**与 `.env`（如涉及新环境变量）。

> 联动红点（改工具面会牵动这些断言 / 清单，出改单时要一并扫）：`tools/mcp_acceptance_check.py` 的**计数与清单断言**、契约文档的域计数表、本 README 双语域计数表。

---

### 5.7 MCP 人工验证

要按「最低成本、逐个确认可用」手工验一遍全部 45 个工具，步骤见项目文档工作空间的《MCP 服务人工验证方案》。最快路径：先跑一次 `tools/mcp_acceptance_check.py` 拿到机器级结论，再沿 5.5 的八步链路建**一个**需求单走通主干（覆盖约 30 个工具），最后补齐无依赖探针与负向抽查。

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

# MCP 工具面验收（45 个工具 + 真实调用比对，对照《实施推进与验收方案》）
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

### 6.2 统一门禁（G0–G3 四层）

```bash
# 一条命令跑完四层门禁，产出 gate-report.json / gate-report.md（均为运行产物，不入库）
python3 tools/gate_all.py

# 日常快跑：只跑静态 + 单元两层（不写库、不碰真链路）
python3 tools/gate_all.py --layers G0,G1

# 总验收：所有红项一律阻塞（忽略「已知基线红」豁免表）
python3 tools/gate_all.py --strict
```

| 层 | 内容 | 是否写库 |
|---|---|---|
| **G0** | 静态自检（语法编译 + 依赖基线） | 否 |
| **G1** | 单元自证（`*_selftest.py`） | 否 |
| **G2** | 独立复核（`*_check.py`） | 否 |
| **G3** | 真实链路（探活 + 端到端） | **是**，慎跑 |

> 门禁按命名约定**自动发现**同目录脚本：`*_selftest.py` → G1、`*_check.py` → G2、`*_verify.py` → G3。退出码即准入结论：0 全绿，1 有红项。

### 6.3 端到端演练

```bash
python3 poc-eval/poc_e2e_gateway.py
# 产出 poc-eval/e2e-gateway-report.json（机器可读，运行产物，不入库）
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

| 里程碑 | 交付内容 | 验收结论 | 结果（真实数值） |
|---|---|---|---|
| **M1** · 网关骨架 | MCP 7 工具 + Wren 双库接入 + 确定性 ask 兜底 | 已验收 | 7/7 ✅ |
| **M2** · 需求受理 | 需求 CRUD / 脱敏 / 元数据 / 附件 / 知识库 14 工具 | 已验收 | 22/22 ✅ |
| **M3** · 语义分析 + 确认闭环 | 证据编排 P1–P9 + 规则 R1–R7 + 7 工具 | 已验收 | 109/109 ✅ |
| **M4** · 两段式 SQL 生成 | plan() → generate() + 闭集自检 + pack_version 哈希 | 已验收 | 51/0 ✅（库 A） |
| **M5** · 五层门禁 + 纯规则库 | sqlglot + R1–R7 规范化 + 只读 + dry-plan + 结果断言 | 已验收 | 62/0 ✅（库 B） |
| **M6-1** · 规则版本指纹 | `rules_version` 含每条 `evaluate` 的 `source_sha8` | 已验收 | ✅ |
| **M6-2** · 失败回退矩阵 F1–F5 | 分类纯函数 + F2 判据修正（按 `chosen_table` 空） | 已验收 | ✅ |
| **M6-3** · 健壮性（超时 / 重试 / 并发） | 超时三级配置 + READ_TOOLS 3 次退避 + run_id 唯一 | 已验收 | 18/18 ✅ |
| **M6-4** · 审计留痕 + 回放 | actor 透传 + knowledge_citations + 列表按 dataset 反查 + 五段链条回放 | 已验收 | B 段 16/16 ✅ |
| **M6-5** · M6 总验收 | M6 验收单 9 项（规则可枚举 / 正反例 / 5 类回退 / 连续 20 次 / 四类留痕 / 回放 / 零回归 / 换库 0 代码） | 已验收 | 9/9 ✅ |

> 上表数值均为各里程碑验收当时**实测记录**，原始验收单与逐条断言留档在项目文档工作空间。
> 各里程碑的一次性验收脚本（`m2_verify` / `m5_verify` / `audit_verify` 等）属开发过程产物，**不随本仓库分发**——它们绑定当时的容器内部结构，对外部使用者已无复现价值。当前可复现的验证入口见第六章（统一门禁 + 离线自证 + 工具面核验）。

### 7.2 POC 题库验收

| 题库 | 正向准确率 | 拒绝覆盖率 | POC-3 陷阱拦截率 |
|---|---|---|---|
| POC-1（30 条） | 20/20 = 100% | 10/10 = 100% | — |
| POC-3（10 条） | — | — | 10/10 = 100% |

### 7.3 技术债务清单（已知 · 非阻塞）

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
| HC-07 | 九字段留痕一行都不能缺 | `tools/gate_all.py` G3 真实链路断言 |
| HC-08 | actor 必须透传写入 `sql_runs.actor`，不许用默认值 | `tools/gate_all.py` G3 留痕断言 |
| HC-09 | 筛选异常时严禁静默降级成全量 | `tools/gate_all.py` G3 dataset 三态对照 |

### 8.3 验收新功能的三步法

1. **先写自证脚本**，放 `tools/<feature>_selftest.py`（G1 自动发现）或 `<feature>_check.py`（G2 自动发现），全是断言；
2. **再改代码**，改完先跑 `ast.parse` 全量语法检查；
3. **最后跑 `python3 tools/gate_all.py --strict`**，四层门禁全绿才算过。

---

## 九、路线图（Roadmap）

### 短期（v0.4 · 阶段二收尾 → 阶段三）

- [x] **M6 · 规则体系 + 健壮性 + 留痕回放**（M6-0…M6-5）：规则库扩充（一对多 / 时间口径 / 装载日期 / DISTINCT 滥用）+ 失败回退矩阵 F1–F5 + 超时 / 重试 / 并发 + 审计回放；**M6-5 总验收 9/9 全过**。含 POC-3 规则补强（L2 分级阻断 + 枚举值校验）：陷阱拦截率 70% → 100%、误拦 0%
- [ ] **M7 · MCP 工具面完善 + 前后端贯通**（关卡二）：45 工具契约收口 + Agent 仅凭一句业务需求自主编排串通全链路 + 最小前端闭环
- [ ] **M8 · 五菜单工作台 + 交互**（客户化设计）：语义层 · MDL 字典 / 知识储备 / 数据源接入 / 需求分析 · SQL 生成 / SQL 联调 · 交付
- [ ] **M9 · 真实业务系统接入 + 真实场景验收测试**

### 中期（v0.5 · 规划中）

- [ ] 语义层 MDL 在线 Editor + PR 式变更审批流
- [ ] SQL 审查不通过后的一期自动改写（治理指引 `how_to_fix` → 自动 patch）
- [ ] 数据集注册接口（当前走 `gateway/registry.py` 代码注册）
- [ ] FastMCP 5.x 升级 + `create_proxy` 官方能力回归（去掉手写 Wren 客户端）
- [ ] **本体原生查询（图查询 / Cypher）试点**：行动层在 SQL 之外增加一条本体原生路径（当前未实现）

### 长期（v1.0 · 概念阶段）

- [ ] 多租户 + 按 dataset 的细粒度 RBAC
- [ ] **标准本体表达栈（RDF / OWL / SKOS / SWRL 等）与推理机**：让「事理层」从 MDL 演进为可推理本体
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
