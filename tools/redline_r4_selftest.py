#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R4 判据自证（离线，纯函数 + 临时目录，不需要容器/网关/DB）。

为什么必须有这个自证
------------------
R4 的判据名里带着两半：「删数据 / 破坏性操作」**和**「改带 sha256 的冻结基线」。
但实现里那半句**从未落地**：
  · `sha256` / `hashlib` 在判据文件里出现 0 次；
  · 三个常量（清单路径 / 探针样例 / 参数化 token）的 AST Load 次数**全是 0**；
  · `files` 形参**完全没被使用**。
⇒ 判据自称覆盖了一个面，实现上那个面是空的。
实测：改登记值 / 动整份清单 / 改 `poc-eval/bank-b.json` ⇒ **全部 CLEAR**。

⚠️ 这正是「判据自称 vs 实现为空」那类病。**不写自测就永远发现不了。**

本自证覆盖三组：
  A. 破坏性 SQL 探针（8 条，从判据文件迁到这里成为**真正被用起来的 fixture**）；
  B. **sha256 基线子判据是否真的在跑**（改一个字节 ⇒ 必判红；不动 ⇒ 必不判红）；
  C. 口径边界（只对内容变更判红，不对文件增删判红；只比 sha256 前 16 位）。

用法：
    python3 tools/redline_r4_selftest.py
退出码：0 = 全过；1 = 有失败。

⚠️ **必须在仓库根目录运行**（含 `poc-eval/` 与 `wren-docker*/`）：
   清单里登记的是**仓库内相对路径**，自证要拿真实资产算 sha256。
   在容器内跑（镜像里没有这 3 份资产）会得出「资产不存在」的假结论。
"""
import hashlib
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# ⚠️ 必须在 import tempfile **之前**指定 tempdir。
#
#   本机（WorkBuddy 沙箱）禁止任何"让文件从某路径消失"的操作，而 Python 的
#   tempfile 用「建一个探测文件再删掉」来验证目录可写 ⇒ 删不掉 ⇒ 探测抛
#   FileNotFoundError ⇒ `No usable temporary directory found`。
#   直接指定 tempdir 可**跳过探测**（gettempdir() 见到 tempdir 已设就直接返回）。
#   ⇒ 本自证不依赖任何外部 PYTHONPATH 设置，自己就能跑。
import tempfile  # noqa: E402

_TMPROOT = os.path.join(os.path.expanduser("~"), ".workbuddy", "tmp")
try:
    os.makedirs(_TMPROOT, exist_ok=True)
except OSError:
    _TMPROOT = os.path.join(ROOT, ".tmp_gate")
    os.makedirs(_TMPROOT, exist_ok=True)
tempfile.tempdir = _TMPROOT

import redline_guard as rg  # noqa: E402


# ---------------------------------------------------------------------------
# A. 破坏性 SQL 探针 fixture
#
# 这 8 条原先以常量形式躺在 `tools/redline_guard.py` 里，AST Load 次数 0
# —— 判据文件自己从不读它，是一份孤儿。现迁到这里作为**真正被用起来的 fixture**。
#
# 期望：前 5 条判红（真破坏），后 3 条不判红（误报豁免的由来）。
# ---------------------------------------------------------------------------
R4_PROBES = [
    ("TRUNCATE TABLE orders", True),
    ("truncate table t2", True),
    ("TRUNCATE `mytable`", True),
    ("DELETE FROM users WHERE 1=1", True),
    ("DROP TABLE foo", True),
    ("-- auto-truncate tokenization", False),
    ("分词截断 auto-truncate 参数", False),
    ("直接分析数据", False),
]


def _diff_with_added_line(line):
    """造一段最小 unified diff：把 `line` 作为 gateway/xxx.py 的新增行。"""
    return (
        "diff --git a/gateway/xxx.py b/gateway/xxx.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/gateway/xxx.py\n"
        "+++ b/gateway/xxx.py\n"
        "@@ -0,0 +1 @@\n"
        "+%s\n" % line
    )


def run_probes():
    fails = []
    print("=" * 74)
    print("R4 判据自证 · A. 破坏性 SQL 探针（8 条）")
    print("=" * 74)
    for text, expect_red in R4_PROBES:
        hits, _msg = rg.check_r4_data_and_baseline(
            _diff_with_added_line(text), ["gateway/xxx.py"]
        )
        got_red = bool(hits)
        ok = got_red == expect_red
        if not ok:
            fails.append(
                "探针 FAILED: %r 期望%s，实际%s" % (
                    text, "判红" if expect_red else "不判红",
                    "判红" if got_red else "不判红")
            )
        print("[%s] %-40s 期望%s 实际%s" % (
            "OK" if ok else "!!", text,
            "🔴" if expect_red else "⚪",
            "🔴" if got_red else "⚪",
        ))
        for h in hits:
            print("        · %s" % h)
    return fails


# ---------------------------------------------------------------------------
# B + C. sha256 基线子判据
#
# ★ 基底取**仓库真实文件**（poc-eval/bank-b.json），只在**临时副本**上构造破坏，
#   不造假源码、不碰真实工作树。
# ---------------------------------------------------------------------------
def _make_sandbox(tmp, manifest, real_asset):
    """搭一个临时仓库根：放 manifest + 全部登记资产的真实副本。

    ★ 全部登记资产都复制进来（不只是被测的那一个），否则沙箱里另外两项
      会因「不存在」被跳过，B2 的「不动 ⇒ 不判红」就验的不是真实场景。
    """
    os.makedirs(os.path.join(tmp, "tools"), exist_ok=True)
    shutil.copy2(
        os.path.join(ROOT, "tools", "baseline_manifest.json"),
        os.path.join(tmp, "tools", "baseline_manifest.json"),
    )
    for a in manifest["assets"]:
        p = a.get("path")
        if not p:
            continue
        src = os.path.join(ROOT, p)
        if not os.path.exists(src):
            continue
        d = os.path.dirname(p)
        if d:
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        shutil.copy2(src, os.path.join(tmp, p))
    return tmp


def run_sha256_cases():
    fails = []
    print()
    print("=" * 74)
    print("R4 判据自证 · B/C. sha256 基线子判据（是否真的在跑）")
    print("=" * 74)

    real_asset = "poc-eval/bank-b.json"
    if not os.path.exists(os.path.join(ROOT, real_asset)):
        print("[!!] 找不到真实资产 %s，跳过" % real_asset)
        return ["sha256 fixture FAILED: 真实资产不存在"]

    # 登记的期望值直接从仓库真实 manifest 读，不手抄
    with open(os.path.join(ROOT, "tools", "baseline_manifest.json"),
              encoding="utf-8") as fh:
        manifest = json.load(fh)
    entry = None
    for a in manifest["assets"]:
        if a["path"] == real_asset:
            entry = a
            break
    if entry is None:
        print("[!!] manifest 里没有 %s，跳过" % real_asset)
        return ["sha256 fixture FAILED: manifest 未登记该资产"]
    want = entry["sha256_prefix"]
    print("  登记值（读自真实 manifest）：%s = %s" % (real_asset, want))

    # ---- B1：改一个字节 ⇒ 必判红 ----
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        _make_sandbox(tmp, manifest, real_asset)
        target = os.path.join(tmp, real_asset)
        with open(target, "ab") as fh:
            fh.write(b"\n")
        hits, _ = rg.check_frozen_baseline(root=tmp)
        ok = bool(hits)
        if not ok:
            fails.append("B1 FAILED：改了一个字节，sha256 子判据竟未判红")
        print("[%s] B1. 改一个字节 ⇒ %s" % (
            "OK" if ok else "!!", "🔴 判红（正确）" if ok else "⚪ 未判红（漏判！）"))
        for h in hits:
            print("        · %s" % h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- B2：不动 ⇒ 必不判红 ----
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        _make_sandbox(tmp, manifest, real_asset)
        hits, note = rg.check_frozen_baseline(root=tmp)
        ok = not hits
        if not ok:
            fails.append("B2 FAILED：未改动却判红：%s" % hits)
        print("[%s] B2. 不动任何文件 ⇒ %s" % (
            "OK" if ok else "!!", "⚪ 不判红（正确）" if ok else "🔴 误判！"))
        print("        子判据说明：%s" % note)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- B3：登记值本身被改 ⇒ 必判红（否则「改清单」就能永久关掉这条红线）----
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        _make_sandbox(tmp, manifest, real_asset)
        mpath = os.path.join(tmp, "tools", "baseline_manifest.json")
        with open(mpath, encoding="utf-8") as fh:
            m = json.load(fh)
        for a in m["assets"]:
            if a["path"] == real_asset:
                a["sha256_prefix"] = "0000000000000000"
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(m, fh, ensure_ascii=False, indent=2)
        hits, _ = rg.check_frozen_baseline(root=tmp)
        ok = bool(hits)
        if not ok:
            fails.append("B3 FAILED：改了登记值却未判红")
        print("[%s] B3. 改动登记值（清单里的 sha256）⇒ %s" % (
            "OK" if ok else "!!", "🔴 判红（正确）" if ok else "⚪ 未判红（漏判！）"))
        for h in hits:
            print("        · %s" % h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- C1：登记资产不存在 ⇒ 不判红（口径：只对内容变更判红，不对文件增删判红）----
    #
    # ⚠️ 造法**不依赖删文件**（本机沙箱禁止任何"让文件消失"的操作）：
    #   把登记清单里的资产路径改成**一个本来就不存在的路径** ⇒ 判据看到
    #   「文件不存在」⇒ 按口径跳过 ⇒ 不判红。等价于「登记资产被删」，
    #   但全程不碰删除。
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        _make_sandbox(tmp, manifest, real_asset)
        mpath = os.path.join(tmp, "tools", "baseline_manifest.json")
        with open(mpath, encoding="utf-8") as fh:
            m = json.load(fh)
        ghost = "poc-eval/__不存在的资产__.json"
        for a in m["assets"]:
            if a["path"] == real_asset:
                a["path"] = ghost
        with open(mpath, "w", encoding="utf-8") as fh:
            json.dump(m, fh, ensure_ascii=False, indent=2)
        hits, note = rg.check_frozen_baseline(root=tmp)
        ok = not hits
        if not ok:
            fails.append("C1 FAILED：登记资产不存在却判红（口径应为不判红）：%s" % hits)
        print("[%s] C1. 登记资产不存在 ⇒ %s（口径：不对文件增删判红）" % (
            "OK" if ok else "!!", "⚪ 不判红（符合口径）" if ok else "🔴 判红（与口径不符）"))
        print("        子判据说明：%s" % note)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- C2：清单本身缺失 ⇒ 判红（不可判定 ≠ 没违规）----
    #
    # ⚠️ 造法**不依赖删文件**（本机沙箱禁止删除）：
    #   造一个**根本没有 tools/baseline_manifest.json** 的沙箱根 ⇒
    #   清单"不存在"是**天然状态**，不用去删任何东西。
    #   判据语义不变：清单读不到 ⇒ 不可判定 ⇒ 必须判红。
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        # 只放资产、**不放清单** ⇒ 清单天然缺失
        os.makedirs(os.path.join(tmp, "poc-eval"), exist_ok=True)
        shutil.copy2(
            os.path.join(ROOT, real_asset),
            os.path.join(tmp, real_asset),
        )
        hits, _ = rg.check_frozen_baseline(root=tmp)
        ok = bool(hits)
        if not ok:
            fails.append("C2 FAILED：清单缺失却判绿（等于「删清单就永久关掉这条红线」）")
        print("[%s] C2. 清单文件缺失 ⇒ %s（不可判定 ⇒ 判红）" % (
            "OK" if ok else "!!", "🔴 判红（正确）" if ok else "⚪ 未判红（漏判！）"))
        for h in hits:
            print("        · %s" % h)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- C3：只比 sha256 前 16 位（末尾换行等差异不误报）----
    tmp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        _make_sandbox(tmp, manifest, real_asset)
        raw = open(os.path.join(tmp, real_asset), "rb").read()
        full = hashlib.sha256(raw).hexdigest()
        ok = full[:16] == want and full[16:] != want
        if not ok:
            fails.append("C3 FAILED：前 16 位与登记值不一致（sha256 前16 = %s）" % full[:16])
        print("[%s] C3. 登记值确为 sha256 前 16 位（完整值 %s…）" % ("OK" if ok else "!!", full))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- C4：C2 不是空过（负向对照）----
    #
    # ⚠️ 为什么要这条：C2 断言的是「清单缺失 ⇒ 判红」。若造场景失败（清单其实还在），
    #   判据会**正确地判绿** ⇒ C2 报红，但那是**用例没造出场景**，不是判据坏了
    #   （本机沙箱禁删时正是如此）。这属「失败信息字面误导」：报的是「判据漏判」，
    #   实则是「用例没搭好场景」。
    #   ⇒ 用一条**对照**把两者区分开：
    #     · 对照组：清单在、内容一致 ⇒ 必须**不判红**
    #     · 实验组：清单缺失         ⇒ 必须**判红**
    #   两者表现不同 ⇒ C2 验的确实是真判据（缺失这条分支真的会红）。
    tmp_ctrl = tempfile.mkdtemp(prefix="r4selftest_")
    tmp_exp = tempfile.mkdtemp(prefix="r4selftest_")
    try:
        # 对照组：放清单
        _make_sandbox(tmp_ctrl, manifest, real_asset)
        ctrl_hits, _ = rg.check_frozen_baseline(root=tmp_ctrl)
        # 实验组：只放资产、不放清单 ⇒ 清单天然缺失
        os.makedirs(os.path.join(tmp_exp, "poc-eval"), exist_ok=True)
        shutil.copy2(
            os.path.join(ROOT, real_asset),
            os.path.join(tmp_exp, real_asset),
        )
        exp_hits, _ = rg.check_frozen_baseline(root=tmp_exp)
        ok = (not ctrl_hits) and bool(exp_hits)
        if not ok:
            fails.append(
                "C4 FAILED：对照组 hits=%d（应 0）、实验组 hits=%d（应 >0）⇒ C2 可能是空过"
                % (len(ctrl_hits), len(exp_hits))
            )
        print("[%s] C4. C2 非空过（对照组 %d 命中 / 实验组 %d 命中）" % (
            "OK" if ok else "!!", len(ctrl_hits), len(exp_hits)))
        print("        对照组（有清单、内容一致）⇒ %s" % ("不判红 ✅" if not ctrl_hits else "判红 ❌"))
        print("        实验组（清单缺失）        ⇒ %s" % ("判红 ✅" if exp_hits else "未判红 ❌"))
    finally:
        shutil.rmtree(tmp_ctrl, ignore_errors=True)
        shutil.rmtree(tmp_exp, ignore_errors=True)

    return fails


def run_manifest_shape():
    """清单本身的形状自检：只登记仓库内资产、字段齐全、路径真实存在。"""
    fails = []
    print()
    print("=" * 74)
    print("R4 判据自证 · 登记清单自身合规")
    print("=" * 74)
    assets, err = rg.load_frozen_baseline_manifest()
    if err:
        print("[!!] %s" % err)
        return ["清单形状 FAILED: %s" % err]
    ok_all = True
    for a in assets:
        p = a.get("path")
        exists = os.path.exists(os.path.join(ROOT, p)) if p else False
        fields_ok = bool(a.get("path") and a.get("sha256_prefix")) and \
            isinstance(a.get("expected_bytes"), int)
        ok = exists and fields_ok and len(a.get("sha256_prefix", "")) == 16
        ok_all = ok_all and ok
        if not ok:
            fails.append("清单条目不合规：%r" % (a,))
        print("[%s] %-38s 存在=%s 字段=%s 前16位=%s" % (
            "OK" if ok else "!!", p, exists, fields_ok, len(a.get("sha256_prefix", ""))))
    # 文档库那 7 份刻意不登记：仓内不该出现文档库路径
    doclib_hits = [a["path"] for a in assets
                   if "03-" in a["path"] or a["path"].startswith("/")]
    if doclib_hits:
        fails.append("清单里混入了文档库资产：%s" % doclib_hits)
    print("[%s] 清单只含仓库内资产（未混入文档库路径）" % (
        "OK" if not doclib_hits else "!!"))
    return fails


def main():
    fails = []
    fails += run_probes()
    fails += run_sha256_cases()
    fails += run_manifest_shape()
    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("R4 判据自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
