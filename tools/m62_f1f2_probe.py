# -*- coding: utf-8 -*-
"""M6-2 补充核查：F1/F2 在**真实链路**里能否触发（交付方自证用硬编码 classify 代替了真链路）。

- F1（语义定型 / 主体颗粒度不清）：走 analysis.first_round → 看 ambiguity_points
- F2（Schema 筛选 / 找不到明确主题表）：走 sqlgen.plan → 看 draft_gate.status

只读式核查：打印真实输出，不判 PASS/FAIL（人来判读）。
用法：python tools/m62_f1f2_probe.py [--url ...]
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
        res, sid = self._rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                         "clientInfo": {"name": "probe", "version": "1.0.0"}}})
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
            res, sid = self._rpc({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                                  "params": {"name": tool, "arguments": args or {}}}, self._sid)
            if sid:
                self._sid = sid
            if res is None:
                return None
            if "error" in res:
                return {"_rpc_error": res["error"]}
            out = res.get("result", {})
            try:
                texts = [c.get("text", "") for c in (out.get("content") or []) if c.get("type") == "text"]
                if texts:
                    return json.loads(texts[0])
            except Exception:
                pass
            return out


def mk(c, title, desc, out):
    return c.call("demand_create", {
        "title": title, "business_context": "F1/F2 真链路核查（验收方）",
        "description": desc, "expected_output": out, "contact": "验收方-13900000000",
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18080/mcp")
    a = ap.parse_args()
    c = McpClient(a.url)

    print("=" * 72)
    print("F1/F2 真实链路核查")
    print("=" * 72)

    # ---------- F1：语义定型（把限制条件拿掉当"不清楚主体"） ----------
    print("\n【F1】analysis.first_round（看 ambiguity_points）")
    d1 = mk(c, "经营看板", "帮我做一张看板", "随便看看")
    id1 = (d1 or {}).get("demand_id")
    print("  需求=%s（title=经营看板 / desc=帮我做一张看板）" % id1)
    a1 = c.call("analysis_first_round", {"demand_id": id1, "dataset": "B"})
    amb = (a1 or {}).get("ambiguity_points") or (a1 or {}).get("questions") or []
    print("  ambiguity_points 条数 =", len(amb) if isinstance(amb, list) else amb)
    print("  内容 =", json.dumps(amb, ensure_ascii=False)[:600] if amb else "(空)")
    print("  返回键 =", sorted((a1 or {}).keys()))

    # ---------- F2：Schema 筛选（A 库候选空 / B 库模糊） ----------
    print("\n【F2】sqlgen.plan（看 draft_gate.status）")
    d2 = mk(c, "上个季度大客户的复购情况怎么样", "看看大客户复购", "一张表")
    id2 = (d2 or {}).get("demand_id")
    for ds in ("B", "A"):
        p = c.call("sql_plan", {"demand_id": id2, "dataset": ds})
        dg = (p or {}).get("draft_gate") or {}
        cands = (p or {}).get("table_candidates") or []
        print("  dataset=%s：draft_gate.status=%r  候选数=%d  chosen_table=%r" % (
            ds, dg.get("status"), len(cands), (p or {}).get("chosen_table")))
        print("    draft_gate=%s" % json.dumps(dg, ensure_ascii=False)[:400])


if __name__ == "__main__":
    main()
