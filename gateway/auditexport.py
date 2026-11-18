# -*- coding: utf-8 -*-
"""审计导出与对接（FR-7.7 · P2）

导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条 Demo 中照常呈现**。
本模块落地 FR-7.7：「审计日志可按客户 SIEM 要求导出（CSV / Syslog）」。

导出内容对齐 FR-7.4 的全链路审计要素
------------------------------------
  提问人 / 需求号 / 问题、生成 SQL、引用口径版本（requirement_version）、
  扫及表（schema_version 锚点）、执行耗时、返回行数、试跑次数、是否人工改写。

数据从哪来
----------
`sql_runs`（执行留痕，主表） LEFT JOIN `demand_requests`（还原问题标题）；
「是否人工改写」取 `review_decisions` 中该需求是否存在 decision='改写' 的结论。
全部只读，不写任何库。

两种交付格式
------------
  · csv    —— RFC4180 带表头，逗号分隔，字段内换行 / 引号按标准转义
  · syslog —— 每行一条（RFC5424 风格：`<PRI>1 时间 主机 应用 - - - 结构化消息`），
              供直接投递客户 SIEM 采集器

数字口径（数字纪律）
--------------------
留存 ≥180 天为 PRD 带 `[假设]` 的**目标值**，导出头部标注为「目标，非实测」。
"""
import csv
import datetime as dt
import io
import json

import db

# 审计留存目标（PRD FR-7.4 带 [假设]）—— 目标值，非实测
RETENTION_TARGET_DAYS = 180
RETENTION_NOTE = "≥%d 天（目标值 / 演示参数，非实测）" % RETENTION_TARGET_DAYS

# 导出列（顺序即 CSV 表头顺序）
COLUMNS = [
    ("created_at", "时间"),
    ("actor", "提问人"),
    ("demand_id", "需求号"),
    ("demand_title", "问题"),
    ("sql_run_id", "执行号"),
    ("generator_model", "生成路径"),
    ("schema_version", "扫及表版本"),
    ("requirement_version", "引用口径版本"),
    ("row_count", "返回行数"),
    ("elapsed_ms", "执行耗时(ms)"),
    ("retry_count", "试跑次数"),
    ("review_status", "审查结论"),
    ("revised", "是否人工改写"),
    ("adopted", "是否采用交付"),
]


def _rows(dataset="", demand_id="", limit=500):
    """按条件取审计行（sql_runs 为主表）。返回 (行清单, 出错说明)。

    「提问人」取 `demand_requests.contact`（提交人联系方式即提问人，需求表无独立 actor 列）。
    `sql_runs` 自身不带 dataset，按数据集筛选走 `structured_requirements`（该表有 dataset）
    的 EXISTS 子查询——不用 JOIN，避免同一需求多版本结构化需求把审计行放大成多行。
    查询失败**不静默退成空表**：把原因带回去，避免「0 行」被误读成「本来就没事」。
    """
    where, params = [], []
    if demand_id:
        where.append("r.demand_id = %s")
        params.append(demand_id)
    if dataset:
        where.append(
            "EXISTS (SELECT 1 FROM structured_requirements s "
            "WHERE s.demand_id = r.demand_id AND s.dataset = %s)"
        )
        params.append(dataset)
    sql = (
        "SELECT r.sql_run_id, r.demand_id, r.generator_model, r.review_status, "
        "       r.schema_version, r.requirement_version, r.validation_result, "
        "       r.final_delivery_sql, r.created_at::text AS created_at, "
        "       d.contact AS demand_actor, d.title AS demand_title "
        "FROM sql_runs r LEFT JOIN demand_requests d ON d.demand_id = r.demand_id "
    )
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY r.created_at DESC LIMIT %s"
    params.append(limit)
    try:
        rows = db.query(sql, tuple(params))
    except Exception as e:  # noqa: BLE001 —— 不退成「假空表」，把原因回传
        return [], "%s: %s" % (type(e).__name__, str(e)[:240])

    # 「是否人工改写」：查该批需求里存在 decision='改写' 的结论
    revised_ids = set()
    # 「试跑次数」：该需求在 sql_runs 里的执行条数（每条 sql_runs = 一次试跑）
    trial_counts = {}
    try:
        ids = [r["demand_id"] for r in rows]
        if ids:
            for d in db.query(
                "SELECT DISTINCT demand_id FROM review_decisions "
                "WHERE decision = '改写' AND demand_id = ANY(%s)",
                (ids,),
            ):
                revised_ids.add(d["demand_id"])
            for d in db.query(
                "SELECT demand_id, COUNT(*) AS n FROM sql_runs "
                "WHERE demand_id = ANY(%s) GROUP BY demand_id",
                (ids,),
            ):
                trial_counts[d["demand_id"]] = d["n"]
    except Exception:  # noqa: BLE001
        pass

    out = []
    for r in rows:
        vr = r.get("validation_result") or {}
        if isinstance(vr, str):
            try:
                vr = json.loads(vr)
            except Exception:  # noqa: BLE001
                vr = {}
        out.append(
            {
                "created_at": r.get("created_at") or "",
                "actor": r.get("demand_actor") or "",
                "demand_id": r.get("demand_id") or "",
                "demand_title": r.get("demand_title") or "",
                "sql_run_id": r.get("sql_run_id") or "",
                "generator_model": r.get("generator_model") or "",
                "schema_version": r.get("schema_version") or "",
                "requirement_version": (
                    "" if r.get("requirement_version") is None else r.get("requirement_version")
                ),
                "row_count": vr.get("row_count", ""),
                "elapsed_ms": vr.get("elapsed_ms", vr.get("duration_ms", "")),
                "retry_count": trial_counts.get(r.get("demand_id"), ""),
                "review_status": r.get("review_status") or "",
                "revised": "是" if r.get("demand_id") in revised_ids else "否",
                "adopted": "是" if r.get("final_delivery_sql") else "否",
            }
        )
    return out, ""


def _to_csv(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([zh for _, zh in COLUMNS])
    for r in rows:
        writer.writerow([r.get(k, "") for k, _ in COLUMNS])
    return buf.getvalue()


def _to_syslog(rows):
    """RFC5424 风格：`<PRI>1 时间 主机 应用 进程 消息ID - 结构化数据`。

    主机名固定为 `askoda`，应用名 `askoda-audit`，消息体为单行 JSON——
    客户 SIEM 既可按字段解析，也可整行入库。
    """
    lines = []
    for r in rows:
        ts = r.get("created_at") or dt.datetime.now().isoformat()
        # 时间戳形如 2026-11-18 10:00:00+08:00 → 转成 RFC5424 的 T 分隔
        ts = ts.replace(" ", "T")
        msg = json.dumps(
            {"app": "askoda-audit", "event": "sql_run", **r}, ensure_ascii=False
        )
        lines.append("<134>1 %s askoda askoda-audit - - - %s" % (ts, msg))
    return "\n".join(lines) + ("\n" if lines else "")


def export(dataset="", demand_id="", limit=500, fmt="csv"):
    """导出审计日志。fmt ∈ {csv, syslog}；返回内容 + 文件名 + 数字口径说明。"""
    fmt = (fmt or "csv").strip().lower()
    if fmt not in ("csv", "syslog"):
        return {"ok": False, "error": "不支持的导出格式「%s」；仅支持 csv / syslog" % fmt}

    rows, err = _rows(dataset=dataset, demand_id=demand_id, limit=limit)
    content = _to_csv(rows) if fmt == "csv" else _to_syslog(rows)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ext = "csv" if fmt == "csv" else "log"
    name = "askoda-audit-%s.%s" % (stamp, ext)
    result = {
        "ok": True,
        "format": fmt,
        "count": len(rows),
        "filename": name,
        "content": content,
        "columns": [zh for _, zh in COLUMNS],
        "retention": RETENTION_NOTE,
        "audit_fields": "对齐 FR-7.4：提问人 / 问题 / 生成 SQL / 引用口径版本 / 扫及表 / "
                        "执行耗时 / 返回行数 / 试跑次数 / 是否人工改写",
        "note": "导出行为本身也留痕（审计导出与对接，FR-7.7）；留存期为%s。" % RETENTION_NOTE,
    }
    # 取数失败时如实回传原因：0 行 = 取数失败 与 0 行 = 本就无留痕，必须能分清
    if err:
        result["warning"] = "审计取数未成功（返回空表）：%s" % err
    return result


def export_and_log(actor="", dataset="", demand_id="", limit=500, fmt="csv"):
    """导出并在 `config_events` 记一条动作留痕（导出动作可审计）。"""
    result = export(dataset=dataset, demand_id=demand_id, limit=limit, fmt=fmt)
    if result.get("ok"):
        try:
            db.execute(
                "INSERT INTO config_events (scope, action, actor, detail) "
                "VALUES ('audit', 'export', %s, %s)",
                (
                    actor or "admin",
                    json.dumps(
                        {"format": fmt, "count": result.get("count"),
                         "demand_id": demand_id or "", "file": result.get("filename")},
                        ensure_ascii=False,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 —— 留痕失败不影响导出结果返回
            pass
    return result
