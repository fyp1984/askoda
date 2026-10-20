#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁项数核对：文档声明的门禁项数 vs `gate_all.py` 实际产出的项数。

治的是哪一类病
--------------
`docs/GLOSSARY.md` 记着「导出脚本声明层数与实列不符」，而 `CONTRIBUTING.md`
的门禁表**今天仍在犯同一个错**（表里写 58/11/3/2，实跑是别的数）。
**门禁项数这四个字，在本项目里已经骗过至少两次。**

为什么手写数字必然漂移
----------------------
项数不是常量，而是**三层 glob 的函数**：
  · G0 = 环境自检 1 项 + `gateway/*.py` + `tools/*.py` + `bff/*.py` 各 1 项/文件
         + requirements 基线 1 项；
  · G1 = `tools/*_selftest.py` 的文件数；
  · G2 = `tools/*_check.py` 的文件数；
  · G3 = MCP 探活 1 项 + `tools/*_verify.py` 的文件数。
**任何人加一个 `_selftest.py`，项数就变了**，而文档不会自己跟着变。

本脚本的定位
------------
⚠️ **只判定，绝不写文档**（这条是硬约束，不是风格偏好）。
发现不一致时**如实报红并打印正确数字**，由人决定回填哪个文件。
⚠️ **判据自动改文档是危险动作**：它会让人以为「门禁能自愈」，
于是真正的漂移被悄悄抹平——而漂移正是本单要治的病。
判据一旦能改文档，「文档与实际一致」就不再是事实，而是判据自己写的谎。

核对哪两个文件（**为什么只有这两个**）
--------------------------------------
✅ 在范围内：`CONTRIBUTING.md` 门禁表、`README.md`「项目状态」段——**现行文档**，该跟着代码变。

❌ **故意排除**：文档库《阶段A总验收-…-2026-10-06.md》里的门禁项数
（当时记 112 项 · G0 75 · G1 10 · G2 17 · G3 10）。
**这是刻意排除，不是漏掉。** 三条理由：
  1. `tools/docsync.py` 的 `is_historic()` 已把带日期的 `06-测试与验收/`
     判为**历史快照，不同步**——本判据与它口径一致；
  2. 那是 **2026-10-06 的时点记录**，写 112 项**当时是对的**，
     今天「过期」不是它错；
  3. ⚠️ **改它等于篡改历史证据**。本项目铁律是「不改历史、只留最新版，
     新版入归档」，而历史快照的价值恰恰在于它记录的是**当时的事实**。
     判据若去"修正"它，等于用今天的口径覆盖昨天的记录。
⇒ 所以下面 `parse_*` 只认现行文档。看到「文档库那份没被核对」，那是设计，不是缺陷。

独立复算：口径从 `gate_all.py` 源码里读，不另写一套
--------------------------------------------------
⚠️ 本脚本**不自己重写一套 glob 口径**——那会重蹈"两个独立来源"的老路
（和「层数常量写两遍」同类）。做法：
  · 直接 `import gate_all`，复用它的 `REPO_ROOT` 与 `_find_scripts()`；
  · 用 **AST** 读出 `run_g0/g1/g2/g3` 里到底调了哪些采集器、各传什么 glob 模式，
    再据此计数。**若有人给 G0 加第 4 个 py_compile 目标，本脚本自动跟随**，
    不需要改这里。
判红口径 = 「文档数字 ≠ 按 gate_all 实现算出的数字」。

⚠️ 关于「自己算自己」：G2 = `len(glob("tools/*_check.py"))`，
而本文件运行时**已在盘上**，故 glob 结果**天然含自己**，**不需要减 1 再加 1**。
（曾有人想「好心」地减 1——那样反而算错。）

三种响亮失败（都**不静默放行**）
--------------------------
  1. 数字与实际不符 —— 本单主场景；
  2. 文档自相矛盾 —— 同一事实两处声明且值不同（如回填时新写法加了、旧写法忘删）；
  3. 声明读不到 / 采集器结构变了 —— 例如 `run_gN` 里出现未登记的采集器。
**「读不到就当通过」是判据最危险的失效模式**：它让格式变更伪装成一致。
故以上情形一律报错退出，不放行。

用法
----
    python3 tools/gate_count_check.py            # 人读格式
    python3 tools/gate_count_check.py --json     # 结构化

退出码：0 = 文档与实际一致；1 = 不一致（脱节）。
"""
import argparse
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gate_all as ga  # noqa: E402

# 采集器 → 它在 report_list 里追加几项。
#
# `_g0_py_compile` / `_find_scripts` 是**按文件数**展开的（一个文件一项），
# 其余是**一项定长**采集器。这里不写死「几项」：定长采集器的项数由
# `_count_appends()` 数它自己源码里的 `report_list.append(...)` 得出，
# 同样是读出来的，不是猜的。
EXPANDING = {"_g0_py_compile", "_find_scripts"}

# ⚠️ **消费方，不是采集方**：`_run_python_script(layer, s, report_list)` 在
# `for s in scripts:` 里，每个脚本吐 1 项——但这些脚本已经由 `_find_scripts`
# 计过一遍了。若把它当采集器（数它自己 append 几次 = 1），会算出 G1=1。
# 它的真实贡献 = 循环次数 = `_find_scripts` 命中数，已计入，故**跳过**。
CONSUMERS = {"_run_python_script"}

# 与项数无关的字符串工具（对 report_list 零贡献），显式忽略。
IGNORED = {"_repo_rel"}

# 一项定长采集器（各写 1 条 report），项数由 `_count_appends` 数出来，不写死。
FIXED_COLLECTORS = {
    "_g0_env_selfcheck",      # G0：子解释器依赖自检
    "_g0_requirements_baseline",  # G0：requirements.txt 基线核对
    "_gateway_health_probe",  # G3：MCP 探活（这就是 G3=2 里"另一项"的来源）
}

LAYERS = ("G0", "G1", "G2", "G3")
RUNNER = {"G0": "run_g0", "G1": "run_g1", "G2": "run_g2", "G3": "run_g3"}

DOC_CONTRIBUTING = os.path.join(ROOT, "CONTRIBUTING.md")
DOC_README = os.path.join(ROOT, "README.md")


# ---------------------------------------------------------------------------
# 一、按 gate_all.py 的实现算实际项数
# ---------------------------------------------------------------------------
def _count_appends(func_name, tree):
    """数某个采集器函数体里 `report_list.append(...)` 出现了几次。

    为什么数而不写死：定长采集器（env_selfcheck / requirements_baseline /
    探活）各追加几项，是 gate_all 的实现细节。**数出来**而不是假定，
    将来它若改成追加多项，本脚本自动跟随。
    """
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    if fn is None:
        return None
    n = 0
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "report_list"):
            n += 1
    return n


def _gate_all_ast():
    with open(os.path.join(HERE, "gate_all.py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _calls_in(tree, func_name):
    """取 run_gN 里与「项数」有关的调用 [(被调名, 字符串字面量参数)]，按源码顺序。

    ⚠️ 踩过的两个坑（都是靠「实跑 gate_all 交叉核对」抓到的，不是读代码看出来的）：
      ① 只扫函数体顶层 ⇒ G1/G2/G3 的调用写在 `for` 循环体里，一个都找不到，
         算出 G1/G2/G3 全 0。必须 `ast.walk` 递归。
      ② 只认 `Expr(Call(...))`（裸调用语句）⇒ 漏掉全部采集器：
         `run_g1` 里真正的入口是**赋值** `scripts = _find_scripts("*_selftest.py")`，
         而裸调用 `run_g1` 里的 `_run_python_script("G1", s, ...)` 是**消费方**
         （每个脚本吐 1 项，项数已由 `_find_scripts` 计过），
         认它就会算出 G1=1（一个循环调用点）而非真实脚本数。
         故两种形态都收：`Expr(Call)` 与 `Assign(value=Call)`。
    """
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    if fn is None:
        raise AssertionError("gate_all.py 里找不到 %s()——门禁实现结构变了，"
                             "本判据需同步" % func_name)
    found = []
    for node in ast.walk(fn):
        call = None
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], ast.Name)
              and isinstance(node.value, ast.Call)):
            call = node.value          # 形如 `scripts = _find_scripts("...")`
        if call is None or not isinstance(call.func, ast.Name):
            continue
        args = [a.value for a in call.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        found.append((call.lineno, call.func.id, args))
    found.sort(key=lambda t: t[0])       # ast.walk 是 BFS，顺序不保证
    return [(name, args) for _, name, args in found]


def compute_actual(tree=None, script_lister=None):
    """按 gate_all.py 的实现算出每层实际项数。

    `script_lister(pattern) -> list[str]` 默认复用 `gate_all._find_scripts`，
    自测时注入假实现以模拟「新增一个脚本」而不必真动文件系统。
    """
    tree = tree if tree is not None else _gate_all_ast()
    lister = script_lister or ga._find_scripts
    counts, detail = {}, {}
    for layer in LAYERS:
        total = 0
        parts = []
        for name, args in _calls_in(tree, RUNNER[layer]):
            if name in IGNORED:
                continue
            if name in CONSUMERS:
                # 消费方：每个脚本 1 项，项数已由 _find_scripts 计过
                parts.append("%s（消费方，项数已计入 _find_scripts，不重复计）" % name)
                continue
            if name in EXPANDING:
                pattern = args[0]
                if name == "_find_scripts":
                    n = len(lister(pattern))
                    files = [os.path.basename(f) for f in lister(pattern)]
                else:  # _g0_py_compile：模式相对 REPO_ROOT
                    import glob as _glob
                    hits = sorted(_glob.glob(os.path.join(ga.REPO_ROOT, pattern)))
                    n = len(hits)
                    files = [os.path.relpath(h, ga.REPO_ROOT) for h in hits]
                total += n
                parts.append("%s(%s)=%d" % (name, pattern, n))
                detail["%s::%s" % (layer, pattern)] = files
            else:
                # ⚠️ 白名单而非「凡是不认识的都当采集器」：
                # run_g3 里还有 `rel = _repo_rel(s)` 这类**字符串工具调用**，
                # 它对 report_list 零贡献（`_count_appends` 会返回 0）。
                # 放它进来只会让明细多一行 `_repo_rel=0` 误导人。
                # 判据的价值全在明细可信，故只认显式登记的采集器；
                # 新增采集器忘了登记 ⇒ 这里报「判据需同步」（响亮失败，好过静默少算）。
                if name not in FIXED_COLLECTORS:
                    raise AssertionError(
                        "gate_all.py 的 %s() 里出现未登记的调用 %s()——"
                        "若它是新采集器，请登记进 gate_count_check.FIXED_COLLECTORS；"
                        "若它不产生门禁项，请登记进 CONSUMERS/IGNORED。"
                        "（宁可报错，也不静默漏算项数）" % (RUNNER[layer], name))
                n = _count_appends(name, tree)
                total += n
                parts.append("%s=%d" % (name, n))
                detail["%s::%s" % (layer, name)] = n
        counts[layer] = total
        detail[layer] = parts
    counts["TOTAL"] = sum(counts[l] for l in LAYERS)
    # README 还声明了「日常只跑 G0/G1，合计 N 项」
    counts["DAILY"] = counts["G0"] + counts["G1"]
    return counts, detail


# ---------------------------------------------------------------------------
# 二、读文档里声明的数字
# ---------------------------------------------------------------------------
def parse_contributing(path=DOC_CONTRIBUTING):
    """读 CONTRIBUTING.md 门禁表：`| G0 | 描述 | 58 | 否 |` → {G0:58,...}。

    一行都读不到 ⇒ **抛错**，不静默返回空 dict。
    「读不到就当通过」是判据最危险的失效模式：表被改格式时会假装一致。
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    found = {}
    for m in re.finditer(r"^\|\s*(G[0-3])\s*\|[^|]*\|\s*(\d+)\s*\|", text, re.M):
        found[m.group(1)] = int(m.group(2))
    missing = [l for l in LAYERS if l not in found]
    if missing:
        raise AssertionError(
            "CONTRIBUTING.md 门禁表读不到 %s 的项数（表头格式可能已变：期望 `| G0 | 描述 | 58 | 否 |`）。"
            "本判据宁可直接报错，也不静默放行。" % "、".join(missing))
    return found


def _parse_daily_all(text):
    """抽出 README 里**所有**「日常项数」声明，返回 [(原文, 数值)]。

    ⚠️ 为什么要「全部」而不是 `re.search` 取第一个（本脚本初版就栽在这）：
    回填数字时常见两种写法并存——新写法嵌在门禁那行里
    （`…（日常只跑 G0+G1 共 80）`），旧写法留在下一行
    （`（日常只跑 G0/G1，合计 60 项）`）。
    `re.search` 会**静默取到先出现的那条**（往往是已过期的旧值），
    于是判据报出一个「文档写 60」的假脱节，而人真正该看到的是
    「**文档自己前后矛盾**」——那才是要修的东西。
    所以这里全收，交给 `parse_readme` 去判冲突。
    """
    pats = [
        r"日常只跑\s*G0\+G1\s*共\s*(\d+)",          # 行内新写法
        r"日常只跑\s*G0/G1[，,]?\s*合计\s*(\d+)\s*项",  # 独立行旧写法
    ]
    found = []
    for p in pats:
        for m in re.finditer(p, text):
            found.append((m.group(0), int(m.group(1))))
    return found


def parse_readme(path=DOC_README):
    """读 README「门禁 N 项全绿：G0 静态 a · G1 单元 b · G2 独立复核 c · G3 真实链路 d」。

    一并读出**合计**（N 项全绿）与**日常项数**（G0+G1）。
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(
        r"门禁\s*\*\*(\d+)\s*项全绿\*\*："
        r"G0\s*静态\s*(\d+)\s*·\s*G1\s*单元\s*(\d+)\s*·\s*"
        r"G2\s*独立复核\s*(\d+)\s*·\s*G3\s*真实链路\s*(\d+)", text)
    if not m:
        raise AssertionError(
            "README.md 读不到门禁项数声明（期望 `门禁 **N 项全绿**：G0 静态 a · "
            "G1 单元 b · G2 独立复核 c · G3 真实链路 d`）。"
            "本判据宁可直接报错，也不静默放行。")
    out = {"G0": int(m.group(2)), "G1": int(m.group(3)),
           "G2": int(m.group(4)), "G3": int(m.group(5)),
           "TOTAL": int(m.group(1))}
    daily = _parse_daily_all(text)
    if not daily:
        raise AssertionError(
            "README.md 读不到「日常项数」声明（期望 `日常只跑 G0+G1 共 N` 或 "
            "`日常只跑 G0/G1，合计 N 项`）。本判据宁可直接报错，也不静默放行。")
    values = {v for _, v in daily}
    if len(values) > 1:
        # ⚠️ 文档自相矛盾：这比「数字过期」更严重，必须响亮报出，
        # 且**报出全部候选**，让人一眼看到是哪两处写法打架。
        raise AssertionError(
            "README.md 里存在**互相矛盾**的「日常项数」声明：%s。"
            "⇒ 文档自身前后不一致（通常是回填时新写法加了、旧写法没删）。"
            "请删掉过期的那一处，保留唯一一处。"
            % "；".join("「%s」=%d" % (t, v) for t, v in daily))
    out["DAILY"] = daily[0][1]
    return out


# ---------------------------------------------------------------------------
# 三、比对
# ---------------------------------------------------------------------------
def compare(actual, declared):
    """返回不一致清单 [(层, 声明值, 实际值)]；一致则空。"""
    keys = LAYERS + ("TOTAL", "DAILY")
    bad = []
    for k in keys:
        if k not in declared:
            continue
        if declared[k] != actual[k]:
            bad.append((k, declared[k], actual[k]))
    return bad


def _fmt_bad(k, d, a):
    label = {"TOTAL": "合计", "DAILY": "日常(G0+G1)"}.get(k, k)
    return "  ✗ %-12s 文档写 %-4s 实际 %-4s（差 %+d）" % (
        label, d, a, a - d)


def main():
    ap = argparse.ArgumentParser(description="门禁项数核对（文档声明 vs 实跑）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    try:
        actual, detail = compute_actual()
        declared = {"CONTRIBUTING.md": parse_contributing(),
                    "README.md": parse_readme()}
    except AssertionError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)},
                             ensure_ascii=False, indent=2))
        else:
            print("[!!] 判据自身失效：%s" % e)
        return 1

    all_bad = []
    lines = []
    for doc, dec in declared.items():
        bad = compare(actual, dec)
        all_bad.extend((doc,) + b for b in bad)
        if args.json:
            continue
        lines.append("")
        lines.append("── %s" % doc)
        for k in LAYERS + ("TOTAL", "DAILY"):
            if k not in dec:
                continue
            label = {"TOTAL": "合计", "DAILY": "日常(G0+G1)"}.get(k, k)
            mark = "✓" if dec[k] == actual[k] else "✗"
            lines.append("  %s %-12s 文档 %-4s 实际 %-4s"
                         % (mark, label, dec[k], actual[k]))
        for k, d, a in bad:
            lines.append(_fmt_bad(k, d, a))

    ok = not all_bad
    if args.json:
        print(json.dumps({
            "ok": ok,
            "actual": actual,
            "declared": declared,
            "mismatches": [{"doc": b[0], "key": b[1], "declared": b[2],
                            "actual": b[3]} for b in all_bad],
            "actual_breakdown": detail,
            "checked_docs": ["CONTRIBUTING.md", "README.md"],
            "excluded_docs": {
                "文档库带日期的验收单（如 阶段A总验收-2026-10-06.md）":
                    "历史快照：docsync.is_historic() 判其为时点记录，"
                    "改它等于篡改历史证据，故刻意不核对",
            },
            "writes_documents": False,
        }, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    print("=" * 78)
    print("门禁项数核对 · 文档声明 vs gate_all.py 实际")
    print("  口径来源：import gate_all + AST 读 run_g0/g1/g2/g3（不另写一套 glob）")
    print("=" * 78)
    print("实际项数明细：")
    for layer in LAYERS:
        print("  %s = %d" % (layer, actual[layer]))
        for part in detail[layer]:
            print("        %s" % part)
    print("  合计 = %d    日常(G0+G1) = %d" % (actual["TOTAL"], actual["DAILY"]))
    print("")

    if ok:
        print("总判定：✅ 一致——文档声明的门禁项数与实际相符。")
        print()
        print("判据覆盖的文档（⚠️ 下方「不核对」是刻意排除，非遗漏）：")
        print("  核对  · CONTRIBUTING.md 门禁表（现行）")
        print("  核对  · README.md「项目状态」段（现行）")
        print("  不核对 · 文档库带日期的《阶段A总验收…》等历史快照——")
        print("          docsync.is_historic() 已判其为时点记录，写当时的项数是对的；")
        print("          改它等于篡改历史证据（铁律：不改历史，只留最新版）。")
        print("  ⚠️ 本判据只判定、不写文档——数字由人回填。")
        return 0

    print("总判定：❌ 脱节——以下数字与实际不符（判据不做写操作，请人工回填）：")
    for doc, k, d, a in all_bad:
        print("  [%s]%s" % (doc, _fmt_bad(k, d, a).rstrip()))
    print("")
    print("  ⇒ 正确数字：G0=%d G1=%d G2=%d G3=%d 合计=%d 日常=%d"
          % (actual["G0"], actual["G1"], actual["G2"], actual["G3"],
             actual["TOTAL"], actual["DAILY"]))
    print("  ⚠️ 派生关系：G1/G2/G3 随 `tools/*_selftest.py`/`*_check.py`/`*_verify.py`")
    print("     增删而变；G0 随 `gateway|bff|tools` 下 .py 文件数变化（含本文件）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())