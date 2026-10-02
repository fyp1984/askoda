# -*- coding: utf-8 -*-
"""数据需求智能分析助手 · 服务端网关（M1 骨架 + M2 受理与知识底座 + M3 语义分析与确认闭环）

职责
----
1. MCP 服务端：以 FastMCP 暴露应用级工具，供豆包 / Codex / WorkBuddy 等 Agent 调用。
2. 双库接入：经手写 streamable-http client 对接 A / B 两套 Wren MCP（换库只换注册项）。
3. 健康探活：`GET /healthz` 返回网关自检 + 各数据集 Wren + 元数据库 + 附件存储 + 知识库。
4. 确定性兜底：`plan` 工具提供封闭世界 NL→SQL（LLM 路径不可用时的保底）。
5. 只读保护：`query` 工具仅放行 SELECT / WITH。
6. **需求受理（M2）**：`demand_*` 四工具 + 入口侧敏感掩码 + 四道提交校验 + 落库留痕。
7. **知识底座（M2）**：`knowledge_*` 引用式检索（RAGFlow 原生接口，零代码侵入）。
8. **元数据字典（M2）**：`metadata_*` 按表查中文名与口径，MDL 语义与物理结构合流。
9. **附件（M2）**：`attachment_*` 接入 MinIO，高风险附件上传即提示。
10. **语义分析与确认闭环（M3）**：`analysis_*` 第一轮语义分析（六槽位 + 证据链 + 规则
    校验 + 置信度）、`confirmation_*` 待确认问题与版本化答复。

关于 LLM 归属（重要，勿误读）
----------------------------
**网关侧不内置 LLM。** 语义分析在网关内以「证据编排 + 确定性规则」落地，结论一律带
来源与置信度；需要自由生成的部分（如拗口需求的口语化改写）由**客户端 Agent** 承担。
因此 M3 的验收与 30 题回放可以在任意 Agent（豆包 / Codex / WorkBuddy）上跑，
不存在"必须先选定某个模型"这类前置条件。

里程碑落点
----------
- M4：结构化技术需求 + SQL 上下文包。工具已在本文件注册，实现见 gateway/schema_scan.py
  / requirement.py / sqlpack.py（由编码侧交付；模块未就绪时工具返回 not_ready，不影响启停）。
- M5：SQL 生成 + 五层门禁 + 只读执行。工具已在本文件注册，实现见 gateway/gates.py
  / sqlgen.py / sqlrun.py。
- M6 起：图表与可视化交付（未开工）。

运行
----
    python app.py            # 默认 0.0.0.0:8080，MCP 路径 /mcp，健康检查 /healthz
环境变量：
    WREN_A_URL / WREN_B_URL / MDL_A_PATH / MDL_B_PATH            —— 语义层双库
    ASSISTANT_DB_DSN                                             —— 网关自有元数据库
    MINIO_ENDPOINT / MINIO_ACCESS_KEY / MINIO_SECRET_KEY         —— 附件存储
    KNOWLEDGE_API_URL / KNOWLEDGE_API_KEY / KNOWLEDGE_DATASET_ID —— FileBay 知识底座
    GATEWAY_HOST / GATEWAY_PORT
"""
import json
import os
import time
import urllib.error
import urllib.request

from starlette.responses import JSONResponse

from fastmcp import FastMCP

import analysis as analysis_mod
import attachments
import collect as collect_mod
import db
import demand as demand_mod
import knowledge
import metadata as metadata_mod
import planner as planner_mod
import registry
from wren import WrenClient, WrenError

GATEWAY_NAME = "数据需求智能分析助手 · 网关"
GATEWAY_VERSION = "0.3.0"

HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.getenv("GATEWAY_PORT", "8080"))
MCP_PATH = os.getenv("GATEWAY_MCP_PATH", "/mcp")

mcp = FastMCP(GATEWAY_NAME)

_clients = {}


def client_for(dataset):
    """按数据集惰性创建并复用 Wren 客户端。"""
    if dataset.key not in _clients:
        _clients[dataset.key] = WrenClient(dataset.wren_url, name=dataset.key)
    return _clients[dataset.key]


def _probe(dataset):
    """探测单个数据集的 Wren 连通性。"""
    t0 = time.time()
    try:
        txt = client_for(dataset).health()
        return {
            "dataset": dataset.key,
            "ok": True,
            "endpoint": dataset.wren_url + "/mcp",
            "response": txt[:160],
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "dataset": dataset.key,
            "ok": False,
            "endpoint": dataset.wren_url + "/mcp",
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "ms": int((time.time() - t0) * 1000),
        }


# ---------------------------------------------------------------------------
# HTTP 探活端点
# ---------------------------------------------------------------------------
def _probe_components():
    """M2/M3 三个外部依赖的连通性：元数据库 / 附件存储 / 知识库。"""
    meta = db.healthy()
    if meta.get("ok"):
        # 把元数据库自身的规模一并带出：M3 之后 analysis_rounds / confirmations 的
        # 行数是"确认闭环有没有真的在跑"最直接的观测点。
        try:
            meta["tables"] = db.table_stats()
        except Exception as e:  # noqa: BLE001
            meta["tables_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    return {
        "meta_db": meta,
        "attachments": attachments.health(),
        "knowledge": knowledge.health(),
    }


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request):
    datasets = [_probe(ds) for ds in registry.DATASETS.values()]
    comps = _probe_components()
    # 判定口径：网关自身 + 语义层双库 + 元数据库 + 附件存储决定 ok/degraded；
    # 知识库（FileBay 外栈）单独展示——它不可用时问数链路仍应可用，故不拖垮整体状态。
    core_ok = (
        all(d["ok"] for d in datasets)
        and comps["meta_db"].get("ok")
        and comps["attachments"].get("ok")
    )
    body = {
        "status": "ok" if core_ok else "degraded",
        "service": GATEWAY_NAME,
        "version": GATEWAY_VERSION,
        "env": {
            "host": HOST,
            "port": PORT,
            "mcp_path": MCP_PATH,
            "datasets_registered": sorted(registry.DATASETS),
        },
        "datasets": datasets,
        "components": comps,
    }
    return JSONResponse(body, status_code=200 if core_ok else 503)


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------
@mcp.tool
def gateway_health() -> dict:
    """网关健康检查：返回网关自检状态、各数据集 Wren 语义层连通性、M2 依赖组件连通性。"""
    datasets = [_probe(ds) for ds in registry.DATASETS.values()]
    comps = _probe_components()
    return {
        "status": "ok" if all(d["ok"] for d in datasets) else "degraded",
        "service": GATEWAY_NAME,
        "version": GATEWAY_VERSION,
        "datasets": datasets,
        "components": comps,
    }


@mcp.tool
def datasets() -> dict:
    """列出已注册数据集及其语义层规模（模型数 / 字段数 / 关系数）。"""
    return {"datasets": [ds.describe() for ds in registry.DATASETS.values()]}


@mcp.tool
def plan(nl: str, dataset: str = "B") -> dict:
    """确定性 NL→SQL 兜底规划（封闭世界：只引用 MDL 可见对象，未命中即拒）。

    适用：LLM 语义分析路径不可用、或需闭集保底时。
    不适用：需要跨域推理、口径解释类问题——走 M3 的语义分析服务。
    """
    ds = registry.get(dataset)
    return planner_mod.plan(ds, nl)


@mcp.tool
def wren_manifest(dataset: str = "B") -> dict:
    """读取指定数据集的 MDL 语义层清单（模型 / 字段 / 关系）。"""
    ds = registry.get(dataset)
    return {
        "dataset": ds.key,
        "label": ds.label,
        "models": ds.mdl.get("models", []),
        "relationships": ds.relationships,
    }


@mcp.tool
def wren_dry_run(sql: str, dataset: str = "B") -> dict:
    """语义层门禁预演：校验 SQL 引用对象是否全部落在 MDL 可见闭集内。"""
    ds = registry.get(dataset)
    return {"dataset": ds.key, **client_for(ds).dry_run(sql)}


@mcp.tool
def wren_query(sql: str, dataset: str = "B") -> dict:
    """只读执行 SQL（仅放行 SELECT / WITH），返回结构化结果。

    执行前先过只读门禁，再过语义层 dry_run；两者都通过才真正执行。
    """
    ds = registry.get(dataset)
    ok_read, msg = planner_mod.is_readonly(sql)
    if not ok_read:
        return {"ok": False, "stage": "readonly-gate", "error": msg}
    dry = client_for(ds).dry_run(sql)
    if not dry["ok"]:
        return {"ok": False, "stage": "semantic-gate", "error": dry["message"]}
    try:
        result = client_for(ds).query(sql)
    except WrenError as e:
        return {"ok": False, "stage": "execute", "error": str(e)[:300]}
    return {
        "ok": True,
        "dataset": ds.key,
        "row_count": len(result.get("data", [])),
        "columns": result.get("columns", []),
        "data": result.get("data", []),
        "dtypes": result.get("dtypes", {}),
    }


@mcp.tool
def ask(nl: str, dataset: str = "B") -> dict:
    """一句话问数（确定性路径）：规划 → 门禁 → 只读执行，返回 SQL 与结果。

    本工具**只走确定性规划器**，不做语义分析与确认流转。原因是计划本身在语义层
    门禁面前是"能不能出数"的问题，而"业务想问的到底是什么"属于 M3 的职责——
    后者要用 `analysis_first_round`，它会把含糊之处列成待确认问题而不是猜一个答案。

    注意：网关侧不内置 LLM，本工具与 M3 都不依赖任何特定模型；如需口语化改写或
    自由生成，由调用方（客户端 Agent）自行完成。
    """
    ds = registry.get(dataset)
    planned = planner_mod.plan(ds, nl)
    if planned.get("blocked"):
        return {"blocked": True, **planned}
    ok_read, msg = planner_mod.is_readonly(planned["sql"])
    if not ok_read:
        return {"blocked": True, "dataset": ds.key, "intent": "只读门禁拒绝",
                "reason": msg, "sql": planned["sql"]}
    dry = client_for(ds).dry_run(planned["sql"])
    if not dry["ok"]:
        return {"blocked": True, "dataset": ds.key, "intent": planned["intent"],
                "reason": "语义层门禁未通过：" + dry["message"], "sql": planned["sql"]}
    try:
        result = client_for(ds).query(planned["sql"])
        exec_error = None
    except WrenError as e:
        result, exec_error = None, str(e)[:300]
    return {
        "blocked": False,
        **planned,
        "dry_run": dry,
        "row_count": len((result or {}).get("data", [])),
        "result": result,
        "exec_error": exec_error,
    }


# ---------------------------------------------------------------------------
# M2 · 需求受理（PRD §9.2）
# ---------------------------------------------------------------------------
@mcp.tool
def demand_create(
    title: str,
    business_context: str,
    description: str,
    expected_output: str,
    contact: str,
    time_range: str | None = None,
    expected_finish_at: str | None = None,
    attachments: list | None = None,
    actor: str = "",
) -> dict:
    """受理一条数据需求：四道校验 → 敏感掩码 → 落库 → 留痕。

    `actor` = **提交人身份**（PRD §13 第①类留痕）；不传时落默认 `business`。
    真实身份由调用方（上层 BFF / 客户端）传入，网关不猜。

    入参即 PRD §9.2 的提交字段：标题 / 业务背景 / 需求说明 / 输出预期 / 联系人 为必填，
    时间范围 / 期望完成时间 / 附件 为选填。核心三项（背景、说明、输出预期）缺失时**直接拒绝**。

    返回需求单（**默认掩码版**：手机号、身份证、银行卡、地址、姓名、账号标识已脱敏）。
    需要原文请用 `demand_get(reveal=True)`，该动作会在留痕表记录一次原文访问。
    """
    try:
        return demand_mod.create(
            {
                "title": title,
                "business_context": business_context,
                "description": description,
                "expected_output": expected_output,
                "contact": contact,
                "time_range": time_range,
                "expected_finish_at": expected_finish_at,
                "attachments": attachments or [],
            },
            actor=actor or "business",
        )
    except demand_mod.DemandError as e:
        return {"ok": False, "rejected": True, "reason": str(e)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def demand_get(demand_id: str, reveal: bool = False, actor: str = "") -> dict:
    """按需求单号取单据详情（含流转事件留痕）。

    默认返回**掩码版**；`reveal=True` 返回原文，并在事件表写入一条 `reveal_original` 记录。
    """
    try:
        return demand_mod.get(demand_id, reveal=reveal, actor=actor or "viewer")
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def demand_list(status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """列出需求单（可按状态过滤）。

    状态取值：待分析 / 分析中 / 待业务确认 / 待补充修改 / 待审核通过 / 已通过 / 已退回。
    """
    try:
        return demand_mod.list_demands(status=status, limit=limit, offset=offset)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def demand_summarize(demand_id: str | None = None) -> dict:
    """需求汇总：给单号则汇总该单（状态 / 敏感命中 / 事件分布），不给则出全局看板。"""
    try:
        return demand_mod.summarize(demand_id)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def demand_set_status(
    demand_id: str, status: str, note: str | None = None, actor: str = ""
) -> dict:
    """流转需求单状态（PRD §9.3 回退与修改闭环）。状态变更不可覆盖，逐条留痕。"""
    try:
        return demand_mod.set_status(
            demand_id, status, actor=actor or "analyst", note=note
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# M2 · 知识检索（RAGFlow 引用式召回）
# ---------------------------------------------------------------------------
@mcp.tool
def knowledge_search(
    question: str,
    top_k: int = 5,
    threshold: float = 0.1,
    vector_weight: float = 0.7,
    demand_id: str = "",
    round_no: int = 0,
) -> dict:
    """检索知识库（制度 / 口径 / 表样），返回**带来源的引用**。

    每条引用含 `document_name`（来源文档名）、`chunk_id` 与 `positions`（片段定位），
    可直接回溯到"哪篇文档、哪一段"；`answer_context` 是拼好的带标号证据块，供上层取证。

    参数说明（默认值经实测校准）：
    - `threshold` 相似度下限，**必须 >0**（传 0 会被服务端按 falsy 回退成 0.2）；
    - `vector_weight` 向量权重，默认 0.7；官方默认 0.3 偏重关键词，中文长问句易漏召回。
    """
    try:
        out = knowledge.search(
            question, top_k=top_k, threshold=threshold, vector_weight=vector_weight
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}
    # PRD §13 第③类留痕：调用方若指明了「替谁检索」（demand_id），把这次引用登记进
    # knowledge_citations，供事后回答「这条 SQL 引的是哪版知识」。
    # 不给 demand_id 就只检索、不留痕——分析链路内部的取证检索不应污染审计表。
    if demand_id and out.get("citations"):
        out["citations_recorded"] = _wired(
            "knowledge",
            "record_citations",
            demand_id,
            question,
            out["citations"],
            round_no=round_no or None,
        )
    return out


@mcp.tool
def knowledge_health() -> dict:
    """知识库连通性与数据集可见性（含已解析文档数）。"""
    return knowledge.health()


@mcp.tool
def knowledge_documents(limit: int = 50) -> dict:
    """列出知识库当前已入库文档（名称 / 解析状态 / 分块数）。"""
    try:
        return knowledge.list_documents(limit=limit)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# M2 · 元数据字典（MDL 语义 + 物理结构合流）
# ---------------------------------------------------------------------------
@mcp.tool
def metadata_lookup(
    table: str | None = None,
    dataset: str | None = None,
    keyword: str | None = None,
    include_hidden: bool = False,
) -> dict:
    """按表查元数据字典：表中文名、颗粒度、字段清单（中文名 / 口径 / 类型）。

    默认**只返回对 AI 可见的列**（即已建模列）；`include_hidden=True` 会带出物理存在但
    未建模的列，并标注「未建模：对 AI 不可见」——这些是刻意不对 AI 开放的敏感列。
    """
    try:
        return metadata_mod.lookup(
            table=table, dataset=dataset, keyword=keyword, include_hidden=include_hidden
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def metadata_glossary(term: str | None = None, keyword: str | None = None) -> dict:
    """查业务口径词表：口径定义与计算口径（来源标注 mdl / manual）。"""
    try:
        return metadata_mod.glossary(term=term, keyword=keyword)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def metadata_collect(dataset: str = "B", engine: str = "native") -> dict:
    """采集指定数据集的物理结构写入元数据字典，并做一致性自检。

    自检内容：MDL 里声明可见的字段，是否都能在物理库中找到（找不到说明 MDL 写错了表名/字段名）。
    """
    try:
        return collect_mod.collect(dataset, engine=engine, seed_mdl=True)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# M2 · 附件（MinIO）
# ---------------------------------------------------------------------------
@mcp.tool
def attachment_put(
    filename: str, content_base64: str, demand_id: str | None = None
) -> dict:
    """上传附件（内容用 base64 传入）。

    文本类附件（csv / txt / md / json）上传时即做敏感信息扫描，命中则在返回的
    `risk` 字段里给出提示——按 PRD §9.2，这类附件需要提交人确认后才继续流转。
    传入 `demand_id` 时对象键会挂到该需求单下，便于随单追溯。
    """
    import base64

    try:
        data = base64.b64decode(content_base64, validate=False)
    except Exception as e:
        return {"ok": False, "error": "base64 解码失败：%s" % str(e)[:200]}
    try:
        meta = attachments.put(demand_id or "unbound", filename, data)
        return {"ok": True, "attachment": meta}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def attachment_list(demand_id: str | None = None) -> dict:
    """列出已上传附件（可按需求单号过滤）。"""
    try:
        prefix = (demand_id + "/") if demand_id else ""
        return {"ok": True, "prefix": prefix, "objects": attachments.list_objects(prefix)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def attachment_url(object_key: str, expires_seconds: int = 3600) -> dict:
    """生成附件的临时下载链接（默认 1 小时），供前端直连下载。"""
    try:
        return {"ok": True, "url": attachments.presigned(object_key, expires_seconds)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# M3 · 语义分析与确认闭环
#
# 证据优先级阶梯（P1–P8，低优先级不得覆盖高优先级，冲突必须报出而非静默择一）：
#   P1 当前业务说明 > P2 业务确认 > P3 表样 > P4 历史案例 > P5 系统资料
#   > P6 数据字典 > P7 表关系 > P8 通用规则     （P9 模型推断仅作兜底，不入编号）
#
# ⚠️ 基线冲突（已报出，待评审裁定）：《最终技术方案》§2.3 标题写"P1–P8"却并列了
#    9 项；PRD §7.1 另给 6 级且把"已审核知识条目"排在"历史案例"之前。
#    本实现按 §2.3 的**前 8 项**落地，阶梯定义集中在 `evidence.LADDER` 一处，
#    改判后只改那一处即可。
# ---------------------------------------------------------------------------
@mcp.tool
def analysis_first_round(demand_id: str, dataset: str = "B", actor: str = "") -> dict:
    """跑第一轮语义分析：取证据（P1–P8）→ 六槽位结论 → 规则校验 → 生成待确认问题 → 落库。

    六槽位：主体 / 颗粒度 / 时间口径 / 范围条件 / 输出字段 / 风险点。
    **每条结论都带 `evidence`（来源与定位）和 `confidence`（置信度）**，不出现"无源断言"。

    分析用的是需求单的**掩码版**正文——分析链路不需要原文，原文只在受控取原文时展开。

    ⚠️ 两类消费者的边界（勿混发）
    ----------------------------
    - `slots` 是**分析师视角**：里面会出现物理表名、字段名、主键这类技术表述
      （例如"一行 = 一条 ads_member_repurchase_di 记录（主键 member_id）"），
      目的是让分析师能核对口径，**不要直接转给业务**；
    - `questions` 是**业务视角**：已过技术词闸门，只含业务能作答的话，
      可以直接展示给业务。

    返回值要点：
    - `slots` / `slot_labels`：六槽位结论与中文名；
    - `evidence_chain` / `evidence_sources`：证据链与各来源取数状态；
    - `degraded_sources`：**取不到的证据来源**。这里非空意味着相应判定未执行，
      绝不能读成"没问题"——例如 P7 缺失时 R1（一对多）实际未生效；
    - `rule_check`：R1–R7 触发情况；
    - `questions`：待确认问题（**只含影响结果的问题，不含表名/字段名等技术问题**）；
    - `dropped_questions`：被闸门拦下的问题及拦截原因（透明披露，不静默丢弃）；
    - `conflicts`：同槽位不同优先级结论不一致之处。

    **轮次只增不改**：重复调用会生成 `round_no+1`，上一轮原样保留，便于回退确认后并排比对。
    """
    try:
        return analysis_mod.first_round(
            demand_id, dataset=dataset, actor=actor or "analyst"
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def analysis_rounds(demand_id: str) -> dict:
    """列出该需求的全部分析轮次（元信息）：轮次号、证据条数、问题数、主体、阻断项数。"""
    try:
        return analysis_mod.rounds(demand_id)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def analysis_get(demand_id: str, round_no: int | None = None) -> dict:
    """取某一轮的完整结果（默认最新一轮）：槽位、证据链、规则校验、确认问题。"""
    try:
        return analysis_mod.get_round(demand_id, round_no)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def analysis_evidence(demand_id: str, round_no: int | None = None, level: str | None = None) -> dict:
    """取某一轮的证据链，可按优先级过滤（level 取 P1–P9）。

    每条证据含 `level`（优先级）/ `source`（来自哪个来源）/ `locator`（定位，可回溯）/
    `content`（内容），与《最终技术方案》§5.2.2 的 `evidence_chain` 对齐。
    """
    try:
        return analysis_mod.evidence_chain(demand_id, round_no, level)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def confirmation_generate(demand_id: str, dataset: str = "B", actor: str = "") -> dict:
    """**只刷新待确认问题**，不新增分析轮次。

    适用：知识库或元数据更新后想重新提问。与 `analysis_first_round` 的分工是
    "跑一轮分析" vs "只把这一轮的问题重问一遍"——后者不堆轮次。

    问题文本若发生变化，原答复**不会**被当作当前结论：会插入一版未答复的新问题，
    并在事件表记 `confirmation_invalidated`，旧答复仍留在历史里可查。
    """
    try:
        return analysis_mod.generate_confirmations(
            demand_id, dataset=dataset, actor=actor or "analyst"
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def confirmation_list(demand_id: str, include_history: bool = False) -> dict:
    """列出该需求的确认问答。

    默认只返回**当前版本**；`include_history=True` 连历史版本一起返回，用于回放
    "问题 / 答复 / 时间 / 版本"四要素。**确认记录版本化、不可覆盖**。
    """
    try:
        return analysis_mod.list_confirmations(demand_id, include_history)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def confirmation_answer(
    demand_id: str,
    question_id: str,
    answer: str,
    choice: str | None = None,
    actor: str = "",
) -> dict:
    """答复一条确认问题（业务侧动作）。

    `answer` 是自由文本，`choice` 是所选项（两者可只给其一）。**不覆盖历史**：
    插入 `version+1` 的新版本并用 `supersedes` 指向被取代的那版；答复与当前版本
    一致时按 noop 处理，不无谓地堆版本。

    答复后需求状态转「待补充修改」，等分析人员据答复重新整理。
    """
    try:
        return analysis_mod.answer(
            demand_id, question_id, answer, actor=actor or "business", choice=choice
        )
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


# ---------------------------------------------------------------------------
# M4/M5 · 接线层（工具只注册、不实现）
#
# M4/M5 的实现代码在 gateway/{schema_scan,requirement,sqlpack,gates,sqlgen,sqlrun}.py，
# 由编码侧交付。本文件**只做注册与转发**——惰性导入 + 统一异常兜底，换来两个好处：
#   1. 模块尚未落盘时服务照常启停，调用时返回 not_ready，而不是把整个网关带崩；
#   2. 模块一旦就位即自动生效，**无需再改本文件**，避免"多个任务同时改 app.py"的冲突。
# 每个工具对底层模块的函数签名，就是本层与实现层之间的接口契约。
# ---------------------------------------------------------------------------
def _wired(modname, funcname, *args, **kwargs):
    """惰性调用 M4/M5 模块函数；模块不可用时给明确提示，而不是抛异常。

    **版本容忍转发**：工具面（本文件）与实现层（gateway/*.py）是各自演进的，工具
    可能已先行带上实现层尚未支持的**关键字**入参（例如新增的身份参数 `actor`）。
    此时若照原样透传，会得到 TypeError 而把**本来跑得通的链路**打断——工具面先走
    一步不应该造成故障。因此这里按目标函数签名过滤掉它还不接受的关键字参数。

    边界（有意为之）：
    · **位置参数不做过滤**——滤掉会静默错位，比报错更危险；
    · 被丢弃的关键字会**打到 stderr**（docker logs 可见），不静默吞掉。
    """
    import importlib
    import inspect
    import sys

    try:
        mod = importlib.import_module(modname)
    except ImportError as e:
        return {
            "ok": False,
            "not_ready": True,
            "reason": "模块 %s 尚不可用：%s" % (modname, str(e)[:160]),
        }
    try:
        fn = getattr(mod, funcname)
        try:
            params = inspect.signature(fn).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                for k in [k for k in kwargs if k not in params]:
                    kwargs.pop(k)
                    print(
                        "[_wired] %s.%s 暂不接受关键字参数 %r，已跳过（工具面先行，实现层未落地）"
                        % (modname, funcname, k),
                        file=sys.stderr,
                    )
        except (TypeError, ValueError):
            pass
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])}


@mcp.tool
def schema_scan(dataset: str = "B", persist: bool = True) -> dict:
    """M4-1 · 扫描数据集 Schema，产出快照与**稳定版本号**。

    `schema_version` 是「按名排序后的 表.列:类型 清单」的 sha256 前 8 位——**同一库两次
    采集必须同值**，这是"可复现"的可证明点；`digest` 为完整 sha256。persist=True 时写入
    `schema_snapshots`（主键 dataset+schema_version，重复扫描幂等）。
    """
    return _wired("schema_scan", "scan", dataset, persist=persist)


@mcp.tool
def schema_version(dataset: str = "B") -> dict:
    """M4-1 · 取该数据集最近一次 Schema 快照的版本号与规模（表数 / 字段数）。"""
    return _wired("schema_scan", "latest_version", dataset)


@mcp.tool
def requirement_structured(demand_id: str, dataset: str = "B") -> dict:
    """M4-2 · 合成「结构化技术需求对象」并做契约校验。

    输入 = 该需求最新一轮语义分析（六槽位 + 证据链）与已确认答复；输出对象带
    `contract_ok` / `contract_errors`。**版本化、只增不改**：同一需求重复合成 = version+1。
    六槽位里取不到的项留空并进 `residual_risks`，不编造。
    """
    built = _wired("requirement", "build", demand_id, dataset=dataset)
    if built.get("ok") is False:
        return built
    checked = _wired("requirement", "validate", built)
    return {
        **built,
        "contract_ok": bool(checked.get("ok")),
        "contract_errors": checked.get("errors", []),
    }


@mcp.tool
def requirement_get(demand_id: str, version: int | None = None) -> dict:
    """M4-2 · 取结构化技术需求对象的某一版（默认最新版）。"""
    return _wired("requirement", "get", demand_id, version=version)


@mcp.tool
def schema_candidates(demand_id: str, dataset: str = "B") -> dict:
    """M4-3 · 在 MDL 闭集内给出主题表 / 关联路径 / 时间字段候选。

    候选**只在语义层闭集内产生**，不连物理库猜字段；未命中时给 `miss_reason`，**不猜、不填**。
    关联路径每条都能指回 `mdl.relationships` 的下标（可回溯）。
    """
    req = _wired("requirement", "get", demand_id)
    if req.get("ok") is False:
        return req
    return _wired("sqlpack", "candidates", dataset, req)


@mcp.tool
def sql_context_pack(demand_id: str, dataset: str = "B") -> dict:
    """M4-3 · 组装 SQL 生成上下文包（主题 / 颗粒度 / 关联 / 时间 / 过滤 / 聚合 / 模板 / 方言）。

    另带溯源字段：`schema_version` / `requirement_version` / `pack_version`
    （pack_version = sha256(schema_version + requirement_version + 规则集版本) 前 8 位）。
    """
    return _wired("sqlpack", "build", demand_id, dataset=dataset)


@mcp.tool
def sql_review(sql: str, dataset: str = "B") -> dict:
    """M5-1 · 五层门禁审查 SQL（生成与审查分离）。

    L1 语法（SQLGlot parse，失败即阻断）／L2 静态风险（无 ON 的 JOIN、SELECT *、
    GROUP BY 与输出列不一致——记为**警告不阻断**）／L3 只读（仅放行 Select/With，写操作与
    多语句**阻断**）／L4 语义（Wren dry_run）／L5 结果断言（无结果集时 skipped）。
    """
    return _wired("gates", "review", sql, dataset=dataset)


@mcp.tool
def sql_plan(demand_id: str, dataset: str = "B") -> dict:
    """M5-2 · 出「计划草稿」：选哪张主题表 / 什么颗粒度 / 哪条关联路径 / 哪个时间字段。

    这是两段式的第一段，**不含 SQL 正文**；选不出唯一解时给候选集合并标「需人工审核」，
    不硬选。候选一律取自 `sql_context_pack` 的闭集。
    """
    return _wired("sqlgen", "plan", demand_id, dataset=dataset)


@mcp.tool
def sql_generate(demand_id: str, dataset: str = "B",
                 candidate_sql: str | list[str] | None = None) -> dict:
    """M5-2 · 生成 SQL 初稿（两段式第二段）。

    `candidate_sql` 为调用方（客户端 Agent）产出的 SQL：非空则采用，为空则**回落确定性
    规划器**。**网关侧不调用任何模型**——自由生成由客户端承担。生成后立即过五层门禁，
    结果放在 `review` 字段。

    M6-2：`candidate_sql` 可传**多条候选**（list[str]）。多条且差异过大时会 `hold`——
    不下发 SQL（`sql_draft` 置空）、只保留计划草稿供人工选定，返回 `hold` /
    `hold_reason` / `candidate_diff`；单条（str）行为与既有链路完全一致。
    """
    return _wired(
        "sqlgen", "generate", candidate_sql=candidate_sql, demand_id=demand_id, dataset=dataset
    )


@mcp.tool
def sql_execute_readonly(
    demand_id: str, dataset: str = "B", sql: str | None = None, actor: str = ""
) -> dict:
    """M5-2 · 只读执行 SQL 并留痕。

    先过五层门禁，全过才执行（沿用 `wren_query` 的只读 + 语义双门禁写法），结果交 L5 断言；
    每次运行写一条 `sql_runs`（溯源字段齐全，可回放）。`sql` 为空时取该需求最近一次生成的 SQL。
    """
    return _wired(
        "sqlrun",
        "execute_readonly",
        sql,
        dataset=dataset,
        demand_id=demand_id,
        actor=actor or "system",
    )


@mcp.tool
def sql_run_get(demand_id: str) -> dict:
    """M5-2 · 回放该需求全部 SQL 运行记录（生成 SQL / 门禁结果 / 结果校验 / 溯源版本号）。"""
    return _wired("sqlrun", "get_run", demand_id)


@mcp.tool
def sql_run_list(demand_id: str = "", dataset: str = "", limit: int = 20) -> dict:
    """M6-4 · SQL 执行留痕列表（可按需求 / 数据集筛选，按时间倒序）。

    与 `sql_run_get` 的区别：后者按需求取全量明细；这里做**跨需求**列表 + 筛选 + 限流，
    面向审计视角「最近跑了哪些 SQL、谁跑的、审没审过、是否被采用」。
    """
    return _wired("sqlrun", "sql_run_list", demand_id or None, dataset or None, limit=limit)


@mcp.tool
def sql_run_replay(demand_id: str, version: int = 0) -> dict:
    """M6-4 · 按版本**精确复原**当时的完整上下文。

    复原链条：当时的输入（结构化需求 + 上下文包）→ 当时的 pack_version → 当时的 SQL
    → 当时的审查结论 → 当时的结果。version=0 表示取最近一次。
    与 `sql_run_get` 的区别：这里返回「可对照复算的快照」，而不只是留痕行列表。
    """
    return _wired("sqlrun", "sql_run_replay", demand_id, version=version or None)


def main():
    print("%s v%s 启动：http://%s:%d%s" % (GATEWAY_NAME, GATEWAY_VERSION, HOST, PORT, MCP_PATH))
    print("健康检查：http://127.0.0.1:%d/healthz" % PORT)
    # 建表（幂等）。此前只有 tools/collect_metadata.py 会调它，网关自身从不建表——
    # 于是「全新部署 + 首次调用」必然失败，且要等到第一次写库才暴露。放在启动时做掉。
    # 元数据库未就绪时不阻断启动（探活会把状态如实报成 degraded），只是打个警告。
    try:
        tables = db.init_schema()
        print("元数据库就绪，表：%s" % "、".join(tables))
    except Exception as e:  # noqa: BLE001
        print("⚠️ 元数据库初始化失败（%s: %s），相关工具将不可用" % (type(e).__name__, str(e)[:160]))
    for ds in registry.DATASETS.values():
        print("  数据集 %s -> %s" % (ds.key, ds.wren_url + "/mcp"))
    mcp.run(transport="streamable-http", host=HOST, port=PORT, path=MCP_PATH)


if __name__ == "__main__":
    main()
