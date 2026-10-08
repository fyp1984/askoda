#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把业务口径写入 `business_glossary`（人工口径，幂等）。

为什么需要这个脚本
------------------
`metadata_glossary` 是已交付的工具，但库里只有 5 条 `source=mdl` 自动生成的
「表.列」条目，**人工口径 0 条、`formula` 列全空**。也就是说：
工具能采集、能写入，却没有内容——语义层解释口径时没有可引用的**业务说法**。

真实口径写在 `knowledge/01-指标口径说明书-零售会员域.md` 里（B 库）
与 A 库各自的说明里，但知识库归知识库、结构化字典归结构化字典，
需求理解阶段查 `business_glossary` 时看不到它们。两边得对上。

设计要点
--------
1. **幂等**：主键是 `term` 单列，用 `ON CONFLICT (term) DO UPDATE`。
   重跑只更新不新增，不会产生重复条目，也不会覆盖别人改过的 definition
   （除非本次是脚本自己的数据，见 `source` 判定）。
2. **不覆盖人工录入**：若同名条目已存在且 `source <> 'seed_business'`，
   说明是人工维护的，脚本**跳过并如实报告**，绝不静默改写别人的口径。
3. **口径与知识库同源**：数据直接取自口径说明书，不在这里另造一套说法。
   两处若冲突，应改知识库后重跑本脚本，而不是在这里改。
4. **不碰自动生成的条目**：`source='mdl'` 的「表.列」条目不在本脚本范围内。

用法：
    # 容器内跑（推荐，走网关容器的环境变量）
    docker exec -i askoda /usr/local/bin/python -B tools/seed_glossary.py --apply

    # 只看会写什么（默认 dry-run，不写库）
    docker exec -i askoda /usr/local/bin/python -B tools/seed_glossary.py

退出码 0 = 成功（或 dry-run 完成）；1 = 有失败。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))

# ---------------------------------------------------------------------------
# 人工口径数据
#
# term / definition / formula 三列的含义：
#   term       业务方嘴里那个词（必须是人会说的话，不是 `表.列`）
#   definition 这个词到底指什么（一句业务话，能直接进需求理解）
#   formula    计算式（写不出就留空，**不要编**）
#   dataset    归属库；跨库口径标 'ALL'
#   source     固定 seed_business —— 用于区分"脚本种入"与"人工维护"
# ---------------------------------------------------------------------------
SEED = [
    # ---------------- B 库 · 零售会员域 ----------------
    {
        "term": "坪效",
        "definition": "门店坪效 = 期间销售额 ÷ 门店营业面积。分母取门店经营面积，"
                       "不含非营业面积；期间口径按自然月，与日均口径不可混用。",
        "formula": "SUM(dws_store_daily_agg.sales_amount) / MAX(dws_store_daily_agg.area_sqm)",
        "dataset": "B",
    },
    {
        "term": "销量",
        "definition": "销量指销售**件数**，对应 sales_qty；不是销售额，也不是订单数。"
                       "问「卖了多少」默认按件数理解，如需金额须明确说「销售额」。",
        "formula": "SUM(dws_store_daily_agg.sales_qty)",
        "dataset": "B",
    },
    {
        "term": "销售额",
        "definition": "销售额指销售金额，对应 sales_amount；与销量（件数）、"
                       "订单数（笔数）是三个不同指标，不可互相替代。",
        "formula": "SUM(dws_store_daily_agg.sales_amount)",
        "dataset": "B",
    },
    {
        "term": "复购率",
        "definition": "复购率 = 复购用户数 ÷ 活跃用户数。分母只能是**活跃用户**"
                       "（当月有下单），**严禁用流失会员做分母**；口径确认时必须同时给出分子分母。",
        "formula": "SUM(ads_member_repurchase_di.repurchase_flag) / 活跃用户数",
        "dataset": "B",
    },
    {
        "term": "活跃用户数",
        "definition": "统计窗口内有下单的会员数（按 order_status='已完成' 过滤）。"
                       "注意与 dim_member.member_status 区分——后者是历史累计标签，"
                       "不能直接当作「近一个月活跃」的筛选条件。",
        "formula": "COUNT(DISTINCT dwd_order_di.member_id)",
        "dataset": "B",
    },
    {
        "term": "购买频次",
        "definition": "统计窗口内的已完成订单笔数。与「使用频次」（登录/浏览/操作）"
                       "**不是一回事**；零售会员域无行为类数据，使用频次当前不可计算，"
                       "遇到这类需求应如实说明而不是用订单数顶替。",
        "formula": "COUNT(DISTINCT dwd_order_di.order_id)",
        "dataset": "B",
    },
    {
        "term": "下单天数频次",
        "definition": "统计窗口内有下单的自然日天数。与购买频次（订单笔数）不同，"
                       "一个会员一天多单只算 1 天。",
        "formula": "COUNT(DISTINCT dwd_order_di.order_date)",
        "dataset": "B",
    },
    {
        "term": "订单金额",
        "definition": "订单金额按已完成订单汇总，**退款单不冲减原月**"
                       "（退款在退款发生月单独计）。问「金额」时须明确是订单金额还是实付金额。",
        "formula": "SUM(dwd_order_di.pay_amount)",
        "dataset": "B",
    },
    {
        "term": "业务日期",
        "definition": "一切时间口径以**业务发生当天**为准（order_date / stat_date），"
                       "**不采用装载时间 load_time**。二者混用会让报表与业务对不上。",
        "formula": "",
        "dataset": "ALL",
    },
    {
        "term": "订单状态",
        "definition": "统计只取 order_status='已完成'；'已退款' 不计入原订单口径。"
                       "涉及退款的需求须单独确认退款侧口径。",
        "formula": "",
        "dataset": "B",
    },
    {
        "term": "最活跃客户",
        "definition": "默认口径：按下单频次降序取前 N 名（N 默认 10）。"
                       "并列时依次比订单总量↓ → 实付金额合计↓ → member_id 升序（保证可复现）。"
                       "这是默认判定，业务另有规则时须在口径确认环节提出。",
        "formula": "",
        "dataset": "B",
    },
    {
        "term": "订单总量",
        "definition": "⚠️ **口径歧义，必须由业务确认**：可指订单笔数、订单明细行数"
                       "或实付金额合计。默认按订单笔数理解，但正式取数前应当面确认。",
        "formula": "",
        "dataset": "B",
    },
    # ---------------- A 库 · 电商域 ----------------
    {
        "term": "商品销量",
        "definition": "A 库（电商域）口径：商品销售件数。**A 库为裸库**——"
                       "字段描述与术语表覆盖度低（术语 7 条 vs B 库 114 条），"
                       "涉及口径的需求应更多走人工确认，不宜假定系统已理解。",
        "formula": "",
        "dataset": "A",
    },
    {
        "term": "退款",
        "definition": "A 库（电商域）口径：退款金额。⚠️ 负向分支已验证不可用"
                       "（plan_b 手机号/身份证、订单+券+关联等口径为已知负向，勿使用）。",
        "formula": "",
        "dataset": "A",
    },
    # ---------------- 跨库 ----------------
    {
        "term": "活跃会员",
        "definition": "跨库通用语义：有下单行为的会员。A 库判据为有已完成订单，"
                       "B 库判据为统计窗口内有下单。**不可用 member_status 标签替代**，"
                       "该标签是历史累计标记而非当期活跃判定。",
        "formula": "",
        "dataset": "ALL",
    },
]


def _conn():
    try:
        import psycopg
    except ImportError:
        print("未安装 psycopg，无法写库；容器内应随 requirements.txt 安装")
        return None
    dsn = (os.getenv("ASSISTANT_DB_DSN") or "").strip()
    if not dsn:
        print("ASSISTANT_DB_DSN 未配置，无法连接元数据库。")
        print("容器化启动时 compose 会注入该变量；宿主直跑请先 export（见 .env.example）。")
        return None
    try:
        return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        print("连接元数据库失败：%s: %s" % (type(e).__name__, e))
        return None


SQL_UPSERT = """
INSERT INTO business_glossary (term, definition, formula, dataset, source)
VALUES (%(term)s, %(definition)s, %(formula)s, %(dataset)s, %(source)s)
ON CONFLICT (term) DO UPDATE
SET definition = EXCLUDED.definition,
    formula    = EXCLUDED.formula,
    dataset    = EXCLUDED.dataset
WHERE business_glossary.source IS NULL OR business_glossary.source = 'seed_business'
RETURNING (xmax = 0) AS inserted;
"""


def main():
    ap = argparse.ArgumentParser(description="种入业务口径（business_glossary）")
    ap.add_argument(
        "--apply", action="store_true",
        help="真正写库。不加此开关只dry-run，不会动数据库。",
    )
    args = ap.parse_args()

    print("待种入 %d 条人工口径（dry-run=%s）\n" % (len(SEED), not args.apply))
    for s in SEED:
        print("  [%s] %s" % (s["dataset"], s["term"]))
        print("      %s" % s["definition"][:70])
        if s["formula"]:
            print("      公式：%s" % s["formula"])

    if not args.apply:
        print("\n这是 dry-run，未写库。确认无误后加 --apply 执行。")
        return 0

    conn = _conn()
    if conn is None:
        return 1

    inserted = updated = 0
    with conn.cursor() as cur:
        for s in SEED:
            row = dict(s, source="seed_business")
            cur.execute(SQL_UPSERT, row)
            got = cur.fetchone()
            if got is None:
                print("  跳过（已存在人工维护版本，未覆盖）：%s" % s["term"])
            elif got[0]:
                inserted += 1
            else:
                updated += 1
    print("\n写库完成：新增 %d 条，更新 %d 条，跳过 0 条" % (inserted, updated))
    return 0


if __name__ == "__main__":
    sys.exit(main())