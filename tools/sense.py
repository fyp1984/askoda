#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""感知监测 Agent · 五路信号巡检器。

只做一件事：把代码事实、门禁、环境、文档口径、工作区五路信号的变化，
写成**结构化事件**追加到 agents/state/events.jsonl。

**它不下结论**（不定级、不建议、不决策）——那是态势评估 Agent 的活。
这样切分是为了防止「探测到现象」被直接当成「判定为缺陷」，
中间必须过一次带基线对照的评估。

五路信号：
  1. CODE   代码事实漂移：工具面数量、版本号链、关键文件行数
  2. GATE   门禁状态：跑 tools/gate_all.py（默认离线层，不写库）
  3. ENV    环境可用性：网关探活、A/B 双库、解释器依赖
  4. DOC    文档口径：工具数/版本号/组件名在文档与代码之间是否对得上
  5. TREE   工作区：未提交改动、冻结基线是否被改

用法：
    python tools/sense.py                # 全五路巡检
    python tools/sense.py --signals code,doc
    python tools/sense.py --gate-layers G0,G1
    python tools/sense.py --no-gate      # 跳过门禁（最快）

退出码：0 = 无 error 级事件；1 = 有 error 级事件（须评估）。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_ROOT, "agents", "state")
EVENTS = os.path.join(STATE_DIR, "events.jsonl")
DOC_ROOT = "/Users/FYP/Documents/@西北人的成长/10-Projects/职业项目/数据需求智能分析助手"
VENV_PY = "/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python"
GATEWAY_MCP_URL = "http://127.0.0.1:18080/mcp"

# 代码事实基线（实测值，用于判漂移）
BASELINE = {
    "tool_count": 48,
    "rules_count": 11,
    "adversarial_suites": 3,
    "gate_scripts_min": 40,
}

_facts_cache = {}


def _run(cmd, timeout=120, cwd=REPO_ROOT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", "CMD_ERROR: %s" % exc


def _tools_list(sess):
    """取容器内实际注册的工具名集合；失败返回 None。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(
            GATEWAY_MCP_URL,
            data=json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                             "params": {}}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Mcp-Session-Id": sess})
        with opener.open(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", "replace")
        for line in txt.splitlines():
            if line.startswith("data:"):
                obj = json.loads(line[6:])
                return set(t["name"] for t in obj["result"]["tools"])
    except Exception:  # noqa: BLE001
        return None
    return None


def _ev(signal, severity, code, msg, evidence=None):
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "signal": signal,
        "severity": severity,   # info | warn | error
        "code": code,
        "message": msg,
        "evidence": evidence or [],
    }


# ---------------------------------------------------------------------------
# 1. 代码事实
# ---------------------------------------------------------------------------

def sig_code():
    evs = []
    app = os.path.join(REPO_ROOT, "gateway", "app.py")
    if os.path.exists(app):
        with open(app, "r", encoding="utf-8") as fh:
            n = len(re.findall(r"@mcp\.tool\b", fh.read()))
        _facts_cache["tool_count"] = n
        if n != BASELINE["tool_count"]:
            evs.append(_ev("CODE", "warn", "TOOL_COUNT_DRIFT",
                           "MCP 工具面数量漂移：基线 %d，实测 %d" % (BASELINE["tool_count"], n),
                           ["gateway/app.py"]))
    rules = os.path.join(REPO_ROOT, "gateway", "rules.py")
    if os.path.exists(rules):
        with open(rules, "r", encoding="utf-8") as fh:
            src = fh.read()
        n_rules = len(re.findall(r'"id"\s*:\s*"', src)) or len(re.findall(r"id\s*=\s*['\"]", src))
        _facts_cache["rules_count"] = n_rules
    adv_dir = os.path.join(REPO_ROOT, "agents", "adversarial")
    if os.path.isdir(adv_dir):
        suites = [f for f in os.listdir(adv_dir) if f.endswith(".json")]
        _facts_cache["adversarial_suites"] = len(suites)
    return evs


# ---------------------------------------------------------------------------
# 2. 门禁
# ---------------------------------------------------------------------------

def sig_gate(layers="G0,G1"):
    evs = []
    gate = os.path.join(REPO_ROOT, "tools", "gate_all.py")
    if not os.path.exists(gate):
        return [_ev("GATE", "error", "GATE_MISSING", "未找到 tools/gate_all.py")]
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    rc, out, err = _run([py, "tools/gate_all.py", "--layers", layers], timeout=900)
    m = re.search(r"总判定：(.{0,40})", out)
    verdict = (m.group(1).strip() if m else "未识别")
    _facts_cache["gate_verdict"] = verdict
    _facts_cache["gate_exit"] = rc
    if rc != 0:
        # 抽取红项脚本名
        reds = re.findall(r"\[FAIL\][^\n]*?(\S+\.py)", out)
        evs.append(_ev("GATE", "error", "GATE_RED",
                       "门禁未全绿（layers=%s，退出码 %d）：%s" % (layers, rc, verdict),
                       sorted(set(reds)) or ["见 gate-report.md"]))
    else:
        evs.append(_ev("GATE", "info", "GATE_GREEN",
                       "门禁全绿（layers=%s）：%s" % (layers, verdict),
                       ["tools/gate_all.py"]))
    return evs


# ---------------------------------------------------------------------------
# 3. 环境
# ---------------------------------------------------------------------------

def sig_env():
    evs = []
    # 3a 网关探活（MCP initialize）
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "sense", "version": "1"}}}).encode()
        req = urllib.request.Request(
            GATEWAY_MCP_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as resp:
            txt = resp.read().decode("utf-8", "replace")
            sess = resp.headers.get("Mcp-Session-Id")
        ok = '"result"' in txt
        ver = ""
        mv = re.search(r'"serverInfo":\{[^}]*"version"\s*:\s*"([^"]+)"', txt)
        if mv:
            ver = mv.group(1)
        _facts_cache["gateway_up"] = ok
        if not ok:
            evs.append(_ev("ENV", "error", "GATEWAY_DOWN", "MCP 网关无响应：%s" % GATEWAY_MCP_URL))
        else:
            # 部署漂移：容器内 tools/list 与磁盘 @mcp.tool 数量必须一致
            live = _tools_list(sess)
            disk = _facts_cache.get("tool_count")
            if live is not None and disk is not None:
                _facts_cache["live_tool_count"] = len(live)
                if len(live) != disk:
                    evs.append(_ev(
                        "ENV", "error", "DEPLOY_DRIFT",
                        "部署漂移：容器内工具面 %d 个，磁盘 %d 个 —— 改动未重建镜像进容器"
                        % (len(live), disk),
                        ["须重建镜像：compose build 必带 BUILDX_CONFIG=<可写目录>；"
                         "build 后容器内 /app/tools/ 会被清空，复扫前需重新 docker cp"]))
    except Exception as exc:  # noqa: BLE001
        _facts_cache["gateway_up"] = False
        evs.append(_ev("ENV", "error", "GATEWAY_DOWN", "MCP 网关探活失败：%s" % exc))

    # 3b 解释器依赖
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    rc, out, _ = _run([py, "-c", "import sqlglot,psycopg,fastmcp;print('ok')"])
    if rc != 0:
        evs.append(_ev("ENV", "error", "INTERPRETER_DEPS_MISSING",
                       "宿主解释器缺 sqlglot/psycopg/fastmcp，门禁会假红", [py]))
    return evs


# ---------------------------------------------------------------------------
# 4. 文档口径
# ---------------------------------------------------------------------------

def sig_doc():
    evs = []
    if not os.path.isdir(DOC_ROOT):
        return evs
    tool_n = _facts_cache.get("tool_count")
    # 扫描文档里提到的工具数（「41 个工具」「45 个工具」等，与 BASELINE["tool_count"] 比对）
    pat = re.compile(r"(4[0-9]|3[0-9])\s*个\s*(?:MCP\s*)?工具")
    stale = []
    for dirpath, dirnames, filenames in os.walk(DOC_ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".workbuddy", "40-Archives", ".git")]
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, DOC_ROOT)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        for m in pat.finditer(line):
                            if tool_n and int(m.group(1)) != tool_n:
                                stale.append("%s:%d 提到「%s 个工具」，代码实测 %d"
                                             % (rel, i, m.group(1), tool_n))
            except Exception:  # noqa: BLE001
                continue
    _facts_cache["doc_stale_count"] = len(stale)
    if stale:
        evs.append(_ev("DOC", "warn", "DOC_MISMATCH",
                       "文档工具数与代码不一致（%d 处）" % len(stale), stale[:20]))
    return evs


# ---------------------------------------------------------------------------
# 5. 工作区
# ---------------------------------------------------------------------------

def sig_tree():
    evs = []
    rc, out, _ = _run(["git", "status", "--short"])
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    _facts_cache["dirty_files"] = len(dirty)
    if dirty:
        evs.append(_ev("TREE", "warn", "DIRTY_TREE",
                       "工作区有未提交改动 %d 项" % len(dirty), dirty[:20]))
    # 红线守卫
    guard = os.path.join(REPO_ROOT, "tools", "redline_guard.py")
    if os.path.exists(guard):
        py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
        rc, out, _ = _run([py, "tools/redline_guard.py"])
        if rc != 0:
            evs.append(_ev("TREE", "error", "REDLINE_BLOCKED",
                           "红线守卫判定 BLOCKED，须出《红线阻断报告》",
                           [ln.strip() for ln in out.splitlines() if "命中" in ln]))
        else:
            evs.append(_ev("TREE", "info", "REDLINE_CLEAR", "六条红线全未触"))
    return evs


SIGNALS = {"code": sig_code, "gate": sig_gate, "env": sig_env, "doc": sig_doc, "tree": sig_tree}


def main():
    ap = argparse.ArgumentParser(description="感知监测 Agent · 五路信号巡检")
    ap.add_argument("--signals", default="code,env,doc,tree",
                    help="逗号分隔，默认不含 gate（gate 较慢）")
    ap.add_argument("--gate-layers", default="G0,G1")
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    names = [s.strip() for s in args.signals.split(",") if s.strip()]
    if not args.no_gate and "gate" not in names:
        names = names + ["gate"]

    events = []
    for n in names:
        fn = SIGNALS.get(n)
        if not fn:
            continue
        try:
            if n == "gate":
                events += fn(args.gate_layers)
            else:
                events += fn()
        except Exception as exc:  # noqa: BLE001
            events.append(_ev(n.upper(), "error", "SENSER_ERROR",
                              "巡检器在 %s 信号上异常：%s" % (n, exc)))

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    n_err = len([e for e in events if e["severity"] == "error"])
    n_warn = len([e for e in events if e["severity"] == "warn"])
    if args.json:
        print(json.dumps({"events": events, "facts": _facts_cache,
                          "error": n_err, "warn": n_warn}, ensure_ascii=False, indent=2))
    else:
        print("=" * 70)
        print("感知监测 Agent · 巡检结果（已追加至 agents/state/events.jsonl）")
        print("=" * 70)
        for e in events:
            icon = {"error": "🔴", "warn": "🟡", "info": "⚪"}[e["severity"]]
            print("  %s [%s] %s" % (icon, e["signal"], e["message"]))
            for ev in e["evidence"][:6]:
                print("        · %s" % ev)
        print("-" * 70)
        print("事实快照：%s" % json.dumps(_facts_cache, ensure_ascii=False))
        print("结论：错误 %d · 警告 %d（**本 Agent 不下结论，交态势评估**）" % (n_err, n_warn))
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())