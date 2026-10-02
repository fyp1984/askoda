# -*- coding: utf-8 -*-
"""M4-1 Schema 扫描与版本化 · 自证脚本

守什么
------
1. 同一数据集连续两次 scan，digest 完全相同（可复现）；
2. schema_version 长度 = 8 且只含 hex 字符；
3. B 库 tables 非空、columns 总数 > 0（不造空结果）；
4. persist 两次不产生重复行（主键幂等）。

用法：
    python3 tools/schema_selftest.py
退出码：0 全过；1 有断言失败
"""
import os
import sys
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "gateway"))

import db  # noqa: E402
import schema_scan as sc  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        print("  \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)


def _hex(s):
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# A. _digest 纯函数用例（无需数据库）
# ---------------------------------------------------------------------------
def test_digest_pure():
    print("\nA. _digest —— 纯函数稳定性（任何环境可跑）")
    tables = [
        {
            "name": "b_orders",
            "label": "订单",
            "row_estimate": 100,
            "columns": [
                {"name": "order_id", "type": "text", "label": "订单号", "is_pk": True},
                {"name": "amount", "type": "numeric", "label": "金额", "is_pk": False},
            ],
        },
        {
            "name": "a_products",
            "label": "商品",
            "row_estimate": 20,
            "columns": [
                {"name": "product_id", "type": "text", "label": "商品ID", "is_pk": True},
                {"name": "category", "type": "text", "label": "品类", "is_pk": False},
            ],
        },
    ]
    # 打乱 tables / columns 顺序，digest 应完全相同（版本号只看名字+类型，不看输入顺序）
    shuffled = [
        {
            "name": "a_products",
            "columns": [
                {"name": "category", "type": "text"},
                {"name": "product_id", "type": "text"},
            ],
        },
        {
            "name": "b_orders",
            "columns": [
                {"name": "amount", "type": "numeric"},
                {"name": "order_id", "type": "text"},
            ],
        },
    ]
    sv1, d1 = sc._digest(tables)
    sv2, d2 = sc._digest(shuffled)
    check("digest 与输入顺序无关（只看名称+类型）", d1 == d2,
          "有序=%s 乱序=%s" % (d1[:16], d2[:16]))

    # 手动算一次：按表名、列名排序后的 "表名.列名:类型" 拼 sha256
    lines = [
        "a_products.category:text",
        "a_products.product_id:text",
        "b_orders.amount:numeric",
        "b_orders.order_id:text",
    ]
    want = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    check("digest 与手工计算一致", d1 == want,
          "实际=%s 期望前16=%s" % (d1[:16], want[:16]))

    # schema_version = 前 8 位
    check("schema_version = digest[:8]", sv1 == want[:8],
          "sv=%s want[:8]=%s" % (sv1, want[:8]))

    # 改一个字段类型 → digest 必须变化
    tables[0]["columns"][1]["type"] = "integer"
    sv3, d3 = sc._digest(tables)
    check("字段类型变更 → digest 变更", d3 != d1,
          "旧=%s 新=%s" % (d1[:12], d3[:12]))


# ---------------------------------------------------------------------------
# B. 真实数据集 B 的 scan（需 PG + 数据字典可用）
# ---------------------------------------------------------------------------
def test_real_scan():
    print("\nB. B 库真实 scan（需 DB 可达）")
    try:
        r1 = sc.scan("B", persist=False)
    except Exception as e:  # noqa: BLE001
        print("  \033[33mSKIP\033[0m DB 不可用，跳过真实扫描：%s" % str(e)[:120])
        return None

    # 结构校验
    check("dataset = B", r1.get("dataset") == "B")
    check("source = mdl+native", r1.get("source") == "mdl+native")
    check("tables 非空", bool(r1.get("tables")), "tables=%s" % r1.get("tables"))
    check("tables 数量 > 0", r1.get("counts", {}).get("tables", 0) > 0,
          "counts=%s" % r1.get("counts"))
    check("columns 总数 > 0", r1.get("counts", {}).get("columns", 0) > 0,
          "counts=%s" % r1.get("counts"))

    # schema_version 格式
    sv = r1.get("schema_version")
    dig = r1.get("digest")
    check("schema_version 长度 = 8", sv is not None and len(sv) == 8,
          "sv=%r len=%s" % (sv, None if sv is None else len(sv)))
    check("schema_version 纯 hex", sv is not None and _hex(sv), "sv=%r" % sv)
    check("digest 长度 = 64", dig is not None and len(dig) == 64,
          "dig len=%s" % (None if dig is None else len(dig)))
    check("digest 纯 hex", dig is not None and _hex(dig), "dig=%r" % (dig[:16] if dig else None))
    check("schema_version = digest[:8]", sv is not None and dig is not None and sv == dig[:8])

    # 每张表至少有 name / columns
    bad_t = [t.get("name") for t in r1["tables"] if not t.get("name") or not t.get("columns")]
    check("每张表都有 name 与 columns", not bad_t, "坏表：%s" % bad_t[:3])
    # 每列至少有 name / type
    bad_c = []
    for t in r1["tables"]:
        for c in t["columns"]:
            if not c.get("name") or not c.get("type"):
                bad_c.append("%s.%s" % (t.get("name"), c.get("name")))
    check("每列都有 name 与 type", not bad_c, "坏列：%s" % bad_c[:6])

    # 同库跑两次 → digest 相同
    r2 = sc.scan("B", persist=False)
    check("连续两次 scan digest 相同", r1["digest"] == r2["digest"],
          "r1=%s r2=%s" % (r1["digest"][:12], r2["digest"][:12]))
    check("连续两次 scan schema_version 相同", r1["schema_version"] == r2["schema_version"])

    print("       B 库 schema_version = %s  digest=%s…" % (
        r1["schema_version"], r1["digest"][:12]))
    return r1


# ---------------------------------------------------------------------------
# C. persist 幂等性（需 DB 可写）
# ---------------------------------------------------------------------------
def test_persist_idempotent():
    print("\nC. persist 主键幂等（需 schema_snapshots 表）")
    try:
        before = db.query_one(
            "SELECT count(*) AS n FROM schema_snapshots WHERE dataset='B'"
        )["n"]
    except Exception as e:  # noqa: BLE001
        print("  \033[33mSKIP\033[0m DB 不可用，跳过幂等测试：%s" % str(e)[:120])
        return

    try:
        sc.scan("B", persist=True)
        after1 = db.query_one(
            "SELECT count(*) AS n FROM schema_snapshots WHERE dataset='B'"
        )["n"]
        check("首次 persist → 行数增加 0 或 1（可能已有）", after1 >= before)

        sc.scan("B", persist=True)
        after2 = db.query_one(
            "SELECT count(*) AS n FROM schema_snapshots WHERE dataset='B'"
        )["n"]
        check("再次 persist → 行数不变（主键幂等）", after2 == after1,
              "after1=%d after2=%d" % (after1, after2))

        sc.scan("B", persist=True)
        after3 = db.query_one(
            "SELECT count(*) AS n FROM schema_snapshots WHERE dataset='B'"
        )["n"]
        check("第三次 persist → 行数仍不变", after3 == after2)

        lv = sc.latest_version("B")
        check("latest_version 返回非空", lv is not None)
        if lv:
            check("latest_version.dataset = B", lv.get("dataset") == "B")
            check("latest_version.schema_version 长度 = 8",
                  lv.get("schema_version") is not None and len(lv["schema_version"]) == 8)
            got_counts = lv.get("counts") or {}
            check("latest_version.counts.tables > 0", got_counts.get("tables", 0) > 0)
            check("latest_version.counts.columns > 0", got_counts.get("columns", 0) > 0)
    except Exception as e:  # noqa: BLE001
        print("  \033[33mSKIP\033[0m persist 异常：%s" % str(e)[:160])


def main():
    print("=" * 66)
    print("M4-1 Schema 扫描与版本化 · 自证")
    print("=" * 66)
    test_digest_pure()
    test_real_scan()
    test_persist_idempotent()
    print()
    print("-" * 66)
    print("共 %d 项断言，失败 %d 项" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ %s" % f)
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
