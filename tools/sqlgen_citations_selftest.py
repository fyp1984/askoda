#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 生成期「取证留痕」接线自证（gateway/sqlgen.py 的 `_record_sqlgen_citations`）。

为什么需要这个脚本
------------------
2026-10-22 实测发现：引用留痕只有 `plan()` 里写，而 **`generate()` 从不调 `plan()`**
（两段式里 plan 是独立工具 `sql_plan`），主链路（前端 → `sql_generate`）因此
**一条 `sqlgen_plan_stage` 都不新增**。

这个缺陷不会报错、不会变红：
  - 引用表里有历史数据 → 「知识引用」页看着有内容；
  - `knowledge_citation_list` 也查得到 → 「⑬引用溯源」看着正常；
  - 唯独「**这段 SQL 是照着哪份口径文档写的**」查不到 —— 而这是业务方核对数据
    来源时唯一真正要看的东西。

⇒ 所以判据必须盯住**动作**（"generate 路径有没有真的去留痕"），
  而不是盯**结果**（"表里有没有行"）——历史行会让结果永远为真。

测什么
------
1. generate 路径：命中引用 ⇒ `record_citations_once` 被调 1 次，且
   `dataset_id='sqlgen_plan_stage'`、`demand_id` 正确、指纹里有选中的表；
2. 检索无命中 ⇒ 不写库、不抛异常；
3. 检索/留痕抛异常 ⇒ **生成照常返回 SQL**（留痕失败绝不能拖垮生成）；
4. 无 demand_id ⇒ 直接跳过，不检索；
5. `plan()` 仍然走同一套（源码级：plan 体内调用的是同一个公共函数）；
6. 幂等键随结论变化而变化（换表 ⇒ 指纹不同；同表同引用 ⇒ 指纹相同）。

全部离线：桩掉 knowledge 模块与 planner / gates / 取需求三个边界，其余跑真实代码。
用法：python3 tools/sqlgen_citations_selftest.py   （退出码 0=全过）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "gateway"))

# 与本机元数据库解耦：本脚本断言的是"有没有留痕"，不是"本机配置是什么"
os.environ["LLM_SETTINGS_SOURCE"] = "env"
for _k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
    os.environ.pop(_k, None)

import knowledge as knowledge_mod  # noqa: E402
import sqlgen as sg                # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# --------------------------------------------------------------------------
# 桩：只桩边界，不桩被测逻辑
# --------------------------------------------------------------------------
class _Rec(object):
    def __init__(self):
        self.calls = []
        self.search_calls = []

    def search(self, q, top_k=4):
        self.search_calls.append((q, top_k))
        return {"ok": True, "citations": [
            {"document_id": "DOC-1", "document_name": "01-指标口径说明书-零售会员域.md",
             "chunk_id": "C-11"},
            {"document_id": "DOC-2", "document_name": "03-表结构与颗粒度说明.md",
             "chunk_id": "C-22"},
        ]}

    def record(self, demand_id, question, citations, **kw):
        self.calls.append({"demand_id": demand_id, "citations": citations, **kw})
        return {"inserted": len(citations), "skipped": None}


class _Patch(object):
    """临时替换若干属性，退出时按原对象复原（漏复原会让后续用例在假环境下跑）。"""

    def __init__(self, pairs):
        self.pairs = pairs
        self.old = []

    def __enter__(self):
        for obj, attr, val in self.pairs:
            self.old.append((obj, attr, getattr(obj, attr)))
            setattr(obj, attr, val)
        return self

    def __exit__(self, *exc):
        for obj, attr, val in reversed(self.old):
            setattr(obj, attr, val)
        return False


def _gen_env(rec):
    """generate() 的边界桩：planner / gates / 取需求 / 取标题。"""
    return _Patch([
        (knowledge_mod, "search", rec.search),
        (knowledge_mod, "record_citations_once", rec.record),
        (sg, "_demand_title_desc", lambda did: ("各门店月度销售额与订单数统计", "按月汇总各门店")),
        (sg, "_requirement_for_gate", lambda did: (None, "自证桩：无结构化需求")),
        (sg.planner_mod, "plan",
         lambda ds, nl: {"sql": "SELECT 1 AS x", "objects": ["dwd_order_di"], "blocked": False}),
        (sg.registry, "get", lambda ds: object()),
        (sg.gates_mod, "review",
         lambda sql, dataset="B", requirement=None: {"status": "通过", "layers": []}),
    ])


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
@case("generate 路径：命中引用 ⇒ 留痕 1 次，stage/demand/指纹都对")
def _t_generate_records():
    rec = _Rec()
    with _gen_env(rec):
        out = sg.generate(demand_id="DR-CIT-0001", dataset="B")
    assert out.get("sql_draft") == "SELECT 1 AS x", out.get("sql_draft")
    assert len(rec.calls) == 1, "留痕应发生 1 次，实际 %d 次" % len(rec.calls)
    c = rec.calls[0]
    assert c["demand_id"] == "DR-CIT-0001", c["demand_id"]
    assert c.get("dataset_id") == "sqlgen_plan_stage", c.get("dataset_id")
    assert c.get("stage") == "sqlgen_plan_stage", c.get("stage")
    assert "ct=dwd_order_di" in (c.get("fingerprint") or ""), c.get("fingerprint")
    assert len(c["citations"]) == 2, c["citations"]
    # 检索词里应带上选中的表，否则检索等于盲搜
    assert rec.search_calls and "dwd_order_di" in rec.search_calls[0][0], rec.search_calls
    assert any("取证留痕" in str(n) for n in (out.get("generation_notes") or [])), \
        out.get("generation_notes")


@case("generate 路径：检索无命中 ⇒ 不写库、不报错、照常出 SQL")
def _t_generate_no_hit():
    rec = _Rec()
    rec.search = lambda q, top_k=4: {"ok": True, "citations": []}
    with _gen_env(rec):
        out = sg.generate(demand_id="DR-CIT-0002", dataset="B")
    assert out.get("sql_draft") == "SELECT 1 AS x", out.get("sql_draft")
    assert rec.calls == [], "无命中不该写库：%r" % rec.calls


@case("generate 路径：留痕抛异常 ⇒ 生成不被拖垮（仍返回 SQL）")
def _t_generate_records_fail():
    rec = _Rec()

    def boom(*a, **k):
        raise RuntimeError("knowledge db down")

    rec.record = boom
    with _gen_env(rec):
        out = sg.generate(demand_id="DR-CIT-0003", dataset="B")
    assert out.get("sql_draft") == "SELECT 1 AS x", "留痕失败不该影响生成"
    assert any("取证留痕失败" in str(n) for n in (out.get("generation_notes") or [])), \
        out.get("generation_notes")


@case("generate 路径：检索抛异常 ⇒ 生成不被拖垮")
def _t_generate_search_fail():
    rec = _Rec()

    def boom(*a, **k):
        raise RuntimeError("search exploded")

    rec.search = boom
    with _gen_env(rec):
        out = sg.generate(demand_id="DR-CIT-0004", dataset="B")
    assert out.get("sql_draft") == "SELECT 1 AS x", "检索失败不该影响生成"


@case("无 demand_id ⇒ 直接跳过，不检索也不写库")
def _t_no_demand():
    rec = _Rec()
    with _gen_env(rec):
        out = sg.generate(demand_id=None, dataset="B")
    # 无 demand_id 时 nl 构造不出来 ⇒ planner 走 _refuse（这是既有语义，不是本用例的事）；
    # 本用例只关心「留痕这一步不该被触发」。
    assert isinstance(out, dict) and "sql_draft" in out, out
    assert rec.search_calls == [] and rec.calls == [], (rec.search_calls, rec.calls)


@case("幂等键：同结论同引用 ⇒ 指纹相同；换表 ⇒ 指纹不同")
def _t_fingerprint():
    rec = _Rec()
    seen = []

    def record(demand_id, question, citations, **kw):
        seen.append(kw.get("fingerprint"))
        return {"inserted": 1}

    rec.record = record
    with _gen_env(rec):
        sg._record_sqlgen_citations("DR-CIT-0005", chosen_table="t_a")
        sg._record_sqlgen_citations("DR-CIT-0005", chosen_table="t_a")
        sg._record_sqlgen_citations("DR-CIT-0005", chosen_table="t_b")
    assert len(seen) == 3, seen
    assert seen[0] == seen[1], "同表同引用 ⇒ 指纹应相同：%r" % seen[:2]
    assert seen[0] != seen[2], "换表 ⇒ 指纹必须不同：%r" % seen


@case("源码级：plan() 与 generate() 走的是**同一个**留痕函数")
def _t_source_single_path():
    import inspect

    plan_src = inspect.getsource(sg.plan)
    gen_src = inspect.getsource(sg.generate)
    assert "_record_sqlgen_citations(" in plan_src, "plan() 没走公共留痕函数"
    assert "_record_sqlgen_citations(" in gen_src, "generate() 没走公共留痕函数"


def main():
    ok = 0
    fail = []
    for name, fn in CASES:
        try:
            fn()
            ok += 1
            print("  [OK] %s" % name)
        except AssertionError as e:
            fail.append((name, str(e)))
            print("  [FAIL] %s\n         %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            fail.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  [ERROR] %s\n         %s: %s" % (name, type(e).__name__, e))
    print()
    print("sqlgen_citations_selftest: %d/%d 通过" % (ok, len(CASES)))
    if fail:
        print("失败项：")
        for n, m in fail:
            print("  - %s：%s" % (n, m))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
