#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""需求受理（demand.py）纯函数自证（离线，不连数据库）。

为什么单独做这个自证
--------------------
`demand.py` 是「业务自然语言 → 需求单」的入口，四道提交校验（核心字段 /
附件 / 重复 / 时间范围自洽）决定了「什么样的需求能被受理」。这些校验逻辑
一旦悄悄改错，外部表现是"不该收的需求被收了"或"该拦的没拦"，且没有任何报错。

本自证只测**纯逻辑**：`validate()` 内部会调 `find_similar()`（查库找相似需求），
那里用 monkeypatch 替换为"返回空"——相似度比对属于独立复核范畴，不在这条
单元链路里。四道校验本身不碰库，离线可钉死。

用法：
    python3 tools/demand_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import demand as dm  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# find_similar 查库找相似需求，单元链路里用空结果替掉，隔离 DB 依赖。
def _no_similar(*_a, **_k):
    return []


# ---------------------------------------------------------------------------
# 校验：核心字段（必填 + 核心三件套）
# ---------------------------------------------------------------------------

@case("核心字段：完全空 payload ⇒ 必填缺失且核心三件套阻断")
def _t_core_empty():
    dm.find_similar = _no_similar
    r = dm.validate({})
    core = next(c for c in r["checks"] if c["name"] == "core_fields")
    assert not core["passed"], "空 payload 不应通过核心字段校验"
    assert core["blocking"], "核心三件套缺失必须是阻断项"


@case("核心字段：必填齐但核心三件套缺 ⇒ 仍阻断")
def _t_core_trio_missing():
    dm.find_similar = _no_similar
    r = dm.validate({
        "title": "测试需求",
        "contact": "a@b.com",
        # 故意缺 business_context / description / expected_output
    })
    core = next(c for c in r["checks"] if c["name"] == "core_fields")
    assert not core["passed"] and core["blocking"], "核心三件套缺失必须阻断"


@case("附件：未知扩展名 ⇒ 警告（不阻断）且列出文件名")
def _t_attachment_bad():
    dm.find_similar = _no_similar
    r = dm.validate(
        {"title": "t", "business_context": "x", "description": "y",
         "expected_output": "z", "contact": "a@b.com"},
        attachments=[{"filename": "report.xyz"}],
    )
    att = next(c for c in r["checks"] if c["name"] == "attachment")
    assert not att["passed"] and not att["blocking"], "未知扩展名应仅警告不阻断"
    assert "report.xyz" in att["detail"], "应点名无法识别的附件"


@case("时间范围：含强时间口径但无时间范围 ⇒ 自洽警告（不阻断）")
def _t_time_conflict():
    dm.find_similar = _no_similar
    r = dm.validate({
        "title": "本月销售概览",
        "business_context": "看本月销售",
        "description": "统计本月销售额",
        "expected_output": "销售额汇总",
        "contact": "a@b.com",
        # 无 time_range，但文本含「本月」
    })
    ts = next(c for c in r["checks"] if c["name"] == "time_scope")
    assert not ts["passed"] and not ts["blocking"], "时间口径矛盾应警告不阻断"
    assert "本月" in ts["detail"], "应点出命中的强时间口径词"


@case("完整合法 payload ⇒ 四道校验全过、无阻断项")
def _t_all_pass():
    dm.find_similar = _no_similar
    r = dm.validate({
        "title": "Q1 复购率分析",
        "business_context": "分析 Q1 复购",
        "description": "按会员维度统计 Q1 复购率",
        "expected_output": "复购率指标",
        "contact": "a@b.com",
        "time_range": "2026-01-01~2026-03-31",
        "attachments": [{"filename": "data.csv"}],
    })
    assert not r["blocking"], "合法需求不应有阻断项：%r" % r["blocking"]
    for c in r["checks"]:
        assert c["passed"], "校验项 %s 应通过，实际 detail=%s" % (c["name"], c["detail"])


# ---------------------------------------------------------------------------
# 常量 / 状态机契约
# ---------------------------------------------------------------------------

@case("状态机：默认状态在合法状态集合内，且集合含终态")
def _t_status_machine():
    assert dm.DEFAULT_STATUS in dm.STATUSES, "默认状态必须合法"
    for s in ("已通过", "已退回"):
        assert s in dm.STATUSES, "状态机必须包含终态 %s" % s


@case("常量：附件白名单含 csv，强时间词含「本月」")
def _t_constants():
    assert ".csv" in dm.KNOWN_EXT, "csv 必须可识别"
    assert "本月" in dm.STRONG_TIME_WORDS, "「本月」必须作为强时间口径词"
    assert "title" in dm.REQUIRED, "title 是必填项"


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
    print("\ndemand_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
