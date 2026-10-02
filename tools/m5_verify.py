# -*- coding: utf-8 -*-
"""M5 端到端验收：两段式 SQL 生成 + 只读执行 + §7 九字段留痕（可复跑）

核验什么（《实施推进与验收方案》§6 · M5 验收标准 + §9.3 证据要求）
-------------------------------------------------------------------
在 M4 链路基础上，对每条样例再跑 5 条断言：

  C1. sql_plan：结构完整（18 个字段齐全），chosen_table 若非空则 ∈
      table_candidates；join_path 若非空则 relationship_index 合法。
      · A 库候选空时 draft_gate.status == "需人工审核" —— 这是 A 库的预期行为，
        **只打印不判失败**（设计§4.7 明文"选不出不硬选，标需人工审核"）。

  C2. sql_generate：sql_draft 非空字符串；review.status != "不通过"
      （警告/通过都可；status 为"不通过"就直接失败，因为链路到此断裂）。
      打印 sql_draft 的前 200 字符（证据要求：真实数值）。

  C3. sql_execute_readonly：返回真实 row_count ≥ 0；validation_result 是
      非空 dict 且含 signals；suspicious_signals 为 list。打印 row_count 实际数字。

  C4. 换库 0 代码证明：同一条业务需求（title/description 完全相同）分别跑
      dataset="A" 与 dataset="B" 各一遍；**两次运行前后**各对 gateway/*.py
      全量做（行数 + sha256 + size + mtime）采样，证明 gateway 代码 0 改动。
      —— 这是§6 里"一个网关两个数据集 0 代码切换"的硬证据，不是口头说的。

  C5. sql_run_get：返回 ≥ 1 条记录，每条 9 个溯源字段（demand_id / requirement_version /
      schema_version / pack_version / generator_model / generated_sql / review_status /
      validation_result / final_delivery_sql）**全部非空**。9 字段逐条打印字段名 + 非空值，
      空值标 FAIL。

A 库的设计容忍度：样例 M4A-4/M4A-5 的 C1 若 draft_gate.status 为"需人工审核"，不算失败。
但只要 C2 能靠 planner 兜底产出非空 SQL（因为 A 库的关键词在 plan_a 里是中文关键词命中，
不依赖 sqlpack 候选），C2~C5 一样必须通过。

不准出现 PASS/FAIL 无细节；不准出现"已验证"。所有断言附带真实数值：行数、review.status
原文、row_count 数字、SQL 前 200 字符、gateway 文件指纹前后差值（应该=0）。

用法
----
    python tools/m5_verify.py                    # 默认 http://127.0.0.1:18080/mcp
    python tools/m5_verify.py --url http://... --keep

退出码：0 全部通过；1 有失败项。
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATEWAY_DIR = os.path.join(ROOT, "gateway")
sys.path.insert(0, GATEWAY_DIR)
sys.path.insert(0, HERE)

import example_demands_m45 as demands_mod  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"

FAILS = []
CHECKS = [0]
A_FAILS, B_FAILS = [], []
A_CHECKS, B_CHECKS = [0], [0]


def check(name, cond, detail="", dataset=None):
    CHECKS[0] += 1
    bucket = A_CHECKS if dataset == "A" else B_CHECKS
    bucket[0] += 1
    if cond:
        print("    \033[32mPASS\033[0m %s" % name)
    else:
        print("    \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)
        (A_FAILS if dataset == "A" else B_FAILS).append(name)
    return cond


# ---------------------------------------------------------------------------
# McpClient（与 m3_verify.py 逐字符一致，禁止重写）
# ---------------------------------------------------------------------------
class McpClient:
    def __init__(self, url, timeout=180):
        self.endpoint = url
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._sid = None
        self._lock = threading.Lock()
        self._id = 0

    def _rpc(self, payload, sid=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), headers=headers
        )
        resp = self._opener.open(req, timeout=self.timeout)
        new_sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode()
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), new_sid
        return None, new_sid

    def handshake(self):
        res, sid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "m5-verify", "version": "1.0.0"},
                },
            }
        )
        self._sid = sid
        if sid:
            try:
                self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except Exception:
                pass
        return res

    def call(self, tool, args=None, _retry=True):
        with self._lock:
            if self._sid is None:
                self.handshake()
            self._id += 1
            try:
                res, sid = self._rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": self._id,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args or {}},
                    },
                    self._sid,
                )
                if sid:
                    self._sid = sid
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()
                except Exception:
                    pass
                if _retry and ("session" in detail.lower() or e.code in (400, 404)):
                    self._sid = None
                    return self.call(tool, args, _retry=False)
                raise RuntimeError("MCP HTTP %s: %s" % (e.code, detail[:200]))
        if res is None:
            return None
        if "error" in res:
            raise RuntimeError(json.dumps(res["error"], ensure_ascii=False)[:300])
        result = res.get("result", {})
        txt = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(txt[:300] or "工具返回错误")
        try:
            return json.loads(txt)
        except Exception:
            return {"_raw": txt}

    def list_tools(self):
        with self._lock:
            if self._sid is None:
                self.handshake()
            self._id += 1
            res, sid = self._rpc(
                {"jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {}},
                self._sid,
            )
            if sid:
                self._sid = sid
        return [t["name"] for t in (res or {}).get("result", {}).get("tools", [])]


# ---------------------------------------------------------------------------
# gateway 代码指纹（行数 + size + mtime + sha256）— 用于 C4 换库 0 代码证明
# ---------------------------------------------------------------------------
def _gateway_files():
    out = []
    for nm in sorted(os.listdir(GATEWAY_DIR)):
        p = os.path.join(GATEWAY_DIR, nm)
        if nm.endswith(".py") and os.path.isfile(p):
            out.append(p)
    return out


def gateway_snapshot(tag):
    """给 gateway/*.py 拍指纹快照。返回 {filename: {lines, size, mtime, sha256}}"""
    snap = {}
    for p in _gateway_files():
        try:
            with open(p, "rb") as f:
                data = f.read()
            lines = data.count(b"\n") + 1
            size = len(data)
            stat = os.stat(p)
            mtime = stat.st_mtime_ns
            sha = hashlib.sha256(data).hexdigest()
        except Exception as e:  # noqa: BLE001
            lines, size, mtime, sha = -1, -1, -1, "ERR:%s" % str(e)[:20]
        snap[os.path.basename(p)] = {
            "lines": lines,
            "size": size,
            "mtime_ns": mtime,
            "sha256": sha[:16],
        }
    print("  [%s] gateway/*.py 共 %d 份；总行数=%d  总字节=%d  sha_tail=%s" % (
        tag,
        len(snap),
        sum(x["lines"] for x in snap.values()),
        sum(x["size"] for x in snap.values()),
        hashlib.sha256(
            json.dumps({k: v["sha256"] for k, v in sorted(snap.items())},
                       sort_keys=True).encode()
        ).hexdigest()[:16],
    ))
    return snap


def snapshot_diff(before, after):
    diffs = []
    for k in sorted(set(before.keys()) | set(after.keys())):
        b, a = before.get(k), after.get(k)
        if not b or not a:
            diffs.append("%s: 缺失一侧（b=%s a=%s）" % (k, bool(b), bool(a)))
            continue
        if b["sha256"] != a["sha256"] or b["lines"] != a["lines"] or b["size"] != a["size"]:
            diffs.append(
                "%s: lines %d→%d  size %d→%d  sha %s→%s" % (
                    k, b["lines"], a["lines"], b["size"], a["size"], b["sha256"], a["sha256"]
                )
            )
    return diffs


# ---------------------------------------------------------------------------
# §7 九字段留痕检查
# ---------------------------------------------------------------------------
TRACE9 = [
    "demand_id", "requirement_version", "schema_version", "pack_version",
    "generator_model", "generated_sql", "review_status", "validation_result",
    "final_delivery_sql",
]


def _nonempty(v):
    if v is None:
        return False
    if isinstance(v, (dict, list, str)):
        return len(v) > 0
    return True


def verify_trace9(key, run):
    empty_fields = [k for k in TRACE9 if not _nonempty(run.get(k))]
    for k in TRACE9:
        v = run.get(k)
        v_show = ""
        if isinstance(v, str):
            v_show = v[:48] + ("…" if len(v) > 48 else "")
        elif isinstance(v, int):
            v_show = str(v)
        elif isinstance(v, (dict, list)):
            v_show = "<%s len=%d>" % (type(v).__name__, len(v))
        print("      · %-22s = %s  %s" % (
            k, v_show,
            ("\033[31m✗空\033[0m" if k in empty_fields else "✓"),
        ))
    return empty_fields


# ---------------------------------------------------------------------------
# 单样例链路
# ---------------------------------------------------------------------------
PLAN_KEYS = [
    "demand_id", "dataset", "pack_version",
    "chosen_table", "chosen_table_reason",
    "granularity", "granularity_reason",
    "join_path", "join_path_reason",
    "time_field", "time_field_reason",
    "aggregate_fields", "field_mapping",
    "draft_gate",
]
# 完整结构 16 项：demand_id/dataset/pack_version/(requirement+schema)_version + 13 个草稿字段。
# 说明：sqlgen.plan() 的返回里**不含候选池**（table_candidates / key_candidates 属
# sql_context_pack 的产出）；对「chosen_table ∈ 候选闭集」的校验改由 C1b 直接取
# sql_context_pack 的 table_candidates 完成——那才是候选闭集的权威来源，检验更强。
FULL_PLAN_KEYS = [
    "demand_id", "dataset", "pack_version",
    "requirement_version", "schema_version",
    "chosen_table", "chosen_table_reason",
    "granularity", "granularity_reason",
    "join_path", "join_path_reason",
    "time_field", "time_field_reason",
    "aggregate_fields", "field_mapping",
    "draft_gate",
]


def _sample_with_dataset(s, ds):
    """用 s 的 payload 生成一份但 dataset 覆盖为 ds。"""
    return {
        "key": s["key"] + "-SWAP-" + ds,
        "intent": s["intent"] + "（换库 dataset=%s）" % ds,
        "dataset": ds,
        "payload": dict(s["payload"]),
    }


def verify_sample(cli, sample):
    key = sample["key"]
    ds = sample.get("dataset", "B")
    print("\n%s [%s]  dataset=%s" % ("-" * 70, key, ds))
    print("  意图：%s" % sample["intent"])

    payload = dict(sample["payload"])
    d = cli.call("demand_create", payload)
    if not d.get("demand_id"):
        check("%s demand_create 成功" % key, False, json.dumps(d, ensure_ascii=False)[:200], dataset=ds)
        return None
    did = d["demand_id"]
    print("  demand_id = %s" % did)

    # 先把 M4 前置步骤跑掉（不判失败，为了 requirement 落库）
    for tool in ["analysis_first_round", "requirement_structured"]:
        _ = cli.call(tool, {"demand_id": did, "dataset": ds})

    # C1. sql_plan
    pl = cli.call("sql_plan", {"demand_id": did, "dataset": ds})
    print("  [C1] sql_plan")
    missing_plan_keys = [k for k in FULL_PLAN_KEYS if k not in pl]
    check("%s C1a sql_plan 结构完整（%d 字段齐全）" % (key, len(FULL_PLAN_KEYS)),
          len(missing_plan_keys) == 0,
          "缺字段：%s" % missing_plan_keys, dataset=ds)
    # chosen_table 若非空 → ∈ table_candidates（闭集的权威来源 = sql_context_pack）
    chosen = pl.get("chosen_table") or ""
    pack = cli.call("sql_context_pack", {"demand_id": did, "dataset": ds})
    tbl_pool = {t.get("table") for t in (pack.get("table_candidates") or [])}
    chosen_ok = (chosen == "") or (chosen in tbl_pool)
    check("%s C1b chosen_table 为空 或 ∈ table_candidates（chosen=%r 闭集=%d 张）"
          % (key, chosen, len(tbl_pool)),
          chosen_ok, "chosen_table=%r 不在候选 %s" % (chosen, sorted(tbl_pool)[:6]), dataset=ds)
    # A 库：若 draft_gate.status == "需人工审核" —— 只打印不判失败（§4.7 预期）
    dg = pl.get("draft_gate") or {}
    dg_status = dg.get("status") or ""
    if ds == "A":
        print("    [A库容忍] draft_gate.status = %s  （非空，detail 前 120 = %s）"
              % (dg_status, str(dg.get("detail") or "")[:120]))
    else:
        check("%s C1c B 库 draft_gate.status ∈ {通过, 需人工审核}" % key,
              dg_status in ("通过", "需人工审核"),
              "status=%r" % dg_status, dataset=ds)
    join_path = pl.get("join_path") or []
    # join_path 合法性：relationship_index ∈ mdl relationships 长度
    man = cli.call("wren_manifest", {"dataset": ds})
    n_rel = len(man.get("relationships") or [])
    bad_join = [j for j in join_path if not (
        isinstance(j.get("relationship_index"), int)
        and 0 <= j["relationship_index"] < n_rel
    )]
    check("%s C1d join_path 项 (%d 条) 全部 relationship_index ∈ [0,%d)" % (key, len(join_path), n_rel),
          len(bad_join) == 0, "越界：%s" % [j.get("relationship_index") for j in bad_join[:3]],
          dataset=ds)

    # C2. sql_generate
    gen = cli.call("sql_generate", {"demand_id": did, "dataset": ds, "candidate_sql": None})
    print("  [C2] sql_generate")
    sql_d = gen.get("sql_draft") or ""
    check("%s C2a sql_draft 为非空字符串（长度=%d）" % (key, len(sql_d)),
          isinstance(sql_d, str) and len(sql_d) > 20,
          "sql_draft=%r" % sql_d[:80], dataset=ds)
    review = gen.get("review") or {}
    rv_st = review.get("status") or ""
    check("%s C2b review.status != 不通过（实际=%s）" % (key, rv_st),
          rv_st != "不通过",
          "layers_False=%s" % [
              (l.get("layer"), l.get("name"), str(l.get("detail") or "")[:60])
              for l in (review.get("layers") or []) if not l.get("ok") and not l.get("skipped")
          ][:3],
          dataset=ds)
    print("    sql_draft 前 200 = %s" % sql_d[:200])
    print("    generator=%s  review.status=%s" % (gen.get("generator"), rv_st))

    # C3. sql_execute_readonly —— 两步：
    #   ① 显式传入上一步生成的 SQL（这是设计的正常用法），验真实执行 + L5 结果断言；
    #   ② 再传 sql=None，验「sql 为空时取该需求最近一次生成的 SQL」这条兜底契约
    #      （① 已写入 sql_runs，故 ② 才有源可回取）。
    print("  [C3] sql_execute_readonly · ① 显式传 SQL")
    exe = cli.call("sql_execute_readonly", {"demand_id": did, "dataset": ds, "sql": sql_d})
    exe_blocked = exe.get("blocked") or exe.get("ok") is False and exe.get("blocked_by")
    if exe_blocked:
        # 写操作被拒也算正常；但验收样例是只读查询 → blocked = FAIL
        check("%s C3a 只读查询未被门禁 blocked" % key, False,
              "blocked_by=%s error=%s" % (exe.get("blocked_by"), (exe.get("error") or "")[:160]),
              dataset=ds)
        return did
    rc = exe.get("row_count")
    vr = exe.get("validation_result") or {}
    ss = exe.get("suspicious_signals") or []
    check("%s C3a row_count 为整数且 ≥ 0" % key, isinstance(rc, int) and rc >= 0,
          "row_count=%r" % rc, dataset=ds)
    check("%s C3b validation_result 为 dict（keys=%s）" % (key, sorted(vr.keys())[:8]),
          isinstance(vr, dict) and len(vr) > 0, "vr=%r" % vr, dataset=ds)
    check("%s C3c suspicious_signals 为 list（条数=%d）" % (key, len(ss)),
          isinstance(ss, list), "实际类型=%s" % type(ss).__name__, dataset=ds)
    print("    row_count = %s" % rc)
    print("    execution_summary = %s" % (exe.get("execution_summary") or ""))
    print("    suspicious_signals = %s" % (ss[:6] if isinstance(ss, list) else ss))
    print("    signals (from validation_result) = %s" % (vr.get("signals", [])[:6]))

    # C3-②. sql=None 兜底：① 已写入 sql_runs → 这里应能取到「最近一次生成的 SQL」并执行
    print("  [C3-②] sql_execute_readonly · sql=None（取上次生成 SQL 兜底）")
    exe2 = cli.call("sql_execute_readonly", {"demand_id": did, "dataset": ds, "sql": None})
    rc2 = exe2.get("row_count")
    check("%s C3d sql=None 兜底可执行（row_count=%r）" % (key, rc2),
          isinstance(rc2, int) and rc2 >= 0,
          "error=%s" % str(exe2.get("error"))[:160], dataset=ds)

    # C5. sql_run_get — 9 字段
    runs_res = cli.call("sql_run_get", {"demand_id": did})
    print("  [C5] sql_run_get · §7 九字段留痕回放")
    runs = runs_res.get("runs") if runs_res.get("ok") else []
    check("%s C5a sql_run_get 返回 ≥ 1 条记录（实际=%d）" % (key, len(runs)),
          len(runs) >= 1,
          "runs_res=%s" % json.dumps(runs_res, ensure_ascii=False)[:200], dataset=ds)
    total_empty = []
    for i, r in enumerate(runs[:3]):
        print("    run #%d  sql_run_id=%s  created_at=%s" % (
            i, r.get("sql_run_id"), (r.get("created_at") or "")[:19]))
        emp = verify_trace9(key, r)
        total_empty.extend(emp)
    check("%s C5b 运行记录 9 字段全部非空" % key, len(total_empty) == 0,
          "空字段：%s" % sorted(set(total_empty))[:6], dataset=ds)
    return did


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="M5 端到端验收：两段式 SQL 生成 + 执行 + 留痕")
    ap.add_argument("--url", default=os.getenv("GATEWAY_MCP_URL", "http://127.0.0.1:18080/mcp"))
    ap.add_argument("--keep", action="store_true", help="跑完不清理需求单")
    args = ap.parse_args()

    b_n = sum(1 for s in demands_mod.SAMPLES if s.get("dataset", "B") == "B")
    a_n = len(demands_mod.SAMPLES) - b_n
    print("=" * 74)
    print("M5 端到端验收 —— 两段式 SQL 生成 + 只读执行 + §7 九字段留痕")
    print("端点：%s" % args.url)
    print("样例总数=%d （B库 %d / A库 %d）；另有 1 条换库对比（同一个样例 A↔B 各跑一遍）"
          % (len(demands_mod.SAMPLES), b_n, a_n))
    print("=" * 74)

    cli = McpClient(args.url)
    tools = cli.list_tools()
    required = [
        "demand_create", "demand_set_status",
        "analysis_first_round", "requirement_structured",
        "sql_plan", "sql_generate", "sql_execute_readonly", "sql_run_get",
        "wren_manifest",
    ]
    missing = [t for t in required if t not in tools]
    check("M5 九个工具均已注册", not missing, "缺：%s" % missing)
    print("    已注册：%s" % "、".join(t for t in required if t in tools))

    made = []
    a_done, b_done = 0, 0
    try:
        # ---------- 正常链路：逐条样例跑 A/B 各自 dataset ----------
        for s in demands_mod.SAMPLES:
            ds = s.get("dataset", "B")
            if ds == "A":
                a_done += 1
            else:
                b_done += 1
            did = verify_sample(cli, s)
            if did:
                made.append(did)

        # ---------- C4：换库 0 代码证明 ----------
        print("\n" + "=" * 74)
        print("【C4】换库 0 代码证明：同一个 payload 在 A 库/B 库各跑一遍，gateway/*.py 零改动")
        print("=" * 74)
        # 用 B 库的 M4B-1（「各门店本月复购用户数」）——A 库 planner 不一定命中，但只测"代码不崩 + 指纹不变"
        swap_base = [s for s in demands_mod.SAMPLES if s.get("key") == "M4B-1"][0] \
            if any(s.get("key") == "M4B-1" for s in demands_mod.SAMPLES) else demands_mod.SAMPLES[0]

        snap_before = gateway_snapshot("BEFORE 换库 run")

        # 跑 A 库版 —— 只跑 generate/execute（不因为 C1 空而卡断），让流程完整
        sa = _sample_with_dataset(swap_base, "A")
        print("\n  [swap → A 库] %s" % sa["key"])
        da = cli.call("demand_create", sa["payload"])
        if da.get("demand_id"):
            for t in ["analysis_first_round", "requirement_structured"]:
                _ = cli.call(t, {"demand_id": da["demand_id"], "dataset": "A"})
            _ = cli.call("sql_plan", {"demand_id": da["demand_id"], "dataset": "A"})
            g_a = cli.call("sql_generate", {"demand_id": da["demand_id"], "dataset": "A", "candidate_sql": None})
            print("    generate.review.status=%s  sql 前 80=%s" % (
                (g_a.get("review") or {}).get("status"), (g_a.get("sql_draft") or "")[:80]))
            made.append(da["demand_id"])

        snap_mid_a = gateway_snapshot("AFTER A 库 run")

        # 跑 B 库版
        sb = _sample_with_dataset(swap_base, "B")
        print("\n  [swap → B 库] %s" % sb["key"])
        db_ = cli.call("demand_create", sb["payload"])
        if db_.get("demand_id"):
            for t in ["analysis_first_round", "requirement_structured"]:
                _ = cli.call(t, {"demand_id": db_["demand_id"], "dataset": "B"})
            _ = cli.call("sql_plan", {"demand_id": db_["demand_id"], "dataset": "B"})
            g_b = cli.call("sql_generate", {"demand_id": db_["demand_id"], "dataset": "B", "candidate_sql": None})
            print("    generate.review.status=%s  sql 前 80=%s" % (
                (g_b.get("review") or {}).get("status"), (g_b.get("sql_draft") or "")[:80]))
            made.append(db_["demand_id"])

        snap_after = gateway_snapshot("AFTER B 库 run")
        d1 = snapshot_diff(snap_before, snap_mid_a)
        d2 = snapshot_diff(snap_mid_a, snap_after)
        d_all = snapshot_diff(snap_before, snap_after)
        check("C4-1 【换库 A 库】前后 gateway/*.py 指纹无差异（diffs=%d）" % len(d1),
              len(d1) == 0, "有差异文件：%s" % d1[:6])
        check("C4-2 【换库 B 库】前后 gateway/*.py 指纹无差异（diffs=%d）" % len(d2),
              len(d2) == 0, "有差异文件：%s" % d2[:6])
        check("C4-3 【两次换库累计】BEFORE↔AFTER 网关指纹全量无差异（diffs=%d）" % len(d_all),
              len(d_all) == 0, "有差异文件：%s" % d_all[:6])
        print()

    finally:
        if not args.keep and made:
            print("\n%s 清理演示数据（共 %d 条）" % ("-" * 70, len(made)))
            for did in made:
                try:
                    r = cli.call("demand_set_status",
                                 {"demand_id": did, "status": "已退回",
                                  "note": "M5 验收现场清理"})
                    print("    %s -> %s" % (did, r.get("status")))
                except Exception as e:  # noqa: BLE001
                    print("    %s 状态流转失败：%s" % (did, str(e)[:120]))

    # 汇总
    print("\n" + "=" * 74)
    print("【合计】共 %d 项断言，失败 %d 项。样例=B库 %d 条 / A库 %d 条 / 换库对比 1 组"
          % (CHECKS[0], len(FAILS), b_done, a_done))
    print("【B 库】共 %d 项断言，失败 %d 项" % (B_CHECKS[0], len(B_FAILS)))
    for f in B_FAILS:
        print("     ✗ %s" % f)
    print("【A 库】共 %d 项断言，失败 %d 项" % (A_CHECKS[0], len(A_FAILS)))
    for f in A_FAILS:
        print("     ✗ %s" % f)
    tot = max((b_done + a_done + 1), 1)
    pass_rate_overall = (CHECKS[0] - len(FAILS)) / CHECKS[0] * 100 if CHECKS[0] else 100.0
    pass_b = (B_CHECKS[0] - len(B_FAILS)) / B_CHECKS[0] * 100 if B_CHECKS[0] else 100.0
    pass_a = (A_CHECKS[0] - len(A_FAILS)) / A_CHECKS[0] * 100 if A_CHECKS[0] else 100.0
    print()
    print("通过率（按断言）：合计 %.2f%%   B库 %.2f%%   A库 %.2f%%" % (
        pass_rate_overall, pass_b, pass_a))
    if FAILS:
        print("\033[31mM5 验收未通过（%d 项失败）\033[0m" % len(FAILS))
        return 1
    print("\033[32mM5 验收通过（%d 断言 / 0 失败；换库 0 代码证明指纹一致）\033[0m" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
