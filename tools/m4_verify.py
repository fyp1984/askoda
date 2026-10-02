# -*- coding: utf-8 -*-
"""M4 端到端验收：结构化需求 + 上下文包（§6 两条验收标准 · 可复跑）

核验什么（《实施推进与验收方案》M4 验收标准）
------------------------------------------------
M4 有 4 条本工具逐条核（对每条业务样例跑 demand_create → first_round
→ requirement_structured → schema_candidates → sql_context_pack 整条链路）：

  C1. demand_create + analysis_first_round 都成功，六槽位可枚举。
      first_round.ok = True 且 slots 含 subject/granularity/time/scope/fields/risks
      六项（允许值为空，但结构必须完整——结构缺键直接判失败，因为后段会 KeyError）。

  C2. requirement_structured 通过契约校验：contract_ok=True 且 contract_errors=[]。
      如果 contract_errors 非空，**打印全量错误原文**（带 JSON Path），
      不接受"人工看一眼没毛病"式验收——契约就是契约。

  C3. schema_candidates：B 库要求表候选非空；A 库允许表候选为空，
      但 miss_reason 必须非空（打印 miss_reason 原文，这是 A 库的价值证据）。
      A 库语义词库仅 7 条、列 description 覆盖 6.9%，业务口吻必然全空——
      **空但给出 miss_reason 才算通过**，直接抛异常或 miss_reason 空仍判失败。

  C4. sql_context_pack 的三溯源版本号齐全且合法：
      schema_version / requirement_version / pack_version 都非空且都是字符串/整数；
      join_candidates 每项的 relationship_index 必须 ∈ [0, len(mdl_relationships))
      （用 wren_manifest 现取 relationships 长度，客户端独立重算一遍，不能信任服务端返回）。

分库打印：A 库样例打印 A 库段，B 库样例打印 B 库段，最后各自的失败计数 + 合计。
不准只写 PASS/FAIL，不准出现「已验证」——必须带真实数值（行数、版本号、
miss_reason 原文、join_candidates 条数与 relationship_index 范围）。

用法
----
    python tools/m4_verify.py                     # 默认 http://127.0.0.1:18080/mcp
    python tools/m4_verify.py --url http://... --keep

    --keep   跑完不清理需求单（默认清理）

退出码：0 全部通过；1 有失败项。
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))
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
# MCP over streamable-http（与 tools/m3_verify.py 逐字符一致，不得改写法）
# ---------------------------------------------------------------------------
class McpClient:
    def __init__(self, url, timeout=120):
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
                    "clientInfo": {"name": "m4-verify", "version": "1.0.0"},
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
# 单样例校验
# ---------------------------------------------------------------------------
SLOT_KEYS = ["subject", "granularity", "time", "scope", "fields", "risks"]


def verify_sample(cli, sample):
    key = sample["key"]
    ds = sample.get("dataset", "B")
    print("\n%s [%s]  dataset=%s" % ("-" * 70, key, ds))
    print("  意图：%s" % sample["intent"])

    # C1. 建单 + first_round
    payload = dict(sample["payload"])
    d = cli.call("demand_create", payload)
    if not d.get("demand_id"):
        check("%s C1a demand_create 成功" % key, False,
              json.dumps(d, ensure_ascii=False)[:240], dataset=ds)
        return None
    did = d["demand_id"]
    print("  demand_id = %s   dataset = %s" % (did, ds))

    fr = cli.call("analysis_first_round", {"demand_id": did, "dataset": ds})
    if fr.get("ok") is False:
        check("%s C1b analysis_first_round.ok = True" % key, False,
              json.dumps(fr, ensure_ascii=False)[:240], dataset=ds)
        return did
    check("%s C1a demand_create 成功（demand_id=%s）" % (key, did), True, dataset=ds)
    check("%s C1b analysis_first_round.ok = True" % key, fr.get("ok") is True, dataset=ds)
    slots = fr.get("slots") or {}
    missing_keys = [k for k in SLOT_KEYS if k not in slots]
    check("%s C1c first_round 六槽位结构齐全（%s）" % (key, SLOT_KEYS), not missing_keys,
          "缺：%s" % missing_keys, dataset=ds)
    for k in SLOT_KEYS:
        val = slots.get(k)
        print("    - slot[%s] value_head=%s  confidence=%s evidence_n=%s" % (
            k,
            ((val or {}).get("value") or "")[:40],
            (val or {}).get("confidence"),
            len((val or {}).get("evidence") or []),
        ))

    # C2. requirement_structured 契约校验
    rs = cli.call("requirement_structured", {"demand_id": did, "dataset": ds})
    rs_ok = rs.get("contract_ok") is True
    rs_errs = rs.get("contract_errors") or []
    check("%s C2a requirement_structured.contract_ok = True" % key, rs_ok,
          "contract_ok=%r  contract_errors=%s" % (rs.get("contract_ok"), rs_errs[:4]),
          dataset=ds)
    check("%s C2b requirement_structured.contract_errors = []" % key, len(rs_errs) == 0,
          "errors 共 %d 条：%s" % (len(rs_errs), rs_errs[:4]), dataset=ds)
    if not rs_ok:
        for err in rs_errs[:8]:
            print("     · 契约错误：%s" % err)
    print("    requirement_version = %s   schema_version 指向 = %s" % (
        rs.get("version"), rs.get("schema_version")))

    # C3. schema_candidates（A/B 双口径）
    sc = cli.call("schema_candidates", {"demand_id": did, "dataset": ds})
    subj_tables = sc.get("subject_tables") or []
    join_paths = sc.get("join_paths") or []
    miss = (sc.get("_debug") or {}).get("miss_reason") or sc.get("miss_reason") or ""
    if ds == "B":
        sc_pass = len(subj_tables) > 0
        check("%s C3 B库 schema_candidates.subject_tables 非空（条数=%d）" % (key, len(subj_tables)),
              sc_pass, "实际=0（A 库才允许空）", dataset=ds)
        for t in subj_tables[:4]:
            print("    - subject_tables 前 4：table=%s score=%s hits=%s" % (
                t.get("table"), t.get("score"),
                [h.get("kind") + ":" + h.get("needle", "")[:12] for h in (t.get("match_hits") or [])[:3]]))
    else:  # A 库：允许空，但 miss_reason 非空
        sc_pass = (len(subj_tables) == 0 and len(str(miss).strip()) > 0) or len(subj_tables) > 0
        check("%s C3 A库：subject_tables 非空 或 空+miss_reason 非空（n=%d miss_reason=%s）" % (
                  key, len(subj_tables), repr(str(miss)[:200])),
              sc_pass, "miss_reason=%r tables_n=%d" % (miss, len(subj_tables)), dataset=ds)
        if len(subj_tables) == 0 and str(miss).strip():
            print("    · A 库价值证据 · miss_reason 原文：%s" % str(miss)[:360])

    # C4. sql_context_pack 三版本齐全 + join_candidates relationship_index 合法
    pack = cli.call("sql_context_pack", {"demand_id": did, "dataset": ds})
    if pack.get("ok") is False:
        check("%s C4a sql_context_pack.ok=True" % key, False,
              pack.get("error")[:240], dataset=ds)
        return did
    sv = pack.get("schema_version")
    rv = pack.get("requirement_version")
    pv = pack.get("pack_version")
    check("%s C4a schema_version 非空且非空字符串" % key,
          isinstance(sv, str) and len(sv) >= 4,
          "实际=%r（类型=%s）" % (sv, type(sv).__name__), dataset=ds)
    check("%s C4b requirement_version 非空（整数或数值字符串）" % key,
          rv is not None and str(rv).isdigit() if isinstance(rv, (str, int)) else False,
          "实际=%r（类型=%s）" % (rv, type(rv).__name__), dataset=ds)
    check("%s C4c pack_version 非空字符串（sha8）" % key,
          isinstance(pv, str) and len(pv) >= 6,
          "实际=%r（长度=%s）" % (pv, len(pv) if isinstance(pv, str) else None), dataset=ds)
    print("    溯源版本三要素：schema_version=%s  requirement_version=%s  pack_version=%s"
          % (sv, rv, pv))

    join_cands = pack.get("join_candidates") or []
    man = cli.call("wren_manifest", {"dataset": ds})
    n_rel = len(man.get("relationships") or [])
    print("    MDL relationships 总条数 = %d（relationship_index 的合法上限 = %d）" % (n_rel, n_rel - 1 if n_rel else 0))
    bad_idxs = []
    for j in join_cands:
        idx = j.get("relationship_index")
        if (isinstance(idx, int) and 0 <= idx < n_rel) or (
            isinstance(idx, str) and idx.isdigit() and 0 <= int(idx) < n_rel
        ):
            continue
        bad_idxs.append((idx, j.get("name"), j.get("models")))
    check("%s C4d join_candidates 共 %d 条，relationship_index 全部 ∈ [0,%d)"
          % (key, len(join_cands), n_rel if n_rel else 1),
          len(bad_idxs) == 0, "越界项 = %s" % bad_idxs[:3], dataset=ds)
    for j in join_cands[:5]:
        print("    - join idx=%s  models=%s  joinType=%s" % (
            j.get("relationship_index"), j.get("models"), j.get("joinType")))
    return did


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="M4 端到端验收：结构化需求 + 上下文包")
    ap.add_argument("--url", default=os.getenv("GATEWAY_MCP_URL", "http://127.0.0.1:18080/mcp"))
    ap.add_argument("--keep", action="store_true", help="跑完不清理需求单")
    args = ap.parse_args()

    print("=" * 74)
    print("M4 端到端验收 —— 结构化需求 + 上下文包（§6 两条验收标准）")
    print("端点：%s   样例总数：%d  （B 库 %d 条 / A 库 %d 条）" % (
        args.url,
        len(demands_mod.SAMPLES),
        sum(1 for s in demands_mod.SAMPLES if s.get("dataset", "B") == "B"),
        sum(1 for s in demands_mod.SAMPLES if s.get("dataset", "B") == "A"),
    ))
    print("=" * 74)

    cli = McpClient(args.url)
    tools = cli.list_tools()
    required = [
        "demand_create", "demand_set_status",
        "analysis_first_round", "requirement_structured",
        "schema_candidates", "sql_context_pack", "wren_manifest",
    ]
    missing = [t for t in required if t not in tools]
    check("M4 七个工具均已注册", not missing, "缺：%s" % missing)
    print("    已注册工具名：%s" % "、".join(t for t in required if t in tools))

    made = []
    a_done, b_done = 0, 0
    try:
        for s in demands_mod.SAMPLES:
            ds = s.get("dataset", "B")
            if ds == "A":
                a_done += 1
            else:
                b_done += 1
            did = verify_sample(cli, s)
            if did:
                made.append(did)
    finally:
        if not args.keep and made:
            print("\n%s 清理演示数据" % ("-" * 70))
            for did in made:
                try:
                    r = cli.call("demand_set_status",
                                 {"demand_id": did, "status": "已退回",
                                  "note": "M4 验收现场清理"})
                    print("    %s -> %s" % (did, r.get("status")))
                except Exception as e:  # noqa: BLE001
                    print("    %s 状态流转失败：%s" % (did, str(e)[:120]))

    print("\n" + "=" * 74)
    print("【合计】共 %d 项断言，失败 %d 项" % (CHECKS[0], len(FAILS)))
    print("【B 库】共 %d 项断言，失败 %d 项（样例数=%d）" % (B_CHECKS[0], len(B_FAILS), b_done))
    for f in B_FAILS:
        print("     ✗ %s" % f)
    print("【A 库】共 %d 项断言，失败 %d 项（样例数=%d）" % (A_CHECKS[0], len(A_FAILS), a_done))
    for f in A_FAILS:
        print("     ✗ %s" % f)
    if FAILS:
        print("\033[31mM4 验收未通过（%d 项失败）\033[0m" % len(FAILS))
        return 1
    print("\033[32mM4 验收通过（A/B 两库合计 %d 断言 / 0 失败）\033[0m" % CHECKS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
