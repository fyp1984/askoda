#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R3 判据自证（离线，纯函数 + 临时目录，不需要容器/网关/DB）。

治的是哪一类病
--------------
R3（改 D1 行为：F4 不通过时自动改写 SQL）的判据名是「新增行里出现对
`revised_sql` 的赋值，且不是恒 None」，实现却是**正则扫文本**：

    re.search(r"revised_sql\\s*=\\s*(?!None)", body)

⇒ 分不清「赋值」与「字符串里恰好出现」。实测旧正则的 12 种行为：

    ✓ 真赋值            判红
    ✗ print 里的字符串  判红   ← 误判（本单要修的）
    ✗ 恒 None           判红   ← 误判（D1 要求的写法自己被判红）
    ✗ 注释 / 文档字符串   判红   ← 误判
    ✓ 中间变量          判红
    ✗ 下标赋值          豁免   ← 漏判
    ✗ 增强赋值          豁免   ← 漏判
    ✗ 字典字面量        豁免   ← 漏判

⇒ 这与 R2 是**同一个病根**：判据自称覆盖了一个面，实现上那个面是空的
（正则扫文本 vs AST 解析语法树）。本单用 `ast.parse` 改掉。

判据的四个必测分支
------------------
    ① 真赋值                  ⇒ 判红
    ② print 里的字符串        ⇒ 不判红
    ③ `revised_sql = None`   ⇒ 不判红
    ④ 经中间变量赋值          ⇒ 仍判红

用法：
    python3 tools/redline_r3_selftest.py
退出码：0 = 全过；1 = 有失败。
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import redline_guard as rg  # noqa: E402


# ---------------------------------------------------------------------------
# fixture 的「基线源码」
#
# ★ 基底取**仓库真实的 gateway/gates.py** 的 review() 结构（照抄其 D1 形态：
#   `revised_sql = None` + 若干 `"revised_sql": revised_sql` 的字典字面量），
#   逐个 case 只替换其中一行 ⇒ 验的是真实代码形态，不是凭空捏造的玩具。
# ---------------------------------------------------------------------------
BASE_SRC = '''# -*- coding: utf-8 -*-
"""demo · 复刻 gateway/gates.py 的 review() D1 形态。"""


def review(sql):
    """只报告，不改写（红线 D1）。"""
    revised_sql = None
    print("    revised_sql=%s（应为 None）" % revised_sql)
    return {
        "status": "通过",
        "revised_sql": revised_sql,
    }
'''


def _diff_all_added(path, content):
    """造一份 unified diff：整个文件为新增（最简、最不易算错行号）。"""
    lines = content.splitlines()
    out = [
        "diff --git a/%s b/%s" % (path, path),
        "new file mode 100644",
        "--- /dev/null",
        "+++ b/%s" % path,
        "@@ -0,0 +1,%d @@" % len(lines),
    ]
    out.extend("+" + l for l in lines)
    return "\n".join(out) + "\n"


def _judge(content, path="demo_review.py", root=None):
    """把 content 当作「本次新增的文件」跑一遍 R3 判据，返回 findings。

    ★ 完全不碰文件系统：判据的**判定核心**（`_r3_findings_in_source`）是纯函数，
      只吃「源码文本 + 改动行号表」。这里把源码直接喂给它。
      之所以不落盘到临时目录：临时目录方案在**沙箱禁止删除**的环境下不可用
      （Python 的 tempfile 靠「建一个探测文件再删掉」来验证目录可写，
      删不掉 ⇒ 判定该目录不可用 ⇒ `No usable temporary directory`），
      自证脚本会因此变成「测环境」而不是「测判据」。
    """
    added_map = {i + 1: t for i, t in enumerate(content.splitlines())}
    hits = rg._r3_findings_in_source(content, added_map)
    if hits is None:
        raise AssertionError("fixture 源码无法被 AST 解析")
    return hits


# ---------------------------------------------------------------------------
# 6 个场景的**单行形态**：同时用来验「主路径」与「fallback 路径」
#
# ★ 为什么必须两条路径都验：
#   R3 有两条判定路径——
#     · 主路径：整个文件能解析 ⇒ `ast.walk` 走结构化判定；
#     · fallback：整个文件**语法坏了** ⇒ 逐行做局部 AST 判定。
#   历史实测：fallback 走旧正则时 **6 个用例错 4 个**（既误判又漏判）。
#   ⇒ 两条路径必须**口径一致**，否则同一个违规在「文件恰好有语法错」时会被放过。
# ---------------------------------------------------------------------------
SCENARIOS = [
    ("① 真赋值 revised_sql = \"SELECT 1\"",
     'revised_sql = "SELECT 1"', True),
    ("② print 里的字符串（%s 格式化）",
     'print("    revised_sql=%s（应为 None）" % r.get("revised_sql"))', False),
    ("③ 下标赋值 result[\"revised_sql\"]",
     'result["revised_sql"] = "SELECT 1"', True),
    ("④ 恒 None（revised_sql = None）",
     'revised_sql = None', False),
    ("⑤ 属性赋值 self.revised_sql",
     'self.revised_sql = "SELECT 1"', True),
    ("⑥ 字典字面量 out = {\"revised_sql\": ...}",
     'out = {"revised_sql": "SELECT 1"}', True),
    # ⚠️ ⑦⑧ 是 team-lead 本批点名要钉住的两类（Task B · 变量透传收窄）：
    #   ⑦ 下标目标 + 右边是**变量名** ⇒ 右边值来自别处（通常恒 None）⇒ **不判红**
    #   ⑧ 字典字面量 + 右边是**变量名** ⇒ 同上 ⇒ **不判红**
    #   ★ 与 ③⑥ 构成对照：**字面量 ⇒ 判红；变量 ⇒ 不判红**。
    #     收窄的是「目标形态」，不是「值是不是硬编码」——
    #     真赋值那一行（`revised_sql = "SELECT 1"`）仍被 ① 抓住 ⇒ 不会漏放。
    ("⑦ 下标 + 变量透传 out[\"revised_sql\"] = revised_sql",
     'out["revised_sql"] = revised_sql', False),
    ("⑧ 字典字面量 + 变量 out = {\"revised_sql\": revised_sql}",
     'out = {"revised_sql": revised_sql}', False),
]


def run_paths_agree():
    """主路径与 fallback 路径必须**口径一致**（同一场景、同一结论）。

    ⚠️ 这条是「fallback 退化」的唯一防线：若只测主路径，
      fallback 哪天悄悄退化（旧正则那种）没人会发现。
    ⇒ 逐场景对比两条路径的判定结果，任何一条不一致即判红。
    """
    fails = []
    print()
    print("=" * 74)
    print("R3 判据自证 · 主路径 vs fallback 路径（口径必须一致）")
    print("=" * 74)
    for name, line, expect in SCENARIOS:
        added = {1: line}
        # 主路径：把该行放进一个语法正确的文件里
        main_src = "def f():\n    %s\n" % line
        main_hits = rg._r3_findings_in_source(main_src, {2: line})
        main_red = bool(main_hits) if main_hits is not None else None
        # fallback：单行直接判（模拟整文件语法坏时的退路）
        fb_hits = rg._r3_regex_fallback(added)
        fb_red = bool(fb_hits)
        agree = (main_red == fb_red)
        ok = agree and main_red == expect
        if not ok:
            fails.append(
                "路径一致性 FAILED：%s 主路径=%s fallback=%s 期望=%s"
                % (name, main_red, fb_red, expect)
            )
        print("[%s] %-44s 主路径=%-5s fallback=%-5s 期望=%-5s" % (
            "OK" if ok else "!!", name,
            "🔴" if main_red else "⚪", "🔴" if fb_red else "⚪",
            "🔴" if expect else "⚪"))
        for h in (fb_hits or []):
            print("        fallback · %s" % h)
    return fails


def run_negative_control():
    """负向验证：把判据改坏 ⇒ 用例必须变红（否则断言是空过的）。

    ⚠️ 没有负向验证的自证，可能整份都是「断言写错但永远绿」。
    ⇒ 这里**故意**用一个已知错误的判据（历史 fallback 的旧正则实现）跑同一组场景，
      断言它**必须至少错一个场景** ⇒ 证明这组断言真的有鉴别力。
      （判据：不是要求「每个场景都能区分好坏」——有些场景两种实现都判红，
        那是**正确的**；要求的是「整体上坏判据跑不过这组断言」。）
    """
    fails = []
    print()
    print("=" * 74)
    print("R3 判据自证 · 负向验证（判据改坏 ⇒ 用例必须变红）")
    print("=" * 74)
    # 已知错误的判据：退回旧正则（历史 fallback 的实现，实测 6 个用例错 4 个）
    import re as _re

    def broken_fallback(added_map):
        out = []
        for text in added_map.values():
            if "revised_sql" in text and _re.search(r"revised_sql\s*=\s*(?!None)", text):
                out.append(text)
        return out

    wrong_scenarios = []
    for name, line, expect in SCENARIOS:
        broken_red = bool(broken_fallback({1: line}))
        fixed_red = bool(rg._r3_regex_fallback({1: line}))
        broken_ok = (broken_red == expect)
        fixed_ok = (fixed_red == expect)
        if not fixed_ok:
            fails.append("负向验证 FAILED：修好后的判据在 %s 场景上判错" % name)
        if not broken_ok:
            wrong_scenarios.append(name)
        print("[%s] %-44s 坏判据=%-5s(%s) 修好=%-5s(%s) 期望=%-5s" % (
            "OK" if fixed_ok else "!!", name,
            "🔴" if broken_red else "⚪", "对" if broken_ok else "错",
            "🔴" if fixed_red else "⚪", "对" if fixed_ok else "错",
            "🔴" if expect else "⚪"))
    print()
    print("  坏判据判错的场景: %d/%d %s" % (
        len(wrong_scenarios), len(SCENARIOS), wrong_scenarios))
    if not wrong_scenarios:
        fails.append("负向验证 FAILED：坏判据在【所有】场景上都判对 ⇒ 这组断言钉不住任何东西")
    else:
        print("  ⇒ 坏判据跑不过这组断言 ⇒ 断言有鉴别力 ✅")
    return fails


def run_negative_control_narrowing():
    """负向验证 ②：把「下标/字典的收窄」去掉 ⇒ ⑦⑧ 两个用例必须变红。

    ⚠️ 为什么**要两组负向验证**，而不是一组：
      负向验证 ① 用的是**旧正则**（fallback 的历史实现）⇒ 它证明的是
      「AST 判定比文本匹配强」。
      但它**证明不了**另一件事：**收窄本身**（Task B）被钉住了。
      ⇒ 若有人把 `_r3_assign_findings` 里的收窄那段删掉（退回"非 None 就判红"），
        旧正则坏判据**照样跑不过**这组断言（它本来就错）⇒ 第一组验证**察觉不到**这次退化。
      ⇒ 必须再有一个「**只**去掉收窄、其余全对」的判据，用它当负向对照：
        断言它在 ⑦⑧ 上判红（错），而在 ①②③④⑤⑥ 上判对。
    """
    fails = []
    print()
    print("=" * 74)
    print("R3 判据自证 · 负向验证②（去掉「下标/字典收窄」⇒ ⑦⑧ 必须变红）")
    print("=" * 74)

    def too_wide_fallback(added_map):
        """故意去掉收窄的判据：下标/字典只要非 None 字面量就判红，变量也判红。"""
        out = []
        for text in added_map.values():
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                tree = ast.parse(stripped)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value == "revised_sql":
                            out.append(text)
                            break
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if rg._is_revised_sql_target(t):
                            out.append(text)
                            break
        return out

    narrow_scenarios = ("⑦", "⑧")
    wrong_narrow = []
    for name, line, expect in SCENARIOS:
        wide_red = bool(too_wide_fallback({1: line}))
        fixed_red = bool(rg._r3_regex_fallback({1: line}))
        wide_ok = (wide_red == expect)
        fixed_ok = (fixed_red == expect)
        if not fixed_ok:
            fails.append("负向验证② FAILED：现判据在 %s 场景上判错" % name)
        if not wide_ok:
            wrong_narrow.append(name)
        mark = "OK" if fixed_ok else "!!"
        print("[%s] %-44s 宽判据=%-5s(%s) 现判据=%-5s(%s) 期望=%-5s" % (
            mark, name,
            "🔴" if wide_red else "⚪", "对" if wide_ok else "错",
            "🔴" if fixed_red else "⚪", "对" if fixed_ok else "错",
            "🔴" if expect else "⚪"))
    print()
    hit_narrow = [n for n in wrong_narrow if n.startswith(narrow_scenarios)]
    print("  宽判据判错的场景: %d/%d %s" % (len(wrong_narrow), len(SCENARIOS), wrong_narrow))
    if not hit_narrow:
        fails.append(
            "负向验证② FAILED：宽判据在 ⑦⑧（变量透传）上竟然判对 ⇒ "
            "收窄这条用例钉不住任何东西（判据被改坏时不会变红）")
    else:
        print("  ⇒ 宽判据在 ⑦⑧ 上判红（错）而现判据判对 ⇒ 收窄被这组用例钉住了 ✅")
    return fails


# ---------------------------------------------------------------------------
# 四条必测分支
# ---------------------------------------------------------------------------
CASES = [
    (
        "① 真赋值（revised_sql = \"SELECT 1\"）",
        True,
        BASE_SRC.replace(
            "    revised_sql = None\n",
            '    revised_sql = "SELECT 1"\n',
        ),
        "D1 被破坏：审查结果里带上了改写后的 SQL ⇒ 必须判红。",
    ),
    (
        "② print 里的字符串（%s 格式化）",
        False,
        BASE_SRC.replace(
            '    print("    revised_sql=%s（应为 None）" % revised_sql)\n',
            '    print("    revised_sql=%s（应为 None）" % revised_sql)\n'
            '    # 追加一行同形的打印\n'
            '    print("    revised_sql=%s（应为 None）" % revised_sql)\n',
        ),
        "字符串里出现 revised_sql= 不是赋值 ⇒ 不得判红。"
        "★ 这正是本单要修的误判（旧正则会判红）。",
    ),
    (
        "③ 恒 None（revised_sql = None）",
        False,
        BASE_SRC,
        "D1 要求的写法就是恒 None ⇒ 不得判红。"
        "★ 旧正则同样会误判（`(?!None)` 在 `= None` 带空格时因回溯而失配）。",
    ),
    (
        "④ 经中间变量赋值（x = \"SELECT 1\"; revised_sql = x）",
        True,
        BASE_SRC.replace(
            "    revised_sql = None\n",
            '    x = "SELECT 1"\n    revised_sql = x\n',
        ),
        "值来自中间变量 ⇒ 运行时仍是非 None ⇒ 必须判红。"
        "（AST 看得见「赋的是个变量」而不是「恒 None」。）",
    ),
]


def run_cases():
    fails = []
    print("=" * 74)
    print("R3 判据自证 · 四条必测分支")
    print("=" * 74)
    for title, expect_red, content, why in CASES:
        hits = _judge(content)
        got_red = bool(hits)
        ok = got_red == expect_red
        if not ok:
            fails.append(
                "%s FAILED：期望%s，实际%s；findings=%s" % (
                    title, "判红" if expect_red else "不判红",
                    "判红" if got_red else "不判红", hits)
            )
        print("[%s] %s" % ("OK" if ok else "!!", title))
        print("     期望：%s   实际：%s" % (
            "🔴 判红" if expect_red else "⚪ 不判红",
            "🔴 判红" if got_red else "⚪ 不判红",
        ))
        for h in hits:
            print("     · %s" % h)
        print("     理由：%s" % why)
    return fails


# ---------------------------------------------------------------------------
# 附加：不得放松 —— 旧正则能抓到的，新判据也必须抓到
# ---------------------------------------------------------------------------
EXTRA_CASES = [
    ("E1. 字典字面量里的非 None 值（旧正则漏判）",
     '    revised_sql = None\n    out = {"revised_sql": "SELECT 1"}\n', True),
    ("E2. 下标赋值 result[\"revised_sql\"]（旧正则漏判）",
     '    revised_sql = None\n    result["revised_sql"] = "SELECT 1"\n', True),
    ("E3. 属性赋值 self.revised_sql（旧正则能抓）",
     '    revised_sql = None\n    self.revised_sql = "SELECT 1"\n', True),
    ("E4. 注释里的 revised_sql=（旧正则误判）",
     '    revised_sql = None\n    # 备注：revised_sql = "SELECT 1"\n', False),
    ("E5. 文档字符串里的 revised_sql=（旧正则误判）",
     '    revised_sql = None\n    """示例：revised_sql = "SELECT 1"""\n', False),
    ("E6. is None 比较（不是赋值）",
     '    revised_sql = None\n    assert r.get("revised_sql") is None\n', False),
]


def run_extra():
    fails = []
    print()
    print("=" * 74)
    print("R3 判据自证 · 附加（覆盖力只增不减：旧正则漏判的也要抓到）")
    print("=" * 74)
    for title, body, expect_red in EXTRA_CASES:
        content = BASE_SRC.replace("    revised_sql = None\n", body)
        hits = _judge(content)
        got_red = bool(hits)
        ok = got_red == expect_red
        if not ok:
            fails.append(
                "%s FAILED：期望%s，实际%s；findings=%s" % (
                    title, "判红" if expect_red else "不判红",
                    "判红" if got_red else "不判红", hits)
            )
        print("[%s] %-52s 期望%s 实际%s" % (
            "OK" if ok else "!!", title,
            "🔴" if expect_red else "⚪", "🔴" if got_red else "⚪"))
        for h in hits:
            print("        · %s" % h)
    return fails


def run_no_touch_outside_diff():
    """本次没改动的语句不该判红（判据只管「本次改动」）。"""
    fails = []
    print()
    print("=" * 74)
    print("R3 判据自证 · 范围边界（未改动的既有语句不判红）")
    print("=" * 74)
    # 文件里既有 `revised_sql = "SELECT 1"`（既有行，本次没碰），
    # 本次只新增了一行别的 ⇒ 不该判红。
    content = BASE_SRC.replace(
        "    revised_sql = None\n",
        '    revised_sql = "SELECT 1"\n',
    )
    lines = content.splitlines()
    # 只把最后一行的注释算作本次新增（其余行都是未改动的既有行）
    added_map = {len(lines): "# 本次只加了这行注释"}
    hits = rg._r3_findings_in_source(content, added_map)
    if hits is None:
        hits = ["（AST 解析失败）"]
    ok = not hits
    if not ok:
        fails.append("范围边界 FAILED：未改动的既有赋值被误判：%s" % hits)
    print("[%s] 未改动的既有 revised_sql 赋值 ⇒ %s" % (
        "OK" if ok else "!!", "⚪ 不判红（正确）" if ok else "🔴 误判！"))
    for h in hits:
        print("     · %s" % h)
    return fails


def main():
    fails = []
    fails += run_cases()
    fails += run_extra()
    fails += run_paths_agree()
    fails += run_negative_control()
    fails += run_negative_control_narrowing()
    fails += run_no_touch_outside_diff()
    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("R3 判据自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
