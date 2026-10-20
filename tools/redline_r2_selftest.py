#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R2 判据自证（离线，纯函数，不需要容器/网关）。

为什么必须有这个自证
------------------
R2 判据名叫「改已有工具入参名/顺序」，旧实现却只做一件事：
在 diff 删除侧匹配 `-def <已注册工具名>(`。它**从不比较参数名与顺序**，
于是四种性质完全不同的改动给出一模一样的结果：

    末尾追加可选参数（向后兼容）   → 判红（假阳性）
    重排参数顺序（破坏调用契约）   → 判红
    删除参数（破坏调用契约）       → 判红
    改函数名（破坏调用契约）       → 判红

附带风险：把两个工具函数整块互换位置（签名一字未改，def 行整行位移）也会被判红。
**假阳性面远大于保护面。**

判据自称与实际不符，本身就是缺陷。所以本文件用 fixture 钉死新判据的
每一个分支——判据改了但没有自带测试，等于又一次「判据说它做了它没做的事」。

用法：
    python3 tools/redline_r2_selftest.py
退出码：0 = 全过；1 = 有失败。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import redline_guard as rg  # noqa: E402


# ---------------------------------------------------------------------------
# fixture：一段最小的 app.py 源码。工具 `probe` 的签名字段由各 case 替换。
# ---------------------------------------------------------------------------

BASE_SRC = '''\
@mcp.tool
def probe(a: str, b: str = "B") -> dict:
    """基线工具。"""
    return {}


@mcp.tool
def untouched(x: str) -> dict:
    """不该被任何 case 波及的工具。"""
    return {}
'''

# case 9 用：probe 的装饰器 + def + 函数体整体删掉，只剩 untouched
DELETED_TOOL_SRC = '''\
@mcp.tool
def untouched(x: str) -> dict:
    """不该被任何 case 波及的工具。"""
    return {}
'''


def _src(tool_body: str) -> str:
    return BASE_SRC.replace(
        'def probe(a: str, b: str = "B") -> dict:',
        tool_body,
    )


# (case 名, 期望判红?, 新版 probe 签名, 说明)
CASES = [
    (
        "1. 末尾追加可选参数（demand_id: str = \"\"）",
        False,
        'def probe(a: str, b: str = "B", c: str = "") -> dict:',
        "真实演进形态：PR #14 给 sql_review 末尾追加 demand_id 就是这个形状。"
        "既有调用方按名传参不受影响 ⇒ 必须豁免。",
    ),
    (
        "2. 末尾追加**两个**可选参数",
        False,
        'def probe(a: str, b: str = "B", c: str = "", d: int = 0) -> dict:',
        "豁免不因追加数量而失效。",
    ),
    (
        "3. 重排参数顺序（a 与 b 互换）",
        True,
        'def probe(b: str, a: str) -> dict:',
        "同名参数换位：位置传参全错位（a 的值被当成 b 传下去），"
        "且 FastMCP 生成的工具 schema 里 parameter 顺序即契约 ⇒ 判红。",
    ),
    (
        "4. 改已有参数名（a → alpha）",
        True,
        'def probe(alpha: str, b: str = "B") -> dict:',
        "按名传参的调用方直接报未知参数 ⇒ 判红。",
    ),
    (
        "5. 改名函数（probe → probe2）",
        True,
        'def probe(a: str, b: str = "B") -> dict:',
        "工具面契约是函数名本身；改名后旧名调用直接失联 ⇒ 判红。",
    ),
    (
        "6. 在已有参数中间插参（a 与 b 之间插 c）",
        True,
        'def probe(a: str, c: str = "", b: str = "B") -> dict:',
        "前缀关系被破坏：已有参数 b 的相对位置变了 ⇒ 判红。",
    ),
    (
        "7. 删除已有参数（删掉 b）",
        True,
        'def probe(a: str) -> dict:',
        "传过 b 的调用方直接多传参数报错 ⇒ 判红。",
    ),
    (
        "8. 末尾追加**无默认值**的参数（仅限 keyword-only）",
        True,
        'def probe(a: str, b: str = "B", *, c: str) -> dict:',
        "豁免要求「带默认值」。位置参数无法表达「末尾追加必填」（Python 语法不允许"
        "无默认值参数跟在有默认值参数之后），所以这条只能落在 keyword-only 上——"
        "而它确实会要命：既有只传 (a) 的调用方直接缺参报错 ⇒ 判红。",
    ),
    (
        "9. 删掉整个工具（@mcp.tool 及其 def 一起删）",
        True,
        "__DELETE_TOOL__",
        "工具面少了一个入口 ⇒ 判红（这是 R2 真正要拦的那条）。",
    ),
    (
        "10. 签名完全未变（只改函数体）",
        False,
        'def probe(a: str, b: str = "B") -> dict:',
        "R2 约束的是入参契约，不是函数体 ⇒ 不判红。",
    ),
]


# ---------------------------------------------------------------------------
# 「不可判定 ⇒ 判红」的 fixture：旧实现在这些情况下**全部判绿**。
#
# 旧实现的早退分支把「app.py 读不到」（被删 / 被改名 / 语法坏）一律
# 当成「无违规」⇒ 判绿。而这三种恰恰是 R2 最该拦的改动：
#   · 删掉 gateway/app.py ⇒ 48 个已注册工具**全部消失**
#   · 把 app.py 改名        ⇒ 同上（工具面入口整个不见）
#   · app.py 语法写坏       ⇒ 工具面无法确认
# 「判不出来」不等于「没违规」——红线守卫不设通融通道。
# ---------------------------------------------------------------------------
UNDECIDABLE_CASES = [
    (
        "U1. 改动侧没有 gateway/app.py（被删）⇒ 全部已注册工具丢失",
        {},
        "absent",
    ),
    (
        "U2. 改动侧 app.py 语法坏 ⇒ 不可判定",
        {"probe": [("a", False), ("b", True)]},
        "broken",
    ),
    (
        "U3. 基线侧 app.py 语法坏 ⇒ 不可判定",
        None,
        "base_broken",
    ),
]


def _apply(name, tool_body):
    """按 case 名产出「新版 app.py 源码」。"""
    if tool_body == "__DELETE_TOOL__":
        return DELETED_TOOL_SRC
    if name.startswith("5."):
        return _src(tool_body).replace("def probe(", "def probe2(")
    return _src(tool_body)


def run_cases():
    fails = []
    print("=" * 74)
    print("R2 判据自证 · 参数名与顺序（fixture 逐条比对）")
    print("=" * 74)
    for name, expect_red, body, why in CASES:
        base = rg.tool_signatures(BASE_SRC)
        cur = rg.tool_signatures(_apply(name, body))
        if base is None or cur is None:
            fails.append("%s FAILED: 源码解析失败" % name)
            print("[!!] %s —— 源码解析失败" % name)
            continue
        findings = rg.compare_tool_signatures(base, cur)
        got_red = bool(findings)
        ok = got_red == expect_red
        if not ok:
            fails.append(
                "%s FAILED: 期望%s，实际%s；findings=%s"
                % (name, "判红" if expect_red else "豁免",
                   "判红" if got_red else "豁免", findings)
            )
        print("[%s] %s" % ("OK" if ok else "!!", name))
        print("     期望：%s   实际：%s" % (
            "🔴 判红" if expect_red else "⚪ 豁免",
            "🔴 判红" if got_red else "⚪ 豁免",
        ))
        for f in findings:
            print("     · %s" % f)
        print("     理由：%s" % why)
    return fails


def run_undecidable_cases():
    """「不可判定 ⇒ 判红」的 fixture（用真实 app.py 副本做基底）。

    这里不造假源码：基底取仓库**真实的** gateway/app.py，只在副本上做
    「删掉 / 改坏」的构造，验证判据在真实文件上确实拦得住。
    """
    fails = []
    print()
    print("=" * 74)
    print("R2 判据自证 · 不可判定必须判红（旧实现在此全部判绿）")
    print("=" * 74)

    real_path = os.path.join(ROOT, "gateway", "app.py")
    with open(real_path, "r", encoding="utf-8") as fh:
        real_src = fh.read()
    real_sigs = rg.tool_signatures(real_src)
    if not real_sigs:
        print("[!!] 读不到真实 gateway/app.py 的工具面，跳过")
        return ["不可判定 fixture FAILED: 真实 app.py 解析失败"]

    for name, _sigs, kind in UNDECIDABLE_CASES:
        if kind == "absent":
            base, base_st = real_sigs, "ok"
            cur, cur_st = {}, "absent"
        elif kind == "broken":
            base, base_st = real_sigs, "ok"
            cur, cur_st = None, "broken"
        else:  # base_broken
            base, base_st = None, "broken"
            cur, cur_st = real_sigs, "ok"

        # 走与 check_r2_tool_contract 相同的分支逻辑
        if base_st in ("error", "broken"):
            findings = ["无法读取基线（%s）的 gateway/app.py，R2 不可判定" % "HEAD"]
        elif cur_st in ("error", "broken"):
            findings = ["改动侧（%s）的 gateway/app.py 语法无法解析，R2 不可判定" % "WORKTREE"]
        else:
            findings = rg.compare_tool_signatures(base, cur)

        ok = bool(findings)
        if not ok:
            fails.append("%s FAILED：期望判红，实际判绿" % name)
        print("[%s] %s" % ("OK" if ok else "!!", name))
        print("     实际：%s" % ("🔴 判红" if ok else "⚪ 豁免"))
        for f in findings[:3]:
            print("     · %s" % f)
        if len(findings) > 3:
            print("     · ...（共 %d 条）" % len(findings))
    return fails


def run_real_case():
    """真实案例：PR #14 给 sql_review 末尾追加 demand_id ⇒ 必须豁免。

    基线取追加前的 cf7d281，改动侧取追加后的 18886f0——都是仓库真实历史，
    不是手搓的假源码。
    """
    fails = []
    print()
    print("=" * 74)
    print("R2 判据自证 · 真实案例（仓库真实历史，非人造 fixture）")
    print("=" * 74)

    def sigs_at(rev):
        rc, out = rg._run(["git", "show", "%s:gateway/app.py" % rev], cwd=ROOT)
        if rc != 0:
            return None
        return rg.tool_signatures(out)

    before = sigs_at("cf7d281")   # 追加 demand_id 之前
    after = sigs_at("18886f0")    # 追加 demand_id 之后
    if before is None or after is None:
        rc, out = rg._run(["git", "log", "--oneline", "-5"], cwd=ROOT)
        print("[!!] 取不到 cf7d281 / 18886f0 的 app.py，无法验证真实案例")
        print(out.strip()[:400])
        return ["真实案例 FAILED: 历史版本不可读"]

    print("  基线 cf7d281  sql_review: %s" % (before.get("sql_review"),))
    print("  改动 18886f0  sql_review: %s" % (after.get("sql_review"),))
    findings = rg.compare_tool_signatures(before, after)
    ok = not findings
    if not ok:
        fails.append("真实案例 FAILED：末尾追加 demand_id 被误判为破契约：%s" % findings)
    print("[%s] 真实案例：sql_review 末尾追加 demand_id（带默认值）⇒ %s" % (
        "OK" if ok else "!!", "豁免（不判红）" if ok else "判红（假阳性！）"))
    for f in findings:
        print("     · %s" % f)

    # 反向对照：同一段真实源码，把 demand_id 拿掉（= 删参数）必须判红。
    trimmed = dict(after)
    trimmed["sql_review"] = after["sql_review"][:2]
    findings2 = rg.compare_tool_signatures(after, trimmed)
    ok2 = bool(findings2)
    if not ok2:
        fails.append("真实案例反向对照 FAILED：删参数竟未判红")
    print("[%s] 反向对照：同一函数删掉 demand_id ⇒ %s" % (
        "OK" if ok2 else "!!", "判红（正确）" if ok2 else "豁免（漏判！）"))
    for f in findings2:
        print("     · %s" % f)
    return fails


def run_worktree_clean():
    """当前工作树相对 HEAD 不得有 R2 命中（判据自身的回归防线）。"""
    print()
    print("=" * 74)
    print("R2 判据自证 · 当前工作树 vs HEAD")
    print("=" * 74)
    hits, msg = rg.check_r2_tool_contract("", [])
    ok = not hits
    print("[%s] 工作树相对 HEAD：%s" % (
        "OK" if ok else "!!", "CLEAR" if ok else "BLOCKED"))
    for h in hits:
        print("     · %s" % h)
    return [] if ok else ["工作树 R2 命中：%s" % hits]


def main():
    fails = []
    fails += run_cases()
    fails += run_undecidable_cases()
    fails += run_real_case()
    fails += run_worktree_clean()
    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("R2 判据自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
