#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REQ-COVERAGE 阶段二 · **接线**自证（证明 L-REQ 在**生产链路**上真的生效了）

⚠️ 阶段二口径变更（2026-10-20）：``_requirement_check`` 已换成覆盖判定实现，
   签名从 ``(requirement)`` 变成 ``(requirement, sql=None)``——
   覆盖判定必须拿得到 SQL 才谈得上「覆盖没覆盖」。
   ⇒ 本脚本的断言随之改写：**L-REQ 不再是「有没有传 requirement」的开关，
   而是「有没有把 SQL 与需求一起喂进去」的开关**。

为什么需要这个脚本（不能只看门禁绿）
------------------------------------
门禁绿只证明「没有把已有行为搞坏」，**不证明**新能力真的接上了。
本脚本自证一件具体的事：

    生产链路（sql_generate / sql_execute_readonly）上，L-REQ 那一层必须
    **真的参与审查**（``skipped=False``）——而不是因为「没传 SQL」
    或「取不到需求」而静默跳过。

🔴 静默失效是这个项目正在修的那个病
------------------------------------
「取不到 ⇒ skipped」是**设计意图**（无可比对需求就不该判红），
但它同时是**最危险的形态**：接线漏了、需求取不到、SQL 没传，
三者都表现为同一个「skipped=True」——**分不清是"没接线"还是"没需求"**。
⇒ 所以本脚本不只断言 skipped=False，还要断言
   **「SQL 真的被传进去了」**（记录 gates.review 每次调用的实参），
   否则「skipped=False」也可能是别的路径蒙对的。

⚠️ 离线可跑（不依赖 DB / 容器 / Wren）
-------------------------------------
要素抽取要走 ``reqelem.build_elements_from_requirement``，它内部要反查
需求原文（**真·DB 边界**）。本脚本只桩**那两个 DB 边界**
（``demand.get`` / ``evidence.build_lexicon``），
**其余全部跑真实代码**（要素抽取、粒度归类、judge 判定、门禁聚合）。
⚠️ 刻意**不桩要素层**：桩掉要素层就等于自证建立在合成要素的假定上，
   那与本项目 REQ-COVERAGE 缺陷同构。

用法：
    python3 tools/reqcov_wiring_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import contextlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))

import demand as _demand_mod          # noqa: E402
import evidence as _evidence_mod      # noqa: E402
import gates as gt                    # noqa: E402
import reqelem as _reqelem_mod        # noqa: E402
import requirement as _requirement_mod  # noqa: E402
import sqlgen as sqlgen_mod           # noqa: E402
import sqlrun as sqlrun_mod           # noqa: E402


#: 一条带**月度**粒度需求的需求单（离线夹具，字段照抄 demand.get 的返回形状）
DEMAND_WITH_MONTH = {
    "demand_id": "DR-WIRE-0001",
    "dataset": "B",
    "status": "已归档",
    "title": "各门店月度销售额与订单数统计",
    "business_context": "需要看每个门店的月度销售额，同时要订单数",
    "description": "按月汇总各门店",
    "expected_output": "门店、月份、销售额、订单数",
}
_ALL_DEMANDS = {DEMAND_WITH_MONTH["demand_id"]: DEMAND_WITH_MONTH}

#: SQL：GROUP BY 里**没有**月度粒度 ⇒ 月度要素应判「未覆盖」
#:
# ⚠️ 必须选一条**能过 L4 语义校验**的 SQL（B 库真实建模）：
#   预审查一旦被 L4 判红就**提前 return**，后审查根本不会跑 ⇒
#   「预审查 + 后审查都接线了」这条断言就只剩一半可测。
#   实测：`dws_store_daily_agg` 的合法列是 stat_date / store_id 等，
#   `store_name` 在该模型上不存在（那是 dim_store 的列）⇒ L4 报
#   "No field named store_name" ⇒ 预审查直接判红。
SQL_NO_MONTH = ("SELECT store_id, COUNT(*) AS cnt "
                "FROM dws_store_daily_agg GROUP BY store_id")
#: SQL：GROUP BY 里**有**月度粒度（用 to_char 写法）⇒ 月度要素应判「已覆盖」
SQL_WITH_MONTH = ("SELECT store_id, to_char(stat_date, 'YYYY-MM') AS ym, "
                   "SUM(sales_amount) AS sales FROM dws_store_daily_agg "
                   "GROUP BY store_id, to_char(stat_date, 'YYYY-MM')")


def _lreq(review):
    """从 review 结果里取出 L-REQ 层。"""
    for layer in (review or {}).get("layers") or []:
        if layer.get("layer") == "L-REQ":
            return layer
    return None


def _fails(msg):
    print("  ✗ %s" % msg)
    return False


@contextlib.contextmanager
def _offline_env(mode=None):
    """桩掉 DB 边界，让覆盖判定链在**无 DB** 环境里跑真实代码。

    ⚠️ 桩的**只有 DB 边界**（三处），其余全部跑真实代码：
      ① ``requirement.get``   —— 取结构化需求对象（sqlgen/sqlrun 走这条）
      ② ``demand.get``        —— 取需求单原文（reqelem 重建 bundle 走这条）
      ③ ``evidence.build_lexicon`` —— 取词表
    ⚠️ **刻意不桩要素层**（``reqelem.build_elements_from_requirement``）：
       桩掉它就等于自证建立在合成要素的假定上——那与本项目 REQ-COVERAGE
       缺陷同构（"看起来判过了，其实判的是别的东西"）。

    ⚠️ 离开时**必须复原**四样（DB 桩 ×3 / 模式 / bundle 缓存）：
       漏复原会让后续用例在「假环境」下跑，绿灯不再可信——
       这本身又是一处静默失效。
    """
    _orig_req = _requirement_mod.get
    _orig_get = _demand_mod.get
    _orig_lex = _evidence_mod.build_lexicon
    _orig_mode = os.environ.get(gt._REQ_COVERAGE_MODE_ENV)
    _orig_cache = dict(getattr(_reqelem_mod, "_BUNDLE_CACHE", {}) or {})

    def _fake_req_get(demand_id, version=None):
        # 结构化需求对象：形状照抄 requirement.build 的产出（顶层**无 ok 键**）
        if demand_id not in _ALL_DEMANDS:
            return {"ok": False, "error": "需求 %s 不存在" % demand_id}
        return _requirement_of(demand_id)

    def _fake_get(demand_id, *a, **kw):
        d = _ALL_DEMANDS.get(demand_id)
        return dict(d) if d else None

    _requirement_mod.get = _fake_req_get
    _demand_mod.get = _fake_get
    _evidence_mod.build_lexicon = lambda dataset="B": []
    if mode is not None:
        os.environ[gt._REQ_COVERAGE_MODE_ENV] = mode
    try:
        yield
    finally:
        _requirement_mod.get = _orig_req
        _demand_mod.get = _orig_get
        _evidence_mod.build_lexicon = _orig_lex
        if mode is not None:
            if _orig_mode is None:
                os.environ.pop(gt._REQ_COVERAGE_MODE_ENV, None)
            else:
                os.environ[gt._REQ_COVERAGE_MODE_ENV] = _orig_mode
        cache = getattr(_reqelem_mod, "_BUNDLE_CACHE", None)
        if cache is not None:
            cache.clear()
            cache.update(_orig_cache)


def _requirement_of(demand_id, dataset="B"):
    """造一份结构化需求对象（形状照抄 requirement.build 的产出）。

    ⚠️ 只放覆盖判定真正要用的键：reqelem 靠 demand_id / dataset 重建 bundle。
    """
    return {"demand_id": demand_id, "dataset": dataset, "version": 1,
            "source_round": 1, "schema_version": 1, "status": "待审核"}


def test_review_direct():
    """对照组：gates.review 本身的语义（阶段二口径）。"""
    print("=" * 72)
    print("1. 对照组：gates.review ⇒ L-REQ 是否真的参与审查（skipped=False）")
    print("=" * 72)
    ok = True
    req = _requirement_of(DEMAND_WITH_MONTH["demand_id"])

    with _offline_env():
        # ① 不传 SQL ⇒ 判不了覆盖 ⇒ skipped（这是**设计意图**，不是缺陷）
        #    ⚠️ 必须真的传**空串**（而不是换一条别的 SQL）：传了别的 SQL 就会
        #    正常进入覆盖判定，skipped=False，反而测不到「无 SQL ⇒ skipped」这条。
        r_nosql = gt.review("", dataset="B", requirement=req,
                            _dry_run_fn=lambda s: {"ok": True, "message": "桩"})
        l_nosql = _lreq(r_nosql)
        print("  空 SQL                        L-REQ.skipped=%s" % (
            l_nosql["skipped"] if l_nosql else None))
        ok &= (l_nosql and l_nosql["skipped"] is True) or _fails(
            "SQL 为空时 L-REQ 应为 skipped（无可比对对象），实际 %s"
            % (l_nosql or {}).get("skipped"))

        # ② 传 SQL + 需求 ⇒ 真的参与审查 ⇒ skipped=False
        r_full = gt.review(SQL_NO_MONTH, dataset="B", requirement=req,
                           _dry_run_fn=lambda s: {"ok": True, "message": "桩"})
        l_full = _lreq(r_full)
        print("  传 SQL + 需求（无月度）    L-REQ.skipped=%s ok=%s" % (
            l_full["skipped"] if l_full else None, l_full["ok"] if l_full else None))
        ok &= (l_full and l_full["skipped"] is False) or _fails(
            "传了 SQL 与需求后 L-REQ 仍 skipped ⇒ 接线/实现没生效")

        # ③ 覆盖判定真的跑了：月度要素应出现在 detail 里
        detail = (l_full or {}).get("detail") or ""
        has_month = "月度" in detail
        print("  detail 里提到「月度」：%s" % has_month)
        ok &= has_month or _fails(
            "L-REQ detail 里没有「月度」⇒ 覆盖判定没真的跑（只是放行了）")

    # ④ 无 requirement ⇒ skipped（向后兼容红线）
    r_none = gt.review(SQL_NO_MONTH, dataset="B",
                       _dry_run_fn=lambda s: {"ok": True, "message": "桩"})
    l_none = _lreq(r_none)
    print("  不传 requirement          L-REQ.skipped=%s" % (
        l_none["skipped"] if l_none else None))
    ok &= (l_none and l_none["skipped"] is True) or _fails(
        "不传 requirement 时 L-REQ 应为 skipped，实际 %s" % (l_none or {}).get("skipped"))
    return ok


def test_sqlgen_wiring():
    """主证 1：sql_generate 走生产路径时，L-REQ 必须真的参与审查。"""
    print()
    print("=" * 72)
    print("2. 主证：sqlgen.generate（生产路径）⇒ L-REQ.skipped=False")
    print("=" * 72)
    ok = True
    seen = {}
    orig_review = sqlgen_mod.gates_mod.review

    def recording_review(sql, dataset="B", requirement=None, exec_result=None, **kw):
        seen["sql"] = sql
        seen["requirement"] = requirement
        r = orig_review(sql, dataset=dataset, requirement=requirement,
                        exec_result=exec_result, **kw)
        seen["lreq_skipped"] = (_lreq(r) or {}).get("skipped")
        return r

    with _offline_env():
        sqlgen_mod.gates_mod.review = recording_review
        try:
            out = sqlgen_mod.generate(candidate_sql=SQL_NO_MONTH,
                                      demand_id=DEMAND_WITH_MONTH["demand_id"],
                                      dataset="B")
        except Exception as exc:  # noqa: BLE001
            return _fails("sqlgen.generate 抛异常：%s: %s" % (type(exc).__name__, str(exc)[:200]))
        finally:
            sqlgen_mod.gates_mod.review = orig_review

    lreq = _lreq(out.get("review"))
    print("  L-REQ.skipped=%s" % (lreq["skipped"] if lreq else None))
    print("  gates.review 收到的 sql=%r" % (str(seen.get("sql"))[:46]))
    print("  gates.review 收到的 requirement=%s" % (
        "None" if seen.get("requirement") is None
        else "demand_id=%s" % (seen.get("requirement") or {}).get("demand_id")))

    ok &= (lreq is not None) or _fails("review 里没有 L-REQ 层")
    ok &= (lreq and lreq["skipped"] is False) or _fails(
        "接线后 L-REQ.skipped 仍为 True ⇒ SQL/需求没传到 gates.review")
    ok &= (seen.get("sql") is not None) or _fails("gates.review 没收到 SQL")
    ok &= (seen.get("requirement") is not None) or _fails("gates.review 没收到 requirement")
    return ok


def test_sqlrun_wiring():
    """主证 2：sqlrun.execute_readonly 的**预审查 + 后审查**都接上了。"""
    print()
    print("=" * 72)
    print("3. 主证：sqlrun.execute_readonly（预审查 + 后审查）⇒ L-REQ.skipped=False")
    print("=" * 72)
    ok = True
    orig_review = sqlrun_mod.gates_mod.review
    orig_wren = sqlrun_mod._wren_for
    calls = []

    class _FakeWren:
        def query(self, sql):
            return {"columns": ["cnt"], "data": [[3]]}

    def recording_review(sql, dataset="B", requirement=None, exec_result=None, **kw):
        r = orig_review(sql, dataset=dataset, requirement=requirement,
                        exec_result=exec_result, **kw)
        calls.append({
            "requirement": requirement,
            "exec_result": exec_result,
            "sql": sql,
            "lreq_skipped": (_lreq(r) or {}).get("skipped"),
        })
        return r

    with _offline_env():
        sqlrun_mod.gates_mod.review = recording_review
        sqlrun_mod._wren_for = lambda ds: _FakeWren()
        try:
            sqlrun_mod.execute_readonly(
                sql=SQL_NO_MONTH, dataset="B",
                demand_id=DEMAND_WITH_MONTH["demand_id"])
        except Exception as exc:  # noqa: BLE001
            return _fails("sqlrun.execute_readonly 抛异常：%s: %s" % (type(exc).__name__, str(exc)[:200]))
        finally:
            sqlrun_mod.gates_mod.review = orig_review
            sqlrun_mod._wren_for = orig_wren

    print("  gates.review 被调用 %d 次" % len(calls))
    for i, c in enumerate(calls, 1):
        tag = "预审查" if c["exec_result"] is None else "后审查"
        print("    第%d次(%s)：requirement=%s  sql=%s  L-REQ.skipped=%s" % (
            i, tag,
            "None" if c["requirement"] is None else "已传入",
            "有" if c["sql"] else "**缺失**",
            c["lreq_skipped"]))

    # ① 调用次数：预审查 + 后审查 = 2
    ok &= (len(calls) == 2) or _fails(
        "期望 gates.review 被调 2 次（预审查 + 后审查），实际 %d 次" % len(calls))
    # ② 每次调用的 requirement 与 SQL 都非 None
    for i, c in enumerate(calls, 1):
        ok &= (c["requirement"] is not None) or _fails(
            "第 %d 次 gates.review 调用没拿到 requirement（接线未生效）" % i)
        ok &= (c["sql"] is not None) or _fails(
            "第 %d 次 gates.review 调用没拿到 SQL（覆盖判定无从做起）" % i)
    # ③ 每次调用返回的 L-REQ 层 skipped == False
    for i, c in enumerate(calls, 1):
        ok &= (c["lreq_skipped"] is False) or _fails(
            "第 %d 次 review 的 L-REQ.skipped 应为 False（接线未生效），实际 %s"
            % (i, c["lreq_skipped"]))
    return ok


def test_call_sites_source():
    """源码级自证：三处 gates.review 调用都带 requirement= 与 sql。"""
    print()
    print("=" * 72)
    print("4. 源码级自证：三处 gates.review 调用都带 requirement=")
    print("=" * 72)
    ok = True
    import inspect
    import re
    for mod, label in ((sqlgen_mod, "sqlgen"), (sqlrun_mod, "sqlrun")):
        src = inspect.getsource(mod)
        calls = re.findall(r"gates_mod\.review\((.*?)\)\s*\n", src, re.S)
        for i, c in enumerate(calls, 1):
            has = "requirement=" in c
            ok &= has or _fails("%s 第 %d 处 review 调用没带 requirement=" % (label, i))
            print("  %s 第 %d 处 review(...) 带 requirement=：%s" % (label, i, has))
    return ok


def main():
    results = [
        ("1. review 直接对照", test_review_direct()),
        ("2. sqlgen 接线", test_sqlgen_wiring()),
        ("3. sqlrun 接线", test_sqlrun_wiring()),
        ("4. 三处调用点源码自证", test_call_sites_source()),
    ]
    print()
    print("=" * 72)
    all_ok = True
    for name, ok in results:
        all_ok &= ok
        print("  %-24s %s" % (name, "PASS" if ok else "FAIL"))
    print("=" * 72)
    if all_ok:
        print("接线自证全部通过")
        return 0
    print("接线自证有失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
