#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收方独立复扫 · M5 收口（PT-1 候选判别力 + PT-2 门禁分组误报）。容器内运行。

设计原则：**不复用交付方自证里的任何用例**。自证测的是实现方想得到的 6 条 SQL 与 2 组分布；
本脚本专挑它没覆盖的对抗面：
  PT-1：平局不硬选、倍率临界、阈值地板、判别力单调性、同一 needle 跨表权重一致、
        top_needles 结构、排序确定性、低分表不被删（只降权不删表）。
  PT-2：真实 planner 产出 SQL（原误报）是否消除、序号指向第 2 项、同表达式两侧、
        大小写差异、别名+序号混合、GROUP BY 序号越界不崩、过度归一保护（真不一致仍检出）。

用法（容器内）：
    docker exec -e PYTHONPATH=/app askoda python /tmp/pt_independent_check.py
退出码 0 = 全过。
"""
import sys

import registry
import sqlpack
import sqlgen
import gates

FAILS = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -> %s" % str(detail)[:200]) if (detail and not cond) else ""))
    if not cond:
        FAILS.append(name)


def _dry(sql):
    """L4 桩：语义层预演恒通过（把被测点收敛在 L2 静态规则上）。"""
    return {"ok": True, "message": "stub-dry-run"}


def gb_flag(sql, ds="A"):
    """返回 (是否检出 GROUP_BY_INCONSISTENT, review 结果)。"""
    r = gates.review(sql, dataset=ds, _dry_run_fn=_dry)
    hit = any(i.get("rule") == "GROUP_BY_INCONSISTENT" for i in (r.get("semantic_issues") or []))
    return hit, r


def _claim(v, conf=0.8):
    return {"value": v, "confidence": conf, "evidence": [
        {"level": "P1", "level_name": "需求原文", "rank": 1, "source": "indep-pt", "locator": "x", "content": v}]}


def _fake_pack(table_specs):
    """构造最小可用假 pack，注入 sqlgen.plan 以精确控制 top1/top2 分数。"""
    tc = [{"table": t, "model": t, "table_reference": None,
           "match_hits": [{"kind": "列中文名", "needle": n, "target": n, "weight": float(s)}],
           "score": float(s), "top_needles": [{"needle": n, "weight": float(s)}]}
          for t, s, n in table_specs]
    return {
        "pack_version": "testpack", "requirement_version": 1,
        "schema_version": "testschema", "rules_version": "testrules",
        "table_candidates": tc, "join_candidates": [], "key_candidates": [],
        "time_constraints": {"candidate_columns": []},
        "filter_constraints": {"candidate_columns": []},
        "aggregation_constraints": [],
        "subject_definition": {"value": "门店销售订单", "confidence": 0.9, "evidence": []},
        "granularity_definition": {"value": "按日", "confidence": 0.9, "evidence": []},
        "required_fields": [],
        "_debug": {"mdl_coverage_pct": 100.0, "miss_reason": ""},
    }


def plan_with_pack(demand_id, specs, ds="B"):
    """临时替换 sqlpack.build，跑 sqlgen.plan，返回 plan 输出后还原。"""
    orig = sqlgen.sqlpack_mod.build
    sqlgen.sqlpack_mod.build = lambda did, dataset=None, _p=_fake_pack(specs): _p
    try:
        return sqlgen.plan(demand_id, dataset=ds)
    finally:
        sqlgen.sqlpack_mod.build = orig


print("=" * 78)
print("验收方独立复扫 · M5 收口 PT-1/PT-2（不复用交付方自证用例）")
print("=" * 78)

# ===========================================================================
# PT-1 · 候选判别力（sqlpack 打分）
# ===========================================================================
print("\n[PT-1] 候选打分：判别力单调性 / 稳定性 / 只降权不删表")

mdlB = {m["name"] for m in registry.get("B").mdl["models"]}
# 需求里混入一个"跨表高频词"与若干"判别性词"，观察权重差异
req = {
    "subject": _claim("订单"),
    "output_fields": [_claim("销售额"), _claim("会员等级"), _claim("门店")],
}
c = sqlpack.candidates("B", req)
tabs = c["subject_tables"]

# 1) 只降权不删表：候选集合 == 有 match_hits 的表集合；且 score 全 > 0
tables_all = {t["table"] for t in tabs}
ck("P1 候选表名全部 ∈ MDL 模型名（闭集性未被破坏）", tables_all <= mdlB, sorted(tables_all - mdlB))
ck("P2 每张候选表 score > 0（未出现 0 分表混入）", all(t["score"] > 0 for t in tabs),
   [(t["table"], t["score"]) for t in tabs if t["score"] <= 0])
ck("P3 每张候选表都至少 1 条 match_hits（低分表未被删，只降权）",
   all(len(t["match_hits"]) >= 1 for t in tabs),
   [t["table"] for t in tabs if len(t["match_hits"]) < 1])

# 2) 排序确定性：同输入两次 → 表序与分值完全一致
c2 = sqlpack.candidates("B", req)
ck("P4 同输入两次 candidates → 表序与分值完全一致（确定性）",
   [(t["table"], t["score"]) for t in tabs] == [(t["table"], t["score"]) for t in c2["subject_tables"]])

# 3) 排序：score 非递增
scores = [t["score"] for t in tabs]
ck("P5 候选取值按 score 非递增排列", all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)), scores)

# 4) 逐条独立复算权重（不信实现：自己重算 df 与长度权重，按公式比对）
import metadata as metadata_mod
from collections import defaultdict


def _norm_txt(x):
    return (str(x).lower().replace(" ", "").replace("_", "")) if x is not None else ""


def _indep_df(needle, mdl):
    """独立实现 df：needle 命中过多少不同模型（闭集=模型名/物理表名/列名/列中文名）。
    与 gateway 无共享代码。"""
    cnt = 0
    for m in mdl.get("models", []):
        if needle in _norm_txt(m.get("name")):
            cnt += 1
            continue
        tr = (m.get("tableReference") or {}).get("table")
        if tr and needle in _norm_txt(tr):
            cnt += 1
            continue
        for col in m.get("columns", []) or []:
            lbl, _ = metadata_mod.split_label_caliber(col.get("description"))
            if needle in _norm_txt(col.get("name")) or (lbl and needle in _norm_txt(lbl)):
                cnt += 1
                break
    return max(1, cnt)


mdl_b = registry.get("B").mdl
w_bad = []
for t in tabs:
    for h in t["match_hits"]:
        n = h["needle"]
        d = _indep_df(n, mdl_b)
        exact = (h["kind"] in ("模型名", "物理表名")
                 and len(n) == len(_norm_txt(h["target"])) and n == _norm_txt(h["target"]))
        length_w = 3.0 if exact else float(min(4, max(2, len(n))))
        expect = round(length_w * (1.0 / (1.0 + 0.5 * (d - 1))), 3)
        if abs(expect - round(h["weight"], 3)) > 1e-9:
            w_bad.append((t["table"], n, h["kind"],
                          "实际=%.3f 期望=%.3f df=%d exact=%s" % (h["weight"], expect, d, exact)))
ck("P6 每条命中 weight == 独立复算（长度权重 × 判别力 1/(1+0.5(df-1))，df 独立重算）",
   not w_bad, w_bad[:4])

# 5) 判别力确有区分：同长度下，df 越大权重越小（对独立 df 做单调性检查）
grouped = defaultdict(dict)
for t in tabs:
    for h in t["match_hits"]:
        if h["kind"] in ("列名", "列中文名"):
            grouped[len(h["needle"])][h["needle"]] = _indep_df(h["needle"], mdl_b)
mono_bad = []
for L, nd in grouped.items():
    lw = float(min(4, max(2, L)))
    for a, da in nd.items():
        for b, db in nd.items():
            if da > db and (lw / (1 + 0.5 * (da - 1))) >= (lw / (1 + 0.5 * (db - 1))):
                mono_bad.append((L, a, da, b, db))
ck("P7 判别力单调：同长度下 df 越大权重越小", not mono_bad, mono_bad[:3])
ck("P7b 判别力确有区分（样本中同时存在 df=1 与 df>1 的 needle）",
   any(v == 1 for nd in grouped.values() for v in nd.values())
   and any(v > 1 for nd in grouped.values() for v in nd.values()))

# 6) top_needles 结构
struct_bad = []
for t in tabs:
    tn = t.get("top_needles")
    if not isinstance(tn, list) or len(tn) > 3:
        struct_bad.append((t["table"], "数量异常"))
        continue
    ws = [x.get("weight") for x in tn]
    if ws != sorted(ws, reverse=True):
        struct_bad.append((t["table"], "非降序", ws))
    if len({x.get("needle") for x in tn}) != len(tn):
        struct_bad.append((t["table"], "needle 重复"))
ck("P8 top_needles ≤3 项、权重降序、needle 唯一", not struct_bad, struct_bad)

# 7) 权重上界：任何单条命中 weight ≤ 4.0（length_w 上确界 × disc ≤1）
w_bad = [(t["table"], h["needle"], h["weight"]) for t in tabs for h in t["match_hits"] if h["weight"] > 4.0 + 1e-9]
ck("P9 单条命中 weight ≤ 4.0（长度权重上确界）", not w_bad, w_bad)

# ===========================================================================
# PT-1 · plan 唯一解判定：平局 / 临界 / 地板
# ===========================================================================
print("\n[PT-1] plan 唯一解判定：平局不硬选 / 倍率临界 / 阈值地板")

p_tie = plan_with_pack("DR-X", [("tbl_a", 5.0, "x"), ("tbl_b", 5.0, "y"), ("tbl_c", 3.0, "z")])
ck("P10 两表同分（5.0/5.0）→ 不硬选（chosen_table 为空）", (p_tie.get("chosen_table") or "") == "", p_tie.get("chosen_table"))
ck("P11 平局 → draft_gate.status == 需人工审核", p_tie["draft_gate"]["status"] == "需人工审核", p_tie["draft_gate"]["status"])

p_just_below = plan_with_pack("DR-X", [("tbl_a", 4.0, "x"), ("tbl_b", 2.67, "y")])   # ratio≈1.498 < 1.5
ck("P12 倍率 1.498（<1.5）→ 不选", (p_just_below.get("chosen_table") or "") == "", p_just_below.get("chosen_table"))

p_just_above = plan_with_pack("DR-X", [("tbl_a", 4.0, "x"), ("tbl_b", 2.60, "y")])   # ratio≈1.538 > 1.5
ck("P13 倍率 1.538（>1.5）且 s1=4.0 → 选中 top1", p_just_above.get("chosen_table") == "tbl_a", p_just_above.get("chosen_table"))

p_floor = plan_with_pack("DR-X", [("tbl_a", 3.9, "x"), ("tbl_b", 0.1, "y")])          # 倍率极高但 s1<4.0
ck("P14 s1=3.9（<4.0 地板）→ 不选（不靠倍率硬凑）", (p_floor.get("chosen_table") or "") == "", p_floor.get("chosen_table"))

p_single = plan_with_pack("DR-X", [("tbl_only", 0.6, "x")])
ck("P15 仅 1 张候选（哪怕分低）→ 仍自动选中（沿用既有行为）", p_single.get("chosen_table") == "tbl_only", p_single.get("chosen_table"))

p_orig_order = plan_with_pack("DR-X", [("tbl_low", 1.0, "x"), ("tbl_high", 9.0, "y"), ("tbl_mid", 4.0, "z")])
ck("P16 候选原始顺序被打乱时仍取最高分（plan 内部重排生效）", p_orig_order.get("chosen_table") == "tbl_high", p_orig_order.get("chosen_table"))

# ===========================================================================
# PT-2 · 门禁分组误报：真实 SQL 与对抗用例
# ===========================================================================
print("\n[PT-2] GROUP_BY_INCONSISTENT：误报消除 + 真阳性保留 + 过度归一保护")

# 真实 planner 产出（A 库样例 4，原实现误报为"警告"）
REAL_A4 = ("SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
           "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
           "GROUP BY p.category_name ORDER BY 销售额 DESC")
hit, r = gb_flag(REAL_A4, "A")
ck("Q1 真实 planner SQL（别名异名分组）不再误报", not hit, [i.get("snippet") for i in (r.get("semantic_issues") or [])])
print("       该 SQL review.status = %s（原实现为「警告」）" % r.get("status"))

# 真实 planner 产出（A 库样例 1，原实现误报）
REAL_A1 = ("SELECT p.category_name AS 大类, SUM(p.stock) AS 总库存, COUNT(*) AS SKU数量 "
           "FROM products p GROUP BY p.category_name ORDER BY 总库存 DESC")
hit, r = gb_flag(REAL_A1, "A")
ck("Q2 真实 planner SQL（三列中两聚合）不再误报", not hit)
print("       该 SQL review.status = %s（原实现为「警告」）" % r.get("status"))

CASES = [
    ("Q3 序号分组指向第 2 个非聚合投影",
     "SELECT p.category_name AS 品类, p.brand AS 品牌, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY 1, 2", False),
    ("Q4 两侧同为函数表达式（DATE_TRUNC）",
     "SELECT DATE_TRUNC('month', o.order_date) AS m, SUM(o.amount) AS amt FROM orders o "
     "GROUP BY DATE_TRUNC('month', o.order_date)", False),
    ("Q5 大小写差异的别名/列名",
     "SELECT p.category_name AS Category_Name, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY P.CATEGORY_NAME", False),
    ("Q6 别名 + 序号混合分组",
     "SELECT p.category_name AS 品类, p.brand AS 品牌, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY 品类, 2", False),
    ("Q7 【真阳性】SELECT 两维度只按一列分组",
     "SELECT p.category_name AS 品类, p.brand AS 品牌, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY p.category_name", True),
    ("Q8 【真阳性】无别名、两列只按一列分组",
     "SELECT p.category_name, p.brand, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY p.category_name", True),
    ("Q9 【过度归一保护】GROUP BY 的是另一列（p.b 而非 p.a）",
     "SELECT p.a AS x, q.b AS y FROM t1 p JOIN t2 q ON p.id=q.id GROUP BY p.b, q.b", True),
    ("Q10 【真阳性】SELECT 非聚合列完全没进 GROUP BY",
     "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY 1 + 1", True),
]
for label, sql, expect in CASES:
    hit, r = gb_flag(sql, "A")
    ck("%s（期望%s）" % (label, "检出" if expect else "不检出"), hit == expect,
       "实际 %s" % ("检出" if hit else "不检出"))

# 序号越界 / 退化输入：只要不抛异常
for label, sql in [
    ("Q11 GROUP BY 序号越界（GROUP BY 9，仅 2 投影）",
     "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt FROM order_items oi "
     "JOIN products p ON oi.product_id=p.product_id GROUP BY 9"),
    ("Q12 空 SQL", ""),
    ("Q13 纯注释", "-- 只有注释"),
]:
    try:
        rr = gates.review(sql, dataset="A", _dry_run_fn=_dry)
        ck("%s → 规范返回不抛异常" % label, isinstance(rr, dict) and "status" in rr)
    except Exception as e:  # noqa: BLE001
        ck("%s → 规范返回不抛异常" % label, False, "%s: %s" % (type(e).__name__, e))

# ===========================================================================
print("\n" + "=" * 78)
print("用例 %d 个，失败 %d 个" % (N[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("   ✗ %s" % f)
    sys.exit(1)
print("✅ PT 独立复扫全过")
sys.exit(0)
