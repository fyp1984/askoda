#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""态势评估 Agent · 基线对照器（归属判定器）。

解决的问题：判定一个缺陷是「本批引入」还是「既有缺陷」。
这是项目里最容易出错、也最必须机器化的一步——
B2b 的 P1-13 就是靠跑基线对照才坐实为「既有缺陷」，否则会被算成新引入，
进而导致错误的返工与错误的话术。

做法（与项目既定纪律一致）：
    git archive HEAD gateway | tar -x -C /tmp/<baseline>
    用 PYTHONPATH 指向基线 gateway，同入口、同解释器、同需求，再跑一次，
    直接比对输出，不看被测方自述。

用法：
    # 准备基线树（一次性）
    python tools/baseline_diff.py --prepare

    # 对照一批探针 SQL：分别跑基线与现网，逐条比对
    python tools/baseline_diff.py --probes probes.json

    # 模块级对照（示例：sql_plan 的 time_field）
    python tools/baseline_diff.py --module probe_timefield --demands SOO1,ZHAV,NF5O,JQT9

输出：agents/state/findings/<ts>-baseline.json，并打印逐条比对表。
退出码：0 = 全部一致或差异已被标注；1 = 对照失败。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(REPO_ROOT, "agents", "state")
FINDINGS = os.path.join(STATE_DIR, "findings")
VENV_PY = "/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python"
BASE_DIR = "/tmp/askoda_baseline"


def _py():
    return VENV_PY if os.path.exists(VENV_PY) else sys.executable


def _env(extra_path=None, with_mdl=True):
    e = dict(os.environ)
    if with_mdl:
        e["MDL_A_PATH"] = os.path.join(REPO_ROOT, "wren-docker", "workspace", "mdl.json")
        e["MDL_B_PATH"] = os.path.join(REPO_ROOT, "wren-docker-b", "workspace", "mdl.json")
    e["PYTHONPYCACHEPREFIX"] = "/tmp/pyc_bl"
    if extra_path:
        e["PYTHONPATH"] = extra_path
    return e


def prepare_baseline():
    """从 HEAD 导出基线代码树。"""
    if os.path.isdir(BASE_DIR):
        shutil.rmtree(BASE_DIR, ignore_errors=True)
    os.makedirs(BASE_DIR, exist_ok=True)
    rc, out, err = _run_pipe(["git", "archive", "HEAD", "gateway"])
    if rc != 0:
        return False, "git archive 失败：%s" % err
    p = subprocess.run(["tar", "-x", "-C", BASE_DIR], input=out,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return False, "tar 解包失败：%s" % p.stderr
    return True, "基线已导出至 %s" % BASE_DIR


def _run_pipe(cmd, cwd=REPO_ROOT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


def _run_py(code, pythonpath=None):
    """在指定 PYTHONPATH 下跑一段 Python，返回 (rc, stdout, stderr)。"""
    env = _env(pythonpath)
    try:
        p = subprocess.run([_py(), "-c", code], capture_output=True, text=True,
                           timeout=300, env=env, cwd=REPO_ROOT)
        return p.returncode, p.stdout, p.stderr
    except Exception as exc:  # noqa: BLE001
        return 127, "", str(exc)


# ---------------------------------------------------------------------------
# 对照 1：规则命中（基线 vs 现网，同一批 SQL）
# ---------------------------------------------------------------------------

PROBE_RULES = r"""
import json, sys, sqlglot
import rules as R
PROBES = json.loads(sys.argv[1])
out = []
for name, sql in PROBES:
    try:
        st = sqlglot.parse_one(sql, read="postgres")
        idx = R.build_mdl_index("A")
        res = R.evaluate_all(st, {"dataset":"A","mdl_index":idx,"requirement":None})
        r1 = len([x for x in res if x.get("rule")=="ONE_TO_MANY_UNHANDLED"])
        out.append({"name": name, "r1_hits": r1,
                    "rules": sorted({x.get("rule") for x in res})})
    except Exception as exc:
        out.append({"name": name, "error": "%s: %s" % (type(exc).__name__, str(exc)[:120])})
print("@@JSON@@" + json.dumps(out, ensure_ascii=False))
"""

PROBE_TIMEFIELD = r"""
import json, sys
import sqlgen, demand
PROBES = json.loads(sys.argv[1])
out = []
for did in PROBES:
    try:
        p = sqlgen.plan(demand_id=did)
        out.append({"demand_id": did,
                    "chosen_table": p.get("chosen_table"),
                    "time_field": p.get("time_field"),
                    "scope_models": p.get("time_field_scope_models")})
    except Exception as exc:
        out.append({"demand_id": did, "error": "%s: %s" % (type(exc).__name__, str(exc)[:150])})
print("@@JSON@@" + json.dumps(out, ensure_ascii=False))
"""


def _extract_json(stdout):
    for line in stdout.splitlines():
        if line.startswith("@@JSON@@"):
            return json.loads(line[len("@@JSON@@"):])
    return None


def compare_probe(probe_code, payload, label):
    """同一探针分别跑基线与现网，逐条比对。"""
    arg = json.dumps(payload, ensure_ascii=False)
    # 构造带 argv[1] 的调用：写成临时脚本
    tmp = "/tmp/_bl_probe.py"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(probe_code)
    code = "import sys;sys.argv=['x',%r];exec(open(%r).read())" % (arg, tmp)

    base_py = os.path.join(BASE_DIR, "gateway")
    rc_b, out_b, err_b = _run_py(code, pythonpath=base_py)
    rc_c, out_c, err_c = _run_py(code, pythonpath=os.path.join(REPO_ROOT, "gateway"))

    base = _extract_json(out_b)
    cur = _extract_json(out_c)
    if base is None or cur is None:
        return None, "基线/现网至少一侧无输出。基线rc=%s 现网rc=%s\n%s\n%s" % (
            rc_b, rc_c, err_b[-300:], err_c[-300:])

    rows, changed = [], []
    for b, c in zip(base, cur):
        key = b.get("name") or b.get("demand_id")
        bv = {k: v for k, v in b.items() if k != "name"}
        cv = {k: v for k, v in c.items() if k != "name"}
        same = bv == cv
        rows.append({"key": key, "baseline": bv, "current": cv, "same": same})
        if not same:
            changed.append(key)
    return {"label": label, "rows": rows, "changed": changed}, None


def main():
    ap = argparse.ArgumentParser(description="态势评估 Agent · 基线对照器")
    ap.add_argument("--prepare", action="store_true", help="导出 HEAD 基线树")
    ap.add_argument("--module", default=None, choices=["rules", "timefield"],
                    help="对照模块")
    ap.add_argument("--probes", default=None, help="探针 JSON 文件（[{name,sql}] 或 [demand_id]）")
    ap.add_argument("--demands", default=None, help="逗号分隔的需求 id")
    ap.add_argument("--auto", action="store_true",
                    help="默认对照集：A 库 R1 方向 6 例 + 4 个历史需求 time_field")
    args = ap.parse_args()

    if args.prepare:
        ok, msg = prepare_baseline()
        print(("✅ " if ok else "❌ ") + msg)
        return 0 if ok else 1

    if not os.path.isdir(os.path.join(BASE_DIR, "gateway")):
        ok, msg = prepare_baseline()
        if not ok:
            print("❌ " + msg)
            return 1
        print("（已自动准备基线：%s）" % msg)

    results = []

    R1_PROBES = [
        ["A2-1 链式 SUM(oi.subtotal)", "SELECT o.customer_id, SUM(oi.subtotal) FROM orders o JOIN order_items oi ON oi.order_id=o.id GROUP BY o.customer_id"],
        ["A2-2 双分支 SUM(oi.subtotal)", "SELECT o.customer_id, SUM(oi.subtotal) FROM orders o JOIN order_items oi ON oi.order_id=o.id JOIN customers c ON c.id=o.customer_id GROUP BY o.customer_id"],
        ["A1 两表 SUM(oi.subtotal)", "SELECT oi.order_id, SUM(oi.subtotal) FROM order_items oi GROUP BY oi.order_id"],
        ["真阳性 SUM(o.total_amount)", "SELECT o.customer_id, SUM(o.total_amount) FROM orders o JOIN order_items oi ON oi.order_id=o.id GROUP BY o.customer_id"],
        ["真阳性 COUNT(*)", "SELECT o.customer_id, COUNT(*) FROM orders o JOIN order_items oi ON oi.order_id=o.id GROUP BY o.customer_id"],
        ["真阳性 SUM(p.list_price)", "SELECT p.name, SUM(p.list_price) FROM orders o JOIN order_items oi ON oi.order_id=o.id JOIN products p ON p.id=oi.product_id GROUP BY p.name"],
    ]

    if args.module in (None, "rules") and not args.probes:
        r, err = compare_probe(PROBE_RULES, R1_PROBES, "R1 方向判据（A 库 6 例）")
        if err:
            print("❌ " + err)
            return 1
        results.append(r)

    if args.module in (None, "timefield") and not args.probes:
        demands = (args.demands.split(",") if args.demands
                   else ["DR-20261002-SOO1", "DR-20261002-ZHAV",
                         "DR-20261002-NF5O", "DR-20260930-JQT9"])
        r, err = compare_probe(PROBE_TIMEFIELD, demands, "sql_plan time_field（4 需求）")
        if err:
            print("❌ " + err)
            return 1
        results.append(r)

    if args.probes:
        with open(args.probes, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data and isinstance(data[0], dict):
            r, err = compare_probe(PROBE_RULES, [[d.get("name"), d.get("sql")] for d in data],
                                   "自定义规则探针")
        else:
            r, err = compare_probe(PROBE_TIMEFIELD, data, "自定义需求探针")
        if err:
            print("❌ " + err)
            return 1
        results.append(r)

    print("=" * 78)
    print("态势评估 Agent · 基线对照（基线 = HEAD，现网 = 工作区；同入口同解释器）")
    print("=" * 78)
    verdict = {"same": [], "changed": []}
    for res in results:
        print("\n【%s】" % res["label"])
        for row in res["rows"]:
            key = row["key"]
            mark = "  一致  " if row["same"] else "★ 差异★"
            print("  %s %s" % (mark, key))
            if not row["same"]:
                print("      基线: %s" % json.dumps(row["baseline"], ensure_ascii=False)[:160])
                print("      现网: %s" % json.dumps(row["current"], ensure_ascii=False)[:160])
        if res["changed"]:
            verdict["changed"].append({"module": res["label"], "keys": res["changed"]})
            print("  → 归属判定：这些差异是**本批引入**（改动前后行为不同）")
        else:
            print("  → 归属判定：**既有行为，未因本批改变**（若现象本就存在，则属既有缺陷）")

    os.makedirs(FINDINGS, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = {
        "agent": "assess.baseline_diff",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "baseline_ref": "HEAD",
        "results": results,
        "verdict": verdict,
    }
    fp = os.path.join(FINDINGS, "%s-baseline.json" % ts)
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("\n证据落盘：%s" % os.path.relpath(fp, REPO_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())