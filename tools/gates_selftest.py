#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-1 五层 SQL 门禁自证（离线，不需要容器与 Wren）。

L4 语义门禁用注入的假 dry_run 函数桩：默认全部通过。
如需测试 L4 阻断，自行传 _dry_run_fn=lambda sql: {"ok": False, "message": "故意失败"}。

用法：
    python3 tools/gates_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 脚本定位："离线、不需要容器与 Wren"——直接引用仓库内的 workspace 副本，
# 不强制用户手动 export MDL_A_PATH / MDL_B_PATH。
# （若宿主已显式设置环境变量则不覆盖。）
os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import gates as gt  # noqa: E402


def _fake_dry_run_ok(sql):
    return {"ok": True, "message": "（假桩）语义层预演通过"}


def _layer_by(result, layer_key):
    for l in result["layers"]:
        if l["layer"] == layer_key:
            return l
    return None


def main():
    fails = []

    # ------------------------------------------------------------------
    # 1. 合法 SELECT → L1/L3 通过
    # ------------------------------------------------------------------
    title = "1. 合法 SELECT：L1/L3 通过，status=通过"
    sql = "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name ORDER BY cnt DESC"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l1 = _layer_by(r, "L1")
    l3 = _layer_by(r, "L3")
    ok = (
        r["status"] == "通过"
        and l1 and l1["ok"] and not l1["skipped"]
        and l3 and l3["ok"] and not l3["skipped"]
    )
    if not ok:
        fails.append("%s FAILED: status=%s L1=%s L3=%s" % (title, r["status"], l1, l3))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L1.ok=%s  L3.ok=%s" % (r["status"], l1["ok"] if l1 else None, l3["ok"] if l3 else None))

    # ------------------------------------------------------------------
    # 2. INSERT INTO t VALUES (1) → L3 不通过且写明节点类型
    # ------------------------------------------------------------------
    title = "2. INSERT → L3 不通过，写明 Insert 节点类型"
    sql = "INSERT INTO t VALUES (1)"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_insert_type = False
    for v in r["rule_violations"]:
        if v["layer"] == "L3" and "INSERT" in v.get("type", ""):
            has_insert_type = True
            break
    l3_detail_mentions = l3 and "Insert" in (l3.get("detail") or "")
    ok = (
        r["status"] == "不通过"
        and l3 and not l3["ok"]
        and has_insert_type
        and l3_detail_mentions
    )
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  detail_snippet=%s" % (
        r["status"], l3["ok"] if l3 else None,
        (l3.get("detail")[:60] + "...") if l3 and l3.get("detail") else None))
    print("    rule_violations(L3 only)=", [v for v in r["rule_violations"] if v["layer"] == "L3"])

    # ------------------------------------------------------------------
    # 3. DROP TABLE t → L3 不通过
    # ------------------------------------------------------------------
    title = "3. DROP TABLE → L3 不通过"
    sql = "DROP TABLE t"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_drop = any(v["layer"] == "L3" and "DROP" in v.get("type", "") for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l3 and not l3["ok"] and has_drop
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  violations(L3)=%s" % (
        r["status"], l3["ok"] if l3 else None,
        [v for v in r["rule_violations"] if v["layer"] == "L3"]))

    # ------------------------------------------------------------------
    # 4. SELECT * FROM a, b → L2 分级阻断（未建模表 + 无条件 JOIN 均属阻断类）
    # ------------------------------------------------------------------
    title = "4. SELECT * FROM a,b → L2 分级阻断（未建模表 + 无条件 JOIN 均属阻断类），status=不通过"
    sql = "SELECT * FROM a, b"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    has_join_cond = any(i["rule"] == "JOIN_WITHOUT_CONDITION" for i in r["semantic_issues"])
    has_select_star = any(i["rule"] == "SELECT_STAR" for i in r["semantic_issues"])
    has_unmapped = any(i["rule"] == "UNMAPPED_OBJECT_REF" for i in r["semantic_issues"])
    ok = (
        r["status"] == "不通过"
        and has_join_cond and has_select_star and has_unmapped
    )
    if not ok:
        fails.append("%s FAILED: status=%s issues=%s" % (
            title, r["status"], r["semantic_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  JOIN_WITHOUT_CONDITION=%s  SELECT_STAR=%s  UNMAPPED_OBJECT_REF=%s" % (
        r["status"], has_join_cond, has_select_star, has_unmapped))
    print("    semantic_issues rules=", sorted({i["rule"] for i in r["semantic_issues"]}))

    # ------------------------------------------------------------------
    # 5. SELECT (( → L1 不通过（语法错误）
    # ------------------------------------------------------------------
    title = "5. SELECT (( → L1 语法错误，status=不通过"
    sql = "SELECT (("
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l1 = _layer_by(r, "L1")
    ok = r["status"] == "不通过" and l1 and not l1["ok"] and len(r["syntax_issues"]) >= 1
    if not ok:
        fails.append("%s FAILED: status=%s L1=%s syntax=%s" % (
            title, r["status"], l1, r["syntax_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  syntax_issues=%s" % (r["status"], r["syntax_issues"]))

    # ------------------------------------------------------------------
    # 6. SELECT 1; SELECT 2 → L3 不通过（多语句）
    # ------------------------------------------------------------------
    title = "6. 多语句分号分隔 → L3 不通过，MULTI_STATEMENT"
    sql = "SELECT 1; SELECT 2"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_multi = any(v["layer"] == "L3" and v["type"] == "MULTI_STATEMENT" for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l3 and not l3["ok"] and has_multi
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  MULTI_STATEMENT=%s" % (
        r["status"], l3["ok"] if l3 else None, has_multi))

    # ------------------------------------------------------------------
    # 7. 插槽层 skipped=True 且不影响总状态
    # ------------------------------------------------------------------
    title = "7. SQLFluff/GX 插槽层 skipped=True，不拖垮 status"
    sql = "SELECT 1 AS one"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    sf = _layer_by(r, "L-SQLFluff")
    gx = _layer_by(r, "L-GX")
    ok = (
        sf and sf["skipped"]
        and gx and gx["skipped"]
        and r["status"] in ("通过", "警告")
    )
    if not ok:
        fails.append("%s FAILED: status=%s SQLFluff=%s GX=%s" % (
            title, r["status"], sf, gx))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  SQLFluff.skipped=%s  GX.skipped=%s  插槽细节reason=%s" % (
        r["status"],
        sf["skipped"] if sf else None,
        gx["skipped"] if gx else None,
        (sf or {}).get("detail"),
    ))

    # ------------------------------------------------------------------
    # 附加：is_write() 辅助函数
    # ------------------------------------------------------------------
    title = "附加-8. is_write() 正反向用例"
    write_cases = [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD c INT",
        "CREATE TABLE t (id INT)",
        "TRUNCATE TABLE t",
        "SELECT 1; SELECT 2",
        "SELECT ((",
    ]
    read_cases = [
        "SELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ]
    bad_w = [c for c in write_cases if not gt.is_write(c)]
    bad_r = [c for c in read_cases if gt.is_write(c)]
    ok = not bad_w and not bad_r
    if not ok:
        fails.append("%s FAILED: 写操作漏拦=%s 读操作误拦=%s" % (title, bad_w, bad_r))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    写操作漏拦=%d 读操作误拦=%d" % (len(bad_w), len(bad_r)))

    # ------------------------------------------------------------------
    # 附加：L5 结果断言（无 exec_result 时 skipped）
    # ------------------------------------------------------------------
    title = "附加-9. L5 无 exec_result → skipped=True，不拖垮总状态"
    sql = "SELECT 1 AS x"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l5 = _layer_by(r, "L5")
    ok = l5 and l5["skipped"] and not l5["ok"] is False and r["status"] != "不通过"
    if not ok:
        fails.append("%s FAILED: status=%s L5=%s" % (title, r["status"], l5))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L5.skipped=%s  L5.detail=%s" % (
        r["status"], l5["skipped"] if l5 else None, (l5 or {}).get("detail", "")[:50]))

    # ------------------------------------------------------------------
    # 附加：L2 各类静态规则（GROUP BY 不一致、UNNECESSARY DISTINCT）
    # ------------------------------------------------------------------
    title = "附加-10. GROUP BY 不一致 + 不必要 DISTINCT 警告（status=警告）"
    # 修正（2026-10-02）：原 SQL 用假表 t / 假列 a,b,c。因「引用未建模对象」现已属
    # 硬错误（blocking），会把本用例顶成"不通过"，偏离本用例意图（本用例只验
    # GROUP BY 软规则能被检出且不阻断）。改用 B 库真实表/列，保持原意图。
    sql = "SELECT DISTINCT store_id, order_status, COUNT(*) FROM dwd_order_di GROUP BY store_id"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    rules = {i["rule"] for i in r["semantic_issues"]}
    has_gb = "GROUP_BY_INCONSISTENT" in rules
    has_distinct = "UNNECESSARY_DISTINCT" in rules
    # 该 SQL 还带聚合，UNNECESSARY_DISTINCT 不一定会命中（因有聚合函数），
    # 所以不强求 has_distinct；只要求 GROUP BY 不一致能检出且 status != 不通过
    ok = has_gb and r["status"] != "不通过"
    if not ok:
        fails.append("%s FAILED: status=%s rules=%s issues=%s" % (
            title, r["status"], rules, r["semantic_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  rules=%s" % (r["status"], sorted(rules)))

    # ------------------------------------------------------------------
    # 附加：PT-2 GROUP_BY_INCONSISTENT 6 例（别名/序号/别名引用 去误报；真阳性保留）
    # ------------------------------------------------------------------
    title = "附加-10B. PT-2 GROUP BY 6 例：①②③④⑤ 无误报、⑥ 真阳性 1 条"
    gb_cases = [
        # ① 无别名
        ("① 无别名",
         "SELECT p.category_name, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ② 中文别名 GROUP BY 原列名
        ("② 中文别名+GROUP BY 原列",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ③ 英文同名别名 GROUP BY 原列
        ("③ 英文同名别名",
         "SELECT p.category_name AS category_name, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ④ 序号 GROUP BY 1
        ("④ GROUP BY 序号",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY 1",
         0),
        # ⑤ GROUP BY 别名引用
        ("⑤ GROUP BY 别名引用",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY 品类",
         0),
        # ⑥ 真缺列：SELECT 两个维度列，但 GROUP BY 只写一个
        ("⑥ 真缺列（品牌未 GROUP BY）",
         "SELECT p.category_name AS 品类, p.brand AS 品牌, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         1),
    ]
    ok_10b = True
    details_10b = []
    for label, sql, expected_n in gb_cases:
        r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
        actual_n = sum(1 for i in r["semantic_issues"] if i.get("rule") == "GROUP_BY_INCONSISTENT")
        hit = actual_n == expected_n
        if not hit:
            ok_10b = False
        details_10b.append((label, expected_n, actual_n, hit))
    if not ok_10b:
        fails.append("%s FAILED: details=%s" % (title, details_10b))
    print("[%s] %s" % ("OK" if ok_10b else "!!", title))
    for label, expected_n, actual_n, hit in details_10b:
        print("    %s  GROUP_BY_INCONSISTENT 期望=%d 实测=%d → %s" % (
            label, expected_n, actual_n, "OK" if hit else "!!"))

    # ------------------------------------------------------------------
    # 附加：L5 断言（给 exec_result 验证负值检测/空行检测/超上限检测）
    # ------------------------------------------------------------------
    title = "附加-11. L5 exec_result 行超限 & 金额负值 → status=不通过"
    sql = "SELECT * FROM big_table"
    exec_result = {
        "columns": ["id", "amount", "销售额"],
        "data": [[i, -100.5, -50] for i in range(100001)],
    }
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok, exec_result=exec_result)
    l5 = _layer_by(r, "L5")
    has_over = any("超过上限" in v["detail"] for v in r["rule_violations"] if v["layer"] == "L5")
    has_neg = any("金额类列" in v["detail"] and "负值" in v["detail"] for v in r["rule_violations"] if v["layer"] == "L5")
    ok = r["status"] == "不通过" and l5 and not l5["skipped"] and not l5["ok"] and has_over and has_neg
    if not ok:
        fails.append("%s FAILED: status=%s L5=%s violations(L5)=%s" % (
            title, r["status"], l5, [v for v in r["rule_violations"] if v["layer"] == "L5"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  over_limit=%s  neg_amount=%s" % (r["status"], has_over, has_neg))

    # ------------------------------------------------------------------
    # 附加：L4 注入失败桩 → 阻断
    # ------------------------------------------------------------------
    title = "附加-12. L4 dry_run 失败桩 → 阻断"
    def _fake_fail(sql):
        return {"ok": False, "message": "故意失败：引用不存在的模型"}
    sql = "SELECT * FROM nonexistent_table"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_fail)
    l4 = _layer_by(r, "L4")
    has_sem_violation = any(v["layer"] == "L4" for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l4 and not l4["ok"] and not l4["skipped"] and has_sem_violation
    if not ok:
        fails.append("%s FAILED: status=%s L4=%s violations=%s" % (
            title, r["status"], l4, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L4.ok=%s  L4.detail=%s" % (
        r["status"], l4["ok"] if l4 else None, (l4 or {}).get("detail", "")[:60]))

    # ------------------------------------------------------------------
    # 附加-13. 复合 SQL 命中阻断类规则（无 ON JOIN） → status=不通过
    # ------------------------------------------------------------------
    title = "附加-13. 复合SQL: SELECT* + 无ON JOIN(阻断类) + GROUP_CONCAT无ORDER → status=不通过"
    sql = """
        SELECT *, GROUP_CONCAT(t1.sku_id, ',') AS skus
        FROM dwd_order_detail_di t1, dim_store t2
        GROUP BY t1.store_id
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l2 = _layer_by(r, "L2")
    sem_count = len(r.get("semantic_issues") or [])
    ok = (
        r["status"] == "不通过"
        and sem_count >= 3
        and (l2 is None or (not l2.get("skipped") and not l2.get("fatal")))
    )
    if not ok:
        fails.append("%s FAILED: status=%s sem_count=%d issues=%s" % (
            title, r["status"], sem_count, r.get("semantic_issues")[:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  semantic_issues=%d  rules=%s" % (
        r["status"], sem_count, [x.get("rule") for x in (r.get("semantic_issues") or [])[:5]]))

    # ------------------------------------------------------------------
    # 附加-14. 含 CTE 的正常 SQL → R5 UNMAPPED_OBJECT_REF 0 条不误报
    # ------------------------------------------------------------------
    title = "附加-14. CTE monthly 正常 SQL → UNMAPPED_OBJECT_REF 0 条"
    sql = """
        WITH monthly AS (
            SELECT store_id, SUM(sales_amount) AS amt
            FROM dws_store_daily_agg
            GROUP BY store_id
        )
        SELECT m.store_id, s.store_name, m.amt
        FROM monthly m
        JOIN dim_store s ON m.store_id = s.store_id
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    unmapped_n = sum(1 for x in (r.get("semantic_issues") or []) if x.get("rule") == "UNMAPPED_OBJECT_REF")
    ok = unmapped_n == 0 and r["status"] in ("警告", "通过")
    if not ok:
        fails.append("%s FAILED: status=%s UNMAPPED_OBJECT_REF=%d issues=%s" % (
            title, r["status"], unmapped_n,
            [x for x in (r.get("semantic_issues") or []) if x.get("rule") == "UNMAPPED_OBJECT_REF"][:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  UNMAPPED_OBJECT_REF=%d" % (r["status"], unmapped_n))

    # ------------------------------------------------------------------
    # 附加-15. MANY_TO_ONE：事实表⋈维表 + 聚合 → ONE_TO_MANY_UNHANDLED 0 条 + status=通过
    # ------------------------------------------------------------------
    title = "附加-15. MANY_TO_ONE: dws_store_daily_agg⋈dim_store 聚合 ONE_TO_MANY_UNHANDLED=0"
    sql = """
        SELECT b.region_name, SUM(a.sales_amount) AS gmv,
               SUM(a.order_cnt) AS order_cnt,
               SUM(a.member_cnt) AS member_cnt
        FROM dws_store_daily_agg a
        JOIN dim_store b
          ON a.store_id = b.store_id
        WHERE a.stat_date >= '2025-03-01'
        GROUP BY b.region_name
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    r1_n = sum(1 for x in (r.get("semantic_issues") or []) if x.get("rule") == "ONE_TO_MANY_UNHANDLED")
    ok = r1_n == 0 and r["status"] == "通过"
    if not ok:
        fails.append("%s FAILED: status=%s ONE_TO_MANY_UNHANDLED=%d issues=%s" % (
            title, r["status"], r1_n,
            [x for x in (r.get("semantic_issues") or []) if x.get("rule") == "ONE_TO_MANY_UNHANDLED"][:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  ONE_TO_MANY_UNHANDLED=%d  all_rules=%s" % (
        r["status"], r1_n, [x.get("rule") for x in (r.get("semantic_issues") or [])[:5]]))

    # ------------------------------------------------------------------
    # 附加-16. 仅命中 warning 类规则 → status=警告（不阻断）—— 分级阻断的阴性对照
    # ------------------------------------------------------------------
    title = "附加-16. 仅 TIME_FIELD_SUSPECT(warning) → status=警告，不阻断"
    sql = ("SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, "
           "SUM(pay_amount) AS 销售金额 FROM dwd_order_di WHERE order_status = '已完成' "
           "GROUP BY date_trunc('month', order_date) ORDER BY 月份")
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    hit = {x.get("rule") for x in (r.get("semantic_issues") or [])}
    ok = r["status"] == "警告" and "TIME_FIELD_SUSPECT" in hit
    if not ok:
        fails.append("%s FAILED: status=%s rules=%s" % (title, r["status"], sorted(hit)))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  rules=%s" % (r["status"], sorted(hit)))

    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
