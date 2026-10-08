# -*- coding: utf-8 -*-
"""确定性 NL→SQL 规划器（封闭世界兜底组件）

定位
----
按《实施推进与验收方案》§12 风险表「LLM 输出不确定」条款落地：**保留确定性 plan()
作封闭世界保底**。当 LLM 路径不可用或结论置信度不足时，网关退回到本规划器——
它只引用 MDL 可见对象，未命中任何已建模意图即拒绝生成，不猜测、不编造。

来源
----
- A 库意图库：迁移自 `demo-server.py` 的实测规划器（13 正向 + 3 拒绝分支）
- B 库意图库：迁移自 `poc-eval/poc_eval.py` 的受控生成器（POC-1/3 回放同源）

两套意图库与 P0 题库 bank-v1 一一对应，迁移后逻辑与判定口径保持不变。
"""
import re


def _ok(dataset_key, intent, sql, objects, steps_extra=None):
    steps = [
        "业务语义分析：命中意图「%s」" % intent,
        "MDL 语义对齐：引用 %d 个语义层对象" % len(objects),
        "用户确认语义理解无误",
        "经 MDL dry-run 门禁校验",
        "Wren query 真实执行（只读）",
    ]
    if steps_extra:
        steps = steps_extra
    return {
        "dataset": dataset_key,
        "engine": "deterministic-planner",
        "blocked": False,
        "intent": intent,
        "sql": sql,
        "objects": objects,
        "reason": "",
        "steps": steps,
    }


def _refuse(dataset_key, concept, reason, guide):
    return {
        "dataset": dataset_key,
        "engine": "deterministic-planner",
        "blocked": True,
        "intent": "硬门禁拒绝：「%s」" % concept,
        "sql": "",
        "objects": [],
        "reason": reason,
        "guide": guide,
        "steps": [
            "业务语义分析：识别到未建模概念「%s」" % concept,
            "硬门禁：未在语义层（MDL）命中 → 禁止生成 SQL",
            "治理指引：%s" % guide,
        ],
    }


def _objects(dataset, models, columns=None, enums=None, rules=None):
    objs = []
    for mn in models:
        m = dataset.models.get(mn)
        objs.append(
            {
                "type": "model",
                "name": mn,
                "detail": (m or {}).get("tableReference", {}).get("table", "") if m else "",
            }
        )
    for col in columns or []:
        objs.append({"type": "column", "name": col, "detail": ""})
    for en in enums or []:
        objs.append({"type": "enum", "name": en["name"], "detail": en["value"]})
    for ru in rules or []:
        objs.append({"type": "rule", "name": ru, "detail": ""})
    return objs


# ---------------------------------------------------------------------------
# A 库 · 电商域意图库
# ---------------------------------------------------------------------------
def plan_a(dataset, nl):
    t = nl.strip()
    if re.match(r"^\s*(select|with)\b", t, re.I):
        return _ok(
            "A",
            "用户直接编写 SQL（经语义层校验执行）",
            t,
            [],
            steps_extra=[
                "识别为原始 SQL，跳过意图规划",
                "经 MDL dry-run 门禁校验",
                "Wren query 真实执行（只读）",
            ],
        )

    tl = t.lower()
    if any(k in t for k in ["手机号", "电话", "手机", "邮箱", "邮件", "身份证", "证件号"]):
        return _refuse(
            "A", "客户联系方式",
            "手机号/邮箱/身份证等敏感字段未纳入语义层建模，对 AI 不可见，禁止生成与明文输出",
            "先于「语义层 · MDL 字典」补充建模 / 准入口径后再运行",
        )
    if any(k in t for k in ["情绪指数", "满意度", "评分", "好评", "NPS", "情绪"]):
        return _refuse(
            "A", "情绪指数",
            "「情绪指数」在语义层（MDL）中无任何已建模口径，禁止猜测生成",
            "先于「语义层 · MDL 字典」补充建模 / 准入口径后再运行",
        )
    if any(k in t for k in ["利润", "毛利率", "成本"]):
        return _refuse(
            "A", "利润/成本",
            "利润、毛利率、成本等口径尚未在语义层建模，禁止猜测生成",
            "先于「语义层 · MDL 字典」补充建模 / 准入口径后再运行",
        )

    if any(k in t for k in ["品类", "类目", "大类", "类"]) and any(
        k in t for k in ["销售", "销售额", "营收", "卖"]
    ):
        sql = (
            "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
            "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
            "GROUP BY p.category_name ORDER BY 销售额 DESC"
        )
        return _ok(
            "A",
            "各品类销售额（事实表 order_items × 维度退化列 products.category_name）",
            sql,
            _objects(
                dataset,
                ["order_items", "products"],
                columns=["order_items.subtotal", "products.category_name"],
                rules=["维度退化：products.category_name 免 JOIN categories"],
            ),
        )
    if ("已完成" in t and any(k in t for k in ["订单", "金额", "总额", "成交"])) or (
        "完成" in t and "金额" in t
    ):
        sql = "SELECT COUNT(*) AS 已完成订单数, SUM(amount) AS 总金额 FROM orders WHERE status = 3"
        return _ok(
            "A",
            "已完成订单金额（orders.status 枚举：3=已完成）",
            sql,
            _objects(
                dataset,
                ["orders"],
                columns=["orders.status", "orders.amount"],
                enums=[{"name": "orders.status=3", "value": "已完成"}],
                rules=["枚举口径：status=3 即已完成（MDL 字典定义）"],
            ),
        )
    if "退款率" in t or ("退款" in t and "率" in t):
        sql = (
            "SELECT ROUND(100.0 * COUNT(r.refund_id) / COUNT(o.order_id), 1) AS 退款率百分比 "
            "FROM orders o LEFT JOIN refunds r ON r.order_id = o.order_id"
        )
        return _ok(
            "A",
            "退款率（退款订单数 / 总订单数）",
            sql,
            _objects(dataset, ["orders", "refunds"], columns=["orders.order_id", "refunds.refund_id"]),
        )
    if "退款原因" in t:
        sql = (
            "SELECT reason AS 退款原因, COUNT(*) AS 笔数, SUM(refund_amount) AS 退款金额 "
            "FROM refunds GROUP BY reason ORDER BY 笔数 DESC"
        )
        return _ok(
            "A",
            "退款原因分布（refunds 按原因聚合）",
            sql,
            _objects(dataset, ["refunds"], columns=["refunds.reason", "refunds.refund_amount"]),
        )
    if any(k in t for k in ["退款", "退单"]):
        sql = (
            "SELECT r.refund_id AS 退款ID, o.order_id AS 订单号, o.status AS 订单状态码, "
            "o.order_date AS 下单日期, r.refund_amount AS 退款金额, r.reason AS 原因 "
            "FROM refunds r JOIN orders o ON r.order_id = o.order_id"
        )
        return _ok(
            "A",
            "退款明细（refunds 事实表 × orders，含订单状态码）",
            sql,
            _objects(
                dataset,
                ["orders", "refunds"],
                columns=["refunds.refund_amount", "refunds.reason", "orders.status"],
                enums=[{"name": "orders.status", "value": "1已下单/2已发货/3已完成/4已退款"}],
                rules=["枚举口径由 MDL 字典定义；refunds 为退款事实表"],
            ),
        )
    if "客户" in t and any(k in tl for k in ["排行", "top", "消费金额"]):
        sql = (
            "SELECT c.first_name || ' ' || c.last_name AS 客户, c.segment AS 客户类型, "
            "SUM(oi.subtotal) AS 消费额 "
            "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
            "JOIN order_items oi ON oi.order_id = o.order_id "
            "GROUP BY c.first_name, c.last_name, c.segment ORDER BY 消费额 DESC"
        )
        return _ok(
            "A",
            "客户消费排行（customers × orders × order_items）",
            sql,
            _objects(
                dataset,
                ["customers", "orders", "order_items"],
                columns=[
                    "customers.first_name",
                    "customers.last_name",
                    "customers.segment",
                    "order_items.subtotal",
                ],
            ),
        )
    if any(k in t for k in ["客户类型", "分群", "消费者", "公司", "客户群", "按客户"]) and any(
        k in t for k in ["gmv", "销售额", "成交", "下单量", "销量"]
    ):
        sql = (
            "SELECT c.segment AS 客户类型, COUNT(DISTINCT o.order_id) AS 下单量, SUM(oi.subtotal) AS GMV "
            "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
            "JOIN order_items oi ON oi.order_id = o.order_id GROUP BY c.segment"
        )
        return _ok(
            "A",
            "客户分群 GMV（customers × orders × order_items）",
            sql,
            _objects(
                dataset,
                ["customers", "orders", "order_items"],
                columns=["customers.segment", "orders.order_id", "order_items.subtotal"],
            ),
        )
    if any(k in t for k in ["库存", "存货", "备货"]) and any(
        k in t for k in ["品类", "大类", "类目", "类", "sku", "商品"]
    ):
        sql = (
            "SELECT p.category_name AS 大类, SUM(p.stock) AS 总库存, COUNT(*) AS SKU数量 "
            "FROM products p GROUP BY p.category_name ORDER BY 总库存 DESC"
        )
        return _ok(
            "A",
            "各大类库存（products 维度退化列 category_name）",
            sql,
            _objects(dataset, ["products"], columns=["products.stock", "products.category_name"]),
        )
    if any(k in t for k in ["客单价", "平均订单", "平均金额", "均价"]):
        sql = "SELECT COUNT(*) AS 订单数, ROUND(AVG(amount), 2) AS 客单价 FROM orders WHERE status = 3"
        return _ok(
            "A",
            "客单价（已完成订单平均金额）",
            sql,
            _objects(
                dataset,
                ["orders"],
                columns=["orders.amount", "orders.status"],
                enums=[{"name": "orders.status=3", "value": "已完成"}],
            ),
        )
    if any(k in t for k in ["状态分布", "状态", "各状态", "订单状态"]):
        sql = "SELECT status AS 状态码, COUNT(*) AS 订单数 FROM orders GROUP BY status ORDER BY status"
        return _ok(
            "A",
            "订单状态分布（枚举：1已下单/2已发货/3已完成/4已退款）",
            sql,
            _objects(
                dataset,
                ["orders"],
                columns=["orders.status"],
                enums=[{"name": "orders.status", "value": "1已下单/2已发货/3已完成/4已退款"}],
            ),
        )
    if "连带" in t:
        sql = (
            "SELECT ROUND(1.0 * SUM(quantity) / COUNT(DISTINCT order_id), 2) AS 连带率件每单 "
            "FROM order_items"
        )
        return _ok(
            "A",
            "连带率（销售件数 / 下单订单数）",
            sql,
            _objects(dataset, ["order_items"], columns=["order_items.quantity", "order_items.order_id"]),
        )
    if any(k in t for k in ["趋势", "按日", "每天", "每日"]):
        sql = (
            "SELECT order_date AS 日期, COUNT(*) AS 订单数, SUM(amount) AS 销售金额 "
            "FROM orders GROUP BY order_date ORDER BY order_date"
        )
        return _ok(
            "A",
            "订单趋势（按下单日期聚合）",
            sql,
            _objects(dataset, ["orders"], columns=["orders.order_date", "orders.amount"]),
        )
    if any(k in t for k in ["畅销", "热销", "最卖", "top", "销量", "卖得", "商品排行"]):
        sql = (
            "SELECT p.product_name AS 商品, SUM(oi.quantity) AS 销量, SUM(oi.subtotal) AS 销售额 "
            "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
            "GROUP BY p.product_name ORDER BY 销售额 DESC"
        )
        return _ok(
            "A",
            "商品销量与销售额排行（order_items × products）",
            sql,
            _objects(
                dataset,
                ["order_items", "products"],
                columns=["order_items.quantity", "order_items.subtotal", "products.product_name"],
            ),
        )

    return _refuse(
        "A",
        "未命中已建模式",
        "未能在语义层（MDL）匹配到对应已建模对象，禁止猜测生成",
        "先在语义层补充建模，或参考已支持的示例",
    )


# ---------------------------------------------------------------------------
# B 库 · 零售会员域意图库
# ---------------------------------------------------------------------------
NEG_SENS = [
    ("手机号", "会员手机号为敏感字段（建表未建模，对 AI 不可见）"),
    ("身份证", "会员身份证号为敏感字段（建表未建模，对 AI 不可见）"),
    ("证件号", "会员证件号为敏感字段（建表未建模，对 AI 不可见）"),
]
NEG_UNDEFINED = [
    ("情绪指数", "「情绪指数」未定义口径，未建模"),
    ("核销率", "「核销率」未定义口径，未建模"),
    ("收货地址", "「收货地址」未建模"),
    ("装载时间", "装载时间为技术字段（非业务日期），不建模；业务日期请使用 order_date"),
]
NEG_PENDING = [
    ("券成本", "「券成本」口径待上架（D3），未经准入不可分析"),
    ("核销GMV", "「核销GMV」口径待上架（D3），未经准入不可分析"),
    ("核销 gmv", "「核销GMV」口径待上架（D3），未经准入不可分析"),
]


def _months_in(t):
    ms = re.findall(r"(\d{4})年(\d{1,2})月", t)
    return ["%s-%02d-01" % (y, int(m)) for y, m in ms]


def plan_b(dataset, nl):
    t = nl.strip()
    for kw, why in NEG_SENS:
        if kw in t:
            return _refuse("B", "敏感字段拦截：%s" % kw, why, "如需该字段分析，走数据治理与脱敏审批流程")
    for kw, why in NEG_UNDEFINED:
        if kw in t:
            return _refuse("B", "未定义/未建模拦截：%s" % kw, why, "先在语义层补充建模，再运行")
    for kw, why in NEG_PENDING:
        if kw in t:
            return _refuse("B", "待上架口径拦截：%s" % kw, why, "该口径待准入（D3），准入后上架再运行")
    if ("订单" in t or "用券" in t) and ("券" in t) and any(
        k in t for k in ["关联", "join", "JOIN", "连接", "结合起来"]
    ):
        return _refuse(
            "B",
            "跨域无关系拦截：订单表与券核销表之间无建模关系路径",
            "订单表与券核销表之间无建模关系路径",
            "如需用券订单分析，请先补充口径与关系建模（知识治理）",
        )
    if "复购" in t and any(k in t for k in ["按日", "每日", "每天"]):
        return _refuse(
            "B",
            "颗粒度不符：复购汇总为月粒度表，不支持按日分析",
            "复购汇总为月粒度表，不支持按日分析",
            "如需日粒度复购，请先在源头建日粒度表并建模",
        )

    ms = _months_in(t)
    m1 = ms[0] if ms else None

    # ── 会员活跃度与购买频次（2026-10-07 依知识库第九节补齐）────────────────
    # 口径来源：knowledge/01-指标口径说明书·零售会员域.md 第九节「会员活跃度与购买频次」
    #（业务方 2026-10-07 补充，与 _backfill 回填、P2 证据链同源）。
    # 之前只有门店维度意图（复购用户数/复购率/坪效/销量），会员维度未建模 → 需求被「未命中即拒」。
    #
    # 三条口径约束（第九节 9.1 / 9.2 / 9.4）：
    #   ① 只统计 order_status='已完成'，不含已退款（退款在退款月单独计，不冲减原月）
    #   ② 活跃度按下单频次（订单笔数）降序；并列依次比订单总量 → member_id，保证可复现
    #   ③ 「使用频次」类行为指标本数据域无埋点，**不得用购买频次替代**
    _MEMBER_ACT_KEYS = ("活跃客户", "最活跃", "会员活跃", "活跃会员", "购买频次", "下单频次", "订单频次")
    if any(k in t for k in _MEMBER_ACT_KEYS):
        if any(k in t for k in ("使用频次", "访问频次", "浏览", "停留", "登录")):
            # 行为类指标无数据源 → 如实拒绝，不拿购买频次顶替
            return _refuse(
                "B",
                "未建模拦截：零售会员域无行为类数据",
                "本数据域没有登录/浏览/操作行为埋点，「使用频次/访问频次/停留时长」不可计算",
                "如需该指标，请先接入行为数据源并在语义层建模；"
                "若只想看购买频次，请把口径改为「购买频次/下单频次」",
            )
        _act_cols = "COUNT(DISTINCT o.order_id) AS 购买频次, COUNT(DISTINCT o.order_date) AS 下单天数, SUM(o.pay_amount) AS 订单总量"
        if "各门店" in t or "按门店" in t:
            return _ok(
                "B",
                "活跃会员·门店月（购买频次口径）",
                "SELECT s.store_name, m.member_name AS 客户, %s "
                "FROM dwd_order_di o JOIN dim_member m ON o.member_id = m.member_id "
                "JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' "
                "GROUP BY s.store_name, m.member_name "
                "ORDER BY 购买频次 DESC, 订单总量 DESC, m.member_name ASC" % _act_cols,
                _objects(dataset, ["dwd_order_di", "dim_member", "dim_store"]),
                steps_extra=[
                    "口径：只统计已完成订单（order_status='已完成'）",
                    "排序：购买频次降序 → 订单总量降序 → member_name 升序（可复现）",
                    "来源：知识库第九节「会员活跃度与购买频次」9.1/9.2",
                ],
            )
        return _ok(
            "B",
            "活跃会员排行（购买频次口径）",
            "SELECT m.member_name AS 客户, %s "
            "FROM dwd_order_di o JOIN dim_member m ON o.member_id = m.member_id "
            "WHERE o.order_status = '已完成' "
            "GROUP BY m.member_name "
            "ORDER BY 购买频次 DESC, 订单总量 DESC, m.member_name ASC" % _act_cols,
            _objects(dataset, ["dwd_order_di", "dim_member"]),
            steps_extra=[
                "口径：只统计已完成订单（order_status='已完成'）",
                "排序：购买频次降序 → 订单总量降序 → member_name 升序（可复现）",
                "来源：知识库第九节「会员活跃度与购买频次」9.1/9.2",
            ],
        )

    if "复购用户数" in t:
        if "按门店" in t or "各门店" in t:
            return _ok(
                "B",
                "复购用户数·门店月",
                "SELECT s.store_name, r.stat_month, SUM(r.repurchase_flag) AS 复购用户数 "
                "FROM ads_member_repurchase_di r JOIN dim_store s ON r.store_id = s.store_id "
                "GROUP BY s.store_name, r.stat_month",
                _objects(dataset, ["ads_member_repurchase_di", "dim_store"]),
            )
        if m1:
            return _ok(
                "B",
                "复购用户数·总月",
                "SELECT SUM(repurchase_flag) AS 复购用户数 FROM ads_member_repurchase_di "
                "WHERE stat_month = '%s'" % m1,
                _objects(dataset, ["ads_member_repurchase_di"]),
            )
    if "复购率" in t:
        if "按门店" in t or "各门店" in t:
            return _ok(
                "B",
                "复购率·门店月",
                "SELECT s.store_name, r.stat_month, SUM(r.repurchase_flag) AS 复购用户数, "
                "SUM(r.order_cnt) AS 活跃订单量 FROM ads_member_repurchase_di r "
                "JOIN dim_store s ON r.store_id = s.store_id GROUP BY s.store_name, r.stat_month",
                _objects(dataset, ["ads_member_repurchase_di", "dim_store"]),
            )
        return _ok(
            "B",
            "复购率·总（含分母）",
            "SELECT ROUND(CAST(100.0 * a.c / b.m AS DECIMAL(10,1)), 1) AS 复购率百分比, "
            "a.c AS 复购用户数, b.m AS 活跃会员数 "
            "FROM (SELECT SUM(repurchase_flag) AS c FROM ads_member_repurchase_di) a, "
            "(SELECT COUNT(DISTINCT member_id) AS m FROM dwd_order_di) b",
            _objects(
                dataset,
                ["ads_member_repurchase_di", "dwd_order_di"],
                rules=["复购率必须随附分母（活跃会员数），不单独给比率"],
            ),
        )
    if ("会员数" in t or "会员人数" in t) and ("门店" in t):
        return _ok(
            "B",
            "归属门店·会员数",
            "SELECT s.store_name AS 归属门店, COUNT(*) AS 会员数 "
            "FROM dim_member m JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name",
            _objects(dataset, ["dim_member", "dim_store"]),
        )
    if "活跃会员数" in t or ("活跃" in t and "会员" in t):
        if m1:
            y, mo = int(m1[:4]), int(m1[5:7])
            ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
            return _ok(
                "B",
                "活跃会员数",
                "SELECT COUNT(DISTINCT member_id) AS 活跃会员数 FROM dwd_order_di "
                "WHERE order_date >= '%s' AND order_date < '%04d-%02d-01'" % (m1, ny, nm),
                _objects(dataset, ["dwd_order_di"], rules=["活跃口径：区间内有下单的不同会员"]),
            )
    if any(k in t for k in ["升级", "降级", "升降级", "等级异动"]):
        if "门店" in t:
            return _ok(
                "B",
                "升降级·门店",
                "SELECT s.store_name, g.change_type, COUNT(*) AS 数量 "
                "FROM ads_member_grade_change_wi g JOIN dim_member m ON g.member_id = m.member_id "
                "JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name, g.change_type",
                _objects(dataset, ["ads_member_grade_change_wi", "dim_member", "dim_store"]),
            )
        if m1:
            y, mo = int(m1[:4]), int(m1[5:7])
            ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
            return _ok(
                "B",
                "升级人数·月",
                "SELECT COUNT(*) AS 升级人数 FROM ads_member_grade_change_wi "
                "WHERE change_type = '升级' AND change_month >= '%s' AND change_month < '%04d-%02d-01'"
                % (m1, ny, nm),
                _objects(dataset, ["ads_member_grade_change_wi"]),
            )
        return _ok(
            "B",
            "升降级分布",
            "SELECT change_type, COUNT(*) AS 数量 FROM ads_member_grade_change_wi GROUP BY change_type",
            _objects(dataset, ["ads_member_grade_change_wi"]),
        )
    if "会员状态" in t:
        return _ok(
            "B",
            "会员状态分布",
            "SELECT member_status AS 会员状态, COUNT(*) AS 人数 FROM dim_member GROUP BY member_status",
            _objects(dataset, ["dim_member"]),
        )
    if "等级" in t and ("会员" in t or "人数" in t):
        return _ok(
            "B",
            "等级分布",
            "SELECT grade AS 会员等级, COUNT(*) AS 人数 FROM dim_member GROUP BY grade",
            _objects(dataset, ["dim_member"]),
        )
    if "坪效" in t:
        return _ok(
            "B",
            "坪效",
            "SELECT s.store_name, ROUND(CAST(SUM(d.sales_amount) / MAX(d.area_sqm) AS DECIMAL(10,2)), 2) AS 坪效 "
            "FROM dws_store_daily_agg d JOIN dim_store s ON d.store_id = s.store_id GROUP BY s.store_name",
            _objects(dataset, ["dws_store_daily_agg", "dim_store"]),
        )
    if "销售额" in t or "销售金额" in t:
        if "大区" in t:
            if "华东" in t and "各" not in t:
                return _ok(
                    "B",
                    "销售额·华东（排除退款）",
                    "SELECT SUM(o.pay_amount) AS 华东销售额 FROM dwd_order_di o "
                    "JOIN dim_store s ON o.store_id = s.store_id "
                    "WHERE s.region_name = '华东大区' AND o.order_status = '已完成'",
                    _objects(
                        dataset,
                        ["dwd_order_di", "dim_store"],
                        rules=["销售额口径：仅 order_status='已完成'，排除退款"],
                    ),
                )
            return _ok(
                "B",
                "销售额·各大区",
                "SELECT s.region_name AS 大区, SUM(o.pay_amount) AS 销售额 "
                "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' GROUP BY s.region_name",
                _objects(
                    dataset,
                    ["dwd_order_di", "dim_store"],
                    rules=["销售额口径：仅 order_status='已完成'，排除退款"],
                ),
            )
        if "日均" in t:
            return _ok(
                "B",
                "日均销售额",
                "SELECT s.store_name, ROUND(AVG(d.sales_amount), 2) AS 日均销售额 "
                "FROM dws_store_daily_agg d JOIN dim_store s ON d.store_id = s.store_id GROUP BY s.store_name",
                _objects(dataset, ["dws_store_daily_agg", "dim_store"]),
            )
        if "门店" in t and "各" in t:
            return _ok(
                "B",
                "销售额·门店",
                "SELECT s.store_name, SUM(o.pay_amount) AS 销售额 "
                "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' GROUP BY s.store_name",
                _objects(dataset, ["dwd_order_di", "dim_store"]),
            )
        if "工作日" in t or "周末" in t or "日型" in t:
            return _ok(
                "B",
                "销售额·日型",
                "SELECT day_type, SUM(sales_amount) AS 销售额 FROM dws_store_daily_agg GROUP BY day_type",
                _objects(dataset, ["dws_store_daily_agg"]),
            )
        if "每月" in t or "趋势" in t or "按月" in t:
            return _ok(
                "B",
                "月度趋势",
                "SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, "
                "SUM(pay_amount) AS 销售金额 FROM dwd_order_di WHERE order_status = '已完成' "
                "GROUP BY date_trunc('month', order_date) ORDER BY 月份",
                _objects(dataset, ["dwd_order_di"]),
            )
    if "品类" in t:
        return _ok(
            "B",
            "品类销售额",
            "SELECT category_name, SUM(sales_amount) AS 销售额 FROM dwd_order_detail_di GROUP BY category_name",
            _objects(dataset, ["dwd_order_detail_di"]),
        )
    if ("每月" in t or "趋势" in t) and ("订单" in t or "销售" in t):
        return _ok(
            "B",
            "月度趋势",
            "SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, "
            "SUM(pay_amount) AS 销售金额 FROM dwd_order_di WHERE order_status = '已完成' "
            "GROUP BY date_trunc('month', order_date) ORDER BY 月份",
            _objects(dataset, ["dwd_order_di"]),
        )
    # ── 未命中：把「缺什么」讲清楚，并给出可执行的补全清单 ──
    # 诉求（用户原话，2026-10-07）：系统应明确提示业务/技术人员补全什么，
    # 而不是让人靠猜或反复调试。本处给出：已支持清单 + 差距分析 + 两条补全路径。
    _supported = _supported_intents_b(t)
    _gap = _explain_gap_b(dataset, t)
    _guide = (
        "请按以下顺序补齐后重试（不需要改代码）：\n"
        "① 业务口径 → 在知识库《01-指标口径说明书·零售会员域》补充该口径定义"
        "（含计算公式、统计范围、排除项、并列排序规则），"
        "由业务方确认口径、技术方落文档；\n"
        "② 语义建模 → 在 gateway/planner.py 的 plan_b 中登记该意图"
        "（参照同文件内既有意图写法，SQL 必须只引用 MDL 可见对象）；\n"
        "③ 补齐后重新点「生成 SQL」，系统会重新对齐。"
    )
    return _refuse(
        "B",
        "未命中已建模意图：%s" % (_gap or "该问题未对齐任何已建模口径"),
        "受控生成约束：问题未对齐任何已建模口径/意图，拒绝生成（未命中即拒）。"
        "已支持：%s。当前差距：%s" % (_supported, _gap),
        _guide,
    )


def _supported_intents_b(t):
    """列出 B 库当前已支持的口径名（给用户一个可对照的清单）。"""
    names = []
    if "复购用户数" in t or "复购率" in t:
        names += ["复购用户数·门店月", "复购用户数·总月", "复购率·门店月"]
    if "坪效" in t:
        names.append("门店坪效")
    if "销售额" in t or "订单金额" in t or "销售金额" in t:
        names.append("销售额·门店/时间")
    if "订单量" in t or "订单数" in t or "销量" in t:
        names.append("订单量·门店/时间")
    if "最活跃" in t or "活跃客户" in t or "购买频次" in t or "下单频次" in t:
        names.append("活跃会员排行（购买频次口径）")
    if "券" in t or "核销" in t:
        names.append("（券核销相关口径「待上架」，当前不可用）")
    return "、".join(names) if names else "（无匹配：当前需求未触及任何已建模口径）"


def _explain_gap_b(dataset, t):
    """说清缺的是什么：数据没有 / 口径未定义 / 字段未标注，三类分开说。"""
    gaps = []
    # 1) 物理数据缺失：B 库没有相关表
    try:
        models = set((dataset.models or {}).keys())
    except Exception:
        models = set()
    kw_table = {
        "库存": ["库存", "stock", "inventory"],
        "仓储": ["仓储", "仓库", "warehouse"],
        "物流": ["物流", "配送", "shipping", "logistics"],
        "商品类目": ["品类", "类目", "category"],
        "客单价": ["客单价", "均价"],
    }
    for human, kws in kw_table.items():
        if any(k in t for k in kws):
            hit = [m for m in models if any(k in m.lower() for k in kws)]
            if not hit:
                gaps.append("**数据缺失**：B 库无「%s」相关表（当前 8 表：%s）"
                            % (human, "、".join(sorted(models)) or "未读到"))
    # 2) 口径存在但未定义
    if not gaps:
        if "分" in t or "层" in t or "分级" in t or "分层" in t:
            gaps.append("**口径未定义**：`member_status`/`grade` 有枚举值，但"
                        "「消费分层」的划分标准未在知识库定义（多少消费额算高价值？）")
        else:
            gaps.append("**口径未建模**：该问题涉及的业务口径尚未在知识库《指标口径说明书》登记"
                        "（需明确：计算公式、统计范围、排除项、并列排序规则）")
    return "；".join(gaps)


_PLANNERS = {"A": plan_a, "B": plan_b}


def plan(dataset, nl):
    """按数据集选择意图库；未注册意图库的数据集明确拒绝（不静默降级）。"""
    if dataset.key not in _PLANNERS:
        return {
            "dataset": dataset.key,
            "engine": "deterministic-planner",
            "blocked": True,
            "intent": "该数据集尚未接入确定性兜底意图库",
            "sql": "",
            "objects": [],
            "reason": "数据集「%s」暂无确定性意图库（M3 随语义分析逐步补齐）" % dataset.key,
            "guide": "改用 LLM 语义分析路径，或先在 gateway/planner.py 补齐意图库",
            "steps": ["确定性兜底不可用 → 交回 LLM 语义分析路径"],
        }
    return _PLANNERS[dataset.key](dataset, nl)


# ---------------------------------------------------------------------------
# 只读执行保护
# ---------------------------------------------------------------------------
_BANNED = [
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "grant", "revoke", "merge", "replace", "call", "exec",
]


def is_readonly(sql):
    """只读门禁：仅放行 SELECT / WITH，拒写操作与多语句。"""
    s = re.sub(r"--.*", "", sql, flags=re.I)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = s.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        return False, "只允许 SELECT / WITH（只读）语句"
    for b in _BANNED:
        if re.search(r"\b%s\b" % b, s):
            return False, "检测到非只读关键字「%s」，已拦截" % b
    if ";" in s.rstrip(";"):
        return False, "检测到多语句提交，已拦截"
    return True, ""
