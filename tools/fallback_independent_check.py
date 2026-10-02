# -*- coding: utf-8 -*-
"""M6-2 失败回退矩阵 · 验收方独立复扫（不采信交付方自证）

设计原则（沿用本项目验收纪律）：
  1. 独立复算 —— F3 的指纹/差异公式由本脚本**自行实现一遍**（与 fallback.py 零共享代码），
     再逐条比对 f3_candidate_diff 输出；不是"看实现有没有按公式写"。
  2. 对抗用例 —— 专打交付方自证覆盖不到的空白：阈值边界、鲁棒输入、
     REGISTRY 全量覆盖、退化路径、类型陷阱（bool 是 int 子类）。
  3. 环境无关 —— 宿主/容器均可跑；无 MDL 依赖时跳过依赖项而非报错。

跑法：PYTHONPATH=<repo>/gateway python tools/fallback_independent_check.py
exit 0 = 全部通过；exit 1 = 存在硬失败。
"""
import os
import sys
import difflib

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, os.path.join(ROOT, "gateway")):
    if p not in sys.path:
        sys.path.insert(0, p)

HARD = []      # 硬失败（实现缺陷或与我方独立复算不符）
QUALITY = []   # 质量项（不阻断，但记录）


def ck(name, cond, detail="", quality=False):
    tag = "OK " if cond else ("QUAL" if quality else "FAIL")
    print("  [%s] %s %s" % (tag, name, ("  -> " + str(detail)[:200] if detail else "")))
    if not cond:
        (QUALITY if quality else HARD).append(name)


print("=" * 72)
print("M6-2 失败回退矩阵 · 验收方独立复扫")
print("=" * 72)

import fallback as fb  # noqa: E402

# ======================================================================
# A. F3 差异判定 —— 独立复算（本脚本自实现指纹公式）
# ======================================================================
print("\n[A] F3 f3_candidate_diff 独立复算")


def my_fingerprint(sql_text):
    """独立实现（与 fallback._norm_sql_fingerprint 同规格、不同代码路径）。"""
    import sqlglot
    if not isinstance(sql_text, str):
        return ""
    s = sql_text.strip()
    if not s:
        return ""
    try:
        return sqlglot.parse_one(s, read="postgres").sql(dialect="postgres", pretty=False).strip()
    except Exception:
        return "".join(s.split()).lower()


def my_max_diff(cands):
    """独立复算：先按规格过滤，再两两 SequenceMatcher。"""
    clean = [c for c in cands if isinstance(c, str) and c.strip()]
    if len(clean) <= 1:
        return 0.0, len(clean)
    fps = [my_fingerprint(c) for c in clean]
    mx = 0.0
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            a, b = fps[i], fps[j]
            d = 0.0 if a == b else (1.0 - difflib.SequenceMatcher(None, a, b).ratio())
            mx = max(mx, d)
    return mx, len(clean)


A_CASES = [
    ("完全相同(大小写/空白/SELECT 显式列)", [
        "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date='2025-09-01' GROUP BY store_id",
        "  select  store_id ,  sum( sales_amount )  from dws_store_daily_agg  where stat_date = '2025-09-01' group by store_id ; ",
    ]),
    ("仅 WHERE 值不同", [
        "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date BETWEEN '2025-08-01' AND '2025-08-31' GROUP BY 1",
        "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date BETWEEN '2025-09-01' AND '2025-09-30' GROUP BY 1",
    ]),
    ("主题表 + JOIN 全不同", [
        "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date='2025-09-01' GROUP BY 1",
        "SELECT m.member_id, COUNT(DISTINCT o.order_id) FROM dwd_order_di o JOIN dim_member m ON o.member_id=m.member_id WHERE o.stat_date>'2025-01-01' GROUP BY 1 HAVING COUNT(DISTINCT o.order_id)>=5",
    ]),
    ("3 候选（2 同 1 异）", [
        "SELECT a FROM t1",
        "select a from t1;",
        "SELECT b, c, d FROM t2 JOIN t3 ON t2.k=t3.k WHERE t2.x>1",
    ]),
]

for tag, cands in A_CASES:
    r = fb.f3_candidate_diff(cands)
    exp_mx, exp_n = my_max_diff(cands)
    same = abs(r["max_diff"] - exp_mx) < 1e-3 and r["n"] == exp_n
    ck("A1 独立复算 max_diff/n 一致：%s" % tag, same,
       "impl(n=%s,max=%.4f hold=%s) vs mine(n=%s,max=%.4f)" % (
           r["n"], r["max_diff"], r["hold"], exp_n, exp_mx))

# A2 阈值边界：用环境变量精确夹住
os.environ["F3_DIFF_THRESHOLD"] = "0.40"
lo = fb.f3_candidate_diff(A_CASES[1][1])   # 小差异
hi = fb.f3_candidate_diff(A_CASES[2][1])   # 大差异
ck("A2a 阈值 0.40：小差异(%.4f) 不 hold" % hi["threshold"] if False else
   "A2a 阈值 0.40：小差异不 hold", lo["hold"] is False, "max=%.4f thr=%.3f" % (lo["max_diff"], lo["threshold"]))
ck("A2b 阈值 0.40：大差异 hold", hi["hold"] is True, "max=%.4f thr=%.3f" % (hi["max_diff"], hi["threshold"]))
# 同一对大差异，把阈值抬到 0.99 → 应不 hold
os.environ["F3_DIFF_THRESHOLD"] = "0.99"
hi2 = fb.f3_candidate_diff(A_CASES[2][1])
ck("A2c 阈值 0.99：同一对大差异(max=%.3f) 转为不 hold" % hi["max_diff"], hi2["hold"] is False,
   "thr=%.3f max=%.4f" % (hi2["threshold"], hi2["max_diff"]))
# 非法阈值 → 回落 0.35
os.environ["F3_DIFF_THRESHOLD"] = "abc"
r_bad = fb.f3_candidate_diff(A_CASES[2][1])
ck("A2d 非法阈值 'abc' → 回落默认 0.35", abs(r_bad["threshold"] - 0.35) < 1e-9, "thr=%s" % r_bad["threshold"])
# 越界阈值夹紧
os.environ["F3_DIFF_THRESHOLD"] = "5"
ck("A2e 阈值 5 → 夹到 0.999", fb.f3_candidate_diff(A_CASES[1][1])["threshold"] == 0.999)
os.environ["F3_DIFF_THRESHOLD"] = "-3"
ck("A2f 阈值 -3 → 夹到 0.001", fb.f3_candidate_diff(A_CASES[1][1])["threshold"] == 0.001)
os.environ.pop("F3_DIFF_THRESHOLD", None)

# A3 鲁棒输入：绝不抛异常
robust = [
    ("None", None, 0),
    ("空串", "", 1),
    ("空 list", [], 0),
    ("[None, '']", [None, ""], 0),
    ("单个 str（非 list）", "SELECT 1", 1),
    ("纯数字", 12345, 1),
    ("嵌套 list[list]", [["SELECT 1"], ["SELECT 2"]], 2),   # 元素非 str → 被过滤 → n=0
    ("tuple 2 元素", ("SELECT a FROM t", "SELECT b FROM t"), 2),
]
for tag, inp, exp_n in robust:
    try:
        r = fb.f3_candidate_diff(inp)
        ck("A3 鲁棒（%s）不抛异常" % tag, True, "n=%s hold=%s" % (r.get("n"), r.get("hold")))
    except Exception as e:
        ck("A3 鲁棒（%s）不抛异常" % tag, False, "抛了 %s: %s" % (type(e).__name__, e))

# A4 结论一致性：hold 必须等价于 max_diff >= threshold（除 n<=1 与 0.001 特例）
consist_ok = True
for tag, cands in A_CASES:
    r = fb.f3_candidate_diff(cands)
    if r["n"] > 1 and r["threshold"] > 0.001:
        if bool(r["hold"]) != bool(r["max_diff"] >= r["threshold"]):
            consist_ok = False
ck("A4 hold 与 max_diff>=threshold 结论一致（除 n<=1/极小阈值特例）", consist_ok)

# ======================================================================
# B. F4 治理指引 —— 覆盖 rules.REGISTRY 全部规则（动态读取，不写死）
# ======================================================================
print("\n[B] F4 f4_guidance 规则覆盖")
import rules as rules_mod  # noqa: E402

reg_ids = [r.get("id") for r in (getattr(rules_mod, "REGISTRY", None) or []) if r.get("id")]
ck("B1 REGISTRY 规则数 == 10", len(reg_ids) == 10, "实际 %d：%s" % (len(reg_ids), reg_ids))

gd = fb.f4_guidance(reg_ids)
by_id = {g["rule"]: g for g in gd}
ck("B2 f4_guidance 覆盖全部 REGISTRY id", set(by_id.keys()) == set(reg_ids),
   "缺=%s 多=%s" % (set(reg_ids) - set(by_id), set(by_id) - set(reg_ids)))
missing_how = [i for i in reg_ids if not (by_id.get(i, {}).get("how_to_fix") or "").strip()]
ck("B3 每条规则 how_to_fix 非空（无靠兜底文案蒙混）", not missing_how, "空=%s" % missing_how)
missing_why = [i for i in reg_ids if not (by_id.get(i, {}).get("why") or "").strip()]
ck("B4 每条规则 why 均取自 REGISTRY statement（非空）", not missing_why, "空=%s" % missing_why,
   quality=True)

# 未知 id → 兜底文案而非崩溃
unk = fb.f4_guidance(["NOT_A_REAL_RULE", "SELECT_STAR"])
unk_by = {g["rule"]: g for g in unk}
ck("B5 未知 id → how_to_fix 落兜底文案且不崩",
   "NOT_A_REAL_RULE" in unk_by and unk_by["NOT_A_REAL_RULE"]["how_to_fix"] and
   unk_by["NOT_A_REAL_RULE"]["why"] == "")
ck("B6 空/None 输入 → []", fb.f4_guidance([]) == [] and fb.f4_guidance(None) == [])
ck("B7 单 str 输入 → 包一层正常返回", len(fb.f4_guidance("SELECT_STAR")) == 1)
ck("B8 输入含 None/空串 → 剔除", [g["rule"] for g in fb.f4_guidance(["SELECT_STAR", None, "", "  "])] == ["SELECT_STAR"])

# ======================================================================
# C. F5 可交付判定 —— 类型陷阱与边界
# ======================================================================
print("\n[C] F5 f5_deliverable 类型与边界")
ck("C1 bool True（int 子类陷阱）→ 行数异常", fb.f5_deliverable(True, 5, [])["deliverable"] is False,
   fb.f5_deliverable(True, 5, [])["reason"])
ck("C2 float 5.0 非 int → 行数异常", fb.f5_deliverable(5.0, 5, [])["deliverable"] is False)
ck("C3 str '100' 非 int → 行数异常", fb.f5_deliverable("100", 5, [])["deliverable"] is False)
ck("C4 None 行数 → 行数异常", fb.f5_deliverable(None, 5, [])["deliverable"] is False)
ck("C5 0 行优先于 signals（0 行是主因）",
   "0 行" in fb.f5_deliverable(0, 5, ["L5: 某信号"])["reason"], fb.f5_deliverable(0, 5, ["x"])["reason"])
r6 = fb.f5_deliverable(10, 3, ["s1", "s2", "s3", "s4", "s5", "s6", "s7"])
ck("C6 信号 >5 条 → 截前 5 + '等 N 条'", "等 7 条" in r6["reason"], r6["reason"])
ck("C7 signals 为 str → 包一层（不逐字符拆）",
   fb.f5_deliverable(10, 3, "单一信号")["reason"] == "单一信号",
   fb.f5_deliverable(10, 3, "单一信号")["reason"])
ck("C8 signals 为 int（非 list/str）→ 当空，可交付", fb.f5_deliverable(10, 3, 123)["deliverable"] is True)
ck("C9 column_count None → 仅影响文案不影响结论",
   fb.f5_deliverable(10, None, [])["deliverable"] is True)
ck("C10 正常运行带 N×M 文案", "10 行 × 3 列" in fb.f5_deliverable(10, 3, [])["reason"])
ck("C11 负数 column_count 夹到 0 不崩", fb.f5_deliverable(10, -9, [])["deliverable"] is True)

# ======================================================================
# D. classify 汇总语义
# ======================================================================
print("\n[D] classify 汇总")
d_all = fb.classify(f1_hit=True, f2_hit=True, f3_diff={"hold": True, "max_diff": 0.9, "threshold": 0.35, "pairs": [[0, 1, 0.9]], "n": 2},
                    f4_rule_ids=["SELECT_STAR"], f5={"deliverable": False, "reason": "0 行"})
ck("D1 全命中 → kinds 顺序 F1..F5", d_all["kinds"] == ["F1", "F2", "F3", "F4", "F5"], d_all["kinds"])
ck("D2 F2/F3 → hold=True", d_all["hold"] is True)
ck("D3 F4/F5 命中 → deliverable=False", d_all["deliverable"] is False)
d_none = fb.classify()
ck("D4 空输入 → kinds=[] hold=False deliverable=True（无异常信号默认可交付）",
   d_none["kinds"] == [] and d_none["hold"] is False and d_none["deliverable"] is True, d_none)
d_f5ok = fb.classify(f5_rowcount=100, f5_colcount=6, f5_signals=[])
ck("D5 正常结果（100×6）→ 不命中 F5，可交付", "F5" not in d_f5ok["kinds"] and d_f5ok["deliverable"] is True)
d_f1s = fb.classify(f1_hit="yes")   # 真值字符串 → truthy
ck("D6 f1_hit 真值字符串 → 命中", "F1" in d_f1s["kinds"])
# 异常隔离：乱塞类型不崩
try:
    fb.classify(f3_diff="不是dict", f4_rule_ids=12345, f5=[1, 2, 3], f5_rowcount="x")
    ck("D7 classify 异常隔离（乱塞类型不崩）", True)
except Exception as e:
    ck("D7 classify 异常隔离（乱塞类型不崩）", False, "%s: %s" % (type(e).__name__, e))

# ======================================================================
# E. sqlgen.generate 多候选路径（F3 真链路）与向后兼容
# ======================================================================
print("\n[E] sqlgen.generate 多候选路径 / 向后兼容")
import sqlgen as sg  # noqa: E402

S_A = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date='2025-09-01' GROUP BY 1"
S_B = ("SELECT b.region, COUNT(DISTINCT a.member_id) FROM dwd_order_di a JOIN dim_store b ON a.store_id=b.store_id "
       "WHERE a.stat_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY 1")

# E1 两条完全相同 → 多候选分支但 hold=False，下发首条，且**无** hold 键
out_same = sg.generate(candidate_sql=[S_A, "  " + S_A + " ; "], dataset="B")
ck("E1 两条等价候选 → hold 键不存在（不 hold 不写 hold）", "hold" not in out_same,
   "keys=%s" % sorted(out_same.keys()))
ck("E1b 两条等价候选 → sql_draft 非空且取首条", (out_same.get("sql_draft") or "").startswith("SELECT store_id"),
   (out_same.get("sql_draft") or "")[:60])
ck("E1c 两条等价候选 → generator=client-agent", out_same.get("generator") == "client-agent")
ck("E1d 两条等价候选 → 附 candidate_diff 供审计", isinstance(out_same.get("candidate_diff"), dict))

# E2 两条大差异 → hold=True，sql_draft 置空
out_diff = sg.generate(candidate_sql=[S_A, S_B], dataset="B")
ck("E2 两条大差异 → hold=True 且 sql_draft 为空", out_diff.get("hold") is True and (out_diff.get("sql_draft") or "") == "",
   "hold=%s draft=%r" % (out_diff.get("hold"), out_diff.get("sql_draft")))

# E3 list of 1 → 等价 str 路径（无 hold/candidate_diff 键）
out_one = sg.generate(candidate_sql=[S_A], dataset="B")
ck("E3 list 单元素 → 退化为 str 路径（无 hold / candidate_diff 键）",
   "hold" not in out_one and "candidate_diff" not in out_one, sorted(out_one.keys()))
ck("E3b list 单元素 → sql_draft 非空", (out_one.get("sql_draft") or "") != "")

# E4 str 单候选 → 与 M5-3 完全一致（无新键）
out_str = sg.generate(candidate_sql=S_A, dataset="B")
base_keys = {"sql_draft", "field_mapping", "generation_notes", "generation_risks", "generator", "review"}
ck("E4 str 单候选 → 输出键集严格等于 M5-3 基线 6 键", set(out_str.keys()) == base_keys,
   "多出=%s 缺少=%s" % (set(out_str.keys()) - base_keys, base_keys - set(out_str.keys())))

# E5 非 str/非 list（int）→ 走原路径不崩
try:
    out_int = sg.generate(candidate_sql=12345, dataset="B")
    ck("E5 candidate_sql=int → 不崩（走 str() 原路径）", isinstance(out_int, dict))
except Exception as e:
    ck("E5 candidate_sql=int → 不崩（走 str() 原路径）", False, "%s: %s" % (type(e).__name__, e))

# ======================================================================
# F. sqlrun 硬约束：既有键不被覆写（读源码结构核验 + 键集回归靠 m5_verify）
# ======================================================================
print("\n[F] sqlrun 只增键不改键（静态结构核验）")
import inspect  # noqa: E402
src = inspect.getsource(sys.modules.get("sqlrun")) if "sqlrun" in sys.modules else ""
if not src:
    try:
        import sqlrun as srm
        src = inspect.getsource(srm)
    except Exception:
        src = ""
if src:
    # 新增键必须全部出现
    for k in ("deliverable", "deliverable_reason", "adopted"):
        ck("F1 成功返回含新增键 %s" % k, ('"%s": ' % k) in src or ("'%s': " % k) in src)
    # 旧键必须仍在
    for k in ("execution_summary", "validation_result", "suspicious_signals", "sql_run_id",
              "requirement_version", "schema_version", "pack_version"):
        ck("F2 旧键 %s 仍在返回结构" % k, k in src)
else:
    print("  [SKIP] 未加载 sqlrun 源码")

# ======================================================================
print("\n" + "=" * 72)
print("总览：硬失败 %d 项 / 质量项 %d 项" % (len(HARD), len(QUALITY)))
if HARD:
    print("硬失败清单：")
    for x in HARD:
        print("   - " + x)
    print("独立复扫未通过")
    sys.exit(1)
print("独立复扫通过")
sys.exit(0)
