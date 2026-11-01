#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径独立性复核（G2）

守什么
------
1. **生效口径是不是好的**：视图/列/模型/时间语义逐条核对。
   口径写错不会报错、只会算错——「维度写成视图里没有的列」编译出来是空结果，
   「视图名拼错」要到用户点生成那一刻才炸。
2. **种子与生效是否对得上**：`db/credit/04_metrics.json` 现在是**初始种子**
   （重建环境时导入），真源是库。种子里写了、库里没有 ⇒ 说明有人改了文件没导入，
   重建一次口径就少一条，而且没有任何报错。

为什么走 MCP 而不是读库
----------------------
2026-11-01 起，口径的真源是 `dataset_registry.mdl_json`（网关规划器读的就是它），
界面改完当场生效。门禁要是直接读那一行，就成了「拿被测对象自己的数据证明它对」。
这里改成**走对外协议**（和 Agent 客户端同一条路）取生效口径，再用
`gateway/metric_spec.py` 的同一份判据复核结构 —— 判据同源、数据独立。

⚠️ 前提：网关在跑（与 `mcp_acceptance_check.py` 一致）。网关不可用时判**失败**，
不静默跳过——长期红的门禁等于没有门禁，静默跳过的门禁更糟。

用法：
    python3 tools/metric_registry_check.py
退出码 0 = 通过，1 = 有问题。
"""
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

import metric_spec  # noqa: E402

SEED_PATH = os.path.join(_ROOT, "db", "credit", "04_metrics.json")
URL = os.getenv("ASKODA_MCP_URL") or "http://127.0.0.1:18080/mcp"
DATASET = "C"

_SID = {"v": None}
_RID = {"v": 0}


def _rpc(method, params=None, notify=False):
    _RID["v"] += 1
    payload = {"jsonrpc": "2.0", "method": method}
    if not notify:
        payload["id"] = _RID["v"]
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    if _SID["v"]:
        req.add_header("Mcp-Session-Id", _SID["v"])
    with urllib.request.urlopen(req, timeout=30) as resp:
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


def _payload(res):
    r = (res or {}).get("result", {})
    sc = r.get("structuredContent")
    if sc:
        return sc.get("result", sc)
    for c in r.get("content", []) or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:  # noqa: BLE001
                return c["text"]
    return r


def _call(tool, args):
    return _payload(_rpc("tools/call", {"name": tool, "arguments": args}))


def main():
    if not os.path.exists(SEED_PATH):
        print("  FAIL  找不到种子文件：%s" % SEED_PATH)
        print("\nmetric_registry_check: 0/1")
        return 1

    try:
        _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientVersion": "1.0",
                            "clientInfo": {"name": "metric-check", "version": "1.0"}})
        _rpc("notifications/initialized", notify=True)
        live = _call("metric_list", {"dataset": DATASET})
        manifest = _call("wren_manifest", {"dataset": DATASET})
    except Exception as e:  # noqa: BLE001
        print("  FAIL  网关不可用（%s）：%s" % (URL, type(e).__name__))
        print("        口径复核需要读取**生效**口径，网关不在跑就无法判断线上到底是哪一版。")
        print("\nmetric_registry_check: 0/1")
        return 1

    problems = []
    if not isinstance(live, dict) or live.get("ok") is False:
        problems.append("metric_list 取不到生效口径：%s" % str(live)[:200])
        live = {}
    if not isinstance(manifest, dict) or not (manifest.get("models") or []):
        problems.append("wren_manifest 取不到模型清单（无法核对视图引用的对象）")
        manifest = {"models": []}

    section = {"dataset": DATASET, "version": live.get("version") or "",
               "views": live.get("views") or [], "metrics": live.get("metrics") or []}
    if not (section["views"] and section["metrics"]):
        problems.append("生效口径为空：该数据集还没登记口径（界面「口径维护」可登记）")
    else:
        problems.extend(metric_spec.static_check(section, manifest))

    # 种子 → 生效：种子里写的每一条，线上必须真的在
    try:
        with open(SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
    except (OSError, ValueError) as e:
        seed = {}
        problems.append("种子文件读不出来：%s" % e)
    orphans = metric_spec.seed_orphans(seed, section)
    if orphans:
        problems.append(
            "种子里有、生效口径里没有（改了文件没导入）：%s" % "、".join(orphans)
        )

    ok = not problems
    print("  OK    生效口径可用：%d 视图 / %d 口径（%s），种子 %d 条全在册"
          % (len(section["views"]), len(section["metrics"]),
             section["version"] or "-", len(seed.get("metrics") or []))
          if ok else "  FAIL  口径复核未通过（%d 处）" % len(problems))
    for p in problems:
        print("        - %s" % p)
    print("\nmetric_registry_check: %d/%d" % (0 if problems else 1, 1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
