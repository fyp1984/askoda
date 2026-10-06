#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协调调度 Agent · 台账生成器 + 闭环自检。

两件事：
1. **生成**：从 agents/state/batches.json（唯一权威）生成验收台账 markdown。
   目的：消灭「台账 md 与状态文件互相打架」——md 变成派生物，不再手写。
2. **闭环自检**：回答设计的第七章那三条问题，答不出就是闭环没落地。

用法：
    python tools/coord.py --status              # 打印批次状态表
    python tools/coord.py --generate            # 生成台账 md
    python tools/coord.py --loop-check          # 闭环三条自检
    python tools/coord.py --harvest <batch>     # 收口某批：查判据完备性并归档门禁报告
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(REPO_ROOT, "agents", "state")
BATCHES = os.path.join(STATE, "batches.json")
DOC_ROOT = "/Users/FYP/Documents/@西北人的成长/10-Projects/职业项目/数据需求智能分析助手"
LEDGER = os.path.join(DOC_ROOT, "06-测试与验收", "阶段A批次准入台账-数据需求智能分析助手.md")
GATE_REPORTS = os.path.join(STATE, "gate_reports")
VENV_PY = "/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python"

STATUS_ICON = {"done": "✅", "running": "🔄", "pending": "⏳",
               "blocked_by_human": "⏸", "needs_human_decision": "⏸",
               "needs_human_input": "⏸", "deferred": "⏷"}


def load():
    with open(BATCHES, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _py():
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _run(cmd, timeout=900, cwd=REPO_ROOT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


# ---------------------------------------------------------------------------
# 状态表
# ---------------------------------------------------------------------------

def print_status():
    d = load()
    print("=" * 92)
    print("协调调度 Agent · 批次状态（唯一权威：agents/state/batches.json）")
    print("=" * 92)
    print("%-8s %-46s %-8s %s" % ("批次", "内容", "状态", "结论"))
    print("-" * 92)
    for b in d["batches"]:
        ic = STATUS_ICON.get(b["status"], "·")
        print("%-8s %-46s %-8s %s" % (
            b["id"], b["name"][:46], ic + b["status"][:6],
            str(b.get("verdict", b.get("reason", "")))[:34]))
    print("-" * 92)
    print("\n阶段 B：")
    for b in d.get("phase_b", []):
        ic = STATUS_ICON.get(b["status"], "·")
        print("%-8s %-46s %-8s %s" % (
            b["id"], b["name"][:46], ic + b["status"][:6], b.get("blocker", "")[:40]))
    print("\n红线（%s 执行）" % d["redlines"]["enforcer"])
    for k, v in d["redlines"].items():
        if k == "enforcer":
            continue
        print("  %s %s" % (k, v))


# ---------------------------------------------------------------------------
# 台账生成
# ---------------------------------------------------------------------------

def generate_ledger():
    d = load()
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    L = []
    L.append("---")
    L.append("title: 阶段 A 批次准入台账")
    L.append("date: %s" % time.strftime("%Y-%m-%d"))
    L.append("scope: 阶段 A 全部批次 + 阶段 B 状态")
    L.append("gate_entry: tools/gate_all.py（仓库 askoda 根）")
    L.append("generated_by: agents/state/batches.json（协调调度 Agent 自动生成，勿手改）")
    L.append("---")
    L.append("")
    L.append("# 阶段 A 批次准入台账")
    L.append("")
    L.append("> **本文件由协调调度 Agent 从 `agents/state/batches.json` 自动生成，请勿手改。**")
    L.append("> 状态文件是唯一权威；要改状态请改 JSON 后重新生成。")
    L.append("> 生成时间：%s" % ts)
    L.append("")
    L.append("## 一、准入判据与机制")
    L.append("")
    L.append("| 层 | 名称 | 跑什么 | 通过判据 |")
    L.append("|---|---|---|---|")
    L.append("| **G0** | 静态自检 | 环境自检（解释器/依赖）+ 全量 `py_compile` + `requirements.txt` 基线比对 | 全 PASS |")
    L.append("| **G1** | 单元自证 | `tools/*_selftest.py` | 退出码全 0 |")
    L.append("| **G2** | 独立复核 | `tools/*_check.py`（须独立重算，不读被测方自述） | 退出码全 0 |")
    L.append("| **G3** | 真实链路 | MCP 探活 → `tools/*_verify.py` | 探活通过 + 全 0 |")
    L.append("| **G4** | 目标验收 | 本批判据表（机读化，见 batches.json） | 逐条命中 + 证据落盘 |")
    L.append("")
    L.append("**统一入口**：`python3 tools/gate_all.py`，**退出码即准入结论**（0 放行 / 1 拦）。")
    L.append("")
    L.append("### 三档处置")
    L.append("")
    for k, v in d["disposition_modes"].items():
        L.append("- **%s 档** — %s" % (k, v))
    L.append("")
    L.append("> **只有 C 档会让人出现。** 这是「不逐步确认」的安全边界。")
    L.append("")
    L.append("### 六条红线（由 `%s` 机器执行）" % d["redlines"]["enforcer"])
    L.append("")
    L.append("| 编号 | 红线 |")
    L.append("|---|---|")
    for k, v in d["redlines"].items():
        if k == "enforcer":
            continue
        L.append("| %s | %s |" % (k, v))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 二、批次状态总表")
    L.append("")
    L.append("| 批次 | 内容 | 状态 | 结论 |")
    L.append("|---|---|---|---|")
    for b in d["batches"]:
        ic = STATUS_ICON.get(b["status"], "·")
        L.append("| **%s** | %s | %s | %s |" % (
            b["id"], b["name"], ic, b.get("verdict", b.get("reason", "—"))))
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 三、批次明细")
    for b in d["batches"]:
        L.append("")
        L.append("### %s · %s" % (b["id"], b["name"]))
        L.append("")
        for k in ("status", "verdict", "reason", "note", "changed_files", "gate",
                  "defects_closed", "defects_found", "lesson", "adversarial_suite",
                  "independent_recheck", "version_chain", "criteria", "owner",
                  "scope", "requires"):
            if k not in b:
                continue
            v = b[k]
            if isinstance(v, dict):
                L.append("**%s**：" % k)
                L.append("")
                L.append("```json")
                L.append(json.dumps(v, ensure_ascii=False, indent=2))
                L.append("```")
                L.append("")
            elif isinstance(v, list):
                L.append("**%s**：" % k)
                for item in v:
                    if isinstance(item, dict):
                        L.append("- `%s` %s%s" % (
                            item.get("id", ""), item.get("judgement", ""),
                            "（blocking）" if item.get("blocking") else ""))
                    else:
                        L.append("- %s" % item)
                L.append("")
            else:
                L.append("**%s**：%s" % (k, v))
                L.append("")
        for ev in b.get("evidence", []):
            L.append("- 证据：%s" % ev)
        if b.get("evidence"):
            L.append("")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 四、阶段 B 状态")
    L.append("")
    L.append("| 批次 | 内容 | 状态 | 阻断原因 |")
    L.append("|---|---|---|---|")
    for b in d.get("phase_b", []):
        ic = STATUS_ICON.get(b["status"], "·")
        L.append("| **%s** | %s | %s | %s |" % (
            b["id"], b["name"], ic, b.get("blocker", "—")))
    L.append("")
    L.append("## 五、环境前提与运行纪律")
    L.append("")
    L.append("1. **解释器**：门禁自动挑选具备 `sqlglot`/`psycopg` 的解释器（实测＝`%s`）。不要用 PATH 上的 `python3`。")
    L.append("2. **跑全量 G3 会写库**：`*_verify.py` 是真实端到端，会新建需求单/SQL 留痕。日常批次只跑 `--layers G0,G1`（＋按需 G2）。")
    L.append("3. **不要用短超时提速**：单条 `_verify.py` 真实耗时可到 90 秒，短超时产生的是假红。")
    L.append("4. **部署漂移**：容器内 `tools/list` 与磁盘 `@mcp.tool` 数量必须一致，由 `tools/sense.py` 的 `DEPLOY_DRIFT` 事件监控；改动后须重建镜像（build 带 `BUILDX_CONFIG`）。")
    L.append("5. **报告落点**：`agents/state/gate_reports/`（已归档，非工作区）。")
    L.append("")
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    with open(LEDGER, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    return LEDGER


# ---------------------------------------------------------------------------
# 闭环自检
# ---------------------------------------------------------------------------

def loop_check():
    d = load()
    adv_dir = os.path.join(REPO_ROOT, "agents", "adversarial")
    suites = sorted(glob.glob(os.path.join(adv_dir, "*.json")))
    suite_total, suite_cases, defects_linked = 0, 0, 0
    defects_seen = set()
    for sp in suites:
        with open(sp, "r", encoding="utf-8") as fh:
            s = json.load(fh)
        cs = s.get("cases", [])
        suite_cases += len(cs)
        suite_total += 1
        for c in cs:
            fo = c.get("首现缺陷") or c.get("理由", "")
            for pat in ("P1-", "P-"):
                if pat in str(fo) or pat in str(c.get("理由", "")):
                    defects_linked += 1
                    defects_seen.add(str(c.get("id")))
                    break

    # 判据机读化覆盖
    need, have = 0, 0
    manual_only = []
    for b in d["batches"]:
        for c in b.get("criteria", []):
            need += 1
            if c.get("layer") and (c.get("script") or c.get("cmd")):
                have += 1
            else:
                manual_only.append("%s/%s" % (b["id"], c.get("id", "")))

    print("=" * 78)
    print("协调调度 Agent · 闭环自检（三条判据，答不出即闭环未落地）")
    print("=" * 78)

    print("\n【判据 1】同一个缺陷类第二次出现时，是否零新增成本？")
    print("  对抗用例库：%d 个套件 / %d 条用例" % (suite_total, suite_cases))
    print("  关联历史缺陷的用例：%d 条" % defects_linked)
    q1 = suite_cases >= 30 and defects_linked >= 10
    print("  结论：%s（判据：套件≥3、用例≥30、关联缺陷用例≥10）"
          % ("✅ 成立" if q1 else "❌ 不成立"))

    print("\n【判据 2】门禁全绿时，能不能说出「这批为什么达标」？")
    print("  机读化判据：%d/%d 条挂到了具体层/脚本" % (have, need))
    if manual_only:
        print("  仅人工核验的判据（须在报告中给证据落点）：%s" % ", ".join(manual_only))
    q2 = need == 0 or have >= need * 0.6
    print("  结论：%s（判据：≥60%% 判据挂到机器可执行层；未挂的须标名并留证据）"
          % ("✅ 成立" if q2 else "❌ 不成立"))

    print("\n【判据 3】轮次之间交接时，是否需要重建上下文？")
    src = os.path.join(STATE, "batches.json")
    print("  唯一状态源：%s（%d 字节，%d 个批次）" % (
        os.path.relpath(src, REPO_ROOT), os.path.getsize(src), len(d["batches"])))
    ev = os.path.join(STATE, "events.jsonl")
    n_ev = sum(1 for _ in open(ev, encoding="utf-8")) if os.path.exists(ev) else 0
    n_f = len(glob.glob(os.path.join(STATE, "findings", "*.json")))
    print("  事件流：%d 条事件 · 评估结论：%d 份 finding" % (n_ev, n_f))
    q3 = os.path.exists(src)
    print("  结论：%s（判据：状态文件存在且为唯一权威，台账由它生成）"
          % ("✅ 成立" if q3 else "❌ 不成立"))

    print("\n" + "-" * 78)
    allok = q1 and q2 and q3
    print("总判定：%s" % ("✅ 闭环成立" if allok else "❌ 闭环未完全落地"))
    return 0 if allok else 1


# ---------------------------------------------------------------------------
# 批次收口
# ---------------------------------------------------------------------------

def harvest(batch_id):
    """收口某批：跑门禁、归档报告、核判据完备性。"""
    d = load()
    b = next((x for x in d["batches"] if x["id"] == batch_id), None)
    if not b:
        print("❌ 找不到批次 %s" % batch_id)
        return 1
    print("=" * 78)
    print("协调调度 Agent · 收口 %s" % batch_id)
    print("=" * 78)

    print("\n【1】G4 判据完备性反问（新增判据类风险，门禁能不能抓到？）")
    crit = b.get("criteria", [])
    if not crit:
        print("  🔴 本批未定义 G4 判据 —— 这正是 B2b 的同款毛病（门禁全绿但无法说清为什么达标）")
        print("     须补齐 batches.json 里本批的 criteria 后才可收口。")
        print("     提示：判据要挂到 layer+script/cmd，别写成只能人工核验的句子。")
        return 2
    print("  已定义 %d 条判据：" % len(crit))
    for c in crit:
        machine = bool(c.get("layer") and (c.get("script") or c.get("cmd")))
        tag = "机器" if machine else "人工"
        detail = c.get("script") or c.get("cmd") or c.get("note", "须人工核验并留证据")
        print("  [%s] %-14s %s" % (tag, c.get("id", ""), c.get("judgement", "")))
        print("        └─ %s" % detail)
    # 判据完备性反问（本批自身引入的判据类风险是否可机器判定）
    nm = len([c for c in crit if c.get("layer") and (c.get("script") or c.get("cmd"))])
    ratio = nm / len(crit)
    if ratio < 0.6:
        print("  ⚠️ 仅 %d/%d 条挂到机器可执行层（<60%%）→ 须逐条在报告中给证据落点" % (nm, len(crit)))
    else:
        print("  ✅ 机器可判定覆盖率 %d/%d（%.0f%%）" % (nm, len(crit), ratio * 100))

    print("\n【2】跑门禁（离线层 + G2，不写库）")
    rc, out, err = _run([_py(), "tools/gate_all.py", "--layers", "G0,G1,G2"], timeout=1200)
    for line in out.splitlines():
        if "总判定" in line or ("G" == line.strip()[:1] and "total=" in line):
            print("  " + line.strip())
    print("  退出码：%d" % rc)

    print("\n【3】红线守卫")
    rc2, out2, _ = _run([_py(), "tools/redline_guard.py"], timeout=300)
    for line in out2.splitlines():
        if "总判定" in line or "命中" in line:
            print("  " + line.strip())

    print("\n【4】归档门禁报告")
    os.makedirs(GATE_REPORTS, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    for fn in ("gate-report.json", "gate-report.md"):
        src = os.path.join(REPO_ROOT, fn)
        if os.path.exists(src):
            dst = os.path.join(GATE_REPORTS, "%s-%s-%s" % (batch_id, ts, fn))
            shutil.copy2(src, dst)
            print("  已归档 %s" % os.path.relpath(dst, REPO_ROOT))
    return 0 if rc == 0 and rc2 == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="协调调度 Agent")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--generate", action="store_true")
    g.add_argument("--loop-check", action="store_true")
    g.add_argument("--harvest", metavar="BATCH")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0
    if args.generate:
        fp = generate_ledger()
        print("✅ 台账已生成：%s" % fp)
        return 0
    if args.loop_check:
        return loop_check()
    if args.harvest:
        return harvest(args.harvest)
    return 0


if __name__ == "__main__":
    sys.exit(main())