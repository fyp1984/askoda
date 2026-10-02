#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关双探活脚本

M1 验收标准：`docker compose up -d` 后 `/healthz` 与应用 MCP `initialize` 均返回 200。

两道探活：
  1. HTTP 探活  GET /healthz                        —— 网关自检 + 各数据集 Wren 连通性
  2. MCP 探活   POST /mcp  initialize + tools/list  —— 应用 MCP 协议握手与工具面可见

用法：
    python healthcheck.py [--base http://127.0.0.1:18080] [--deep]
退出码：0 = 两道全过；1 = 任一失败
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

PASS, FAIL = "✅", "❌"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_get(op, url):
    try:
        resp = op.open(url, timeout=30)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def mcp_rpc(op, endpoint, payload, sid=None, timeout=30):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers)
    resp = op.open(req, timeout=timeout)
    new_sid = resp.headers.get("Mcp-Session-Id")
    body = resp.read().decode()
    out = []
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except Exception:
                pass
    if not out:
        # 部分实现直接返回 JSON（非 SSE）
        try:
            return resp.status, json.loads(body), new_sid
        except Exception:
            return resp.status, None, new_sid
    return resp.status, out[0], new_sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080", help="网关地址")
    ap.add_argument("--deep", action="store_true", help="额外调用一次 gateway_health 工具")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    endpoint = base + "/mcp"
    op = _opener()
    failures = []

    print("=" * 72)
    print("网关双探活  目标：%s" % base)
    print("=" * 72)

    # ---- 探活 1：HTTP /healthz ----
    print("\n[1/2] HTTP 探活  GET /healthz")
    try:
        code, body = http_get(op, base + "/healthz")
        print("      HTTP %s" % code)
        if code != 200:
            failures.append("healthz 返回 %s（期望 200）" % code)
            print("      %s 状态码不符" % FAIL)
        else:
            data = json.loads(body)
            print("      status = %s | version = %s" % (data.get("status"), data.get("version")))
            for ds in data.get("datasets", []):
                flag = PASS if ds.get("ok") else FAIL
                detail = ds.get("response") or ds.get("error", "")
                print("      %s 数据集 %s  %s  %sms  %s"
                      % (flag, ds.get("dataset"), ds.get("endpoint"), ds.get("ms"), detail[:70]))
            if data.get("status") != "ok":
                failures.append("healthz status = %s（期望 ok）" % data.get("status"))
            print("      %s HTTP 探活通过" % (PASS if not failures else FAIL))
    except Exception as e:
        failures.append("healthz 不可达：%s: %s" % (type(e).__name__, e))
        print("      %s 不可达：%s" % (FAIL, e))

    # ---- 探活 2：MCP initialize + tools/list ----
    print("\n[2/2] MCP 探活  POST /mcp  initialize → notifications/initialized → tools/list")
    try:
        code, res, sid = mcp_rpc(
            op,
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "healthcheck", "version": "1.0"},
                },
            },
        )
        server = (res or {}).get("result", {}).get("serverInfo", {})
        print("      HTTP %s | serverInfo = %s %s"
              % (code, server.get("name", "?"), server.get("version", "")))
        print("      Mcp-Session-Id = %s" % (sid or "(未下发)"))
        if code != 200 or not res or "result" not in res:
            failures.append("MCP initialize 失败：HTTP %s" % code)
            print("      %s initialize 未通过" % FAIL)
        else:
            print("      %s initialize 通过" % PASS)
            mcp_rpc(op, endpoint, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            code2, res2, _ = mcp_rpc(
                op, endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid
            )
            tools = [t["name"] for t in (res2 or {}).get("result", {}).get("tools", [])]
            print("      工具面（%d 个）：%s" % (len(tools), ", ".join(sorted(tools))))
            if not tools:
                failures.append("tools/list 返回空工具面")
                print("      %s 工具面为空" % FAIL)
            else:
                print("      %s tools/list 通过" % PASS)

            if args.deep:
                code3, res3, _ = mcp_rpc(
                    op,
                    endpoint,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "gateway_health", "arguments": {}},
                    },
                    sid,
                )
                txt = "".join(
                    c.get("text", "")
                    for c in (res3 or {}).get("result", {}).get("content", [])
                )
                ok = '"status": "ok"' in txt or '"status":"ok"' in txt
                print("      %s 深检 gateway_health 工具调用：%s"
                      % (PASS if ok else FAIL, txt[:120]))
                if not ok:
                    failures.append("gateway_health 工具调用未返回 ok")
    except Exception as e:
        failures.append("MCP 不可达：%s: %s" % (type(e).__name__, e))
        print("      %s 不可达：%s" % (FAIL, e))

    # ---- 结论 ----
    print("\n" + "=" * 72)
    if failures:
        print("%s 探活失败 %d 项：" % (FAIL, len(failures)))
        for f in failures:
            print("   - %s" % f)
        return 1
    print("%s 两道探活全部通过（HTTP 200 + MCP initialize 200）" % PASS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
