# -*- coding: utf-8 -*-
"""M6-5 收官遗留清理 · 独立复扫（验收方资产 · 容器内运行）。

覆盖 M6-5 验收单 §五 记的三处遗留：
  遗留1 澄清问答闭环 —— `confirmation_answer` 走 **MCP 真实接口**真答一次，
                        再用**独立 SQL 回查** confirmations.answered_by / demand_events
                        （不采信返回值的自述）。此前全库 answered_by 恒为 NULL，
                        根因不是功能缺陷，而是"这条链路从未被真实调用过一次"。
  遗留2 `sql_run_list` 的 ok 字段 —— 正常 / 按 dataset 筛选 / 空结果 三态都带 ok。
  遗留3 `fallback.classify()` 接入生产 —— `execute_readonly` 返回 fallback_matrix，
                        且 F5（0 行不可交付）要真能命中（证明不是写死的空壳）。

运行（容器内）：
  docker cp tools/m65_remediation_check.py demand-gateway:/app/tools/
  docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/m65_remediation_check.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GW = os.path.join(ROOT, "gateway")
if not os.path.isdir(GW):
    GW = ROOT          # 容器内代码平铺在 /app，没有 gateway/ 子目录
if GW not in sys.path:
    sys.path.insert(0, GW)

import db  # noqa: E402

MCP_URL = os.environ.get("M65_MCP_URL", "http://127.0.0.1:8080/mcp")
ACTOR = "m65-remediator"
GOOD_SQL = "SELECT 1 AS n"                 # 实测：过五层门禁、row_count=1
ZERO_SQL = "SELECT 1 AS n WHERE 1 = 0"     # 实测：过门禁、row_count=0 → 应命中 F5

FAIL = []
SID = None


def expect(name, cond, detail=""):
    if cond:
        print("  [OK]", name)
    else:
        msg = "  [FAIL] %s%s" % (name, (" | " + str(detail)) if detail else "")
        print(msg)
        FAIL.append(msg)


def info(name, value):
    print("  [--] %s: %s" % (name, value))


# ---------------------------------------------------------------------------
# MCP HTTP（真实接口）
# ---------------------------------------------------------------------------
def _rpc(method, params=None):
    global SID
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if SID:
        h["Mcp-Session-Id"] = SID
    req = urllib.request.Request(MCP_URL, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
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
        return {"_parse_error": "%s: %s" % (type(e).__name__, e), "_raw": str(r)[:400]}


def mcp_init():
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "m65-remediation", "version": "1.0"}})
    _rpc("notifications/initialized", {})


def nn(v, default=-999999):
    """显式 None 判定，避免 `0 or -1` 把合法的 0 判成失败（本项目第三次踩零值坑）。"""
    return default if v is None else v


# ===========================================================================
# A · 遗留1：澄清问答闭环（MCP 真实接口 + 独立回查）
# ===========================================================================
def section_a():
    print("\n[A] 遗留1 · 澄清问答闭环（confirmation_answer 真实调用 + 独立回查）")
    cur = db.query_one(
        """
        SELECT c.demand_id, c.question_id, c.version
        FROM confirmations c
        WHERE c.version = (SELECT max(version) FROM confirmations c2
                           WHERE c2.demand_id = c.demand_id AND c2.question_id = c.question_id)
          AND c.answer IS NULL
        ORDER BY c.demand_id
        LIMIT 1
        """
    )
    if not cur:
        expect("A0 库内存在『可答复的确认问题』", False, "当前版本已无未答复问题")
        return
    did, qid = cur["demand_id"], cur["question_id"]
    info("样本", "%s / %s" % (did, qid))

    # 答前基线（独立查库）
    before = db.query(
        "SELECT version FROM confirmations WHERE demand_id=%s AND question_id=%s ORDER BY version",
        (did, qid),
    )
    before_n = len(before)
    before_max = max(int(r["version"]) for r in before) if before else 0
    g_by = db.query_one("SELECT count(answered_by) AS b FROM confirmations")
    info("答前 该问题版本数 / 最大版本", "%d / %d" % (before_n, before_max))
    info("答前 全库 answered_by 非空行数", nn(g_by["b"], 0))

    # --- 走 MCP 真实接口答复 ---
    ans = mcp_call("confirmation_answer", {
        "demand_id": did, "question_id": qid,
        "answer": "【M6-5 遗留清理·验收测试答复】按业务单据逐条统计",
        "actor": ACTOR,
    })
    info("MCP 返回", json.dumps({k: ans.get(k) for k in ("ok", "noop", "version", "answer")},
                                ensure_ascii=False)[:220])
    expect("A1 confirmation_answer 经 MCP 真实接口返回 ok", ans.get("ok") is True, ans)
    expect("A2 非 noop（确实新增了一版答复）", ans.get("noop") is False, ans.get("noop"))

    # --- 独立 SQL 回查（不采信返回值） ---
    after = db.query(
        "SELECT version, answer, answered_by, answered_at FROM confirmations "
        "WHERE demand_id=%s AND question_id=%s ORDER BY version",
        (did, qid),
    )
    expect("A3 版本行数 +1（版本化、不覆盖历史）", len(after) == before_n + 1,
           "before=%d after=%d" % (before_n, len(after)))
    latest = after[-1] if after else {}
    expect("A4 新行 version == 旧最大 +1", int(latest.get("version") or 0) == before_max + 1,
           "new=%s expect=%d" % (latest.get("version"), before_max + 1))
    expect("A5 独立回查 confirmations.answered_by == 传入 actor",
           latest.get("answered_by") == ACTOR, latest.get("answered_by"))
    expect("A6 answer 非空", bool((latest.get("answer") or "").strip()), latest.get("answer"))
    expect("A7 answered_at 非空（落库时间由库侧 now() 写）",
           latest.get("answered_at") is not None, latest.get("answered_at"))

    ev = db.query(
        "SELECT actor, detail FROM demand_events WHERE demand_id=%s "
        "AND event_type='confirmation_answered' ORDER BY created_at DESC LIMIT 1",
        (did,),
    )
    expect("A8 demand_events 留有 confirmation_answered 且 actor 正确",
           bool(ev) and ev[0]["actor"] == ACTOR,
           ev[0] if ev else "无事件")

    g_after = db.query_one("SELECT count(answered_by) AS b FROM confirmations")
    expect("A9 全库 answered_by 非空行数 ≥ 1（此前恒 0，链路首次被真实走通）",
           int(nn(g_after["b"], 0)) >= 1, "after=%s" % g_after["b"])


# ===========================================================================
# B · 遗留2：sql_run_list 的三态都带 ok
# ===========================================================================
def section_b():
    print("\n[B] 遗留2 · sql_run_list 返回 ok（正常 / 筛选 / 空结果 三态）")
    r_all = mcp_call("sql_run_list", {"limit": 3})
    expect("B1 不筛 → ok is True", r_all.get("ok") is True, sorted(r_all.keys())[:12])
    expect("B2 不筛 → total>0 且 items 非空",
           int(nn(r_all.get("total"), -1)) > 0 and bool(r_all.get("items")),
           "total=%s items=%d" % (r_all.get("total"), len(r_all.get("items") or [])))

    r_a = mcp_call("sql_run_list", {"dataset": "A", "limit": 3})
    expect("B3 dataset=A → ok is True 且筛选生效（total 小于全量）",
           r_a.get("ok") is True and int(nn(r_a.get("total"), -1)) < int(nn(r_all.get("total"), 0)),
           "A=%s 全量=%s" % (r_a.get("total"), r_all.get("total")))

    r_no = mcp_call("sql_run_list", {"dataset": "__NO_SUCH_DATASET__", "limit": 3})
    expect("B4 不存在的 dataset → ok is True 且 total==0 且 items 空（绝不降级成全量）",
           r_no.get("ok") is True
           and int(nn(r_no.get("total"), -1)) == 0
           and not (r_no.get("items") or []),
           "ok=%s total=%s items=%d" % (r_no.get("ok"), r_no.get("total"),
                                        len(r_no.get("items") or [])))


# ===========================================================================
# C · 遗留3：fallback.classify 接入 execute_readonly（fallback_matrix）
# ===========================================================================
def section_c(demand_id):
    print("\n[C] 遗留3 · fallback_matrix（classify 接入生产链路）")
    o1 = mcp_call("sql_execute_readonly",
                  {"demand_id": demand_id, "dataset": "B", "sql": GOOD_SQL, "actor": ACTOR})
    fm = o1.get("fallback_matrix")
    expect("C1 execute_readonly 返回含 fallback_matrix", isinstance(fm, dict),
           sorted(o1.keys()))
    need = ("kinds", "actions", "hold", "deliverable", "detail")
    expect("C2 fallback_matrix 结构齐全 %s" % (need,),
           isinstance(fm, dict) and all(k in fm for k in need),
           sorted((fm or {}).keys()))
    expect("C3 正常执行 → kinds 为空 / hold False / deliverable True",
           (fm or {}).get("kinds") == [] and (fm or {}).get("hold") is False
           and (fm or {}).get("deliverable") is True, fm)
    expect("C4 既有字段不回归：deliverable/adopted/row_count 与之前一致",
           o1.get("deliverable") is True and o1.get("adopted") is True
           and o1.get("row_count") == 1,
           "deliverable=%s adopted=%s row_count=%s"
           % (o1.get("deliverable"), o1.get("adopted"), o1.get("row_count")))

    # F5 真命中：0 行 → 不可交付
    o2 = mcp_call("sql_execute_readonly",
                  {"demand_id": demand_id, "dataset": "B", "sql": ZERO_SQL, "actor": ACTOR})
    fm2 = o2.get("fallback_matrix") or {}
    expect("C5 0 行 → fallback_matrix.kinds 含 F5（证明 classify 真在算，不是空壳）",
           "F5" in (fm2.get("kinds") or []), fm2)
    expect("C6 0 行 → fallback_matrix.deliverable False，且与既有 deliverable 字段一致",
           fm2.get("deliverable") is False and o2.get("deliverable") is False,
           "matrix=%s deliverable=%s" % (fm2.get("deliverable"), o2.get("deliverable")))


def main():
    print("=" * 78)
    print("M6-5 收官遗留清理 · 独立复扫")
    print("=" * 78)
    mcp_init()
    cur = db.query_one(
        """
        SELECT c.demand_id FROM confirmations c
        WHERE c.version = (SELECT max(version) FROM confirmations c2
                           WHERE c2.demand_id = c.demand_id AND c2.question_id = c.question_id)
          AND c.answer IS NULL ORDER BY c.demand_id LIMIT 1
        """
    )
    demand_id = cur["demand_id"] if cur else None
    section_a()
    section_b()
    if demand_id:
        section_c(demand_id)
    else:
        expect("C0 需要一条可用 demand_id 作执行样本", False, "库内无未答复确认问题")
    print("\n" + "=" * 78)
    if FAIL:
        print("失败 %d 项：" % len(FAIL))
        for m in FAIL:
            print("  ", m.strip())
        sys.exit(1)
    print("全部通过。")
    sys.exit(0)


if __name__ == "__main__":
    main()
