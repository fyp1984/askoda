#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""REQ-COVERAGE 阶段二 · 底座三件（gateway/reqcov.py）离线自证

设计目的
--------
本脚本是 **G1 离线自证**（可被 tools/gate_all.py 的 G1 glob 自动发现：
``tools/*_selftest.py``）。它对 ``gateway/reqcov.py`` 做四组断言：

    1. SqFacts 对 **4 种粒度表达**各产出正确类别（month / day / year）
    2. judge() 对 **6 条分级口径**各产出正确 verdict
    3. **确定性**：同一 SQL 解析两次，SqFacts 输出逐字节相同
    4. ElementContract 的 8 字段校验：缺字段 / 类型错要被拒

为什么必须自证（不能只靠门禁绿）
--------------------------------
「新增的 reqcov.py 需要自证」——门禁绿只证明**没有把已有行为搞坏**，
不证明**新能力真的对**。REQ-COVERAGE 这个缺陷的形状恰恰是
「全绿但没生效」：需求要素被 SQL 丢掉时，五层门禁照样判通过、
deliverable=true、业务方拿到错数据、**全程零报错**。
所以新能力必须**自己证明自己**，否则只是换了个地方继续静默失效。

⚠️ 断言纪律（今天立的规范第 4 条）
--------------------------------
**凡写入断言的行为描述，必须能追到一个具体的运行命令或行号**；
**判据须先按字面执行一遍，确认可执行再交付**。
本脚本的每条断言都在下方注释里写明「期望」与「依据（行号/实测）」。

用法：
    python3 tools/reqcov_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))

import reqcov as R  # noqa: E402


# ==========================================================================
# 工具
# ==========================================================================

def _ok(cond, label, detail=""):
    print("[%s] %s%s" % ("OK" if cond else "!!", label,
                          ("  —— " + detail) if detail else ""))
    return bool(cond)


# ==========================================================================
# 1. SqFacts 对 4 种粒度表达各产出正确类别
# ==========================================================================
#
# 4 种表达（架构师已实测的 sqlglot 归一形态）：
#   ① date_trunc('month', order_date) → exp.TimestampTrunc(unit=Var(MONTH))
#   ② to_char(order_date,'YYYY-MM')   → exp.TimeToStr(format=Literal('%Y-%m'))
#   ③ stat_month（裸列）              → exp.Column(name='stat_month')
#   ④ substr(order_date,1,7)         → exp.Substring
#
# 每种 × 3 个类别（month/day/year）= 12 例。
# 期望：全部归到正确类别（依据 = 实测输出，见下方 selftest 运行结果）。

GRANULARITY_CASES = [
    # (标签, SQL, 期望类别)
    ("① date_trunc month",
     "SELECT date_trunc('month', order_date) AS m, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "month"),
    ("① date_trunc day",
     "SELECT date_trunc('day', order_date) AS d, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "day"),
    ("① date_trunc year",
     "SELECT date_trunc('year', order_date) AS y, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "year"),

    ("② to_char YYYY-MM",
     "SELECT to_char(order_date,'YYYY-MM') AS m, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "month"),
    ("② to_char YYYY-MM-DD",
     "SELECT to_char(order_date,'YYYY-MM-DD') AS d, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "day"),
    ("② to_char YYYY",
     "SELECT to_char(order_date,'YYYY') AS y, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "year"),

    ("③ 裸列 stat_month",
     "SELECT stat_month, COUNT(*) AS cnt FROM dws_store_daily_agg "
     "GROUP BY stat_month", "month"),
    ("③ 裸列 stat_day",
     "SELECT stat_day, COUNT(*) AS cnt FROM dws_store_daily_agg "
     "GROUP BY stat_day", "day"),
    ("③ 裸列 stat_year",
     "SELECT stat_year, COUNT(*) AS cnt FROM dws_store_daily_agg "
     "GROUP BY stat_year", "year"),

    ("④ substr 前7位",
     "SELECT substr(order_date,1,7) AS m, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "month"),
    ("④ substr 前10位",
     "SELECT substr(order_date,1,10) AS d, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "day"),
    ("④ substr 前4位",
     "SELECT substr(order_date,1,4) AS y, COUNT(*) AS cnt "
     "FROM dwd_order_di GROUP BY 1", "year"),
]


def test_granularity_extraction():
    print("=" * 72)
    print("1. SqFacts 对 4 种粒度表达各产出正确类别（month / day / year）")
    print("=" * 72)
    all_ok = True
    for label, sql, expect in GRANULARITY_CASES:
        facts = R.extract_facts(sql)
        classes = facts.granularity_classes()
        hit = (classes == [expect])
        all_ok &= _ok(hit, label, "期望=%s 实测=%s keys=%s"
                      % (expect, classes, list(facts.group_by_granularities.keys())))
    # 反向：不含任何时间粒度的 GROUP BY ⇒ 归类为空（不得瞎猜）
    facts = R.extract_facts(
        "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name")
    all_ok &= _ok(facts.granularity_classes() == [],
                  "反例：无时间粒度的 GROUP BY ⇒ 类别为空",
                  "实测=%s" % facts.granularity_classes())
    # 反向：substr 非前缀（substr(x,3,7)）不得套用「前7位」规则
    facts = R.extract_facts(
        "SELECT substr(order_date,3,7) AS m, COUNT(*) FROM t GROUP BY 1")
    all_ok &= _ok(facts.granularity_classes() == [],
                  "反例：substr(x,3,7) 非前缀 ⇒ 不套用前7位规则",
                  "实测=%s" % facts.granularity_classes())

    # ---- 归一化契约：GROUP BY 1 与 GROUP BY store_name 必须归一到同一个键 ----
    #
    # 为什么这条必须钉：归一化失效 ⇒ 同一条 SQL 换个写法（写序号还是写列名）
    # 就判「未覆盖」⇒ 假阳性率飙升 ⇒ 门禁被绕过。
    # 归一化由 rules._canonical_key 提供（**本模块不另起一套**），
    # 但**复用**的判据也得被本模块的用例钉住，否则上游改了它、
    # 这里跟着一起变坏，两边都照样全绿。
    norm_1 = R.extract_facts(
        "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY 1")
    norm_named = R.extract_facts(
        "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name")
    all_ok &= _ok(
        norm_1.group_by_keys == norm_named.group_by_keys == ("store_name",),
        "归一化：GROUP BY 1 与 GROUP BY store_name 归一到同一个键",
        "序号写法=%s 列名写法=%s" % (list(norm_1.group_by_keys),
                                    list(norm_named.group_by_keys)))

    # 别名解引用：GROUP BY 别名 与 GROUP BY 原表达式 同键
    norm_alias = R.extract_facts(
        "SELECT store_name AS 门店, COUNT(*) AS cnt FROM dim_store GROUP BY 门店")
    norm_expr = R.extract_facts(
        "SELECT store_name AS 门店, COUNT(*) AS cnt FROM dim_store GROUP BY store_name")
    all_ok &= _ok(
        norm_alias.group_by_keys == norm_expr.group_by_keys,
        "归一化：GROUP BY 别名 与 GROUP BY 原表达式 归一到同一个键",
        "别名写法=%s 原表达式写法=%s" % (list(norm_alias.group_by_keys),
                                       list(norm_expr.group_by_keys)))
    return all_ok


# ==========================================================================
# 2. judge() 对 6 条分级口径各产出正确 verdict
# ==========================================================================
#
# 冻结的分级阻断口径（需求方冻结，不可改）：
#   阻断 = (confidence ≥ 0.6) AND (needs_confirmation == False) AND (polarity == must_cover)
#   警告 = 其余全部
#   不判 = 原文未提出 / 业务方显式豁免
#
# 6 条用例覆盖三条条件的**每一个分界** + verdict 的三种取值：
#   ① 全部满足 + 已覆盖        ⇒ 已覆盖
#   ② 全部满足 + 未覆盖        ⇒ 未覆盖 / blocking
#   ③ confidence < 0.6         ⇒ 未覆盖 / warning
#   ④ needs_confirmation=True  ⇒ 未覆盖 / warning
#   ⑤ polarity=may_omit        ⇒ 不判
#   ⑥ matcher kind 未注册      ⇒ 不判（缺席显式可见，不当已覆盖）

SQL_MONTH = ("SELECT date_trunc('month', order_date) AS m, COUNT(*) AS cnt "
             "FROM dwd_order_di GROUP BY 1")
SQL_NO_TIME = ("SELECT store_name, COUNT(*) AS cnt FROM dim_store "
               "GROUP BY store_name")


def _element(element="月度", slot="granularity", provenance="需求原文第2句",
             confidence=0.85, needs_confirmation=False, mandatory=True,
             polarity="must_cover", matcher="time_granularity:month"):
    return {
        "element": element, "slot": slot, "provenance": provenance,
        "confidence": confidence, "needs_confirmation": needs_confirmation,
        "mandatory": mandatory, "polarity": polarity, "matcher": matcher,
    }


def test_judge_grading():
    print()
    print("=" * 72)
    print("2. judge() 对 6 条分级口径各产出正确 verdict")
    print("=" * 72)
    all_ok = True
    facts_month = R.extract_facts(SQL_MONTH)
    facts_none = R.extract_facts(SQL_NO_TIME)

    # ① 三条件全满足 + SQL 已覆盖 ⇒ 已覆盖
    res = R.judge([_element()], facts_month)
    e = res["elements"][0]
    all_ok &= _ok(
        res["verdict"] == R.VERDICT_COVERED and e["verdict"] == R.VERDICT_COVERED,
        "① 全部满足且已覆盖 ⇒ 已覆盖",
        "verdict=%s severity=%s" % (e["verdict"], e["severity"]))

    # ② 三条件全满足 + SQL 未覆盖 ⇒ 未覆盖 / blocking
    res = R.judge([_element()], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        res["verdict"] == R.VERDICT_UNCOVERED and e["verdict"] == R.VERDICT_UNCOVERED
        and e["severity"] == R.SEVERITY_BLOCKING
        and res["stats"]["blocking"] == 1 and len(res["gaps"]) == 1,
        "② 全部满足但未覆盖 ⇒ 未覆盖 / blocking",
        "verdict=%s severity=%s stats=%s" % (e["verdict"], e["severity"], res["stats"]))

    # ③ confidence < 0.6 ⇒ 未覆盖 / warning（不阻断）
    res = R.judge([_element(confidence=0.5)], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["verdict"] == R.VERDICT_UNCOVERED and e["severity"] == R.SEVERITY_WARNING
        and res["stats"]["blocking"] == 0 and res["stats"]["warning"] == 1,
        "③ confidence=0.5 < 0.6 ⇒ 未覆盖 / warning",
        "verdict=%s severity=%s stats=%s" % (e["verdict"], e["severity"], res["stats"]))

    # ③b ★★ **阈值边界**：confidence **恰好 = 0.6** ⇒ 必须 blocking
    #
    # 🔴 为什么这条必须存在（实测踩到过，不是假想）
    # --------------------------------------------
    # 冻结口径写的是「confidence **≥** 0.6 ⇒ 阻断」，即 0.6 **本身**够格。
    # 实现里若把比较符写成 ``<=``（0.6 <= 0.6 为真），0.6 就会被降级成警告——
    # 而那句文案还写着「confidence=0.6 < 0.6，按警告处理」，
    # **代码与文案自相矛盾**（0.6 < 0.6 是假的）。
    # 后果很实际：实例层给「月度」的置信度**恰好是 0.60**，
    # 于是「月度」永远进不了阻断级 ⇒ 需求要素缺失在主链路上静默放过。
    # ⇒ 边界（= 阈值）必须单独钉一条，**不能**只测「小于阈值」那侧。
    res = R.judge([_element(confidence=0.6)], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["verdict"] == R.VERDICT_UNCOVERED and e["severity"] == R.SEVERITY_BLOCKING
        and res["stats"]["blocking"] == 1 and res["stats"]["warning"] == 0,
        "③b confidence=0.6（恰好等于阈值）⇒ 未覆盖 / blocking（≥ 0.6 口径）",
        "verdict=%s severity=%s stats=%s" % (e["verdict"], e["severity"], res["stats"]))
    # 边界另一侧：0.599999999 仍应是警告（确认比较是「严格小于」而非「小于等于」）
    res = R.judge([_element(confidence=0.599999999)], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["severity"] == R.SEVERITY_WARNING and res["stats"]["warning"] == 1,
        "③b 边界另一侧 confidence=0.599999999 ⇒ warning（严格小于 0.6）",
        "severity=%s" % e["severity"])

    # ④ needs_confirmation=True ⇒ 未覆盖 / warning（不阻断）
    res = R.judge([_element(needs_confirmation=True)], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["verdict"] == R.VERDICT_UNCOVERED and e["severity"] == R.SEVERITY_WARNING
        and res["stats"]["blocking"] == 0,
        "④ needs_confirmation=True ⇒ 未覆盖 / warning",
        "verdict=%s severity=%s" % (e["verdict"], e["severity"]))

    # ④b 边界：confidence=0.6（够格）但 needs_confirmation=True ⇒ 仍 warning
    #（三条件是**与**关系：任一不满足即降级为警告）
    res = R.judge([_element(confidence=0.6, needs_confirmation=True)], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["severity"] == R.SEVERITY_WARNING and res["stats"]["blocking"] == 0,
        "④b confidence=0.6 但待确认=True ⇒ warning（三条件是与关系）",
        "severity=%s" % e["severity"])

    # ⑤ polarity=may_omit ⇒ 不判（不是缺口）
    res = R.judge([_element(polarity="may_omit")], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["verdict"] == R.VERDICT_NOT_JUDGED and e["severity"] == R.SEVERITY_NONE
        and res["verdict"] == R.VERDICT_NOT_JUDGED
        and res["stats"]["not_judged"] == 1 and res["stats"]["uncovered"] == 0
        and len(res["gaps"]) == 0,
        "⑤ polarity=may_omit ⇒ 不判（不算缺口）",
        "verdict=%s stats=%s" % (e["verdict"], res["stats"]))

    # ⑥ matcher kind 未注册 ⇒ 不判 + 单列统计（**绝不当成已覆盖**）
    res = R.judge([_element(matcher="metric:销售额")], facts_none)
    e = res["elements"][0]
    all_ok &= _ok(
        e["verdict"] == R.VERDICT_NOT_JUDGED
        and res["stats"]["unjudged_unknown_matcher"] == 1
        and res["stats"]["covered"] == 0 and res["stats"]["uncovered"] == 0,
        "⑥ matcher kind 未注册 ⇒ 不判 + 单列统计（不当已覆盖）",
        "verdict=%s stats=%s" % (e["verdict"], res["stats"]))

    # 附加：粒度按**类别**判 —— 4 种写法都算覆盖了 month
    print("    -- 附加：粒度按类别判（4 种写法等价）--")
    for label, sql in [
        ("date_trunc", SQL_MONTH),
        ("to_char", "SELECT to_char(order_date,'YYYY-MM') AS m, COUNT(*) AS cnt "
                    "FROM dwd_order_di GROUP BY 1"),
        ("裸列", "SELECT stat_month, COUNT(*) AS cnt FROM dws_store_daily_agg "
                 "GROUP BY stat_month"),
        ("substr", "SELECT substr(order_date,1,7) AS m, COUNT(*) AS cnt "
                   "FROM dwd_order_di GROUP BY 1"),
    ]:
        res = R.judge([_element()], R.extract_facts(sql))
        all_ok &= _ok(res["verdict"] == R.VERDICT_COVERED,
                      "    %s 实现 ⇒ 同一「月度」需求判为已覆盖" % label,
                      "verdict=%s" % res["verdict"])

    # 附加：判据层**不含**策略 —— 输出里不得出现 status / block 之类的字段
    res = R.judge([_element()], facts_none)
    forbidden = [k for k in res.keys() if k in ("status", "blocked", "block", "should_block")]
    all_ok &= _ok(not forbidden,
                  "附加：judge() 输出不含「要不要判红」策略字段",
                  "顶层键=%s" % sorted(res.keys()))
    return all_ok


# ==========================================================================
# 3. 确定性：同一 SQL 解析两次，SqFacts 输出逐字节相同
# ==========================================================================
#
# 为什么必须钉：判定结论会被写进门禁报告并进入审计。
# 若同一 SQL 两次解析出的事实不同（例如集合无序、dict 顺序不定），
# 则「同一份 SQL 今天判覆盖、明天判未覆盖」⇒ 门禁不可信、审计不可追。
#
# 判据：``json.dumps(facts.to_dict(), sort_keys=True)`` 的 **UTF-8 字节**全等。

DETERMINISM_SQLS = [
    SQL_MONTH,
    SQL_NO_TIME,
    "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt, COUNT(*) AS cnt "
    "FROM products p JOIN order_items oi ON p.id = oi.product_id "
    "WHERE oi.status = '已完成' AND oi.dt >= '2025-01-01' "
    "GROUP BY p.category_name",
    "SELECT to_char(order_date,'YYYY-MM') AS m, COUNT(*) FROM t GROUP BY 1",
    "SELECT stat_month, COUNT(*) FROM t GROUP BY stat_month",
]


def test_determinism():
    print()
    print("=" * 72)
    print("3. 确定性：同一 SQL 解析两次，SqFacts 输出逐字节相同")
    print("=" * 72)
    all_ok = True
    for sql in DETERMINISM_SQLS:
        b1 = json.dumps(R.extract_facts(sql).to_dict(),
                        sort_keys=True, ensure_ascii=False).encode("utf-8")
        b2 = json.dumps(R.extract_facts(sql).to_dict(),
                        sort_keys=True, ensure_ascii=False).encode("utf-8")
        same = (b1 == b2)
        all_ok &= _ok(same, "逐字节相同：%s" % sql[:58].replace("\n", " "),
                      "len=%d" % len(b1))
    # 附加：to_dict() 的 JSON 必须是**稳定序列化**的（键有序）
    facts = R.extract_facts(SQL_MONTH)
    d1 = json.dumps(facts.to_dict(), sort_keys=True, ensure_ascii=False)
    d2 = json.dumps(facts.to_dict(), sort_keys=True, ensure_ascii=False)
    all_ok &= _ok(d1 == d2, "附加：to_dict() 序列化稳定")
    return all_ok


# ==========================================================================
# 4. ElementContract 的 8 字段校验：缺字段 / 类型错要被拒
# ==========================================================================
#
# 为什么必须钉：契约宽松 = 静默失效。上游漏传 matcher 时若被"宽容补默认"，
# 判定照跑、结论照出，但**结论是关于另一个问题的**——
# 业务方看到"已覆盖"却根本没人检查过他要的东西。
# 宁可当场拒收（调用方立刻炸），也不要产出看起来正常的错结论。

def test_contract_validation():
    print()
    print("=" * 72)
    print("4. ElementContract 的 8 字段校验：缺字段 / 类型错要被拒")
    print("=" * 72)
    all_ok = True

    # 基准：8 字段齐备 ⇒ 构造成功
    good = _element()
    try:
        ec = R.ElementContract.from_dict(good)
        built = True
        err = ""
    except R.ElementContractError as exc:
        built = False
        ec = None
        err = str(exc)
    all_ok &= _ok(built, "基准：8 字段齐备 ⇒ 构造成功", err)
    all_ok &= _ok(ec is not None and sorted(ec.to_dict().keys()) == sorted(R.CONTRACT_FIELDS),
                  "基准：契约字段恰为 8 个且不多不少",
                  "字段=%s" % sorted(R.CONTRACT_FIELDS))

    # 逐字段缺失 ⇒ 必须被拒
    for field in R.CONTRACT_FIELDS:
        bad = dict(good)
        bad.pop(field)
        try:
            R.ElementContract.from_dict(bad)
            rejected = False
            msg = "**未被拒收**"
        except R.ElementContractError as exc:
            rejected = True
            msg = str(exc)
        all_ok &= _ok(rejected, "缺字段 %s ⇒ 被拒" % field, msg)

    # 逐字段类型错 ⇒ 必须被拒
    type_bad = [
        ("element", 123),
        ("slot", None),
        ("provenance", ["a"]),
        ("confidence", "0.85"),
        ("needs_confirmation", "false"),
        ("mandatory", 1),
        ("polarity", "MAY_COVER"),
        ("matcher", 42),
    ]
    for field, value in type_bad:
        bad = dict(good)
        bad[field] = value
        try:
            R.ElementContract.from_dict(bad)
            rejected = False
            msg = "**未被拒收**"
        except R.ElementContractError as exc:
            rejected = True
            msg = str(exc)
        all_ok &= _ok(rejected, "类型错 %s=%r ⇒ 被拒" % (field, value), msg)

    # 契约外字段 ⇒ 必须被拒（防契约漂移无声发生）
    bad = dict(good)
    bad["extra_field"] = 1
    try:
        R.ElementContract.from_dict(bad)
        rejected = False
        msg = "**未被拒收**"
    except R.ElementContractError as exc:
        rejected = True
        msg = str(exc)
    all_ok &= _ok(rejected, "契约外字段 ⇒ 被拒", msg)

    # 边界值：confidence 必须落在 [0,1]
    for conf, should_reject in [(-0.1, True), (1.1, True), (0.0, False), (1.0, False)]:
        bad = dict(good)
        bad["confidence"] = conf
        try:
            R.ElementContract.from_dict(bad)
            rejected = False
            msg = "接受"
        except R.ElementContractError as exc:
            rejected = True
            msg = str(exc)
        all_ok &= _ok(rejected == should_reject,
                      "confidence=%s ⇒ %s" % (conf, "被拒" if should_reject else "接受"),
                      msg)

    # bool 不许当数字用（Python 里 bool 是 int 的子类）
    bad = dict(good)
    bad["confidence"] = True
    try:
        R.ElementContract.from_dict(bad)
        rejected = False
        msg = "**未被拒收**"
    except R.ElementContractError as exc:
        rejected = True
        msg = str(exc)
    all_ok &= _ok(rejected, "confidence=True（bool 冒充数字）⇒ 被拒", msg)
    return all_ok


def main():
    results = [
        ("1. 粒度提取", test_granularity_extraction()),
        ("2. 分级判定", test_judge_grading()),
        ("3. 确定性", test_determinism()),
        ("4. 契约校验", test_contract_validation()),
    ]
    print()
    print("=" * 72)
    all_ok = True
    for name, ok in results:
        all_ok &= ok
        print("  %-14s %s" % (name, "PASS" if ok else "FAIL"))
    print("=" * 72)
    if all_ok:
        print("自证全部通过")
        return 0
    print("自证有失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
