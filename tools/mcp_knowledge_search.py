#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通过真实 MCP 端点调用 knowledge_search（B4b 验收用）。

用法:
  $PY tools/mcp_knowledge_search.py "复购率怎么算"
  $PY tools/mcp_knowledge_search.py --loop 10 "复购率怎么算"   # 连跑 N 次统计稳定性
  $PY tools/mcp_knowledge_search.py --m2                    # 跑 m2_verify 的三问
"""
import json
import sys
import time
import urllib.request

URL = "http://127.0.0.1:18080/mcp"


class MCP:
    def __init__(self, url=URL):
        self.url = url
        self.sid = None
        self.rid = 0

    def _post(self, payload):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=h, method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as r:
            got = r.headers.get("Mcp-Session-Id")
            if got:
                self.sid = got
            raw = r.read().decode("utf-8", "replace")
        for line in raw.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                return json.loads(body, strict=False) if body else None
        return json.loads(raw, strict=False) if raw.strip() else None

    def init(self):
        self._post(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "b4b", "version": "0"},
                },
            }
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name, args):
        self.rid += 1
        r = self._post(
            {
                "jsonrpc": "2.0",
                "id": self.rid,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        return r


def unwrap(resp):
    """把 MCP result 解成 dict（content[0].text 是 JSON 串）。"""
    if resp.get("error"):
        return {"__mcp_error__": resp["error"]}
    res = resp.get("result") or {}
    if res.get("isError"):
        texts = [c.get("text", "") for c in (res.get("content") or []) if c.get("type") == "text"]
        return {"__tool_error__": "\n".join(texts)}
    for c in res.get("content") or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"], strict=False)
            except Exception:
                return {"__text__": c["text"]}
    return res


def one(question, top_k=5, threshold=0.1, vector_weight=0.7):
    """走真实 MCP 端点调 knowledge_search。

    入参名严格取自 gateway/app.py:366 的签名（question/top_k/threshold/
    vector_weight/demand_id/round_no）——**没有 dataset_ids**，数据集由服务端
    KNOWLEDGE_DATASET_ID 环境变量决定。
    """
    m = MCP()
    m.init()
    t0 = time.time()
    resp = m.call(
        "knowledge_search",
        {
            "question": question,
            "top_k": top_k,
            "threshold": threshold,
            "vector_weight": vector_weight,
        },
    )
    ms = int((time.time() - t0) * 1000)
    return unwrap(resp), ms


def brief(d, ms):
    if "__mcp_error__" in d or "__tool_error__" in d or "__text__" in d:
        return "  ERR(mcp): %s" % json.dumps(d, ensure_ascii=False)[:300]
    if d.get("ok") is False:
        return "  ERR(app): %s" % str(d.get("error"))[:300]
    cites = d.get("citations") or []
    lines = [
        "  total=%s  elapsed_ms=%s  wall_ms=%d  fallback_query=%r"
        % (d.get("total"), d.get("elapsed_ms"), ms, d.get("fallback_query"))
    ]
    for c in cites:
        lines.append(
            "  [%d] sim=%s vec=%s term=%s  《%s》 chunk=%s"
            % (
                c.get("n"),
                c.get("similarity"),
                c.get("vector_similarity"),
                c.get("term_similarity"),
                c.get("document_name"),
                str(c.get("chunk_id"))[:12],
            )
        )
        lines.append("       %s" % (c.get("snippet") or "")[:110].replace("\n", " "))
    if not cites:
        lines.append("  (0 条命中)")
    return "\n".join(lines)


def main():
    args = sys.argv[1:]
    loop = 1
    if args and args[0] == "--loop":
        loop = int(args[1])
        args = args[2:]
    if args and args[0] == "--m2":
        qs = ["复购率怎么算", "门店坪效口径", "敏感字段怎么处理"]
        n = loop if loop > 1 else 1
        for i in range(n):
            print("=== ROUND %d/%d ===" % (i + 1, n))
            for q in qs:
                d, ms = one(q)
                print("[Q] %s" % q)
                print(brief(d, ms))
        return
    for q in args:
        d, ms = one(q)
        print("[Q] %s" % q)
        print(brief(d, ms))


if __name__ == "__main__":
    main()
