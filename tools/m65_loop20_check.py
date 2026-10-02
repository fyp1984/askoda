# -*- coding: utf-8 -*-
"""M6-5 清单 #5：连续 20 次调用（验收方资产）。

判据（《M6 作战计划》§十 第 5 条）：
  · 无崩溃；
  · `sql_runs` 行数 == 成功执行次数（精确相等，不是 ≥）；
  · 双库业务数据未变（A/B 两库业务表 count 前后一致）。

与 `tools/robustness_verify.py` 的 D 段**刻意分工**：
  · robustness D 段 = **并发** 20 次（ThreadPoolExecutor(8)），进程内直调 execute_readonly；
  · 本脚本       = **连续**（串行）20 次，**走 MCP HTTP 真实接口**（产品入口），
                  并**直连 A/B 两库**独立取 count 对照。

运行（宿主；MCP 走 127.0.0.1:18080）：
  python3 tools/m65_loop20_check.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys
import urllib.request

import psycopg

MCP_URL = os.environ.get("M65_MCP_URL", "http://127.0.0.1:18080/mcp")
META_DSN = "postgresql://assistant:assistant@127.0.0.1:15434/assistant"
A_DSN = "postgresql://test:test@127.0.0.1:15432/test"
B_DSN = "postgresql://test:test@127.0.0.1:15433/retail"

# 与 robustness D 段同款的、能过五层门禁的业务 SQL（B 库）
GOOD_SQL = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg GROUP BY store_id"
A_TABLE = "orders"        # A 库（15432/test）真实业务表，实测 7 行
B_TABLE = "dws_store_daily_agg"   # B 库（15433/retail）真实业务表，实测 21 行
N = 20

FAIL = []
SID = None


def expect(name, cond, detail=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + ((" | " + str(detail)) if detail else ""))
    if not cond:
        FAIL.append(name)


def _rpc(method, params=None):
    global SID
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if SID:
        h["Mcp-Session-Id"] = SID
    req = urllib.request.Request(MCP_URL, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read().decode()
        SID = r.headers.get("Mcp-Session-Id") or SID
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def mcp_call(name, args):
    r = _rpc("tools/call", {"name": name, "arguments": args})
    if r is None or "result" not in r:
        return {"_rpc_error": r}
    try:
        return json.loads(r["result"]["content"][0]["text"])
    except Exception as e:  # noqa: BLE001
        return {"_parse_error": "%s: %s" % (type(e).__name__, e), "_raw": str(r)[:300]}


def count(dsn, table):
    with psycopg.connect(dsn, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute('SELECT count(*) FROM "%s"' % table)
            return int(cur.fetchone()[0])


def meta_scalar(sql, params=()):
    with psycopg.connect(META_DSN, autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


def main():
    print("=" * 78)
    print("M6-5 #5 · 连续 %d 次调用（MCP 真实接口）" % N)
    print("  MCP =", MCP_URL)
    print("=" * 78)
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "m65-loop20", "version": "1.0"}})
    _rpc("notifications/initialized", {})

    # 建一条专用测试需求（不复用历史单，避免污染既有计数）
    d = mcp_call("demand_create", {
        "title": "M6-5 连续 20 次执行验收",
        "business_context": "M6-5 清单第 5 条：连续 20 次只读执行",
        "description": "门店看板 GMV 按门店汇总",
        "expected_output": "store_id, gmv",
        "contact": "M6-5验收-13900000000",
        "actor": "m65-verifier",
    })
    demand_id = d.get("demand_id") if isinstance(d, dict) else None
    print("  测试 demand_id =", demand_id or d)
    if not demand_id:
        print("  [FATAL] 无法创建测试需求")
        return 1

    a_before = count(A_DSN, A_TABLE)
    b_before = count(B_DSN, B_TABLE)
    before_runs = meta_scalar("SELECT count(*) FROM sql_runs WHERE demand_id=%s", (demand_id,))
    print("  前置：A.%s=%d  B.%s=%d  该 demand sql_runs=%d"
          % (A_TABLE, a_before, B_TABLE, b_before, before_runs))

    ok_cnt, rid_list, crash = 0, [], []
    for i in range(1, N + 1):
        try:
            out = mcp_call("sql_execute_readonly",
                           {"demand_id": demand_id, "dataset": "B", "sql": GOOD_SQL,
                            "actor": "m65-verifier"})
            good = (out.get("ok") is not False) and out.get("sql_run_id") and out.get("row_count") is not None
            if good:
                ok_cnt += 1
                rid_list.append(out["sql_run_id"])
            else:
                crash.append((i, str(out)[:160]))
        except Exception as e:  # noqa: BLE001
            crash.append((i, "EXC %s: %s" % (type(e).__name__, e)))

    a_after = count(A_DSN, A_TABLE)
    b_after = count(B_DSN, B_TABLE)
    after_runs = meta_scalar("SELECT count(*) FROM sql_runs WHERE demand_id=%s", (demand_id,))
    added = int(after_runs) - int(before_runs)
    print("  执行：成功 %d / %d  异常=%s" % (ok_cnt, N, crash[:3] if crash else []))
    print("  收尾：sql_runs 新增=%d  A.%s=%d→%d  B.%s=%d→%d"
          % (added, A_TABLE, a_before, a_after, B_TABLE, b_before, b_after))

    expect("#5-1 连续 %d 次无崩溃（ok==%d）" % (N, N), ok_cnt == N, "实际 ok=%d 异常=%s" % (ok_cnt, crash[:2]))
    expect("#5-2 sql_runs 行数 == 成功执行次数（精确 %d）" % N, added == N, "实际新增=%d" % added)
    expect("#5-3 %d 个 sql_run_id 两两不同" % N,
           len(rid_list) == N and len(set(rid_list)) == N,
           "len=%d distinct=%d" % (len(rid_list), len(set(rid_list))))
    expect("#5-4 双库业务数据未变（A/B count 前后一致）",
           a_before == a_after and b_before == b_after,
           "A:%d→%d B:%d→%d" % (a_before, a_after, b_before, b_after))

    print("=" * 78)
    if FAIL:
        print("失败 %d 项：%s" % (len(FAIL), FAIL))
        return 1
    print("M6-5 #5 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
