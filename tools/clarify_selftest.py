#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「退回需求方补充」规则引擎自证（离线，不需要网关、不需要数据库）。

为什么值得单独立一份
--------------------
CR010 的产品判断是「需求说不清时，退需求比补知识库快两个数量级」。
这套判断全压在 ``gateway/clarify.py`` 的规则上，而规则最容易出的错不是"不工作"，
是**乱问**：让业务方回答一堆他答不了的问题（"表为什么不存在"），
或者把已经写清楚的口径再问一遍。那是把最便宜的路径变成最贵的一条。

本文件把下列判据钉成断言：

1. 含糊需求 → 判给 demand，且必答项覆盖「定义 / 时间 / 维度」；
2. 完整需求 → 不再生成必答项（route 落到 knowledge / engineering）；
3. 每条待明确项**必须有原文证据** (anchor)，不许凭空追问；
4. 已确认的要素进 already_known，不重复问；
5. 数据缺失类缺口不问需求方（问也白问）；
6. 一键采纳有落点：suggested_description 非空且能写回描述；
7. compose_supplement 追加而非覆盖原文。

用法：
    python3 tools/clarify_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

import clarify  # noqa: E402

RESULTS = []


def case(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:
            RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))
        return fn

    return deco


VAGUE = {
    "demand_id": "DR-TEST-VAGUE",
    "title": "各门店新增会员数",
    "business_context": "会员运营月度例会要看新增会员的门店分布。",
    "description": "统计各门店新增会员数，按会员数降序排列。",
    "expected_output": "门店名称、新增会员数",
    "contact": "13800000000",
    "time_range": "",
}


@case("① 含糊需求判给 demand，且必答项覆盖定义/时间/维度")
def _():
    pack = clarify.build(VAGUE, requirement=None, gap_kind="口径未建模")
    assert pack["route"] == "demand", "route=%s" % pack["route"]
    slots = {i["slot"] for i in pack["items"] if i["required"]}
    assert "指标定义" in slots, "缺「指标定义」：%s" % slots
    assert "统计区间" in slots, "缺「统计区间」：%s" % slots
    assert pack["required_count"] >= 2, "required=%d" % pack["required_count"]
    assert "新增" in pack["headline"] or "关键信息" in pack["headline"]


@case("①b 只写「表单/图表」没点字段 → 追问输出字段")
def _():
    d = dict(VAGUE)
    d["expected_output"] = "以表单和图表方式输出"
    pack = clarify.build(d, requirement=None, gap_kind="口径未建模")
    slots = {i["slot"] for i in pack["items"]}
    assert "输出字段" in slots, slots


@case("①c 示例跟着主体走：会员需求不许出现「一家门店」")
def _():
    d = dict(VAGUE)
    d["title"] = "汇总会员信息"
    d["description"] = "统计系统里所有会员信息"
    d["expected_output"] = "表单展示"
    pack = clarify.build(d, requirement=None, gap_kind="口径未建模")
    desc = pack["suggested_description"]
    assert "店" not in desc, "会员需求却给了门店示例：%s" % desc
    assert "会员" in desc, desc


@case("② 每条待明确项都有原文证据，不许凭空问")
def _():
    pack = clarify.build(VAGUE, requirement=None, gap_kind="口径未建模")
    for i in pack["items"]:
        assert i.get("anchor"), "item 无 anchor：%s" % i.get("question")
        assert i.get("question"), "item 无 question"
        assert i.get("options"), "item 无选项：%s" % i.get("question")


CLEAR = {
    "demand_id": "DR-TEST-CLEAR",
    "title": "2026年1月各门店新增会员数",
    "business_context": "月度例会看分布。",
    "description": (
        "新增会员以注册日期为准，统计 2026-01-01 至 2026-01-31 首次注册的会员，"
        "按门店汇总，结果一行 = 一家门店，按会员数降序排列，并列按门店编号升序。"
    ),
    "expected_output": "门店名称、新增会员数两列，按新增会员数从高到低排序",
    "contact": "西北人",
    "time_range": "2026-01-01 至 2026-01-31",
}


@case("③ 需求已写清 → 不再有必答项")
def _():
    pack = clarify.build(CLEAR, requirement=None, gap_kind="口径未建模")
    assert pack["required_count"] == 0, "仍有必答项：%s" % [
        i["question"] for i in pack["items"] if i["required"]
    ]


@case("④ 数据缺失类缺口不问需求方，直接判技术侧")
def _():
    pack = clarify.build(CLEAR, requirement=None, gap_kind="数据缺失",
                         gap_text="B 库无「库存」相关表")
    assert pack["route"] == "engineering", "route=%s" % pack["route"]
    assert pack["required_count"] == 0, "不该有必答项"


@case("⑤ 需求已写清但口径未登记 → 判知识侧，且给可选确认项")
def _():
    pack = clarify.build(CLEAR, requirement=None, gap_kind="口径未建模")
    assert pack["route"] == "knowledge", "route=%s" % pack["route"]
    slots = {i["slot"] for i in pack["items"]}
    assert "口径归属" in slots, "缺口径归属确认：%s" % slots
    assert all(not i["required"] for i in pack["items"]), "知识侧不应有必答项"


@case("⑥ 已确认的要素进 already_known，不重复问")
def _():
    req = {
        "granularity": {"value": "按门店汇总，一行 = 一家门店"},
        "subject": {"value": "dim_member"},
        "output_fields": [{"value": "门店"}, {"value": "新增会员数"}],
    }
    demand = dict(VAGUE)
    demand["time_range"] = "2026-01-01 至 2026-01-31"
    pack = clarify.build(demand, requirement=req, gap_kind="口径未建模")
    labels = {k["label"] for k in pack["already_known"]}
    assert "颗粒度" in labels, labels
    assert "需求单时间范围" in labels, labels
    slots = {i["slot"] for i in pack["items"] if i["required"]}
    assert "统计区间" not in slots, "已填时间范围却还在问：%s" % slots
    assert "输出维度" not in slots, "已给输出字段却还在问：%s" % slots


@case("⑦ 一键采纳有落点：suggested_description 非空且覆盖必答项")
def _():
    pack = clarify.build(VAGUE, requirement=None, gap_kind="口径未建模")
    desc = pack["suggested_description"]
    assert desc and len(desc) > 10, "补充草稿为空"
    for i in pack["items"]:
        if i["required"]:
            assert i["slot"] in desc, "草稿缺「%s」：%s" % (i["slot"], desc)


@case("⑧ compose_supplement 追加成段，不覆盖原文")
def _():
    block = clarify.compose_supplement(
        [{"question": "「新增」按哪个口径算？", "answer": "首次注册，以注册日期为准"}],
        extra_description="",
    )
    assert block.startswith("\n\n【需求补充】"), repr(block[:20])
    assert "首次注册" in block
    assert clarify.compose_supplement([], "") == "", "空输入应返回空串"


@case("⑨ 金额类需求未声明排除项 → 生成「排除项」问句")
def _():
    d = dict(VAGUE)
    d["description"] = "统计各门店销售额与订单数"
    d["title"] = "门店销售额"
    pack = clarify.build(d, requirement=None, gap_kind="口径未建模")
    slots = {i["slot"] for i in pack["items"]}
    assert "排除项" in slots, slots


@case("⑩ 需求单为空也不崩，且明确说缺什么")
def _():
    pack = clarify.build({}, requirement=None, gap_kind="口径未建模")
    assert pack["route"] == "demand"
    assert pack["items"], "空需求应至少问出时间/维度"
    assert pack["demand_id"] is None


def main():
    ok = sum(1 for r in RESULTS if r[0])
    for passed, name, err in RESULTS:
        print(("  OK  " if passed else " FAIL ") + name + ("" if passed else " → " + err))
    print("\nclarify_selftest: %d/%d" % (ok, len(RESULTS)))
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
