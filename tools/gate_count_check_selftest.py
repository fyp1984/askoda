#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁项数核对判据（`tools/gate_count_check.py`）的 fixture 自测。

为什么必须有这个
----------------
⚠️ 判据自己写着「文档与实际脱节」，但**判据本身也可能撒谎**——
本单初版就把 G1/G2/G3 算成 0 / 1，靠的是「实跑 gate_all 交叉核对」抓到的，
不是靠读代码看出来的。这说明「看起来对」与「真对」差得很远，
所以改判据必须自带 fixture（沿用 `CONTRIBUTING.md` 的红线 HC：
「改判据必须自带 fixture 测试」）。

三条必测（与验收要求一一对应）
------------------------------
  T1 文档数字与实际一致 ⇒ **不判红**（防止判据永远红，成为噪声）
  T2 把某个文档数字改错 ⇒ **必须判红**，且输出里含正确数字
     （⚠️ 在**副本**上改，绝不动真文件）
  T3 模拟「新增一个 `*_check.py` 脚本但文档没更新」⇒ **必须判红**
     （这是本单的核心价值：下次有人加脚本忘了改文档，门禁自己报红）

做法：T2/T3 一律在 `tempfile` 临时目录里造副本，绝不碰仓库真文件。

用法：
    python3 tools/gate_count_check_selftest.py
退出码：0 = 全过，1 = 有失败。
"""
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gate_count_check as gcc  # noqa: E402

FAILS = []


def _check(ok, name, detail=""):
    print("[%s] %s" % ("OK" if ok else "!!", name))
    if detail:
        print("    %s" % detail)
    if not ok:
        FAILS.append(name)
    return ok


def _actual():
    return gcc.compute_actual()[0]


# ---------------------------------------------------------------------------
# T1：文档数字与实际一致 ⇒ 不判红
# ---------------------------------------------------------------------------
def t1_consistent_not_red():
    """把「实际项数」当成「文档声明」喂进去 ⇒ compare() 必须零不一致。

    这是**反向构造**：用判据自己算出的数当声明，等价于「文档已被正确回填」。
    好处是**不依赖仓库真文件的当前状态**（真文件此刻是脱节的，
    正因如此才不能拿真文件当 T1 的输入）。
    """
    actual = _actual()
    bad = gcc.compare(actual, dict(actual))
    ok = not bad
    _check(ok, "T1 文档与实际一致 ⇒ 不判红",
           "一致时不一致清单=%s（应为空）；实际=%s" % (
               bad, {k: actual[k] for k in ("G0", "G1", "G2", "G3", "TOTAL")}))
    return ok


# ---------------------------------------------------------------------------
# T2：文档数字改错（副本上改）⇒ 必判红，且输出含正确数字
# ---------------------------------------------------------------------------
def t2_wrong_doc_number_is_red():
    real_a, _ = gcc.compute_actual()
    tmp = tempfile.mkdtemp(prefix="gcc_t2_")
    try:
        # 复制两份真文档到临时目录，只改副本
        cpath = os.path.join(tmp, "CONTRIBUTING.md")
        rpath = os.path.join(tmp, "README.md")
        shutil.copy(gcc.DOC_CONTRIBUTING, cpath)
        shutil.copy(gcc.DOC_README, rpath)

        dec0 = gcc.parse_contributing(cpath)
        # 故意把 G2 声明改错：+100（模拟"文档没跟上"）
        broken = dict(dec0)
        broken["G2"] = dec0["G2"] + 100
        bad = gcc.compare(real_a, broken)
        ok = bool(bad) and any(b[0] == "G2" for b in bad)
        _check(ok, "T2a CONTRIBUTING 数字改错 ⇒ 判红",
               "G2 声明改成 %d（实际 %d）⇒ 不一致清单=%s"
               % (broken["G2"], real_a["G2"], bad))

        # README 同理：改「合计」
        r0 = gcc.parse_readme(rpath)
        rbroken = dict(r0)
        rbroken["TOTAL"] = r0["TOTAL"] + 100
        rbad = gcc.compare(real_a, rbroken)
        ok2 = bool(rbad) and any(b[0] == "TOTAL" for b in rbad)
        _check(ok2, "T2b README 合计改错 ⇒ 判红",
               "TOTAL 声明改成 %d（实际 %d）⇒ 不一致清单=%s"
               % (rbroken["TOTAL"], real_a["TOTAL"], rbad))

        # 且改错后必须能从**副本文件**里读出那个错值（证明读的是副本、不是缓存真文件）
        src = open(cpath, encoding="utf-8").read()
        mutated = re.sub(r"^(\|\s*G2\s*\|[^|]*\|\s*)(\d+)(\s*\|)",
                         lambda m: m.group(1) + str(int(m.group(2)) + 100) + m.group(3),
                         src, count=1, flags=re.M)
        open(cpath, "w", encoding="utf-8").write(mutated)
        got = gcc.parse_contributing(cpath)
        ok3 = got["G2"] == dec0["G2"] + 100
        _check(ok3, "T2c 判据确实读副本文件（改副本→读出改后的值）",
               "副本读到 G2=%d，期望 %d；真文件仍是 %d"
               % (got["G2"], dec0["G2"] + 100, dec0["G2"]))
        return ok and ok2 and ok3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# T3：新增一个 *_check.py 但文档没更新 ⇒ 必判红（本单核心价值）
# ---------------------------------------------------------------------------
def t3_new_script_without_doc_update_is_red():
    """注入一个假的 `*_check.py` 命中（**不动真实文件系统**）。

    做法：给 `compute_actual` 传一个假的 `script_lister`，它对
    `*_check.py` 多返回 1 个文件，模拟「有人加了一个新 check 脚本」。
    文档声明保持旧值 ⇒ 必须判红，且报出的 G2 正确 +1。
    """
    real_a, _ = gcc.compute_actual()
    base = gcc.ga._find_scripts

    def fake_lister(pattern):
        hits = list(base(pattern))
        if pattern == "*_check.py":
            hits.append("/fake/tools/brand_new_check.py")
        return hits

    new_a, _ = gcc.compute_actual(script_lister=fake_lister)

    ok_count = new_a["G2"] == real_a["G2"] + 1
    _check(ok_count, "T3a 注入新 *_check.py ⇒ G2 实际项数 +1",
           "注入前 G2=%d，注入后 G2=%d（应 +1）" % (real_a["G2"], new_a["G2"]))

    # 文档声明仍是旧值 ⇒ 必须判红，且差异恰好定位到 G2
    declared = {k: real_a[k] for k in ("G0", "G1", "G2", "G3", "TOTAL")}
    bad = gcc.compare(new_a, declared)
    ok_red = any(b[0] == "G2" for b in bad)
    _check(ok_red, "T3b 文档未更新 ⇒ 判红且定位到 G2",
           "不一致清单=%s（G2 声明 %d vs 实际 %d）"
           % (bad, real_a["G2"], new_a["G2"]))

    # 反向：把文档同步成新值后 ⇒ 必须转绿（证明不是「永远红」）
    synced = {k: new_a[k] for k in ("G0", "G1", "G2", "G3", "TOTAL")}
    bad2 = gcc.compare(new_a, synced)
    ok_green = not bad2
    _check(ok_green, "T3c 文档同步为新值后 ⇒ 转绿（证明判据不是永远红）",
           "同步后不一致清单=%s（应为空）" % (bad2,))
    return ok_count and ok_red and ok_green


# ---------------------------------------------------------------------------
# T4（附加）：门禁项数判据必须能抓住「G0 目标增加」——防另一类脱节
# ---------------------------------------------------------------------------
def t4_g0_glob_target_detected():
    """`_g0_py_compile` 模式是从 gate_all.py 读出来的。

    这里验证：判据用的 glob 模式与 gate_all.py 源码里**一致**——
    即「口径来源是 gate_all 自己」这条前提真的成立。
    """
    tree = gcc._gate_all_ast()
    patterns = [args[0] for name, args in gcc._calls_in(tree, "run_g0")
                if name == "_g0_py_compile"]
    expect = ["gateway/*.py", "tools/*.py", "bff/*.py"]
    ok = patterns == expect
    _check(ok, "T4 G0 的 py_compile 目标由 gate_all.py 读出（非本判据写死）",
           "从 gate_all.py 读到=%s" % (patterns,))
    return ok


# ---------------------------------------------------------------------------
# T5 / T6：README「日常项数」解析的两种病态（**这两个是真实踩过的坑**）
# ---------------------------------------------------------------------------
def _readme_cases():
    """造三份 README 副本：正常 / 新旧并存且矛盾 / 声明被删。"""
    with open(gcc.DOC_README, encoding="utf-8") as fh:
        base = fh.read()
    # 从真文件里抠出「日常」那一段原文，保证 fixture 跟真文档同步演进
    m = re.search(r"（日常只跑\s*G0\+G1\s*共\s*\d+）", base)
    frag = m.group(0) if m else "（日常只跑 G0+G1 共 80）"
    return base, frag


def t5_daily_conflict_detected():
    """README 里「日常项数」出现两处且互相矛盾 ⇒ 必须报错，**不能静默取第一个**。

    ⚠️ 这是**真实踩过的坑**：team-lead 回填时把新写法嵌进门禁那行
    （`…（日常只跑 G0+G1 共 80）`），旧的独立行 `（…合计 60 项）` 没删，
    一度形成两处并存。判据初版用 `re.search` **静默取到先出现的 60**，
    于是报出「文档写 60 / 实际 80」的**假脱节**——
    而真正该报的是「文档自己前后矛盾」。
    """
    base, frag = _readme_cases()
    tmp = tempfile.mkdtemp(prefix="gcc_t5_")
    try:
        p = os.path.join(tmp, "README.md")
        # 场景 A：只有一处 ⇒ 能读出
        open(p, "w", encoding="utf-8").write(base)
        try:
            one = gcc.parse_readme(p)
            okA = one.get("DAILY") == gcc.compute_actual()[0]["DAILY"]
            _check(okA, "T5a README 只有一处日常声明 ⇒ 正常读出",
                   "读到 DAILY=%s（实际 %s）"
                   % (one.get("DAILY"), gcc.compute_actual()[0]["DAILY"]))
        except AssertionError as e:
            okA = False
            _check(False, "T5a README 只有一处日常声明 ⇒ 正常读出", str(e)[:120])

        # 场景 B：并存且矛盾 ⇒ 必须报错，且报错信息里要能看到两个候选值
        open(p, "w", encoding="utf-8").write(
            base + "\n  （日常只跑 G0/G1，合计 %d 项）\n"
            % (gcc.compute_actual()[0]["DAILY"] - 20))
        try:
            gcc.parse_readme(p)
            okB = False
            _check(False, "T5b README 日常声明前后矛盾 ⇒ 判红",
                   "未检出矛盾（判据失效）")
        except AssertionError as e:
            msg = str(e)
            okB = "矛盾" in msg
            _check(okB, "T5b README 日常声明前后矛盾 ⇒ 判红",
                   msg[:130])
        return okA and okB
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def t6_daily_missing_detected():
    """README 的「日常项数」声明被整段删掉 ⇒ 必须报错，不得静默放行。

    「读不到就当通过」是判据最危险的失效模式。
    """
    base, frag = _readme_cases()
    tmp = tempfile.mkdtemp(prefix="gcc_t6_")
    try:
        p = os.path.join(tmp, "README.md")
        open(p, "w", encoding="utf-8").write(base.replace(frag, ""))
        try:
            gcc.parse_readme(p)
            ok = False
            _check(False, "T6 README 日常声明被删 ⇒ 判红（不得静默放行）",
                   "未检出缺失（判据失效）")
        except AssertionError as e:
            ok = True
            _check(ok, "T6 README 日常声明被删 ⇒ 判红（不得静默放行）",
                   str(e)[:120])
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("=" * 78)
    print("门禁项数核对判据 · fixture 自测")
    print("=" * 78)
    t1_consistent_not_red()
    print("-" * 78)
    t2_wrong_doc_number_is_red()
    print("-" * 78)
    t3_new_script_without_doc_update_is_red()
    print("-" * 78)
    t4_g0_glob_target_detected()
    print("-" * 78)
    t5_daily_conflict_detected()
    print("-" * 78)
    t6_daily_missing_detected()
    print("=" * 78)
    if FAILS:
        print("自测未通过：%s" % "、".join(FAILS))
        return 1
    print("自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())