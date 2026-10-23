#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 质量基线：跑固定题集，出可复算的分数，并防回退。

治的是哪一类病
--------------
「SQL 质量更高了」这句话，在本项目里一直是**形容词**。确定性规划器有封闭意图库，
大模型是开放生成 —— 改一个分支、换一句 prompt、调一次温度，
到底变好还是变坏，此前只能靠感觉。感觉不能当验收。

本脚本把它变成**动词**：同一份题集（`tools/sql_quality_corpus.json`）、
同一套评分口径、同一组数字。跑完出六个指标，并与基线文件比对；
任一项低于基线就判失败（回退即红）。

⚠️ 期望值按「业务上应该怎样」写，**不按当前实现怎样**写
---------------------------------------------------------
所以首次跑出来的分数可能很难看 —— 那是**真实差距**，不是题集写错了。
把期望值改小可以让分数变好看，那等于自己给自己发合格证。
⇒ 本脚本**只做两件事**：如实测、对比基线。不改题集、不改期望值。

怎么离线跑（不需要数据库 / Wren / 容器）
---------------------------------------
规划器与门禁都是纯函数链，只要两份 MDL 在本地就能跑：

    python3 tools/sql_quality_baseline.py              # 跑一遍，打印分数
    python3 tools/sql_quality_baseline.py --update      # 把当前分数写成新基线
    python3 tools/sql_quality_baseline.py --json        # 机器可读输出
    python3 tools/sql_quality_baseline.py --report      # 另存 md+json 到 tools/_artifacts/

MDL 路径缺失时会按仓库约定自动找（`wren-docker*/workspace/mdl.json`）；
找不到就**显式报错**，不静默当成"0 题通过"（那是本项目最熟悉的失效形态）。

退出码：0 = 不低于基线；1 = 有回退（或题集/环境有问题）。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))

CORPUS_PATH = os.path.join(HERE, "sql_quality_corpus.json")
BASELINE_PATH = os.path.join(HERE, "sql_quality_baseline.json")
ARTIFACT_DIR = os.path.join(HERE, "_artifacts")

# 指标名 → 中文说明。顺序即报告顺序。
METRICS = [
    ("generated_rate", "出数率（该出数的题里，真的生成了 SQL 的比例）"),
    ("gate_pass_rate", "门禁通过率（生成出来的 SQL 里，五层全过的比例）"),
    ("element_rate", "要素齐备率（该出数的题里，必须出现的要素全都出现了的比例）"),
    ("trap_rate", "陷阱规避率（该出数的题里，没有踩中禁用列/写法的比例）"),
    ("refuse_rate", "拒绝正确率（不该出数的题里，明确拒绝且理由对得上的比例）"),
    ("overall_rate", "整体达标率（全部题目逐题达标的比例）"),
]


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------
def ensure_mdl_paths():
    """确保两份 MDL 路径可用；缺失则显式报错（不静默降级）。

    ⚠️ 为什么必须显式报错：`registry.get()` 在 MDL 缺失时会给出一个空模型集，
    于是所有题都"未命中已建模意图"被拒 —— 而 kind=refuse 的题**照样通过**。
    净效果是「分数看起来还行，其实什么都没跑」。这正是 REQ-COVERAGE 的病根形态，
    不能在测量工具里重演一遍。
    """
    cands = {
        "MDL_A_PATH": ["wren-docker/workspace/mdl.json"],
        "MDL_B_PATH": ["wren-docker-b/workspace/mdl.json"],
    }
    missing = []
    for var, rels in cands.items():
        cur = (os.getenv(var) or "").strip()
        if cur and os.path.isfile(cur):
            continue
        found = ""
        for rel in rels:
            p = os.path.join(ROOT, rel)
            if os.path.isfile(p):
                found = p
                break
        if not found:
            missing.append("%s（试过 env 与 %s）" % (var, "、".join(rels)))
            continue
        os.environ[var] = found
    if missing:
        raise SystemExit(
            "无法定位 MDL，测量不会执行：\n  - %s\n"
            "⇒ 先确认 wren-docker*/workspace/mdl.json 存在，或显式设置环境变量。\n"
            "⚠️ 不会「跳过并报 0 题通过」——那会让一次没跑的测量看起来像通过。"
            % "\n  - ".join(missing))


def _norm(s):
    """比对用的归一化：小写 + 压掉所有空白。

    ⚠️ 必须压掉空白：SQL 里的换行与缩进会随拼接方式变化
    （`"ORDER BY"\n" 销售额"` 与 `"ORDER BY 销售额"` 是同一件事），
    不压掉就会出现「同一份 SQL 时对时错」的假波动。
    """
    return re.sub(r"\s+", "", str(s or "")).lower()


# ---------------------------------------------------------------------------
# 单题评分
# ---------------------------------------------------------------------------
def evaluate_case(case, ds, plan_fn, review_fn):
    """跑一题，返回记录 dict（字段即报告字段）。"""
    cid = case.get("id") or "?"
    kind = case.get("kind") or "sql"
    text = case.get("text") or ""
    rec = {
        "id": cid, "dataset": case.get("dataset"), "kind": kind, "text": text,
        "note": case.get("note") or "",
        "generated": False, "intent": "", "generator": "deterministic-planner",
        "sql": "", "gate": "", "gate_layers_failed": [],
        "missing": [], "traps": [], "ok": False, "why": "",
    }
    try:
        r = plan_fn(ds, text) or {}
    except Exception as e:  # noqa: BLE001
        rec["why"] = "规划器抛异常 %s: %s" % (type(e).__name__, str(e)[:120])
        return rec

    rec["intent"] = str(r.get("intent") or "")
    sql = str(r.get("sql") or "")
    rec["generated"] = bool(sql.strip())
    rec["sql"] = sql

    if kind == "refuse":
        # 期望拒绝：看它有没有拒、以及理由对不对得上
        if rec["generated"]:
            rec["why"] = "期望拒绝，却生成了 SQL（等于替业务方猜了一个口径）"
            return rec
        hay = _norm(rec["intent"] + " " + str(r.get("reason") or ""))
        groups = case.get("refuse_reason") or []
        if not groups:
            rec["ok"] = True
            rec["why"] = "已拒绝（题集未限定理由关键词）"
            return rec
        for g in groups:
            if not any(_norm(k) in hay for k in g):
                rec["why"] = "已拒绝，但理由里缺「%s」——拒得不明不白，业务方不知道该怎么办" % "或".join(g)
                return rec
        rec["ok"] = True
        rec["why"] = "已拒绝且理由对得上"
        return rec

    # ---- 期望出数 ----
    if not rec["generated"]:
        rec["why"] = "期望出数，却被拒：%s" % str(r.get("reason") or rec["intent"])[:120]
        return rec

    try:
        rev = review_fn(sql, dataset=case.get("dataset") or "B") or {}
    except Exception as e:  # noqa: BLE001
        rev = {}
        rec["why"] = "门禁抛异常 %s: %s" % (type(e).__name__, str(e)[:120])
    rec["gate"] = str(rev.get("status") or "")
    for layer in (rev.get("layers") or []):
        # ⚠️ 用 skipped 而不是 ok 判「这层到底有没有查」：
        #   L-SQLFluff / L-GX 是「一期未启用」，它们的 ok 恒为 False，
        #   但从未真正执行过 —— 把它们算成"失败层"会让报告指向不存在的问题。
        if layer.get("ok") is False and layer.get("skipped") is not True:
            rec["gate_layers_failed"].append(str(layer.get("layer")))

    hay = _norm(sql)
    rec["missing"] = [t for t in (case.get("must_have") or []) if _norm(t) not in hay]
    rec["traps"] = [t for t in (case.get("must_not_have") or []) if _norm(t) in hay]

    problems = []
    if rec["gate"] != "通过":
        problems.append("门禁=%s（%s）" % (rec["gate"] or "无结果",
                                         "、".join(rec["gate_layers_failed"]) or "原因未给出"))
    if rec["missing"]:
        problems.append("缺要素：%s" % "、".join(rec["missing"]))
    if rec["traps"]:
        problems.append("踩陷阱：%s" % "、".join(rec["traps"]))
    rec["ok"] = not problems
    rec["why"] = "；".join(problems) if problems else "SQL 达标"
    return rec


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def summarize(records):
    sql_cases = [r for r in records if r["kind"] == "sql"]
    ref_cases = [r for r in records if r["kind"] == "refuse"]
    gen = [r for r in sql_cases if r["generated"]]

    def _rate(num, den):
        return round(float(num) / den, 4) if den else 0.0

    metrics = {
        "total": len(records),
        "sql_cases": len(sql_cases),
        "refuse_cases": len(ref_cases),
        "generated": len(gen),
        "generated_rate": _rate(len(gen), len(sql_cases)),
        "gate_pass_rate": _rate(sum(1 for r in gen if r["gate"] == "通过"), len(gen)),
        "element_rate": _rate(sum(1 for r in gen if not r["missing"]), len(gen)),
        "trap_rate": _rate(sum(1 for r in gen if not r["traps"]), len(gen)),
        "refuse_rate": _rate(sum(1 for r in ref_cases if r["ok"]), len(ref_cases)),
        "overall_rate": _rate(sum(1 for r in records if r["ok"]), len(records)),
    }
    return metrics


def compare(metrics, baseline):
    """与基线比对，返回 (是否回退, 回退明细)。

    ⚠️ 三个方向的约定（写反任何一个都会让回退被藏起来）：
      ① 当前值 < 基线值            ⇒ 回退；
      ② 基线里没有这个指标          ⇒ **跳过**（新加指标不该一上线就红）；
      ③ 当前**缺**这个指标          ⇒ **算回退**（没算出来 ≠ 没变化；
                                      否则测量脚本某段崩了，看起来会像"一切照旧"）。
    """
    drops = []
    for key, label in METRICS:
        old = (baseline or {}).get("metrics", {}).get(key)
        if old is None:
            continue
        cur = metrics.get(key, 0.0)
        if cur + 1e-9 < float(old):
            drops.append((key, label, float(old), cur))
    return (len(drops) > 0), drops


def case_regressions(records, baseline):
    """逐题比对：基线里本来就过的题，这次变成不过 = 回退。

    ⚠️ 为什么除总分之外还要逐题看：**总分会被题目之间的此长彼消掩盖**。
    一个分支修好了（+1）、另一个分支改坏了（-1），整体率纹丝不动，
    于是回退被完美隐藏 —— 这是"整体指标"的通病，必须配逐题清单。
    """
    old = (baseline or {}).get("cases") or {}
    out = []
    for r in records:
        if old.get(r["id"]) is True and not r["ok"]:
            out.append(r)
    return out


def new_passes(records, baseline):
    """基线里没过、这次过了 —— 值得报告的改善（不改判定，只报喜）。"""
    old = (baseline or {}).get("cases") or {}
    return [r for r in records if old.get(r["id"]) is False and r["ok"]]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def pct(v):
    return "%.1f%%" % (float(v) * 100.0)


def print_table(records, metrics):
    print("=" * 96)
    print("SQL 质量基线 · 逐题")
    print("=" * 96)
    print("%-6s %-3s %-30s %-6s %-8s %-6s %s"
          % ("编号", "库", "需求原话", "出数", "门禁", "达标", "不达标原因"))
    print("-" * 96)
    for r in records:
        txt = r["text"] if len(r["text"]) <= 28 else r["text"][:27] + "…"
        print("%-6s %-3s %-30s %-6s %-8s %-6s %s"
              % (r["id"], r["dataset"], txt,
                 "是" if r["generated"] else "否",
                 r["gate"] or "—",
                 "✓" if r["ok"] else "✗",
                 r["why"] if not r["ok"] else ""))
    print("-" * 96)
    print("汇总")
    for key, label in METRICS:
        print("  %-18s %-8s %s" % (key, pct(metrics.get(key, 0)), label))
    print("  题数：共 %d（该出数 %d / 该拒绝 %d），实际出数 %d"
          % (metrics["total"], metrics["sql_cases"],
             metrics["refuse_cases"], metrics["generated"]))


def write_report(records, metrics, verdict, drops, case_drops):
    if not os.path.isdir(ARTIFACT_DIR):
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = {
        "generated_at": ts, "metrics": metrics,
        "verdict": verdict, "drops": drops,
        "case_regressions": [r["id"] for r in case_drops],
        "cases": records,
    }
    jp = os.path.join(ARTIFACT_DIR, "sql-quality-%s.json" % ts)
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    mp = os.path.join(ARTIFACT_DIR, "sql-quality-%s.md" % ts)
    lines = ["# SQL 质量基线报告 %s" % ts, "",
             "判定：**%s**" % verdict, "", "## 汇总", "",
             "| 指标 | 值 | 说明 |", "|---|---|---|"]
    for key, label in METRICS:
        lines.append("| `%s` | %s | %s |" % (key, pct(metrics.get(key, 0)), label))
    lines += ["", "题数：共 %d（该出数 %d / 该拒绝 %d）" %
              (metrics["total"], metrics["sql_cases"], metrics["refuse_cases"]), "",
              "## 逐题", "", "| 编号 | 库 | 需求 | 出数 | 门禁 | 达标 | 说明 |",
              "|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |"
                     % (r["id"], r["dataset"], r["text"],
                        "是" if r["generated"] else "否", r["gate"] or "—",
                        "✓" if r["ok"] else "✗", r["why"] or ""))
    with open(mp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return jp, mp


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(corpus=None, corpus_path=None):
    """跑题集，返回 (records, metrics)。供 CLI 与自测共用。"""
    ensure_mdl_paths()
    import registry            # noqa: PLC0415
    import planner             # noqa: PLC0415
    import gates               # noqa: PLC0415

    if corpus is None:
        with open(corpus_path or CORPUS_PATH, "r", encoding="utf-8") as f:
            corpus = json.load(f)
    cases = corpus.get("cases") or []
    if not cases:
        raise SystemExit("题集为空：%s —— 空题集跑出来的是「0/0 = 通过」，那是假绿。"
                         % (corpus_path or CORPUS_PATH))

    plan_fns = {"A": planner.plan_a, "B": planner.plan_b}

    def _review(sql, dataset="B"):
        return gates.review(sql, dataset=dataset, requirement=None)

    records = []
    for c in cases:
        ds_key = (c.get("dataset") or "B").upper()
        try:
            ds = registry.get(ds_key)
        except Exception as e:  # noqa: BLE001
            records.append({
                "id": c.get("id"), "dataset": ds_key, "kind": c.get("kind"),
                "text": c.get("text"), "note": c.get("note") or "",
                "generated": False, "intent": "", "generator": "",
                "sql": "", "gate": "", "gate_layers_failed": [],
                "missing": [], "traps": [], "ok": False,
                "why": "取数据集失败 %s: %s" % (type(e).__name__, str(e)[:100]),
            })
            continue
        plan_fn = plan_fns.get(ds_key)
        if plan_fn is None:
            raise SystemExit("题集里出现未知数据集 %r（题 %s）" % (ds_key, c.get("id")))
        records.append(evaluate_case(c, ds, plan_fn, _review))
    return records, summarize(records)


def main(argv=None):
    ap = argparse.ArgumentParser(description="SQL 质量基线（离线、可复算、防回退）")
    ap.add_argument("--corpus", default=CORPUS_PATH, help="题集路径")
    ap.add_argument("--baseline", default=BASELINE_PATH, help="基线文件路径")
    ap.add_argument("--update", action="store_true", help="把当前分数写成新基线")
    ap.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    ap.add_argument("--report", action="store_true", help="另存 md+json 到 tools/_artifacts/")
    args = ap.parse_args(argv)

    t0 = time.time()
    records, metrics = run(corpus_path=args.corpus)
    elapsed = time.time() - t0

    baseline = None
    if os.path.isfile(args.baseline):
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                baseline = json.load(f)
        except Exception as e:  # noqa: BLE001
            raise SystemExit("基线文件读不动（%s）：%s" % (args.baseline, e))

    if args.update:
        payload = {
            "version": 1,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "about": [
                "SQL 质量基线（资产，入库）。由 tools/sql_quality_baseline.py --update 写入。",
                "⚠️ 这个文件记的是「当前真实水平」，不是「目标水平」。",
                "把数字改大不改变任何事实；要让它变大，只能改代码或改题集里的期望值，",
                "而改期望值等于降低验收标准 —— 改之前先想清楚是不是真的不该要求。",
            ],
            "metrics": metrics,
            "cases": {r["id"]: bool(r["ok"]) for r in records},
        }
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("已写入基线：%s（%d 题，整体达标率 %s）"
              % (args.baseline, metrics["total"], pct(metrics["overall_rate"])))
        print_table(records, metrics)
        return 0

    has_drop, drops = compare(metrics, baseline)
    case_drops = case_regressions(records, baseline)
    improved = new_passes(records, baseline)

    if args.json:
        print(json.dumps({
            "metrics": metrics, "elapsed_seconds": round(elapsed, 3),
            "baseline_present": baseline is not None,
            "regressed": has_drop or bool(case_drops),
            "metric_drops": [{"metric": k, "was": o, "now": c} for k, _l, o, c in drops],
            "case_regressions": [r["id"] for r in case_drops],
            "cases": records,
        }, ensure_ascii=False, indent=2))
    else:
        print_table(records, metrics)
        print("\n耗时 %.2fs" % elapsed)

    if baseline is None:
        print("\n⚠️ 没有基线文件（%s）⇒ 本次**只测量、不判定**。" % args.baseline)
        print("   确认这套分数可以作为起点后，跑 --update 写入基线。")
        if args.report:
            jp, mp = write_report(records, metrics, "无基线（仅测量）", drops, case_drops)
            print("报告已存：%s / %s" % (jp, mp))
        return 0

    if drops:
        print("\n❌ 指标回退（与基线比）：")
        for key, label, old, cur in drops:
            print("   %-18s %s → %s   %s" % (key, pct(old), pct(cur), label))
    if case_drops:
        print("\n❌ 逐题回退（基线里过、这次不过）：")
        for r in case_drops:
            print("   %-6s %s —— %s" % (r["id"], r["text"], r["why"]))
    if improved and not (drops or case_drops):
        print("\n✅ 相比基线有改善（%d 题从不过变过）：%s"
              % (len(improved), "、".join(r["id"] for r in improved)))

    verdict = "回退" if (drops or case_drops) else "不低于基线"
    if args.report:
        jp, mp = write_report(records, metrics, verdict, drops, case_drops)
        print("\n报告已存：%s / %s" % (jp, mp))

    if drops or case_drops:
        return 1
    print("\n✅ %s：%d 题整体达标率 %s"
          % (verdict, metrics["total"], pct(metrics["overall_rate"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
