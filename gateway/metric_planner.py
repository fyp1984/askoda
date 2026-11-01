# -*- coding: utf-8 -*-
"""口径驱动的通用规划器（数据驱动，不再每个库手写一套意图）

为什么要有这一层
----------------
`planner.py` 里的 A/B 意图库是「一个库一段代码」：换库要重写几十种问法组合，
而口径（业务说法怎么算）在代码里和知识库文档里各写一份，改一处不知道另一处。
2026-11-01 接入信贷库（C）时这条路走到头——132 列宽表不可能再手写一遍。

本模块把两者拆开：
  · **口径是数据**：写在 MDL 的 `x_askoda_metrics` 里（由 `db/credit/04_metrics.json`
    登记、`tools/credit_metrics.py` 注入），字段化描述「用哪个视图、怎么算、哪些维度、
    是时点量还是流量、有哪些坑」；
  · **编译是通用的**：命中口径后，时间条件、维度分组、排序取前 N、同比环比由本模块
    按同一套模板拼装，与具体是哪个库无关。

于是接新库变成「登记口径」，而不是「再写一个 plan_x」。这也是「知识本体 → 口径 →
SQL」这条链上唯一缺的那一步：本体说「怎么算」，这里把它编译成可执行 SQL。

边界
----
- 只认已登记口径，未命中一律拒绝并**列出已登记口径**（说清楚能问什么，不猜）。
- 不碰 A/B 两库（它们的意图库已过基线，改动只会引入回归）。
"""
import re

# 维度中文名 → 视图列。同一列可有多个说法；metric.dims 里没这一列时不会用。
DIM_LABELS = {
    "prdno": ["产品", "贷款产品", "产品编号", "品种", "业务品种", "贷款品种"],
    "prdnm": ["产品名称", "品种名称"],
    "org_no": ["机构", "经办机构", "核算机构", "机构号", "分行", "支行"],
    "org_nm": ["机构名称"],
    "branch_no": ["分行号", "归属分行"],
    "five_level": ["五级分类", "风险分类", "分类", "贷款分类"],
    "cust_type": ["客户类型", "客户种类"],
    "bsnsts": ["贷款状态", "业务状态", "借据状态"],
}

# 客户类型取值（本库存中文）
CUST_TYPE_WORDS = {"对公": "对公", "公司": "对公", "企业": "对公",
                   "个人": "个人", "零售": "个人", "同业": "同业", "金融机构": "同业"}

# 五级分类词 → 字母码
LEVEL_WORDS = {"正常": "N", "关注": "A", "次级": "S", "可疑": "D", "损失": "L"}

# 产品关键词 → prdnm 模糊匹配片段（视图里没有 prdnm 时退化为不支持）
PRODUCT_HINTS = [
    ("流动资金", "流动资金"),
    ("流贷", "流动资金"),
    ("项目贷款", "项目贷款"),
    ("固定资产", "固定资产"),
    ("贸易融资", "贸易融资"),
    ("按揭", "住房按揭"),
    ("住房", "住房按揭"),
    ("消费贷", "消费"),
    ("经营性", "经营性"),
    ("拆借", "同业拆借"),
]

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
           "七": 7, "八": 8, "九": 9, "十": 10}

_MONTH_WORDS = r"(?:([0-9]{4})\s*年)?\s*([0-9]{1,2}|十[一二]?|[一二三四五六七八九十])\s*月"


def _cn2int(s):
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    if s.startswith("十"):
        return 10 + (_CN_NUM.get(s[1:], 0) if len(s) > 1 else 0)
    return None


def _metrics_of(dataset):
    reg = (getattr(dataset, "mdl", None) or {}).get("x_askoda_metrics") or {}
    return reg.get("metrics") or [], (reg.get("views") or {})


def _known(dataset):
    """已登记口径的人话清单（拒绝时告诉业务方能问什么）。"""
    metrics, _ = _metrics_of(dataset)
    return "、".join(m.get("label") or m.get("key") for m in metrics) or "（空）"


def _refuse(dataset_key, concept, reason, guide):
    return {
        "dataset": dataset_key,
        "engine": "metric-planner",
        "blocked": True,
        "intent": "硬门禁拒绝：「%s」" % concept,
        "sql": "",
        "objects": [],
        "reason": reason,
        "guide": guide,
        "steps": [
            "口径匹配：未命中任何已登记口径（不猜测）",
            "硬门禁：没有口径就不生成 SQL",
            "治理指引：%s" % guide,
        ],
    }


def _ok(dataset_key, metric, sql, objects, notes):
    return {
        "dataset": dataset_key,
        "engine": "metric-planner",
        "blocked": False,
        "intent": "命中口径「%s」" % (metric.get("label") or metric.get("key")),
        "sql": sql,
        "objects": objects,
        "reason": "",
        "guide": "",
        "steps": [
            "口径匹配：命中已登记口径「%s」（%s）" % (
                metric.get("label") or metric.get("key"), metric.get("key")),
            "维度解析：%s" % ("、".join(notes.get("dims") or []) or "不分组（汇总一行）"),
            "时间口径：%s" % notes.get("time", ""),
            "经 MDL dry-run 门禁校验",
            "Wren query 真实执行（只读）",
        ],
    }


# --------------------------------------------------------------------------
# 口径命中
# --------------------------------------------------------------------------
def _pick_metric(metrics, text):
    """按别名长度打分：'不良率' 应命中 npl_ratio，而不是 '不良' 命中 npl_balance。"""
    best, best_len = None, 0
    for m in metrics:
        for al in (m.get("aliases") or []):
            if al and al in text and len(al) > best_len:
                best, best_len = m, len(al)
    return best


# --------------------------------------------------------------------------
# 时间口径
# --------------------------------------------------------------------------
def _month_bounds(year, month):
    if not month:
        return None, None
    end_y, end_m = (year + 1, 1) if month == 12 else (year, month + 1)
    return "%04d-%02d-01" % (year, month), "%04d-%02d-01" % (end_y, end_m)


def _time_where(metric, text):
    """返回 (SQL 条件, 人话说明)。时点量与流量分开处理——这是最容易出错的地方。"""
    view = metric.get("view")
    col = metric.get("time_col") or "stat_date"
    mode = metric.get("time_mode") or "snapshot"
    latest_snap = "SELECT MAX(%s) FROM %s" % (col, view)

    # 区间词
    m_last = re.search(r"(?:最近|近|过去)\s*([0-9]{1,2}|[一二三四五六七八九十])\s*(?:个)?月", text)
    m_month = re.search(_MONTH_WORDS, text)
    m_year = re.search(r"(20[0-9]{2})\s*年(?!\s*[0-9]{1,2}\s*月)", text)

    if mode == "snapshot":
        if m_month and m_month.group(2):
            y = int(m_month.group(1)) if m_month.group(1) else None
            mo = _cn2int(m_month.group(2))
            if mo and 1 <= mo <= 12:
                if y:
                    start, end = _month_bounds(y, mo)
                    return (
                        "%s = (SELECT MAX(%s) FROM %s WHERE %s <= DATE '%s')"
                        % (col, col, view, col, end),
                        "%d 年 %d 月末快照（取不晚于该月末的最新快照日）" % (y, mo),
                    )
                # 只有月份没年份：取数据里最近一个该月
                return (
                    "%s = (SELECT MAX(%s) FROM %s WHERE EXTRACT(MONTH FROM %s) = %d)"
                    % (col, col, view, col, mo),
                    "最近一个 %d 月快照（未指定年份，按数据中最近的一个 %d 月）" % (mo, mo),
                )
        if "上季" in text or "上季度" in text:
            return (
                "%s = (SELECT MAX(%s) FROM %s WHERE %s < date_trunc('quarter', (%s)))"
                % (col, col, view, col, latest_snap),
                "上一个季度末快照",
            )
        if "去年" in text and not m_month:
            return (
                "%s = (SELECT MAX(%s) FROM %s WHERE %s < date_trunc('year', (%s)))"
                % (col, col, view, col, latest_snap),
                "上一年度末快照",
            )
        return (
            "%s = (%s)" % (col, latest_snap),
            "最新快照日（时点量必须限定单一日期，否则跨日相加会把金额翻倍）",
        )

    # ---- 流量（放款金额等，按放款月累加）
    if m_last:
        n = _cn2int(m_last.group(1)) or 1
        n = max(1, min(n, 60))
        return (
            "%s >= date_trunc('month', (%s)) - INTERVAL '%d months'"
            % (col, latest_snap, n - 1),
            "以数据最新月为终点、近 %d 个月（含当月）" % n,
        )
    if "本季" in text or "本季度" in text or "季度" in text:
        return (
            "%s >= date_trunc('quarter', (%s))" % (col, latest_snap),
            "数据最新月所在季度至今",
        )
    if "本年" in text or "今年" in text or "年初" in text:
        return (
            "%s >= date_trunc('year', (%s))" % (col, latest_snap),
            "数据最新月所在年度至今",
        )
    if m_month and m_month.group(2):
        y = int(m_month.group(1)) if m_month.group(1) else None
        mo = _cn2int(m_month.group(2))
        if mo and 1 <= mo <= 12:
            if y:
                start, end = _month_bounds(y, mo)
                return (
                    "%s = date_trunc('month', DATE '%s')" % (col, start),
                    "%d 年 %d 月（按放款月）" % (y, mo),
                )
            return (
                "EXTRACT(MONTH FROM %s) = %d" % (col, mo),
                "数据中每个月里属于 %d 月的那部分（未指定年份）" % mo,
            )
    if "上月" in text:
        return (
            "%s = date_trunc('month', (%s)) - INTERVAL '1 month'" % (col, latest_snap),
            "数据最新月的前一个月",
        )
    return (
        "%s = date_trunc('month', (%s))" % (col, latest_snap),
        "数据最新月份（默认当期；流量指标按期累加）",
    )


def _base_period_where(metric, where):
    """把当期条件改写成基期条件（同比 -1 年 / 环比 -1 月）。"""
    if "INTERVAL" in where:
        return re.sub(r"INTERVAL '(\d+) months'", lambda m: "INTERVAL '%d months'" % (int(m.group(1)) + 12), where, count=1)
    return where


# --------------------------------------------------------------------------
# 维度 / 过滤
# --------------------------------------------------------------------------
def _dims_in_text(metric, text):
    cols = []
    for col in (metric.get("dims") or []):
        for label in DIM_LABELS.get(col, []):
            if label in text:
                cols.append(col)
                break
    # 产品名/客户类型这种「值」不是维度，别误当分组维度
    if "prdno" in cols and any(h in text for h in [p[0] for p in PRODUCT_HINTS]) and "各" not in text and "每个" not in text and "按" not in text:
        cols.remove("prdno")
    return cols


def _filters(metric, text):
    frags, notes = [], []
    dims = metric.get("dims") or []
    view_cols = set(metric.get("_view_cols") or [])

    for word, val in CUST_TYPE_WORDS.items():
        if word in text and "cust_type" in dims:
            frags.append("cust_type = '%s'" % val)
            notes.append("客户类型=%s" % val)
            break

    hits = [v for k, v in LEVEL_WORDS.items() if k in text]
    if "five_level" in dims:
        # 口径本身已经是「不良」口径时不再叠加不良过滤，否则会与 expr 里的 CASE 重复
        _already_npl = str(metric.get("key") or "").startswith("npl")
        if "不良" in text and not hits and not _already_npl:
            frags.append("five_level IN ('S', 'D', 'L')")
            notes.append("五级分类∈{次级,可疑,损失}（不良口径）")
        elif len(hits) == 1:
            frags.append("five_level = '%s'" % hits[0])
            notes.append("五级分类=%s" % hits[0])
        elif len(hits) > 1:
            frags.append("five_level IN (%s)" % ", ".join("'%s'" % h for h in hits))
            notes.append("五级分类∈{%s}" % ",".join(hits))

    if "prdnm" in view_cols and "prdnm" in dims:
        for kw, frag in PRODUCT_HINTS:
            if kw in text and "prdno" not in _dims_in_text(metric, text):
                frags.append("prdnm LIKE '%%%s%%'" % frag)
                notes.append("产品≈%s" % frag)
                break
    return frags, notes


def _rank(text):
    m = re.search(r"(?:前|top\s*|TOP\s*)([0-9]{1,3}|[一二三四五六七八九十]{1,3})\s*(?:名|个|位)?", text)
    if not m:
        return None
    return _cn2int(m.group(1))


def _wants_trend(text):
    return any(k in text for k in ["趋势", "走势", "按月", "月度", "每月", "逐月"])


def _compare(text):
    if "同比" in text or "同期" in text:
        return "yoy"
    if "环比" in text or "较上" in text:
        return "mom"
    return None


def _has_window(text):
    """是否指定了「近 N 个月」这类区间（与同比/环比互斥）。"""
    return bool(re.search(
        r"(?:最近|近|过去)\s*([0-9]{1,2}|[一二三四五六七八九十])\s*(?:个)?月", text))


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def plan(dataset, nl):
    key = getattr(dataset, "key", "?")
    text = (nl or "").strip()
    metrics, views = _metrics_of(dataset)

    if not metrics:
        return _refuse(
            key, "信贷口径",
            "该数据集尚未登记口径：MDL 里没有 x_askoda_metrics，规划器不知道该按什么口径算",
            "在 db/credit/04_metrics.json 登记口径后执行 tools/credit_metrics.py --apply",
        )
    if not text:
        return _refuse(key, "空需求", "需求文本为空，无法匹配口径", "补全需求描述后重试")

    metric = _pick_metric(metrics, text)
    if metric is None:
        return _refuse(
            key, "未登记的口径",
            "需求里的指标不在已登记口径内（当前已登记：%s）" % _known(dataset),
            "换用已登记口径的说法，或先在 04_metrics.json 登记该口径再重算",
        )

    view = metric.get("view")
    vcols = (views.get(view) or {}).get("columns") or []
    metric = dict(metric)
    metric["_view_cols"] = vcols

    if not metric.get("expr"):
        return _refuse(key, metric.get("label") or "", "口径定义缺少 expr，无法编译", "补齐 04_metrics.json")

    time_where, time_note = _time_where(metric, text)
    dim_cols = _dims_in_text(metric, text)
    filter_frags, filter_notes = _filters(metric, text)
    trend = _wants_trend(text)
    top = _rank(text)
    cmp_mode = _compare(text)

    tcol = metric.get("time_col")
    latest_snap = "SELECT MAX(%s) FROM %s" % (tcol, view)
    if trend and tcol not in dim_cols:
        dim_cols = [tcol] + dim_cols

    where_all = [time_where] + filter_frags
    alias = metric["key"]

    def _select_list(dims):
        cols = []
        for d in dims:
            cols.append(d if d != tcol else "%s AS %s" % (tcol, tcol))
        return cols

    if cmp_mode:
        if metric.get("time_mode") != "flow":
            return _refuse(
                key, metric.get("label") + " " + ("同比" if cmp_mode == "yoy" else "环比"),
                "时点量（快照）做同比/环比需要两个可比时点的快照；本库只有 2026-06-30 与 "
                "2026-09-30 两个快照日，去年同期无数据",
                "换用流量口径（如放款金额）做同比，或先把历史快照补进数仓",
            )
        if _has_window(text) or trend:
            return _refuse(
                key, metric.get("label") + " " + ("同比" if cmp_mode == "yoy" else "环比"),
                "同比/环比是「当期 vs 基期」两期之比，与「近 N 个月趋势」是两种问法，不能叠加",
                "分开问：先问「本月放款金额同比」，或先问「近 6 个月放款金额趋势」",
            )
        # 基期单独构造，不在当期条件上打补丁——打补丁会把两期写成同一期，
        # 增长率恒为 0 且不报错（2026-11-01 自测抓到）。
        shift = "'1 year'" if cmp_mode == "yoy" else "'1 month'"
        base_where = "%s = date_trunc('month', (%s)) - INTERVAL %s" % (tcol, latest_snap, shift)
        filter_sql = (" AND " + " AND ".join(filter_frags)) if filter_frags else ""
        dims_no_time = [d for d in dim_cols if d != tcol]
        if dims_no_time:
            g = ", ".join(dims_no_time)
            on = " AND ".join("c.%s = b.%s" % (k, k) for k in dims_no_time)
            sql = (
                "WITH cur AS (SELECT %s, %s AS v FROM %s WHERE %s%s GROUP BY %s), "
                "base AS (SELECT %s, %s AS v FROM %s WHERE %s%s GROUP BY %s) "
                "SELECT %s, c.v AS %s, b.v AS %s_base, "
                "(c.v - b.v) / NULLIF(b.v, 0) AS %s_growth "
                "FROM cur c LEFT JOIN base b ON %s ORDER BY c.v DESC"
                % (g, metric["expr"], view, time_where, filter_sql, g,
                   g, metric["expr"], view, base_where, filter_sql, g,
                   ", ".join("c.%s" % d for d in dims_no_time), alias, alias, alias, on)
            )
        else:
            # 无维度：不用 CROSS JOIN —— 两行单行表无条件连接会被 L2 判成
            # JOIN_WITHOUT_CONDITION（阻断）。改成标量子查询，语义更直白。
            _cur = "(SELECT %s FROM %s WHERE %s%s)" % (
                metric["expr"], view, time_where, filter_sql)
            _base = "(SELECT %s FROM %s WHERE %s%s)" % (
                metric["expr"], view, base_where, filter_sql)
            sql = (
                "SELECT %s AS %s, %s AS %s_base, "
                "(%s - %s) / NULLIF(%s, 0) AS %s_growth"
                % (_cur, alias, _base, alias, _cur, _base, _base, alias)
            )
        notes = {
            "dims": [d for d in dim_cols],
            "time": time_note + (
                "；基期=当期前 12 个月" if cmp_mode == "yoy" else "；基期=当期前 1 个月"),
        }
        objs = _objects(view, dim_cols + [alias], metric)
        return _ok(key, metric, sql, objs, notes)

    # 时间列统一转成日期再输出：引擎按 timestamp 返回会带 00:00:00.000000，业务侧看不懂。
    # 注意 SELECT 与 GROUP BY 必须分开写——GROUP BY 里带 AS 是语法错误（实测报 ParserError）。
    sel_dim = ", ".join(
        ("CAST(%s AS DATE) AS %s" % (d, d)) if d == tcol else d for d in dim_cols
    )
    grp_dim = ", ".join(dim_cols)
    if sel_dim:
        sql = "SELECT %s, %s AS %s FROM %s WHERE %s GROUP BY %s" % (
            sel_dim, metric["expr"], alias, view, " AND ".join(where_all), grp_dim,
        )
    else:
        sql = "SELECT %s AS %s FROM %s WHERE %s" % (
            metric["expr"], alias, view, " AND ".join(where_all),
        )
    if sel_dim:
        # 趋势类问题按时间正序（业务方要的是「走势」）；排名/普通分组按数值倒序
        if trend and tcol in dim_cols:
            sql += " ORDER BY %s ASC" % tcol
        else:
            sql += " ORDER BY %s DESC" % alias
    if top:
        sql += " LIMIT %d" % top

    notes = {
        "dims": dim_cols,
        "time": time_note + ("；" + "；".join(filter_notes) if filter_notes else ""),
    }
    return _ok(key, metric, sql, _objects(view, dim_cols + [alias], metric), notes)


def _objects(view, cols, metric):
    objs = [{
        "type": "view",
        "name": view,
        "detail": "口径视图：%s" % (metric.get("definition") or "")[:60],
    }]
    for c in cols:
        if c and c not in ("v",):
            objs.append({"type": "column", "name": c, "detail": ""})
    objs.append({
        "type": "rule",
        "name": "口径 %s" % metric.get("key"),
        "detail": (metric.get("definition") or "")[:120],
    })
    if metric.get("caveats"):
        objs.append({
            "type": "rule",
            "name": "口径坑点 %s" % metric.get("key"),
            "detail": metric["caveats"][:120],
        })
    return objs
