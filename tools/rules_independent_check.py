#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-1 · 验收方独立复扫（gateway/rules.py 的 10 条静态规则）

设计原则：**不复用交付方 tools/rules_selftest.py 的任何用例**。
自证只测「实现方想得到的输入」；这里专挑实现方未必想到的对抗面：

  A. R1 精确性——最常见分析模式「事实表 ⋈ 维表 + 聚合」会不会被误报？
     （多对一连接不放大行数，按规则自身 statement 不应触发）
  B. R6 白名单质量——build_mdl_index 的 time_cols 有没有被"描述里的单字日/年"污染，
     导致 R6 漏报？（白名单越脏，R6 越哑）
  C. 非预期类型输入的健壮性（None / 空串 / 非法 dataset / 非 Select 根节点）
  D. 单条规则抛异常时的隔离性 & evaluate_all 顺序稳定性
  E. R5 排除清单是否真的排除 CTE / 子查询别名 / SELECT 别名
  F. R2/R3/R4 的边界正反例
  G. rules_version 稳定性与自洽

用法（容器内；R5/R6 需要 MDL）：
    docker exec -e PYTHONPATH=/app demand-gateway python /tmp/rules_independent_check.py
退出码 0 = 全过；非 0 = 有失败（失败项即验收结论依据）。
"""
import sys

import sqlglot
from sqlglot import exp

import rules
import gates

FAILS = []
N = [0]
DEFECTS = []   # 与 HARD 失败分开：质量缺陷（规则"能跑"但判得不准）


def ck(name, cond, detail="", quality=False):
    N[0] += 1
    tag = "PASS" if cond else ("DEFECT" if quality else "FAIL")
    line = "  [%s] %s" % (tag, name)
    if detail and not cond:
        line += "  -> %s" % str(detail)[:220]
    print(line)
    if not cond:
        (DEFECTS if quality else FAILS).append(name)


def parse1(sql):
    return list(sqlglot.parse(sql, read="postgres"))[0]


def ev(sql, dataset="B", ctx=None):
    stmt = parse1(sql)
    c = ctx if ctx is not None else {"dataset": dataset, "mdl_index": None, "requirement": None}
    return rules.evaluate_all(stmt, c)


def fired(issues, rule_id):
    return sum(1 for x in issues if x.get("rule") == rule_id)


print("=" * 78)
print("A. R1 ONE_TO_MANY_UNHANDLED —— 精确性（最常见事实⋈维表+聚合不应误报）")
print("=" * 78)
# A1-A3：正确的分析 SQL，多对一连接，不放大 → R1 不应触发
A_CASES = [
    ("A 库 order_items(N)⋈products(1) + SUM 聚合",
     "A", "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
          "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
          "GROUP BY p.category_name"),
    ("B 库 dws_store_daily_agg(N)⋈dim_store(1) + SUM 聚合",
     "B", "SELECT s.store_name AS 门店, SUM(a.sales_amount) AS 销售额 "
          "FROM dws_store_daily_agg a JOIN dim_store s ON a.store_id = s.store_id "
          "GROUP BY s.store_name"),
    ("B 库 dwd_order_di(N)⋈dim_member(1) + COUNT 聚合",
     "B", "SELECT m.member_name AS 会员, COUNT(o.order_id) AS 订单数 "
          "FROM dwd_order_di o JOIN dim_member m ON o.member_id = m.member_id "
          "GROUP BY m.member_name"),
]
for label, ds, sql in A_CASES:
    issues = ev(sql, dataset=ds)
    n = fired(issues, "ONE_TO_MANY_UNHANDLED")
    ck("A/R1 不应误报：%s" % label, n == 0,
       "触发 %d 次（多对一连接不放大行数，按规则自身 statement 不应命中）" % n,
       quality=True)

# A4 单表聚合 → 不触发（必须）
issues = ev("SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg GROUP BY store_id")
ck("A/R1 单表聚合不触发（必须）", fired(issues, "ONE_TO_MANY_UNHANDLED") == 0)

# A5 真阳性：自造 MDL 含 ONE_TO_MANY 关系，两端都出现在 SQL 里 → 应触发
synth_idx = {
    "models": {}, "tables": {"fact_a": "fact_a", "dim_b": "dim_b"},
    "all_tables": {"fact_a", "dim_b"},
    "all_columns": {"fact_a.col1", "dim_b.col2", "col1", "col2"},
    "time_cols": set(),
    "rels": [{"joinType": "ONE_TO_MANY", "models": ["fact_a", "dim_b"], "name": "r1"}],
}
issues = rules.evaluate_all(
    parse1("SELECT b.col2, SUM(a.col1) AS s FROM fact_a a JOIN dim_b b ON a.id=b.id GROUP BY b.col2"),
    {"dataset": "B", "mdl_index": synth_idx, "requirement": None})
ck("A/R1 真阳性：declared ONE_TO_MANY 两端命中 → 应触发",
   fired(issues, "ONE_TO_MANY_UNHANDLED") >= 1,
   "未触发（真阳性漏检）")

print()
print("=" * 78)
print("B. R6 白名单质量 —— build_mdl_index 的 time_cols 是否被'描述单字'污染")
print("=" * 78)
idx_b = rules.build_mdl_index("B")
tc_b = idx_b["time_cols"]
plain = {c for c in tc_b if "." not in c}
# 严重非时间列（描述里含"当日X"被单字'日'命中）——M6-1 返工项2 后应全清
NON_TIME_HARD = ["member_cnt", "order_cnt", "sales_amount", "member_id"]
bad_hard = sorted(c for c in NON_TIME_HARD if c in plain)
ck("B/time_cols 不应含严重非时间列 %s（返工项2）" % NON_TIME_HARD, not bad_hard,
   "仍残留=%s" % bad_hard)
# 低危残留：day_type（label='日期类型；枚举：…' 被 startswith('日期') 命中）
LEFT = ["day_type"]
bad_left = sorted(c for c in LEFT if c in plain)
ck("B/time_cols 低危残留 day_type（label『日期类型…』命中 startswith『日期』）", not bad_left,
   "残留=%s —— 低危：day_type 非 time_like，不直接致 R6 漏报，属白名单纯度隐患" % bad_left,
   quality=True)
SHOULD_HAVE = ["stat_date", "order_date", "open_date", "register_date", "stat_month"]
missing = sorted(c for c in SHOULD_HAVE if c not in plain)
ck("B/time_cols 应含真实时间列 %s" % SHOULD_HAVE, not missing,
   "缺失=%s" % missing)

print()
print("=" * 78)
print("C. 非预期类型输入 —— 不许抛异常")
print("=" * 78)
# C1 ast=None
try:
    out = rules.evaluate_all(None, {"dataset": "B", "mdl_index": None, "requirement": None})
    ck("C1 evaluate_all(None) 不抛异常", isinstance(out, list), repr(out)[:120])
except Exception as e:
    ck("C1 evaluate_all(None) 不抛异常", False, "%s: %s" % (type(e).__name__, e))
# C2 空串 / 纯注释 → 走 gates.review 的 L1，应拦成不通过
for label, sql in [("空串", ""), ("纯注释", "-- just a comment")]:
    try:
        r = gates.review(sql, dataset="B", _dry_run_fn=lambda s: {"ok": True})
        ck("C2 gates.review(%s) → status=不通过 且不崩" % label,
           r.get("status") == "不通过", "status=%s" % r.get("status"))
    except Exception as e:
        ck("C2 gates.review(%s) 不崩" % label, False, "%s: %s" % (type(e).__name__, e))
# C3 ctx=None
try:
    out = rules.evaluate_all(parse1("SELECT 1"), None)
    ck("C3 evaluate_all(ast, None) 不抛异常", isinstance(out, list))
except Exception as e:
    ck("C3 evaluate_all(ast, None) 不抛异常", False, "%s: %s" % (type(e).__name__, e))
# C4 ctx.dataset 非法（未注册）→ 不崩
try:
    out = rules.evaluate_all(parse1("SELECT a FROM t"), {"dataset": "Z", "mdl_index": None, "requirement": None})
    ck("C4 evaluate_all(非法 dataset='Z') 不抛异常", isinstance(out, list))
except Exception as e:
    ck("C4 evaluate_all(非法 dataset) 不抛异常", False, "%s: %s" % (type(e).__name__, e))
# C5 非 Select 根（INSERT / DROP）直接喂 evaluate_all → 不崩
for label, sql in [("INSERT", "INSERT INTO t VALUES (1)"), ("DROP", "DROP TABLE t")]:
    try:
        out = rules.evaluate_all(parse1(sql), {"dataset": "B", "mdl_index": None, "requirement": None})
        ck("C5 evaluate_all(%s 根节点) 不抛异常" % label, isinstance(out, list))
    except Exception as e:
        ck("C5 evaluate_all(%s 根节点) 不抛异常" % label, False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 78)
print("D. 异常隔离 & 顺序稳定性")
print("=" * 78)
# D1 注入一个会抛异常的规则 → evaluate_all 不整体崩，其他规则结果仍在
_saved = list(rules.REGISTRY)
try:
    def _boom(ast_stmt, ctx):
        raise RuntimeError("故意抛异常")
    rules.REGISTRY.append({
        "id": "ZZ_BOOM", "name": "ZZ_BOOM", "severity": "warning",
        "statement": "测试用", "detect": "boom", "evaluate": _boom,
    })
    out = rules.evaluate_all(parse1("SELECT * FROM dws_store_daily_agg"),
                             {"dataset": "B", "mdl_index": None, "requirement": None})
    has_star = any(x.get("rule") == "SELECT_STAR" for x in out)
    has_diag = any(x.get("rule") == "RULE_INTERNAL_ERROR" for x in out)
    ck("D1 单规则抛异常 → 整体不崩，其他规则结果保留", has_star,
       "SELECT_STAR 结果丢失")
    ck("D1b 异常规则留下 RULE_INTERNAL_ERROR 诊断", has_diag)
finally:
    rules.REGISTRY[:] = _saved
# D2 顺序稳定：同输入两次，结果序列完全一致
s = "SELECT a, b, SUM(c) FROM t GROUP BY a"
o1 = ev(s); o2 = ev(s)
seq1 = [(x["rule"], x["snippet"]) for x in o1]
seq2 = [(x["rule"], x["snippet"]) for x in o2]
ck("D2 evaluate_all 同输入两次结果顺序一致", seq1 == seq2)
# D3 REGISTRY 契约
ids = [r["id"] for r in rules.REGISTRY]
ck("D3 REGISTRY 有 10 条规则", len(ids) == 10, "实际=%d" % len(ids))
ck("D3b 规则 id 唯一", len(ids) == len(set(ids)), "重复=%s" % [i for i in ids if ids.count(i) > 1])
sev = {r["severity"] for r in rules.REGISTRY}
ck("D3c 一期 severity 全为 warning", sev == {"warning"}, "实际=%s" % sev)
need_keys = {"id", "name", "severity", "statement", "detect", "evaluate"}
bad_keys = [r["id"] for r in rules.REGISTRY if not need_keys <= set(r)]
ck("D3d 每条规则含 6 个必需键", not bad_keys, "缺键=%s" % bad_keys)
old4 = {"JOIN_WITHOUT_CONDITION", "SELECT_STAR", "GROUP_BY_INCONSISTENT", "UNNECESSARY_DISTINCT"}
ck("D3e 4 条老规则名一字未改", old4 <= set(ids), "缺=%s" % (old4 - set(ids)))

print()
print("=" * 78)
print("E. R5 UNMAPPED_OBJECT_REF —— 排除清单有效性")
print("=" * 78)
E1 = ("WITH monthly AS (SELECT store_id, SUM(sales_amount) AS amt FROM dws_store_daily_agg GROUP BY store_id) "
      "SELECT m.store_id, s.store_name FROM monthly m JOIN dim_store s ON m.store_id = s.store_id")
ck("E1 CTE 名不误报", fired(ev(E1), "UNMAPPED_OBJECT_REF") == 0,
   [x["snippet"] for x in ev(E1) if x["rule"] == "UNMAPPED_OBJECT_REF"][:2])
E2 = "SELECT x.store_id FROM (SELECT store_id FROM dws_store_daily_agg) x"
ck("E2 子查询别名不误报", fired(ev(E2), "UNMAPPED_OBJECT_REF") == 0,
   [x["snippet"] for x in ev(E2) if x["rule"] == "UNMAPPED_OBJECT_REF"][:2])
E3 = "SELECT store_id AS 门店编号 FROM dws_store_daily_agg"
ck("E3 SELECT 别名不误报", fired(ev(E3), "UNMAPPED_OBJECT_REF") == 0)
E4 = "SELECT foo_id FROM nonexistent_table"
ck("E4 真未建模表 → 报（真阳性不漏）", fired(ev(E4), "UNMAPPED_OBJECT_REF") >= 1)

print()
print("=" * 78)
print("F. R2 / R3 / R4 边界正反例")
print("=" * 78)
f1 = ev("SELECT store_id FROM dws_store_daily_agg WHERE dw_insert_time >= '2025-01-01' GROUP BY store_id")
ck("F1 R2：WHERE dw_insert_time → 报", fired(f1, "LOAD_DATE_SUBSTITUTION") >= 1)
f1b = ev("SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date >= '2025-01-01' GROUP BY store_id ORDER BY dw_insert_time")
ck("F1b R2：ORDER BY dw_insert_time → 报", fired(f1b, "LOAD_DATE_SUBSTITUTION") >= 1,
   "ORDER BY 子句漏扫")
f2 = ev("SELECT store_id, GROUP_CONCAT(sku_id, ',') AS skus FROM dwd_order_detail_di GROUP BY store_id")
ck("F2 R3：GROUP_CONCAT 无 ORDER BY → 报", fired(f2, "MULTI_VALUE_NO_ORDER") >= 1)
f2b = ev("SELECT store_id, GROUP_CONCAT(sku_id, ',' ORDER BY sku_id) AS skus FROM dwd_order_detail_di GROUP BY store_id")
ck("F2b R3：GROUP_CONCAT 带 ORDER BY → 不报", fired(f2b, "MULTI_VALUE_NO_ORDER") == 0)
f3 = ev("SELECT store_id, SUM(sales_amount)/NULLIF(SUM(area_sqm),0) AS 坪效 FROM dws_store_daily_agg GROUP BY store_id")
ck("F3 R4：比率 + 有除法 → 不报", fired(f3, "DANGLING_DENOMINATOR") == 0)
f3b = ev("SELECT store_id, AVG(repurchase_flag) AS 复购率 FROM ads_member_repurchase_di GROUP BY store_id")
ck("F3b R4：比率 + 单表无分母 → 报", fired(f3b, "DANGLING_DENOMINATOR") >= 1)

print()
print("=" * 78)
print("G. rules_version 稳定性与自洽")
print("=" * 78)
v1 = rules.__dict__  # noqa: F841  仅占位避免误删
try:
    import sqlpack
    rv1 = sqlpack._rules_version()
    rv2 = sqlpack._rules_version()
    ck("G1 rules_version 两次一致", rv1 == rv2, "%s vs %s" % (rv1, rv2))
    ck("G1b rules_version 为 8 位十六进制", len(rv1) == 8 and all(ch in "0123456789abcdef" for ch in rv1), rv1)
except Exception as e:
    ck("G1 rules_version 可复算", False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 78)
print("合计：用例 %d 个；硬失败 %d 个；质量缺陷 %d 个" % (N[0], len(FAILS), len(DEFECTS)))
if FAILS:
    print("硬失败：")
    for f in FAILS:
        print("  -", f)
if DEFECTS:
    print("质量缺陷（规则能跑，但判得不准）：")
    for d in DEFECTS:
        print("  -", d)
print("=" * 78)
sys.exit(1 if (FAILS or DEFECTS) else 0)
