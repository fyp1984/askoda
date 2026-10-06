#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收方独立复扫 · B2b 四个主链路缺陷（P1-12 / P1-13 / P1-14 / P1-15）。

为什么单开这个脚本
------------------
B2b 交付时门禁 92 项全绿，但复核抓到的 4 个缺陷**一条都没被门禁覆盖**——
因为门禁里只有"能不能跑通"的判据，没有"候选纯度 / 词条质量 / 落库幂等"这类判据。
本脚本就是把这三类**做不出来的事**变成可回归的红线：

  P1-12 时间字段判据纯度：全列扫描，非时间类型一律不得通过；单字正词不得再误命中
  P1-13 时间字段不跨表：选定 time_field 必须落在本次相关表里（NF5O 曾取到券订单表）
  P1-14 口径词条质量：套话不得进词表；枚举取值必须是 enum 身份，不得升为字段候选
  P1-15 落库幂等：同一 (demand_id, stage, 内容指纹) 重复调用只写一次

用法（容器内）：
    docker exec -e PYTHONPATH=/app askoda python /tmp/b2b_defects_check.py
退出码 0 = 全过。
"""
import re
import sys

import db
import metadata
import evidence
import knowledge
import sqlgen

FAILS = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  -> %s" % str(detail)[:220]) if (detail and not cond) else ""))


TIME_TYPE_TOKENS = ("date", "time", "interval", "timestamp")

print("=" * 78)
print("B2b 四缺陷独立复扫")

# ===========================================================================
# P1-12 · 时间字段硬判据纯度
# ===========================================================================
print("\n-- P1-12 时间字段硬判据 --")

EXPECT_PASS = {
    "B": {
        "ads_coupon_order_di.stat_date", "ads_member_grade_change_wi.change_month",
        "ads_member_repurchase_di.stat_month", "dim_member.register_date",
        "dim_store.open_date", "dwd_order_di.load_time",
        "dwd_order_di.order_date", "dws_store_daily_agg.stat_date",
    },
    "A": {"customers.created_at", "orders.order_date", "refunds.refund_date"},
}

for ds in ("B", "A"):
    rows = db.query(
        "SELECT table_name, column_name, column_label, data_type, is_primary_key "
        "FROM column_docs WHERE dataset=%s ORDER BY table_name, column_name", (ds,))
    passed, bad_type = [], []
    for r in rows:
        loc = "%s.%s" % (r["table_name"], r["column_name"])
        ok, _why = metadata.is_eligible_time_field(
            loc, dataset=ds, data_type_hint=r["data_type"],
            is_pk_hint=bool(r["is_primary_key"]), label_hint=r["column_label"])
        if ok:
            passed.append(loc)
            dt = (r["data_type"] or "").lower()
            if not any(t in dt for t in TIME_TYPE_TOKENS):
                bad_type.append("%s(%s)" % (loc, r["data_type"]))
    ck("%s 库：通过硬判据的列**全部**是时间类型（共 %d 列通过）"
       % (ds, len(passed)), not bad_type, bad_type)
    got = set(passed)
    ck("%s 库：期望的时间列一个都没被误杀" % ds,
       EXPECT_PASS[ds].issubset(got), sorted(EXPECT_PASS[ds] - got))

# 曾被误放行的三类污染列
for loc, hint in [
    ("ads_member_repurchase_di.member_id", "整型主键"),
    ("dws_store_daily_agg.sales_amount", "当日销售额"),
    ("ads_coupon_order_di.coupon_amount", "券面额"),
    ("dws_store_daily_agg.day_type", "日期类型枚举"),
    ("dwd_order_detail_di.category_name", "品类名，描述含「分」"),
]:
    ok, why = metadata.is_eligible_time_field(loc, dataset="B")
    ck("污染列必须被拒：%s（%s）" % (loc, hint), not ok, why)

# A 库纯英文字段不得因描述含「分」放行
ok, why = metadata.is_eligible_time_field("products.category_name", dataset="A")
ck("A 库 products.category_name 必须被拒", not ok, why)

# 单字污染不再误判为时间正词
ck("「直接分析」不再被判含时间正词", not metadata._has_positive_time_word("便于免 JOIN 直接分析"))
ck("「当日销售额」不再被判含时间正词", not metadata._has_positive_time_word("当日销售额"))
ck("「周末」不再被判含时间正词", not metadata._has_positive_time_word("工作日 / 周末 / 节假日"))
ck("「订单日期」仍判含时间正词（正向不回退）", metadata._has_positive_time_word("订单日期"))
ck("英文 order_date 仍判含时间正词", metadata._has_positive_time_word("order_date"))

# ===========================================================================
# P1-13 · time_field 不跨表
# ===========================================================================
print("\n-- P1-13 时间字段不跨表 --")

NF5O = "DR-20261002-NF5O"
try:
    pl = sqlgen.plan(NF5O, "B")
    tf = pl.get("time_field") or ""
    ck("%s 的 time_field 不再是券订单表的列" % NF5O,
       "ads_coupon_order_di" not in tf, tf)
    scope = pl.get("time_field_scope_models") or []
    ck("%s 的 time_field 落在本次相关表 %s 内" % (NF5O, scope),
       (not tf) or any(tf.startswith(m + ".") for m in scope), "%s vs %s" % (tf, scope))
except Exception as e:  # noqa: BLE001
    ck("%s plan 不抛异常" % NF5O, False, "%s: %s" % (type(e).__name__, e))

for did, ds in [("DR-20261002-SOO1", "B"), ("DR-20261002-ZHAV", "B")]:
    try:
        pl = sqlgen.plan(did, ds)
        ck("%s 的 time_field 回归为 ads_member_repurchase_di.stat_month" % did,
           pl.get("time_field") == "ads_member_repurchase_di.stat_month", pl.get("time_field"))
        ck("%s 的前置过滤确实剔除了不合格列（filtered>0）" % did,
           len(pl.get("time_candidates_filtered") or []) > 0,
           pl.get("time_candidates_filtered"))
    except Exception as e:  # noqa: BLE001
        ck("%s plan 不抛异常" % did, False, "%s: %s" % (type(e).__name__, e))

# A 库不误伤
try:
    pl = sqlgen.plan("DR-20260930-JQT9", "A")
    ck("A 库需求不硬猜时间列（time_field 为空）", not (pl.get("time_field") or ""),
       pl.get("time_field"))
except Exception as e:  # noqa: BLE001
    ck("A 库 plan 不抛异常", False, "%s: %s" % (type(e).__name__, e))

# ===========================================================================
# P1-14 · 口径词条质量
# ===========================================================================
print("\n-- P1-14 口径词条质量 --")

lex = evidence.build_lexicon("B")
cal_terms = {t["term"] for t in lex if t["kind"] == "caliber_expression"}
BOILER = ["表主键之一", "无其他取值", "联合唯一", "关联",
          "分析一律用此字段", "装载时间不建模", "存当月1日"]
hit = [b for b in BOILER if b in cal_terms]
ck("口径词条里不得出现说明性套话", not hit, hit)

kind_by_term = {}
for t in lex:
    kind_by_term.setdefault(t["term"], t["kind"])
ck("「美妆」必须是 enum（过滤值），不得是字段候选来源",
   kind_by_term.get("美妆") == "enum", kind_by_term.get("美妆"))
ck("「个护」必须是 enum", kind_by_term.get("个护") == "enum", kind_by_term.get("个护"))
ck("caliber_expression 优先级低于 enum（数值更大）",
   evidence._KIND_PRIO["caliber_expression"] > evidence._KIND_PRIO["enum"],
   evidence._KIND_PRIO)
ck("正向口径词不回退：「月粒度」仍指向 ads_member_repurchase_di.stat_month",
   any(t["term"] == "月粒度"
       and t.get("kind") == "caliber_expression"
       and t.get("locator") == "ads_member_repurchase_di.stat_month" for t in lex))
ck("质量门槛函数：2 字及以下一律不收", not evidence._is_quality_caliber_phrase("关联"))

# ===========================================================================
# P1-15 · 审计落库幂等（用假 db，**不写真库**）
# ===========================================================================
print("\n-- P1-15 落库幂等（假库，不写真库） --")


class _FakeDb(object):
    """按**指纹**记账的假库：写过的指纹才能被后续查到（不碰真库）。"""

    def __init__(self):
        self.present = set()
        self.writes = 0

    def query_one(self, sql, params=None):
        pat = params[1] if (params and len(params) > 1) else ""
        return {"citation_id": "KC-FAKE"} if pat in self.present else None

    def execute(self, sql, params=None):
        self.writes += 1
        q = params[2] if (params and len(params) > 2) else ""
        m = re.search(r"AUTO_INTERNAL · stage=[^ ]+ · fp=[0-9a-f]+", q or "")
        if m:
            self.present.add("%" + m.group(0) + "%")
        return None

    def query(self, *a, **k):
        return []


_real_db = sys.modules.get("db")
_fake = _FakeDb()
try:
    sys.modules["db"] = _fake
    cites = [{"document_id": "D1", "document_name": "打桩文档", "chunk_id": "C%d" % i}
             for i in range(4)]
    r1 = knowledge.record_citations_once("DR-FAKE", "q", cites,
                                         stage="sqlgen_plan_stage", actor="check",
                                         fingerprint="ct=T;cits=C0|C1|C2|C3",
                                         dataset_id="sqlgen_plan_stage")
    r2 = knowledge.record_citations_once("DR-FAKE", "q", cites,
                                         stage="sqlgen_plan_stage", actor="check",
                                         fingerprint="ct=T;cits=C0|C1|C2|C3",
                                         dataset_id="sqlgen_plan_stage")
    ck("首次调用正常写入（4 条）", r1.get("inserted") == 4, r1)
    ck("重复调用被指纹挡住（不再新增）",
       r2.get("inserted") == 0 and r2.get("skipped") == "duplicate", r2)
    ck("重复调用没有真的插表", _fake.writes == 4, _fake.writes)
    # 指纹变化 → 允许再写（内容变了应该留新证据）
    r3 = knowledge.record_citations_once("DR-FAKE", "q", cites,
                                         stage="sqlgen_plan_stage", actor="check",
                                         fingerprint="ct=T2;cits=C0|C1|C2|C3",
                                         dataset_id="sqlgen_plan_stage")
    ck("指纹变化后允许再写一次", r3.get("inserted") == 4, r3)
finally:
    if _real_db is not None:
        sys.modules["db"] = _real_db
    else:
        sys.modules.pop("db", None)

ck("record_citations 原契约未被修改（仍是 6 个入参）",
   knowledge.record_citations.__code__.co_argcount == 6,
   knowledge.record_citations.__code__.co_varnames[:knowledge.record_citations.__code__.co_argcount])

# ===========================================================================
print("\n" + "=" * 78)
print("用例 %d 个，失败 %d 个" % (N[0], len(FAILS)))
if FAILS:
    for f in FAILS:
        print("   x %s" % f)
    sys.exit(1)
print("OK B2b 四缺陷独立复扫全过")
sys.exit(0)
