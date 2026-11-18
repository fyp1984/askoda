# -*- coding: utf-8 -*-
"""全量人工复核（FR-6.7 · AC-13 · D15）

产品内核之一：**质量内核**。与「智能查数」并列——不是等级，是两件事都要做。

铁律：**全量人工复核，不看置信度分流**
--------------------------------------
每一条生成的 SQL 都进复核队列，系统只出「快捷草稿」。
置信度是给复核人排序用的**参考**，**不是**分流开关：
  · 高置信度 ≠ 免复核（实测里最自信的错答恰恰最危险）；
  · 未经人工在真实环境预测试确认，**不得**标注「可执行」。

因此本模块：
  · `enqueue()` 对每条 SQL **无条件**建任务（幂等键 = sql_run_id）；
  · 复核结论 `decide()` **只增不改**（append-only），旧结论留在历史里可回放；
  · 任务状态由**最新一条结论**派生，不由置信度决定。

与「门禁」的分工
----------------
门禁（gateway/gates.py）是**机器**判「SQL 写得对不对」；
复核是**人**判「这条 SQL 是不是业务真要的那一条」。
两者都通过，才叫「可交付」。
"""
import datetime as dt
import secrets

import db


# 复核任务状态（由最新结论派生，代码不直接赋值除这几个以外的值）
STATUS_PENDING = "待复核"
STATUS_APPROVED = "已通过"
STATUS_REJECTED = "已退回"
STATUS_REVISED = "已改写"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_REVISED)

# 复核结论取值 → 派生出的任务状态
_DECISION_TO_STATUS = {
    "通过": STATUS_APPROVED,
    "退回": STATUS_REJECTED,
    "改写": STATUS_REVISED,
}

# 对外声明的策略（前端展示「为什么不按置信度分流」）
POLICY = {
    "full_review": "全量人工复核",
    "confidence_routing": False,
    "note": "每条 SQL 都进人工复核队列；置信度仅供复核排序参考，不作为免复核依据。"
            "未经人工在真实环境预测试确认，不得标注「可执行」。",
}


class ReviewError(RuntimeError):
    pass


def _new_task_id():
    return "RV-" + secrets.token_hex(8)


def _new_decision_id():
    return "RD-" + secrets.token_hex(8)


# ---------------------------------------------------------------------------
# 入队（全量）
# ---------------------------------------------------------------------------
def enqueue(demand_id, sql_run_id, dataset="B", sql="", generator="",
            confidence=None, actor="system"):
    """把一条 SQL 运行送进复核队列。**幂等**：同一 sql_run_id 只建一个任务。

    返回 {"ok": True, "task_id": ..., "created": bool, "status": ...}
    """
    if not demand_id or not sql_run_id:
        return {"ok": False, "error": "enqueue 需要 demand_id 与 sql_run_id"}

    try:
        row = db.query_one(
            "SELECT task_id, status FROM review_tasks WHERE sql_run_id=%s",
            (sql_run_id,),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "review_tasks 查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}

    if row:
        return {"ok": True, "task_id": row["task_id"], "created": False, "status": row["status"]}

    task_id = _new_task_id()
    try:
        db.execute(
            """
            INSERT INTO review_tasks
                (task_id, demand_id, sql_run_id, dataset, sql_text, generator, confidence, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (task_id, demand_id, sql_run_id, dataset, (sql or "")[:20000],
             generator or "", confidence, STATUS_PENDING),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "review_tasks 写入失败 %s: %s" % (type(e).__name__, str(e)[:200])}

    return {"ok": True, "task_id": task_id, "created": True, "status": STATUS_PENDING}


# ---------------------------------------------------------------------------
# 队列 / 明细
# ---------------------------------------------------------------------------
def list_queue(status=None, dataset=None, limit=50):
    """复核队列（跨需求，待复核优先 + 时间倒序）。"""
    where, args = [], []
    if status:
        where.append("status=%s")
        args.append(status)
    if dataset:
        where.append("dataset=%s")
        args.append(dataset)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        rows = db.query(
            "SELECT task_id, demand_id, sql_run_id, dataset, generator, confidence, status, "
            "       sql_text, created_at::text AS created_at, updated_at::text AS updated_at "
            "FROM review_tasks " + clause + " "
            "ORDER BY (status = %s) DESC, created_at DESC LIMIT %s",
            tuple(args) + (STATUS_PENDING, int(limit)),
        )
        total = db.query_one(
            "SELECT count(*) AS n FROM review_tasks " + clause, tuple(args)
        )["n"]
        pending_clause = clause + (" AND " if clause else "WHERE ") + "status=%s"
        pending = db.query_one(
            "SELECT count(*) AS n FROM review_tasks " + pending_clause,
            tuple(args) + (STATUS_PENDING,),
        )["n"]
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "复核队列查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}

    for r in rows:
        r["sql_preview"] = (r.get("sql_text") or "")[:300]
        r.pop("sql_text", None)
    return {
        "ok": True,
        "total": int(total),
        "pending": int(pending),
        "items": rows,
        "policy": POLICY,
    }


def get(demand_id):
    """某需求的复核任务 + 每个任务的结论历史（append-only）。"""
    try:
        tasks = db.query(
            "SELECT task_id, demand_id, sql_run_id, dataset, generator, confidence, status, "
            "       sql_text, created_at::text AS created_at, updated_at::text AS updated_at "
            "FROM review_tasks WHERE demand_id=%s ORDER BY created_at DESC",
            (demand_id,),
        )
        decisions = db.query(
            "SELECT decision_id, task_id, decision, reviewer, note, rewritten_sql, "
            "       created_at::text AS created_at "
            "FROM review_decisions WHERE demand_id=%s ORDER BY created_at ASC",
            (demand_id,),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "复核明细查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}

    by_task = {}
    for d in decisions:
        by_task.setdefault(d["task_id"], []).append(d)
    for t in tasks:
        t["decisions"] = by_task.get(t["task_id"], [])
        t["latest_decision"] = t["decisions"][-1] if t["decisions"] else None
    return {"ok": True, "demand_id": demand_id, "total": len(tasks), "items": tasks, "policy": POLICY}


# ---------------------------------------------------------------------------
# 结论（只增不改）
# ---------------------------------------------------------------------------
def decide(task_id, decision, reviewer="", note="", rewritten_sql=""):
    """登记一条复核结论。**不覆盖历史**：每次 = 新增一条 decision 行。

    decision 取值：通过 / 退回 / 改写。
    """
    if decision not in _DECISION_TO_STATUS:
        return {
            "ok": False,
            "error": "decision 必须是 %s 之一，实际 %r"
                     % ("、".join(_DECISION_TO_STATUS.keys()), decision),
        }
    if not str(reviewer or "").strip():
        return {"ok": False, "error": "复核人（reviewer）必填：复核必须留痕到人。"}
    if decision in ("退回", "改写") and not str(note or "").strip():
        return {"ok": False, "error": "「%s」必须写明理由（note）。" % decision}
    if decision == "改写" and not str(rewritten_sql or "").strip():
        return {"ok": False, "error": "「改写」必须给出改写后的 SQL（rewritten_sql）。"}

    try:
        task = db.query_one(
            "SELECT task_id, demand_id, status FROM review_tasks WHERE task_id=%s",
            (task_id,),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "review_tasks 查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}
    if not task:
        return {"ok": False, "error": "复核任务 %s 不存在" % task_id}

    new_status = _DECISION_TO_STATUS[decision]
    decision_id = _new_decision_id()
    try:
        db.execute(
            """
            INSERT INTO review_decisions
                (decision_id, task_id, demand_id, decision, reviewer, note, rewritten_sql)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (decision_id, task_id, task["demand_id"], decision, reviewer,
             note or "", (rewritten_sql or "")[:20000]),
        )
        db.execute(
            "UPDATE review_tasks SET status=%s, updated_at=now() WHERE task_id=%s",
            (new_status, task_id),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "复核结论写入失败 %s: %s" % (type(e).__name__, str(e)[:200])}

    return {
        "ok": True,
        "task_id": task_id,
        "decision_id": decision_id,
        "decision": decision,
        "status": new_status,
        "reviewer": reviewer,
        "decided_at": dt.datetime.now().isoformat(timespec="seconds"),
        "policy": POLICY,
    }


def summary(demand_id=None, dataset=None):
    """复核态汇总：各状态计数 + 是否「全部已复核通过」。

    供交付页判定「未经人工确认不得标可执行」。
    """
    where, args = [], []
    if demand_id:
        where.append("demand_id=%s")
        args.append(demand_id)
    if dataset:
        where.append("dataset=%s")
        args.append(dataset)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    try:
        rows = db.query(
            "SELECT status, count(*) AS n FROM review_tasks %s GROUP BY status" % clause,
            tuple(args),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "复核汇总查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}
    counts = {s: 0 for s in STATUSES}
    for r in rows:
        counts[r["status"]] = int(r["n"])
    total = sum(counts.values())
    return {
        "ok": True,
        "demand_id": demand_id,
        "total": total,
        "counts": counts,
        "all_approved": total > 0 and counts[STATUS_PENDING] == 0 and counts[STATUS_REJECTED] == 0,
        "policy": POLICY,
    }
