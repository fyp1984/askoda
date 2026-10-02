#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-1 · rules.py 离线自证脚本（仓库根目录运行：python3 tools/rules_selftest.py）

设计目的
--------
对 10 条规则逐条验证：
    (1) 正例：必须命中 ≥ 1 条该 rule 的 issues；
    (2) 反例：必须命中 = 0 条该 rule 的 issues。
离线纯 AST 验证，不连 DB、不连 Wren、无任何副作用。
对于 R5 / R6 / R1 需要 MDL 的规则，使用本地 mdl.json 构造最小化 mdl_index，
不依赖 registry 的容器路径。
"""
import sys
import os
import json
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join(ROOT, "gateway")
sys.path.insert(0, GATEWAY)

import sqlglot
from sqlglot import exp
import rules as rules_mod


# ==========================================================================
# 工具：构建最小化 mdl_index（用于 R5/R6/R1；MDL 来源 = B 库 workspace）
# ==========================================================================
def build_mdl_index_b():
    """构造 B 库 mdl_index，形状对齐 rules.build_mdl_index 输出。

    只用到：all_tables（模型名+物理表名 lower）、all_columns（三层：col / model.col / table.col）、
    time_cols（同三层）、rels（relationships 原文）。
    严格按 rules.build_mdl_index 的策略：列名正则命中，或 label 开头/全等时间词。
    """
    mdl_path = os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json")
    with open(mdl_path, "r", encoding="utf-8") as f:
        mdl = json.load(f)
    models = mdl.get("models", []) or []
    all_tables = set()
    all_columns = set()
    time_cols = set()
    import re as _re
    import metadata as md_mod
    time_norms = [w.strip().lower().replace(" ", "").replace('"', "").replace("'", "") for w in
                  ["日期", "时间", "date", "dt", "month", "月份",
                   "统计月份", "统计日期", "时间戳", "timestamp"]]
    col_time_pat = _re.compile(
        r"(日期|时间|date|dt|month|月份|统计月份|统计日期|timestamp|_date$|_dt$|_month$|stat_date|stat_month|order_date|open_date|register_date|change_month)",
        _re.IGNORECASE,
    )

    def _is_label_time(label_n: str) -> bool:
        if not label_n:
            return False
        for tn in time_norms:
            if not tn:
                continue
            if label_n == tn or label_n.startswith(tn):
                return True
        return False

    for m in models:
        m_name = m.get("name") or ""
        tbl_ref = m.get("tableReference") or {}
        phys = tbl_ref.get("table") or ""
        all_tables.add(m_name.lower())
        if phys:
            all_tables.add(phys.lower())
        cols = m.get("columns", []) or []
        for c in cols:
            c_name = c.get("name") or ""
            desc = c.get("description") or ""
            if c_name:
                all_columns.add(c_name.lower())
                all_columns.add("%s.%s" % (m_name.lower(), c_name.lower()))
                if phys:
                    all_columns.add("%s.%s" % (phys.lower(), c_name.lower()))
                label, _ = md_mod.split_label_caliber(desc)
                label_n = (label or "").strip().lower().replace(" ", "").replace('"', "").replace("'", "")
                hit_time = False
                if col_time_pat.search(c_name or ""):
                    hit_time = True
                if not hit_time and _is_label_time(label_n):
                    hit_time = True
                if hit_time:
                    time_cols.add(c_name.lower())
                    time_cols.add("%s.%s" % (m_name.lower(), c_name.lower()))
                    if phys:
                        time_cols.add("%s.%s" % (phys.lower(), c_name.lower()))
    return {
        "models": {m.get("name"): m for m in models},
        "tables": {m.get("name"): (m.get("tableReference") or {}).get("table") for m in models},
        "all_tables": all_tables,
        "all_columns": all_columns,
        "time_cols": time_cols,
        "rels": mdl.get("relationships", []) or [],
    }


MDL_B = build_mdl_index_b()
CTX_B = {"dataset": "B", "mdl_index": MDL_B, "requirement": None}


def run(sql: str, ctx=None):
    """跑单条 SQL，返回 list[dict] rules issues。"""
    ast_stmts = list(sqlglot.parse(sql, read="postgres"))
    issues = []
    for s in ast_stmts:
        issues.extend(rules_mod.evaluate_all(s, ctx or {"dataset": "B", "mdl_index": None, "requirement": None}))
    return issues


def count_rule(issues, rule_id):
    return sum(1 for x in issues if x.get("rule") == rule_id)


def assert_pos(name, sql, rule_id, ctx=None, extra_ok=None, _show=False):
    """正例：必须命中该 rule。"""
    issues = run(sql, ctx=ctx)
    n = count_rule(issues, rule_id)
    ok = n >= 1
    if extra_ok is not None:
        ok = ok and extra_ok(issues)
    mark = "[OK]" if ok else "[!!]"
    print("%s %s 正例 %s  命中 %s=%d  总issues=%d" % (mark, rule_id, name, rule_id, n, len(issues)))
    if not ok or _show:
        for x in issues:
            print("    · %s | %s | %s" % (x.get("rule"), x.get("snippet")[:120], x.get("detail")[:80]))
    return ok


def assert_neg(name, sql, rule_id, ctx=None, _show=False):
    """反例：必须 0 命中该 rule。"""
    issues = run(sql, ctx=ctx)
    n = count_rule(issues, rule_id)
    ok = n == 0
    mark = "[OK]" if ok else "[!!]"
    print("%s %s 反例 %s  命中 %s=%d" % (mark, rule_id, name, rule_id, n))
    if not ok or _show:
        for x in issues:
            if x.get("rule") == rule_id:
                print("    · [误报] %s | %s | %s" % (x.get("rule"), x.get("snippet")[:120], x.get("detail")[:80]))
    return ok


# ==========================================================================
# 用例主体
# ==========================================================================
def main():
    all_ok = True
    # ----------------------------------------------------------------
    # 1. JOIN_WITHOUT_CONDITION
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "跨表无ON",
        "SELECT a.member_id, b.store_name FROM dim_member a, dim_store b",
        "JOIN_WITHOUT_CONDITION",
    )
    all_ok &= assert_neg(
        "带等值连接",
        "SELECT a.member_id, b.store_name FROM dim_member a JOIN dim_store b ON a.store_id = b.store_id",
        "JOIN_WITHOUT_CONDITION",
    )
    # ----------------------------------------------------------------
    # 2. SELECT_STAR
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "SELECT*",
        "SELECT * FROM dws_store_daily_agg",
        "SELECT_STAR",
    )
    all_ok &= assert_neg(
        "显式列",
        "SELECT stat_date, store_id, sales_amount FROM dws_store_daily_agg",
        "SELECT_STAR",
    )
    # ----------------------------------------------------------------
    # 3. GROUP_BY_INCONSISTENT（M6-0 PT-2 老判定迁移）
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "两维只分组一维",
        "SELECT store_id, stat_date, SUM(sales_amount) FROM dws_store_daily_agg GROUP BY store_id",
        "GROUP_BY_INCONSISTENT",
    )
    all_ok &= assert_neg(
        "全分组（序号分组）",
        "SELECT store_id, stat_date, SUM(sales_amount) FROM dws_store_daily_agg GROUP BY 1, 2",
        "GROUP_BY_INCONSISTENT",
    )
    # ----------------------------------------------------------------
    # 4. UNNECESSARY_DISTINCT（R7 强化版：单表无聚合无JOIN 也判）
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "单表无聚合套DISTINCT",
        "SELECT DISTINCT store_id, store_name FROM dim_store",
        "UNNECESSARY_DISTINCT",
    )
    all_ok &= assert_neg(
        "有JOIN需DISTINCT",
        "SELECT DISTINCT a.member_id, b.store_name FROM dim_member a JOIN dim_store b ON a.store_id=b.store_id",
        "UNNECESSARY_DISTINCT",
    )
    # ----------------------------------------------------------------
    # 5. R1 ONE_TO_MANY_UNHANDLED
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "复购明细JOIN dws聚合放大场景（未声明关系，梯次2命中）",
        """
        SELECT a.stat_month, a.store_id, SUM(a.repurchase_flag) AS repurchase_users,
               SUM(b.sales_amount) AS gmv
        FROM ads_member_repurchase_di a
        JOIN dws_store_daily_agg b ON a.store_id = b.store_id
        GROUP BY a.stat_month, a.store_id
        """,
        "ONE_TO_MANY_UNHANDLED",
        ctx=CTX_B,
    )
    all_ok &= assert_neg(
        "单表聚合",
        "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg GROUP BY store_id",
        "ONE_TO_MANY_UNHANDLED",
        ctx=CTX_B,
    )
    all_ok &= assert_neg(
        "已声明MANY_TO_ONE: dws⋈dim_store + 聚合（不应放大）",
        """
        SELECT b.region_name, SUM(a.sales_amount) AS gmv
        FROM dws_store_daily_agg a
        JOIN dim_store b ON a.store_id = b.store_id
        GROUP BY b.region_name
        """,
        "ONE_TO_MANY_UNHANDLED",
        ctx=CTX_B,
    )
    all_ok &= assert_neg(
        "已声明MANY_TO_ONE: dwd_order_di⋈dim_member + COUNT（多对一不放大）",
        """
        SELECT b.member_status, COUNT(DISTINCT a.order_id) AS order_cnt
        FROM dwd_order_di a
        JOIN dim_member b ON a.member_id = b.member_id
        GROUP BY b.member_status
        """,
        "ONE_TO_MANY_UNHANDLED",
        ctx=CTX_B,
    )
    # ----------------------------------------------------------------
    # 6. R2 LOAD_DATE_SUBSTITUTION — dw_insert_time 命中
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "WHERE dw_insert_time",
        """
        SELECT stat_date, store_id, SUM(sales_amount) amt
        FROM dws_store_daily_agg
        WHERE dw_insert_time >= '2025-01-01'
        GROUP BY stat_date, store_id
        ORDER BY dw_insert_time
        """,
        "LOAD_DATE_SUBSTITUTION",
    )
    all_ok &= assert_neg(
        "WHERE 业务日期order_date",
        "SELECT store_id, SUM(pay_amount) FROM dwd_order_di WHERE order_date = '2025-03-01' GROUP BY store_id",
        "LOAD_DATE_SUBSTITUTION",
    )
    # ----------------------------------------------------------------
    # 7. R3 MULTI_VALUE_NO_ORDER — GROUP_CONCAT 无 ORDER BY
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "GROUP_CONCAT无ORDER BY",
        """
        SELECT store_id, GROUP_CONCAT(sku_id, ',') AS skus
        FROM dwd_order_detail_di
        GROUP BY store_id
        """,
        "MULTI_VALUE_NO_ORDER",
    )
    all_ok &= assert_neg(
        "GROUP_CONCAT带ORDER BY",
        """
        SELECT store_id, GROUP_CONCAT(sku_id, ',' ORDER BY sku_id) AS skus
        FROM dwd_order_detail_di
        GROUP BY store_id
        """,
        "MULTI_VALUE_NO_ORDER",
    )
    # ----------------------------------------------------------------
    # 8. R4 DANGLING_DENOMINATOR — 单表输出复购率 无除号无JOIN
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "输出复购率无分母",
        """
        SELECT store_id, SUM(repurchase_flag) AS 复购用户数,
               AVG(repurchase_flag) AS 复购率
        FROM ads_member_repurchase_di
        GROUP BY store_id
        """,
        "DANGLING_DENOMINATOR",
    )
    all_ok &= assert_neg(
        "A/B 除法分母明确",
        "SELECT store_id, SUM(sales_amount) / NULLIF(SUM(area_sqm), 0) AS 坪效 FROM dws_store_daily_agg GROUP BY store_id",
        "DANGLING_DENOMINATOR",
    )
    # ----------------------------------------------------------------
    # 9. R5 UNMAPPED_OBJECT_REF — 引用不存在模型
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "引用 nonexistent_table",
        "SELECT foo_id, bar_col FROM nonexistent_table",
        "UNMAPPED_OBJECT_REF",
        ctx=CTX_B,
    )
    all_ok &= assert_neg(
        "正常SQL（CTE+子查询别名）",
        """
        WITH monthly AS (
            SELECT store_id, SUM(sales_amount) AS amt
            FROM dws_store_daily_agg
            GROUP BY store_id
        )
        SELECT m.store_id, s.store_name, m.amt
        FROM monthly m
        JOIN dim_store s ON m.store_id = s.store_id
        """,
        "UNMAPPED_OBJECT_REF",
        ctx=CTX_B,
    )
    # ----------------------------------------------------------------
    # 10. R6 TIME_FIELD_SUSPECT — stat_date 不报，dw_insert_time 报
    # ----------------------------------------------------------------
    all_ok &= assert_pos(
        "WHERE dw_insert_time 非MDL时间候选",
        """
        SELECT store_id, SUM(sales_amount)
        FROM dws_store_daily_agg
        WHERE dw_insert_time >= '2025-01-01'
        GROUP BY store_id
        """,
        "TIME_FIELD_SUSPECT",
        ctx=CTX_B,
    )
    all_ok &= assert_neg(
        "WHERE stat_date 标准时间候选",
        "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date >= '2025-01-01' GROUP BY store_id",
        "TIME_FIELD_SUSPECT",
        ctx=CTX_B,
    )

    print()
    if all_ok:
        print("==== 全部通过 ====")
        sys.exit(0)
    else:
        print("==== 有失败项！ ====")
        sys.exit(1)


if __name__ == "__main__":
    main()
