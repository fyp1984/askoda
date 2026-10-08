#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""综合决策 Agent · 文档口径同步器。

解决「文档互相打架」与「同源旧口径残留」两类问题：
代码变了（工具数、版本号、组件名、判据数），文档没跟上；
或者改了源头文件，下游引用还指着旧口径。

两个模式：
  --scan     只扫，列出不一致清单（默认，**只读，不改任何文件**）
  --sync     按映射批量替换（**须显式传 --yes** 才落盘）

安全设计：
  · 扫描永远只读；sync 必须 --yes，避免误改
  · 跳过归档目录（40-Archives）与冻结基线相关文件
  · 每次替换都记 evidence（文件:行号），可逐条回溯
  · 不碰任何代码文件（只处理 .md）
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

DOC_ROOT = "/Users/FYP/Documents/@西北人的成长/10-Projects/职业项目/数据需求智能分析助手"
SKIP_DIRS = {".git", ".workbuddy", "40-Archives", "node_modules", "__pycache__", ".venv"}
# 冻结基线文件不得改内容（含 sha256 登记）
FROZEN_HINT = "实施基线v1冻结清单"

# 事实源（须与代码实测一致）
FACTS = {
    "tool_count": 48,          # gateway/app.py 的 @mcp.tool 数量
    "gate_scripts": 40,        # tools/ 下门禁类脚本数（下限）
    "rules_count": 11,         # REGISTRY 条数
    "adversarial_cases": 50,   # 对抗用例库总用例数
}


def _iter_md():
    for dirpath, dirnames, filenames in os.walk(DOC_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".md"):
                yield os.path.join(dirpath, fn)


def scan():
    """扫出与代码事实不一致的文档表述。返回清单。"""
    issues = []
    # 规则 1：工具数
    pat_tool = re.compile(r"(\d{2})\s*个\s*(?:MCP\s*)?工具")
    for fp in _iter_md():
        rel = os.path.relpath(fp, DOC_ROOT)
        frozen = FROZEN_HINT in fn_text(fp)
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines, 1):
            if is_subset_statement(line):
                continue  # 「覆盖 N 个工具」是子集口径，不是工具面总数
            for m in pat_tool.finditer(line):
                n = int(m.group(1))
                if n != FACTS["tool_count"]:
                    issues.append({
                        "file": rel, "line": i, "kind": "TOOL_COUNT",
                        "found": n, "expect": FACTS["tool_count"],
                        "frozen_baseline": frozen,
                        "text": line.strip()[:140],
                    })
    # 规则 2：对抗用例数（若文档提到）
    pat_adv = re.compile(r"对抗用例\s*(\d+)\s*条")
    for fp in _iter_md():
        rel = os.path.relpath(fp, DOC_ROOT)
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception:  # noqa: BLE001
            continue
        for i, line in enumerate(lines, 1):
            for m in pat_adv.finditer(line):
                n = int(m.group(1))
                if n < FACTS["adversarial_cases"]:
                    issues.append({
                        "file": rel, "line": i, "kind": "ADVERSARIAL_COUNT",
                        "found": n, "expect": FACTS["adversarial_cases"],
                        "frozen_baseline": False,
                        "text": line.strip()[:140],
                    })
    return issues


def fn_text(fp):
    try:
        with open(fp, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001
        return ""


# 现行文档白名单：只有这些文件里的工具数是「现行口径」，才允许改。
# 目录基准：《实施推进与验收方案》与《MCP工具契约与注册说明》，
# 以及全量交付执行方案（M7 之后的现行方案）、M7 作战计划。
CURRENT_DOCS = {
    "03-架构与设计/最终技术方案-数据需求智能分析助手.md",
    "03-架构与设计/MCP工具契约与注册说明-数据需求智能分析助手.md",
    "00-索引与规范/文档库索引与阅读顺序.md",
    "05-开发实施/全量交付执行方案与任务提示词-数据需求智能分析助手-2026-10-02.md",
    "05-开发实施/MVP亟待解决任务事项-数据需求智能分析助手-2026-10-02.md",
    "01-立项与规划/实施推进与验收方案-数据需求智能分析助手.md",
    "01-立项与规划/M7作战计划-数据需求智能分析助手-2026-10-02.md",
    "06-测试与验收/MCP服务人工验证方案-数据需求智能分析助手-2026-10-02.md",
    "06-测试与验收/阶段A批次准入台账-数据需求智能分析助手.md",
}
# 历史快照目录/文件：里面写的工具数是「当时的真实值」，改了就是篡改历史
HISTORIC_PREFIXES = ("04-调研与选型/",)
HISTORIC_EXACT = {
    "01-立项与规划/M6作战计划-规则体系与健壮性与留痕回放-数据需求智能分析助手-2026-09-30.md",
    "01-立项与规划/M4-M5冲刺作战计划-数据需求智能分析助手-2026-09-30.md",
}
# 「覆盖 N 个工具」= 人工验证方案所覆盖的**子集**，不是工具面总数，不得替换
SUBSET_MARKERS = ("覆盖", "涉及", "用到")


def is_subset_statement(line):
    """判断这一句说的是「子集覆盖 N 个工具」而非「工具面共 N 个工具」。"""
    for m in SUBSET_MARKERS:
        if m in line:
            # 形如「共覆盖 24 个工具」「覆盖 24 个工具」→ 子集
            if re.search(r"(覆盖|涉及|用到)[^。；]{0,6}?\d+\s*个", line):
                return True
    return False


def is_historic(rel):
    """判断该文件是否为历史快照（只读保留，不同步）。"""
    if rel in HISTORIC_EXACT:
        return True
    if any(rel.startswith(p) for p in HISTORIC_PREFIXES):
        return True
    # 验收单里带日期的多数是「该里程碑时点的快照」，只有 MCP 服务方案与阶段A台账是现行的
    if rel.startswith("06-测试与验收/") and rel not in CURRENT_DOCS:
        return True
    return False


def sync(issues, yes=False):
    """按工具数口径批量替换。默认 dry-run，须 --yes 落盘。"""
    by_file = {}
    skipped = {"historic": 0, "frozen": 0}
    for it in issues:
        if it["kind"] != "TOOL_COUNT":
            continue
        if it["frozen_baseline"]:
            skipped["frozen"] += 1
            continue
        if is_historic(it["file"]) or it["file"] not in CURRENT_DOCS:
            skipped["historic"] += 1
            continue
        by_file.setdefault(it["file"], []).append(it)

    changed, backed_up = [], []
    ts = time.strftime("%Y%m%d-%H%M%S")
    bak_dir = os.path.expanduser("~/.workbuddy/backups/doc-sync-%s" % ts)
    for rel, its in by_file.items():
        fp = os.path.join(DOC_ROOT, rel)
        src = fn_text(fp)
        new = src
        for it in its:
            # 只替换「N 个工具」中的 N，避免误伤其它数字
            new = re.sub(r"(?<!\d)%d(\s*个\s*(?:MCP\s*)?工具)" % it["found"],
                         r"%d\1" % FACTS["tool_count"], new)
        if new != src:
            if yes:
                os.makedirs(bak_dir, exist_ok=True)
                dst = os.path.join(bak_dir, rel.replace("/", "_"))
                shutil.copy2(fp, dst)
                backed_up.append(dst)
                with open(fp, "w", encoding="utf-8") as fh:
                    fh.write(new)
            changed.append({"file": rel, "hits": len(its)})
    return changed, backed_up, bak_dir, skipped


def main():
    ap = argparse.ArgumentParser(description="文档口径同步器（综合决策 Agent）")
    ap.add_argument("--scan", action="store_true", help="只扫不改（默认）")
    ap.add_argument("--sync", action="store_true", help="批量替换")
    ap.add_argument("--yes", action="store_true", help="确认落盘（配合 --sync）")
    args = ap.parse_args()

    issues = scan()
    print("=" * 78)
    print("综合决策 Agent · 文档口径扫描（事实源：代码实测）")
    print("  工具面 %d · 对抗用例 %d 条 · REGISTRY %d 条"
          % (FACTS["tool_count"], FACTS["adversarial_cases"], FACTS["rules_count"]))
    print("=" * 78)
    if not issues:
        print("✅ 无口径不一致。")
        return 0

    by_kind = {}
    for it in issues:
        by_kind.setdefault(it["kind"], []).append(it)
    for k, its in by_kind.items():
        print("\n【%s】%d 处" % (k, len(its)))
        shown = 0
        for it in its:
            if is_historic(it["file"]):
                tag = "（历史快照·保留原值）"
            elif it["frozen_baseline"]:
                tag = "（冻结基线·需人工处理）"
            elif it["file"] not in CURRENT_DOCS:
                tag = "（非现行文档·待人工判定）"
            else:
                tag = "（现行口径·可同步）"
            print("  %s:%d  「%d 个工具」→ 应为 %d%s"
                  % (it["file"], it["line"], it["found"], it["expect"], tag))
            shown += 1
            if shown >= 40:
                print("  … 另有 %d 处" % (len(its) - 40))
                break

    if args.sync:
        changed, backed, bak_dir, skipped = sync(issues, yes=args.yes)
        print("\n" + "-" * 78)
        print("跳过：历史快照 %d 处 · 冻结基线 %d 处" % (skipped["historic"], skipped["frozen"]))
        if args.yes:
            print("✅ 已同步 %d 个文件（备份：%s）" % (len(changed), bak_dir))
            for c in changed:
                print("   · %s（%d 处）" % (c["file"], c["hits"]))
        else:
            print("（dry-run）将同步 %d 个现行文档。重跑加 --yes 落盘。" % len(changed))
            for c in changed:
                print("   · %s（%d 处）" % (c["file"], c["hits"]))
    else:
        print("\n（只读扫描，未改任何文件。修：--sync --yes）")
    return 0


if __name__ == "__main__":
    sys.exit(main())