# -*- coding: utf-8 -*-
"""证据编排 · 词表与命中自证（离线/近线，可随时复跑）

守什么
------
本文件守的是**词表污染**这一类缺陷。2026-09-30 实测踩到过：字段标签
`会员ID（关联 dim_member；与 stat_month 联合唯一）` 因「先按 ；切分」被劈成
`会员ID（关联 dim_member`——带残缺左括号进词表。这类脏词条有两个后果：
① 永远匹配不上真实业务文本；② 若碰巧匹配上，会把错误位置当证据。

所以断言分两组：
  A. `clean_label` 的纯函数用例（不需数据库，任何环境可跑）
  B. 真实词表体检（需元数据库可用，不可用时如实跳过而非假装通过）

用法：
    python tools/evidence_selftest.py
退出码：0 全过；1 有断言失败
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "gateway"))

import evidence as ev  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        print("  \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)


# ---------------------------------------------------------------------------
# A. clean_label 纯函数用例
# ---------------------------------------------------------------------------
def test_clean_label():
    print("\nA. clean_label —— 标签清洗（纯函数）")
    cases = [
        # (输入, 期望)
        ("会员ID（关联 dim_member；与 stat_month 联合唯一）", "会员ID"),
        ("业务日期（分析一律用此字段；装载时间不建模，禁止用装载时间过滤业务日期）", "业务日期"),
        ("销售金额（元）=数量×单价；口径：订单金额=SUM(sales_amount)，退款单除外", "销售金额"),
        ("维度退化列：品类名（美妆/个护/食品/服饰/家居/数码），免 JOIN 直接分组", "品类名"),
        ("会员等级；枚举：普通 / 银卡 / 金卡 / 钻石", "会员等级"),
        ("门店经营面积（平方米）", "门店经营面积"),
        ("券面额（元）", "券面额"),
        ("券种；枚举：满减券 / 折扣券 / 新人券。按券种细分核销属【待上架】口径，暂不可分析", "券种"),
        ("当月是否复购：0/1", "当月是否复购"),
        ("统计月份（月粒度，存当月1日）", "统计月份"),
        ("", ""),
    ]
    for raw, want in cases:
        got = ev.clean_label(raw)
        check("clean_label(%r) == %r" % (raw[:22], want), got == want, "实际 = %r" % got)

    # 反例：清洗结果不得残留残缺括号 / 角色前缀 / 尾标点
    dirty = ["会员ID（关联 dim_member", "维度退化列：品类名", "订单金额，", "券面额（元"]
    for d in dirty:
        got = ev.clean_label(d)
        ok = not any(ch in got for ch in "（）") and not got.endswith(("，", "。", "：")) \
            and not ev.ROLE_PREFIX_RE.match(got)
        check("清洗后无残留：%r -> %r" % (d, got), ok)


# ---------------------------------------------------------------------------
# B. 真实词表体检
# ---------------------------------------------------------------------------
def test_lexicon():
    print("\nB. 词表体检（需元数据库）")
    try:
        lex = ev.build_lexicon("B")
    except Exception as e:  # noqa: BLE001
        print("  \033[33mSKIP\033[0m 元数据库不可用，跳过词表体检：%s" % str(e)[:100])
        return None

    check("词表非空", len(lex) > 20, "实际 %d 条" % len(lex))

    bad = [
        t["term"]
        for t in lex
        if "（" in t["term"] or "）" in t["term"] or "：" in t["term"]
        or "【" in t["term"] or t["term"].endswith(("，", "。"))
    ]
    check("无残缺/脏词条", not bad, "坏词条：%s" % bad[:6])

    # 关键标准词必须在册，且指向真实 locator
    must = ["坪效", "复购率", "销售额", "活跃用户数", "会员等级", "门店经营面积",
            "订单状态", "大区", "品类名", "业务日期"]
    missing = [m for m in must if not any(t["term"] == m for t in lex)]
    check("关键标准词在册", not missing, "缺：%s" % missing)

    noloc = [t["term"] for t in lex if not t.get("locator")]
    check("每条词条都有 locator（可溯源）", not noloc, "无 locator：%s" % noloc[:6])

    # 未建模字段不得进词表（对 AI 不可见的东西不能参与匹配）
    hidden = [t["term"] for t in lex if t["term"] in ("会员手机号", "身份证号", "装载时间")]
    check("未建模字段未进词表", not hidden, "混入：%s" % hidden)

    # 别名：必须在册，且 canonical 指向真实 locator
    alias_ok, alias_bad = 0, []
    for canonical, aliases in ev.SYNONYMS.items():
        for a in aliases:
            hit = [t for t in lex if t["term"] == a]
            if hit and hit[0].get("synonym_of") and hit[0].get("locator"):
                alias_ok += 1
            else:
                alias_bad.append(a)
    check("口语别名在册且指向真实字段", not alias_bad, "未生效：%s" % alias_bad)
    print("       别名生效 %d 条" % alias_ok)
    return lex


# ---------------------------------------------------------------------------
# C. 命中质量
# ---------------------------------------------------------------------------
def test_matching(lex):
    print("\nC. 中文命中（含别名与不重叠）")
    if not lex:
        print("  \033[33mSKIP\033[0m 无词表，跳过")
        return
    text = "想看近30天各门店的坪效和会员复购率，按区域汇总，排除已退款订单，输出门店名称、营业额、活跃会员数"
    hits = ev.match_terms(text, lex)
    got = {h["matched"] for h in hits}
    for want in ["坪效", "复购率", "已退款", "区域", "营业额", "活跃会员数"]:
        check("命中 %s" % want, want in got, "实际命中：%s" % sorted(got))

    # 不重叠：命中区间两两不相交
    spans = sorted([tuple(h["span"]) for h in hits])
    overlap = [(a, b) for a, b in zip(spans, spans[1:]) if a[1] > b[0]]
    check("命中区间互不重叠", not overlap, "重叠：%s" % overlap)

    # 命中位置必须真的是该词
    bad_span = [h["term"] for h in hits if text[h["span"][0]:h["span"][1]] != h["matched"]]
    check("命中位置与词面一致", not bad_span, "错位：%s" % bad_span)


def main():
    print("=" * 66)
    print("证据编排 · 词表与命中自证")
    print("=" * 66)
    test_clean_label()
    lex = test_lexicon()
    test_matching(lex)
    print()
    print("-" * 66)
    print("共 %d 项断言，失败 %d 项" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ %s" % f)
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
