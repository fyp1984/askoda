#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POC 端到端演练 · 走自研网关 MCP 工具面（面向演示）

与 P0 期 `poc_eval.py` 的区别（这是本脚本存在的理由）：
  · `poc_eval.py`   —— 直连 Wren MCP（9002），只测「语义层 + 受控生成」两件事；
  · 本脚本          —— 走**自研网关** `http://127.0.0.1:18080/mcp`（MCP 工具面 41 个），
                       演练的是**整条产品链路**：受理 → 语义分析 → 澄清问答 → 结构化需求
                       → SQL 生成 → 五层门禁 → 只读执行 → 九字段留痕 → 审计回放。

四段内容：
  Part 1 主线故事   —— 一条业务需求走完整链路，逐步打印真实输出（演示主线）
  Part 2 POC-1      —— bank-b.json 30 题（20 正向 + 10 负向）**经网关**复跑，算准确率 / 拒绝率
  Part 3 POC-3      —— PRD §17.4 的 10 条陷阱用例，测门禁拦截率 + 单次 review 耗时
  Part 4 POC-2      —— 8 表 MDL 建模事实核对（模型数 / 列数 / 关系数 / 敏感字段零建模）

用法：
  python3 poc_e2e_gateway.py [--mcp http://127.0.0.1:18080/mcp] [--quick]
输出：
  e2e-gateway-report.json（机器可读）+ 控制台可读摘要
退出码：0（演练完成）。指标是否达标在报告里判定，不以退出码表达。
"""
import argparse
import json
import os
import pathlib
import time
import urllib.request

HERE = pathlib.Path(__file__).parent
ROOT = HERE.parent

DEFAULT_MCP = os.environ.get("E2E_MCP_URL", "http://127.0.0.1:18080/mcp")
DATASET = "B"


# ---------------------------------------------------------------------------
# 网关 MCP 客户端（streamable-http）
# ---------------------------------------------------------------------------
class Gw:
    def __init__(self, url):
        self.url = url
        self.sid = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.t0 = time.time()

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = 1
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.sid:
            hdrs["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=hdrs)
        with self.opener.open(req, timeout=120) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.sid = sid
            text = r.read().decode()
        out = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    out.append(json.loads(line[5:]))
                except Exception:
                    pass
        return out

    def connect(self):
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "poc-e2e-gateway", "version": "1.0"}})
        self._rpc("notifications/initialized", {}, notify=True)

    def call(self, tool, args):
        for r in self._rpc("tools/call", {"name": tool, "arguments": args or {}}):
            if "result" in r:
                try:
                    return json.loads(r["result"]["content"][0]["text"])
                except Exception as e:  # noqa: BLE001
                    return {"_parse_error": "%s: %s" % (type(e).__name__, e)}
        return {"_rpc_empty": True}


def hr(title):
    print("\n" + "=" * 76)
    print(title)
    print("=" * 76)


def step(n, text):
    print("\n  [%s] %s" % (n, text))


# ---------------------------------------------------------------------------
# Part 1 · 主线故事
# ---------------------------------------------------------------------------
DEMO_Q = "各门店的复购用户数分别是多少"


def part1(gw, report):
    hr("Part 1 · 主线故事（一条业务需求走完整链路）")
    print("  业务口吻需求：%s" % DEMO_Q)
    rec = {"question": DEMO_Q}

    step("1/11", "受理需求（demand_create）")
    d = gw.call("demand_create", {
        "title": DEMO_Q,
        "business_context": "季度经营分析会需要按门店看复购情况",
        "description": DEMO_Q,
        "expected_output": "门店 × 复购用户数，一行一店",
        "contact": "张三 / 会员运营组 / 138-0000-0000",
        "actor": "demo-business",
    })
    did = d.get("demand_id")
    rec["demand_id"] = did
    print("      demand_id = %s ｜ status = %s" % (did, d.get("status")))
    masked = (d.get("record") or {}).get("contact") or d.get("contact") or ""
    print("      掩码抽查 contact = %s" % masked)

    step("2/11", "语义分析（analysis_first_round）")
    a = gw.call("analysis_first_round", {"demand_id": did, "dataset": DATASET, "actor": "demo-analyst"})
    slots = a.get("slots") or {}
    top = {}
    for k, v in slots.items():
        cs = (v or {}).get("candidates") or []
        top[k] = (cs[0] or {}).get("value") if cs else None
    qs = a.get("questions") or []
    print("      槽位首候选：%s" % json.dumps(top, ensure_ascii=False)[:220])
    print("      证据链 %d 条 ｜ 确认问题 %d 个 ｜ 置信度 %s"
          % (len(a.get("evidence_chain") or []), len(qs), a.get("confidence_overall")))
    rec["analysis"] = {"slots": top, "questions": len(qs),
                       "evidence": len(a.get("evidence_chain") or []),
                       "confidence": a.get("confidence_overall")}

    step("3/11", "澄清问答闭环（confirmation_list → answer）")
    cl = gw.call("confirmation_list", {"demand_id": did})
    items = cl.get("items") or []
    answered_txt = None
    if items:
        q0 = items[0]
        print("      待确认：%s" % q0.get("question"))
        ans = gw.call("confirmation_answer", {
            "demand_id": did, "question_id": q0.get("question_id"),
            "answer": "按业务单据逐条统计", "actor": "demo-business",
        })
        answered_txt = ans.get("answer")
        # answer() 的返回体本身不带明细（只有 noop 分支才回带 confirmation），
        # 因此答完重新列一次，用 answered_by 证明"真的落库了"。
        cl2 = gw.call("confirmation_list", {"demand_id": did})
        by = next((it.get("answered_by") for it in (cl2.get("items") or [])
                   if it.get("question_id") == q0.get("question_id")), None)
        print("      答复落库：answered_by = %s ｜ version = %s" % (by, ans.get("version")))
        rec["clarify"] = {"question": q0.get("question"), "answer": answered_txt,
                          "answered_by": by}
    else:
        print("      本轮无需业务确认（问题数 0）")
        rec["clarify"] = None

    step("4/11", "结构化需求（requirement_structured）")
    rq = gw.call("requirement_structured", {"demand_id": did, "dataset": DATASET})
    rec["requirement_version"] = rq.get("requirement_version") or rq.get("version")
    print("      requirement_version = %s" % rec["requirement_version"])

    step("5/11", "SQL 计划（sql_plan）")
    pl = gw.call("sql_plan", {"demand_id": did, "dataset": DATASET})
    rec["chosen_table"] = pl.get("chosen_table")
    print("      chosen_table = %s ｜ 颗粒度 = %s ｜ 时间字段 = %s"
          % (pl.get("chosen_table"), pl.get("granularity"), pl.get("time_field")))
    print("      draft_gate = %s" % json.dumps(pl.get("draft_gate") or {}, ensure_ascii=False)[:150])

    step("6/11", "上下文包（sql_context_pack）")
    cp = gw.call("sql_context_pack", {"demand_id": did, "dataset": DATASET})
    rec["pack_version"] = cp.get("pack_version")
    print("      pack_version = %s ｜ 候选表 %d 张"
          % (cp.get("pack_version"), len(cp.get("table_candidates") or [])))

    step("7/11", "生成 SQL（sql_generate）")
    ge = gw.call("sql_generate", {"demand_id": did, "dataset": DATASET, "candidate_sql": None})
    sql_draft = ge.get("sql_draft") or ""
    rec["sql_draft"] = sql_draft
    print("      SQL = %s" % sql_draft[:200])
    print("      review.status = %s" % (ge.get("review") or {}).get("status"))

    step("8/11", "五层门禁（sql_review）")
    rv = gw.call("sql_review", {"sql": sql_draft, "dataset": DATASET})
    rec["review_status"] = rv.get("status")
    layers = rv.get("layers") or []
    print("      status = %s ｜ 层数 = %d" % (rv.get("status"), len(layers)))
    print("      各层：%s" % " / ".join("%s=%s" % (l.get("layer"), l.get("ok")) for l in layers[:6]))

    step("9/11", "只读执行（sql_execute_readonly）")
    ex = gw.call("sql_execute_readonly", {"demand_id": did, "dataset": DATASET,
                                          "sql": sql_draft, "actor": "demo-engine"})
    rec["row_count"] = ex.get("row_count")
    rec["sql_run_id"] = ex.get("sql_run_id")
    rec["adopted"] = ex.get("adopted")
    print("      row_count = %s ｜ adopted = %s ｜ sql_run_id = %s"
          % (ex.get("row_count"), ex.get("adopted"), ex.get("sql_run_id")))
    print("      样本首行 = %s" % json.dumps((ex.get("sample_rows") or [{}])[0], ensure_ascii=False)[:160])

    step("10/11", "九字段留痕（sql_run_list / sql_run_get）")
    one = None
    rl = gw.call("sql_run_list", {"demand_id": did, "limit": 5})
    items = rl.get("items") or []
    if items:
        one = items[0]
        print("      sql_run_list total = %s ｜ 首条 adopted = %s ｜ actor = %s"
              % (rl.get("total"), one.get("adopted"), one.get("actor")))

    step("11/11", "审计回放（sql_run_replay）")
    rp = gw.call("sql_run_replay", {"demand_id": did, "version": 0})
    print("      回放键：%s" % sorted((rp or {}).keys())[:12])
    print("      input.pack_version = %s ｜ gaps = %s"
          % ((rp.get("input") or {}).get("pack_version"), rp.get("gaps")))
    rec["replay_ok"] = bool(rp.get("demand_id"))
    rec["replay_gaps"] = rp.get("gaps")

    report["part1_demo"] = rec
    return did


# ---------------------------------------------------------------------------
# Part 2 · POC-1 题库复跑（经网关）
# ---------------------------------------------------------------------------
def _plan_sql(gw, q, dataset=DATASET):
    """建需求 → 前置 → 计划 → 生成；返回 (demand_id, sql_draft, review_status, plan_blocked)。"""
    d = gw.call("demand_create", {
        "title": q, "business_context": "POC-1 题库经网关复跑", "description": q,
        "expected_output": "按题库期望判定", "contact": "poc@demo.local",
    })
    did = d.get("demand_id")
    if not did:
        return None, "", "", True
    for t in ("analysis_first_round", "requirement_structured"):
        gw.call(t, {"demand_id": did, "dataset": dataset})
    pl = gw.call("sql_plan", {"demand_id": did, "dataset": dataset})
    ge = gw.call("sql_generate", {"demand_id": did, "dataset": dataset, "candidate_sql": None})
    draft = ge.get("sql_draft") or ""
    rv = (ge.get("review") or {}).get("status") or ""
    return did, draft, rv, (not draft)


def part2(gw, report, quick=False):
    hr("Part 2 · POC-1 题库复跑（bank-b.json 30 题 · 经网关）")
    bank = json.loads((HERE / "bank-b.json").read_text(encoding="utf-8"))
    qs = bank["questions"]
    if quick:
        qs = qs[:6]
    pos_pass = neg_pass = 0
    pos_n = neg_n = 0
    rows_out = []
    t0 = time.time()
    for i, q in enumerate(qs, 1):
        did, draft, rv, blocked = _plan_sql(gw, q["q"])
        judge, reason, rows = "❌", "", 0
        if q["type"] == "负向":
            neg_n += 1
            if blocked or rv == "不通过":
                judge = "✅"; neg_pass += 1; reason = "已拒绝"
            else:
                reason = "漏拒（生成了 SQL）"
        else:
            pos_n += 1
            if blocked:
                reason = "误拦截（未生成 SQL）"
            else:
                ex = gw.call("sql_execute_readonly", {"demand_id": did, "dataset": DATASET,
                                                      "sql": draft, "actor": "poc-replay"})
                rows = ex.get("row_count")
                if rows is None:
                    reason = "执行失败：%s" % str(ex.get("error"))[:60]
                elif rows > 0:
                    judge = "✅"; pos_pass += 1
                else:
                    reason = "0 行返回"
        # 额外取一次 L2 命中规则（评估「L2 升级为阻断」会不会误拦正向题）
        l2_rules = []
        if draft:
            rv2 = gw.call("sql_review", {"sql": draft, "dataset": DATASET})
            l2_rules = sorted({i.get("rule") for i in (rv2.get("semantic_issues") or []) if i.get("rule")})
        rows_out.append({"id": q["id"], "type": q["type"], "q": q["q"], "intent": q.get("intent"),
                         "sql": draft, "review": rv, "rows": rows, "judge": judge,
                         "l2_rules": l2_rules, "reason": reason})
        print("  [%s] %-3s %-6s rows=%-4s | %s | %s"
              % (judge, q["id"], q["type"], rows, q["q"][:26], reason[:40]))
    acc = 100.0 * pos_pass / max(pos_n, 1)
    rej = 100.0 * neg_pass / max(neg_n, 1)
    summary = {"正向题数": pos_n, "正向通过": pos_pass, "准确率%": round(acc, 1),
               "负向题数": neg_n, "负向拒绝": neg_pass, "拒绝率%": round(rej, 1),
               "耗时s": round(time.time() - t0, 1)}
    print("\n  小结：%s" % json.dumps(summary, ensure_ascii=False))
    print("  验收线：准确率 ≥90%%（实测 %.1f%% %s）｜ 拒绝率 100%%（实测 %.1f%% %s）"
          % (acc, "达标" if acc >= 90 else "未达标", rej, "达标" if rej >= 100 else "未达标"))
    warned = [r for r in rows_out if r["type"] == "正向" and r.get("l2_rules")]
    summary["正向题L2命中数"] = len(warned)
    print("  正向题 L2 命中（若一刀切升级为阻断，即为误拦）：%d/%d = %.0f%%"
          % (len(warned), pos_n, 100.0 * len(warned) / max(pos_n, 1)))
    for r in warned:
        print("    %s %s :: %s" % (r["id"], r["q"][:22], ",".join(r["l2_rules"])))
    report["part2_poc1"] = {"summary": summary, "records": rows_out}


# ---------------------------------------------------------------------------
# Part 3 · POC-3 陷阱用例（门禁拦截）
# ---------------------------------------------------------------------------
TRAPS = [
    ("T01", "引用未建模字段（会员手机号）",
     "SELECT member_mobile FROM dim_member"),
    ("T02", "未上架口径（按券种看核销 GMV）",
     "SELECT coupon_type, SUM(verify_gmv) AS 核销GMV FROM ads_coupon_order_di GROUP BY coupon_type"),
    ("T03", "错口径映射（用支付金额冒充核销 GMV）",
     "SELECT coupon_type, SUM(pay_amount) AS 核销GMV FROM ads_coupon_order_di GROUP BY coupon_type"),
    ("T04", "聚合缺 GROUP BY",
     "SELECT store_id, SUM(pay_amount) FROM dwd_order_di"),
    ("T05", "一对多未定颗粒度（会员⋈订单直接 COUNT 会员）",
     "SELECT COUNT(m.member_id) AS 会员数 FROM dim_member m "
     "JOIN dwd_order_di o ON m.member_id = o.member_id"),
    ("T06", "装载时间冒充业务日期",
     "SELECT COUNT(*) FROM dwd_order_di WHERE load_time = '2026-09-01'"),
    ("T07", "跨域无关系 JOIN（订单 ⋈ 券核销，无关系路径）",
     "SELECT COUNT(*) FROM dwd_order_di o "
     "JOIN ads_coupon_order_di c ON o.order_id = c.coupon_order_id"),
    ("T08", "枚举值错误（region_name=西南大区）",
     "SELECT store_name FROM dim_store WHERE region_name = '西南大区'"),
    ("T09", "敏感字段泄露（身份证号）",
     "SELECT member_id_card FROM dim_member"),
    ("T10", "引用已下架 / 不存在对象",
     "SELECT SUM(pay_amount) FROM dwd_order_di_archived"),
]


def part3(gw, report):
    hr("Part 3 · POC-3 dry-plan 拦截效果（10 条陷阱用例 · 走五层门禁）")
    hits = 0
    rows_out = []
    for tid, desc, sql in TRAPS:
        t0 = time.time()
        rv = gw.call("sql_review", {"sql": sql, "dataset": DATASET})
        ms = int((time.time() - t0) * 1000)
        status = rv.get("status")
        blocked = (status == "不通过")
        if blocked:
            hits += 1
        why = []
        for l in (rv.get("layers") or []):
            if not l.get("ok") and not l.get("skipped"):
                why.append("%s:%s" % (l.get("layer"), l.get("name")))
        for r in (rv.get("rule_violations") or [])[:2]:
            why.append(str(r.get("rule_id")))
        rows_out.append({"id": tid, "desc": desc, "sql": sql, "status": status,
                         "blocked": blocked, "why": why[:3], "ms": ms})
        print("  [%s] %-3s %-8s %sms | %s | %s"
              % ("拦截" if blocked else "放行", tid, status or "-", ms, desc[:30],
                 "；".join(why[:2])[:50]))
    rate = 100.0 * hits / len(TRAPS)
    avg_ms = sum(r["ms"] for r in rows_out) / max(len(rows_out), 1)
    print("\n  小结：拦截 %d/%d = %.1f%%（验收线 100%%）；单次 review 平均 %dms（验收线 ≤500ms）"
          % (hits, len(TRAPS), rate, int(avg_ms)))
    report["part3_poc3"] = {"traps": len(TRAPS), "blocked": hits, "拦截率%": round(rate, 1),
                            "平均review_ms": int(avg_ms), "records": rows_out}


# ---------------------------------------------------------------------------
# Part 4 · POC-2 MDL 建模事实
# ---------------------------------------------------------------------------
def part4(report):
    hr("Part 4 · POC-2 核心表 MDL 建模事实核对")
    mdl = json.loads((ROOT / "wren-docker-b" / "workspace" / "mdl.json").read_text(encoding="utf-8"))
    models = mdl.get("models") or []
    cols = sum(len(m.get("columns") or []) for m in models)
    rels = mdl.get("relationships") or []
    gen = ROOT / "wren-docker-b" / "gen_mdl_b.py"
    fact = {"模型数": len(models), "字段数": cols, "关系数": len(rels),
            "脚本化生成": gen.exists(),
            "模型清单": [m.get("name") for m in models]}
    print("  模型 %d 个 ｜ 字段 %d 个 ｜ 关系 %d 条 ｜ 脚本化生成=%s"
          % (fact["模型数"], fact["字段数"], fact["关系数"], fact["脚本化生成"]))
    print("  模型：%s" % ", ".join(fact["模型清单"]))
    print("  口径：PRD §17.3 要求 8 表合计 ≤12 人天、单表 ≤2 人天；本库由 gen_mdl_b.py 一次脚本化生成。")
    report["part4_poc2"] = fact


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mcp", default=DEFAULT_MCP)
    ap.add_argument("--quick", action="store_true", help="题库只跑前 6 题（冒烟）")
    args = ap.parse_args()

    gw = Gw(args.mcp)
    gw.connect()
    hr("POC 端到端演练 · 经自研网关 %s" % args.mcp)

    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "mcp": args.mcp, "dataset": DATASET}

    # Part 0 · 探活
    print("  探活：healthz ...")
    try:
        with urllib.request.urlopen(args.mcp.replace("/mcp", "/healthz"), timeout=10) as r:
            hz = json.loads(r.read().decode())
        report["healthz"] = hz
        env = hz.get("env") or {}
        print("  网关 %s ｜ 版本 %s ｜ 数据集 %s"
              % (hz.get("status"), hz.get("version"), env.get("datasets_registered")))
    except Exception as e:  # noqa: BLE001
        print("  healthz 不可达：%s" % e)

    part1(gw, report)
    part2(gw, report, quick=args.quick)
    part3(gw, report)
    part4(report)

    out = HERE / "e2e-gateway-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    hr("演练完成 · 报告已写入 %s" % out.name)
    p2 = report.get("part2_poc1", {}).get("summary", {})
    p3 = report.get("part3_poc3", {})
    p4 = report.get("part4_poc2", {})
    print("  POC-1 准确率 %s%% ｜ 拒绝率 %s%%" % (p2.get("准确率%"), p2.get("拒绝率%")))
    print("  POC-3 拦截率 %s%% ｜ 平均 review %sms" % (p3.get("拦截率%"), p3.get("平均review_ms")))
    print("  POC-2 MDL %s 模型 / %s 字段 / %s 关系"
          % (p4.get("模型数"), p4.get("字段数"), p4.get("关系数")))


if __name__ == "__main__":
    main()
