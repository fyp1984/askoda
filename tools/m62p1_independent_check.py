# -*- coding: utf-8 -*-
"""M6-2 补丁 P1 · 验收方独立复扫（对抗用例）

目的：证明 tools/fallback_verify.py 的「F1/F2 真链路」断言**真的随链路输出变化**，
      而不是"无论链路给什么都判过"（自证脚本最典型的自欺）。
手法：打桩（monkeypatch sys.modules），把链路函数的返回值换掉，看断言是否按预期翻红/翻绿。

覆盖：
  对抗A  F1：打桩 analysis.first_round → questions=[]      → F1 真链路断言必须 FAIL
  对抗B  F2：打桩 sqlgen.plan      → chosen_table 非空      → F2 正例（#2/#3）必须 FAIL
  对抗C  F2：打桩 sqlgen.plan      → chosen_table 空        → F2 负例（#1）必须 FAIL
  对抗D  --live-required 机制：默认模式 skip 不记失败；强模式 skip 记 FAIL
  对抗E  DB 不可达：默认模式 SKIP 不记失败；强模式转 FAIL

跑法：仓库根目录 → python3 tools/m62p1_independent_check.py
exit 0 全部符合预期；exit 1 有不符合。
"""
import os
import sys
import types
import importlib

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
GW = os.path.join(ROOT, "gateway")
for _p in (ROOT, GW):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))

import fallback_verify as fv  # noqa: E402

RESULT = []


def ck(name, cond, detail=""):
    RESULT.append(bool(cond))
    print("  [%s] %s %s" % ("OK" if cond else "FAIL", name,
                           ("  -> " + str(detail)[:200]) if detail else ""))


def _fake(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


_KEYS = ("demand", "analysis", "requirement", "sqlgen", "schema_scan")


def _snap():
    return {k: sys.modules.get(k) for k in _KEYS}


def _restore(s):
    for k, v in s.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _stub_chain(plan_ret):
    """把整条 F2 前置链打桩成固定返回。"""
    sys.modules["demand"] = _fake("demand", create=lambda payload, actor="x": {"demand_id": "FAKE-F2"})
    sys.modules["schema_scan"] = _fake("schema_scan",
                                       latest_version=lambda ds: {"schema_version": "FAKE"},
                                       scan=lambda ds, persist=True: {"schema_version": "FAKE"})
    sys.modules["analysis"] = _fake("analysis", first_round=lambda *a, **k: {})
    sys.modules["requirement"] = _fake("requirement", build=lambda *a, **k: {})
    sys.modules["sqlgen"] = _fake("sqlgen", plan=lambda *a, **k: plan_ret)


# ---------------------------------------------------------------------------
# 对抗 A：F1 真链路 —— 链路返回 0 条问题，断言必须翻红
# ---------------------------------------------------------------------------
def case_A():
    print("\n[对抗A] 打桩 analysis.first_round → questions=[] ；F1 真链路断言必须 FAIL")
    s = _snap()
    sys.modules["demand"] = _fake("demand", create=lambda payload, actor="x": {"demand_id": "FAKE-A"})
    sys.modules["analysis"] = _fake("analysis", first_round=lambda *a, **k: {"questions": []})
    fv.FAIL.clear()
    fv.check_F1_live(True, "")
    ck("对抗A：链路给出 0 条问题 → F1 真链路断言判 FAIL（证明 f1_hit 非恒真）",
       len(fv.FAIL) == 1, "FAIL 条目=%r" % (fv.FAIL,))
    fv.FAIL.clear()
    _restore(s)


# ---------------------------------------------------------------------------
# 对抗 B / C：F2 真链路 —— 判据极性翻转，断言必须跟着翻
# ---------------------------------------------------------------------------
def case_B():
    print("\n[对抗B] 打桩 sqlgen.plan → chosen_table 非空 ；F2 的 #2/#3（正例）必须 FAIL")
    s = _snap()
    _stub_chain({"chosen_table": "some_table",
                 "table_candidates": [{"table": "some_table", "score": 9.9}],
                 "draft_gate": {}})
    fv.FAIL.clear()
    fv.check_F2_live(True, "")
    ck("对抗B：chosen_table 非空（不该触发 F2）→ 2 条正例断言 FAIL、1 条负例断言 OK",
       len(fv.FAIL) == 2, "FAIL 条目=%r" % (fv.FAIL,))
    fv.FAIL.clear()
    _restore(s)


def case_C():
    print("\n[对抗C] 打桩 sqlgen.plan → chosen_table 空 ；F2 的 #1（负例）必须 FAIL")
    s = _snap()
    _stub_chain({"chosen_table": "",
                 "table_candidates": [{"table": "a", "score": 1.0}],
                 "draft_gate": {}})
    fv.FAIL.clear()
    fv.check_F2_live(True, "")
    ck("对抗C：chosen_table 为空（该触发 F2）→ 1 条负例断言 FAIL、2 条正例断言 OK",
       len(fv.FAIL) == 1, "FAIL 条目=%r" % (fv.FAIL,))
    fv.FAIL.clear()
    _restore(s)


# ---------------------------------------------------------------------------
# 对抗 D：--live-required 机制
# ---------------------------------------------------------------------------
def case_D():
    print("\n[对抗D] skip() 行为：默认模式记 SKIP；--live-required 下记 FAIL")
    sys.argv = [a for a in sys.argv if a != "--live-required"]
    m1 = importlib.reload(fv)
    b1 = len(m1.FAIL)
    m1.skip("D-默认模式样例", "模拟不可达")
    ck("对抗D-1：默认模式 skip() 不记失败", len(m1.FAIL) == b1,
       "FAIL 增量=%d" % (len(m1.FAIL) - b1))

    sys.argv = sys.argv + ["--live-required"]
    m2 = importlib.reload(m1)
    b2 = len(m2.FAIL)
    m2.skip("D-强模式样例", "模拟不可达")
    ck("对抗D-2：--live-required 下 skip() 记 FAIL", len(m2.FAIL) == b2 + 1,
       "FAIL 增量=%d" % (len(m2.FAIL) - b2))
    sys.argv = [a for a in sys.argv if a != "--live-required"]
    importlib.reload(m2)


# ---------------------------------------------------------------------------
# 对抗 E：DB 不可达路径
# ---------------------------------------------------------------------------
def case_E():
    print("\n[对抗E] DB 不可达：默认模式真链路 SKIP；强模式转 FAIL")
    sys.argv = [a for a in sys.argv if a != "--live-required"]
    m1 = importlib.reload(fv)
    b1 = len(m1.FAIL)
    m1.check_F1_live(False, "模拟不可达")
    m1.check_F2_live(False, "模拟不可达")
    ck("对抗E-1：默认模式 DB 不可达 → F1/F2 真链路 SKIP、不记失败", len(m1.FAIL) == b1,
       "FAIL 增量=%d" % (len(m1.FAIL) - b1))

    sys.argv = sys.argv + ["--live-required"]
    m2 = importlib.reload(m1)
    b2 = len(m2.FAIL)
    m2.check_F1_live(False, "模拟不可达")
    m2.check_F2_live(False, "模拟不可达")
    ck("对抗E-2：强模式 DB 不可达 → F1 与 F2 各记 1 条 FAIL（共 2）", len(m2.FAIL) == b2 + 2,
       "FAIL 增量=%d" % (len(m2.FAIL) - b2))
    sys.argv = [a for a in sys.argv if a != "--live-required"]
    importlib.reload(m2)


def main():
    print("=" * 68)
    print("M6-2 补丁 P1 · 验收方独立复扫（对抗用例）")
    print("=" * 68)
    case_A()
    case_B()
    case_C()
    case_D()
    case_E()
    n_ok = sum(RESULT)
    print("\n==================== 总览 ====================")
    print("共 %d 项断言，通过 %d，失败 %d" % (len(RESULT), n_ok, len(RESULT) - n_ok))
    if n_ok != len(RESULT):
        print("不符合预期！")
        sys.exit(1)
    print("全部符合预期")
    sys.exit(0)


if __name__ == "__main__":
    main()
