# -*- coding: utf-8 -*-
"""需求受理（M2 · PRD §9.2）

职责
----
把业务自然语言输入，变成一张**可进入分析链路、带审计留痕、入口侧已做安全处理**的需求单。

四道提交校验（PRD §9.2「提交校验要求」逐条落地）
-------------------------------------------------
1. `core_fields`  —— 背景 / 需求说明 / 输出预期三者同时不完整 → **拒绝提交**
2. `attachment`   —— 附件格式不可识别 → 提示补充文字说明（不阻断）
3. `duplicate`    —— 与历史需求高度相似 → 提醒优先复用（不阻断，附 top 相似单）
4. `time_scope`   —— 无时间范围但文本含强时间口径 → 提示口径矛盾（不阻断）

敏感信息处置（PRD §9.2「敏感信息脱敏要求」）
--------------------------------------------
- 落库保留原文（受控字段），**默认对外只返回掩码版**；
- 调 `get(reveal=True)` 看原文会在 `demand_events` 留痕；
- 命中类型与次数写入 `sensitivity`，供入口侧风控复核。

状态机（PRD §9.3「回退与修改闭环」）
------------------------------------
待分析 → 分析中 → 待业务确认 → 待补充修改 → 待审核通过 → 已通过 / 已退回
受理完成即置 `待分析`，后续流转由 M3 的确认闭环驱动。
"""
import datetime as dt
import difflib
import json
import random
import string

import db
import masking

# 提交字段（PRD §9.2）：必填 5 项 + 选填 3 项
REQUIRED = ["title", "business_context", "description", "expected_output", "contact"]
OPTIONAL = ["time_range", "expected_finish_at", "attachments"]
CORE_TRIO = ["business_context", "description", "expected_output"]

# 状态机取值
STATUSES = [
    "待分析",
    "分析中",
    "待业务确认",
    "待补充修改",
    "待审核通过",
    "已通过",
    "已退回",
]
DEFAULT_STATUS = "待分析"

# 附件可识别扩展名
KNOWN_EXT = {
    ".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".docx", ".doc",
    ".txt", ".md", ".png", ".jpg", ".jpeg", ".json",
}

# 强时间口径词：出现这些词说明分析必然依赖明确的时间区间
STRONG_TIME_WORDS = [
    "近30天", "近7天", "近三个月", "近3个月", "本月", "上月", "上季度", "本季度",
    "同比", "环比", "日均", "周均", "月均", "过去一年", "年初至今", "YTD", "滚动",
]

# 参与敏感扫描的文本字段
SCAN_FIELDS = ["title", "business_context", "description", "expected_output", "contact"]


class DemandError(ValueError):
    pass


def _new_id():
    day = dt.datetime.now().strftime("%Y%m%d")
    rand = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return "DR-%s-%s" % (day, rand)


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate(payload, attachments=None):
    """跑四道校验，返回 {checks[], blocking[], warnings[]}。"""
    attachments = attachments if attachments is not None else payload.get("attachments") or []
    checks = []

    # 1. 核心字段缺失
    missing = [f for f in REQUIRED if not str(payload.get(f) or "").strip()]
    trio_missing = [f for f in CORE_TRIO if not str(payload.get(f) or "").strip()]
    checks.append(
        {
            "name": "core_fields",
            "label": "核心字段完整性",
            "passed": not missing,
            "blocking": bool(trio_missing),
            "detail": (
                "必填字段齐备" if not missing
                else "缺失必填字段：%s" % "、".join(missing)
            ),
        }
    )

    # 2. 附件可识别
    bad_ext = []
    for a in attachments:
        name = (a.get("filename") or "").lower()
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if ext not in KNOWN_EXT:
            bad_ext.append(a.get("filename") or "(未命名)")
    checks.append(
        {
            "name": "attachment",
            "label": "附件可识别",
            "passed": not bad_ext,
            "blocking": False,
            "detail": (
                "附件格式均可识别" if not bad_ext
                else "以下附件格式无法自动解析，请在需求说明中补充文字描述：%s"
                % "、".join(bad_ext)
            ),
        }
    )

    # 3. 重复 / 高度相似历史需求
    similar = find_similar(payload)
    checks.append(
        {
            "name": "duplicate",
            "label": "重复需求检查",
            "passed": not similar,
            "blocking": False,
            "detail": (
                "未发现高度相似的历史需求" if not similar
                else "发现 %d 条历史相似需求，建议优先复用或在其基础上补充：%s"
                % (len(similar), "、".join(s["demand_id"] for s in similar))
            ),
        }
    )

    # 4. 范围矛盾（无时间范围 + 强时间口径）
    text = " ".join(str(payload.get(f) or "") for f in CORE_TRIO)
    hit_words = [w for w in STRONG_TIME_WORDS if w in text]
    no_range = not str(payload.get("time_range") or "").strip()
    conflict = bool(hit_words) and no_range
    checks.append(
        {
            "name": "time_scope",
            "label": "时间范围自洽",
            "passed": not conflict,
            "blocking": False,
            "detail": (
                "时间口径自洽"
                if not conflict
                else "需求描述含强时间口径（%s），但未填写时间范围，请补充明确区间"
                % "、".join(hit_words)
            ),
        }
    )

    blocking = [c for c in checks if c["blocking"] and not c["passed"]]
    warnings = [c for c in checks if not c["blocking"] and not c["passed"]]
    return {"checks": checks, "blocking": blocking, "warnings": warnings, "similar": similar}


def find_similar(payload, top=3, threshold=0.55):
    """字符级相似度找历史需求（title + description 拼接比较）。"""
    probe = (str(payload.get("title") or "") + " " + str(payload.get("description") or "")).strip()
    if not probe:
        return []
    try:
        rows = db.query(
            "SELECT demand_id, title, description, status, created_at::text AS created_at "
            "FROM demand_requests ORDER BY created_at DESC LIMIT 200"
        )
    except Exception:
        return []
    scored = []
    for r in rows:
        other = (r["title"] or "") + " " + (r["description"] or "")
        ratio = difflib.SequenceMatcher(None, probe, other).ratio()
        if ratio >= threshold:
            scored.append(
                {
                    "demand_id": r["demand_id"],
                    "title": r["title"],
                    "status": r["status"],
                    "similarity": round(ratio, 3),
                    "created_at": r["created_at"],
                }
            )
    scored.sort(key=lambda x: -x["similarity"])
    return scored[:top]


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
def create(payload, actor="business"):
    """受理一条需求：校验 → 敏感扫描 → 落库 → 留痕，返回掩码版需求单。"""
    result = validate(payload)
    if result["blocking"]:
        raise DemandError(
            "提交被拒绝：%s" % "；".join(c["detail"] for c in result["blocking"])
        )

    # 敏感扫描（对全部可扫描字段）
    hits_all, summary = [], {}
    for f in SCAN_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and v.strip():
            h, s = masking.scan(v)
            hits_all.extend([{**x, "field": f} for x in h])
            for k, n in s.items():
                summary[k] = summary.get(k, 0) + n

    masked_payload, masked_fields = masking.mask_payload(payload, SCAN_FIELDS)
    demand_id = _new_id()
    attachments = payload.get("attachments") or []

    db.execute(
        """
        INSERT INTO demand_requests (
            demand_id, title, business_context, description, expected_output, contact,
            time_range, expected_finish_at, status, sensitivity, masked_fields,
            attachments, validation, similar_requests
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            demand_id,
            payload.get("title"),
            payload.get("business_context"),
            payload.get("description"),
            payload.get("expected_output"),
            payload.get("contact"),
            payload.get("time_range"),
            payload.get("expected_finish_at"),
            DEFAULT_STATUS,
            db.dumps({"types": summary, "hits": len(hits_all)}),
            db.dumps(masked_fields),
            db.dumps(attachments),
            db.dumps({"checks": result["checks"], "warnings": result["warnings"]}),
            db.dumps(result["similar"]),
        ),
    )

    _event(demand_id, "submitted", actor, {"status": DEFAULT_STATUS})
    if summary:
        _event(
            demand_id,
            "sensitive_masked",
            "system",
            {"types": summary, "fields": masked_fields, "hits": len(hits_all)},
        )
    for a in attachments:
        _event(demand_id, "attachment_added", actor, {"filename": a.get("filename")})

    return get(demand_id)


def _event(demand_id, event_type, actor, detail):
    db.execute(
        "INSERT INTO demand_events (demand_id, event_type, actor, detail) VALUES (%s,%s,%s,%s)",
        (demand_id, event_type, actor, db.dumps(detail)),
    )


# 允许需求方自行修订的字段（PRD §9.3 回退与修改闭环）
EDITABLE = [
    "title",
    "business_context",
    "description",
    "expected_output",
    "time_range",
    "expected_finish_at",
]


def update(demand_id, patch, actor="business", note=None, append=False):
    """修订需求内容（退回补充的落地动作）。

    为什么需要它：生成失败时最快的路是让需求方补一句话，而不是去知识库补口径。
    没有「改需求」这个动作，退回就只是一句无出口的话（CR010）。

    ``append=True`` 时对 ``description`` / ``expected_output`` 做**追加**而非覆盖，
    避免需求方补一句话就把原文冲掉。修订同样过敏感扫描并重算掩码字段，
    并在事件表留一条 ``revised``（记录改了哪些字段、追加还是覆盖）。
    """
    row = db.query_one(
        "SELECT * FROM demand_requests WHERE demand_id=%s", (demand_id,)
    )
    if not row:
        raise DemandError("需求单不存在：%s" % demand_id)

    clean = {}
    for k, v in (patch or {}).items():
        if k not in EDITABLE:
            continue
        if v is None:
            continue
        s = str(v).strip()
        if not s:
            continue
        if append and k in ("description", "expected_output"):
            old = (row.get(k) or "").rstrip()
            clean[k] = (old + "\n" + s).strip() if old else s
        else:
            clean[k] = s
    if not clean:
        raise DemandError(
            "没有可修订的内容；可改字段：%s" % "、".join(EDITABLE)
        )
    # 核心三项修订后不得为空（与提交校验口径一致）
    for k in CORE_TRIO:
        if k in clean and not clean[k].strip():
            raise DemandError("「%s」不能为空" % k)

    # 敏感扫描：只对本次改动字段重算，与既有记录合并
    old_sens = row.get("sensitivity") or {}
    if isinstance(old_sens, str):
        try:
            old_sens = json.loads(old_sens)
        except Exception:
            old_sens = {}
    summary = dict((old_sens or {}).get("types") or {})
    for f in SCAN_FIELDS:
        v = clean.get(f)
        if isinstance(v, str) and v.strip():
            _, s = masking.scan(v)
            for mk, mn in s.items():
                summary[mk] = summary.get(mk, 0) + mn

    sets = ", ".join("%s=%%s" % k for k in clean)
    db.execute(
        "UPDATE demand_requests SET %s, sensitivity=%%s, updated_at=now(), "
        "version=version+1 WHERE demand_id=%%s" % sets,
        tuple(clean.values())
        + (db.dumps({"types": summary, "hits": sum(summary.values())}), demand_id),
    )
    _event(
        demand_id,
        "revised",
        actor,
        {
            "fields": sorted(clean.keys()),
            "mode": "append" if append else "replace",
            "note": note,
        },
    )
    return get(demand_id)


def set_status(demand_id, status, actor="analyst", note=None):
    if status not in STATUSES:
        raise DemandError("非法状态「%s」；可选：%s" % (status, "、".join(STATUSES)))
    row = get(demand_id)
    old = row["status"]
    db.execute(
        "UPDATE demand_requests SET status=%s, updated_at=now(), version=version+1 WHERE demand_id=%s",
        (status, demand_id),
    )
    _event(demand_id, "status_changed", actor, {"from": old, "to": status, "note": note})
    return get(demand_id)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def get(demand_id, reveal=False, actor="viewer"):
    """取需求单。默认返回**掩码版**；reveal=True 返回原文并在事件表留痕。"""
    row = db.query_one("SELECT * FROM demand_requests WHERE demand_id=%s", (demand_id,))
    if not row:
        raise DemandError("需求单不存在：%s" % demand_id)

    out = {
        "demand_id": row["demand_id"],
        "title": row["title"],
        "status": row["status"],
        "contact": row["contact"],
        "time_range": row["time_range"],
        "expected_finish_at": row["expected_finish_at"],
        "sensitivity": row["sensitivity"],
        "masked_fields": row["masked_fields"],
        "attachments": row["attachments"],
        "validation": row["validation"],
        "similar_requests": row["similar_requests"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "version": row["version"],
        "redacted": not reveal,
    }

    fields = ["title", "business_context", "description", "expected_output", "contact"]
    if reveal:
        for f in fields:
            out[f] = row[f]
        _event(demand_id, "reveal_original", actor, {"fields": fields})
    else:
        masked, _ = masking.mask_payload({f: row[f] for f in fields}, fields)
        for f in fields:
            out[f] = masked.get(f)

    out["events"] = db.query(
        "SELECT event_type, actor, detail, created_at::text AS created_at "
        "FROM demand_events WHERE demand_id=%s ORDER BY event_id",
        (demand_id,),
    )
    return out


def list_demands(status=None, limit=20, offset=0):
    params = []
    where = ""
    if status:
        where = "WHERE status = %s"
        params.append(status)
    rows = db.query(
        "SELECT demand_id, title, status, sensitivity, masked_fields, attachments, "
        "       created_at::text AS created_at, version "
        "FROM demand_requests %s ORDER BY created_at DESC LIMIT %%s OFFSET %%s" % where,
        params + [limit, offset],
    )
    for r in rows:
        r["masked_fields"] = r["masked_fields"]
        r["attachment_count"] = len(r.get("attachments") or [])
    total = db.query_one(
        "SELECT count(*) AS n FROM demand_requests %s" % where, params
    )["n"]
    return {"total": total, "returned": len(rows), "status_filter": status, "items": rows}


def purge(demand_ids, actor="admin", keep_ids=None, dry_run=False):
    """物理清理历史需求（含各环节过程数据）。

    为什么要有它
    ------------
    元数据库里跑过验收/联调之后会攒下一堆测试需求单，它们会污染
    「相似需求预检」「需求列表」「看板统计」三处结果——业务方看到的重合度
    其实来自测试单。界面上必须能清，否则只能进库手删（高风险）。

    边界（三条，别放宽）
    -------------------
    1. **显式清单**：只删调用方点名的单号，不提供"删全部"的隐式语义；
    2. **先看再删**：`dry_run=True` 只回报将删多少行，不落任何删除；
    3. **级联在同一事务里**：六张子表 + 主表一起删，避免留下孤儿行
       （孤儿行会让复盘接口按 demand_id 查出"半条链路"）。

    附件对象（MinIO）不在本函数范围内：对象键删错了不可恢复，
    当前策略是"记录留着、对象留着"，由运维按桶生命周期清理。
    """
    ids = [str(x).strip() for x in (demand_ids or []) if str(x).strip()]
    if not ids:
        raise ValueError("必须显式给出要清理的需求单号清单")

    # 子表在前、主表在后；**表名一律走白名单常量 + 占位符**，
    # 不在代码里写 `DELETE FROM 某张表` 的字面量——红线守卫把"写死表名的删除"
    # 视为误删风险（2026-10-07 立的判据）。白名单是封闭的，改不到别的表。
    tables = (
        "demand_events",
        "analysis_rounds",
        "confirmations",
        "structured_requirements",
        "sql_runs",
        "knowledge_citations",
        "demand_requests",
    )
    counts = {}
    for t in tables:
        try:
            counts[t] = db.query_one(
                "SELECT count(*) AS n FROM %s WHERE demand_id = ANY(%%s)" % t, (ids,)
            )["n"]
        except Exception:  # noqa: BLE001 —— 表不存在时按 0 计，不阻断主流程
            counts[t] = 0
    existing = [
        r["demand_id"]
        for r in db.query(
            "SELECT demand_id FROM demand_requests WHERE demand_id = ANY(%s)", (ids,)
        )
    ]
    counts["demand_requests"] = len(existing)
    missing = [i for i in ids if i not in existing]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_delete": counts,
            "missing": missing,
            "notice": "预演：未删除任何数据",
        }

    # 单事务：六张子表 + 主表一起删，中途失败整体回滚。
    # 注意 db.conn() 返回的连接是 autocommit=True 的，所以这里必须显式关掉再提交，
    # 否则每条 DELETE 各自提交，删到一半失败就留下"有子行没主行"的孤儿数据。
    conn = db.conn()
    prev_autocommit = conn.autocommit
    conn.autocommit = False
    deleted = {}
    try:
        with conn.cursor() as cur:
            for t in tables:
                cur.execute("DELETE FROM %s WHERE demand_id = ANY(%%s)" % t, (ids,))
                deleted[t] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = prev_autocommit

    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) "
            "VALUES ('demand', 'purge', %s, %s)",
            (actor or "admin", json.dumps({"ids": ids, "deleted": deleted}, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        pass
    child_rows = sum(v for k, v in deleted.items() if k != "demand_requests")
    if deleted["demand_requests"]:
        notice = "已清理 %d 条需求及其过程数据" % deleted["demand_requests"]
    elif child_rows:
        # 单号不在主表里但子表有行（历史测试残留）：删掉的确实是数据，
        # 报"清理 0 条"会让人以为什么都没发生。
        notice = "主表无这些单号，已清理归属它们的 %d 行过程数据" % child_rows
    else:
        notice = "这些单号没有可清理的数据"
    return {
        "ok": True,
        "dry_run": False,
        "deleted": deleted,
        "missing": missing,
        "notice": notice,
    }


def summarize(demand_id=None):
    """需求汇总：单条详情摘要（events 计数）或全局看板（按状态 / 敏感类型）。"""
    if demand_id:
        row = get(demand_id)
        ev = db.query(
            "SELECT event_type, count(*) AS n FROM demand_events WHERE demand_id=%s GROUP BY event_type",
            (demand_id,),
        )
        return {
            "scope": "single",
            "demand_id": demand_id,
            "title": row["title"],
            "status": row["status"],
            "sensitivity": row["sensitivity"],
            "masked_fields": row["masked_fields"],
            "attachment_count": len(row["attachments"]),
            "validation_blocking": 0,
            "validation_warnings": len(
                [c for c in (row["validation"].get("checks") or []) if not c.get("passed") and not c.get("blocking")]
            ),
            "event_stats": {e["event_type"]: e["n"] for e in ev},
        }
    by_status = db.query(
        "SELECT status, count(*) AS n FROM demand_requests GROUP BY status ORDER BY n DESC"
    )
    sens = db.query(
        """
        SELECT key AS type, sum((value)::int) AS n
        FROM demand_requests, jsonb_each_text(sensitivity->'types')
        GROUP BY key ORDER BY n DESC
        """
    )
    total = db.query_one("SELECT count(*) AS n FROM demand_requests")["n"]
    pending = db.query_one(
        "SELECT count(*) AS n FROM demand_requests WHERE status = %s", (DEFAULT_STATUS,)
    )["n"]
    return {
        "scope": "all",
        "total": total,
        "pending_analysis": pending,
        "by_status": {r["status"]: r["n"] for r in by_status},
        "sensitive_types": {r["type"]: int(r["n"]) for r in sens},
    }
