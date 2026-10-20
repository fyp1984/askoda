#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REQ-COVERAGE 阶段二 · 接线自证（证明 L-REQ 在**生产链路**上真的生效了）

为什么需要这个脚本（不能只看门禁绿）
------------------------------------
门禁绿只证明「没有把已有行为搞坏」，**不证明**新能力真的接上了。
本脚本自证一件具体的事：

    生产链路（sql_generate / sql_execute_readonly）上，L-REQ 那一层的
    ``skipped`` 值必须从 **True**（接线前：没传 requirement ⇒ 恒跳过）
    变成 **False**（接线后：喂了 requirement ⇒ 该层真的参与审查）。

⚠️ 判据必须**先按字面执行一遍**（今天立的规范第 4 条）：
    凡写入断言的行为描述，必须能追到一个具体的运行命令或行号。

本脚本怎么做到「不依赖数据库也能跑」
------------------------------------
⚠️ 真实链路需要 DB（本机默认未配置 ASSISTANT_DB_DSN）。
    本脚本用 **monkeypatch** 把 ``requirement.get`` 换成一个返回
    **已知结构化需求**的假实现，从而在**离线**环境里也能断言
    「L-REQ 的 skipped 变 False」。
    —— 这是对**接线逻辑**的自证（参数有没有传到位），
    不是对「DB 里真有这条需求」的自证；后者由 DB 可达时的那段一并验证。

用法：
    python3 tools/reqcov_wiring_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))

import gates as gates_mod          # noqa: E402
import sqlgen as sqlgen_mod        # noqa: E402
import sqlrun as sqlrun_mod        # noqa: E402
import requirement as requirement_mod  # noqa: E402


# 一个**槽位齐备**的结构化需求（阶段一的 L-REQ 会判它「齐备性通过」）
GOOD_REQUIREMENT = {
    "subject": {"value": "各门店月度销售额", "evidence": [{"level": "P8"}], "confidence": 0.9},
    "granularity": {"value": "月度", "evidence": [{"level": "P8"}], "confidence": 0.9},
    "time_semantics": {"value": "按下单时间", "evidence": [{"level": "P8"}], "confidence": 0.9},
    "data_scope": {"value": "B 库 dwd_order_di", "evidence": [{"level": "P8"}], "confidence": 0.9},
    "output_fields": [
        {"value": "销售额", "evidence": [{"level": "P8"}], "confidence": 0.9},
    ],
}

SQL_OK = "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name"


def _lreq(review):
    """从 review 结果里取出 L-REQ 层。"""
    for layer in (review or {}).get("layers") or []:
        if layer.get("layer") == "L-REQ":
            return layer
    return None


def _fails(msg):
    print("  ✗ %s" % msg)
    return False


def test_review_direct():
    """对照组：gates.review 本身的语义（接线前 vs 接线后）。"""
    print("=" * 72)
    print("1. 对照组：gates.review 传 / 不传 requirement ⇒ L-REQ.skipped")
    print("=" * 72)
    ok = True
    r_none = gates_mod.review(SQL_OK, dataset="B", _dry_run_fn=lambda s: {"ok": True, "message": "桩"})
    l_none = _lreq(r_none)
    ok &= (l_none is not None and l_none["skipped"] is True) or _fails(
        "不传 requirement 时 L-REQ.skipped 应为 True，实际 %s" % (l_none or {}).get("skipped"))
    print("  不传 requirement：L-REQ.skipped=%s ok=%s" % (
        l_none["skipped"] if l_none else None, l_none["ok"] if l_none else None))

    r_req = gates_mod.review(SQL_OK, dataset="B", requirement=GOOD_REQUIREMENT,
                             _dry_run_fn=lambda s: {"ok": True, "message": "桩"})
    l_req = _lreq(r_req)
    ok &= (l_req is not None and l_req["skipped"] is False) or _fails(
        "传 requirement 时 L-REQ.skipped 应为 False，实际 %s" % (l_req or {}).get("skipped"))
    print("  传 requirement  ：L-REQ.skipped=%s ok=%s" % (
        l_req["skipped"] if l_req else None, l_req["ok"] if l_req else None))
    return ok


def test_sqlgen_wiring():
    """主证 1：sql_generate 走生产路径时，L-REQ 必须不再 skipped。"""
    print()
    print("=" * 72)
    print("2. 主证：sqlgen.generate（生产路径）⇒ L-REQ.skipped 必须为 False")
    print("=" * 72)
    ok = True
    orig_get = requirement_mod.get
    seen = {}

    def fake_get(demand_id, version=None):
        seen["demand_id"] = demand_id
        return dict(GOOD_REQUIREMENT)

    requirement_mod.get = fake_get
    try:
        out = sqlgen_mod.generate(candidate_sql=SQL_OK, demand_id="DR-TEST-0001", dataset="B")
    except Exception as exc:  # noqa: BLE001
        requirement_mod.get = orig_get
        return _fails("sqlgen.generate 抛异常：%s: %s" % (type(exc).__name__, str(exc)[:200]))
    finally:
        requirement_mod.get = orig_get

    review = out.get("review") or {}
    lreq = _lreq(review)
    print("  L-REQ.skipped=%s ok=%s" % (
        lreq["skipped"] if lreq else None, lreq["ok"] if lreq else None))
    print("  requirement.get 收到的 demand_id=%s" % seen.get("demand_id"))
    ok &= (lreq is not None) or _fails("review 里没有 L-REQ 层")
    ok &= (lreq and lreq["skipped"] is False) or _fails(
        "接线后 L-REQ.skipped 仍为 True ⇒ requirement 没传到 gates.review")
    ok &= (seen.get("demand_id") == "DR-TEST-0001") or _fails(
        "requirement.get 没被调用 / demand_id 不对，实际 %s" % seen.get("demand_id"))
    return ok


def test_sqlrun_wiring():
    """主证 2：sqlrun.execute_readonly 的**预审查 + 后审查**都接上了。

    ⚠️ 这条用例曾经**只会打印、不会断言**（`ok` 从未被修改 ⇒ 永远 PASS）——
       那是「绿灯不能变红」的最隐蔽形态：接线被摘掉时它照样全绿。
       现改为**记录每次 gates.review 调用**并逐次断言，三重保险：
         ① 调用次数（预审查 + 后审查 = 2 次）
         ② 每次调用的 requirement 参数**非 None**
         ③ 每次调用返回的 L-REQ 层 skipped == False
    """
    print()
    print("=" * 72)
    print("3. 主证：sqlrun.execute_readonly（预审查 + 后审查）⇒ L-REQ.skipped=False")
    print("=" * 72)
    ok = True
    orig_get = requirement_mod.get
    orig_review = sqlrun_mod.gates_mod.review
    orig_wren = sqlrun_mod._wren_for

    def fake_get(demand_id, version=None):
        return dict(GOOD_REQUIREMENT)

    class _FakeWren:
        def query(self, sql):
            return {"columns": ["cnt"], "data": [[3]]}

    calls = []

    def recording_review(sql, dataset="B", requirement=None, exec_result=None, **kw):
        # 只做记录，**原样转发**（不改被测行为）
        r = orig_review(sql, dataset=dataset, requirement=requirement,
                        exec_result=exec_result, **kw)
        calls.append({"requirement": requirement, "exec_result": exec_result,
                      "lreq_skipped": (_lreq(r) or {}).get("skipped")})
        return r

    requirement_mod.get = fake_get
    sqlrun_mod.gates_mod.review = recording_review
    sqlrun_mod._wren_for = lambda ds: _FakeWren()
    try:
        sqlrun_mod.execute_readonly(
            sql=SQL_OK, dataset="B", demand_id="DR-TEST-0002")
    except Exception as exc:  # noqa: BLE001
        return _fails("sqlrun.execute_readonly 抛异常：%s: %s" % (type(exc).__name__, str(exc)[:200]))
    finally:
        requirement_mod.get = orig_get
        sqlrun_mod.gates_mod.review = orig_review
        sqlrun_mod._wren_for = orig_wren

    print("  gates.review 被调用 %d 次" % len(calls))
    for i, c in enumerate(calls, 1):
        tag = "预审查" if c["exec_result"] is None else "后审查"
        print("    第%d次(%s)：requirement=%s  返回的 L-REQ.skipped=%s" % (
            i, tag,
            "None" if c["requirement"] is None else "已传入",
            c["lreq_skipped"]))

    # ① 调用次数：预审查 + 后审查 = 2
    ok &= (len(calls) == 2) or _fails(
        "期望 gates.review 被调 2 次（预审查 + 后审查），实际 %d 次" % len(calls))

    # ② 每次调用的 requirement 都非 None（接线生效的直接证据）
    for i, c in enumerate(calls, 1):
        ok &= (c["requirement"] is not None) or _fails(
            "第 %d 次 gates.review 调用没拿到 requirement（接线未生效）" % i)

    # ③ 每次调用返回的 L-REQ 层 skipped == False
    for i, c in enumerate(calls, 1):
        ok &= (c["lreq_skipped"] is False) or _fails(
            "第 %d 次 review 的 L-REQ.skipped 应为 False（接线未生效），实际 %s"
            % (i, c["lreq_skipped"]))
    return ok


def test_sqlrun_call_sites():
    """源码级自证：sqlrun 的两处 review 调用都带了 requirement=。"""
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
        ("4. 三处调用点源码自证", test_sqlrun_call_sites()),
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
