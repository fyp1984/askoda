#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""交付→候选知识回流（knowledge_backflow.py）纯函数自证（离线，不连知识库）。

为什么单独做这个自证
--------------------
`knowledge_backflow.py` 是 PRD ⑭「交付→候选知识」最小闭环的落点。它的价值
完全在**纯构建**与**优雅降级**两件事上：
  · build_candidate_doc 把交付摊平成候选知识文档——一旦改错，回流的就是
    错内容（比不回流更糟）；
  · propose_candidate_knowledge 在「开关未开 / 未配知识库」时**必须** SKIP 且
    不抛异常，否则会反噬主交付链路。
这两点都不依赖知识库服务，离线可钉死。

用法：
    python3 tools/knowledge_backflow_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

GW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway")
sys.path.insert(0, GW)
import knowledge_backflow as kbf  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# build_candidate_doc：纯构建
# ---------------------------------------------------------------------------

@case("构建：完整 dict 需求 ⇒ 文档含需求单号 / 数据集 / SQL / 候选标记")
def _t_build_full():
    doc = kbf.build_candidate_doc({
        "demand_id": "DR-20260101-AB12",
        "dataset": "B",
        "requirement": {"title": "月度复购率", "business_context": "看复购",
                        "description": "按会员维度统计", "expected_output": "复购率"},
        "final_sql": "SELECT 1",
        "caliber_notes": "复购定义为30天内再次购买",
        "generator": "deterministic-planner",
    })
    for token in ("DR-20260101-AB12", "B", "SELECT 1",
                  "候选知识", "复购定义为30天内再次购买", "deterministic-planner"):
        assert token in doc, "候选文档应含 %r，实际缺失" % token
    # dict 需求应被摊平成文本（含字段内容）
    assert "月度复购率" in doc and "按会员维度统计" in doc, "需求文本未摊平进文档"


@case("构建：str 需求 ⇒ 原样进入文档")
def _t_build_str():
    doc = kbf.build_candidate_doc({"demand_id": "D1", "requirement": "统计本月销售额"})
    assert "统计本月销售额" in doc, "str 需求应原样进入文档"


@case("构建：缺需求 ⇒ 占位符而非抛异常")
def _t_build_no_req():
    doc = kbf.build_candidate_doc({"demand_id": "D2", "final_sql": "SELECT 2"})
    assert "（未提供需求描述）" in doc, "缺需求应有占位符"
    assert "SELECT 2" in doc


@case("构建：None 需求 ⇒ 不抛异常")
def _t_build_none_req():
    doc = kbf.build_candidate_doc({"demand_id": "D3", "requirement": None})
    assert isinstance(doc, str) and doc, "None 需求也应产出非空文档"


# ---------------------------------------------------------------------------
# propose_candidate_knowledge：优雅降级（绝不抛异常）
# ---------------------------------------------------------------------------

@case("回流：开关未开 ⇒ skipped 且不抛异常")
def _t_propose_off():
    saved = os.environ.pop("KNOWLEDGE_BACKFLOW_ENABLED", None)
    try:
        r = kbf.propose_candidate_knowledge({"demand_id": "D4", "final_sql": "SELECT 3"})
        assert r["skipped"] is True and r["ok"] is False, "开关未开应 SKIP：%r" % r
    finally:
        if saved is not None:
            os.environ["KNOWLEDGE_BACKFLOW_ENABLED"] = saved


@case("回流：开关开但无知识库配置 ⇒ skipped（不伪装成功）")
def _t_propose_no_config():
    envs = {}
    for k in ("KNOWLEDGE_BACKFLOW_ENABLED", "KNOWLEDGE_API_URL",
              "KNOWLEDGE_API_KEY", "KNOWLEDGE_CANDIDATE_DATASET_ID", "KNOWLEDGE_DATASET_ID"):
        envs[k] = os.environ.pop(k, None)
    try:
        os.environ["KNOWLEDGE_BACKFLOW_ENABLED"] = "1"
        r = kbf.propose_candidate_knowledge({"demand_id": "D5", "final_sql": "SELECT 4"})
        assert r["skipped"] is True, "无知识库配置应 SKIP：%r" % r
        assert "doc" in r, "SKIP 时仍应回带构建好的候选文档供旁路使用"
    finally:
        for k, v in envs.items():
            if v is not None:
                os.environ[k] = v


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
    print("\nknowledge_backflow_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
