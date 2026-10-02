#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M4-2 live 端到端检查（经 MCP 真实调用，覆盖自证盖不到的 build 落库路径）。

自证脚本能证明 validate（纯函数）正确，但**盖不到**：
  - build 从 analysis_rounds / confirmations 取真实数据能否取到；
  - schema_version 的取法（查快照 / 失败再扫 / 仍无报错）；
  - version 只增不改（重复 build = version+1，旧版可回读）；
  - 落库 structured_requirements 是否真写入。

本脚本按真实链路走一遍：demand_create → analysis_first_round →
requirement_structured → requirement_get，并核对上述四点。

    python tools/m42_live_check.py [--url http://127.0.0.1:18080/mcp] [--dataset B]

退出码 0 = 全过。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import example_demands  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"
FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("    \033[32mPASS\033[0m %s" % name)
    else:
        print("    \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)
    return cond


class McpClient:
    def __init__(self, endpoint, timeout=90):
        self.endpoint = endpoint
        self.timeout = timeout
        self._sid = None
        self._id = 0
        self._opener = urllib.request.build_opener()

    def _rpc(self, payload, sid=None):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(),
                                     headers=headers)
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
                       "clientInfo": {"name": "m42-live-check", "version": "1.0.0"}}})
        self._sid = sid
        if sid:
            try:
                self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except Exception:
                pass
        return res

    def call(self, tool, args=None, _retry=True):
        if self._sid is None:
            self.handshake()
        self._id += 1
        try:
            res, sid = self._rpc({
                "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}}}, self._sid)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18080/mcp")
    ap.add_argument("--dataset", default="B")
    ap.add_argument("--sample", default="S2", help="用哪个 M3 样例（默认 S2 会员复购率）")
    args = ap.parse_args()

    cli = McpClient(args.url)

    sample = next((s for s in example_demands.SAMPLES if s["key"] == args.sample), None)
    if sample is None:
        print("找不到样例 %s" % args.sample)
        sys.exit(2)

    print("=" * 78)
    print("M4-2 live 端到端：%s（%s 库）· 样例 %s" % (sample["payload"]["title"], args.dataset, args.sample))
    print("=" * 78)

    # 1. 建需求
    d = cli.call("demand_create", dict(sample["payload"]))
    did = d.get("demand_id")
    check("demand_create 返回 demand_id", bool(did), str(d)[:200])
    if not did:
        sys.exit(1)
    print("         demand_id = %s" % did)

    # 2. 首轮语义分析（六槽位）
    rnd = cli.call("analysis_first_round", {"demand_id": did, "dataset": args.dataset})
    rno = rnd.get("round_no")
    slots = rnd.get("slots") or {}
    nonempty = [k for k, v in slots.items() if (v or {}).get("candidates")]
    check("analysis_first_round 产出轮次", bool(rno), str(rnd)[:200])
    if args.dataset == "B":
        check("B 库：六槽位至少有一项有候选（否则 build 无料可取）", len(nonempty) > 0,
              "有候选的槽位=%s" % nonempty)
    else:
        # A 库是裸库（语义描述覆盖极低），槽位大面积为空是**预期行为**；
        # 这里不判失败，只观测——关键在下一段：即使槽位全空，build 仍须产出合法对象。
        print("         (A 库：槽位全空属预期，下一段验 build 是否仍产出合法对象)")
    print("         round_no = %s | 有候选槽位 = %s" % (rno, nonempty))

    # 3. requirement_structured（首次 build）
    r1 = cli.call("requirement_structured", {"demand_id": did, "dataset": args.dataset})
    check("requirement_structured 返回对象（无 ok 键/或 ok 非 False）", r1.get("ok") is not False,
          str(r1)[:240])
    check("契约校验通过 contract_ok=true", r1.get("contract_ok") is True,
          "contract_errors=%s" % r1.get("contract_errors"))
    check("version 首版 = 1", r1.get("version") == 1, "version=%s" % r1.get("version"))
    check("schema_version 非空", bool(r1.get("schema_version")), "sv=%s" % r1.get("schema_version"))
    check("source_round 对齐分析轮次", r1.get("source_round") == rno,
          "source_round=%s round_no=%s" % (r1.get("source_round"), rno))
    check("status 默认为 待审核", r1.get("status") == "待审核", "status=%s" % r1.get("status"))

    # 契约顶层键集（证明 additionalProperties=false 不会误伤）
    top_keys = set(k for k in r1.keys() if k not in ("contract_ok", "contract_errors"))
    expect_keys = {"demand_id", "dataset", "version", "source_round", "schema_version",
                   "subject", "granularity", "time_semantics", "data_scope", "output_fields",
                   "aggregation_rules", "confirmed_facts", "evidence_chain",
                   "residual_risks", "status"}
    check("顶层键 == 契约 15 字段（无多无少）", top_keys == expect_keys,
          "多=%s 少=%s" % (sorted(top_keys - expect_keys), sorted(expect_keys - top_keys)))

    # 证据链与无源结论
    ev_len = len(r1.get("evidence_chain") or [])
    check("evidence_chain 非空（有源可回溯）", ev_len > 0, "条数=%d" % ev_len)
    print("         证据链 %d 条 | output_fields %d 项 | residual_risks %d 项 | aggregation_rules %d 项"
          % (ev_len, len(r1.get("output_fields") or []),
             len(r1.get("residual_risks") or []), len(r1.get("aggregation_rules") or [])))

    # 每个 SlotClaim 的 evidence 必须非空（无源断言为零）
    bad_claims = []
    for f in ("subject", "granularity", "time_semantics", "data_scope"):
        ev = (r1.get(f) or {}).get("evidence")
        if not ev:
            bad_claims.append(f)
    for i, it in enumerate(r1.get("output_fields") or []):
        if not (it or {}).get("evidence"):
            bad_claims.append("output_fields[%d]" % i)
    check("无源断言为零（所有 SlotClaim 都带证据）", len(bad_claims) == 0,
          "无源项=%s" % bad_claims)

    # 槽位缺失必须留痕：output_fields 为空时，residual_risks 里要能找到对应项
    if not (r1.get("output_fields") or []):
        miss = [x for x in (r1.get("residual_risks") or [])
                if (x or {}).get("missing_field") == "output_fields"]
        check("output_fields 为空时已在 residual_risks 留痕", len(miss) > 0,
              "residual_risks 中未找到 missing_field=output_fields")
    else:
        print("         (output_fields 非空，跳过缺失留痕检查)")

    # 4. requirement_get 回读一致
    g1 = cli.call("requirement_get", {"demand_id": did})
    check("requirement_get 回读 version=1", g1.get("version") == 1, "version=%s" % g1.get("version"))
    check("回读对象与 build 结果一致（version/schema_version）",
          g1.get("schema_version") == r1.get("schema_version")
          and g1.get("demand_id") == did, "g1=%s" % str(g1)[:160])

    # 5. 只增不改：再 build 一次 → version=2，且 v1 仍可回读
    r2 = cli.call("requirement_structured", {"demand_id": did, "dataset": args.dataset})
    check("重复 build = version+1（只增不改）", r2.get("version") == 2,
          "version=%s" % r2.get("version"))
    g_v1 = cli.call("requirement_get", {"demand_id": did, "version": 1})
    check("旧版 v1 仍可回读（未被覆盖）", g_v1.get("version") == 1, str(g_v1)[:160])
    g_latest = cli.call("requirement_get", {"demand_id": did})
    check("默认取最新版 = v2", g_latest.get("version") == 2, "version=%s" % g_latest.get("version"))

    # 6. 取不到的需求 → 返回明确错误而非崩
    bad = cli.call("requirement_get", {"demand_id": "NO-SUCH-DEMAND-XYZ"})
    check("不存在的需求：返回 ok=False 且带说明（不抛异常）",
          bad.get("ok") is False and bool(bad.get("error")), str(bad)[:200])

    print()
    print("=" * 78)
    if FAILS:
        print("❌ M4-2 live 检查失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ M4-2 live 端到端全过（共 %d 项断言）· demand_id=%s（保留在库，供 M5 链路复用）"
          % (CHECKS[0], did))
    sys.exit(0)


if __name__ == "__main__":
    main()
