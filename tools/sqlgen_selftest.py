#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 生成侧的纯函数自证（离线，不需要数据库 / Wren）。

为什么单独做这个自证
--------------------
`sqlgen.py` 是「结构化需求 → SQL」这一段的落点，是主链路之一，
此前**没有任何单元测试**。而 `generate()` 要连数据库取需求、连语义层 dry_run，
没法离线跑；但它内部有几个**纯函数**决定了「生成结果长什么样」，
一旦悄悄改错，外部表现全是"SQL 能跑但内容不对"：

  · `_split_guide` —— 把多行执行指南拆成前端步骤数组。
    它负责**剥掉「①」「1.」这类序号前缀**，由前端统一重新编号。
    如果它改坏了，前端就会出现"步骤编号跟正文对不上"，
    甚至序号被当成正文内容念出来。

  · `_classify_gap` —— 把差距描述分类，**决定前端着色和责任方归属**。
    分错的后果是：把"数据缺失"（要业务+技术一起补）显示成"技术方待上架"，
    指派给错的人，需求就卡住了。

  · `_extract_between` —— 从文本里截取区间，是若干口径解析的基础动作。

这三个都是纯函数，可离线钉死。

用法：
    python3 tools/sqlgen_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import sqlgen as sg  # noqa: E402


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# _split_guide：执行指南拆步
# ---------------------------------------------------------------------------

@case("拆步：带圈序号应被剥掉（前端统一编号，不能出现两个序号）")
def _t_guide_circle():
    out = sg._split_guide("① 提交需求\n② 确认口径\n③ 生成 SQL")
    assert len(out) == 3, "应拆成3 步，实际=%d 步：%r" % (len(out), out)
    for i, s in enumerate(out):
        assert not s.startswith("①②③④⑤"), (
            "第%d 步仍带圈序号 %r，前端会显示双重编号" % (i + 1, s)
        )
    assert out[0] == "提交需求", "第1 步内容应为「提交需求」，实际=%r" % out[0]


@case("拆步：带点号序号应被剥掉（1. 2. 3.）")
def _t_guide_dot():
    out = sg._split_guide("1. 提交需求\n2. 确认口径\n3. 生成 SQL")
    assert len(out) == 3, "应拆成 3 步，实际=%r" % out
    assert out == ["提交需求", "确认口径", "生成 SQL"], "序号未剥干净：%r" % out


@case("拆步：带顿号序号应被剥掉（1、2、）")
def _t_guide_dun():
    out = sg._split_guide("1、提交需求\n2、确认口径")
    assert out == ["提交需求", "确认口径"], "顿号序号未剥干净：%r" % out


@case("拆步：无序号的纯文本按行拆，且去掉首尾空白")
def _t_guide_plain():
    out = sg._split_guide("  提交需求  \n\n  确认口径\n")
    assert out == ["提交需求", "确认口径"], "空行未被正确跳过或空白未清理：%r" % out


@case("拆步：空输入 / None 返回空数组（前端会直接 .map，不能给 null）")
def _t_guide_empty():
    assert sg._split_guide("") == []
    assert sg._split_guide(None) == []
    assert sg._split_guide("   \n  \n") == [], "纯空白行应产出空数组"


@case("拆步：单步不带序号时原样保留")
def _t_guide_single():
    assert sg._split_guide("直接执行") == ["直接执行"]


@case("拆步：序号前缀不能吃掉正文（剥完仍要有内容）")
def _t_guide_keeps_body():
    out = sg._split_guide("① 这是第一步的正文")
    assert out == ["这是第一步的正文"], "剥序号时把正文一起吃掉了：%r" % out


# ---------------------------------------------------------------------------
# _classify_gap：差距分类与责任方
# ---------------------------------------------------------------------------

@case("分类：「数据缺失 / 无行为类数据」应归业务+技术，且kind=数据缺失")
def _t_gap_missing():
    for t in ("该表没有行为类数据", "数据缺失：会员行为日志表不存在"):
        r = sg._classify_gap(t)
        assert r["kind"] == "数据缺失", (
            "「%s」应归为「数据缺失」，实际=%r" % (t, r["kind"])
        )
        assert "业务" in r["owner"] and "技术" in r["owner"], (
            "「%s」的责任方应同时含业务与技术，实际=%r" % (t, r["owner"])
        )


@case("分类：「口径未定义」应归业务方")
def _t_gap_undefined():
    r = sg._classify_gap("复购率的口径未定义")
    assert r["kind"] == "口径未定义", "实际=%r" % r["kind"]
    assert "业务" in r["owner"], "口径未定义须归业务方，实际=%r" % r["owner"]


@case("分类：「待上架」应归技术方")
def _t_gap_pending():
    r = sg._classify_gap("券核销口径待上架")
    assert r["kind"] == "口径待上架", "实际=%r" % r["kind"]
    assert "技术" in r["owner"], "待上架须归技术方，实际=%r" % r["owner"]


@case("分类：无法识别的应落到默认类，且仍有责任方（不能空 owner）")
def _t_gap_default():
    for t in ("", None, "说不清楚的问题"):
        r = sg._classify_gap(t)
        assert r.get("kind"), "「%r」未分类出 kind" % t
        assert r.get("owner"), "「%r」的 owner 为空，需求将无人可指派" % t


@case("分类：每类都必须给出 owner（前端据此指派，缺了就卡住）")
def _t_gap_all_have_owner():
    for t in ("数据缺失", "口径未定义", "待上架", "乱七八糟"):
        r = sg._classify_gap(t)
        assert isinstance(r, dict) and set(("kind", "owner")) <= set(r), (
            "分类结果必须含 kind 与 owner 两个键，实际=%r" % r
        )


# ---------------------------------------------------------------------------
# _extract_between：区间截取
# ---------------------------------------------------------------------------

@case("截取：正常区间")
def _t_between_ok():
    txt = "口径为[坪效]计算"
    assert sg._extract_between(txt, "[", "]") == "坪效", "实际=%r" % sg._extract_between(txt, "[", "]")


@case("截取：缺失起始符返回空串（不能抛异常，也不能截错）")
def _t_between_no_start():
    assert sg._extract_between("没有方括号", "[", "]") == ""


@case("截取：缺结束符时取到末尾（不能返回空）")
def _t_between_no_end():
    r = sg._extract_between("前缀[内容", "[", "]")
    assert r == "内容", "实际=%r" % r


@case("截取：结果去掉尾部句读")
def _t_between_strip_punct():
    assert sg._extract_between("前缀[内容。]", "[", "]") == "内容", (
        "尾部句读未去掉：%r" % sg._extract_between("前缀[内容。]", "[", "]")
    )


@case("截取：空输入返回空串")
def _t_between_empty():
    assert sg._extract_between("", "[", "]") == ""
    assert sg._extract_between(None, "[", "]") == ""


@case("规划器粒度：需求写了「按月」⇒ SQL 必须按月分组（不许静默丢粒度）")
def _t_planner_month_granularity():
    """2026-10-23 实测缺陷：`各门店销售额` 意图原先只看「各门店」，
    需求里的「按月」被直接丢掉 ⇒ SQL 少了月度维度。REQ-COVERAGE 上线后这类 SQL
    会被正确判红，但**产品不能只会拦**——同一句话说了按月，就该按月出数。

    这里直接调 plan_b（不经 registry），所以用一个只提供 models 的轻量替身。
    """
    import planner as pl

    class _DS:
        key = "B"
        models = {"dwd_order_di": {}, "dim_store": {}}

    monthly = pl.plan_b(_DS(), "各门店按月的销售额")
    assert monthly.get("blocked") is not True, monthly.get("reason")
    sql_m = monthly.get("sql") or ""
    assert "date_trunc('month'" in sql_m, "按月需求却没用月度表达式：%s" % sql_m
    assert "GROUP BY" in sql_m and "月份" in sql_m, "月度维度没进 SELECT/GROUP BY：%s" % sql_m

    plain = pl.plan_b(_DS(), "各门店销售额")
    assert plain.get("blocked") is not True, plain.get("reason")
    sql_p = plain.get("sql") or ""
    assert "date_trunc" not in sql_p, "没要求按月却加了月度维度（过度拟合）：%s" % sql_p


class _StubDS(object):
    """B 库的最小替身：只提供 plan_b 需要的 `models` 键。

    ⚠️ 用**全部 8 张表**而不是只放两张：`_objects()` 会按 models 求交集，
    替身缺表时 SQL 里的表会被悄悄过滤掉，判据就测不到真实形态了。
    """
    key = "B"
    models = {name: {} for name in (
        "ads_coupon_order_di", "ads_member_grade_change_wi", "ads_member_repurchase_di",
        "dim_member", "dim_store", "dwd_order_detail_di", "dwd_order_di",
        "dws_store_daily_agg",
    )}


def _plan_b(text):
    import planner as pl
    r = pl.plan_b(_StubDS(), text)
    return (r.get("sql") or ""), r


@case("规划器：说「排名」就要排序；没说就不要加（双向防过拟合）")
def _t_planner_rank():
    """2026-10-23 基线实测缺陷：`各门店销售额排名` 生成的 SQL **没有 ORDER BY** ——
    "排名"只是把行列出来，业务方拿到的是一堆无序数据。"""
    ranked, _r = _plan_b("各门店销售额排名")
    assert "order by" in ranked.lower(), "说了排名却没有排序：%s" % ranked
    assert "desc" in ranked.lower(), "排序方向必须是降序（排名＝从高到低）：%s" % ranked

    plain, _r2 = _plan_b("各门店销售额")
    assert "order by" not in plain.lower(), "没要求排序却加了 ORDER BY（过度拟合）：%s" % plain

    # ⚠️ 单字「前」不能当排序信号：「目前」「之前」会误命中
    cur, _r3 = _plan_b("目前各门店销售额")
    assert "order by" not in cur.lower(), "「目前」被误判成排序意图：%s" % cur

    top, _r4 = _plan_b("前5名门店销售额")
    assert "order by" in top.lower(), "「前5名」没有排序：%s" % top


@case("规划器：一句话要两样东西就要出两样（订单量 + 销售额）")
def _t_planner_two_metrics():
    """2026-10-23 基线实测缺陷：`各门店月度订单量和销售额` 只出了销售额，
    订单数被整段吞掉。这类"说了没做"以前没人逐项比对需求，所以一直没被发现。"""
    sql, _r = _plan_b("各门店月度订单量和销售额")
    assert "count(" in sql.lower(), "要了订单量却没有计数：%s" % sql
    assert "order_cnt" in sql or "订单数" in sql, "订单数没有出现在输出列：%s" % sql
    assert "pay_amount" in sql, "销售额丢了：%s" % sql
    assert "date_trunc('month'" in sql, "月度维度丢了：%s" % sql


@case("规划器：说「占比」就要算出占比（占比的分母范围要跟维度一致）")
def _t_planner_share():
    total, _r = _plan_b("各门店销售额占比")
    assert "over (" in total.lower(), "说了占比却没有窗口函数算分母：%s" % total
    assert "partition by" not in total.lower(), (
        "不带月度却在窗口里分区了（分母变成每行自己）：%s" % total)

    monthly, _r2 = _plan_b("各门店按月的销售额占比")
    assert "partition by" in monthly.lower(), (
        "月度占比没有按月分区 ⇒ 分母成了全期合计，跨月比较没有意义：%s" % monthly)


@case("规划器：客单价是派生指标（不能被判成「数据缺失」）")
def _t_planner_aov():
    """2026-10-23 基线实测缺陷：客单价走了兜底，提示「B 库无客单价相关表」——
    客单价本来就不是一张表，这句提示会把业务方引去找数据源。"""
    sql, r = _plan_b("各门店客单价")
    assert "客单价" in sql, "客单价需求没有生成 SQL：%s / %s" % (sql, r.get("reason"))
    assert "count(distinct" in sql.lower(), (
        "客单价分母必须是不同订单号（一单多明细会重复计）：%s" % sql)
    assert "pay_amount" in sql, "客单价分子应为实付金额：%s" % sql

    overall, _r2 = _plan_b("客单价")
    assert "客单价" in overall, "整体客单价也没出数：%s" % overall


@case("规划器：泛化说法「复购情况」不该被当成「未建模」硬拒")
def _t_planner_repurchase_generic():
    sql, r = _plan_b("会员复购情况")
    assert sql, "表与字段都在库里却拒了（这是「明明有数据说没有」）：%s" % r.get("reason")
    assert "repurchase_flag" in sql, "复购口径没用到标记列：%s" % sql
    # 不替业务方算比率：分母口径未确认，算了会被直接拿去汇报
    assert "复购率" not in sql, "未指定比率却自己算了复购率：%s" % sql


@case("规划器：说「按月」就要按月分组（等级异动）")
def _t_planner_grade_change_month():
    sql, _r = _plan_b("会员等级异动按月趋势")
    assert "change_month" in sql, "change_month 就在表里，说按月却没按月分组：%s" % sql
    assert "group by" in sql.lower(), "没有分组：%s" % sql

    plain, _r2 = _plan_b("会员等级异动情况")
    assert "change_month" not in plain, "没要求按月却加了月份维度：%s" % plain


@case("规划器：同比/环比按「日历」对基期，不用行号窗口")
def _t_planner_yoy_mom():
    """2026-10-23 基线实测缺陷：说「同比 / 环比」→ 返回一张普通月度销售额表，
    对比那一段整段丢掉。

    ⚠️ 这里钉的不是"有没有对比列"，而是**基期怎么取**：
      `LAG(SUM(x), 1) OVER (ORDER BY 月份)` 在月份连续时与日历自连接**结果完全一样**，
      只有缺一个月时才分道扬镳 —— 行号窗口会拿两个月前的数当"上月"，
      而 SQL 照样跑通、门禁照样过，**只是数字是错的**。
      所以必须断言"没有 LAG"，否则哪天有人图省事换成窗口函数，
      在连续月份的数据上看不出任何区别，测试也不会红。
    """
    # ⚠️ 断言一律对 lower() 做：SQL 里的关键字是 `INTERVAL` / `ORDER BY` 大写，
    #   直接对原文断言会因为大小写而永远失败（这里第一次写就踩了）。
    mom, _r = _plan_b("各门店销售额环比")
    _m = mom.lower()
    assert "interval '1 month'" in _m, "环比基期没有按日历取上一个月：%s" % mom
    assert "lag(" not in _m, (
        "用了行号窗口取基期：缺月时会拿错月份且不报错：%s" % mom)
    assert "环比" in mom, "没有环比列：%s" % mom
    assert "interval '1 year'" not in _m, "只要环比却带上了同比：%s" % mom

    yoy, _r2 = _plan_b("销售额同比")
    _y = yoy.lower()
    assert "interval '1 year'" in _y, "同比基期没有按日历年取：%s" % yoy
    assert "lag(" not in _y, "同比用了行号窗口：%s" % yoy
    # 基期金额列必须一起出：否则增长率整列 NULL 时业务方分不清"没数据"还是"算错"
    assert "去年同期" in yoy, (
        "没输出基期金额列 ⇒ 增长率空的时候说不出原因：%s" % yoy)

    both, _r3 = _plan_b("各门店销售额同比和环比")
    _b = both.lower()
    assert "interval '1 year'" in _b and "interval '1 month'" in _b, (
        "要了两样却只给一样：%s" % both)

    plain, _r4 = _plan_b("各门店销售额")
    assert "interval" not in plain.lower(), (
        "没要同比环比却加了对比（过度拟合）：%s" % plain)


@case("规划器：组合分支里的「排名 / 前N名」不能被吃掉")
def _t_planner_combo_rank():
    """2026-10-23 基线实测缺陷：`各门店会员数和销售额排名` 返回按**门店名**排的表
    —— 排序是有的，只是排在错的列上。这比"没有排序"更难发现：
    SQL 里有 ORDER BY、门禁看不出问题，只有拿去做"前十名"时才发现顺序不对。
    """
    plain, _r = _plan_b("各门店会员数和销售额")
    assert "order by s.store_name" in plain.lower(), (
        "不排名时应按门店名稳定输出：%s" % plain)
    assert "desc" not in plain.lower(), "没要求排名却降序了（过度拟合）：%s" % plain
    assert "limit" not in plain.lower(), "没要求前N名却限行了：%s" % plain

    ranked, _r2 = _plan_b("各门店会员数和销售额排名")
    assert "desc" in ranked.lower(), "说了排名却没有降序：%s" % ranked
    assert "销售额" in ranked, "排名基数不是销售额：%s" % ranked
    # 两个指标一个都不能少（排名不该把组合吃掉）
    assert "dim_member" in ranked and "pay_amount" in ranked, (
        "排名把跨指标组合吃掉了：%s" % ranked)

    top3, _r3 = _plan_b("各门店会员数和销售额前3名")
    assert "limit 3" in top3.lower(), (
        "「前3名」只排序不限行 ⇒ 返回全榜单：%s" % top3)


@case("规划器：中位数必须是 MEDIAN，不是 SUM")
def _t_planner_median():
    sql, _r = _plan_b("各门店销售额中位数")
    assert "median(" in sql.lower(), "问中位数没给 MEDIAN：%s" % sql
    _flat = sql.lower().replace(" ", "")
    assert "sum(o.pay_amount)as销售额" not in _flat, (
        "中位数被换成了合计 —— 中位数的意义正是「不被少数大额订单拉高」，"
        "换成 SUM 等于把这个问题取消了：%s" % sql)


@case("规划器：说「每天」就要落到日粒度")
def _t_planner_daily():
    sql, _r = _plan_b("各门店每天的销售额")
    assert "order_date" in sql, "日粒度没有用业务日期：%s" % sql
    assert "date_trunc('month'" not in sql, (
        "说了「每天」却按月分组（粒度丢失）：%s" % sql)


@case("规划器：「最高的品类」要出品类列（组内取最大，不是跨店排名）")
def _t_planner_top_category():
    sql, _r = _plan_b("各门店销售额最高的品类")
    assert "category_name" in sql, "问品类却没有品类列（问 A 答 B）：%s" % sql
    assert "row_number" in sql.lower(), "没有组内排名，取不出「每店最高的那个」：%s" % sql
    assert "partition by" in sql.lower(), "组内排名没有分区 ⇒ 变成全表排名：%s" % sql
    assert "date_trunc('month'" not in sql, (
        "「最高的品类」被串到「最高的月份」分支了：%s" % sql)


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
    print("\nsqlgen_selftest: %d/%d 通过" % (total - len(failed), total))
    if failed:
        print("失败项：")
        for name, msg in failed:
            print("  - %s：%s" % (name, msg))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())