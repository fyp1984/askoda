#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口径规划器自证（离线：不连引擎、不连库）

守的是什么
----------
2026-11-01 给信贷库（C）补口径时，第一版编译器能出 SQL，但实测抓到两类**静默错**：

1. **两期写成同一期**：同比 SQL 的「基期条件」是在当期条件上打补丁生成的，
   当期里没有 `INTERVAL` 可替换，于是基期 = 当期，增长率恒为 0——不报错、看着像真数。
2. **时点量跨日相加**：余额是快照量，漏了 `stat_date = (...)` 就把两个快照日加在一起，
   金额翻倍且不报错（知识库早就写着这条坑，代码却没强制）。

所以这里把判据钉成断言，覆盖：
  ① 未登记口径 → 拒绝且话术指向登记入口
  ② 别名最长优先（「不良率」不能被「不良」抢走）
  ③ 每个时点量口径都必须把时间限定到单一快照日（结构性断言，不枚举具体口径）
  ④ 未登记指标 → 拒绝并列出已登记口径（告诉业务方能问什么）
  ⑤ 快照指标做同比 → 拒绝（本库没有去年同期快照，硬算会给出假的 0%）
  ⑥ 同比/环比：当期与基期条件必须不同（防「两期同期」回归）
  ⑦ 趋势类按时间正序
  ⑧ 排名类带 LIMIT
  ⑨ 生成的 SQL 一律通过只读门禁
  ⑩ 口径登记表与 MDL 视图列一致（口径不能引用不存在的列）

用法：
    python3 tools/metric_planner_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "gateway"))

import metric_planner  # noqa: E402
import planner  # noqa: E402

MDL_PATH = os.path.join(_ROOT, "wren-docker-c", "workspace", "mdl.json")
METRICS_PATH = os.path.join(_ROOT, "db", "credit", "04_metrics.json")

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


def _ok(what, cond, got=""):
    if not cond:
        raise AssertionError("%s << 实际=%r" % (what, got))


class _DS:
    def __init__(self, key, mdl):
        self.key = key
        self.mdl = mdl


def _ds():
    with open(MDL_PATH, encoding="utf-8") as f:
        return _DS("C", json.load(f))


def _metrics():
    with open(METRICS_PATH, encoding="utf-8") as f:
        return json.load(f)


@case("① 未登记口径的数据集 → 拒绝，并指向登记入口")
def _t1():
    ds = _DS("X", {"models": []})
    r = metric_planner.plan(ds, "统计本月放款金额")
    _ok("应拒绝", r["blocked"] is True, r)
    _ok("原因应说明缺口径登记", "尚未登记口径" in r["reason"], r["reason"])
    _ok("指引应给出登记入口", "04_metrics.json" in r["guide"], r["guide"])


@case("② 别名最长优先：'不良率' 命中 npl_ratio，不被 '不良' 抢走")
def _t2():
    ds = _ds()
    r1 = metric_planner.plan(ds, "全行不良率是多少")
    _ok("不良率应命中 npl_ratio", "npl_ratio" in r1["sql"], r1["sql"])
    r2 = metric_planner.plan(ds, "全行不良贷款余额是多少")
    _ok("不良余额应命中 npl_balance", "npl_balance" in r2["sql"], r2["sql"])


@case("③ 结构性：每个时点量口径都必须限定单一快照日")
def _t3():
    ds = _ds()
    reg = ds.mdl["x_askoda_metrics"]
    snaps = [m for m in reg["metrics"] if m.get("time_mode") == "snapshot"]
    _ok("应至少登记 1 个时点量口径", len(snaps) >= 1, len(snaps))
    for m in snaps:
        al = m["aliases"][0]
        r = metric_planner.plan(ds, "统计%s" % al)
        _ok("「%s」不应被拒绝" % al, not r["blocked"], r.get("reason"))
        _ok("「%s」SQL 必须含单日快照限定" % al,
            "= (SELECT MAX(%s)" % m["time_col"] in r["sql"], r["sql"])


@case("④ 未登记指标 → 拒绝，并列出已登记口径")
def _t4():
    ds = _ds()
    r = metric_planner.plan(ds, "客户的手机号是多少")
    _ok("应拒绝", r["blocked"] is True, r)
    _ok("应列出已登记口径", "已登记" in r["reason"], r["reason"])
    _ok("应点到已登记项", "贷款余额" in r["reason"], r["reason"])


@case("⑤ 快照指标做同比/环比 → 拒绝（本库无去年同期快照）")
def _t5():
    ds = _ds()
    r = metric_planner.plan(ds, "贷款余额同比")
    _ok("应拒绝", r["blocked"] is True, r)
    _ok("原因应说明缺可比时点", "快照" in r["reason"], r["reason"])


@case("⑥ 同比/环比：当期与基期条件必须不同（防两期同期）")
def _t6():
    ds = _ds()
    for word, marker in (("同比", "INTERVAL '1 year'"), ("环比", "INTERVAL '1 month'")):
        r = metric_planner.plan(ds, "本月放款金额%s" % word)
        _ok("%s 不应被拒绝" % word, not r["blocked"], r.get("reason"))
        sql = r["sql"]
        _ok("%s 基期应带位移 %s" % (word, marker), marker in sql, sql)
        # 两段期次条件都要在（少一段就是「只算了一期」）
        _ok("%s 应有两段期次条件" % word, sql.count("date_trunc('month'") >= 2, sql)
        # 当期段不能也带位移：取基期位移之前最近的一段 date_trunc 条件来判
        head = sql.split(marker)[0]
        tail = head[head.rfind("date_trunc('month'"):]
        _ok("%s 当期条件不应带位移" % word, "INTERVAL" not in tail, tail[:140])


@case("⑦ 趋势类按时间正序（走势不能倒着看）")
def _t7():
    ds = _ds()
    r = metric_planner.plan(ds, "近6个月放款金额趋势")
    _ok("不应被拒绝", not r["blocked"], r.get("reason"))
    _ok("应按时间正序", "ORDER BY stat_month ASC" in r["sql"], r["sql"])
    _ok("应限定区间", "INTERVAL" in r["sql"], r["sql"])


@case("⑧ 排名类带 LIMIT 且按数值倒序")
def _t8():
    ds = _ds()
    r = metric_planner.plan(ds, "放款金额前3名的产品")
    _ok("不应被拒绝", not r["blocked"], r.get("reason"))
    _ok("应有 LIMIT 3", "LIMIT 3" in r["sql"], r["sql"])
    _ok("应按数值倒序", "ORDER BY putout_amt DESC" in r["sql"], r["sql"])


@case("⑨ 生成的 SQL 全部通过只读门禁")
def _t9():
    ds = _ds()
    for q in ("统计本月各贷款产品的放款金额", "按五级分类看不良贷款余额",
              "本月放款金额同比", "近6个月放款金额趋势", "放款金额前3名的产品"):
        r = metric_planner.plan(ds, q)
        if r["blocked"]:
            continue
        ok, msg = planner.is_readonly(r["sql"])
        _ok("「%s」应只读" % q, ok, msg)


@case("⑩ 口径登记表与 MDL 视图列一致（不许引用不存在的列）")
def _t10():
    ds = _ds()
    reg = ds.mdl["x_askoda_metrics"]
    views = reg["views"]
    for m in reg["metrics"]:
        v = views.get(m["view"])
        _ok("口径 %s 的视图 %s 应已登记" % (m["key"], m["view"]), v is not None, m["view"])
        cols = set(v["columns"])
        for d in m.get("dims") or []:
            _ok("口径 %s 的维度 %s 应在视图列里" % (m["key"], d), d in cols, sorted(cols))
        _ok("口径 %s 的 time_col 应在视图列里" % m["key"], m["time_col"] in cols, sorted(cols))


@case("⑪ 登记文件与 MDL 内嵌口径同源（版本号一致）")
def _t11():
    ds = _ds()
    reg = ds.mdl["x_askoda_metrics"]
    file_metrics = _metrics()
    _ok("版本号应一致", reg.get("version") == file_metrics.get("version"),
        (reg.get("version"), file_metrics.get("version")))
    _ok("口径条数应一致",
        len(reg.get("metrics") or []) == len(file_metrics.get("metrics") or []),
        (len(reg.get("metrics") or []), len(file_metrics.get("metrics") or [])))


def main():
    for ok, name, detail in RESULTS:
        print(("  OK  " if ok else "  FAIL") + "  " + name + (("  << " + detail) if detail else ""))
    bad = [r for r in RESULTS if not r[0]]
    print("\nmetric_planner_selftest: %d/%d" % (len(RESULTS) - len(bad), len(RESULTS)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
