#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口侧脱敏自证（离线，不需要网关与数据库）。

为什么值得单独立一份
--------------------
`gateway/masking.py` 在需求受理的**入口**跑，任何一处误判都有代价，且两个方向的
代价不对称：

- **漏判**（真敏感值没掩）= 敏感数据落进库，是安全事故；
- **误判**（把普通词当人名掩掉）= 需求单被改成"读不通"，如实测踩到的
  「会员复购情况分析」被判成客户姓名。

实测已修掉两类真实缺陷，本文件把结论钉成断言防回归：
1. 姓名误判 —— 「会员复购」被当人名。修法是给姓名规则加"首字必须是常见姓氏"闸门。
2. 重复计数 —— 一个 18 位身份证同时被算作身份证号**和**银行卡号。修法是按区间去重。

用法：
    python3 tools/masking_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import masking as mk  # noqa: E402


# 正例：(文本, 期望命中的类型集合)
POSITIVE = [
    ("手机 13800001234", {"手机号"}),
    ("身份证 110101199001011234", {"身份证号"}),
    ("邮箱 zhangwei@example.com", {"邮箱"}),
    ("客户张伟是做服装批发的", {"客户姓名"}),
    ("王经理负责这块业务", {"客户姓名"}),
    ("客户号 A8899123456 的复购情况", {"账号标识"}),
    ("银行卡 6222021234567890123", {"银行卡号"}),
]

# 负例：这些**不得**被判成客户姓名（真实误判过的词）
NEGATIVE = [
    "会员复购情况分析",
    "总体来看券核销率偏低",
    "客户等级分布怎么样",
    "订单金额按月汇总",
    "优惠券核销口径",
    "门店坪效对比",
]


def main():
    fails = []

    print("— 正例：应当命中 —")
    for text, expect in POSITIVE:
        _masked, types = mk.summarize_text(text)
        got = set(types)
        ok = expect.issubset(got)
        if not ok:
            fails.append("POS %r 期望含 %s，实际 %s" % (text, expect, got))
        print("  [%s] %-34s -> %s" % ("OK" if ok else "!!", text, sorted(got)))

    print()
    print("— 负例：不得误判为姓名 —")
    for text in NEGATIVE:
        _masked, types = mk.summarize_text(text)
        ok = "客户姓名" not in types
        if not ok:
            fails.append("NEG %r 被误判为客户姓名" % text)
        print("  [%s] %-34s -> %s" % ("OK" if ok else "!!", text, sorted(types) or "无命中"))

    print()
    print("— 身份证不得被重复计为银行卡号 —")
    text = "身份证 110101199001011234"
    _m, types = mk.summarize_text(text)
    ok = types.get("身份证号") == 1 and "银行卡号" not in types
    if not ok:
        fails.append("OVERLAP %r -> %s" % (text, types))
    print("  [%s] %s" % ("OK" if ok else "!!", types))

    print()
    print("— 掩码后不得残留明文 —")
    plain = "客户张伟手机 13800001234 身份证 110101199001011234 邮箱 a@b.com"
    masked = mk.mask(plain)
    leaks = [p for p in ("13800001234", "110101199001011234", "a@b.com", "张伟") if p in masked]
    ok = not leaks
    if not ok:
        fails.append("LEAK %s" % leaks)
    print("  [%s] %s" % ("OK" if ok else "!!", masked))
    if leaks:
        print("       残留明文：%s" % leaks)

    print()
    print("— 掩码幂等（对已掩码文本再掩一次不应变化）—")
    twice = mk.mask(masked)
    ok = twice == masked
    if not ok:
        fails.append("IDEMPOTENT 二次掩码结果不同\n    一次=%s\n    二次=%s" % (masked, twice))
    print("  [%s] 一次 == 二次：%s" % ("OK" if ok else "!!", ok))

    print()
    print("— 掩码结果仍可读（不得整段抹掉）—")
    # 掩码的副作用是让需求单失去可读性，这里守住"保留可读骨架"
    ok = len(masked) >= len(plain) * 0.5
    if not ok:
        fails.append("READABLE 长度塌缩：%d -> %d" % (len(plain), len(masked)))
    print("  [%s] 原长 %d -> 掩码后 %d" % ("OK" if ok else "!!", len(plain), len(masked)))

    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
