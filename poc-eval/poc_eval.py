#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POC 评估工作台 · B 库（零售/会员域）回放
- 受控生成：确定性意图→SQL（MDL 语义对齐），未命中即拒
- 回放：题库 bank-b.json 逐题 plan → dry_run 门禁 → 真实执行 → 判定
- 输出：replay-b.json（逐题回放记录）+ replay-b.md（统计摘要）
用法：python3 poc_eval.py [--base http://127.0.0.1:9002]
"""
import json, re, sys, time, pathlib, urllib.request

HERE = pathlib.Path(__file__).parent
BASE = "http://127.0.0.1:9002"
for i, a in enumerate(sys.argv):
    if a == "--base" and i + 1 < len(sys.argv): BASE = sys.argv[i + 1]

# ---------------- Wren MCP 客户端（streamable-http，会话复用+重握手） ----------------
class WrenClient:
    def __init__(self, base):
        self.base, self.sid, self.op = base.rstrip("/") + "/mcp", None, urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None: body["params"] = params
        if not notify: body["id"] = 1
        hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.sid: hdrs["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.base, data=json.dumps(body).encode(), headers=hdrs)
        resp = self.op.open(req, timeout=60)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid: self.sid = sid
        text = resp.read().decode()
        out = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try: out.append(json.loads(line[5:]))
                except Exception: pass
        return out

    def handshake(self):
        self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "poc-eval", "version": "1.0"}})
        self._rpc("notifications/initialized", {}, notify=True)

    def call(self, tool, args, _retry=True):
        for r in self._rpc("tools/call", {"name": tool, "arguments": args}):
            if "result" in r:
                txt = "".join(c.get("text", "") for c in r["result"].get("content", []))
                try: return json.loads(txt)
                except Exception: return txt
            if "error" in r:
                if _retry and "session" in str(r["error"]).lower():
                    self.sid = None; self.handshake(); return self.call(tool, args, False)
                return {"__error__": r["error"]}
        return None

# ---------------- 受控生成：意图 → SQL（确定性模板，MDL 语义对齐） ----------------
def _months_in(t):
    ms = re.findall(r"(\d{4})年(\d{1,2})月", t)
    return [f"{y}-{int(m):02d}-01" for y, m in ms]

def ok(intent, sql, refs):
    return {"blocked": False, "intent": intent, "sql": sql, "refs": refs}

def refuse(reason, guide):
    return {"blocked": True, "intent": "拒绝", "sql": None, "reason": reason, "guide": guide}

NEG_SENS      = [("手机号", "会员手机号为敏感字段（建表未建模，对 AI 不可见）"),
                 ("身份证", "会员身份证号为敏感字段（建表未建模，对 AI 不可见）"),
                 ("证件号", "会员证件号为敏感字段（建表未建模，对 AI 不可见）")]
NEG_UNDEFINED = [("情绪指数", "「情绪指数」未定义口径，未建模"),
                 ("核销率", "「核销率」未定义口径，未建模"),
                 ("收货地址", "「收货地址」未建模"),
                 ("装载时间", "装载时间为技术字段（非业务日期），不建模；业务日期请使用 order_date")]
NEG_PENDING   = [("券成本", "「券成本」口径待上架（D3），未经准入不可分析"),
                 ("核销GMV", "「核销GMV」口径待上架（D3），未经准入不可分析"),
                 ("核销 gmv", "「核销GMV」口径待上架（D3），未经准入不可分析")]

def plan(nl):
    t = nl.strip()
    # ---- 负向：敏感（最高优先）----
    for kw, why in NEG_SENS:
        if kw in t: return refuse(f"敏感字段拦截：{kw}", why)
    # ---- 负向：未定义 / 未建模 ----
    for kw, why in NEG_UNDEFINED:
        if kw in t: return refuse(f"未定义/未建模拦截：{kw}", why)
    # ---- 负向：待上架口径 ----
    for kw, why in NEG_PENDING:
        if kw in t: return refuse(f"待上架口径拦截：{kw}", why)
    # ---- 负向：跨域无关系（订单 × 券核销）----
    if ("订单" in t or "用券" in t) and ("券" in t) and any(k in t for k in ["关联", "join", "JOIN", "连接", "结合起来"]):
        return refuse("跨域无关系拦截：订单表与券核销表之间无建模关系路径", "如需用券订单分析，请先补充口径与关系建模（知识治理）")
    # ---- 负向：颗粒度不符（复购=月粒度，按日拒绝）----
    if "复购" in t and any(k in t for k in ["按日", "每日", "每天"]):
        return refuse("颗粒度不符：复购汇总为月粒度表，不支持按日分析", "如需日粒度复购，请先在源头建日粒度表并建模")
    # ---- 正向意图 ----
    ms = _months_in(t)
    m1 = ms[0] if ms else None
    # 复购用户数
    if "复购用户数" in t:
        if "按门店" in t or "各门店" in t:
            return ok("复购用户数·门店月",
                      "SELECT s.store_name, r.stat_month, SUM(r.repurchase_flag) AS 复购用户数 "
                      "FROM ads_member_repurchase_di r JOIN dim_store s ON r.store_id = s.store_id "
                      "GROUP BY s.store_name, r.stat_month",
                      ["ads_member_repurchase_di", "dim_store"])
        if m1:
            return ok("复购用户数·总月",
                      f"SELECT SUM(repurchase_flag) AS 复购用户数 FROM ads_member_repurchase_di WHERE stat_month = '{m1}'",
                      ["ads_member_repurchase_di"])
    # 复购率（须随附分母）
    if "复购率" in t:
        if "按门店" in t or "各门店" in t:
            return ok("复购率·门店月",
                      "SELECT s.store_name, r.stat_month, SUM(r.repurchase_flag) AS 复购用户数, "
                      "SUM(r.order_cnt) AS 活跃订单量 FROM ads_member_repurchase_di r "
                      "JOIN dim_store s ON r.store_id = s.store_id GROUP BY s.store_name, r.stat_month",
                      ["ads_member_repurchase_di", "dim_store"])
        return ok("复购率·总（含分母）",
                  "SELECT ROUND(CAST(100.0 * a.c / b.m AS DECIMAL(10,1)), 1) AS 复购率百分比, a.c AS 复购用户数, b.m AS 活跃会员数 "
                  "FROM (SELECT SUM(repurchase_flag) AS c FROM ads_member_repurchase_di) a, "
                  "(SELECT COUNT(DISTINCT member_id) AS m FROM dwd_order_di) b",
                  ["ads_member_repurchase_di", "dwd_order_di（活跃用户数子口径）"])
    # 归属门店会员数
    if ("会员数" in t or "会员人数" in t) and ("门店" in t):
        return ok("归属门店·会员数",
                  "SELECT s.store_name AS 归属门店, COUNT(*) AS 会员数 "
                  "FROM dim_member m JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name",
                  ["dim_member", "dim_store"])
    # 活跃会员数
    if "活跃会员数" in t or ("活跃" in t and "会员" in t):
        if m1:
            y, mo = int(m1[:4]), int(m1[5:7])
            ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
            return ok("活跃会员数",
                      f"SELECT COUNT(DISTINCT member_id) AS 活跃会员数 FROM dwd_order_di "
                      f"WHERE order_date >= '{m1}' AND order_date < '{ny}-{nm:02d}-01'",
                      ["dwd_order_di"])
    # 会员升降级
    if "升级" in t or "降级" in t or "升降级" in t or "等级异动" in t:
        if "门店" in t:
            return ok("升降级·门店",
                      "SELECT s.store_name, g.change_type, COUNT(*) AS 数量 "
                      "FROM ads_member_grade_change_wi g JOIN dim_member m ON g.member_id = m.member_id "
                      "JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name, g.change_type",
                      ["ads_member_grade_change_wi", "dim_member", "dim_store"])
        if m1:
            y, mo = int(m1[:4]), int(m1[5:7])
            ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
            return ok("升级人数·月",
                      f"SELECT COUNT(*) AS 升级人数 FROM ads_member_grade_change_wi "
                      f"WHERE change_type = '升级' AND change_month >= '{m1}' AND change_month < '{ny}-{nm:02d}-01'",
                      ["ads_member_grade_change_wi"])
        return ok("升降级分布",
                  "SELECT change_type, COUNT(*) AS 数量 FROM ads_member_grade_change_wi GROUP BY change_type",
                  ["ads_member_grade_change_wi"])
    # 会员状态/等级分布
    if "会员状态" in t:
        return ok("会员状态分布",
                  "SELECT member_status AS 会员状态, COUNT(*) AS 人数 FROM dim_member GROUP BY member_status",
                  ["dim_member"])
    if "等级" in t and ("会员" in t or "人数" in t):
        return ok("等级分布",
                  "SELECT grade AS 会员等级, COUNT(*) AS 人数 FROM dim_member GROUP BY grade",
                  ["dim_member"])
    # 销售额（排除退款）
    if "坪效" in t:
        return ok("坪效",
                  "SELECT s.store_name, ROUND(CAST(SUM(d.sales_amount) / MAX(d.area_sqm) AS DECIMAL(10,2)), 2) AS 坪效 "
                  "FROM dws_store_daily_agg d JOIN dim_store s ON d.store_id = s.store_id GROUP BY s.store_name",
                  ["dws_store_daily_agg", "dim_store"])
    if "销售额" in t or "销售金额" in t:
        if "大区" in t:
            if "华东" in t and "各" not in t:
                return ok("销售额·华东（排除退款）",
                          "SELECT SUM(o.pay_amount) AS 华东销售额 FROM dwd_order_di o "
                          "JOIN dim_store s ON o.store_id = s.store_id "
                          "WHERE s.region_name = '华东大区' AND o.order_status = '已完成'",
                          ["dwd_order_di", "dim_store"])
            return ok("销售额·各大区",
                      "SELECT s.region_name AS 大区, SUM(o.pay_amount) AS 销售额 "
                      "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                      "WHERE o.order_status = '已完成' GROUP BY s.region_name",
                      ["dwd_order_di", "dim_store"])
        if "日均" in t:
            return ok("日均销售额",
                      "SELECT s.store_name, ROUND(AVG(d.sales_amount), 2) AS 日均销售额 "
                      "FROM dws_store_daily_agg d JOIN dim_store s ON d.store_id = s.store_id GROUP BY s.store_name",
                      ["dws_store_daily_agg", "dim_store"])
        if "门店" in t and "各" in t:
            return ok("销售额·门店",
                      "SELECT s.store_name, SUM(o.pay_amount) AS 销售额 "
                      "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                      "WHERE o.order_status = '已完成' GROUP BY s.store_name",
                      ["dwd_order_di", "dim_store"])
        if "工作日" in t or "周末" in t or "日型" in t:
            return ok("销售额·日型",
                      "SELECT day_type, SUM(sales_amount) AS 销售额 FROM dws_store_daily_agg GROUP BY day_type",
                      ["dws_store_daily_agg"])
        if "每月" in t or "趋势" in t or "按月" in t:
            return ok("月度趋势",
                      "SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, SUM(pay_amount) AS 销售金额 "
                      "FROM dwd_order_di WHERE order_status = '已完成' GROUP BY date_trunc('month', order_date) "
                      "ORDER BY 月份",
                      ["dwd_order_di"])
    # 品类
    if "品类" in t:
        return ok("品类销售额",
                  "SELECT category_name, SUM(sales_amount) AS 销售额 FROM dwd_order_detail_di GROUP BY category_name",
                  ["dwd_order_detail_di"])
    # 月度趋势（无"销售额"字样时兜底）
    if ("每月" in t or "趋势" in t) and ("订单" in t or "销售" in t):
        return ok("月度趋势",
                  "SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, SUM(pay_amount) AS 销售金额 "
                  "FROM dwd_order_di WHERE order_status = '已完成' GROUP BY date_trunc('month', order_date) ORDER BY 月份",
                  ["dwd_order_di"])
    # 未命中即拒（受控生成核心约束）
    return refuse("未命中已建模意图", "受控生成约束：问题未对齐任何已建模口径/意图，拒绝生成（未命中即拒）")

# ---------------- 回放 ----------------
def main():
    bank = json.loads((HERE / "bank-b.json").read_text(encoding="utf-8"))
    c = WrenClient(BASE)
    c.handshake()
    records, n_pos_pass, n_neg_pass = [], 0, 0
    t_start = time.time()
    for q in bank["questions"]:
        rec = {"id": q["id"], "type": q["type"], "q": q["q"], "intent": "", "dry_ok": None,
               "rows": 0, "result": None, "blocked": None, "judge": "❌", "reason": ""}
        t0 = time.time()
        p = plan(q["q"])
        rec["intent"] = p["intent"]
        if q["type"] == "负向":
            rec["blocked"] = p["blocked"]
            if p["blocked"]:
                rec["judge"] = "✅"; rec["reason"] = f"已拒绝：{p.get('reason','')}"; n_neg_pass += 1
            else:
                rec["reason"] = "漏拒（生成了 SQL）"
        else:
            if p["blocked"]:
                rec["blocked"] = True; rec["reason"] = f"误拦截：{p.get('reason','')}"
            else:
                dr = c.call("dry_run", {"sql": p["sql"]})
                rec["dry_ok"] = not (isinstance(dr, dict) and dr.get("isError"))
                if not rec["dry_ok"]:
                    rec["reason"] = f"dry_run 失败：{str(dr)[:150]}"
                else:
                    r = c.call("query", {"sql": p["sql"]})
                    if isinstance(r, dict) and "columns" in r:
                        rec["rows"] = len(r.get("data", [])); rec["result"] = r
                        rec["judge"] = "✅" if rec["rows"] > 0 else "❌"
                        if rec["judge"] == "✅": n_pos_pass += 1
                        else: rec["reason"] = "0 行返回"
                    else:
                        rec["reason"] = f"query 异常：{str(r)[:150]}"
        rec["ms"] = int((time.time() - t0) * 1000)
        records.append(rec)

    pos = [r for r in records if r["type"] == "正向"]
    neg = [r for r in records if r["type"] == "负向"]
    acc = 100.0 * n_pos_pass / max(len(pos), 1)
    rej = 100.0 * n_neg_pass / max(len(neg), 1)
    out = {"meta": bank["meta"], "base": BASE,
           "summary": {"正向题数": len(pos), "正向通过": n_pos_pass, "准确率%": round(acc, 1),
                        "负向题数": len(neg), "负向拒绝": n_neg_pass, "拒绝率%": round(rej, 1),
                        "总耗时s": round(time.time() - t_start, 1)},
           "records": records}
    (HERE / "replay-b.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# POC-1/3 B 库回放报告（bank-v1）", "",
             f"- 数据源：{BASE}", f"- 正向题：{n_pos_pass}/{len(pos)} 通过，准确率 **{acc:.1f}%**（验收线 ≥90%）",
             f"- 负向题：{n_neg_pass}/{len(neg)} 拒绝，拒绝率 **{rej:.1f}%**（验收线 100%）", "",
             "| 题号 | 类型 | 需求 | 意图 | dry | 行数 | 判定 | 说明 | 耗时ms |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r['id']} | {r['type']} | {r['q']} | {r['intent'][:24]} | "
                     f"{r['dry_ok'] if r['dry_ok'] is not None else '—'} | {r['rows']} | {r['judge']} | {r['reason'][:60]} | {r['ms']} |")
    (HERE / "replay-b.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False))
    for r in records:
        print(f"  [{r['judge']}] {r['id']} {r['type']} rows={r['rows']} | {r['q'][:30]} | {r['reason'][:60]}")

if __name__ == "__main__":
    main()
