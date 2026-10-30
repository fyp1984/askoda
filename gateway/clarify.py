"""生成失败时的「退回需求方补充」包（CR010）。

产品判断（2026-10-30 西北人）
------------------------------
需求说不清时，让提出人补一句话（分钟级、一个人）远比去知识库补口径
（小时/天级、跨部门立项）便宜。所以缺口处置的第一顺位是**退回需求**，
第二顺位才是补知识库，第三顺位是技术建模接数据源。

本模块只负责「把缺什么说成人话」——生成待明确项与补充建议；
写入与状态流转在 :mod:`demand` 与 :mod:`app` 里，本模块不碰 DB。

设计约束
--------
1. **每条待明确项必须有原文证据**（``anchor`` 字段）。扫不到证据就不生成，
   宁可少问，不可凭空造问题让需求方做无效劳动。
2. **只问需求方能答的**。「表不存在」「字段未建模」这类不生成问句，
   直接判给技术侧——问需求方等于浪费一轮来回。
3. **不问已知项**。已确认的要素进 ``already_known``，需求方不必重复劳动。
4. 「一键采纳」要有落点：``suggested_description`` 是可直接写回需求描述
   的草稿，不是一段空话建议。
"""

import datetime as dt
import re

# ---------------------------------------------------------------------------
# 业务术语 → 待明确项模板
# ---------------------------------------------------------------------------
# 这些词一旦出现、且文本里没有给出定义，就会产生一条「定义待明确」。
# 选项写法遵循一个原则：**给业务方选，而不是让他写**——自由文本门槛高得多。
TERM_TEMPLATES = {
    "新增": {
        "question": "「新增」按哪个口径算？",
        "why": "同一个词至少有三种算法，结果可能差一倍以上",
        "options": [
            "首次注册（以注册日期落在区间内为准）",
            "首次下单（以首笔订单时间为准）",
            "首次产生任意业务行为",
        ],
        "example": "新增会员 = 统计区间内首次注册的会员，以注册日期为准",
        "affects": "决定主体表与 WHERE 条件",
    },
    "活跃": {
        "question": "「活跃」的判定标准是什么？",
        "why": "没有阈值就无法筛人，不同阈值下人数差一个量级",
        "options": [
            "区间内有 ≥1 笔订单",
            "区间内有 ≥N 笔订单（N 需指定）",
            "区间内有登录/访问行为",
        ],
        "example": "活跃会员 = 统计区间内至少产生 1 笔有效订单的会员",
        "affects": "决定是否需要行为表与筛选阈值",
    },
    "流失": {
        "question": "「流失」怎么定义？",
        "why": "流失是「多久没来」的判断题，天数不定则无法计算",
        "options": ["连续 N 天无订单（N 需指定）", "连续 N 天无登录", "会员状态标记为流失"],
        "example": "流失会员 = 距统计日连续 90 天无有效订单的会员",
        "affects": "决定时间窗口与主体筛选",
    },
    "复购": {
        "question": "「复购」按哪个口径算？",
        "why": "按订单数还是按品类算，得到的复购率完全不同",
        "options": [
            "区间内下单 ≥2 次",
            "跨月下单 ≥2 个自然月",
            "购买过 ≥2 个不同品类",
        ],
        "example": "复购会员 = 统计区间内有效订单数 ≥2 的会员",
        "affects": "决定是否需去重计数与跨期口径",
    },
    "高价值": {
        "question": "「高价值」按什么标准划分？",
        "why": "没有分档标准就没法分组统计",
        "options": [
            "累计消费金额 Top 20%",
            "累计消费金额 ≥ 固定阈值（需指定金额）",
            "按现有会员等级字段划分",
        ],
        "example": "高价值会员 = 统计区间内累计消费金额排名前 20% 的会员",
        "affects": "决定分组方式与排序规则",
    },
    "有效": {
        "question": "「有效」是指排除了哪些单据？",
        "why": "退款、作废、取消单是否计入，直接影响金额与单量",
        "options": ["排除已退款/退货", "排除作废与取消单", "全部计入，不做排除"],
        "example": "有效订单 = 排除已退款、已作废与已取消的订单",
        "affects": "决定 WHERE 中的排除条件",
    },
}

# 需求文本里出现这些词，说明口径本身已经有明确定义（不必再问）
_DEFINITION_MARKERS = ("以…为准", "以", "定义为", "指的是", "即", "口径为", "口径是")

# 时间范围是否已明确
# ⚠️ 必须匹配**真实时间表达**，不能用单字（「月度例会」里的「月」会误判成
#    已给时间范围）。这与 reqelem 里单字「月」误命中的坑是同一类，见 OPEN_ITEMS。
_TIME_PATTERNS = (
    r"\d{4}\s*年",
    r"\d{4}\s*[-/.]\s*\d{1,2}",
    r"近\s*\d+\s*(天|日|周|个月|月|年)",
    r"(本|上|下|去|今|明)(月|季度|周|年)",
    r"年初至今|YTD|至今",
    r"统计(区间|期间|范围)",
)

# 输出维度是否已明确
_DIM_MARKERS = ("各", "每", "按", "分", "一行", "维度", "排名", "前", "Top", "top")

# 输出字段是否已点名（没点名就只会在"表单/图表"这一层打转）
_FIELD_MARKERS = (
    "数量", "数", "金额", "名称", "编号", "率", "占比", "明细", "字段", "列",
)

# 需求主体 → (示例粒度, 维度选项)。示例会被「一键采纳」写进需求，必须贴合主体。
_SUBJECT_HINTS = (
    ("会员", "一个会员（按会员去重）", ["逐条会员明细", "按门店汇总", "按会员等级汇总", "按注册月份看趋势"]),
    ("订单", "一笔订单", ["逐条订单明细", "按门店汇总", "按日期汇总", "按品类汇总"]),
    ("门店", "一家门店", ["按门店汇总", "按区域汇总", "按日期趋势"]),
    ("商品", "一个商品/SKU", ["按商品汇总", "按品类汇总", "按日期趋势"]),
    ("券", "一张券", ["按券类型汇总", "按核销状态汇总", "按日期趋势"]),
    ("员工", "一名员工", ["按员工明细", "按部门汇总"]),
)

# 金额类需求：需问是否排除无效单据
_AMOUNT_MARKERS = ("金额", "销售额", "订单", "交易", "消费", "营收", "GMV")

# 排序类需求：需问并列规则
# ⚠️ 「前」不能单字判定：「当前」「全部」里都含「前」，会把普通需求误判成 TopN 需求。
_SORT_PATTERNS = (r"前\s*\d+", r"前[一二三四五六七八九十]+", r"[Tt]op\s*\d+", r"排名", r"降序", r"升序")


def _text_of(demand):
    """把需求里参与判定的文本拼起来（只看能问出问题的字段）。"""
    parts = []
    for k in ("title", "business_context", "description", "expected_output", "time_range"):
        v = demand.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    return " ｜ ".join(parts)


def _slot_value(req, key):
    """从结构化需求里取槽位的可读值（兼容 dict / list / str 三种形状）。"""
    if not isinstance(req, dict):
        return ""
    v = req.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, list):
        vals = []
        for x in v:
            if isinstance(x, dict):
                x = x.get("value")
            if x and "无候选结论" not in str(x):
                vals.append(str(x))
        return "；".join(vals)
    if v is None:
        return ""
    s = str(v)
    return "" if "无候选结论" in s else s


def _has_definition(text, term):
    """文本里有没有给该术语下定义（避免重复问已说清的口径）。"""
    idx = text.find(term)
    if idx < 0:
        return False
    window = text[idx: idx + 60]
    return any(m in window for m in _DEFINITION_MARKERS)


def _item(slot, question, why, options, example, affects, anchor, required=True,
          term=""):
    """造一条待明确项；``anchor`` 是触发它的原文片段，用于自证不瞎问。"""
    return {
        "id": "Q-%s-%s" % (slot, abs(hash(question)) % 10000),
        "slot": slot,
        "question": question,
        "why": why,
        "options": list(options),
        "example": example,
        "affects": affects,
        "anchor": anchor,
        "required": required,
        "term": term,
    }


def _scan_terms(text, req):
    """扫未定义的业务术语。"""
    items = []
    for term, tpl in TERM_TEMPLATES.items():
        if term not in text:
            continue
        if _has_definition(text, term):
            continue
        # 结构化需求若已给该槽位结论，说明业务已对齐过，不再问
        gran = _slot_value(req, "granularity")
        if gran and term in gran:
            continue
        items.append(
            _item(
                slot="指标定义",
                question=tpl["question"],
                why=tpl["why"],
                options=tpl["options"],
                example=tpl["example"],
                affects=tpl["affects"],
                anchor=text[max(0, text.find(term) - 12): text.find(term) + 18],
                term=term,
            )
        )
    return items


def _scan_time(demand, text, req):
    """统计区间没写清 ⇒ 一条待明确项。

    ⚠️ 「时间字段口径」≠「统计区间」。``time_semantics`` 常给出「按数据加工入库
    当天」这类**用哪个字段**的答复，它并不回答**统计哪一段**。早先只要该槽位非空
    就认定区间已明确，于是「汇总全部会员」这种需求被漏问时间范围（2026-10-30 实测）。
    故此处只认**真实的区间表达**。
    """
    tr = (demand.get("time_range") or "").strip()
    if tr:
        return []
    ts = _slot_value(req, "time_semantics")
    if ts and any(re.search(p, ts) for p in _TIME_PATTERNS):
        return []
    if any(re.search(p, text) for p in _TIME_PATTERNS):
        return []
    return [
        _item(
            slot="统计区间",
            question="统计哪一段时间的数据？",
            why="没有区间就只能全量扫描，结果也不能与往期对比",
            options=["本月", "上月", "本季度", "今年至今", "自定义区间"],
            example="2026-01-01 至 2026-01-31",
            affects="决定 WHERE 中的时间条件",
            anchor="（需求单未填「时间范围」，正文也未出现任何时间区间）",
        )
    ]


def _scan_dimension(demand, text, req):
    """输出维度没写清 ⇒ 一条待明确项。

    ⚠️ 示例必须跟着需求主体走。早先固定写「一家门店」，于是「汇总会员信息」这类
    需求拿到的补充草稿变成「结果一行 = 一家门店」——把业务方往错的方向带
    （2026-10-30 实测）。示例是可以被一键采纳的，写错就是主动误导。
    """
    out = (demand.get("expected_output") or "").strip()
    if out and any(m in out for m in _DIM_MARKERS):
        return []
    of = _slot_value(req, "output_fields")
    if of:
        return []
    hint = next((h for h in _SUBJECT_HINTS if h[0] in text), None)
    example = "结果一行 = %s" % (hint[1] if hint else "一条明细记录（或按某维度汇总后一行）")
    options = list(hint[2]) if hint else ["按门店汇总", "按日期趋势", "逐条明细"]
    return [
        _item(
            slot="输出维度",
            question="结果一行代表什么？按什么维度看？",
            why="一行是「一个会员」还是「一家门店」，决定分组方式与行数",
            options=options,
            example=example,
            affects="决定 GROUP BY 与明细粒度",
            anchor="（输出预期为空或未说明分组维度）",
        )
    ]


def _scan_output_fields(demand, text=""):
    """输出预期只说了"表单/图表"，没说要有哪些字段 ⇒ 一条待明确项。"""
    out = (demand.get("expected_output") or "").strip()
    if not out:
        return []
    if any(m in out for m in _FIELD_MARKERS):
        return []
    subj = next((h[0] for h in _SUBJECT_HINTS if h[0] in text), "对象")
    return [
        _item(
            slot="输出字段",
            question="结果里要有哪几列？",
            why="只说「表单/图表」无法确定取哪些列，容易多取敏感字段或漏关键指标",
            options=["名称 + 数量", "名称 + 数量 + 占比", "逐字段明细", "先给我看明细再定"],
            example="%s名称、数量、占比" % subj,
            affects="决定 SELECT 列与是否触碰敏感字段",
            anchor=out[:40],
        )
    ]


def _scan_exclusion(text):
    """金额类需求未声明排除项 ⇒ 一条待明确项。"""
    if not any(m in text for m in _AMOUNT_MARKERS):
        return []
    if any(m in text for m in ("退款", "退货", "作废", "取消", "无效", "有效")):
        return []
    return [
        _item(
            slot="排除项",
            question="是否排除退款、作废、取消的单据？",
            why="是否排除会让金额与单量出现差额，且事后难以解释",
            options=["排除退款/退货", "排除作废与取消", "全部计入不排除"],
            example="仅含有效订单，排除已退款、已作废、已取消",
            affects="决定 WHERE 中的单据状态过滤",
            anchor=text[:40],
        )
    ]


def _scan_sort(text):
    """有排序/取前 N 但未说明并列规则 ⇒ 一条待明确项。"""
    if not any(re.search(p, text) for p in _SORT_PATTERNS):
        return []
    if "并列" in text:
        return []
    return [
        _item(
            slot="排序规则",
            question="排名并列时怎么处理？",
            why="并列不定义时，取前 N 的结果不可复现",
            options=["并列全部保留", "并列时按第二个指标再排", "并列任取，需注明"],
            example="排名并列时按第二指标再排，并在结果中注明排序依据",
            affects="决定 ORDER BY 与 LIMIT 的稳定性",
            anchor=text[:40],
            required=False,
        )
    ]


def _scan_caliber_owner(gap_kind):
    """口径本身缺登记时，追一条「以谁为准」——即使需求已写清也值得确认。"""
    if gap_kind not in ("口径未建模", "口径未定义"):
        return []
    return [
        _item(
            slot="口径归属",
            question="这个口径以哪个部门/文档的版本为准？",
            why="同名词在不同部门常有不同算法，先定归属才能落库",
            options=["以业务部门现行口径为准", "以财务口径为准", "暂无标准，本次临时定义"],
            example="本口径以业务部门《会员运营指标口径》为准",
            affects="决定是否需要在知识库登记标准口径",
            anchor="（系统内未检索到该口径的登记记录）",
            required=False,
        )
    ]


def _known_items(req, demand):
    """把已确认的要素列出来，免得需求方重复劳动。"""
    known = []
    mapping = (
        ("subject", "分析主体"),
        ("granularity", "颗粒度"),
        ("time_semantics", "时间口径"),
        ("data_scope", "范围条件"),
        ("output_fields", "输出字段"),
    )
    for key, label in mapping:
        v = _slot_value(req, key)
        if v:
            known.append({"label": label, "value": v[:120]})
    tr = (demand.get("time_range") or "").strip()
    if tr:
        known.append({"label": "需求单时间范围", "value": tr})
    return known


def decide_route(gap_kind, items):
    """定处置路径：需求侧 > 知识侧 > 技术侧（成本从低到高）。

    - 只要有「必须回答」的待明确项 ⇒ 退回需求方补充（分钟级）
    - 没有必答项、缺的是数据/字段   ⇒ 技术侧建模
    - 其余（口径未登记）            ⇒ 补知识库
    """
    if any(i.get("required") for i in items):
        return "demand", "需求描述里缺关键信息，补上即可出数（最快）"
    if gap_kind == "数据缺失":
        return "engineering", "所需数据/字段在本库不存在，补需求也解决不了"
    return "knowledge", "需求已说清，但该口径未在知识库登记"


def _suggest_description(items):
    """把待明确项的示例拼成可直接写回需求的草稿。"""
    lines = []
    for i in items:
        if i.get("example"):
            lines.append("%s：%s" % (i.get("slot"), i["example"]))
    return "\n".join(lines)


def build(demand, requirement=None, dataset="B", gap_kind="", gap_text=""):
    """组装退回包。

    参数
    ----
    demand : dict   需求单（demand.get 的返回）
    requirement : dict | None  结构化需求（requirement.get 的返回），可缺
    dataset : str   数据集键
    gap_kind : str  缺口类型（口径未建模 / 数据缺失 / 口径未定义 / 口径待上架）
    gap_text : str  缺口原文，供展示与自证
    """
    text = _text_of(demand)
    req = requirement if isinstance(requirement, dict) else {}
    items = []
    items += _scan_terms(text, req)
    items += _scan_time(demand, text, req)
    items += _scan_dimension(demand, text, req)
    items += _scan_output_fields(demand, text)
    items += _scan_exclusion(text)
    items += _scan_sort(text)
    items += _scan_caliber_owner(gap_kind)

    # 去重：同一 slot 只留第一条（先扫到的更贴近原文）
    seen, uniq = set(), []
    for it in items:
        key = (it["slot"], it.get("term") or it["question"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    items = uniq

    route, route_reason = decide_route(gap_kind, items)
    required_n = sum(1 for i in items if i.get("required"))
    if route == "demand":
        headline = "这条需求还差 %d 项关键信息，补齐后即可出数" % required_n
    elif route == "engineering":
        headline = "所需数据不在当前库，需技术侧接入数据源"
    else:
        headline = "需求已写清，但该口径尚未登记——补口径文档或先向业务方确认"

    return {
        "demand_id": demand.get("demand_id"),
        "dataset": dataset,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "gap_kind": gap_kind or "口径未建模",
        "gap_text": gap_text or "",
        "route": route,
        "route_reason": route_reason,
        "headline": headline,
        "required_count": required_n,
        "items": items,
        "already_known": _known_items(req, demand),
        "suggested_description": _suggest_description(items),
        "note": (
            "退回不是打回重做：只需回答下列问题或直接把示例口径写进需求描述，"
            "系统会重新分析。知识库补口径属于长期治理，不必为单条需求先做。"
        ),
    }


def compose_supplement(answers, extra_description=""):
    """把需求方的作答拼成一段可写回需求描述的「补充说明」。

    ``answers`` 形如 ``[{"question": ..., "answer": ..., "choice": ...}]``。
    """
    lines = []
    if extra_description and extra_description.strip():
        lines.append(extra_description.strip())
    for a in answers or []:
        ans = (a.get("answer") or a.get("choice") or "").strip()
        if not ans:
            continue
        q = (a.get("question") or "").strip()
        lines.append("- %s：%s" % (q, ans) if q else "- %s" % ans)
    if not lines:
        return ""
    return "\n\n【需求补充】\n" + "\n".join(lines)
