# -*- coding: utf-8 -*-
"""语义分析服务（M3 · 五个 Skill 的确定性落地）

定位
----
《最终技术方案》§4.3 把「业务需求分析」的 LLM 归属客户端 Agent，网关侧只做
**证据组装 + 规则硬约束**。所以本模块不是"另一个模型"，而是：

    在证据支持的范围内，把能确定的东西确定下来；确定不了的，明确标成"需确认"。

这条边界是刻意的。业务口径错了的代价远大于"多问一句"——所以凡是证据不足的地方，
本模块宁可输出低置信 + 待确认，也不给一个看起来很确定的错答案（Skill 4.3 明文要求）。

落地了哪几个 Skill
------------------
- 4.1 需求接入摘要      → `summary()`
- 4.2 需求理解          → `understand()`（主体/时间/范围/字段/聚合意图/使用目的）
- 4.3 颗粒度识别        → `granularity()`（含 1:N 风险）
- 4.4 时间口径识别      → `time_semantics()`
- 4.7 规则校验          → `rules()`（7 条规则的确定性判定）
- 4.8 确认问题生成      → `questions()`（只问影响结果的问题，且过技术词闸门）

本模块**不碰数据库**（取证在 `evidence.py`，落库在 `analysis.py`），因此可离线自证。
"""
import re

import evidence as ev

SLOT_KEYS = ["subject", "granularity", "time", "scope", "fields", "risks"]
SLOT_LABELS = {
    "subject": "主体",
    "granularity": "颗粒度",
    "time": "时间口径",
    "scope": "范围条件",
    "fields": "输出字段",
    "risks": "风险点",
}

# 置信度基线：按证据优先级给底分。数字不是精确科学，是**可解释、可争论**的约定：
# 业务亲口确认过 > 数据字典写死的口径 > 表关系 > 制度文档 > 需求原文里的字面表述。
LEVEL_BASE = {
    "P1": 0.45,  # 需求原文：业务自己说的，但可能说得含糊
    "P2": 0.85,  # 业务确认：已答复，最高可信
    "P3": 0.70,  # 表样附件
    "P4": 0.65,  # 历史案例：可参考，不等于现行口径
    "P5": 0.72,  # 系统资料 / 制度 / 口径说明书
    "P6": 0.80,  # 数据字典：字段级定义
    "P7": 0.82,  # 表关系：结构性事实
    "P8": 0.50,  # 规则：约束而非事实
    "P9": 0.30,  # 模型推断：兜底
}
CONF_CAP_NEEDS_CONFIRM = 0.60  # 需确认的结论，置信度上限（不许"高置信地不确定"）
CONF_CAP_DEGRADED = 0.50  # 有关键证据源取不到时，整轮结论的置信度上限

# 证据源缺失的后果分级。
#
# 判据只有一条：**这个来源支撑的判定是不是 blocking 级**。是，则缺失本身就是
# blocking——因为"没法判"和"判过没问题"对下游的影响完全不同，不能混为一谈。
# 反过来说，若某来源只支撑参考性判断（历史案例、表样），缺失只是 warning。
DEGRADED_IMPACT = {
    "P1": ("当前业务说明", "blocking", "需求正文取不到，后续结论没有业务依据"),
    "P2": ("业务确认", "warning", "已确认口径取不到，可能重复追问业务已确认过的问题"),
    "P3": ("表样", "warning", "表样取不到，字段口径只能依赖字典描述"),
    "P4": ("历史案例", "warning", "历史案例取不到，同类需求的可复用做法未被参考"),
    "P5": ("系统资料", "warning", "系统资料取不到，口径依据可能不足"),
    "P6": ("数据字典", "blocking", "数据字典取不到，字段与颗粒度结论退化为推测"),
    "P7": ("表关系", "blocking", "表关系取不到，主体是否跨一对多无从判断，R1 实际未生效"),
    "P8": ("通用规则", "blocking", "内置规则集未装载，规则校验未执行"),
}


# ---------------------------------------------------------------------------
# 关键词表
# ---------------------------------------------------------------------------
TIME_SEMANTICS = {
    "发生时间": ["下单", "成交", "支付", "付款", "核销", "使用", "注册", "开业", "发生", "交易", "购买"],
    "状态时间": ["当前状态", "截至", "时点", "目前", "现存", "最新状态", "状态为", "期末状态"],
    "快照时间": ["快照", "月末", "期末", "月初", "日终", "余额", "存量"],
    "统计期间": ["近", "本月", "上月", "本季", "上季", "本年度", "去年", "今年", "同比", "环比",
                 "日均", "周均", "月均", "期间", "区间", "累计", "逐日", "逐月"],
}

PERIOD_RE = re.compile(
    r"(近\s*\d+\s*[天日周月年]|过去\s*\d+\s*[天日周月年]|\d{4}\s*年(?:份)?|本月|上月|本季度|上季度"
    r"|本年度|去年|今年|同比|环比|年初至今|YTD|[1-9]{1,2}\s*月)"
)

SCOPE_KEYWORDS = {
    "排除": ["排除", "不含", "剔除", "除去", "不包含", "去掉"],
    "限定": ["仅", "只", "限定", "仅看", "只要", "只看"],
    "状态筛选": ["已退款", "已完成", "活跃", "沉默", "流失", "直营", "加盟"],
}

AGG_KEYWORDS = {
    "汇总求和": ["合计", "总计", "总和", "求和", "汇总", "累计"],
    "计数": ["数量", "个数", "笔数", "单量", "人数", "次数", "有多少", "多少家"],
    "平均": ["平均", "均值", "人均", "店均", "日均"],
    "比率": ["率", "占比", "比例", "百分比"],
    "排名": ["排名", "排行", "TOP", "top", "前十", "前10", "最高", "最低", "最好", "最差"],
    "趋势对比": ["趋势", "走势", "变化", "对比", "相比", "比较", "增减"],
    "分组": ["按", "分", "各", "每个", "各个", "分别", "逐"],
}

PURPOSE_KEYWORDS = {
    "监控预警": ["监控", "预警", "告警", "异常", "实时"],
    "绩效评估": ["考核", "绩效", "评估", "对标", "达标"],
    "复盘分析": ["复盘", "回顾", "总结", "归因", "原因"],
    "经营决策": ["决策", "策略", "优化", "提升", "改善"],
}

# 风险信号词（供 4.7 规则校验判定）
LOAD_DATE_WORDS = ["装载时间", "装载日期", "入库时间", "入库日期", "load_time", "数据加载时间", "etl时间"]
MULTI_VALUE_WORDS = ["多值", "多个状态", "拼接", "合并字段", "多个标签", "标签拼接"]
PENDING_CALIBER_WORDS = ["券成本", "核销GMV", "核销gmv", "券种细分", "按券种", "券面额成本", "券口径"]
RATIO_WORDS = ["复购率", "回购率", "占比", "比率", "坪效", "人效"]
# 分母词：口径说明书里复购率分母=活跃用户数、坪效分母=经营面积
DENOM_WORDS = ["活跃用户数", "活跃会员数", "活跃人数", "分母", "基数", "活跃客户数", "面积"]
DEDUP_WORDS = ["去重", "不重复", "唯一", "按订单去重", "明细口径", "主表口径", "只算一次"]
UNMODELLED_WORDS = [
    "手机号", "手机号码", "联系电话", "身份证", "身份证号", "会员姓名", "客户姓名",
    "姓名", "装载时间", "load_time", "id_card", "member_phone",
]

# 「确认问题不许含技术词」闸门的技术词
BANNED_QUESTION_TERMS = [
    "表名", "字段名", "字段", "主键", "外键", "索引", "分区表", "视图", "宽表",
    "事实表", "维度表", "关联表", "库表", "数据类型", "varchar", "numeric",
    "timestamp", "boolean", "JOIN", "join", "SQL", "sql", "SELECT", "select",
    "WHERE", "where", "GROUP BY", "group by", "ORDER BY", "order by", "DISTINCT",
    "distinct", "SUM(", "COUNT(", "AVG(", "MDL", "mdl",
]


# ---------------------------------------------------------------------------
# 候选结论
# ---------------------------------------------------------------------------
def confidence(levels, extra=0.0, cap=None):
    """按证据等级算置信度。无证据 = 只能算模型推断（0.30）。"""
    if not levels:
        base = LEVEL_BASE["P9"]
    else:
        base = max(LEVEL_BASE.get(l, LEVEL_BASE["P9"]) for l in levels)
    val = min(0.95, base + extra)
    if cap is not None:
        val = min(val, cap)
    return round(val, 2)


def _citing(ev_items, limit=4):
    out = []
    for e in ev_items[:limit]:
        out.append(
            {
                "level": e["level"],
                "level_name": e["level_name"],
                "source": e["source"],
                "locator": e.get("locator"),
            }
        )
    return out


def _cand(slot, value, cited, conf=None, needs=False, note=None, **extra):
    """构造一条带来源与置信度的结论。

    `cited` 允许传「完整证据项」或「引用摘要」两种形态，这里统一压成引用摘要——
    调用方不该为"我手上这份是哪种"而分心，那正是出错的地方。
    """
    cited = _citing(list(cited or []))
    levels = [c["level"] for c in cited]
    if conf is None:
        conf = confidence(levels)
    if needs:
        conf = min(conf, CONF_CAP_NEEDS_CONFIRM)
    out = {
        "slot": slot,
        "value": value,
        "confidence": round(conf, 2),
        "needs_confirmation": bool(needs),
        "evidence": cited,
    }
    if note:
        out["note"] = note
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 4.1 需求接入摘要
# ---------------------------------------------------------------------------
def summary(demand):
    """标准化摘要。**不做业务判断**（Skill 4.1 明文边界）。"""
    fields = ["title", "business_context", "description", "expected_output", "contact"]
    missing = [f for f in fields if not str(demand.get(f) or "").strip()]
    if not str(demand.get("time_range") or "").strip():
        missing.append("time_range")

    atts = [a for a in (demand.get("attachments") or []) if isinstance(a, dict)]
    risky = [a.get("filename") for a in atts if (a.get("risk") or {}).get("level") in ("high", "unknown")]
    sample = [a.get("filename") for a in atts if (a.get("filename") or "").lower().endswith(
        (".csv", ".tsv", ".xlsx", ".xls"))]

    return {
        "demand_summary": "%s：%s" % (
            str(demand.get("title") or "").strip(),
            str(demand.get("description") or "").strip()[:120],
        ),
        "attachment_summary": "共 %d 个附件%s" % (
            len(atts), ("，其中需注意：%s" % "、".join(str(x) for x in risky)) if risky else "",
        ),
        "sample_summary": "可作为表样的文件：%s" % ("、".join(str(x) for x in sample) if sample else "无"),
        "missing_info": missing,
        "parse_status": "ok" if not [m for m in missing if m in ("title", "description")] else "incomplete",
    }


# ---------------------------------------------------------------------------
# 4.2 需求理解
# ---------------------------------------------------------------------------
def _hits_by_kind(hits, kinds):
    return [h for h in hits if (h.get("kind") or "").replace("_alias", "") in kinds]


GENERIC_ONLY_MIN = 2  # 仅靠泛化词支撑的表，至少要 2 个泛化词才够格当主体


def _is_derived_hit(h):
    """判断命中是否来自「泛化形式」——去修饰变体或口语别名。

    判据是"匹配到的词比它所属标签的规范形式更短"：标签「归属门店」派生出「门店」，
    于是命中「门店」属泛化；标签本身就是「坪效」，命中「坪效」则不属泛化。
    """
    lbl = ev.clean_label(h.get("label") or "")
    return bool(lbl) and h.get("term") != lbl


def _tables_of(hits, with_strength=False):
    """把命中折算到"涉及哪张表"，并剔除只靠泛化词支撑的表。

    踩过的坑：字段「归属门店」会派生变体「门店」，业务文本里"各门店"一出现，
    该字段所属的表就被拉成主体——于是"门店坪效"这种需求会莫名多出
    `ads_member_repurchase_di`。判据：泛化命中单独出现不足以确立主体，
    需 ≥2 个不同泛化词才作数。

    `with_strength=True` 时额外带回 `{表: {"strong": n, "weak": m}}` 的强弱结构。
    这是排序需要的关键信息：一张靠 2 个泛化词凑够下限入围的表，证据强度
    低于一张有实词命中的表（详见 `understand()` 的主体排序）。
    """
    per = {}
    for h in hits:
        t = h.get("table")
        if not t:
            canon = h.get("canonical") or ""
            t = canon.split(".")[0] if "." in canon else None
        if not t:
            continue
        d = per.setdefault(t, {"strong": set(), "weak": set(), "hits": []})
        (d["weak"] if _is_derived_hit(h) else d["strong"]).add(h["matched"])
        d["hits"].append(h)

    out = {}
    strength = {}
    for t, d in per.items():
        if d["strong"] or len(d["weak"]) >= GENERIC_ONLY_MIN:
            out[t] = d["hits"]
            strength[t] = {"strong": len(d["strong"]), "weak": len(d["weak"])}
    return (out, strength) if with_strength else out


def _dedup_hits(hits):
    """去掉完全重复的命中（同 term + 同 kind + 同 locator + 同表 + **同位置**）。

    2026-10-07 实测根因：匹配层会把「门店 → ads.store_id」这条**完全相同**的命中
    重复产出多次，使 hit_count虚高（2 → 6）。主体排序按 hit_count 时，
    重复灌水的那张表就会压过只命中一次的区分词表（选错主体）。
    这里按「命中四元组 + 出现位置」去重，不改变命中集合本身，只消除计数噪声。

    位置必须参与去重键：同一个词在需求里出现多次是**真实信息**
    （「所有门店」vs 只出现一次），去掉位置会让"在诉求字段里出现 3 次"
    和"只出现 1 次"塌缩成同一条，`demand_field_hits` 就再也分不开。
    """
    seen = set()
    out = []
    for h in hits:
        key = (
            h.get("matched"),
            h.get("kind"),
            h.get("locator"),
            h.get("table") or (h.get("canonical") or "").split(".")[0],
            tuple(h.get("span") or ()),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out


def _term_table_freq(hits):
    """每个命中词覆盖多少张表。词越常见 → 区分度越低。

    实测根因（2026-10-07）：主体候选原按 `hit_count`（原始命中**次数**）排序，
    于是「订单数/门店」这类泛用词在多张表里重复命中，次数压过
    「销售件数/销售额」这类只命中一张表的区分词，导致主体选错。
    """
    freq = {}
    for h in hits:
        t = h.get("table")
        if not t:
            canon = h.get("canonical") or ""
            t = canon.split(".")[0] if "." in canon else None
        if not t:
            continue
        freq.setdefault(h.get("matched") or "", set()).add(t)
    return {k: len(v) for k, v in freq.items()}


def _discriminative_score(terms, freq):
    """区分度 = Σ(1 / 该词命中的表数)。

    只命中一张表的词权重 1.0（强证据）；到处都有的词权重趋近 0（弱证据）。
    """
    return round(sum(1.0 / max(1, freq.get(t, 1)) for t in terms), 4)


def _demand_field_weight(hs):
    """这张表上有多少**不同诉求字段**（title/description/expected_output）里出现过命中词。

    用途仅限冲突消解的 tiebreak 与说明，不参与主体排序的主导权重——
    「门店」在诉求里出现 4 次并不代表门店维表就该当主体（实测 DR-20261006-17YP：
    ads靠「门店」多出场压过真正持有 `sales_qty` 的 dws，见 `understand()` 注释）。
    """
    return len({
        f
        for h in hs
        if h.get("in_demand_text")
        for f in (h.get("demand_fields") or ())
    })


def understand(bundle):
    """第一层业务语义理解：只回答"需求在说什么"，不判定 SQL 实现（Skill 4.2 边界）。"""
    text = bundle["text"]
    hits = bundle["matched"]
    by_level = bundle["by_level"]

    # ---- 主体候选：由命中折算出的表 ----
    # 排序键：置信度 → 实词命中数 → 区分度 → 命中次数。
    #
    # 「实词命中数」接回排序键（2026-10-07）：`_tables_of` 一直在算 strong/weak
    # 却没有出口，靠 2 个泛化派生词凑够 `GENERIC_ONLY_MIN` 才入围的表，
    # 证据强度低于有一条例实词命中的表。区分度排在命中数之前——泛用词在多表
    # 重复命中时，次数会被灌水（`_dedup_hits` 只消除了完全重复的那部分）。
    #
    # 这里**刻意不加**「诉求字段命中数」权重。实测 DR-20261006-17YP
    # （门店累计销量）试过：诉求是"按门店分组算销量"，"门店"在 title/description/
    # expected_output 里出现 4 次，而度量词"销售件数"只在 description 出现 1 次。
    # 按诉求字段数加权会让门店维表 `ads_member_repurchase_di` 压过真正持有
    # `sales_qty` 的 `dws_store_daily_agg`——**更错**。频次反映的是用词习惯，
    # 不是主体倾向，这类权重不该主导。
    #
    # 两候选在这些信号上打平时**不再静默择一**（见下方 `ambiguous` 标注）：
    # 按 R4「未确认关键口径不得进入高置信输出」，应交业务确认。
    # 另注：主体候选只影响给业务看的建议与冲突提示，不决定实际 SQL——
    # 生成走 `candidate_sql`（业务/Agent 传入）+ 五层门禁。
    term_freq = _term_table_freq(hits)
    subj_tables, subj_strength = _tables_of(_dedup_hits(hits), with_strength=True)
    subj = []
    for table, hs in subj_tables.items():
        cited = [b for h in hs[:3] for b in _ev_for_term(by_level, h)]
        cited = _dedup_cited(cited) or _p1_cite(by_level)
        terms = sorted({h["matched"] for h in hs})
        st = subj_strength.get(table, {"strong": 0, "weak": 0})
        c = _cand(
            "subject",
            table,
            cited,
            conf=confidence([x["level"] for x in cited], extra=0.05 if len(terms) > 1 else 0.0),
            matched_terms=terms,
            hit_count=len(hs),
        )
        c["discriminative_score"] = _discriminative_score(terms, term_freq)
        c["strong_hits"] = st["strong"]
        c["weak_hits"] = st["weak"]
        c["demand_field_hits"] = _demand_field_weight(hs)
        subj.append(c)
    subj.sort(
        key=lambda x: (
            -x["confidence"],
            -x.get("strong_hits", 0),
            -x.get("discriminative_score", 0.0),
            -len(x.get("matched_terms") or ()),
            -x["hit_count"],
        )
    )
    # 打平即歧义：排序键（不含 hit_count）完全相同说明证据分不出高下，
    # 按 R4 标为需业务确认，而不是靠列表顺序静默定一个赢家——那正是本次修复的
    # 原始缺陷（实测主体槽位选中门店维表，而真正持有销量列的是另一张表）。
    #
    # hit_count 不参与判平：它是"命中在需求里出现了几次"，反映用词频次而非
    # 证据强度。实测中 ads 的「门店」在诉求里出现 4 次、hit 6，
    # 而持有 sales_qty 的 dws 只命中 1 次、hit 2——按 hit 判胜负恰好选反。
    if len(subj) >= 2:
        k = lambda c: (  # noqa: E731
            c["confidence"],
            c.get("strong_hits", 0),
            c.get("discriminative_score", 0.0),
            len(c.get("matched_terms") or ()),
        )
        if k(subj[0]) == k(subj[1]):
            for c in subj[:2]:
                c["ambiguous"] = True
            subj[0]["ambiguous_with"] = [subj[1]["value"]]

    # ---- 输出字段候选：来自字段类命中 + 口径表达式命中 ----
    field_hits = _hits_by_kind(
        hits, ["column", "glossary", "glossary_phrase", "caliber_expression"]
    )
    fields = []
    seen = set()
    for h in field_hits:
        loc = h.get("locator")
        if not loc or loc in seen:
            continue
        seen.add(loc)
        cited = _ev_for_term(by_level, h) or _p1_cite(by_level)
        k = h["kind"]
        # hit_by 用来区分：字段直接名命中 / 口径术语命中 / 口径表达式短语命中
        if k == "caliber_expression":
            hit_by = ["mdl_caliber_expression"]
            note_line = "命中口径表达式：%s" % (h.get("source_caliber") or h.get("definition") or "")[:80]
        elif k.startswith("glossary"):
            hit_by = ["business_glossary"]
            note_line = ""
        else:
            hit_by = ["column_label_or_name"]
            note_line = ""
        cand = _cand(
            "fields", h["term"], cited, matched_terms=[h["matched"]],
            field_ref=loc, kind=k, hit_by=hit_by,
        )
        if note_line:
            cand["note"] = note_line
        fields.append(cand)

    # ---- P-C · fields 空候选必须给出非空、归因清楚的 miss_reason ----
    fields_miss_reason = None
    if not fields:
        # 归因：需要主体表列表（按 subject_candidates）
        subj_tables = [s.get("value") for s in subj if s.get("value")]
        text_tokens = sorted({
            seg for seg in __import__("re").split(
                r"[，。；、（）()=＝/／\+\-\*：:\s]+", text or ""
            ) if 2 <= len(seg) <= 12 and any("\u4e00" <= ch <= "\u9fa5" for ch in seg)
        })
        if not subj_tables:
            mr_code = "F-MISS-NO-SUBJECT"
            mr_detail = (
                "字段候选为空（先无主体表 → 无对应字段全集）。"
                "需求分词=%s；当前主体候选为空，请先确认分析主题（订单/会员/门店…）。"
            ) % ("、".join(text_tokens[:12]) or "无")
        else:
            mr_code = "F-MISS-NO-CALIBER"
            mr_detail = (
                "字段候选为空（F-MISS-NO-CALIBER）：需求词既未命中字段列名/中文名，"
                "也未在当前主体表的 MDL 口径表达式中匹配到业务词条。"
                "主体表范围=%s；需求分词=%s；"
                "请检查：① 对应字段的 MDL description 是否显式包含口径；② 业务词是否为同义词需加进别名表。"
            ) % (
                "、".join(subj_tables[:6]) or "空",
                "、".join(text_tokens[:15]) or "无",
            )
        fields_miss_reason = {
            "code": mr_code,
            "detail": mr_detail,
            "subject_tables_checked": subj_tables,
            "text_tokens_checked": text_tokens,
        }

    # ---- 时间候选 ----
    periods = sorted(set(m.group(0).strip() for m in PERIOD_RE.finditer(text)))
    time_c = []
    if periods:
        time_c.append(
            _cand("time", "统计期间：%s" % "、".join(periods), _p1_cite(by_level), conf=0.6)
        )
    for label, words in TIME_SEMANTICS.items():
        got = [w for w in words if w in text]
        if got:
            time_c.append(
                _cand("time", label, _p1_cite(by_level), conf=0.55,
                      matched_terms=got[:5])
            )

    # ---- 范围候选 ----
    scope_c = []
    for label, words in SCOPE_KEYWORDS.items():
        got = [w for w in words if w in text]
        if got:
            scope_c.append(
                _cand("scope", label, _p1_cite(by_level), conf=0.6, matched_terms=got[:5])
            )

    # ---- 聚合意图 / 使用目的 ----
    agg = [lbl for lbl, ws in AGG_KEYWORDS.items() if any(w in text for w in ws)]
    purpose = [lbl for lbl, ws in PURPOSE_KEYWORDS.items() if any(w in text for w in ws)]

    return {
        "subject_candidates": subj,
        "time_candidates": time_c,
        "scope_candidates": scope_c,
        "field_candidates": fields,
        "fields_miss_reason": fields_miss_reason,
        "aggregation_intent": agg,
        "business_purpose": purpose,
    }


def _ev_for_term(by_level, hit):
    """把词条命中对应回 P6 证据项（按 locator 匹配）。"""
    loc = hit.get("locator")
    out = []
    for it in by_level.get("P6", []):
        if it.get("locator") == loc:
            out.append(it)
    return out


def _p1_cite(by_level):
    return (by_level.get("P1") or [])[:2]


def _dedup_cited(cited):
    seen, out = set(), []
    for c in cited:
        k = (c["level"], c["source"], c.get("locator"))
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 4.3 颗粒度识别
# ---------------------------------------------------------------------------
def granularity(bundle, subjects):
    """识别"最终一行代表什么"。判不稳就输出需确认，不强行高置信（Skill 4.3）。"""
    by_level = bundle["by_level"]
    tmeta = bundle.get("tables") or {}
    cands, uniq, risks = [], [], []

    # 候选来自表级元数据里的颗粒度描述（P6 · table_docs）
    for s in subjects:
        table = s["value"]
        grain = ((tmeta.get(table) or {}).get("grain") or "").strip()
        cite = [
            {
                "level": "P6",
                "level_name": ev.LEVEL_NAME["P6"],
                "source": "table_docs",
                "locator": table,
            }
        ]
        cands.append(
            _cand(
                "granularity",
                grain or "%s（元数据未登记颗粒度，需业务确认一行代表什么）" % table,
                cite,
                needs=not grain,
                table=table,
            )
        )
        if grain:
            m = re.search(r"主键\s*([A-Za-z0-9_,\s]+)", grain)
            if m:
                uniq.append({"table": table, "primary_key": m.group(1).strip()})

    # 1:N 风险：两张主体表之间存在 MDL 关系时，聚合口径必须先定
    subj_tables = [s["value"] for s in subjects]
    for r in by_level.get("P7", []):
        joint = r["content"]
        touched = [t for t in subj_tables if t in joint]
        if len(touched) >= 2:
            risks.append(
                {
                    "type": "one_to_many",
                    "tables": touched,
                    "detail": "主体涉及多张表且存在关联（%s）；不同颗粒度混算会导致金额/行数被放大" % joint,
                    "evidence": _citing([r]),
                }
            )

    multi = len(cands) > 1
    needs = multi or not uniq
    conf = confidence([c["level"] for c in (cands[0]["evidence"] if cands else [])], extra=0.0)
    if multi:
        conf = CONF_CAP_NEEDS_CONFIRM
    if not cands:
        conf = 0.30
    note = None
    if multi:
        note = "有 %d 个颗粒度候选，无法稳定判断一行代表什么，需业务确认" % len(cands)
    elif not uniq:
        note = "未从元数据中取到唯一标识，需业务确认一行代表什么"

    return {
        "granularity_candidates": cands,
        "unique_identifier_candidates": uniq,
        "one_to_many_risks": risks,
        "row_definition_notes": note,
        "confidence": round(min(conf, CONF_CAP_NEEDS_CONFIRM) if needs else conf, 2),
        "needs_confirmation": needs,
    }


# ---------------------------------------------------------------------------
# 4.4 时间口径识别
# ---------------------------------------------------------------------------
TIME_FIELD_INDEX = [
    ("业务日期", "dwd_order_di.order_date"),
    ("核销日期", "ads_coupon_order_di.stat_date"),
    ("统计日期", "dws_store_daily_agg.stat_date"),
    ("统计月份", "ads_member_repurchase_di.stat_month"),
    ("异动月份", "ads_member_grade_change_wi.change_month"),
    ("注册日期", "dim_member.register_date"),
    ("开业日期", "dim_store.open_date"),
]


def time_semantics(bundle):
    text = bundle["text"]
    by_level = bundle["by_level"]
    hits = bundle["matched"]
    dataset = bundle.get("dataset") or "B"

    import metadata as _md  # noqa: E402

    kinds = []
    for label, words in TIME_SEMANTICS.items():
        if any(w in text for w in words):
            kinds.append({"semantics": label, "matched_terms": [w for w in words if w in text][:5]})

    fields = []
    filtered = []
    for label, loc in TIME_FIELD_INDEX:
        if label in text:
            # P-B · 硬判据：TIME_FIELD_INDEX 命中的列必须过时间类型/语义检查
            eligible, reason = _md.is_eligible_time_field(loc, dataset=dataset, label_hint=label)
            if not eligible:
                filtered.append({"label": label, "locator": loc, "why": reason})
                continue
            cited = [t for t in by_level.get("P6", []) if t.get("locator") == loc][:1] or _p1_cite(by_level)
            fields.append(
                _cand(
                    "time", label, cited, field_ref=loc,
                    time_eligibility_reason=reason,
                    hit_by=["time_index_label"],
                )
            )

    # P-B · 兜底：TIME_FIELD_INDEX 未覆盖所有表（或用户没写明 label）时，
    # 补扫**本次命中的主题表**的时间列。
    #
    # 作用域为什么收窄到主题表（P1-12 修复要点）
    # ----------------------------------------
    # 旧实现全库扫 column_docs，把 dws_store_daily_agg.sales_amount（当日销售额）、
    # ads_coupon_order_di.coupon_amount（券面额）这类名字里蹭到「日/月」的列也
    # 灌进 slots.time.candidates——实测候选从个位数涨到 17–18 条。
    # 没有主题表就不兜底：宁可留空让人确认，也不让别的业务域的列混进来。
    subj_tables = []
    for s in (bundle.get("subject_candidates") or []):
        v = s.get("value")
        if isinstance(v, str) and v:
            subj_tables.append(v)
    subj_tables = list(dict.fromkeys(subj_tables))
    existing_refs = {f.get("field_ref") for f in fields}
    if subj_tables:
        import db as _db_sem  # noqa: E402
        # ORDER BY 保证候选顺序稳定可复现（不依赖数据库的返回顺序）
        fallback_rows = _db_sem.query(
            "SELECT table_name, column_name, column_label, data_type, is_primary_key "
            "FROM column_docs WHERE dataset=%s AND table_name = ANY(%s) "
            "ORDER BY table_name, column_name",
            (dataset, subj_tables),
        )
    else:
        fallback_rows = []
    for r in fallback_rows:
        loc = "%s.%s" % (r["table_name"], r["column_name"])
        if loc in existing_refs:
            continue
        lbl = (r.get("column_label") or r["column_name"]).strip()
        dtype = r.get("data_type") or ""
        # 不再做"正词放行"的粗筛——类型白名单由 is_eligible_time_field 一处把关
        eligible, reason = _md.is_eligible_time_field(
            loc, dataset=dataset, label_hint=lbl,
            data_type_hint=dtype, is_pk_hint=bool(r.get("is_primary_key")),
        )
        if not eligible:
            # 不进 filtered（数量多会太吵），只在 time_candidates_filtered 放前 5
            continue
        cited = [t for t in by_level.get("P6", []) if t.get("locator") == loc][:1] or _p1_cite(by_level)
        c = _cand(
            "time", lbl, cited, field_ref=loc,
            time_eligibility_reason=reason,
            hit_by=["table_time_fallback", "subject_table_scope"],
        )
        c["table"] = r["table_name"]
        fields.append(c)
        existing_refs.add(loc)

    risks = []
    if not kinds:
        risks.append(
            {"type": "time_semantics_missing",
             "detail": "需求未说明按发生时间、状态时间还是统计期间取数，不同取法结果会不同",
             "evidence": _p1_cite(by_level)}
        )
    if len(kinds) > 1:
        risks.append(
            {"type": "time_semantics_ambiguous",
             "detail": "命中多种时间语义（%s），需明确以哪一种为准" % "、".join(k["semantics"] for k in kinds),
             "evidence": _p1_cite(by_level)}
        )
    if any(w in text for w in LOAD_DATE_WORDS):
        risks.append(
            {"type": "load_date_substitution",
             "detail": "需求提到装载/入库时间；装载日期不能替代业务日期",
             "evidence": _p1_cite(by_level)}
        )
    if filtered:
        risks.append(
            {
                "type": "time_field_filtered",
                "detail": "以下 TIME_FIELD_INDEX 命中列因非时间类型/主键黑词被过滤：%s"
                % "；".join("%s(%s)->%s" % (x["label"], x["locator"], x["why"]) for x in filtered[:5]),
                "evidence": _p1_cite(by_level),
            }
        )

    needs = not kinds or len(kinds) > 1
    return {
        "time_semantics": kinds,
        "time_field_candidates": fields,
        "time_field_filtered": filtered,
        "time_risks": risks,
        "need_confirmation": needs,
    }


# ---------------------------------------------------------------------------
# 证据源健康度：把"取不到"如实标成"取不到"
# ---------------------------------------------------------------------------
def degradation(bundle):
    """列出取不到的证据来源，并说明各自让哪些判定失效。

    为什么必须单独做这一步
    ----------------------
    `evidence.collect()` 在某个来源取不到时返回的是**空列表**，而空列表与
    "查过了、确实没有"在数据结构上长得一模一样。下游若直接消费这个空列表，
    就会把「取不到」读成「没问题」——最典型的后果是 P7 读不到时 R1
    （1:N 必须明确处理方式，blocking 级）被静默跳过，风险凭空消失。

    这与「证据优先」的初衷正好相反：**证据缺失应当让结论更保守，而不是更自信。**

    实测触发场景（2026-09-30）：宿主机直跑时 `registry` 读不到容器内的
    `/workspace-b/mdl.json`，P7 抛错被吞，S5「订单金额 vs 商品明细金额核对」
    这种教科书级的 1:N 需求反而报告"无一对多风险"。
    """
    status = bundle.get("status") or {}
    out = []
    for code in sorted(status):
        st = status.get(code) or {}
        if st.get("ok") is True:
            continue
        name, sev, impact = DEGRADED_IMPACT.get(
            code, (code, "warning", "该来源未就绪，相关判定未执行")
        )
        out.append(
            {
                "level": code,
                "level_name": name,
                "severity": sev,
                "impact": impact,
                "error": st.get("error"),
                "count": st.get("count"),
            }
        )
    return out


def _degraded_risks(deg):
    """把降级项转成风险结论，让它们出现在与规则风险同一个列表里。

    与规则风险合并而非另开一个字段，是有意的：复核人看的是"这一轮有哪些不能
    忽略的事"，若降级信息躲在另一个字段，就容易被漏读。
    """
    out = []
    for d in deg:
        cite = [
            {
                "level": d["level"],
                "level_name": d["level_name"],
                "source": "evidence.status",
                "locator": "evidence.status.%s" % d["level"],
            }
        ]
        out.append(
            _cand(
                "risks",
                "证据来源不可用：%s——%s" % (d["level_name"], d["impact"]),
                cite,
                conf=0.90,
                rule_id="SRC-UNAVAILABLE",
                severity=d["severity"],
                level_code=d["level"],
                why=(d.get("error") or "该来源探测未通过"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# 4.7 规则校验
# ---------------------------------------------------------------------------
def rules(bundle, state):
    """对当前分析结论施加规则约束，找出不允许被忽略的风险点。"""
    text = bundle["text"]
    by_level = bundle["by_level"]
    triggered, blocking, warning, mandatory = [], [], [], []

    def fire(rid, why, level="blocking", cite=None):
        r = ev.RULE_BY_ID[rid]
        entry = {
            "rule_id": rid,
            "severity": level,
            "statement": r["statement"],
            "why": why,
            "evidence": _citing(cite or by_level.get("P8", []), limit=2),
        }
        triggered.append(entry)
        (blocking if level == "blocking" else warning).append(entry)

    # R1 1:N 必须明确处理方式
    o2m = state.get("granularity", {}).get("one_to_many_risks") or []
    if o2m and not any(w in text for w in DEDUP_WORDS):
        fire("R1", "主体跨多表且存在关联，需求未说明去重或汇总口径")
        mandatory.append("R1")

    # R2 装载日期不得替代业务日期
    if any(w in text for w in LOAD_DATE_WORDS):
        fire("R2", "需求文本出现装载/入库时间相关表述")
        mandatory.append("R2")

    # R3 多值字段顺序与拼接
    if any(w in text for w in MULTI_VALUE_WORDS):
        fire("R3", "需求涉及多值/拼接，未说明取值顺序与拼接方式", level="warning")

    # R4 未确认关键口径不得高置信输出
    #
    # ⚠️ 两个循环读的是**不同层级**，曾导致「业务方已确认、界面仍报 R4 阻断」：
    #   · 循环1 读 state["slots"][slot]["candidates"][*]["needs_confirmation"]（逐候选）
    #   · 循环2 读 state[key]["needs_confirmation"]（槽位顶层 flag）
    # 而 time / granularity 这两个 key 只存在于 state["slots"] 里，
    # state 顶层没有 → 循环2 永远取到 {} → 判不出问题。
    # 现在统一从 slots 取，并额外尊重「已被业务确认回填」的槽位：
    # 有 filled_by_confirmation_ids 说明业务方已答复，不再要求确认。
    low = []
    slots = state.get("slots") or {}
    for slot, s in slots.items():
        if not isinstance(s, dict):
            continue
        for c in (s.get("candidates") or []):
            if isinstance(c, dict) and c.get("needs_confirmation"):
                low.append("%s=%s" % (slot, c.get("value")))
    for key in ("granularity", "time"):
        blk = slots.get(key) or {}
        if not isinstance(blk, dict):
            continue
        # 业务方已答复 → 该槽位不再要求确认（与 _backfill_slots_from_confirmations 对齐）
        if blk.get("filled_by_confirmation_ids"):
            continue
        if blk.get("needs_confirmation") or blk.get("need_confirmation"):
            low.append(key)
    if low:
        fire("R4", "存在未确认关键口径：%s" % "、".join(sorted(set(low))[:4]))
        mandatory.append("R4")

    # R5 待上架口径不可用于分析
    if any(w in text for w in PENDING_CALIBER_WORDS):
        fire("R5", "需求涉及尚未完成治理的口径（券成本 / 核销GMV / 按券种细分）")
        mandatory.append("R5")

    # R6 比率类分母不得悬空
    ratio_hit = [w for w in RATIO_WORDS if w in text]
    if ratio_hit and not any(w in text for w in DENOM_WORDS):
        fire("R6", "需求出现比率类指标（%s）但未说明分母口径" % "、".join(ratio_hit))
        mandatory.append("R6")

    # R7 未建模字段不可引用
    um = [w for w in UNMODELLED_WORDS if w in text]
    if um:
        fire("R7", "需求涉及对 AI 不可见的字段（%s）" % "、".join(um))
        mandatory.append("R7")

    return {
        "triggered_rules": triggered,
        "blocking_risks": blocking,
        "warning_risks": warning,
        "mandatory_confirmations": sorted(set(mandatory)),
    }


# ---------------------------------------------------------------------------
# 4.8 确认问题生成
# ---------------------------------------------------------------------------
def questions(bundle, state):
    """把关键歧义转成业务能答的问题。

    两条硬约束（PRD §9.3 业务规则 1 + Skill 4.8 规则）：
    1. 只问**影响结果**的问题 —— 因此每个问题必须能说清"不确认会影响什么"，
       说不清影响的问题直接丢弃；
    2. 不许出现表名 / 字段名 / SQL 等技术语 —— 用实际 schema 做闸门，不是靠自觉。
    """
    text = bundle["text"]
    tech = set(bundle.get("technical_terms") or [])
    qs, dropped = [], []
    gran = state.get("granularity") or {}
    tm = state.get("time") or {}

    def add(qid, slot, question, reason, options, impact):
        item = {
            "question_id": qid,
            "slot": slot,
            "question": question,
            "reason": reason,
            "options": options,
            "impact_scope": impact,
        }
        # 闸门必须扫**全部对外文本**，不能只扫题干——选项同样是业务要看到的内容。
        # 实测踩到过：题干干净，选项里却带着物理表名，照样漏给业务。
        scanned = " ".join([question, reason or "", impact or ""] + list(options or []))
        bad = _technical_hits(scanned, tech)
        if bad:
            dropped.append(
                {"question_id": qid, "question": question, "blocked_terms": bad}
            )
            return
        # 说不清影响的问题不问（Skill 4.8 + PRD §9.3 业务规则 1）
        if not impact:
            dropped.append(
                {
                    "question_id": qid,
                    "question": question,
                    "blocked_terms": ["缺少 impact_scope：说不清影响的问题不问"],
                }
            )
            return
        if not options:
            dropped.append(
                {"question_id": qid, "question": question,
                 "blocked_terms": ["未给建议选项：业务无法作答的问题不问"]}
            )
            return
        qs.append(item)

    # 颗粒度
    if gran.get("needs_confirmation"):
        # 选项刻意**不**列出候选表名——那是技术语，业务看不懂也不该看到（PRD §9.3
        # 业务规则 1）。技术候选留在 slots.granularity 里给分析师看，业务只回答
        # 业务颗粒度，两边各看各的。
        add(
            "Q-GRAIN-1", "granularity",
            "这份结果里的每一行，希望代表什么？",
            "颗粒度决定了金额会不会被重复计算；一行代表什么不定下来，口径就无法定型",
            [
                "按业务单据逐条（例如一笔订单一行）",
                "按业务明细逐条（例如一件商品一行）",
                "按对象按天汇总（例如一家门店每天一行）",
                "按对象按月汇总（例如一个会员每月一行）",
                "其他（请补充说明）",
            ],
            "同一份数据按不同颗粒度汇总，金额可能相差数倍（明细放大）",
        )

    # 时间口径
    risks = {r["type"] for r in (tm.get("time_risks") or [])}
    if "time_semantics_missing" in risks or "time_semantics_ambiguous" in risks:
        add(
            "Q-TIME-1", "time",
            "取数时间按哪一个算？是业务发生的那天、状态变更的那天，还是数据加工入库的那天？",
            "三种取法在跨月、跨批次场景下结果不同，且难以事后发现",
            ["按业务发生当天", "按状态变更当天", "按数据加工入库当天", "其他（请补充说明）"],
            "同一笔业务会被归到不同的月份或统计周期，月度对比失真",
        )
    if "load_date_substitution" in risks:
        add(
            "Q-TIME-2", "time",
            "您提到的加工/入库时间，是否只是用来限定数据批次，而统计口径仍按业务发生日期？",
            "装载日期与业务日期存在跨天甚至跨月偏差",
            ["仅用于限定批次，统计仍按业务日期", "统计也要按该时间", "其他（请补充说明）"],
            "用装载日期筛选业务数据会漏掉或错纳跨批次记录",
        )

    # 比率分母
    if "R6" in (state.get("rules", {}).get("mandatory_confirmations") or []):
        add(
            "Q-CALIBER-1", "scope",
            "这个比率的基准人群怎么定？例如复购率，分母是当月有下单的活跃会员，还是全部在册会员？",
            "分子分母口径不成对时，比率无法复现，也无法与其他报表对齐",
            ["当月有下单的活跃会员", "全部在册会员", "期初存量会员", "其他（请补充说明）"],
            "分母口径不同会使比率产生结构性偏差，且无法与既有报表对齐",
        )

    # 一对多处理方式
    if "R1" in (state.get("rules", {}).get("mandatory_confirmations") or []):
        add(
            "Q-JOIN-1", "granularity",
            "同一笔业务下有多条商品记录时，金额希望怎么算？",
            "多表关联后直接汇总会把金额重复计算",
            ["按商品明细逐条累加", "同一笔业务只算一次", "两种口径各出一份", "其他（请补充说明）"],
            "金额可能被放大数倍，导致业绩虚高",
        )

    # 待上架口径
    if "R5" in (state.get("rules", {}).get("mandatory_confirmations") or []):
        add(
            "Q-PENDING-1", "scope",
            "券相关的成本与核销金额口径目前尚未完成治理，本次是否可以先不含这部分？",
            "该口径未上架前出数不会被采纳，做了也要返工",
            ["先不含券相关口径", "本次只出已上架部分，券部分另行安排", "其他（请补充说明）"],
            "含未治理口径的结果不可对外使用，会导致整份交付返工",
        )

    # 未建模字段
    if "R7" in (state.get("rules", {}).get("mandatory_confirmations") or []):
        add(
            "Q-SCOPE-2", "fields",
            "您提到的手机号、身份证等个人身份信息不在可分析范围内，是否改用会员编号这类不含个人身份的口径？",
            "个人身份信息不对分析开放，属于数据安全要求",
            ["改用会员编号等非敏感标识", "本次不需要个人身份信息", "其他（请补充说明）"],
            "涉及个人身份信息的取数申请无法通过安全审查",
        )

    # 范围：是否排除退款
    if "已退款" in text and not any(w in text for w in ("排除", "不含", "剔除", "除去", "不包含")):
        add(
            "Q-SCOPE-1", "scope",
            "已退款的业务是否计入本次统计？",
            "是否含退款直接影响金额与笔数",
            ["排除已退款", "包含已退款", "按退款前后各出一份", "其他（请补充说明）"],
            "含与不含退款的金额差异，会改变对业绩的判断",
        )

    return {"confirmation_questions": qs, "dropped_questions": dropped}


def _technical_hits(text, tech_terms):
    """闸门：问题里不许出现表名 / 字段名 / SQL 技术语。

    注意大小写与词边界——`dim_member` 这类下划线标识符要能命中，
    中文技术词（表名、字段）按子串命中。
    """
    hits = []
    for t in BANNED_QUESTION_TERMS:
        if t in text:
            hits.append(t)
    low = text.lower()
    for t in tech_terms:
        if len(t) < 3:
            continue
        if re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(t.lower()), low):
            hits.append(t)
    return sorted(set(hits))


# ---------------------------------------------------------------------------
# 组装第一轮分析
# ---------------------------------------------------------------------------
def first_round(bundle, region_label="B"):
    """把六个槽位 + 规则 + 问题组装成第一轮分析结果。"""
    u = understand(bundle)
    # P-B · 把主体候选挂回 bundle，time_semantics() 扫 column_docs 时按主题表优先
    bundle = dict(bundle)
    bundle["subject_candidates"] = u["subject_candidates"]
    g = granularity(bundle, u["subject_candidates"])
    t = time_semantics(bundle)

    state = {"granularity": g, "time": t}
    slots = {
        "subject": {"candidates": u["subject_candidates"]},
        "granularity": {
            "candidates": g["granularity_candidates"],
            "needs_confirmation": g["needs_confirmation"],
            "row_definition_notes": g["row_definition_notes"],
        },
        "time": {
            "candidates": u["time_candidates"] + t["time_field_candidates"],
            "semantics": t["time_semantics"],
            "needs_confirmation": t["need_confirmation"],
            "time_field_filtered": t.get("time_field_filtered"),
        },
        "scope": {"candidates": u["scope_candidates"]},
        "fields": {
            "candidates": u["field_candidates"],
            "miss_reason": u.get("fields_miss_reason"),  # P-C · 非空归因
        },
        "risks": {"candidates": []},
    }
    state["slots"] = slots

    r = rules(bundle, state)
    state["rules"] = r

    # 风险槽位：把规则结论与 1:N / 时间风险统一收口
    risk_cands = []
    for b in r["blocking_risks"]:
        risk_cands.append(
            _cand("risks", b["statement"], b["evidence"], conf=0.9,
                  rule_id=b["rule_id"], severity="blocking", why=b["why"])
        )
    for w in r["warning_risks"]:
        risk_cands.append(
            _cand("risks", w["statement"], w["evidence"], conf=0.5,
                  rule_id=w["rule_id"], severity="warning", why=w["why"])
        )
    for o in g["one_to_many_risks"]:
        risk_cands.append(
            _cand("risks", o["detail"], o["evidence"], conf=0.85, rule_id="R1",
                  severity="blocking")
        )
    for o in t["time_risks"]:
        risk_cands.append(
            _cand("risks", o["detail"], o["evidence"], conf=0.8, rule_id="TIME",
                  severity="blocking")
        )
    # 证据源缺失也要出现在风险列表里——它和规则风险一样，是"不能忽略的事"
    deg = degradation(bundle)
    risk_cands.extend(_degraded_risks(deg))
    slots["risks"]["candidates"] = risk_cands

    q = questions(bundle, state)

    all_conf = []
    for s in slots.values():
        for c in s.get("candidates") or []:
            all_conf.append(c["confidence"])
    overall = round(sum(all_conf) / len(all_conf), 2) if all_conf else 0.0
    # 关键证据源缺失时压低整体置信度：证据不全，结论就不该显得那么有把握。
    # （注意不能靠风险项的 conf=0.9 去抬高均值——那是"我对'我缺证据'这件事很有
    #   把握"，不是"我对结论很有把握"，两者不能混为一谈。）
    degraded_cap = None
    if any(d["severity"] == "blocking" for d in deg):
        degraded_cap = CONF_CAP_DEGRADED
        overall = min(overall, CONF_CAP_DEGRADED)

    return {
        "slots": slots,
        "understanding": u,
        "rule_check": r,
        "questions": q["confirmation_questions"],
        "dropped_questions": q["dropped_questions"],
        "confidence_overall": overall,
        "confidence_cap": degraded_cap,
        "degraded_sources": deg,
        "slot_labels": SLOT_LABELS,
    }
