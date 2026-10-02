#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-1 返工专项 · 验收方独立复扫（不复用交付方任何用例）

验收对象 = M6-1 首轮验收提出的 3 项 P0 返工 + 1 项 P1 建议，逐项独立验证：
  A. 返工项1 —— R1 ONE_TO_MANY_UNHANDLED 精确化（第二轮：两两组合 → 图连通性 BFS）
     压测梯次(2)新判定：SQL 引用表集在 MDL 已声明关系子图上是否连通。
     A1 两表负例 / A2 三表链式 4 例（含经中心维表桥接）/ A3 真阳性 /
     A4 声明 ONE_TO_MANY 真放大 / A5 单表 /
     A6 合成图边界（全连通 / 3连通+1孤立 / 2+2两簇 / detail 分组 / self-join /
       表不在 MDL / 全连通但含声明 ONE_TO_MANY）。
  B. 返工项2 —— R6 白名单（build_mdl_index.time_cols）清洗
  C. 返工项3 —— gates_selftest 宿主离线可跑（setdefault 兜底）
  D. 返工项4 —— rules_version 纳入 evaluate 源码指纹（实现变则版本必变）
  E. 回归 —— 顺序稳定 / 异常隔离 / REGISTRY 契约（首轮已过，此处复查不许退化）

用法（容器内；R5/R6 需 MDL）：
    docker exec -e PYTHONPATH=/app demand-gateway python /tmp/rules_rework_check.py
退出码 0 = 全过；非 0 = 有失败（失败项即验收结论依据）。
"""
import hashlib
import inspect
import os
import sys

import sqlglot

import rules
import gates

FAILS = []
N = [0]
DEFECTS = []


def ck(name, cond, detail="", quality=False):
    N[0] += 1
    tag = "PASS" if cond else ("DEFECT" if quality else "FAIL")
    line = "  [%s] %s" % (tag, name)
    if detail and not cond:
        line += "  -> %s" % str(detail)[:240]
    print(line)
    if not cond:
        (DEFECTS if quality else FAILS).append(name)


def parse1(sql):
    return list(sqlglot.parse(sql, read="postgres"))[0]


def ev(sql, dataset="B"):
    return rules.evaluate_all(parse1(sql),
                              {"dataset": dataset, "mdl_index": None, "requirement": None})


def fired(issues, rule_id):
    return sum(1 for x in issues if x.get("rule") == rule_id)


print("=" * 78)
print("A. 返工项1 —— R1 精确化（两表负例 / 真阳性 / 三表链式盲点）")
print("=" * 78)
# --- 两表：事实⋈维表（已声明 MANY_TO_ONE，多对一不放大）→ 必须不报 ---
TWO_TABLE_NEG = [
    ("A 库 order_items(N)⋈products(1) + SUM", "A",
     "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
     "FROM order_items oi JOIN products p ON oi.product_id = p.product_id GROUP BY p.category_name"),
    ("B 库 dws_store_daily_agg(N)⋈dim_store(1) + SUM", "B",
     "SELECT s.store_name AS 门店, SUM(a.sales_amount) AS 销售额 "
     "FROM dws_store_daily_agg a JOIN dim_store s ON a.store_id = s.store_id GROUP BY s.store_name"),
    ("B 库 dwd_order_di(N)⋈dim_member(1) + COUNT", "B",
     "SELECT m.member_name AS 会员, COUNT(o.order_id) AS 订单数 "
     "FROM dwd_order_di o JOIN dim_member m ON o.member_id = m.member_id GROUP BY m.member_name"),
]
for label, ds, sql in TWO_TABLE_NEG:
    n = fired(ev(sql, dataset=ds), "ONE_TO_MANY_UNHANDLED")
    ck("A1 两表负例不误报：%s" % label, n == 0,
       "触发 %d 次（多对一不放大，不应命中）" % n)

# --- 三表链式：A⋈B⋈C，相邻已声明、A-C 无直接声明 → 期望不报（连通即可）---
THREE_TABLE_NEG = [
    ("B 库 dwd_order_detail_di⋈dwd_order_di⋈dim_member（链式）", "B",
     "SELECT m.member_name AS 会员, SUM(od.item_id) AS 件数 "
     "FROM dwd_order_detail_di od "
     "JOIN dwd_order_di o ON od.order_id = o.order_id "
     "JOIN dim_member m ON o.member_id = m.member_id GROUP BY m.member_name"),
    ("A 库 orders⋈order_items⋈products（链式）", "A",
     "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
     "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
     "JOIN products p ON oi.product_id = p.product_id GROUP BY p.category_name"),
    ("B 库 ads_coupon_order_di⋈dim_member⋈dim_store（经中心维表桥接）", "B",
     "SELECT s.store_name AS 门店, SUM(c.coupon_amount) AS 券额 "
     "FROM ads_coupon_order_di c JOIN dim_member m ON c.member_id = m.member_id "
     "JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name"),
    ("A 库 orders⋈order_items⋈customers（双分支汇聚 orders）", "A",
     "SELECT cu.segment AS 客群, SUM(oi.subtotal) AS 销售额 "
     "FROM orders o JOIN order_items oi ON o.order_id = oi.order_id "
     "JOIN customers cu ON o.customer_id = cu.customer_id GROUP BY cu.segment"),
]
for label, ds, sql in THREE_TABLE_NEG:
    issues = ev(sql, dataset=ds)
    n = fired(issues, "ONE_TO_MANY_UNHANDLED")
    ck("A2 **三表链式 JOIN 不应误报**：%s" % label, n == 0,
       "触发 %d 次；三表通过已声明关系连成一片（A-B、B-C 均声明），A-C 本就无需直接关系" % n,
       quality=True)

# --- 真阳性：两张 MDL 表之间确无任何已声明关系 → 应报 ---
POS = [
    ("B 库 ads_member_repurchase_di ⋈ dws_store_daily_agg（MDL 无直接关系）", "B",
     "SELECT a.store_id, SUM(a.repurchase_cnt) AS c "
     "FROM ads_member_repurchase_di a JOIN dws_store_daily_agg d ON a.store_id = d.store_id "
     "GROUP BY a.store_id"),
]
for label, ds, sql in POS:
    n = fired(ev(sql, dataset=ds), "ONE_TO_MANY_UNHANDLED")
    ck("A3 真阳性：未声明关系跨表 JOIN + 聚合 → 应报：%s" % label, n >= 1,
       "未触发（真阳性漏检）")

# --- 合成索引：真正的 ONE_TO_MANY 关系两端都出现在 SQL → 必报 ---
synth = {"models": {}, "tables": {"fact_a": "fact_a", "dim_b": "dim_b"},
         "all_tables": {"fact_a", "dim_b"},
         "all_columns": {"fact_a.col1", "dim_b.col2", "col1", "col2"},
         "time_cols": set(),
         "rels": [{"joinType": "ONE_TO_MANY", "models": ["fact_a", "dim_b"], "name": "r1"}]}
out = rules.evaluate_all(
    parse1("SELECT b.col2, SUM(a.col1) AS s FROM fact_a a JOIN dim_b b ON a.id=b.id GROUP BY b.col2"),
    {"dataset": "B", "mdl_index": synth, "requirement": None})
ck("A4 真阳性：declared ONE_TO_MANY 两端命中 → 应报", fired(out, "ONE_TO_MANY_UNHANDLED") >= 1,
   "未触发")

# 单表聚合 → 不报（必须）
ck("A5 单表聚合不触发（必须）",
   fired(ev("SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg GROUP BY store_id"),
         "ONE_TO_MANY_UNHANDLED") == 0)

# --- A6 合成索引：BFS 连通性边界（图结构与期望由验收方独立构造，不读实现输出）---
def _synth(rels, models):
    return {"models": {}, "tables": {m: m for m in models},
            "all_tables": set(models), "all_columns": set(),
            "time_cols": set(), "rels": rels}


def _rel(a, b, jt="MANY_TO_ONE"):
    return {"joinType": jt, "models": [a, b], "name": "%s_%s" % (a, b)}


def _run_bfs(sql, idx):
    return rules.evaluate_all(parse1(sql), {"dataset": "B", "mdl_index": idx, "requirement": None})


_SQL4 = ("SELECT d.c, SUM(a.v) FROM t_a a JOIN t_b b ON a.id=b.id "
         "JOIN t_c c ON b.id=c.id JOIN t_d d ON c.id=d.id GROUP BY d.c")

# A6a 四表全连通链(a-b-c-d) → 分量=1 → 不报
_idx_chain = _synth([_rel("t_a", "t_b"), _rel("t_b", "t_c"), _rel("t_c", "t_d")],
                    ["t_a", "t_b", "t_c", "t_d"])
ck("A6a 四表全连通链 → 分量=1 不报",
   fired(_run_bfs(_SQL4, _idx_chain), "ONE_TO_MANY_UNHANDLED") == 0)

# A6b 四表 3 连通 + 1 孤立 → 分量=2 → 应报
_idx_iso = _synth([_rel("t_a", "t_b"), _rel("t_b", "t_c")], ["t_a", "t_b", "t_c", "t_d"])
ck("A6b 四表 3连通 + 1孤立 → 分量=2 应报",
   fired(_run_bfs(_SQL4, _idx_iso), "ONE_TO_MANY_UNHANDLED") >= 1,
   "孤立表 t_d 未被识别为独立分量")

# A6c 四表 2+2 两簇 → 分量=2 → 应报
_idx_2x2 = _synth([_rel("t_a", "t_b"), _rel("t_c", "t_d")], ["t_a", "t_b", "t_c", "t_d"])
_iss2 = _run_bfs(_SQL4, _idx_2x2)
ck("A6c 四表两簇(2+2) → 分量=2 应报",
   fired(_iss2, "ONE_TO_MANY_UNHANDLED") >= 1)

# A6d 分裂 detail 显式列出两组
_det = " ".join(x.get("detail", "") for x in _iss2 if x.get("rule") == "ONE_TO_MANY_UNHANDLED")
ck("A6d 分裂 detail 显式列出『连通分量』与两组表",
   ("连通分量" in _det) and ("t_a" in _det) and ("t_c" in _det),
   "detail=%s" % _det[:180], quality=True)

# A6e self-join 同一张表 → 归一后仅 1 表 → 不报
_idx_self = _synth([], ["t_a"])
ck("A6e 同表 self-join → 归一 1 表 不报",
   fired(_run_bfs("SELECT SUM(a.v) FROM t_a a JOIN t_a b ON a.id=b.id", _idx_self),
         "ONE_TO_MANY_UNHANDLED") == 0)

# A6f SQL 引用表均不在 MDL → 不报
ck("A6f SQL 引用表均不在 MDL → 不报",
   fired(_run_bfs("SELECT SUM(a.v) FROM nope_a a JOIN nope_b b ON a.id=b.id", _idx_chain),
         "ONE_TO_MANY_UNHANDLED") == 0)

# A6g 三表全连通但含声明 ONE_TO_MANY → 梯次(1) 仍报（真阳性未因连通性改造而丢）
_idx_otm = _synth([_rel("t_a", "t_b"), _rel("t_b", "t_c"), _rel("t_a", "t_c", jt="ONE_TO_MANY")],
                  ["t_a", "t_b", "t_c"])
ck("A6g 全连通但含声明 ONE_TO_MANY → 梯次(1) 仍报",
   fired(_run_bfs("SELECT c.c, SUM(a.v) FROM t_a a JOIN t_b b ON a.id=b.id "
                  "JOIN t_c c ON b.id=c.id GROUP BY c.c", _idx_otm),
         "ONE_TO_MANY_UNHANDLED") >= 1)

print()
print("=" * 78)
print("B. 返工项2 —— R6 白名单（time_cols）清洗")
print("=" * 78)
idx_b = rules.build_mdl_index("B")
plain_b = {c for c in idx_b["time_cols"] if "." not in c}
SHOULD_HAVE = ["stat_date", "stat_month", "order_date", "open_date", "register_date", "change_month"]
missing = sorted(c for c in SHOULD_HAVE if c not in plain_b)
ck("B1 B 库 time_cols 含全部真实时间列 %s" % SHOULD_HAVE, not missing, "缺失=%s" % missing)
HARD_DIRTY = ["member_cnt", "order_cnt", "sales_amount", "member_id"]
still = sorted(c for c in HARD_DIRTY if c in plain_b)
ck("B2 B 库 time_cols 不含严重脏值 %s" % HARD_DIRTY, not still, "残留=%s" % still)
left = sorted(c for c in ["day_type"] if c in plain_b)
ck("B3 day_type 已彻底剔除（『日期类型』非时间后缀机制生效）", not left,
   "残留=%s —— day_type 非时间列，混入白名单会污染 R6" % left)
# B5 精确集合核对：验收方按 mdl.json 逐列独立推导，B 库时间列恰为这 6 个
EXPECT_EXACT = {"stat_date", "stat_month", "order_date", "open_date",
                "register_date", "change_month"}
ck("B5 B 库 time_cols(plain) 恰等于独立推导的 6 个时间列",
   plain_b == EXPECT_EXACT,
   "实际=%s；多=%s；少=%s" % (sorted(plain_b), sorted(plain_b - EXPECT_EXACT),
                              sorted(EXPECT_EXACT - plain_b)))
idx_a = rules.build_mdl_index("A")
ck("B4 A 库 time_cols 正常（非空且含 order_date 类）",
   len(idx_a["time_cols"]) > 0, "A time_cols 为空")

print()
print("=" * 78)
print("C. 返工项3 —— gates_selftest 宿主离线可跑（setdefault 兜底）")
print("=" * 78)
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
gs_path = os.path.join(here, "tools", "gates_selftest.py")
try:
    gs_src = open(gs_path, encoding="utf-8").read()
    ck("C1 gates_selftest 顶部含 MDL_A_PATH/MDL_B_PATH setdefault 兜底",
       'setdefault("MDL_A_PATH"' in gs_src and 'setdefault("MDL_B_PATH"' in gs_src)
    ck("C2 兜底指向仓库内 workspace 相对路径（不依赖容器、不依赖 env）",
       "wren-docker" in gs_src and "mdl.json" in gs_src)
except FileNotFoundError:
    # 容器内无 tools/ 时跳过（该断言由宿主运行 gates_selftest 实证）
    print("  [SKIP] 容器内无 tools/gates_selftest.py，C 段由宿主 exit=0 实证")

print()
print("=" * 78)
print("D. 返工项4 —— rules_version 纳入 evaluate 源码指纹（实现变则版本必变）")
print("=" * 78)
try:
    import sqlpack
    v0 = sqlpack._rules_version()
    ck("D1 rules_version 为 8 位十六进制",
       len(v0) == 8 and all(ch in "0123456789abcdef" for ch in v0), v0)

    # 造一个语义等价、但**源码文本不同**的 evaluate（多一行无副作用语句）
    _saved_rule = rules.REGISTRY[4]

    def _ev_probe(ast_stmt, ctx):
        _ = "touched"  # 仅改源码文本，不改语义
        return _saved_rule["evaluate"](ast_stmt, ctx)

    try:
        new_rule = dict(_saved_rule)
        new_rule["evaluate"] = _ev_probe
        rules.REGISTRY[4] = new_rule
        v1 = sqlpack._rules_version()
        ck("D2 只改 evaluate 源码（元数据四元组不变）→ rules_version 必变",
           v1 != v0, "未变：%s == %s（说明源码指纹未真正纳入 digest）" % (v0, v1))
    finally:
        rules.REGISTRY[4] = _saved_rule
    v2 = sqlpack._rules_version()
    ck("D3 恢复规则后 rules_version 回到原值", v2 == v0, "恢复后=%s 原=%s" % (v2, v0))

    # 顺便：pack_version 三要素组合关系（采信产出值）
    import db
    row = db.query_one("SELECT demand_id, dataset FROM structured_requirements ORDER BY created_at DESC LIMIT 1")
    if row:
        p = sqlpack.build(row["demand_id"], dataset=row["dataset"])
        h = hashlib.sha256()
        for x in (p.get("schema_version"), str(p.get("requirement_version")), p.get("rules_version")):
            h.update(str(x if x is not None else "").encode("utf-8"))
        ck("D4 pack_version == sha8(schema_version+requirement_version+rules_version)",
           p.get("pack_version") == h.hexdigest()[:8],
           "实际=%s 期望=%s" % (p.get("pack_version"), h.hexdigest()[:8]))
except Exception as e:
    ck("D 段可执行", False, "%s: %s" % (type(e).__name__, e))

print()
print("=" * 78)
print("E. 回归 —— 顺序稳定 / 异常隔离 / REGISTRY 契约（不许退化）")
print("=" * 78)
s = "SELECT a, b, SUM(c) FROM t GROUP BY a"
o1 = [(x["rule"], x["snippet"]) for x in ev(s)]
o2 = [(x["rule"], x["snippet"]) for x in ev(s)]
ck("E1 evaluate_all 同输入两次顺序一致", o1 == o2)
_saved = list(rules.REGISTRY)
try:
    def _boom(ast_stmt, ctx):
        raise RuntimeError("故意抛异常")
    rules.REGISTRY.append({"id": "ZZ_BOOM", "name": "ZZ_BOOM", "severity": "warning",
                           "statement": "t", "detect": "b", "evaluate": _boom})
    out2 = rules.evaluate_all(parse1("SELECT * FROM dws_store_daily_agg"),
                              {"dataset": "B", "mdl_index": None, "requirement": None})
    ck("E2a 单规则抛异常 → 整体不崩且他规则结果保留",
       any(x.get("rule") == "SELECT_STAR" for x in out2))
    ck("E2b 异常规则留下 RULE_INTERNAL_ERROR 诊断",
       any(x.get("rule") == "RULE_INTERNAL_ERROR" for x in out2))
finally:
    rules.REGISTRY[:] = _saved
ids = [r["id"] for r in rules.REGISTRY]
ck("E3 REGISTRY 10 条 / id 唯一", len(ids) == 10 and len(set(ids)) == 10, "实际=%d" % len(ids))
ck("E4 一期 severity 全为 warning", {r["severity"] for r in rules.REGISTRY} == {"warning"})
old4 = {"JOIN_WITHOUT_CONDITION", "SELECT_STAR", "GROUP_BY_INCONSISTENT", "UNNECESSARY_DISTINCT"}
ck("E5 4 条老规则名一字未改", old4 <= set(ids))
# 死代码检查（返工第二轮）：首轮遗留的 _mdl_has_declared_rel 应已彻底删除
src = inspect.getsource(rules)
_dead_refs = src.count("_mdl_has_declared_rel")
ck("E6 首轮死代码 _mdl_has_declared_rel 已彻底删除",
   _dead_refs == 0, "源码中仍存在 %d 处引用（定义或调用）" % _dead_refs)
_bc = src.count("_bfs_connected_components")
ck("E6b 新增 BFS 辅助 _bfs_connected_components 已定义且被调用",
   _bc >= 2, "出现 %d 次（定义+调用应 ≥2）" % _bc)

print()
print("=" * 78)
print("合计：用例 %d 个；硬失败 %d 个；质量缺陷 %d 个" % (N[0], len(FAILS), len(DEFECTS)))
for f in FAILS:
    print("  硬失败：", f)
for d in DEFECTS:
    print("  质量缺陷：", d)
print("=" * 78)
sys.exit(1 if (FAILS or DEFECTS) else 0)
