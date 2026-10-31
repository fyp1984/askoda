#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Schema 候选「命中 / 未命中」判据自证（离线，不需要网关、不需要数据库）。

为什么值得单独立一份
--------------------
2026-10-31 实测 bug：需求 DR-20261030-GJRN（「统计会员信息」）在「数据源接入」页
弹出黄色告警「未命中候选 · 上游 output_fields 为空，未参与匹配」，可它下面紧跟着的
候选原文里明明匹配到了 dim_member（score 6.0）、主键、时间字段、4 个过滤字段。

根因不是匹配算错，而是**一个字段承了两种语义**：``miss_reason`` 既装「真的没匹配到」
又装「上游槽位为空 / 覆盖率偏低」这类解释性说明；前端只要它非空就当"未命中"告警。
后果比报错更坏——业务方会以为系统找不到表，转而去建模、补知识库，白跑一轮。

本文件把判据钉成断言：

1. 命中主体表 ⇒ ``matched`` 为真且 ``miss_reason`` 必须为空；
2. 未命中 ⇒ ``matched`` 为假且 ``miss_reason`` 非空、且写明「未匹配到任何主体表」；
3. 输入不全（output_fields 为空）只是 ``notes``，不得污染 ``miss_reason``；
4. 不变式：``matched`` 与 ``subject_tables`` 非空、``miss_reason`` 为空三者等价；
5. 契约：既有 7 键名字与顺序不变，新键只追加在末尾；
6. 非 dict 输入不抛异常（硬约束：不许把上游脏数据变成 500）。

用法：
    python3 tools/candidates_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

# 宿主直跑兜底：registry 默认值是容器内路径 /workspace-b/mdl.json。
# 门禁（gate_all）会注入 MDL_B_PATH；单独跑这个脚本时也要能起来。
_MDL_B = os.path.join(_ROOT, "wren-docker-b", "workspace", "mdl.json")
if os.path.exists(_MDL_B):
    os.environ.setdefault("MDL_B_PATH", _MDL_B)

import sqlpack  # noqa: E402

RESULTS = []


def case(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((True, name, ""))
        except AssertionError as e:
            RESULTS.append((False, name, str(e)))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))
        return fn

    return deco


def _cand(req):
    return sqlpack.candidates("B", req)


def _ok(label, cond, detail=""):
    assert cond, "%s（实际：%s）" % (label, detail)


HIT = {"subject": {"value": "会员"}, "output_fields": [{"value": "会员等级"}]}
HIT_NO_FIELDS = {"subject": {"value": "会员"}, "output_fields": []}
MISS = {"subject": {"value": "完全不存在的实体XYZQQ"}, "output_fields": []}
# 真实占位串：上游分析没得出结论时写的就是它（不是空字符串，见 sqlpack.MISSING_MARK）
PENDING = sqlpack.MISSING_MARK
EMPTY = {"subject": {"value": PENDING}, "output_fields": [{"value": PENDING}]}


@case("① 命中主体表 ⇒ matched=True 且 miss_reason 为空")
def _t1():
    c = _cand(HIT)
    _ok("应命中主题表", bool(c["subject_tables"]), c["subject_tables"])
    _ok("matched 应为 True", c["matched"] is True, c["matched"])
    _ok("命中时 miss_reason 必须为空", c["miss_reason"] == "", repr(c["miss_reason"]))


@case("② 上游 output_fields 为空但主体命中：只进 notes，不进 miss_reason")
def _t2():
    c = _cand(HIT_NO_FIELDS)
    _ok("应命中主题表", bool(c["subject_tables"]), c["subject_tables"])
    _ok("matched 应为 True", c["matched"] is True, c["matched"])
    _ok("命中时 miss_reason 必须为空（原 bug 的正面断言）",
        c["miss_reason"] == "", repr(c["miss_reason"]))
    _ok("output_fields 缺失要进 notes",
        any("output_fields" in n for n in c["notes"]), c["notes"])


@case("③ 真未命中 ⇒ matched=False，miss_reason 说明未匹配到主体表")
def _t3():
    c = _cand(MISS)
    _ok("subj_tables 应为空", not c["subject_tables"], c["subject_tables"])
    _ok("matched 应为 False", c["matched"] is False, c["matched"])
    _ok("miss_reason 应写明未匹配到主体表",
        "未匹配到任何主体表" in c["miss_reason"], repr(c["miss_reason"]))


@case("④ 主体与字段全空 ⇒ 未命中，且 miss_reason 保留输入缺失原因")
def _t4():
    c = _cand(EMPTY)
    _ok("matched 应为 False", c["matched"] is False, c["matched"])
    _ok("miss_reason 非空", bool(c["miss_reason"].strip()), repr(c["miss_reason"]))
    _ok("miss_reason 要说明无有效候选词输入",
        "无任何有效候选词输入" in c["miss_reason"], repr(c["miss_reason"]))


@case("⑤ 不变式：matched ⇔ 有主题表 ⇔ miss_reason 为空")
def _t5():
    for req in (HIT, HIT_NO_FIELDS, MISS, EMPTY):
        c = _cand(req)
        has = bool(c["subject_tables"])
        _ok("三者不一致：%s" % req.get("subject"), (
            (c["matched"] is has)
            and (c["miss_reason"] == "") is has
        ), "matched=%s tables=%s miss=%r" % (
            c["matched"], len(c["subject_tables"]), c["miss_reason"]))


@case("⑥ 契约：既有 7 键名字与顺序不变，新键只追加在末尾")
def _t6():
    keys = list(_cand(HIT).keys())
    legacy = ["subject_tables", "join_paths", "key_candidates",
              "time_field_candidates", "filter_field_candidates",
              "miss_reason", "_coverage_pct"]
    _ok("既有键或顺序被改动", keys[:len(legacy)] == legacy, keys)
    _ok("新键应追加在末尾", set(keys[len(legacy):]) == {"matched", "notes"}, keys)


@case("⑦ 非 dict 输入不抛异常，且判为未命中")
def _t7():
    c = sqlpack.candidates("B", "会员等级")  # 上游误传字符串
    _ok("应判为未命中", c["matched"] is False, c["matched"])
    _ok("miss_reason 非空", bool(c["miss_reason"].strip()), repr(c["miss_reason"]))


def main():
    for ok, name, detail in RESULTS:
        print(("  OK  " if ok else "  FAIL") + "  " + name + (("  << " + detail) if detail else ""))
    bad = [r for r in RESULTS if not r[0]]
    print("\ncandidates_selftest: %d/%d" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
