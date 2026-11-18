# -*- coding: utf-8 -*-
"""本体可视化浏览（FR-2.6 · P2）

导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条（FR-4.8 / FR-2.6 / FR-7.7）Demo 中照常呈现**。
本模块落地 FR-2.6：「从业务域可下钻到实体 → 表 → 字段 → 引用该字段的 CASE，路径可点击」。

四层下钻（每一层返回下一层的可点击键）
------------------------------------
  业务域 domain → 实体/表 table → 字段 column → CASE（引用该字段的查询案例）

数据从哪来
----------
全部来自仓库自有元数据库，不新建数据源、不调大模型：
  · 业务域 / 实体 / 表 —— `table_docs`（含 domain / table_label / grain / description）
  · 字段            —— `column_docs`（含中文名 / 类型 / 敏感标记 / 主键）
  · CASE            —— `sql_runs`（历史生成 / 交付的查询），按字段名做包含匹配；
                       关联 `demand_requests.title` 还原「这条 CASE 回答的是哪个问题」。

只读：本模块不写任何库，纯浏览。
"""
import db


def _rows(sql, params=()):
    try:
        return db.query(sql, params)
    except Exception:  # noqa: BLE001 —— 元数据表为空/未采集时退成空列表，不阻断浏览
        return []


def domains(dataset="B"):
    """第一层：该数据集下的业务域清单（每个域挂的实体数）。"""
    rows = _rows(
        "SELECT coalesce(nullif(trim(domain), ''), '未归类') AS domain, "
        "       count(*) AS table_count "
        "FROM table_docs WHERE dataset=%s "
        "GROUP BY coalesce(nullif(trim(domain), ''), '未归类') ORDER BY table_count DESC, domain",
        (dataset,),
    )
    return [{"key": r["domain"], "label": r["domain"], "table_count": r["table_count"]} for r in rows]


def entities(dataset, domain):
    """第二层：某业务域下的实体（= 物理表 / 视图）。"""
    rows = _rows(
        "SELECT table_name, table_label, grain, description, row_estimate "
        "FROM table_docs WHERE dataset=%s AND coalesce(nullif(trim(domain), ''), '未归类')=%s "
        "ORDER BY table_name",
        (dataset, domain),
    )
    return [
        {
            "key": r["table_name"],
            "label": r["table_label"] or r["table_name"],
            "table_name": r["table_name"],
            "grain": r["grain"] or "",
            "description": r["description"] or "",
            "row_estimate": r["row_estimate"],
        }
        for r in rows
    ]


def columns(dataset, table):
    """第三层：某实体下的字段（中文名 / 类型 / 敏感标记 / 主键）。"""
    rows = _rows(
        "SELECT column_name, column_label, data_type, nullable, is_sensitive, "
        "       is_primary_key, description "
        "FROM column_docs WHERE dataset=%s AND table_name=%s ORDER BY column_name",
        (dataset, table),
    )
    return [
        {
            "key": r["column_name"],
            "label": r["column_label"] or r["column_name"],
            "column_name": r["column_name"],
            "data_type": r["data_type"] or "",
            "nullable": r["nullable"],
            "is_sensitive": bool(r["is_sensitive"]),
            "is_primary_key": bool(r["is_primary_key"]),
            "description": r["description"] or "",
        }
        for r in rows
    ]


def cases(column, limit=50, dataset=""):
    """第四层：引用该字段的 CASE（历史生成 / 交付的查询）。

    匹配口径：`sql_runs.generated_sql` 或 `final_delivery_sql` 中出现该字段名。
    只做**包含匹配**——不解析 SQL 语义，宁可多报不漏报，人工一眼可核。
    """
    like = "%%%s%%" % column
    rows = _rows(
        "SELECT r.sql_run_id, r.demand_id, r.review_status, r.generator_model, "
        "       r.created_at::text AS created_at, "
        "       (r.final_delivery_sql IS NOT NULL) AS adopted, "
        "       d.title AS demand_title "
        "FROM sql_runs r LEFT JOIN demand_requests d ON d.demand_id = r.demand_id "
        "WHERE r.generated_sql ILIKE %s OR coalesce(r.final_delivery_sql, '') ILIKE %s "
        "ORDER BY r.created_at DESC LIMIT %s",
        (like, like, limit),
    )
    return [
        {
            "key": r["sql_run_id"],
            "sql_run_id": r["sql_run_id"],
            "demand_id": r["demand_id"],
            "demand_title": r["demand_title"] or "",
            "review_status": r["review_status"] or "",
            "adopted": bool(r["adopted"]),
            "generator_model": r["generator_model"] or "",
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def browse(dataset="B", domain="", table="", column="", limit=50):
    """按「当前选中到哪一层」返回本体浏览视图 + 可点击的面包屑路径。

    调用方逐层传参（domain → table → column），本函数返回：
      · `path`     —— 面包屑（每一段都可点击回退到该层）
      · `children` —— 当前层的下一层可下钻项
      · `cases`    —— 选到字段时，引用该字段的 CASE
    路径每一段都带 `level` 与查询键，前端据此把「业务域 → 实体 → 表 → 字段」串成可点链路。
    """
    menu_domains = domains(dataset)

    path = [{"level": "dataset", "label": "数据集 %s" % dataset, "dataset": dataset}]
    children = menu_domains
    level = "domain"

    if domain:
        path.append({"level": "domain", "label": domain, "domain": domain})
        children = entities(dataset, domain)
        level = "entity"

    if domain and table:
        path.append({"level": "entity", "label": table, "domain": domain, "table": table})
        children = columns(dataset, table)
        level = "column"

    case_rows = []
    if domain and table and column:
        path.append(
            {"level": "column", "label": column, "domain": domain, "table": table, "column": column}
        )
        children = []
        case_rows = cases(column, limit=limit, dataset=dataset)

    return {
        "ok": True,
        "dataset": dataset,
        "level": level,
        "path": path,
        "children": children,
        "cases": case_rows,
        "case_note": (
            "CASE = 历史生成 / 交付的查询中引用该字段者（按字段名包含匹配，人工一眼可核）。"
            if case_rows
            else "暂无历史查询引用该字段；随需求积累会在此沉淀为可复用的 CASE。"
        ),
    }
