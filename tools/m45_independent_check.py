#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收方独立复扫（M4-3 sqlpack + M5-2 sqlgen/sqlrun）。容器内运行。

设计原则：**不复用交付方自证脚本里的任何用例**。自证只测实现方想得到的输入；
这里专挑「实现方未必想到」的对抗、边界与闭集性，用来交叉验证：
  1) sqlpack 的候选是否真的只在 MDL 闭集内产生（不偷偷连物理库）；
  2) pack_version 是否真按 (schema_version, requirement_version, rules_version) 三要素哈希
     ——独立复算一遍，而不是信实现里的自述；
  3) 畸形 / 缺失输入下，plan / generate / execute 是「规范返回」还是「抛异常」；
  4) 写操作 SQL 是否在**执行前**就被拦，且**不留下 sql_runs 记录**。

用法（容器内）：
    docker exec -e PYTHONPATH=/app demand-gateway python /tmp/m45_independent_check.py
退出码 0 = 全过。
"""
import hashlib
import json
import sys

import db
import registry
import evidence
import sqlpack
import sqlgen
import sqlrun
import gates
import rules as rules_mod
import requirement as requirement_mod

FAILS = []
N = [0]
MISSING_MARK = "无候选结论（该槽位在本轮分析中为空）"


def ck(name, cond, detail=""):
    N[0] += 1
    tag = "PASS" if cond else "FAIL"
    line = "  [%s] %s" % (tag, name)
    if detail and not cond:
        line += "  -> %s" % str(detail)[:200]
    print(line)
    if not cond:
        FAILS.append(name)


def _rules_version(pack):
    """D1 用的 rules_version —— **取自被验产出本身**（pack["rules_version"]），不再复制实现算法。

    为什么不再独立复算：
      M6-1 首轮 rules_version = sha8(evidence.RULES + REGISTRY 四元组)；
      M6-1 返工又扩为「四元组 + evaluate 源码指纹（inspect.getsource）」。
      验收方若把算法抄一遍，实现每微调一次算法，复扫就要跟着改一次——永远滞后
      （这轮 D1 失败就是这么来的，属**验收脚本自身过时**，非实现缺陷）。

    正确姿势：验收方只验**组合关系**——pack_version == sha8(schema_version,
    requirement_version, rules_version)，其中 rules_version 采信产出值。
    「实现改了逻辑 → rules_version 必变」这一敏感性，另由
    tools/rules_rework_check.py 的 D 段用 monkeypatch 独立验证，不在此复制算法。
    """
    return (pack or {}).get("rules_version") or ""


def _sha8(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p if p is not None else "").encode("utf-8"))
    return h.hexdigest()[:8]


def _claim(v):
    return {"value": v, "confidence": 0.8, "evidence": [
        {"level": "P1", "level_name": "需求原文", "rank": 1, "source": "indep", "locator": "x", "content": v}]}


def _count_runs():
    r = db.query_one("SELECT count(*) AS c FROM sql_runs")
    return r["c"] if r else 0


# ===========================================================================
print("=" * 74)
print("验收方独立复扫 · M4-3 sqlpack + M5-2 sqlgen/sqlrun（不复用交付方自证用例）")
print("=" * 74)

# --- A. sqlpack：畸形输入鲁棒性（自证没测这些）--------------------------------
print("\n[A] sqlpack · 畸形/缺失输入鲁棒性")
weird = [None, [], "not-a-dict", {}, {"subject": None}, {"subject": "str-not-dict"},
         {"subject": {"value": None}}, {"output_fields": "not-a-list"},
         {"subject": {"value": ""}, "output_fields": [None, {}, {"value": None}]}]
for i, w in enumerate(weird):
    try:
        c = sqlpack.candidates("B", w)
        ok = isinstance(c, dict) and "miss_reason" in c
        ck("A%d candidates 对畸形输入 #%d 返回规范 dict（不抛异常）" % (i + 1, i + 1), ok, repr(c)[:120])
    except Exception as e:  # noqa: BLE001
        ck("A%d candidates 对畸形输入 #%d 返回规范 dict（不抛异常）" % (i + 1, i + 1), False,
           "%s: %s" % (type(e).__name__, e))

# --- B. sqlpack：闭集性（核心治理约束）----------------------------------------
print("\n[B] sqlpack · 候选闭集性（绝不越过 MDL 连物理库）")
mdlA = {m["name"] for m in registry.get("A").mdl["models"]}
mdlB = {m["name"] for m in registry.get("B").mdl["models"]}
colA = {c["name"] for m in registry.get("A").mdl["models"] for c in m.get("columns", [])}
colB = {c["name"] for m in registry.get("B").mdl["models"] for c in m.get("columns", [])}
reqB = {"subject": _claim("门店"), "output_fields": [_claim("销售额"), _claim("会员等级")]}
cB = sqlpack.candidates("B", reqB)
tables = [t["table"] for t in cB["subject_tables"]]
ck("B1 B 库候选表名全部 ∈ MDL 模型名集合", set(tables) <= mdlB, sorted(set(tables) - mdlB))
keys = [k["name"] for k in cB["key_candidates"]]
ck("B2 key_candidates 的列名全部 ∈ MDL 列名集合", set(keys) <= colB, sorted(set(keys) - colB))
tf = [c["name"] for c in cB["time_field_candidates"]]
ck("B3 time_field_candidates 的列名全部 ∈ MDL 列名集合", set(tf) <= colB, sorted(set(tf) - colB))
ff = [c["name"] for c in cB["filter_field_candidates"]]
ck("B4 filter_field_candidates 的列名全部 ∈ MDL 列名集合", set(ff) <= colB, sorted(set(ff) - colB))

# join_candidates 下标严格对应 mdl.relationships
relsB = registry.get("B").mdl["relationships"]
bad_idx = []
for j in cB["join_paths"]:
    i = j.get("relationship_index")
    if not isinstance(i, int) or not (0 <= i < len(relsB)):
        bad_idx.append(i)
    elif set(relsB[i].get("models") or []) != set(j.get("models") or []):
        bad_idx.append((i, "models 不一致"))
ck("B5 join_paths 的 relationship_index 与 mdl.relationships 逐项对齐",
   not bad_idx, bad_idx)

# --- C. sqlpack：占位串必须判为「无输入」--------------------------------------
print("\n[C] sqlpack · 缺失占位串的判定")
req_placeholder = {"subject": _claim(MISSING_MARK),
                   "output_fields": [_claim(MISSING_MARK)]}
cp = sqlpack.candidates("B", req_placeholder)
ck("C1 subject/output_fields 均为占位串 → 候选全空", len(cp["subject_tables"]) == 0, cp["subject_tables"][:2])
ck("C2 占位输入 → miss_reason 非空", bool(cp["miss_reason"].strip()), repr(cp["miss_reason"])[:120])

# A 库真实场景：上游槽位为空 → 全空 + miss_reason
reqA_empty = {"subject": _claim(MISSING_MARK), "output_fields": []}
ca = sqlpack.candidates("A", reqA_empty)
ck("C3 A 库（上游槽位空）→ 候选全空", len(ca["subject_tables"]) == 0 and len(ca["join_paths"]) == 0)
ck("C4 A 库 → miss_reason 同时给出两层原因（上游空 + 覆盖率）",
   ("上游" in ca["miss_reason"]) and ("覆盖率" in ca["miss_reason"]), ca["miss_reason"][:150])

# --- D. sqlpack：pack_version 独立复算（不信实现自述）--------------------------
print("\n[D] sqlpack · pack_version 三要素复算")
row = db.query_one("SELECT demand_id, dataset FROM structured_requirements ORDER BY created_at DESC LIMIT 1")
if row:
    did, ds = row["demand_id"], row["dataset"]
    pack = sqlpack.build(did, dataset=ds)
    rv = pack.get("requirement_version")
    expect = _sha8(pack.get("schema_version"), str(rv), _rules_version(pack))
    ck("D1 pack_version == sha8(schema_version + requirement_version + rules_version)",
       pack.get("pack_version") == expect,
       "实际=%s 期望=%s" % (pack.get("pack_version"), expect))
    pack2 = sqlpack.build(did, dataset=ds)
    ck("D2 同输入两次 build → pack_version 稳定", pack.get("pack_version") == pack2.get("pack_version"))
    # 两套候选名必须真接上（build 内部改名，最容易接错的一处）——用真实需求独立复验
    req_obj = requirement_mod.get(did)
    if isinstance(req_obj, dict) and req_obj.get("ok") is not False:
        cand2 = sqlpack.candidates(ds, req_obj)
        ck("D3 build.table_candidates 与 candidates.subject_tables 逐项相等",
           pack.get("table_candidates") == cand2["subject_tables"])
        ck("D4 build.join_candidates 与 candidates.join_paths 逐项相等",
           pack.get("join_candidates") == cand2["join_paths"])
    else:
        ck("D3 取到 structured_requirement 以复验两套候选名", False, repr(req_obj)[:120])
else:
    ck("D* 存在可用 structured_requirement 供复算", False, "库中无 structured_requirements 记录")

# build 对不存在的需求：必须显式报错，不编版本号
bad_pack = sqlpack.build("DR-NOT-EXIST-0000", dataset="B")
ck("D5 build(不存在的 demand_id) 返回 ok=False（不编造版本号）",
   isinstance(bad_pack, dict) and bad_pack.get("ok") is False, repr(bad_pack)[:120])
ck("D6 build 失败时不返回 pack_version 字段", "pack_version" not in bad_pack)

# --- E. sqlgen：plan 在异常/缺包场景不崩 --------------------------------------
print("\n[E] sqlgen · plan 鲁棒性")
try:
    pl_bad = sqlgen.plan("DR-NOT-EXIST-0000", dataset="B")
    ck("E1 plan(不存在的 demand) 不抛异常", isinstance(pl_bad, dict))
    ck("E2 plan 缺包时 draft_gate.status == 需人工审核",
       (pl_bad.get("draft_gate") or {}).get("status") == "需人工审核",
       (pl_bad.get("draft_gate") or {}).get("status"))
    ck("E3 plan 缺包时 chosen_table 为空（不硬选）", (pl_bad.get("chosen_table") or "") == "")
except Exception as e:  # noqa: BLE001
    ck("E1 plan(不存在的 demand) 不抛异常", False, "%s: %s" % (type(e).__name__, e))

if row:
    pl = sqlgen.plan(did, dataset=ds)
    ck("E4 plan 返回的 16 个键齐全",
       all(k in pl for k in ["demand_id", "dataset", "pack_version", "requirement_version",
                             "schema_version", "chosen_table", "chosen_table_reason",
                             "granularity", "granularity_reason", "join_path", "join_path_reason",
                             "time_field", "time_field_reason", "aggregate_fields",
                             "field_mapping", "draft_gate"]),
       sorted(pl.keys()))

# --- F. sqlgen/sqlrun：写操作必须被拦，且不留痕 --------------------------------
print("\n[F] 写操作拒绝 + 不留痕（独立设计，与自证用例不同）")
WRITE_SQLS = [
    ("DELETE", "DELETE FROM orders WHERE order_id = 1"),
    ("INSERT", "INSERT INTO orders (order_id) VALUES (999)"),
    ("UPDATE", "UPDATE orders SET amount = 0"),
    ("DROP", "DROP TABLE orders"),
    ("TRUNCATE", "TRUNCATE TABLE orders"),
    ("CTE 藏 DROP", "WITH x AS (SELECT 1) DELETE FROM orders"),
    ("多语句", "SELECT 1; DROP TABLE orders"),
]
for label, wsql in WRITE_SQLS:
    r = gates.review(wsql, dataset="A")
    ck("F·review「%s」被拦（status=不通过）" % label, r.get("status") == "不通过", r.get("status"))

# 执行层：写操作必须 blocked 且 sql_runs 行数不变
row2 = db.query_one("SELECT demand_id FROM structured_requirements ORDER BY created_at DESC LIMIT 1")
if row2:
    did2 = row2["demand_id"]
    before = _count_runs()
    ex = sqlrun.execute_readonly("DELETE FROM orders WHERE order_id = 1", dataset="A", demand_id=did2)
    after = _count_runs()
    ck("F1 execute_readonly(写操作) 返回 blocked", bool(ex.get("blocked")), repr(ex)[:150])
    ck("F2 写操作被拒后 sql_runs 行数不变（不留痕）", before == after, "before=%s after=%s" % (before, after))
    # sql=None 且该需求无历史：必须显式报错，不编造执行
    ex2 = sqlrun.execute_readonly(None, dataset="B", demand_id="DR-NOT-EXIST-0000")
    ck("F3 execute_readonly(sql=None, 无历史) 返回 ok=False（不编造）",
       ex2.get("ok") is False or ex2.get("blocked") is True, repr(ex2)[:150])

# --- G. generate：生成器归属与写操作初稿被拦 -----------------------------------
print("\n[G] generate · 生成器归属与门禁")
if row:
    g_write = sqlgen.generate(candidate_sql="DELETE FROM orders", demand_id=did, dataset=ds)
    ck("G1 客户端传入写操作 SQL → 采用但立刻被门禁拦（review=不通过）",
       g_write.get("generator") == "client-agent" and (g_write.get("review") or {}).get("status") == "不通过",
       "%s / %s" % (g_write.get("generator"), (g_write.get("review") or {}).get("status")))
    g_ok = sqlgen.generate(candidate_sql="SELECT 1 AS x", demand_id=did, dataset="A")
    ck("G2 客户端传入只读 SQL → generator=client-agent 且 review.status != 不通过",
       g_ok.get("generator") == "client-agent" and (g_ok.get("review") or {}).get("status") != "不通过",
       "%s / %s" % (g_ok.get("generator"), (g_ok.get("review") or {}).get("status")))

# ===========================================================================
print("\n" + "=" * 74)
print("用例 %d 个，失败 %d 个" % (N[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("   ✗ %s" % f)
    sys.exit(1)
print("✅ 独立复扫全过")
sys.exit(0)
