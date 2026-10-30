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
    # 需求原文常写成「2026 年 1 月」（数字与年月之间有空格，业务人员手打就是这样），
    # 原正则 `(\d{4})年(\d{1,2})月` 一个都匹配不到 → 时间条件被静默丢掉，
    # SQL 变成"全量统计"还照常返回，比报错更危险（2026-10-30 实测
    # DR-20261030-GJRN「2026 年 1 月份以来」）。先把年月日两侧空白规整掉再匹配。
    s = re.sub(r"\s*(年|月|日)\s*", r"\1", t or "")
    ms = re.findall(r"(\d{4})年(\d{1,2})月", s)
    return ["%s-%02d-01" % (y, int(m)) for y, m in ms]


def _topn(t):
    """解析「前N名 / 前三名 / top5 / 前 10」→ N；解析不出返回 None。

    ⚠️ 为什么要单独解析：`"前三名"` 若只靠 `re.search(r"前(\\d+)")` 是抓不到的
    （中文数字），而业务方写中文数字比写阿拉伯数字更常见。
    ⇒ 两种都认，**并且必须真的把 N 用在 LIMIT 上**：
      只排序不限行等于"排名全榜单"，跟"前三名"不是一回事。
    """
    m = re.search(r"(?:前|top)\s*(\d+)", t, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:  # noqa: BLE001
            return None
    _zh = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.search(r"前([一二三四五六七八九十])名", t)
    if m:
        return _zh.get(m.group(1))
    return None


def _store_metric_combo(dataset, t):
    """门店维度的**跨指标组合**：会员数 + 销售额/订单量，出在同一行。

    为什么单独做（2026-10-23 基线实测，题集 B-29）
    ----------------------------------------------
    `plan_b` 是"命中一个意图就 return"的线性结构 ⇒ 「各门店会员数和销售额」只会
    出会员数，销售额整段丢掉，**而且不报错**。危害不是"少一点信息"，
    是"业务方以为这张表就是全部"——少给一列比多给一列难发现得多。

    做法：每个指标各算一个**按门店聚合的子查询**，再按门店 LEFT JOIN 对齐。
      · 用 LEFT JOIN 而非 INNER：某店没有会员或没有订单时仍要出现在结果里，
        否则"最差的那个店"会凭空消失，而那恰恰是最需要看的一行；
      · 缺指标留 NULL，**不补 0**：NULL＝这个店确实没有，0＝算出来是零，
        两者业务含义不同，抹平就是把"没有"谎报成"零"。

    ⚠️ 只在**同时要两样以上、且其中包含「会员数」**时接管：
      销售额 + 订单量的门店组合已有专门分支（且带月度/排名/占比变体），
      这里不抢，免得把那些变体吃掉。宽度换稳定性。
    """
    if "门店" not in t:
        return None
    want_member = ("会员数" in t or "会员人数" in t)
    want_sales = ("销售额" in t or "销售金额" in t)
    want_orders = any(k in t for k in ("订单量", "订单数", "单量"))
    if not want_member or not (want_sales or want_orders):
        return None

    parts = [(
        "会员数",
        "SELECT store_id, COUNT(*) AS 会员数 FROM dim_member GROUP BY store_id",
        "dim_member",
    )]
    if want_sales:
        parts.append((
            "销售额",
            "SELECT store_id, SUM(pay_amount) AS 销售额 FROM dwd_order_di "
            "WHERE order_status = '已完成' GROUP BY store_id",
            "dwd_order_di",
        ))
    if want_orders:
        parts.append((
            "订单数",
            "SELECT store_id, COUNT(*) AS 订单数 FROM dwd_order_di "
            "WHERE order_status = '已完成' GROUP BY store_id",
            "dwd_order_di",
        ))

    sels, joins, tables = ["s.store_name"], [], ["dim_store"]
    for i, (name, sub, tbl) in enumerate(parts):
        al = "x%d" % i
        joins.append("LEFT JOIN (%s) %s ON s.store_id = %s.store_id" % (sub, al, al))
        sels.append("%s.%s" % (al, name))
        tables.append(tbl)

    # ⚠️ 排名意图不能被组合吃掉（2026-10-23 基线实测补）：
    #   「各门店会员数和销售额**排名**」原本得到一张按门店名排的表 ——
    #   排序是有的，只是排在错的列上。这比"没有排序"更难发现：
    #   SQL 里有 ORDER BY、门禁看不出问题、界面上也确实排好了，
    #   只有业务方拿去做"前十名"时才会发现顺序不对。属"说了没做"同族。
    #   排序键取「数值最靠前的那个指标」而非固定某一个：
    #     有销售额按销售额（业务默认的主指标）→ 否则订单数 → 否则会员数。
    # 同理「前3名」要真的限行，只排序不限行等于给全榜单（同 B-31 那类）。
    _rank = bool(re.search(
        r"排名|排行|最高|最低|从高到低|从低到高|降序|升序|(?:前|top)\s*\d+",
        t, re.IGNORECASE))
    _limit = _topn(t)
    if _limit is not None:
        _rank = True
    _names = [p[0] for p in parts]
    _order_key = "销售额" if "销售额" in _names else (
        "订单数" if "订单数" in _names else "会员数")
    _order = (" ORDER BY %s DESC NULLS LAST" % _order_key) if _rank else " ORDER BY s.store_name"
    _sql = ("SELECT %s FROM dim_store s %s%s"
            % (", ".join(sels), " ".join(joins), _order))
    if _limit is not None:
        _sql += " LIMIT %d" % _limit

    _rules = ["各指标先按门店各自聚合再对齐（LEFT JOIN）；缺数留 NULL 不补 0"]
    if _rank:
        _rules.append("按「%s」降序排名；NULL（无该指标数据）排最后，不当 0 参与比较"
                      % _order_key)
    return _ok(
        "B",
        "门店组合·" + "＋".join(_names) + ("·排名" if _rank else "")
        + ("·前%d" % _limit if _limit is not None else ""),
        _sql,
        _objects(dataset, tables, rules=_rules),
    )


def _yoy_mom(dataset, t):
    """同比 / 环比：「上期」不是一个行号，而是**日历上的一个月／一年前**。

    为什么不用 ``LAG(SUM(x), 1) OVER (ORDER BY 月份)``
    -------------------------------------------------
    它只在「月份连续无缺口」时才对。库里 2026-07～09 正好连续，两种写法现在算出
    同一个数 —— **看不出区别，恰恰是最危险的时候**：哪天某月缺数（门店停业、
    调度跑挂、口径调整），行号窗口会拿两个月前的数当"上月"、拿 13 个月前的数
    当"去年同期"，**结果照样出、门禁照样过，只是数字是错的**。这与
    REQ-COVERAGE 是同一类病：不报错的错。
    ⇒ 按**日历自连接**（``月份 - INTERVAL '1 month'`` / ``'1 year'``）。
      对不上就留 NULL —— NULL 是"没有可比基期"，拿错月份当基期才是灾难。

    口径（都要落进 SQL 与 rules，不能只写在文档里）
    --------------------------------------------
    · 同比 = 本期 ÷ **去年同期同月** − 1；环比 = 本期 ÷ **上一个月** − 1。
    · 基期为 0 ⇒ 用 ``NULLIF`` 把增长率置 NULL。基数 0 时增长率无定义，
      写 0% 或无穷大都错，**不静默吞成 0**。
    · 基期缺数 ⇒ 增长率 NULL，但**基期金额列照常输出**：业务方看到
      「去年同期销售额 = NULL」立刻明白比率为什么是空，不用回来问我们。
    · 只比可加的「销售额」「订单数」两种；其他指标不接（口径不明，宁可不接）。
    · 全程仅「已完成」订单（与销售额口径一致，排除退款）。

    ⚠️ 只在文本出现「同比 / 环比」且能认出一个可比指标时接管。
    """
    if "同比" not in t and "环比" not in t:
        return None
    metrics = []
    if any(k in t for k in ("销售额", "销售金额", "GMV", "gmv")):
        metrics.append(("销售额", "SUM(o.pay_amount)"))
    if any(k in t for k in ("订单量", "订单数", "单量", "多少单")):
        metrics.append(("订单数", "COUNT(*)"))
    if not metrics:
        return None
    want_yoy = "同比" in t
    want_mom = "环比" in t
    by_store = "门店" in t

    _dim_sel = "s.store_name, " if by_store else ""
    _from = ("FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
             if by_store else "FROM dwd_order_di o ")
    _group = ("s.store_name, date_trunc('month', o.order_date)" if by_store
              else "date_trunc('month', o.order_date)")
    _mcols = ", ".join("%s AS %s" % (expr, name) for name, expr in metrics)
    _cte = ("WITH m AS (SELECT %sdate_trunc('month', o.order_date) AS 月份, %s %s"
            "WHERE o.order_status = '已完成' GROUP BY %s) "
            % (_dim_sel, _mcols, _from, _group))

    _sels = (["m.store_name"] if by_store else []) + ["m.月份"]
    _joins = []
    for name, _expr in metrics:
        _sels.append("m.%s" % name)
    # 基期表与主表是**同一份月度聚合**，只是按日历平移 ⇒ 不需要再扫一遍明细，
    # 也不会因为"另一条口径"导致本期与基期口径不一致。
    if want_yoy:
        _joins.append("LEFT JOIN m y ON %sy.月份 = m.月份 - INTERVAL '1 year'"
                      % ("y.store_name = m.store_name AND " if by_store else ""))
        for name, _expr in metrics:
            _sels.append("y.%s AS 去年同期%s" % (name, name))
            _sels.append(
                "ROUND(CAST((m.%s - y.%s) * 100.0 / NULLIF(y.%s, 0) AS DECIMAL(10,2)), 2) "
                "AS %s同比增长百分比" % (name, name, name, name))
    if want_mom:
        _joins.append("LEFT JOIN m p ON %sp.月份 = m.月份 - INTERVAL '1 month'"
                      % ("p.store_name = m.store_name AND " if by_store else ""))
        for name, _expr in metrics:
            _sels.append("p.%s AS 上月%s" % (name, name))
            _sels.append(
                "ROUND(CAST((m.%s - p.%s) * 100.0 / NULLIF(p.%s, 0) AS DECIMAL(10,2)), 2) "
                "AS %s环比增长百分比" % (name, name, name, name))

    # 默认按时间读趋势（这也是"同比/环比"最常见的读法）。若文本里另有
    # 「排名 / 前N」诉求，则按对应增长率降序 —— 说了排名就得排，不能装没听见。
    _rank = bool(re.search(r"排名|排行|最高|最低|降序|(?:前|top)\s*\d+", t, re.IGNORECASE))
    _rate_col = None
    for _c in _sels:
        if "同比增长百分比" in _c or "环比增长百分比" in _c:
            _rate_col = _c.split(" AS ")[-1].strip()
    if _rank and _rate_col:
        _order = " ORDER BY %s DESC NULLS LAST" % _rate_col
    else:
        # ⚠️ 此串必须自带前导空格：它是 `" ".join(_joins) + _order` 拼上去的，
        #   少了空格会拼出 `…INTERVAL '1 month'ORDER BY …`（语法错误但看着像对的）。
        _order = (" ORDER BY m.月份, m.store_name" if by_store
                  else " ORDER BY m.月份")

    _intent = ("同比" if want_yoy else "") + ("环比" if want_mom else "") \
        + ("·门店" if by_store else "·整体")
    _rules = [
        "同比＝去年同期同月、环比＝上一个月；按日历平移自连接求基期，"
        "不用行号窗口（缺月时行号会拿错月份且不报错）",
        "基期缺数或为 0 时增长率留 NULL，不补 0：NULL＝没有可比基期，0%＝确实没变化",
        "仅统计已完成订单（含退款排除）",
    ]
    return _ok(
        "B",
        _intent,
        _cte + "SELECT " + ", ".join(_sels) + " FROM m " + " ".join(_joins) + _order,
        _objects(dataset, ["dwd_order_di"] + (["dim_store"] if by_store else []),
                 rules=_rules),
    )


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
        # 排序意图（2026-10-23 基线实测补）：「各门店复购率排名」原样只分组不排序，
        # 而"排名"没有排序就等于没有排名。⚠️ 复购率的口径要求"不单独给比率"
        # （分母需业务确认，见上一条），所以这里按**分子**（复购用户数）降序，
        # 不做"比率排名"的暗示。
        _rank_r = bool(re.search(
            r"排名|排行|最高|最低|从高到低|从低到高|降序|升序|(?:前|top)\s*\d+",
            t, re.IGNORECASE))
        if "按门店" in t or "各门店" in t:
            return _ok(
                "B",
                "复购率·门店月" + ("排名" if _rank_r else ""),
                "SELECT s.store_name, r.stat_month, SUM(r.repurchase_flag) AS 复购用户数, "
                "SUM(r.order_cnt) AS 活跃订单量 FROM ads_member_repurchase_di r "
                "JOIN dim_store s ON r.store_id = s.store_id GROUP BY s.store_name, r.stat_month"
                + (" ORDER BY 复购用户数 DESC" if _rank_r else ""),
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
    if "复购" in t:
        # ⚠️ 泛化说法兜底（2026-10-23 基线实测补）：
        #   上面两条只认「复购率」「复购用户数」这两个**精确词**，而业务方最自然的
        #   说法是「会员复购情况」——它一个字都不匹配，于是掉进兜底"未命中已建模意图"
        #   被硬拒。可是 ads_member_repurchase_di 与 repurchase_flag 明明就在库里
        #   ⇒ 这是"明明有数据却说没有"，比算错更伤信任。
        #   ⇒ 未指定比率/用户数时，给**按月复购总览**（复购用户数 + 活跃订单量），
        #     **不替业务方算比率**：比率的分母口径（活跃会员？全部会员？）需要业务确认，
        #     自作主张算出来的比率会被直接拿去汇报。
        return _ok(
            "B",
            "复购情况·按月总览",
            "SELECT r.stat_month, SUM(r.repurchase_flag) AS 复购用户数, "
            "SUM(r.order_cnt) AS 活跃订单量 FROM ads_member_repurchase_di r "
            "GROUP BY r.stat_month ORDER BY r.stat_month",
            _objects(
                dataset,
                ["ads_member_repurchase_di"],
                rules=["不单独给复购率：分母口径需业务确认，本意图只给分子与活跃订单量"],
            ),
        )
    # 同比 / 环比要**排在所有单指标分支之前**：它自带时间口径（必然按月），
    # 一旦被下面的「销售额·门店」分支先命中，「同比」两个字就整段丢失，
    # 返回的是一张普通月度销售额表 —— 少了对比列，界面上看不出来（见 `_yoy_mom`）。
    _yoy = _yoy_mom(dataset, t)
    if _yoy:
        return _yoy
    # 跨指标组合要**排在单指标分支之前**：先命中的赢，而组合需求里
    # 「会员数」那一句会先被下面的单指标分支吃掉（详见函数 docstring）。
    _combo = _store_metric_combo(dataset, t)
    if _combo:
        return _combo
    # ── 新增 / 新注册会员（2026-10-30 补）────────────────────────────────
    # 实测 DR-20261030-GJRN：「统计 2026 年 1 月份以来 所有新注册的会员的数量
    # 及其对应的明细信息」。库里 `dim_member.register_date` 就在语义层里，
    # 但 plan_b 没有这条意图 → 掉进兜底「口径未建模」被硬拒，
    # 业务方看到的是"系统没建这个口径"，而事实是"口径就摆在那没登记"。
    #
    # 三处刻意选择：
    #   ① 明细列**逐个点名**，不用 `SELECT *`：`member_phone` / `id_card` 在 MDL 里是
    #      【未建模：对 AI 不可见】，星号会把它们带出来（红线：敏感字段不进生成结果）。
    #   ② 数量与明细同一次返回：用 `COUNT(*) OVER ()` 挂在明细行上，业务方既看到
    #      逐条明细，也知道总数是多少；分两条 SQL 会让人自己加总，容易口径打架。
    #   ③ 时间用 `>= 月初`（「以来」语义），不给上限——上限写在需求里才加。
    if any(k in t for k in ("新注册", "新增会员", "新增的会员", "注册会员", "新会员")) or (
        "注册" in t and "会员" in t
    ):
        since = m1
        where_sql = "WHERE m.register_date >= '%s'" % since if since else ""
        _member_rule = [
            "口径：新注册会员 = dim_member 中 register_date 落在统计起点之后的会员",
            "敏感字段（会员手机号 / 身份证）未建模，SQL 不引用",
        ]
        if "各门店" in t or "按门店" in t:
            return _ok(
                "B",
                "新增会员数·门店",
                "SELECT s.store_name AS 归属门店, COUNT(*) AS 新增会员数 "
                "FROM dim_member m JOIN dim_store s ON m.store_id = s.store_id %s "
                "GROUP BY s.store_name ORDER BY 新增会员数 DESC" % where_sql,
                _objects(dataset, ["dim_member", "dim_store"], rules=_member_rule),
            )
        want_detail = any(
            k in t for k in ("明细", "基本信息", "信息", "列表", "清单", "逐条", "分别列出")
        )
        if not want_detail:
            return _ok(
                "B",
                "新增会员数",
                "SELECT COUNT(*) AS 新增会员数 FROM dim_member m %s" % where_sql,
                _objects(dataset, ["dim_member"], rules=_member_rule),
            )
        return _ok(
            "B",
            "新增会员明细（含总数）",
            "SELECT m.member_id AS 会员ID, m.member_name AS 会员姓名, m.grade AS 会员等级, "
            "m.member_status AS 会员状态, m.register_date AS 注册日期, "
            "s.store_name AS 归属门店, COUNT(*) OVER () AS 新增会员总数 "
            "FROM dim_member m LEFT JOIN dim_store s ON m.store_id = s.store_id %s "
            "ORDER BY m.register_date, m.member_id" % where_sql,
            _objects(dataset, ["dim_member", "dim_store"], rules=_member_rule),
        )

    if ("会员数" in t or "会员人数" in t) and ("门店" in t):
        return _ok(
            "B",
            "归属门店·会员数",
            "SELECT s.store_name AS 归属门店, COUNT(*) AS 会员数 "
            "FROM dim_member m JOIN dim_store s ON m.store_id = s.store_id GROUP BY s.store_name",
            _objects(dataset, ["dim_member", "dim_store"]),
        )
    if "客单价" in t:
        # ⚠️ 2026-10-23 基线实测补：客单价是**派生指标**（销售额 ÷ 订单数），
        #   却被兜底逻辑当成"物理数据缺失"来报——原文案是
        #   「B 库无「客单价」相关表」。客单价本来就不是一张表，
        #   这句提示会把业务方引到"去找数据源"的错方向（真实情况是：数据齐全，只是没建模）。
        #   与 L-REQ「调用方未传 demand_id」是同一类病：**提示指错方向比不提示更贵**。
        #   口径：仅已完成订单（排除退款），分母用不同订单号，避免一张订单多明细被重复计。
        if "各门店" in t or "按门店" in t or "门店" in t:
            return _ok(
                "B",
                "客单价·门店",
                "SELECT s.store_name, "
                "ROUND(CAST(SUM(o.pay_amount) / COUNT(DISTINCT o.order_id) AS DECIMAL(10,2)), 2) "
                "AS 客单价 FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' GROUP BY s.store_name",
                _objects(
                    dataset,
                    ["dwd_order_di", "dim_store"],
                    rules=["客单价口径：仅已完成订单；分母取不同订单号（COUNT DISTINCT order_id）"],
                ),
            )
        return _ok(
            "B",
            "客单价·总",
            "SELECT ROUND(CAST(SUM(o.pay_amount) / COUNT(DISTINCT o.order_id) AS DECIMAL(10,2)), 2) "
            "AS 客单价 FROM dwd_order_di o WHERE o.order_status = '已完成'",
            _objects(
                dataset,
                ["dwd_order_di"],
                rules=["客单价口径：仅已完成订单；分母取不同订单号（COUNT DISTINCT order_id）"],
            ),
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
        # ⚠️ 月度维度（2026-10-23 基线实测补）：`change_month` 就在表里，
        #   需求说「按月/趋势」时却只按 change_type 分组 ⇒ 时间维度整体丢失。
        #   这类"表里有、需求也要、SQL 不给"的缺口，正是 REQ-COVERAGE 要抓的。
        if any(k in t for k in ("按月", "月度", "每月", "趋势", "各月")):
            return _ok(
                "B",
                "升降级·按月趋势",
                "SELECT change_month AS 月份, change_type AS 异动类型, COUNT(*) AS 数量 "
                "FROM ads_member_grade_change_wi "
                "GROUP BY change_month, change_type ORDER BY 月份, change_type",
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
        # ⚠️ 中位数（2026-10-23 基线实测补）：说「中位数」却给了 SUM，
        #   属于**算错了指标**——比"少给一列"更糟，因为列名还写着"销售额"，
        #   业务方会当成销售额用。中位数与合计是两回事：合计被大店拉高，中位数不受影响。
        #   引擎侧实测：`MEDIAN()` 可用；`PERCENTILE_CONT … WITHIN GROUP` **不支持**
        #   （wren/duckdb 计划阶段报 Invalid function）⇒ 用 MEDIAN()。
        if "中位数" in t or "中位值" in t:
            if "门店" in t:
                return _ok(
                    "B",
                    "销售额中位数·门店",
                    "SELECT s.store_name, MEDIAN(o.pay_amount) AS 销售额中位数 "
                    "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                    "WHERE o.order_status = '已完成' GROUP BY s.store_name",
                    _objects(
                        dataset, ["dwd_order_di", "dim_store"],
                        rules=["中位数口径：仅已完成订单；MEDIAN 不是 SUM，不随大额订单被拉高"],
                    ),
                )
            return _ok(
                "B",
                "销售额中位数·总",
                "SELECT MEDIAN(o.pay_amount) AS 销售额中位数 FROM dwd_order_di o "
                "WHERE o.order_status = '已完成'",
                _objects(dataset, ["dwd_order_di"],
                         rules=["中位数口径：仅已完成订单"]),
            )
        # ⚠️ 日粒度（2026-10-23 基线实测补）：说「每天/按日」却只按门店汇总，
        #   日粒度整体丢失 ⇒ 拿到的是一列总数，趋势、异常日、活动效果全都看不出来。
        #   日期直接用业务日期 order_date（**不是**装载时间 dw_insert_time）。
        _by_day = any(k in t for k in ("每天", "按日", "每日", "逐日", "按天", "日粒度", "各天"))
        if _by_day and "门店" in t:
            return _ok(
                "B",
                "销售额·门店日",
                "SELECT s.store_name, o.order_date AS 日期, SUM(o.pay_amount) AS 销售额 "
                "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' "
                "GROUP BY s.store_name, o.order_date ORDER BY 日期, s.store_name",
                _objects(
                    dataset, ["dwd_order_di", "dim_store"],
                    rules=["日粒度口径：按业务日期 order_date 分组，装载时间不得当业务日期"],
                ),
            )
        # 🔴 分组内取最大（2026-10-23 基线实测补，题集 B-30）：
        #   「各门店销售额最高的月份」原样掉进下面的"排名"分支 ⇒ 产出
        #   「按门店排名」的 SQL，**月份整个丢了** —— 问的是"哪个月最好"，
        #   答的是"哪个店最好"。这比少给一列更危险：**它是给了一个看起来很像的错答案**，
        #   业务方不看列名就会拿去汇报。
        #   正确形态是「每店各自取那个最高的月」（argmax per group）：
        #   先按月×门店算出金额，再用 ROW_NUMBER 在每店内部排名次，最后取名次=1。
        #   ⚠️ 必须排在排名分支**之前**：两者都含「最高」，先命中的赢。
        #   ⚠️ 判据要求同时出现"月"与"最高/最大"：只有「最高」没有月 ⇒ 还是排名；
        #      只有月没有「最高」 ⇒ 还是月度趋势。缺一不可，否则会吃掉别的意图。
        _mth = (bool(_months_in(t)) or any(
            k in t for k in ("按月", "月度", "每月", "分月", "各月", "月份")))
        _top_mth = any(k in t for k in ("最高", "最大", "峰值", "最好", "最多"))
        if _mth and _top_mth and "门店" in t:
            return _ok(
                "B",
                "销售额·每店最高月",
                "SELECT t.store_name, t.月份, t.销售额 FROM ("
                "SELECT s.store_name, date_trunc('month', o.order_date) AS 月份, "
                "SUM(o.pay_amount) AS 销售额, "
                "ROW_NUMBER() OVER (PARTITION BY s.store_name "
                "ORDER BY SUM(o.pay_amount) DESC) AS 名次 "
                "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' "
                "GROUP BY s.store_name, date_trunc('month', o.order_date)"
                ") t WHERE t.名次 = 1 ORDER BY t.销售额 DESC",
                _objects(
                    dataset, ["dwd_order_di", "dim_store"],
                    rules=["每店各取销售额最高的那一个月（组内排名后取第 1 名），"
                           "不是跨店排名；并列时按未定义的顺序取一条"],
                ),
            )
        # 🔴 分组内取最大 · 维度换成品类（2026-10-23 基线实测补）：
        #   「各门店销售额最高的品类」原本掉进排名分支 ⇒ 只按门店汇总，
        #   品类整个丢了。同 B-30 是同一个病：**问 A，答 B，长得还挺像**。
        if "品类" in t and _top_mth and "门店" in t:
            return _ok(
                "B",
                "销售额·每店最高品类",
                "SELECT t.store_name, t.品类, t.销售额 FROM ("
                "SELECT s.store_name, d.category_name AS 品类, "
                "SUM(d.sales_amount) AS 销售额, "
                "ROW_NUMBER() OVER (PARTITION BY s.store_name "
                "ORDER BY SUM(d.sales_amount) DESC) AS 名次 "
                "FROM dwd_order_detail_di d "
                "JOIN dwd_order_di o ON d.order_id = o.order_id "
                "JOIN dim_store s ON d.store_id = s.store_id "
                "WHERE o.order_status = '已完成' "
                "GROUP BY s.store_name, d.category_name"
                ") t WHERE t.名次 = 1 ORDER BY t.销售额 DESC",
                _objects(
                    dataset, ["dwd_order_detail_di", "dwd_order_di", "dim_store"],
                    rules=["每店各取销售额最高的那一个品类（组内排名后取第 1 名），"
                           "不是跨店排名",
                           "仅已完成订单；明细表本身没有订单状态字段，"
                           "必须 JOIN 订单表才能排除退款"],
                ),
            )
        # 排序意图要在分支条件之前算出来：因为「前5名门店销售额」这类说法里
        # **没有「各」**，但"前N名"本身就隐含"逐门店列出来再排"⇒ 必须能进这个分支，
        # 否则会掉进兜底"未命中已建模意图"被硬拒（实测如此）。
        _rank = bool(re.search(
            r"排名|排行|最高|最低|从高到低|从低到高|降序|升序|(?:前|top)\s*\d+",
            t, re.IGNORECASE))
        if "门店" in t and ("各" in t or _rank):
            # ⚠️ 粒度优先（2026-10-23 实测补）：原实现只认「各门店」，不看需求里的
            #   「按月 / 月度 / 每月」⇒ 需求写"按月"，SQL 却只按门店 GROUP BY，
            #   月度要素直接丢失。这类 SQL 以前能过（因为没人比对需求），
            #   REQ-COVERAGE 上线后会被正确判红——**拦是对的，但产品不能只会拦**：
            #   同一句话里既然说了"按月"，就该按月出数。
            #   判据：只要出现按月/月度/每月/分月（或解析出具体年月），就走月度变体。
            _by_month = bool(_months_in(t)) or any(
                k in t for k in ("按月", "月度", "每月", "分月", "各月")
            )
            # ⚠️ 同族缺陷第二次（2026-10-23 基线实测补）：这句话里**说了几样东西，
            #   就要出几样**。原实现写死只出「销售额」，于是
            #     「各门店月度订单量和销售额」→ SQL 里没有订单数（要素被吞），
            #     「各门店销售额排名」        → SQL 里没有 ORDER BY（排名不排序，
            #                                    业务方拿到一堆无序行，"排名"形同虚设）。
            #   这两类都属"需求说了、SQL 没有"，只是过去没人逐项比对需求。
            #   与粒度那条同一个道理：**门禁能拦，但产品不能只会拦**。
            #   ⇒ 判据只认明确的业务说法，避免过度拟合：
            #     · 订单数：订单量 / 订单数 / 单量 / 多少单（「销量」是件数，不混进来）
            #     · 排序：排名 / 排行 / 最高 / 最低 / 从高到低 / 从低到高 / 前N名 / topN
            #       ⚠️ 不认单字「前」（「目前」「之前」会误命中）。
            _need_cnt = any(k in t for k in ("订单量", "订单数", "单量", "多少单"))
            _cnt_sel = ", COUNT(*) AS 订单数" if _need_cnt else ""
            # ⚠️ 同族缺陷第三次（2026-10-23 基线实测补）：
            #   「各门店销售额**占比**」原样返回金额，占比一个字都没有 ——
            #   需求要的是"每天门店占全店多少"，拿到的是一堆绝对数，
            #   业务方只能自己拿计算器除。属"说了没做"，与排名/订单量同一族。
            #   口径：占比 = 本店金额 ÷ 同范围合计，用窗口函数求分母（不额外查一遍库）。
            #     · 不带月度 ⇒ 分母＝全部门店合计；
            #     · 带月度   ⇒ 分母＝**同月**门店合计（跨月比较没有意义）。
            _need_share = any(k in t for k in ("占比", "比重", "份额", "占总"))
            _share_month = (", ROUND(CAST(SUM(o.pay_amount) * 100.0 / "
                            "SUM(SUM(o.pay_amount)) OVER "
                            "(PARTITION BY date_trunc('month', o.order_date)) "
                            "AS DECIMAL(10,2)), 2) AS 占比百分比"
                            if (_need_share and _by_month) else "")
            _share_plain = (", ROUND(CAST(SUM(o.pay_amount) * 100.0 / "
                            "SUM(SUM(o.pay_amount)) OVER () "
                            "AS DECIMAL(10,2)), 2) AS 占比百分比"
                            if (_need_share and not _by_month) else "")
            _suffix = ("排名" if _rank else "") + ("·含订单数" if _need_cnt else "") \
                + ("·含占比" if _need_share else "")
            # ⚠️ 同族缺陷第四次（2026-10-23 基线实测补）：
            #   「各门店销售额**前三名**」原本只排序、**不限行** ⇒ 返回的是全榜单。
            #   "前三名"和"排名"不是一回事：前者要 3 行，后者要全部行。只排序不限行
            #   等于把"前三名"悄悄变成"全部排名"，业务方要贴进汇报时会直接贴全表。
            _limit = _topn(t)
            if _limit is not None:
                _rank = True
                _suffix += "·前%d" % _limit
            if _by_month:
                _order = ("月份, 销售额 DESC" if _rank else "月份, s.store_name")
                if _limit is not None:
                    # 带月度又要"前N名"⇒ 只能是**每月各取前N**（全期共 N 行没有业务含义）。
                    return _ok(
                        "B",
                        "销售额·门店月" + _suffix,
                        "SELECT t.store_name, t.月份, t.销售额 FROM ("
                        "SELECT s.store_name, date_trunc('month', o.order_date) AS 月份, "
                        "SUM(o.pay_amount) AS 销售额, "
                        "ROW_NUMBER() OVER (PARTITION BY date_trunc('month', o.order_date) "
                        "ORDER BY SUM(o.pay_amount) DESC) AS 名次 "
                        "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                        "WHERE o.order_status = '已完成' "
                        "GROUP BY s.store_name, date_trunc('month', o.order_date)"
                        ") t WHERE t.名次 <= %d ORDER BY t.月份, t.名次" % _limit,
                        _objects(
                            dataset, ["dwd_order_di", "dim_store"],
                            rules=["每月各取销售额前 %d 名的门店（月内排名），不是全期前 %d 行"
                                   % (_limit, _limit)],
                        ),
                    )
                return _ok(
                    "B",
                    "销售额·门店月" + _suffix,
                    "SELECT s.store_name, date_trunc('month', o.order_date) AS 月份, "
                    "SUM(o.pay_amount) AS 销售额" + _cnt_sel + _share_month + " "
                    "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                    "WHERE o.order_status = '已完成' "
                    "GROUP BY s.store_name, date_trunc('month', o.order_date) "
                    "ORDER BY " + _order,
                    _objects(
                        dataset, ["dwd_order_di", "dim_store"],
                        rules=["占比口径：分母为同月各门店合计（窗口函数），非全期合计"],
                    ) if _need_share else _objects(dataset, ["dwd_order_di", "dim_store"]),
                )
            return _ok(
                "B",
                "销售额·门店" + _suffix,
                "SELECT s.store_name, SUM(o.pay_amount) AS 销售额" + _cnt_sel + _share_plain + " "
                "FROM dwd_order_di o JOIN dim_store s ON o.store_id = s.store_id "
                "WHERE o.order_status = '已完成' GROUP BY s.store_name"
                + (" ORDER BY 销售额 DESC" if _rank else "")
                + (" LIMIT %d" % _limit if _limit is not None else ""),
                _objects(
                    dataset, ["dwd_order_di", "dim_store"],
                    rules=["占比口径：分母为全部门店合计（窗口函数）"],
                ) if _need_share else _objects(dataset, ["dwd_order_di", "dim_store"]),
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
        # 同族：需求说「排名/最高/最低」就要排序，否则"排名"只是列出来（2026-10-23 基线实测）。
        _rank_cat = bool(re.search(
            r"排名|排行|最高|最低|从高到低|从低到高|降序|升序|(?:前|top)\s*\d+",
            t, re.IGNORECASE))
        # 同族：说「占比/比重/份额」就要算出占比（2026-10-23 基线实测补）。
        # 口径：品类金额 ÷ 全部品类合计，窗口函数求分母。
        _share_cat = any(k in t for k in ("占比", "比重", "份额", "占总"))
        # 🔴 必须排除退款（2026-10-23 人工验收准备时实测发现的真缺陷）：
        #   原来写 `FROM dwd_order_detail_di GROUP BY category_name`，**没有任何订单状态过滤**
        #   ⇒ 品类销售额把已退款订单也算进去了（实测 10300.4），而门店销售额排除了退款
        #   （实测 9644.5）。同一个"销售额"，两个维度两个口径，差 655.9 ——
        #   业务方一核对就发现对不上，而且**从系统里找不到任何解释**（因为它自己不自洽）。
        #   明细表本身**没有 order_status 字段**，所以只能 JOIN 订单表过滤；这不是"可以加
        #   也可以不加"的优化，是口径正确性的前提。
        #   修完：品类合计 9702.5，与订单实付 9644.5 只差 58.0 —— 那是"原价 vs 实付"
        #   的优惠折扣差，属于**可解释**的正常差异，而不是把退款算进来的错误。
        return _ok(
            "B",
            "品类销售额" + ("排名" if _rank_cat else "") + ("·含占比" if _share_cat else ""),
            "SELECT d.category_name, SUM(d.sales_amount) AS 销售额"
            + (", ROUND(CAST(SUM(d.sales_amount) * 100.0 / SUM(SUM(d.sales_amount)) OVER () "
               "AS DECIMAL(10,2)), 2) AS 占比百分比" if _share_cat else "") + " "
            "FROM dwd_order_detail_di d "
            "JOIN dwd_order_di o ON d.order_id = o.order_id "
            "WHERE o.order_status = '已完成' "
            "GROUP BY d.category_name"
            + (" ORDER BY 销售额 DESC" if _rank_cat else ""),
            _objects(
                dataset, ["dwd_order_detail_di", "dwd_order_di"],
                rules=(["占比口径：分母为全部品类合计（窗口函数，已排除退款）"]
                       if _share_cat else
                       ["仅已完成订单；明细表无订单状态字段，必须 JOIN 订单表排除退款"]),
            ),
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
        "请按以下顺序补齐后重试（业务方只需做①，②由技术方完成）：\n"
        "① 业务口径 → 在知识库《01-指标口径说明书·零售会员域》补充该口径定义"
        "（含计算公式、统计范围、排除项、并列排序规则），"
        "由业务方确认口径、技术方落文档；\n"
        "② 语义登记 → 技术方在语义层登记该意图（实现位置 "
        "gateway/planner.py 的 plan_b，参照同文件既有意图写法，"
        "SQL 只引用 MDL 可见对象）——**这一步不是业务方能自助完成的**，"
        "请把本条差距转给技术方排期；\n"
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
    if "新注册" in t or "新增会员" in t or "新会员" in t or "注册" in t:
        names += ["新增会员数", "新增会员明细（含总数）", "新增会员数·门店"]
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
        # ⚠️ 这里曾有「客单价」条目，2026-10-23 移除：客单价是**派生指标**
        #   （销售额 ÷ 订单数），不是一张表 ⇒ 把它判成「数据缺失」是错的，
        #   会把业务方引去找数据源。已改为真实意图（见 plan_b 的「客单价」分支）。
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
