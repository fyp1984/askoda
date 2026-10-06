"""Askoda 统一前端 · BFF（Backend for Frontend）。

红线遵守：
  R1 统一前端是唯一门面，BFF 不暴露任何开源 UI。
  R2 前端只调本BFF；出参经sanitize 脱敏，前端不出现开源组件地址/端口。
  R3 不 fork、不改开源源码（本目录为全新自研代码）。
  R4 无 UI 补丁 / DOM 注入 / 产物替换。
  R5 API 契约版本化：统一前缀 /api/v1。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from config import BFF_PORT, SERVE_STATIC, STATIC_DIR
from mcp_client import McpError, get_client
from sanitize import redact_mcp_tool, sanitize

import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bff")

API = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await get_client().aclose()


app = FastAPI(
    title="Askoda 统一前端 BFF",
    version="1.0.0",
    description=(
        "业务用户唯一入口。仅通过 MCP 网关触达后端能力，"
        "不直连任何开源组件、不直连业务库。所有接口前缀 /api/v1（契约版本化）。"
    ),
    lifespan=lifespan,
)

# 前端开发时可能来自 vite dev server；生产同源托管，故放开本地来源。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# 统一错误体：{code, message, hint}
# PRD §12.7 界面原则 2：任何硬门禁拒绝都要附「为什么拒绝 + 去哪里治理」。
# --------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, code: str, message: str, hint: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": redact_mcp_tool(exc.message), "hint": exc.hint},
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """把 FastAPI 默认的 422 详情改写成统一错误体，并补上 hint。

    PRD §12.7：任何硬门禁拒绝都要附「为什么拒绝 + 去哪里治理」。
    默认 422 只给字段错误列表、不指路，前端拿不到可执行的指引。
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        parts.append(f"{loc or '请求体'} {err.get('msg', '不合法')}")
    detail = "；".join(parts[:6]) or "请求参数不合法"
    return JSONResponse(
        status_code=422,
        content={
            "code": "INVALID_ARGUMENT",
            "message": f"请求参数没填对：{detail}",
            "hint": "请回到「提交需求」页，把标红的必填字段补齐（至少 1 个字符）后重新提交。",
        },
    )


@app.exception_handler(McpError)
async def _mcp_error_handler(request: Request, exc: McpError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "code": "UPSTREAM_UNAVAILABLE",
            "message": redact_mcp_tool(str(exc)),
            "hint": "这是后端能力链路（分析/取数）的问题，不是你的输入有问题。"
                    "请稍后重试；若持续失败，去运维确认 MCP 网关与语义层服务状态。",
        },
    )


# --------------------------------------------------------------------------
# 请求模型（入参以网关 tools/list 实盘 inputSchema 为准）
# --------------------------------------------------------------------------
class DemandCreateIn(BaseModel):
    title: str = Field(min_length=1, description="需求标题")
    business_context: str = Field(min_length=1, description="业务背景")
    description: str = Field(min_length=1, description="需求说明")
    expected_output: str = Field(min_length=1, description="输出预期")
    contact: str = Field(min_length=1, description="联系人")
    time_range: str | None = Field(default=None, description="时间范围")
    expected_finish_at: str | None = Field(default=None, description="期望完成时间")
    actor: str = Field(default="", description="提交人")


class AnalyzeIn(BaseModel):
    dataset: str = Field(default="B", description="数据集（A/B）")
    actor: str = Field(default="", description="操作人")


class ConfirmIn(BaseModel):
    question_id: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    choice: str | None = None
    actor: str = Field(default="")


class SqlIn(BaseModel):
    dataset: str = Field(default="B")
    candidate_sql: str | list[str] | None = None


class SqlExecuteIn(BaseModel):
    dataset: str = Field(default="B")
    sql: str | None = None
    actor: str = Field(default="")


class SimilarPrecheckIn(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    top: int = 5
    threshold: float = 0.55


# --------------------------------------------------------------------------
# 工具调用封装
# --------------------------------------------------------------------------
async def call(name: str, args: dict | None = None) -> Any:
    return await get_client().call_tool(name, args or {})


def _tool_rejected(result: Any) -> str | None:
    """网关工具以 `{"ok": false, ...}` 表达业务拒绝。返回拒绝原因（无拒绝则 None）。"""
    if isinstance(result, dict) and result.get("ok") is False:
        return str(result.get("error") or result.get("reason") or "下游拒绝")
    return None


# --------------------------------------------------------------------------
# 1. GET /api/v1/health —— 网关健康透传
# --------------------------------------------------------------------------
@app.get(f"{API}/health", tags=["运维"])
async def health() -> Any:
    data = await call("gateway_health")
    return sanitize(data)


# 2. GET /api/v1/tools —— 转发 tools/list，便于前端自解释排查
@app.get(f"{API}/tools", tags=["运维"])
async def tools() -> Any:
    data = await get_client().list_tools()
    return {"ok": True, "count": len(data), "tools": sanitize(data)}


# 3. GET /api/v1/datasets —— 数据集清单（脱敏后不含内部地址）
@app.get(f"{API}/datasets", tags=["元数据"])
async def datasets() -> Any:
    data = await call("datasets")
    return sanitize(data)


# 4. POST /api/v1/demand —— 提交需求
@app.post(f"{API}/demand", tags=["需求"])
async def create_demand(body: DemandCreateIn) -> Any:
    payload = body.model_dump()
    result = await call("demand_create", payload)
    reason = _tool_rejected(result)
    if reason:
        raise ApiError(
            code="DEMAND_REJECTED",
            message=reason,
            hint="提交被硬门禁拒绝：上面缺的就是必须补的。"
                 "请回到「提交需求」页把标红字段补齐后重新提交；"
                 "附件类问题请在附件区换成可识别的表格/文档。",
        )
    return sanitize(result)


# 5. POST /api/v1/demand/similar-precheck —— 提交前查重
@app.post(f"{API}/demand/similar-precheck", tags=["需求"])
async def similar_precheck(body: SimilarPrecheckIn) -> Any:
    """相似需求预检。

    实盘核对结论：本机网关实例**未暴露** `demand_similar_precheck` 工具
    （源码 gateway/app.py 有定义，但容器跑的是旧镜像；tools/list 只有 41 个）。
    因此这里优先调原生工具，不可用时降级为「demand_list 拉候选 + difflib
    字符级相似度」，语义与 gateway/demand.py:163 find_similar 保持一致。
    """
    try:
        native = await call(
            "demand_similar_precheck",
            {
                "title": body.title,
                "description": body.description,
                "top": body.top,
                "threshold": body.threshold,
            },
        )
        if isinstance(native, dict) and native.get("ok"):
            native["source"] = "gateway-native"
            return sanitize(native)
        raise McpError(str(native)[:200])
    except McpError as exc:
        log.info("similar_precheck 降级为本地计算: %s", exc)

    candidates = await call("demand_list", {"limit": 50})
    items = candidates.get("items", []) if isinstance(candidates, dict) else []
    probe = f"{body.title} {body.description}".strip()
    scored: list[dict] = []
    for row in items:
        did = row.get("demand_id")
        if not did:
            continue
        ratio = difflib.SequenceMatcher(None, probe, str(did)).ratio()
        # 与历史标题比对：用 demand_id+标题 拼接，贴近 find_similar 的字符级思路
        ratio = max(ratio, difflib.SequenceMatcher(None, body.title, str(did)).ratio())
        if ratio >= body.threshold:
            scored.append(
                {
                    "demand_id": did,
                    "title": row.get("title"),
                    "similarity": round(ratio, 3),
                    "status": row.get("status"),
                }
            )
    scored.sort(key=lambda x: -x["similarity"])
    scored = scored[: body.top]
    return {
        "ok": True,
        "source": "bff-degraded",
        "degraded_reason": "当前网关实例未暴露 demand_similar_precheck，"
                           "已退化为 BFF 侧字符级相似度估算，仅供提示，不作硬门禁。",
        "top": body.top,
        "threshold": body.threshold,
        "matched": len(scored),
        "items": scored,
    }


# 6. POST /api/v1/demand/{id}/analyze —— 首轮分析
@app.post(f"{API}/demand/{{demand_id}}/analyze", tags=["分析"])
async def analyze(demand_id: str, body: AnalyzeIn) -> Any:
    result = await call(
        "analysis_first_round",
        {"demand_id": demand_id, "dataset": body.dataset, "actor": body.actor},
    )
    reason = _tool_rejected(result)
    if reason:
        raise ApiError(
            code="ANALYSIS_FAILED",
            message=reason,
            hint="分析没能跑起来。请去「提交需求」页确认标题/说明是否写清了"
                 "统计主体与时间口径，再回到本页重试。",
        )
    return sanitize(result)


# 7. GET /api/v1/demand/{id}/e2e-status —— ★核心聚合接口
@app.get(f"{API}/demand/{{demand_id}}/e2e-status", tags=["聚合"])
async def e2e_status(demand_id: str, dataset: str = "B", run_limit: int = 20) -> Any:
    """一次打包 6 个 MCP 工具结果，前端只调1 次。

    硬要求：任一子调用失败**不得整体 500**。失败项写入 partial_errors，
    并标明缺的是哪一块，前端据此灰掉对应区块而不是整页报错。
    """
    specs = {
        "demand": ("demand_get", {"demand_id": demand_id}),
        "analysis": ("analysis_get", {"demand_id": demand_id}),
        "confirmations": ("confirmation_list", {"demand_id": demand_id}),
        "requirement": ("requirement_get", {"demand_id": demand_id}),
        "context_pack": ("sql_context_pack", {"demand_id": demand_id, "dataset": dataset}),
        "runs": ("sql_run_list", {"demand_id": demand_id, "limit": run_limit}),
    }

    names = list(specs.keys())

    async def run_one(key: str) -> tuple[str, Any, dict | None]:
        tool, args = specs[key]
        try:
            data = await call(tool, args)
            reason = _tool_rejected(data)
            if reason:
                # 工具自己说「还没到这一步」（例如尚未 requirement_structured），
                # 不算故障，但要告诉前端这一块为什么是空的。
                return key, None, {
                    "block": key,
                    "tool": tool,
                    "kind": "not_ready",
                    "message": reason,
                    "hint": _NOT_READY_HINT.get(key, "该环节尚未产出，请按左侧流程顺序先完成前一步。"),
                }
            return key, data, None
        except McpError as exc:
            return key, None, {
                "block": key,
                "tool": tool,
                "kind": "failed",
                "message": redact_mcp_tool(str(exc)),
                "hint": "这一块的后端调用失败，其它区块不受影响。"
                        "请稍后重试；若持续失败，去运维确认 MCP 网关状态。",
            }
        except Exception as exc:  # noqa: BLE001 - 聚合接口不允许整块崩
            return key, None, {
                "block": key,
                "tool": tool,
                "kind": "failed",
                "message": f"{type(exc).__name__}: {exc}"[:300],
                "hint": "这一块的后端调用异常，其它区块不受影响。请稍后重试。",
            }

    results = await asyncio.gather(*(run_one(k) for k in names))
    payload: dict[str, Any] = {k: None for k in names}
    partial_errors: list[dict] = []
    for key, data, err in results:
        payload[key] = sanitize(data) if data is not None else None
        if err:
            partial_errors.append(err)

    return {
        "demand_id": demand_id,
        "dataset": dataset,
        **payload,
        "partial_errors": partial_errors,
        "healthy": not partial_errors,
    }


_NOT_READY_HINT = {
    "requirement": "还没有结构化技术需求。请先在「查看分析」页确认口径，"
                "再点「生成 SQL」，系统会自动结构化。",
    "context_pack": "还没有取数上下文包。请先完成口径确认，再点「生成 SQL」。",
    "runs": "还没有取数记录。请到「看 SQL 与结果」页点「生成 SQL」。",
    "analysis": "还没有分析轮次。请到「提交需求」页完成提交后点「开始分析」。",
    "confirmations": "没有待确认问题，说明当前没有需要业务拍板的口径。",
    "demand": "取不到该需求。请确认需求编号是否正确，或该需求是否已被清理。",
}


# 8. POST /api/v1/demand/{id}/confirm —— 口径确认答复
@app.post(f"{API}/demand/{{demand_id}}/confirm", tags=["确认"])
async def confirm(demand_id: str, body: ConfirmIn) -> Any:
    """答复一条口径确认。

    实盘行为：答复本身**不会**回填槽位、也不会作为 P2 证据进入证据链。
    必须在答复后重跑一次 analyze。所以这里内部串行做「答复 → 重跑分析」，
    并把两段结果一起返回，前端可直接对比槽位是否已被回填。
    """
    answer = await call(
        "confirmation_answer",
        {
            "demand_id": demand_id,
            "question_id": body.question_id,
            "answer": body.answer,
            "choice": body.choice,
            "actor": body.actor,
        },
    )
    reason = _tool_rejected(answer)
    if reason:
        raise ApiError(
            code="CONFIRM_FAILED",
            message=reason,
            hint="答复没有提交成功。请到「口径确认」页检查是否选了该问题的选项，"
                 "或在「其他（请补充说明）」里补充文字后再提交。",
        )

    before = await call("analysis_get", {"demand_id": demand_id})
    reanalysis = await call(
        "analysis_first_round",
        {"demand_id": demand_id, "dataset": "B", "actor": body.actor},
    )

    backfilled = _detect_backfill(before, reanalysis)
    return sanitize(
        {
            "ok": True,
            "demand_id": demand_id,
            "confirmation": answer,
            "reanalysis": reanalysis,
            "backfill": backfilled,
            "notice": "答复已记录，并已自动重跑一轮分析："
                      "答复以P2「业务确认」证据进入证据链，槽位回填情况见 backfill。",
        }
    )


def _slot_signature(analysis: Any) -> dict[str, Any]:
    if not isinstance(analysis, dict):
        return {}
    slots = analysis.get("slots") or {}
    sig: dict[str, Any] = {}
    for name, body in slots.items():
        if not isinstance(body, dict):
            continue
        cands = body.get("candidates") or []
        sig[name] = {
            "candidate_count": len(cands),
            "needs_confirmation": bool(body.get("needs_confirmation")),
            "values": [c.get("value") for c in cands if isinstance(c, dict)][:8],
        }
    return sig


def _detect_backfill(before: Any, after: Any) -> dict[str, Any]:
    """对比答复前后的槽位，给前端一个直观的「是否被回填」视图。"""
    b, a = _slot_signature(before), _slot_signature(after)
    changed: list[dict] = []
    for slot in sorted(set(b) | set(a)):
        old = b.get(slot, {"candidate_count": 0, "needs_confirmation": False, "values": []})
        new = a.get(slot, {"candidate_count": 0, "needs_confirmation": False, "values": []})
        touched = (
            old["candidate_count"] != new["candidate_count"]
            or old["needs_confirmation"] != new["needs_confirmation"]
            or old["values"] != new["values"]
        )
        if touched:
            changed.append({"slot": slot, "before": old, "after": new})
    ev = (after or {}).get("evidence_chain") or [] if isinstance(after, dict) else []
    p2 = [e for e in ev if isinstance(e, dict) and e.get("level") == "P2"]
    return {
        "slots_changed": changed,
        "p2_evidence_count": len(p2),
        "p2_evidence": p2,
        "backfilled": bool(changed) or bool(p2),
    }


# 9. POST /api/v1/demand/{id}/sql —— 生成 SQL
@app.post(f"{API}/demand/{{demand_id}}/sql", tags=["取数"])
async def sql_generate(demand_id: str, body: SqlIn) -> Any:
    # 实测：`sql_generate` 不会自动产出结构化需求对象，若跳过这一步，
    # e2e-status 里的 requirement / context_pack 会永远是空的。
    # 这里先幂等地确保结构化需求存在（已存在时网关按 demand_id+dataset 覆盖写）。
    try:
        await call("requirement_structured", {"demand_id": demand_id, "dataset": body.dataset})
    except McpError as exc:
        log.info("requirement_structured 未成功（继续尝试生成 SQL）: %s", exc)

    args: dict[str, Any] = {"demand_id": demand_id, "dataset": body.dataset}
    if body.candidate_sql:
        args["candidate_sql"] = body.candidate_sql
    result = await call("sql_generate", args)
    reason = _tool_rejected(result)
    if reason:
        raise ApiError(
            code="SQL_GENERATE_BLOCKED",
            message=reason,
            hint="取数被硬门禁拦住了。请到「口径确认」页把标红（blocking）的口径答复完，"
                 "再回来重新生成 SQL——未确认的关键口径不得直接进入取数。",
        )
    return sanitize(result)


# 10. POST /api/v1/demand/{id}/sql/execute —— 执行只读 SQL
@app.post(f"{API}/demand/{{demand_id}}/sql/execute", tags=["取数"])
async def sql_execute(demand_id: str, body: SqlExecuteIn) -> Any:
    # 没有显式传 SQL 时，先尝试从已生成的 sql_runs 里取最近一条。
    # 网关 sql_generate 已落库初稿（见 gateway/sqlgen.generate），此处优先用
    # final_delivery_sql（若已执行），否则回退 generated_sql（生成初稿）。
    sql_text = (body.sql or "").strip()
    if not sql_text:
        try:
            runs = await call("sql_run_get", {"demand_id": demand_id})
            items = (runs or {}).get("runs") or []
            if items:
                sql_text = (items[0].get("final_delivery_sql")
                            or items[0].get("generated_sql") or "").strip()
        except McpError:
            sql_text = ""

    args: dict[str, Any] = {"demand_id": demand_id, "dataset": body.dataset, "actor": body.actor}
    if sql_text:
        args["sql"] = sql_text
    result = await call("sql_execute_readonly", args)
    reason = _tool_rejected(result)
    if reason:
        raise ApiError(
            code="SQL_EXECUTE_FAILED",
            message=reason,
            hint="没有可执行的 SQL。请先点「生成 SQL」，等门禁通过后再执行。",
        )
    return sanitize(result)


# 11. GET /api/v1/demand/{id}/citations —— 知识引用明细
@app.get(f"{API}/demand/{{demand_id}}/citations", tags=["溯源"])
async def citations(
    demand_id: str, include_retired: bool = False, limit: int = 50
) -> Any:
    """知识引用明细。

    实盘核对结论：本机网关实例**未暴露** `knowledge_citation_list`。
    这里优先调原生工具；不可用时降级用 `knowledge_search` 取该需求的引用线索，
    并在 source 字段标明降级，避免前端误以为是权威留痕表。
    """
    try:
        native = await call(
            "knowledge_citation_list",
            {
                "demand_id": demand_id,
                "include_retired": include_retired,
                "limit": limit,
            },
        )
        if isinstance(native, dict) and not native.get("ok") is False:
            native["source"] = "gateway-native"
            return sanitize(native)
        raise McpError(str(native)[:200])
    except McpError as exc:
        log.info("citations 降级为 knowledge_search: %s", exc)

    runs = await call("sql_run_get", {"demand_id": demand_id})
    demand = await call("demand_get", {"demand_id": demand_id})
    question = f"{demand.get('title', '')} {demand.get('description', '')}".strip()
    search = await call(
        "knowledge_search", {"question": question, "demand_id": demand_id, "top_k": limit}
    )
    cites = (search.get("citations") or []) if isinstance(search, dict) else []
    return sanitize(
        {
            "ok": True,
            "demand_id": demand_id,
            "source": "bff-degraded",
            "degraded_reason": "当前网关实例未暴露 knowledge_citation_list，"
                               "已退化为按需求文本检索知识库，不等同于留痕表。",
            "sql_run_count": (runs or {}).get("total", 0) if isinstance(runs, dict) else 0,
            "total": len(cites),
            "items": cites,
        }
    )


# --------------------------------------------------------------------------
# 静态托管（前端构建产物）。产物缺失时给出明确提示而不是 404。
# --------------------------------------------------------------------------
_INDEX = os.path.join(STATIC_DIR, "index.html")

if SERVE_STATIC and os.path.isdir(STATIC_DIR):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(STATIC_DIR, "assets")),
        name="assets",
    )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = os.path.join(STATIC_DIR, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        if os.path.isfile(_INDEX):
            return FileResponse(_INDEX)
        return JSONResponse(
            status_code=503,
            content={
                "code": "STATIC_NOT_BUILT",
                "message": "前端构建产物尚未生成。",
                "hint": "请在 web/ 目录执行 `npm install && npm run build`，"
                        "构建产物会输出到 bff/static/，之后刷新本页即可。",
            },
        )

else:
    @app.get("/", include_in_schema=False)
    async def root() -> Any:
        return {
            "service": "Askoda 统一前端 BFF",
            "api_prefix": API,
            "docs": "/docs",
            "frontend": "静态产物未构建（bff/static 不存在）。"
                        "请在 web/ 执行 `npm install && npm run build`。",
        }