# -*- coding: utf-8 -*-
"""规则升级预演（不改源码）：验证「修 2 处误报 + 方向反解 + 分级阻断」上线后的预期效果。

用途：在出提示词前，用真实题库 SQL 预判三项改动的「收益」与「误伤面」。
输入：/app/e2e-gateway-report.json（演练报告，含 20 正向题完整 SQL + 10 陷阱 SQL）
输出：stdout 摘要
"""
import json
import sys

sys.path.insert(0, "/app")

import sqlglot
from sqlglot import exp

import rules as R

REPORT = "/app/e2e-gateway-report.json"
d = json.load(open(REPORT, encoding="utf-8"))
recs = d["part2_poc1"]["records"]
traps = d["part3_poc3"]["records"]

MDL = R.build_mdl_index("B")
REL = MDL.get("rels") or []
norm = R._norm_sql_name
model_norm = {norm(m) for m in MDL["models"]}
phys2model = {norm(pn): norm(mn) for mn, pn in (MDL["tables"] or {}).items() if pn}


def to_model(n):
    if n in model_norm:
        return n
    return phys2model.get(n)


# --------------------------------------------------------------------------
# 预演 1：方向反解（T05 修法）—— SQL 从「一端」发起 JOIN 到「多端」= 放大风险
# --------------------------------------------------------------------------
def table_seq(ast):
    seq = []
    for t in ast.find_all(exp.Table):
        m = to_model(norm(t.name or ""))
        if m and m not in seq:
            seq.append(m)
    return seq


def direction_hits(ast):
    seq = table_seq(ast)
    pos = {t: i for i, t in enumerate(seq)}
    hits = []
    for r in REL:
        jt = (r.get("joinType") or "").upper()
        ms = [norm(x) for x in (r.get("models") or [])]
        if len(ms) < 2:
            continue
        if jt == "MANY_TO_ONE":
            multi, one = ms[0], ms[1]
        elif jt == "ONE_TO_MANY":
            multi, one = ms[1], ms[0]
        else:
            continue
        if multi in pos and one in pos and pos[one] < pos[multi]:
            hits.append("%s(one=%s → multi=%s)" % (r.get("name"), one, multi))
    return hits


# --------------------------------------------------------------------------
# 预演 2：UNMAPPED_OBJECT_REF 误报面 —— 表前缀是「子查询/CTE 别名」的列
# --------------------------------------------------------------------------
def unmapped_false_positives(ast):
    ex = R._collect_cte_names(ast) | R._collect_subquery_aliases(ast)
    # 补：全局子查询别名（原 _collect_subquery_aliases 只扫 From，漏掉 Join 里的子查询）
    ex |= {norm(s.alias_or_name) for s in ast.find_all(exp.Subquery) if s.alias_or_name}
    bad = []
    for c in ast.find_all(exp.Column):
        tb = c.args.get("table")
        if tb is None:
            continue
        n = norm(tb.name if hasattr(tb, "name") else str(tb))
        if n in ex:
            bad.append("%s.%s" % (n, c.this.name if c.this else ""))
    return bad


# --------------------------------------------------------------------------
# 预演 3：JOIN_WITHOUT_CONDITION 误报面 —— 无条件连接但操作数是子查询
# --------------------------------------------------------------------------
def join_false_positives(ast):
    out = []
    for j in ast.find_all(exp.Join):
        if j.args.get("on") is not None or j.args.get("using"):
            continue
        if isinstance(j.this, exp.Subquery) or j.this.find(exp.Subquery) is not None:
            out.append(j.sql(dialect="postgres")[:70])
    return out


def parse(sql):
    try:
        return sqlglot.parse_one(sql, read="postgres")
    except Exception:
        return None


pos = [r for r in recs if r.get("type") == "正向" and r.get("sql")]

print("=" * 74)
print("预演 1 · 方向反解：正向题里会不会被误判为「一对多放大」")
print("=" * 74)
hit_pos = []
for r in pos:
    ast = parse(r["sql"])
    if ast is None:
        continue
    h = direction_hits(ast)
    if h:
        hit_pos.append(r["id"])
        print("  [命中] %s %s" % (r["id"], (r.get("q") or "")[:26]))
        print("        %s" % "; ".join(h))
        print("        SQL: %s" % r["sql"][:160])
print("  正向题方向命中：%d/%d  %s" % (len(hit_pos), len(pos),
                                    "→ 零误伤 ✓" if not hit_pos else "→ 存在误伤，需处置"))

print()
print("=" * 74)
print("预演 1b · 方向反解：10 条陷阱题应命中哪些（T05 是目标）")
print("=" * 74)
for t in traps:
    sql = t.get("sql")
    if not sql:
        continue
    ast = parse(sql)
    if ast is None:
        continue
    h = direction_hits(ast)
    mark = "命中" if h else "  - "
    print("  [%s] %-4s %s %s" % (mark, t["id"], (t.get("desc") or "")[:30], "; ".join(h)))

print()
print("=" * 74)
print("预演 2 · UNMAPPED_OBJECT_REF 误报面（应只剩 P04 一处）")
print("=" * 74)
fp2 = []
for r in pos:
    ast = parse(r["sql"])
    if ast is None:
        continue
    b = unmapped_false_positives(ast)
    if b:
        fp2.append(r["id"])
        print("  [误报] %s %s :: %s" % (r["id"], (r.get("q") or "")[:22], ", ".join(b)))
print("  含「子查询别名限定列」的正向题：%d/%d" % (len(fp2), len(pos)))

print()
print("=" * 74)
print("预演 3 · JOIN_WITHOUT_CONDITION 误报面（应只剩 P04 一处）")
print("=" * 74)
fp3 = []
for r in pos:
    ast = parse(r["sql"])
    if ast is None:
        continue
    b = join_false_positives(ast)
    if b:
        fp3.append(r["id"])
        print("  [误报] %s %s :: %s" % (r["id"], (r.get("q") or "")[:22], " | ".join(b)))
print("  含「无条件子查询连接」的正向题：%d/%d" % (len(fp3), len(pos)))

print()
print("=" * 74)
print("小结")
print("=" * 74)
print("  修误报后，正向题 L2 阻断面应从 2/20 降到 %d/20（只余 P19 的 TIME_FIELD_SUSPECT，属软提示不阻断）"
      % (len([r for r in pos if r["id"] in fp2 or r["id"] in fp3])))
print("  方向反解新增误伤：%d 题" % len(hit_pos))
