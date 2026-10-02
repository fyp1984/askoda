#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 B 库（零售/会员域）MDL 语义层 → wren-docker-b/workspace/mdl.json
依据：PRD §17.3（8 表 · 47 可见字段 · 9 口径 · 10 关系）
设计要点：
- 敏感字段（member_phone / id_card）与 load_time 建表【不建模】→ 对 AI 天然不可见（陷阱 #1/#6/#9）
- region 枚举刻意不含"西南大区"（陷阱 #8）
- 券核销的核销GMV/券成本口径为【待上架】（陷阱 #2/#3），MDL 中标注不上架
"""
import json, pathlib

WS = pathlib.Path(__file__).parent / "workspace"

def col(name, type_, desc=None, pk=False):
    c = {"name": name, "type": type_}
    if pk: c["isPrimaryKey"] = True
    if desc: c["description"] = desc
    return c

models = [
    {
        "name": "dwd_order_di",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "dwd_order_di"},
        "primaryKey": "order_id",
        "columns": [
            col("order_id", "integer", "订单ID", pk=True),
            col("member_id", "integer", "会员ID（关联 dim_member）"),
            col("store_id", "integer", "门店ID（关联 dim_store）"),
            col("pay_amount", "numeric", "实付金额（元）；口径：订单金额=SUM(sales_amount) 明细扩展，不含退款单"),
            col("order_date", "date", "业务日期（分析一律用此字段；装载时间不建模，禁止用装载时间过滤业务日期）"),
            col("order_status", "varchar", "订单状态；枚举：已完成 / 已退款"),
        ],
    },
    {
        "name": "dwd_order_detail_di",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "dwd_order_detail_di"},
        "primaryKey": "detail_id",
        "columns": [
            col("detail_id", "integer", "明细行ID", pk=True),
            col("order_id", "integer", "订单ID（关联 dwd_order_di）"),
            col("store_id", "integer", "门店ID（关联 dim_store）"),
            col("sku_id", "integer", "SKU编码"),
            col("category_name", "varchar", "维度退化列：品类名（美妆/个护/食品/服饰/家居/数码），免 JOIN 直接分组"),
            col("quantity", "integer", "购买数量"),
            col("unit_price", "numeric", "成交单价（元）"),
            col("sales_amount", "numeric", "销售金额（元）=数量×单价；口径：订单金额=SUM(sales_amount)，退款单除外"),
        ],
    },
    {
        "name": "dim_store",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "dim_store"},
        "primaryKey": "store_id",
        "columns": [
            col("store_id", "integer", "门店ID", pk=True),
            col("store_name", "varchar", "门店名称"),
            col("region_name", "varchar", "所属大区；枚举：华东大区 / 华北大区 / 华南大区（无其他取值）"),
            col("store_type", "varchar", "门店类型；枚举：直营 / 加盟"),
            col("open_date", "date", "开业日期"),
        ],
    },
    {
        "name": "dws_store_daily_agg",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "dws_store_daily_agg"},
        "primaryKey": "stat_date",
        "columns": [
            col("stat_date", "date", "统计日期（表主键之一）", pk=True),
            col("store_id", "integer", "门店ID（关联 dim_store）"),
            col("sales_amount", "numeric", "当日销售额（元）"),
            col("order_cnt", "integer", "当日订单数"),
            col("member_cnt", "integer", "当日下单会员数"),
            col("area_sqm", "numeric", "门店经营面积（平方米）；口径：坪效=销售额/面积"),
            col("day_type", "varchar", "日期类型；枚举：工作日 / 周末 / 节假日"),
        ],
    },
    {
        "name": "ads_coupon_order_di",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "ads_coupon_order_di"},
        "primaryKey": "coupon_order_id",
        "columns": [
            col("coupon_order_id", "integer", "券核销记录ID", pk=True),
            col("member_id", "integer", "会员ID（关联 dim_member）"),
            col("coupon_type", "varchar", "券种；枚举：满减券 / 折扣券 / 新人券。按券种细分核销属【待上架】口径，暂不可分析"),
            col("coupon_amount", "numeric", "券面额（元）；券成本口径【待上架】，暂不可分析"),
            col("order_gmv", "numeric", "用券订单GMV（元）；核销GMV口径【待上架】，暂不可分析"),
            col("stat_date", "date", "核销日期"),
        ],
    },
    {
        "name": "dim_member",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "dim_member"},
        "primaryKey": "member_id",
        "columns": [
            col("member_id", "integer", "会员ID", pk=True),
            col("member_name", "varchar", "会员姓名"),
            col("grade", "varchar", "会员等级；枚举：普通 / 银卡 / 金卡 / 钻石"),
            col("member_status", "varchar", "会员状态；枚举：活跃 / 沉默 / 流失。口径：复购率分母=活跃用户数（当月有下单），禁止用流失会员做分母"),
            col("register_date", "date", "注册日期"),
            col("store_id", "integer", "归属门店（关联 dim_store）"),
        ],
    },
    {
        "name": "ads_member_grade_change_wi",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "ads_member_grade_change_wi"},
        "primaryKey": "change_id",
        "columns": [
            col("change_id", "integer", "异动记录ID", pk=True),
            col("member_id", "integer", "会员ID（关联 dim_member）"),
            col("change_month", "date", "异动月份（月粒度）"),
            col("change_type", "varchar", "异动类型；枚举：升级 / 降级"),
        ],
    },
    {
        "name": "ads_member_repurchase_di",
        "tableReference": {"catalog": "retail", "schema": "public", "table": "ads_member_repurchase_di"},
        "primaryKey": "member_id",
        "columns": [
            col("member_id", "integer", "会员ID（关联 dim_member；与 stat_month 联合唯一）"),
            col("store_id", "integer", "归属门店（关联 dim_store）"),
            col("stat_month", "date", "统计月份（月粒度，存当月1日）"),
            col("repurchase_flag", "integer", "当月是否复购：0/1；口径：复购用户数=SUM(repurchase_flag)，复购率=复购用户数/活跃用户数（须随附活跃用户数，分母不可悬空）"),
            col("order_cnt", "integer", "当月订单数"),
        ],
    },
]

rels = [
    ("dwd_order_di_dim_member", "dwd_order_di", "dim_member", "dwd_order_di.member_id = dim_member.member_id"),
    ("dwd_order_di_dim_store", "dwd_order_di", "dim_store", "dwd_order_di.store_id = dim_store.store_id"),
    ("order_detail_order", "dwd_order_detail_di", "dwd_order_di", "dwd_order_detail_di.order_id = dwd_order_di.order_id"),
    ("order_detail_store", "dwd_order_detail_di", "dim_store", "dwd_order_detail_di.store_id = dim_store.store_id"),
    ("store_agg_store", "dws_store_daily_agg", "dim_store", "dws_store_daily_agg.store_id = dim_store.store_id"),
    ("coupon_member", "ads_coupon_order_di", "dim_member", "ads_coupon_order_di.member_id = dim_member.member_id"),
    ("member_store", "dim_member", "dim_store", "dim_member.store_id = dim_store.store_id"),
    ("grade_change_member", "ads_member_grade_change_wi", "dim_member", "ads_member_grade_change_wi.member_id = dim_member.member_id"),
    ("repurchase_member", "ads_member_repurchase_di", "dim_member", "ads_member_repurchase_di.member_id = dim_member.member_id"),
    ("repurchase_store", "ads_member_repurchase_di", "dim_store", "ads_member_repurchase_di.store_id = dim_store.store_id"),
]

mdl = {
    "catalog": "wren_catalog",
    "schema": "public",
    "dataSource": "postgres",
    "models": models,
    "relationships": [
        {"name": n, "models": [a, b], "joinType": "MANY_TO_ONE", "condition": c}
        for n, a, b, c in rels
    ],
}

out = WS / "mdl.json"
out.write_text(json.dumps(mdl, ensure_ascii=False, indent=2), encoding="utf-8")
n_cols = sum(len(m["columns"]) for m in models)
print(f"MDL-B 已生成：{out}")
print(f"  models={len(models)} columns={n_cols} relationships={len(rels)}")
