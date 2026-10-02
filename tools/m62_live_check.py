# -*- coding: utf-8 -*-
"""M6-2 活链路检查（验收方）——走 MCP 真调，验证 F4/F5 新字段在真实链路上透出。

交付方自证只测了 fallback.py 纯函数 + sqlgen.generate（进程内）；
本脚本补的是**端到端**：真起需求单 → 真过五层门禁 → 真执行 → 看返回是否带
deliverable / deliverable_reason / adopted / fallback{kind:F4, guidance:[...]}。

用法：python tools/m62_live_check.py [--url http://127.0.0.1:18080/mcp]
"""
import argparse
import json
import threading
import urllib.request

PROTOCOL_VERSION = "2025-03-26"


class McpClient:
    def __init__(self, url, timeout=180):
        self.endpoint = url
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._sid = None
        self._lock = threading.Lock()
        self._id = 0

    def _rpc(self, payload, sid=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(), headers=headers)
        resp = self._opener.open(req, timeout=self.timeout)
        new_sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode()
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), new_sid
        return None, new_sid

    def handshake(self):
        res, sid = self._rpc({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": "m62-live", "version": "1.0.0"}},
        })
        self._sid = sid
        if sid:
            try:
                self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except Exception:
                pass
        return res

    def call(self, tool, args=None):
        with self._lock:
            if self._sid is None:
                self.handshake()
            self._id += 1
            res, sid = self._rpc({
                "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}},
            }, self._sid)
            if sid:
                self._sid = sid
            if res is None:
                return None
            if "error" in res:
                return {"_rpc_error": res["error"]}
            out = res.get("result", {})
            # tools/call 结果：content[0].text 里是 JSON
            try:
                texts = [c.get("text", "") for c in (out.get("content") or []) if c.get("type") == "text"]
                if texts:
                    return json.loads(texts[0])
            except Exception:
                pass
            return out


FAILS = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    print("  [%s] %s %s" % ("OK " if cond else "FAIL", name, ("  -> " + str(detail)[:260] if detail else "")))
    if not cond:
        FAILS.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18080/mcp")
    args = ap.parse_args()
    c = McpClient(args.url)

    print("=" * 72)
    print("M6-2 活链路检查（走 MCP）")
    print("=" * 72)

    # 1) 建需求单
    d = c.call("demand_create", {
        "title": "验证 F4/F5 回退字段透出",
        "business_context": "验收方活链路检查，非业务用例",
        "description": "统计各门店当月销售额，按门店分组",
        "expected_output": "门店 + 销售额",
        "contact": "验收方-13900000000",
    })
    did = (d or {}).get("demand_id") or (d or {}).get("id")
    ck("L1 需求单创建成功（拿到 demand_id）", bool(did), "demand_id=%s" % did)
    if not did:
        print("  无法继续（无 demand_id）")
        return _finish()

    bad_sql = "SELECT * FROM dws_store_daily_agg a, dim_store b"
    good_sql = ("SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg "
                "WHERE stat_date = '2025-09-01' GROUP BY store_id")

    # 2) sql_review 真调：坏 SQL 应 不通过
    rev = c.call("sql_review", {"sql": bad_sql, "dataset": "B"})
    ck("L2 sql_review(坏 SQL) status=不通过", (rev or {}).get("status") == "不通过",
       "status=%s" % ((rev or {}).get("status")))

    # 3) sql_execute_readonly 坏 SQL → 阻断 + F4 fallback 块
    r_bad = c.call("sql_execute_readonly", {"demand_id": did, "dataset": "B", "sql": bad_sql})
    fb = (r_bad or {}).get("fallback") or {}
    gd = fb.get("guidance") or []
    ck("L3a 坏 SQL 被预审查阻断（ok=False, blocked_by=pre_review）",
       (r_bad or {}).get("ok") is False and (r_bad or {}).get("blocked_by") == "pre_review",
       "ok=%s blocked_by=%s" % ((r_bad or {}).get("ok"), (r_bad or {}).get("blocked_by")))
    ck("L3b fallback 块 kind=F4", fb.get("kind") == "F4", "kind=%s" % fb.get("kind"))
    ck("L3c fallback.guidance 非空且每条含 rule/why/how_to_fix",
       len(gd) >= 1 and all(isinstance(x, dict) and x.get("rule") and x.get("how_to_fix") for x in gd),
       "guidance=%s" % json.dumps(gd, ensure_ascii=False)[:300])

    # 4) sql_execute_readonly 好 SQL → 成功包带 deliverable/adopted
    r_ok = c.call("sql_execute_readonly", {"demand_id": did, "dataset": "B", "sql": good_sql})
    ck("L4a 好 SQL 执行成功（有 row_count）", isinstance((r_ok or {}).get("row_count"), int),
       "row_count=%s err=%s" % ((r_ok or {}).get("row_count"), (r_ok or {}).get("error")))
    ck("L4b 成功包含新增键 deliverable", "deliverable" in (r_ok or {}), "keys=%s" % sorted((r_ok or {}).keys()))
    ck("L4c 成功包含新增键 deliverable_reason", "deliverable_reason" in (r_ok or {}),
       "reason=%r" % (r_ok or {}).get("deliverable_reason"))
    ck("L4d 成功包含新增键 adopted", "adopted" in (r_ok or {}), "adopted=%s" % (r_ok or {}).get("adopted"))
    # 旧九字段仍在
    for k in ("execution_summary", "row_count", "sample_rows", "validation_result",
              "suspicious_signals", "review", "sql_run_id"):
        ck("L4e 旧键仍在：%s" % k, k in (r_ok or {}))

    # 5) MCP 面多候选（list）→ F3 hold 真链路（接线层补齐后应可达）
    r_multi = c.call("sql_generate", {"demand_id": did, "dataset": "B", "candidate_sql": [good_sql, bad_sql]})
    if isinstance(r_multi, dict) and r_multi.get("_rpc_error"):
        ck("L5 MCP 面支持多候选 list（F3 hold 可达）", False, "RPC 错误：%s" % r_multi.get("_rpc_error"))
    elif not isinstance(r_multi, dict) or "content" in (r_multi or {}):
        ck("L5 MCP 面支持多候选 list（F3 hold 可达）", False,
           "工具返回校验错误：%s" % json.dumps(r_multi, ensure_ascii=False)[:220])
    else:
        ck("L5 MCP 面支持多候选 list（F3 hold 可达）", True, "keys=%s" % sorted(r_multi.keys()))
        ck("L5b 大差异两候选 → hold=True 且 sql_draft 置空",
           r_multi.get("hold") is True and (r_multi.get("sql_draft") or "") == "",
           "hold=%s draft=%r" % (r_multi.get("hold"), r_multi.get("sql_draft")))
        ck("L5c hold_reason 含「差异过大」", "差异过大" in (r_multi.get("hold_reason") or ""),
           (r_multi.get("hold_reason") or "")[:80])

    return _finish()


def _finish():
    print("\n" + "=" * 72)
    print("活链路检查：用例 %d 个，硬失败 %d 个" % (N[0], len(FAILS)))
    if FAILS:
        for x in FAILS:
            print("   - " + x)
        print("未通过")
        raise SystemExit(1)
    print("通过")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
