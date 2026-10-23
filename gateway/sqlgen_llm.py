# -*- coding: utf-8 -*-
"""大模型原生 SQL 生成（需求 → SQL），带「生成→校验→修复」闭环与确定性回退。

为什么需要它
------------
原设计把自由生成放在「客户端 Agent」，网关侧硬性不调模型（见 sqlgen.py 头注释）。
但本产品面向真实业务：复杂需求（多表 JOIN / 时间过滤 / 排序 / 派生指标）必须能响应，
否则"答非所问"只能被拦、不能被答。产品本身即对接大模型 ⇒ 由网关侧调用模型辅助生成，
是产品化 + 工业化的正确落点。本模块即该能力。

**默认无配置即禁用**：`is_enabled()` 为假时不进入本模块，行为与既有确定性链路完全等价。

质量保障（不让模型"自由发挥"）
------------------------------
① 闭集约束：只允许引用 MDL 可见模型/列 / 已建模关系；prompt 给全量 schema + 关系 + 口径。
② 结构化输出：要求模型返回 JSON（sql / explanation / objects / confidence）。
③ 硬校验：只读关键字白名单 → sqlglot 解析 → 五层门禁（含 L-REQ 需求要素覆盖）。
④ 修复闭环：门禁不过则把错误回喂模型再修（最多 `LLM_MAX_REPAIR` 次）；
   仍不过则**不下发**，交回确定性规划器（宁可不答，绝不答错）。
⑤ 可观测：attempts / notes / risks 全程留痕，生成器标 `generator="llm"`。
"""
from __future__ import annotations

import json
import os
import re

import llm
import planner
import registry

try:  # 口径拆分（中文名 / 括号内口径）。metadata 是网关内模块，正常可导入。
    import metadata as _metadata
except Exception:  # noqa: BLE001
    _metadata = None


def _get_dataset(dataset):
    """接受数据集 key 或 Dataset 对象，统一返回 Dataset。"""
    if isinstance(dataset, str):
        return registry.get(dataset)
    return dataset


def _split_label(desc):
    """把 MDL description 拆成 (中文名, 口径)；失败就原样返回。"""
    if not desc:
        return "", ""
    if _metadata is not None and hasattr(_metadata, "split_label_caliber"):
        try:
            label, caliber = _metadata.split_label_caliber(desc)
            return (label or "", caliber or "")
        except Exception:  # noqa: BLE001
            pass
    return str(desc), ""


def is_enabled():
    """是否启用 LLM 原生生成：模型客户端可用即可。"""
    try:
        return bool(llm.is_enabled())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 上下文：把 MDL 语义层渲染成 prompt 可读的 schema + 关系 + 口径
# ---------------------------------------------------------------------------
def build_schema_context(dataset):
    """渲染数据集 schema / 关系，供模型在闭集内选表选列。"""
    ds = _get_dataset(dataset)
    mdl = getattr(ds, "mdl", {}) or {}
    lines = []
    lines.append("数据集：%s · %s" % (ds.key, getattr(ds, "label", "")))
    if getattr(ds, "domain", ""):
        lines.append("业务域：%s" % ds.domain)
    lines.append("方言：PostgreSQL（仅允许 SELECT / WITH 只读查询；表名/列名必须与下方完全一致）")
    lines.append("")
    lines.append("## 可用模型（只允许引用以下模型与其列）")
    for m in mdl.get("models", []) or []:
        mname = m.get("name") or ""
        tbl = (m.get("tableReference") or {}).get("table") or mname
        pk = m.get("primaryKey") or ""
        head = "### %s（物理表 %s%s）" % (mname, tbl, ("，主键 " + str(pk)) if pk else "")
        lines.append(head)
        for c in m.get("columns", []) or []:
            label, caliber = _split_label(c.get("description"))
            bits = ["- %s %s" % (c.get("name"), c.get("type") or "")]
            if label:
                bits.append("中文名：%s" % label)
            if caliber:
                bits.append("口径：%s" % caliber)
            if c.get("isPrimaryKey"):
                bits.append("主键")
            lines.append("，".join(bits))
    rels = mdl.get("relationships", []) or []
    if rels:
        lines.append("")
        lines.append("## 可用关系（JOIN 只能用以下已建模条件，不得自行编造关联）")
        for r in rels:
            models = r.get("models") or []
            lines.append(
                "- %s  ⟶  %s（%s）"
                % (
                    " ⋈ ".join(models),
                    r.get("name") or "",
                    r.get("condition") or "",
                )
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt 构建（纯函数，离线可测）
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "你是企业级 Text-to-SQL 引擎，面向真实业务取数。硬性要求：\n"
    "1) 只使用【可用模型】中列出的表与列，列名、表名必须逐字一致，禁止编造字段或关联；\n"
    "2) 只用 SELECT 或 WITH 只读语句，禁止任何写操作、DDL、多语句；\n"
    "3) 严格满足需求里的每一个要素：筛选条件、时间范围、聚合口径、分组维度、排序方向、TopN；\n"
    "   若需求含排序（如「降序」）必须体现在 ORDER BY，含时间范围必须体现在 WHERE；\n"
    "4) 若需求要求的要素在语义层确实无法满足，请在 explanation 中说明缺失点，sql 输出空字符串；\n"
    "5) 只输出一个 JSON 对象，不要输出多余文字，格式：\n"
    '{"sql": "<SQL 或空串>", "explanation": "<一句话说明怎么满足需求>", '
    '"objects": [{"type":"model|column","name":"<引用到的对象>"}], '
    '"confidence": <0~1 的数字>}'
)


def _structured_hint(structured):
    """把结构化需求（六槽位）压成几行提示，帮助模型对齐要素。"""
    if not isinstance(structured, dict):
        return ""
    parts = []

    def _val(x):
        if isinstance(x, dict):
            return x.get("value")
        return x

    def _add(label, v):
        v = _val(v)
        if v is None:
            return
        s = str(v).strip()
        if s and "无候选结论" not in s:
            parts.append("· %s：%s" % (label, s))

    for key, label in (
        ("subject", "分析主体"),
        ("granularity_definition", "颗粒度"),
        ("time_scope", "时间口径"),
        ("scope_conditions", "范围条件"),
        ("output_fields", "输出字段"),
        ("risk_points", "风险点"),
    ):
        v = structured.get(key)
        if isinstance(v, list):
            vals = [_val(x) for x in v]
            vals = [str(x).strip() for x in vals if x and str(x).strip() and "无候选结论" not in str(x)]
            if vals:
                parts.append("· %s：%s" % (label, "；".join(vals)))
        else:
            _add(label, v)
    if not parts:
        return ""
    return "已结构化的需求要素（应逐项满足）：\n" + "\n".join(parts)


def build_generation_messages(dataset, requirement_text, structured=None, knowledge=None):
    """构建首轮生成消息。knowledge 为可选的知识/口径片段（list[str]）。"""
    ds = _get_dataset(dataset)
    schema_ctx = build_schema_context(ds)
    user_parts = [schema_ctx, "", "## 业务需求", (requirement_text or "").strip() or "（需求为空）"]
    hint = _structured_hint(structured)
    if hint:
        user_parts += ["", hint]
    if knowledge:
        k_text = "\n".join("- %s" % str(k)[:400] for k in knowledge[:6] if k)
        if k_text:
            user_parts += ["", "## 可参考的业务口径 / 知识（优先据此取口径）", k_text]
    user_parts += [
        "",
        "请输出 JSON：sql / explanation / objects / confidence。"
        "sql 必须只读、只引用上方表列、完整满足所有需求要素。",
    ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


def build_repair_messages(dataset, requirement_text, bad_sql, error, structured=None):
    """把上一轮的门禁错误回喂模型，要求其在闭集内修正。"""
    ds = _get_dataset(dataset)
    schema_ctx = build_schema_context(ds)
    user = (
        "%s\n\n## 业务需求\n%s\n\n"
        "## 上一版 SQL（未通过校验）\n%s\n\n"
        "## 校验反馈（必须修正这些问题）\n%s\n\n"
        "请在语义层闭集内修正，只输出 JSON（sql/explanation/objects/confidence）。"
        "若确实无法在闭集内满足，sql 返回空串并在 explanation 说明缺口。"
        % (schema_ctx, (requirement_text or "").strip(), bad_sql or "（空）", error or "未通过")
    )
    hint = _structured_hint(structured)
    if hint:
        user += "\n\n" + hint
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 响应解析（纯函数，离线可测）
# ---------------------------------------------------------------------------
def _strip_fences(text):
    t = (text or "").strip()
    # ```json ... ``` / ```sql ... ``` / ``` ... ```
    m = re.search(r"```(?:json|sql)?\s*(.*?)```", t, re.S | re.I)
    if m:
        return m.group(1).strip()
    return t


def parse_generation_response(text):
    """从模型输出里抽取 {sql, explanation, objects, confidence, parse_ok}。

    兼容三种形态：纯 JSON、代码围栏包裹的 JSON/SQL、裸 SQL。
    """
    raw = (text or "").strip()
    body = _strip_fences(raw)
    out = {"sql": "", "explanation": "", "objects": [], "confidence": None,
           "parse_ok": False, "raw": raw[:2000]}
    # 1) 直接 JSON
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return _from_obj(data, out)
    except Exception:  # noqa: BLE001
        pass
    # 2) 文本里夹带的第一个 {...}
    i, j = body.find("{"), body.rfind("}")
    if 0 <= i < j:
        try:
            data = json.loads(body[i:j + 1])
            if isinstance(data, dict):
                return _from_obj(data, out)
        except Exception:  # noqa: BLE001
            pass
    # 3) 裸 SQL
    cand = body.strip().rstrip(";").strip()
    if re.match(r"^(select|with)\b", cand, re.I):
        out["sql"] = cand
        out["parse_ok"] = True
    return out


def _from_obj(data, out):
    sql = data.get("sql") or data.get("sql_draft") or data.get("query") or ""
    sql = str(sql).strip().rstrip(";").strip()
    out["sql"] = sql
    out["explanation"] = str(data.get("explanation") or data.get("reason") or "").strip()
    objs = data.get("objects")
    if isinstance(objs, list):
        out["objects"] = [o for o in objs if isinstance(o, dict)][:40]
    conf = data.get("confidence")
    try:
        out["confidence"] = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        out["confidence"] = None
    out["parse_ok"] = True
    return out


# ---------------------------------------------------------------------------
# 校验（只读 + 语法 + 五层门禁含 L-REQ）
# ---------------------------------------------------------------------------
def _review_summary(review):
    """从 gates.review 结果里提取失败层的简述。"""
    if not isinstance(review, dict):
        return ""
    bits = []
    for layer in review.get("layers") or []:
        if isinstance(layer, dict) and layer.get("ok") is False and not layer.get("skipped"):
            bits.append("%s %s：%s" % (
                layer.get("layer") or "", layer.get("name") or "",
                str(layer.get("detail") or "")[:200]))
    return "；".join(bits) or str(review.get("status") or "")


def validate_sql(sql, dataset, requirement=None):
    """对生成的 SQL 做硬校验。返回 (ok, issues, review)。"""
    ds = _get_dataset(dataset)
    if not sql or not sql.strip():
        return False, ["SQL 为空"], None
    # ① 只读白名单
    try:
        ok, why = planner.is_readonly(sql)
    except Exception as e:  # noqa: BLE001
        return False, ["只读检查异常：%s" % str(e)[:160]], None
    if not ok:
        return False, [why], None
    # ② sqlglot 语法（有则解析；无则跳过，交由门禁）
    try:
        import sqlglot  # noqa: F401
        try:
            sqlglot.parse_one(sql, read="postgres")
        except Exception as e:  # noqa: BLE001
            return False, ["SQL 语法解析失败：%s" % str(e)[:200]], None
    except ImportError:
        pass
    # ③ 五层门禁（含 L-REQ 需求要素覆盖）
    try:
        review = planner_gates_review(sql, ds.key, requirement)
    except Exception as e:  # noqa: BLE001
        # 门禁本身异常时不做"假通过"：视为未通过，交由上层回退确定性，避免静默放行
        return False, ["门禁审查异常：%s: %s" % (type(e).__name__, str(e)[:160])], None
    status = (review or {}).get("status")
    if status == "不通过":
        return False, ["门禁不通过：%s" % _review_summary(review)], review
    return True, [], review


def planner_gates_review(sql, dataset_key, requirement):
    """薄封装 gates.review，便于测试时替换/隔离。"""
    import gates as _gates
    return _gates.review(sql, dataset=dataset_key, requirement=requirement)


# ---------------------------------------------------------------------------
# 主入口：生成（含修复闭环）
# ---------------------------------------------------------------------------
def generate_sql(demand_id=None, dataset="B", nl="", structured=None, knowledge=None):
    """LLM 原生生成 SQL。返回：

      · 成功：`{ok:True, sql, explanation, objects, confidence, attempts, notes, risks}`
      · 失败：`{ok:False, error, attempts, risks}`（调用方据此回落确定性规划器）
    """
    if not is_enabled():
        return {"ok": False, "disabled": True, "error": "LLM 未启用", "attempts": [], "risks": []}
    try:
        ds = _get_dataset(dataset)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "数据集不可用：%s" % str(e)[:160], "attempts": [], "risks": []}

    # 修复轮次：优先取生效配置（前端「系统设置」可调），否则回落环境变量。
    # 上限 3 轮——再多的「修 → 判 → 再修」在实践中只会烧 token，不会救回结构错的需求。
    max_repair = llm.config().get("max_repair")
    if max_repair is None:
        max_repair = max(0, int(os.getenv("LLM_MAX_REPAIR", "1") or 1))
    max_repair = max(0, min(3, int(max_repair)))
    messages = build_generation_messages(ds, nl, structured=structured, knowledge=knowledge)
    attempts, notes, risks = [], [], []
    last_err = ""

    for i in range(max_repair + 1):
        resp = llm.chat(messages, json_mode=True)
        if not resp.get("ok"):
            last_err = resp.get("error") or "LLM 调用失败"
            attempts.append({"round": i + 1, "ok": False, "error": last_err})
            risks.append("LLM 调用失败：%s" % last_err)
            break
        parsed = parse_generation_response(resp.get("content") or "")
        sql = parsed.get("sql") or ""
        ok, issues, review = validate_sql(sql, ds, structured)
        attempts.append({
            "round": i + 1,
            "ok": ok,
            "sql": sql[:600],
            "issues": issues,
            "latency_ms": resp.get("latency_ms"),
        })
        if ok:
            notes.append("LLM 首轮即通过门禁" if i == 0 else "LLM 第 %d 轮修复后通过门禁" % (i + 1))
            return {
                "ok": True,
                "sql": sql,
                "explanation": parsed.get("explanation") or "",
                "objects": parsed.get("objects") or [],
                "confidence": parsed.get("confidence"),
                "review_status": (review or {}).get("status"),
                "attempts": attempts,
                "notes": notes,
                "risks": risks,
            }
        last_err = "；".join(issues) or "未通过校验"
        if not sql:
            # 模型明确表示闭集内无法满足：不再盲目修复
            risks.append("LLM 表示闭集内无法满足需求：%s" % (parsed.get("explanation") or "")[:200])
            break
        if i < max_repair:
            messages = build_repair_messages(ds, nl, sql, last_err, structured=structured)

    return {
        "ok": False,
        "error": last_err or "LLM 未产出可校验 SQL",
        "attempts": attempts,
        "risks": risks,
    }
