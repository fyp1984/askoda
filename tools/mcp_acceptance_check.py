# -*- coding: utf-8 -*-
"""M4-1 / M5-1 · MCP 层端到端验收（客户端独立复扫）

不调用容器内函数，而是走真实 MCP 协议（streamable-http）发 JSON-RPC，
验证「工具面是否真的注册」「新工具是否真的能调通」「返回是否符合契约」。

这条路与容器内直接 import 是两条独立路径：容器内测的是代码对不对，
这里测的是「暴露给 Agent 客户端的那一面」对不对。

用法：
    python tools/mcp_acceptance_check.py [http://127.0.0.1:18080/mcp]
"""
import json
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18080/mcp"
_SID = {"v": None}
_RID = {"v": 0}


def rpc(method, params=None, notify=False):
    _RID["v"] += 1
    payload = {"jsonrpc": "2.0", "method": method}
    if not notify:
        payload["id"] = _RID["v"]
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    if _SID["v"]:
        req.add_header("Mcp-Session-Id", _SID["v"])
    with urllib.request.urlopen(req, timeout=60) as resp:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            _SID["v"] = sid
        body = resp.read().decode()
    if notify:
        return None
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def payload_of(res):
    """兼容 fastmcp 的 structuredContent / content[0].text 两种回传形态。"""
    r = (res or {}).get("result", {})
    sc = r.get("structuredContent")
    if sc:
        return sc.get("result", sc)
    for c in r.get("content", []) or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return c["text"]
    return r


def main():
    fails = []

    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "acceptance", "version": "1.0"}})
    rpc("notifications/initialized", notify=True)

    names = sorted(t["name"] for t in rpc("tools/list")["result"]["tools"])
    print("工具面总数: %d" % len(names))
    expect_new = ["schema_scan", "schema_version", "requirement_structured",
                  "requirement_get", "schema_candidates", "sql_context_pack",
                  "sql_review", "sql_plan", "sql_generate",
                  "sql_execute_readonly", "sql_run_get"]
    missing = [n for n in expect_new if n not in names]
    print("M4/M5 具名工具: %d/%d 在册%s" % (
        len(expect_new) - len(missing), len(expect_new),
        ("，缺: " + ", ".join(missing)) if missing else ""))
    if missing:
        fails.append("工具缺失: %s" % missing)

    print("\n--- schema_scan（真实调用）---")
    d = payload_of(rpc("tools/call", {"name": "schema_scan",
                                      "arguments": {"dataset": "A", "persist": False}}))
    if isinstance(d, dict) and d.get("schema_version"):
        print("   A 库 schema_version=%s  counts=%s  source=%s" % (
            d["schema_version"], d.get("counts"), d.get("source")))
    else:
        print("   返回异常: %s" % str(d)[:200])
        fails.append("schema_scan 返回不符合契约")

    print("\n--- sql_review（真实调用，期望与期望值比对）---")
    # 说明：走 MCP 时 L4 会真实调用 Wren 语义层，语义层不接受的语法会先被 L4 阻断，
    # 因此这里对「逗号连接」不预设最终状态，而是校验 L2 是否检出了该风险。
    # L2 警告的隔离验证由离线脚本 tools/gates_independent_check.py 负责（用桩隔离 L4）。
    cases = [
        ("SELECT order_id, amount FROM orders WHERE amount > 100", "通过或警告", "只读查询", None),
        ("DELETE FROM orders", "不通过", "写操作", None),
        ("SELECT * FROM orders o, customers c", None, "逗号连接", "JOIN_WITHOUT_CONDITION"),
        ("SELECT * FROM orders", None, "SELECT *", "SELECT_STAR"),
        ("SELECT 1; DROP TABLE orders", "不通过", "多语句拼接", None),
        ("", "不通过", "空 SQL", None),
    ]
    for sql, want, tag, must_rule in cases:
        res = rpc("tools/call", {"name": "sql_review",
                                 "arguments": {"sql": sql, "dataset": "A"}})
        dd = payload_of(res)
        got = dd.get("status") if isinstance(dd, dict) else "?"
        rules = [i.get("rule") for i in (dd.get("semantic_issues") or [])]
        ok = True
        why = []
        if want is None:
            pass
        elif want == "通过或警告":
            ok = got in ("通过", "警告")
            if not ok:
                why.append("期望 通过/警告")
        else:
            ok = (got == want)
            if not ok:
                why.append("期望 " + want)
        if must_rule and must_rule not in rules:
            ok = False
            why.append("L2 未检出 " + must_rule)
        print("   [%s] %-14s status=%-4s L2规则=%s%s" % (
            "OK" if ok else "FAIL", tag, got, rules or "无",
            ("  ← " + "；".join(why)) if why else ""))
        if not ok:
            fails.append("sql_review[%s] %s" % (tag, "；".join(why)))
            print("        违规明细: %s" % (dd.get("rule_violations") or []))

    print("\n" + "=" * 60)
    print("MCP 层验收：%s（失败 %d 项）" % ("全部通过" if not fails else "存在问题", len(fails)))
    for f in fails:
        print("  - " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
