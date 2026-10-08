#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 生成侧的纯函数自证（离线，不需要数据库 / Wren）。

为什么单独做这个自证
--------------------
`sqlgen.py` 是「结构化需求 → SQL」这一段的落点，是主链路之一，
此前**没有任何单元测试**。而 `generate()` 要连数据库取需求、连语义层 dry_run，
没法离线跑；但它内部有几个**纯函数**决定了「生成结果长什么样」，
一旦悄悄改错，外部表现全是"SQL 能跑但内容不对"：

  · `_split_guide` —— 把多行执行指南拆成前端步骤数组。
    它负责**剥掉「①」「1.」这类序号前缀**，由前端统一重新编号。
    如果它改坏了，前端就会出现"步骤编号跟正文对不上"，
    甚至序号被当成正文内容念出来。

  · `_classify_gap` —— 把差距描述分类，**决定前端着色和责任方归属**。
    分错的后果是：把"数据缺失"（要业务+技术一起补）显示成"技术方待上架"，
    指派给错的人，需求就卡住了。

  · `_extract_between` —— 从文本里截取区间，是若干口径解析的基础动作。

这三个都是纯函数，可离线钉死。

用法：
    python3 tools/sqlgen_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import sqlgen as sg  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# _split_guide：执行指南拆步
# ---------------------------------------------------------------------------

@case("拆步：带圈序号应被剥掉（前端统一编号，不能出现两个序号）")
def _t_guide_circle():
    out = sg._split_guide("① 提交需求\n② 确认口径\n③ 生成 SQL")
    assert len(out) == 3, "应拆成3 步，实际=%d 步：%r" % (len(out), out)
    for i, s in enumerate(out):
        assert not s.startswith("①②③④⑤"), (
            "第%d 步仍带圈序号 %r，前端会显示双重编号" % (i + 1, s)
        )
    assert out[0] == "提交需求", "第1 步内容应为「提交需求」，实际=%r" % out[0]


@case("拆步：带点号序号应被剥掉（1. 2. 3.）")
def _t_guide_dot():
    out = sg._split_guide("1. 提交需求\n2. 确认口径\n3. 生成 SQL")
    assert len(out) == 3, "应拆成 3 步，实际=%r" % out
    assert out == ["提交需求", "确认口径", "生成 SQL"], "序号未剥干净：%r" % out


@case("拆步：带顿号序号应被剥掉（1、2、）")
def _t_guide_dun():
    out = sg._split_guide("1、提交需求\n2、确认口径")
    assert out == ["提交需求", "确认口径"], "顿号序号未剥干净：%r" % out


@case("拆步：无序号的纯文本按行拆，且去掉首尾空白")
def _t_guide_plain():
    out = sg._split_guide("  提交需求  \n\n  确认口径\n")
    assert out == ["提交需求", "确认口径"], "空行未被正确跳过或空白未清理：%r" % out


@case("拆步：空输入 / None 返回空数组（前端会直接 .map，不能给 null）")
def _t_guide_empty():
    assert sg._split_guide("") == []
    assert sg._split_guide(None) == []
    assert sg._split_guide("   \n  \n") == [], "纯空白行应产出空数组"


@case("拆步：单步不带序号时原样保留")
def _t_guide_single():
    assert sg._split_guide("直接执行") == ["直接执行"]


@case("拆步：序号前缀不能吃掉正文（剥完仍要有内容）")
def _t_guide_keeps_body():
    out = sg._split_guide("① 这是第一步的正文")
    assert out == ["这是第一步的正文"], "剥序号时把正文一起吃掉了：%r" % out


# ---------------------------------------------------------------------------
# _classify_gap：差距分类与责任方
# ---------------------------------------------------------------------------

@case("分类：「数据缺失 / 无行为类数据」应归业务+技术，且kind=数据缺失")
def _t_gap_missing():
    for t in ("该表没有行为类数据", "数据缺失：会员行为日志表不存在"):
        r = sg._classify_gap(t)
        assert r["kind"] == "数据缺失", (
            "「%s」应归为「数据缺失」，实际=%r" % (t, r["kind"])
        )
        assert "业务" in r["owner"] and "技术" in r["owner"], (
            "「%s」的责任方应同时含业务与技术，实际=%r" % (t, r["owner"])
        )


@case("分类：「口径未定义」应归业务方")
def _t_gap_undefined():
    r = sg._classify_gap("复购率的口径未定义")
    assert r["kind"] == "口径未定义", "实际=%r" % r["kind"]
    assert "业务" in r["owner"], "口径未定义须归业务方，实际=%r" % r["owner"]


@case("分类：「待上架」应归技术方")
def _t_gap_pending():
    r = sg._classify_gap("券核销口径待上架")
    assert r["kind"] == "口径待上架", "实际=%r" % r["kind"]
    assert "技术" in r["owner"], "待上架须归技术方，实际=%r" % r["owner"]


@case("分类：无法识别的应落到默认类，且仍有责任方（不能空 owner）")
def _t_gap_default():
    for t in ("", None, "说不清楚的问题"):
        r = sg._classify_gap(t)
        assert r.get("kind"), "「%r」未分类出 kind" % t
        assert r.get("owner"), "「%r」的 owner 为空，需求将无人可指派" % t


@case("分类：每类都必须给出 owner（前端据此指派，缺了就卡住）")
def _t_gap_all_have_owner():
    for t in ("数据缺失", "口径未定义", "待上架", "乱七八糟"):
        r = sg._classify_gap(t)
        assert isinstance(r, dict) and set(("kind", "owner")) <= set(r), (
            "分类结果必须含 kind 与 owner 两个键，实际=%r" % r
        )


# ---------------------------------------------------------------------------
# _extract_between：区间截取
# ---------------------------------------------------------------------------

@case("截取：正常区间")
def _t_between_ok():
    txt = "口径为[坪效]计算"
    assert sg._extract_between(txt, "[", "]") == "坪效", "实际=%r" % sg._extract_between(txt, "[", "]")


@case("截取：缺失起始符返回空串（不能抛异常，也不能截错）")
def _t_between_no_start():
    assert sg._extract_between("没有方括号", "[", "]") == ""


@case("截取：缺结束符时取到末尾（不能返回空）")
def _t_between_no_end():
    r = sg._extract_between("前缀[内容", "[", "]")
    assert r == "内容", "实际=%r" % r


@case("截取：结果去掉尾部句读")
def _t_between_strip_punct():
    assert sg._extract_between("前缀[内容。]", "[", "]") == "内容", (
        "尾部句读未去掉：%r" % sg._extract_between("前缀[内容。]", "[", "]")
    )


@case("截取：空输入返回空串")
def _t_between_empty():
    assert sg._extract_between("", "[", "]") == ""
    assert sg._extract_between(None, "[", "]") == ""


def main():
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print("  [OK] %s" % name)
        except AssertionError as e:
            failed.append((name, str(e)))
            print("  [FAIL] %s\n         %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            failed.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  [ERROR] %s\n         %s: %s" % (name, type(e).__name__, e))
    total = len(CASES)
    print("\nsqlgen_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())