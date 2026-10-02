#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识检索「问句压缩」自证（离线，不需要网络与 RAGFlow）。

为什么单独做这个自证
--------------------
`extract_keywords()` 的任务是把业务人员的口语化问句压成关键词，它只在**零命中**
时才被触发——也就是说，一旦它悄悄改错了问题，外部表现为"检索返回了别的东西"，
极难归因。实测踩到过两个真实损坏：

  「系统性能指标有哪些」→「系统性 指标」  （"能"被当虚词全局删掉，且"有哪些"残留）
  「订单金额是否包含运费」→「订单金额 否包含运费」  （"是"被从句中删掉，把"是否"劈成"否"）

根因是用**子串全局替换**处理单字虚词。因此现在的实现分两档：
多字短语可全局替换；单字虚词只在句首/句尾剥离。

本文件把这两类约束都钉成断言，防止回归。用法：
    python3 tools/knowledge_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import knowledge as kb  # noqa: E402


# 口语化问句 → 压缩后必须包含的关键词（关键词检索要能凭它召回）
TARGETS = [
    ("坪效怎么算", "坪效"),
    ("复购率怎么算", "复购率"),
    ("什么是坪效", "坪效"),
    ("会员手机号能查吗", "会员手机号"),
    ("券核销的口径能不能分析", "券核销"),
    ("订单和订单行有什么区别", "订单和订单行"),
    ("需求提交时哪些字段必填", "字段必填"),
    ("订单金额是多少", "订单金额"),
]

# 领域词保护：这些词**必须原封不动**留在压缩结果里
GUARDS = [
    ("系统性能指标有哪些", "系统性能指标"),
    ("订单金额是否包含运费", "订单金额"),
    ("数据质量校验规则", "数据质量校验规则"),
    ("复购率的计算方式", "复购率"),
    ("门店坪效", "门店坪效"),
    ("客户等级分布", "客户等级"),
    ("优惠券核销率", "优惠券核销率"),
]

# 压缩结果不得残留的口语成分
RESIDUE = ["怎么算", "如何算", "是什么", "什么是", "有哪些", "有什么区别", "能不能"]


def main():
    fails = []

    print("— 目标：口语化问句压回关键词 —")
    for q, must in TARGETS:
        got = kb.extract_keywords(q)
        ok = must in got
        if not ok:
            fails.append("TARGET %s -> %r 需含 %r" % (q, got, must))
        print("  [%s] %-22s -> %r" % ("OK" if ok else "!!", q, got))

    print()
    print("— 回归：领域词不得被削 —")
    for q, must in GUARDS:
        got = kb.extract_keywords(q)
        ok = must in got
        if not ok:
            fails.append("GUARD %s -> %r 需含 %r" % (q, got, must))
        print("  [%s] %-22s -> %r" % ("OK" if ok else "!!", q, got))

    print()
    print("— 回归：压缩结果不得残留口语成分 —")
    for q, _ in TARGETS:
        got = kb.extract_keywords(q)
        left = [w for w in RESIDUE if w in got]
        ok = not left
        if not ok:
            fails.append("RESIDUE %s -> %r 残留 %s" % (q, got, left))
        print("  [%s] %-22s 残留=%s" % ("OK" if ok else "!!", q, left or "无"))

    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("自证全部通过（%d 目标 + %d 保护 + %d 残留）"
          % (len(TARGETS), len(GUARDS), len(TARGETS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
