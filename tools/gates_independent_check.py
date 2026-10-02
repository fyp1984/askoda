# -*- coding: utf-8 -*-
"""M5-1 五层门禁 · 客户端独立复扫（不调用被验方自己的自证脚本）

存在意义：自己验自己等于没验。本脚本由验收方独立编写，专攻实现方自证用例
之外的边角与绕过路径，重点验证 L3 只读门禁是否真的漏不进写操作。

用法：
    /Users/FYP/.workbuddy/binaries/python/envs/default/bin/python \
        tools/gates_independent_check.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))

import gates as gt  # noqa: E402


def _dry_ok(sql):
    """L4 固定通过桩，隔离出 L1/L2/L3/L5 的行为。"""
    return {"ok": True, "message": "桩：dry_run 通过"}


# (用例名, SQL, 期望是否为写操作, 期望 status 是否不应为「通过」)
CASES = [
    # --- L3 只读门禁：普通写操作 ---
    ("INSERT", "INSERT INTO orders (order_id) VALUES (1)", True, True),
    ("UPDATE", "UPDATE orders SET amount = 0 WHERE order_id = 1", True, True),
    ("DELETE", "DELETE FROM orders WHERE order_id = 1", True, True),
    ("DROP", "DROP TABLE orders", True, True),
    ("CREATE", "CREATE TABLE t (a int)", True, True),
    ("ALTER", "ALTER TABLE orders ADD COLUMN x int", True, True),
    ("TRUNCATE", "TRUNCATE TABLE orders", True, True),
    ("GRANT", "GRANT ALL ON orders TO public", True, True),

    # --- 多语句拼接（正则型门禁最容易漏的一类）---
    ("多语句-分号", "SELECT 1; DROP TABLE orders", True, True),
    ("多语句-无分号空白", "SELECT 1 DROP TABLE orders", True, True),

    # --- ⚠️ 重点：CTE（WITH）里藏写操作 ---
    ("WITH+SELECT（只读，应通过）", "WITH x AS (SELECT 1 AS n) SELECT * FROM x", False, False),
    ("WITH+INSERT", "WITH x AS (SELECT 1 AS n) INSERT INTO orders (order_id) SELECT n FROM x", True, True),
    ("WITH+DELETE", "WITH x AS (SELECT 1 AS n) DELETE FROM orders WHERE order_id IN (SELECT n FROM x)", True, True),
    ("WITH+UPDATE", "WITH x AS (SELECT 1 AS n) UPDATE orders SET amount = 0 WHERE order_id IN (SELECT n FROM x)", True, True),

    # --- 注释与伪装 ---
    ("注释内藏 DROP（应通过）", "SELECT 1 /* ; DROP TABLE orders; */", False, False),
    ("行注释后 DROP", "SELECT 1 -- x\n", False, False),

    # --- 只读正常用例（不应误拦）---
    # 修正（2026-10-02）：原用例用了 B 库不存在的表（orders/customers）。因「引用未建模对象」
    # 现已属硬错误（blocking），会把只读用例顶成"不通过"，偏离本用例意图（本用例只验
    # L3 只读门禁不误拦合法查询）。改用 B 库真实表/列，保持原意图。
    ("普通 SELECT", "SELECT store_id, store_name FROM dim_store WHERE region_name = '华东大区'", False, False),
    ("聚合 SELECT", "SELECT count(*) FROM dwd_order_di", False, False),
    ("WITH 嵌套 SELECT", "WITH a AS (SELECT 1 AS n), b AS (SELECT n FROM a) SELECT n FROM b", False, False),

    # --- L2 静态风险 ---
    # 修正（2026-10-02）：隐式笛卡尔积按新决策属「硬错误 → 阻断」，故第 4 列由 False 改 True；
    # SELECT * 仍是软提示（SELECT_STAR=warning），期望保持「不阻断」。两条都换成 B 库真实表，
    # 使被测行为纯粹（不掺入"未建模对象"这一无关规则）。
    ("隐式笛卡尔积（硬错误 → 阻断）", "SELECT a.store_id FROM dim_store a, dwd_order_di b", False, True),
    ("SELECT *（软提示 → 不阻断）", "SELECT * FROM dim_store", False, False),

    # --- L1 语法错误 ---
    ("语法错误", "SELECT (( FROM", True, True),
    ("空语句", "", True, True),
]


def main():
    fails = []
    print("=" * 78)
    print("M5-1 五层门禁 · 独立复扫（%d 个用例）" % len(CASES))
    print("=" * 78)
    for name, sql, want_write, want_not_pass in CASES:
        got_write = gt.is_write(sql)
        res = gt.review(sql, dataset="B", _dry_run_fn=_dry_ok)
        status = res["status"]
        ok = True
        notes = []
        if got_write != want_write:
            ok = False
            notes.append("is_write 期望 %s 实得 %s" % (want_write, got_write))
        if want_not_pass and status == "通过":
            ok = False
            notes.append("status 期望非「通过」实得「通过」（漏放行）")
        if (not want_not_pass) and status == "不通过":
            ok = False
            notes.append("status 期望非「不通过」实得「不通过」（误拦）")
        tag = "[OK]  " if ok else "[FAIL]"
        print("%s %-28s is_write=%-5s status=%-4s %s" % (
            tag, name, got_write, status, " | ".join(notes)))
        if not ok:
            fails.append((name, sql, notes, res))

    print("-" * 78)
    print("用例 %d 个，失败 %d 个" % (len(CASES), len(fails)))
    for name, sql, notes, res in fails:
        print("\n### 失败详情：%s" % name)
        print("  SQL   : %s" % sql)
        print("  问题  : %s" % "；".join(notes))
        print("  status: %s" % res["status"])
        for v in res.get("rule_violations", []):
            print("    违规: %s / %s" % (v.get("layer"), v.get("type")))
        print("  说明  : %s" % res.get("review_notes", "")[:200])

    # 单列关键结论，便于粘贴进验收单
    print("\n" + "=" * 78)
    with_write = [c for c in CASES if c[2]]
    leaked = [c[0] for c in CASES if c[2] and gt.review(c[1], dataset="B", _dry_run_fn=_dry_ok)["status"] == "通过"]
    print("写操作类用例 %d 个，漏放行 %d 个 %s" % (
        len(with_write), len(leaked), ("← " + "、".join(leaked)) if leaked else ""))
    readonly = [c for c in CASES if not c[2] and c[0].startswith(("普通", "聚合", "WITH 嵌套", "WITH+SELECT"))]
    falseblock = [c[0] for c in readonly if gt.review(c[1], dataset="B", _dry_run_fn=_dry_ok)["status"] == "不通过"]
    print("只读类用例 %d 个，误拦 %d 个 %s" % (
        len(readonly), len(falseblock), ("← " + "、".join(falseblock)) if falseblock else ""))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
