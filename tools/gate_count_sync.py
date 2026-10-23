#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁项数回填命令（**人主动跑**，判据永不自动写文档）。

为什么需要一个"命令"而不是让判据自己改
--------------------------------------
`tools/gate_count_check.py` 是**判据**：只判定、不写文档。这条约束是刻意的——
判据一旦能改文档，「文档与实际一致」就不再是事实，而是判据自己写的谎；
真正的漂移会被悄悄抹平，而那正是这套判据要治的病。

但反过来，**每次加一个 `*_selftest.py` 都要人工找两处数字去改**，
实测已经因此红过多次（加了脚本 → 忘了回填 → 门禁红 → 以为代码有问题）。
⇒ 解法是把「写」这件事单独做成**一个显式命令**：

    人看到 G2 报红 ⇒ 判断"确实该改" ⇒ 手动跑本命令 ⇒ 再跑一次门禁

关键差别：**谁做决定**。命令只执行人已经做出的决定，不代替人判断。

改动范围（写死，不许扩）
------------------------
  · `README.md`：那一行「门禁 **N 项全绿**：G0 … G3 …（日常只跑 G0+G1 共 M）」
  · `CONTRIBUTING.md`：门禁表里 G0/G1/G2/G3 四行的「项数」列

⚠️ 只动这两处、只动数字。**不改措辞、不加行、不碰其他文件**——
   回填工具一旦会"顺手优化文案"，它就是第二个不可信的自动改写器。

用法：
    python3 tools/gate_count_sync.py            # 只显示差异，不写（预览）
    python3 tools/gate_count_sync.py --write    # 真的写回去
退出码：0 = 已一致或写入成功；1 = 预览时有差异 / 写入失败。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gate_count_check as gcc  # noqa: E402

README = os.path.join(ROOT, "README.md")
CONTRIBUTING = os.path.join(ROOT, "CONTRIBUTING.md")

# README：门禁那一行。整行重写而不是替换零散数字——零散替换容易只改一半。
_README_LINE = re.compile(r"^-\s*门禁\s*\*\*\d+\s*项全绿\*\*.*$", re.MULTILINE)

# CONTRIBUTING：门禁表的数据行（只认 G0–G3 这四行）
# ⚠️ 各分组都用 `[^|\n]*` 而不是 `[^|]*`：后者会把行尾换行吞进分组，
#   于是"没变"的行也会因为重建后少了个 \n 而被判成变更（已踩过）。
_CONTRIB_ROW = re.compile(
    r"^\|\s*(G[0-3])\s*\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|[ \t]*$", re.MULTILINE)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def plan_readme(text, actual):
    """返回 (新文本, 旧行, 新行)。找不到那一行就抛错——不静默跳过。"""
    m = _README_LINE.search(text)
    if not m:
        raise SystemExit(
            "README.md 里找不到门禁那一行（形如 `- 门禁 **N 项全绿**：G0 … G3 …`）。\n"
            "⚠️ 不猜、不新建行：请先确认那一行是不是被改写或删掉了。")
    old_line = m.group(0)
    new_line = ("- 门禁 **%d 项全绿**：G0 静态 %d · G1 单元 %d · G2 独立复核 %d · "
                "G3 真实链路 %d（日常只跑 G0+G1 共 %d）"
                % (actual["TOTAL"], actual["G0"], actual["G1"], actual["G2"],
                   actual["G3"], actual["DAILY"]))
    return text.replace(old_line, new_line, 1), old_line, new_line


def plan_contributing(text, actual):
    """返回 (新文本, [(层, 旧行, 新行)])。四行必须**全都找到**。"""
    found = {}
    changes = []

    def _sub(m):
        layer = m.group(1)
        if layer not in ("G0", "G1", "G2", "G3"):
            return m.group(0)
        found[layer] = True
        old_line = m.group(0)
        new_line = "| %s |%s| %d |%s|" % (layer, m.group(2), actual[layer], m.group(4))
        changes.append((layer, old_line, new_line))
        return new_line

    new_text = _CONTRIB_ROW.sub(_sub, text)
    missing = [k for k in ("G0", "G1", "G2", "G3") if k not in found]
    if missing:
        raise SystemExit(
            "CONTRIBUTING.md 的门禁表里找不到这些行：%r\n"
            "⚠️ 不猜、不新建行：先确认表格是不是被改了结构。" % missing)
    return new_text, changes


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    write = "--write" in argv

    # ⚠️ compute_actual() 返回 (counts, detail) 二元组，不是单个 dict。
    #   直接当 dict 用会得到 "tuple indices must be integers" —— 已踩过一次。
    actual = gcc.compute_actual()[0]
    print("=" * 78)
    print("门禁项数回填 · 实际项数（来自 gate_all.py 的 AST，不另写一套 glob）")
    print("=" * 78)
    print("  G0=%d  G1=%d  G2=%d  G3=%d  合计=%d  日常=%d"
          % (actual["G0"], actual["G1"], actual["G2"], actual["G3"],
             actual["TOTAL"], actual["DAILY"]))
    print("")

    readme_old = _read(README)
    readme_new, r_old, r_new = plan_readme(readme_old, actual)
    contrib_old = _read(CONTRIBUTING)
    contrib_new, c_changes = plan_contributing(contrib_old, actual)

    diffs = []
    if readme_old != readme_new:
        diffs.append(("README.md", r_old.strip(), r_new.strip()))
    for layer, old_line, new_line in c_changes:
        if old_line != new_line:
            diffs.append(("CONTRIBUTING.md " + layer, old_line.strip(), new_line.strip()))

    if not diffs:
        print("✅ 文档与实跑项数已经一致，无需回填。")
        return 0

    print("需要改的地方（共 %d 处）：" % len(diffs))
    for where, old, new in diffs:
        print("\n  [%s]" % where)
        print("    旧：%s" % old)
        print("    新：%s" % new)

    if not write:
        print("\n⚠️ 这是**预览**，什么都没写。确认无误后加 --write 真正回填。")
        print("   为什么不默认写：回填是人的决定，不是脚本的自动反应。")
        return 1

    _write(README, readme_new)
    _write(CONTRIBUTING, contrib_new)
    print("\n✅ 已写入 README.md 与 CONTRIBUTING.md（只动了这 %d 处数字）。" % len(diffs))
    print("   ⇒ 建议紧接着跑一次 `python3 tools/gate_count_check.py` 复核。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
