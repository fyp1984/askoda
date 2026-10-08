# -*- coding: utf-8 -*-
"""证据编排（M3 · P1–P8）

为什么需要这一层
----------------
语义分析最容易出的事，不是「答不上来」，而是**把不同来源的知识混成一锅**：
一句"复购率大概三成"如果是模型随口推断的，和口径说明书里写死的公式，
权重完全不同，但平铺给模型时看起来一样。所以网关必须先按优先级把证据摆好，
再让上层的语义分析只在证据支持的范围内下结论。

本模块只做三件事：**取证据 → 排优先级 → 找冲突**。
不含任何业务判断（那是 `semantics.py` 的事），也不含数据库写入（那是 `analysis.py`）。

优先级阶梯的出处与一处冲突
--------------------------
M3 基线对照指定《最终技术方案》§2.3「证据优先」，其原文为：

    「所有关键判断按 P1–P8 优先级使用证据（当前业务说明 > 业务确认 > 表样 >
      历史案例 > 系统资料 > 数据字典 > 表关系 > 通用规则 > 模型推断）」

**这行原文自身不自洽**：称「P1–P8」（应为 8 级）却并列了 9 项。
本实现按「P1–P8 = 前 8 项、模型推断不编号作兜底」落地（见 `FALLBACK_LEVEL`），
理由是「P1–P8」这一写法在四份文档中出现 5 次以上，且 §4.2 用「P5/P6 证据来源」
反指编号，与 8 级读法一致。

另一处**实质冲突**：《PRD》§7.1 给的是 6 级，且把「已审核知识条目」排在
「历史案例」**之前**——而 §2.3 把 历史案例(P4) 排在 系统资料(P5) 之前。
两者对「口径文档 vs 历史案例谁优先」给出相反答案。本实现按 §2.3 落地，
并在 `M3验收单` 中作为偏离项报出，等评审改判后只需改 `LADDER` 一处。
"""
import re

import db

# ---------------------------------------------------------------------------
# 优先级阶梯（单一可配点）
# ---------------------------------------------------------------------------
LADDER = [
    # (编号, 名称, 取自哪里)
    ("P1", "当前业务说明", "demand_requests 正文"),
    ("P2", "业务确认", "confirmations 已答复记录"),
    ("P3", "表样", "demand_requests.attachments"),
    ("P4", "历史案例", "知识库 · 案例类文档"),
    ("P5", "系统资料", "知识库 · 口径/制度/表结构类文档"),
    ("P6", "数据字典", "PG table_docs / column_docs / business_glossary"),
    ("P7", "表关系", "MDL relationships"),
    ("P8", "通用规则", "内置规则集（Skill 4.7）"),
]

# 不入编号的兜底：P1–P8 全空时才允许出现，且必须显式标为最低置信来源
FALLBACK_LEVEL = ("P9", "模型推断", "LLM（客户端 Agent 侧）")

LEVEL_NAME = {code: name for code, name, _ in LADDER}
LEVEL_NAME[FALLBACK_LEVEL[0]] = FALLBACK_LEVEL[1]
LEVEL_RANK = {code: i for i, (code, _, _) in enumerate(LADDER)}
LEVEL_RANK[FALLBACK_LEVEL[0]] = len(LADDER)

# 知识库文档角色 → 归到 P4 还是 P5。
# 依据：§2.3 把「历史案例」与「系统资料」分列两级，故需按文档角色分流，
# 不能把检索回来的命中一律当同一级。
CASE_DOC_HINTS = ("历史案例", "案例", "复盘", "返工", "问答沉淀", "case")


# ---------------------------------------------------------------------------
# 证据项
# ---------------------------------------------------------------------------
def item(level, source, content, locator=None, **extra):
    """构造一条证据项。字段命名与《最终技术方案》§5.2.2 `evidence_chain` 对齐。"""
    it = {
        "level": level,
        "level_name": LEVEL_NAME.get(level, level),
        "rank": LEVEL_RANK.get(level, 99),
        "source": source,
        "locator": locator,
        "content": content,
    }
    it.update(extra)
    return it


def rank(items):
    """按优先级排序（同级保持给定顺序，便于复现）。"""
    return sorted(items, key=lambda x: x.get("rank", 99))


def group_by_level(items):
    out = {}
    for it in rank(items):
        out.setdefault(it["level"], []).append(it)
    return out


def distinct_source_names(items):
    names = []
    for it in rank(items):
        s = it.get("source") or ""
        if s and s not in names:
            names.append(s)
    return names


# ---------------------------------------------------------------------------
# 领域词表（从证据源反解出来，供命中与槽位提取共用）
# ---------------------------------------------------------------------------
ENUM_RE = re.compile(r"枚举[：:]\s*([^。；\n]+)")
MARKER_RE = re.compile(r"【([^】]+)】")
ROLE_PREFIX_RE = re.compile(r"^(维度退化列|维度|枚举|备注|注意|说明|口径|指标)[：:]")
# 修饰前缀：字段名常带「所属/当日/当月」这类限定，业务说话时不带
MODIFIER_PREFIX_RE = re.compile(r"^(所属|归属|所在|当日|当月|本年度|本年|当前|累计|最近)")
DIGIT_TAIL_RE = re.compile(r"[：:]\s*[0-9/\-]+$")


def clean_label(raw):
    """把字段/表的中文标签压成可用于匹配的词。

    踩过的坑：**先按「；」切分会把括号对劈开**。以
    `会员ID（关联 dim_member；与 stat_month 联合唯一）` 为例，先切分只剩
    `会员ID（关联 dim_member`——带着残缺左括号进了词表，既占位又永远匹配不上。
    所以顺序必须是「先去括号、再切分」。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"[（(][^）)]*[）)]", "", s)  # 成对括号（连同内容）
    s = re.sub(r"[（(][^）)]*$", "", s)  # 未闭合的尾括号
    s = re.sub(r"【[^】]*】", "", s)
    s = re.split(r"[；;，,。]", s)[0]
    s = s.split("=")[0]
    s = ROLE_PREFIX_RE.sub("", s)
    s = DIGIT_TAIL_RE.sub("", s)
    return s.strip("；; ,，。：:、 ")


def _term_variants(raw):
    """把标签变成词表词条（已清洗，故只需过长度与语种闸门）。

    对带修饰前缀的标签额外产出一个**去前缀变体**。理由是子串方向问题：
    字段叫「所属大区」，业务说「按大区汇总」——`text.find("所属大区")` 找不到，
    于是这个词永远匹配不上。实测踩到过。
    """
    s = clean_label(raw)
    if not s:
        return []
    # 只保留含中文的词条：纯 ASCII 的是表名/字段名，业务人员不会这么说话。
    # ⚠️ 不能写成 `re.fullmatch(r"[\w.]+", s)` 取反——Python 的 `\w` 默认是
    #    Unicode 语义，**中文也算 \w**，那样会把全部中文词条误杀。
    #    （2026-09-30 实测踩到：词表被砍到只剩口径句切出来的零星几条。）
    if len(s) < 2 or len(s) > 24:
        return []
    if not re.search(r"[\u4e00-\u9fa5]", s):
        return []

    out = [s]
    m = MODIFIER_PREFIX_RE.match(s)
    if m:
        bare = s[m.end():].strip()
        if 2 <= len(bare) <= 24:
            out.append(bare)
    return out


# P1-14 · 口径表达式词条质量门槛
#
# 列的 description 里除了业务口径，还有大量**说明性套话**。把它们切开当词条会
# 造成两类误命中（均已实测）：
#   · 套话被需求文本命中 → 无关列被拉成字段候选（"表主键之一"→stat_date、
#     "联合唯一"→member_id、"关联"→store_id、"分析一律用此字段"→order_date）
#   · 两字词沦为噪声（"关联"）；且会顶掉枚举身份，把「美妆」这种过滤值升成字段
_CALIBER_MIN_LEN = 3

# `business_glossary` 里 source='mdl' 的行以字段 locator 作 term（如 `表.列`）。
# 用点号 + 两侧均为标识符来识别，避免把真术语（中文或含空格的英文短语）误判。
_MDL_CALIBER_LOCATOR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")

# 需求的**诉求字段**——业务真正在要什么的字段。命中的词落在这里，
# 比只落在 business_context/contact 这类背景说明里，更能代表主体表倾向。
DEMAND_TEXT_FIELDS = frozenset({"title", "description", "expected_output"})
_CALIBER_STOPWORDS = (
    "主键", "唯一", "无其他", "一律", "不建模", "未建模", "便于", "冗余", "取值", "之一",
    "同上", "详见", "参考", "假设", "示例", "本字段", "该字段", "待补", "禁用", "禁止",
    "存当月", "快照", "退化", "免 JOIN",
)


def _is_quality_caliber_phrase(p):
    """这段短语值不值得当词表词条？只放真正的业务词。"""
    p = (p or "").strip()
    if len(p) < _CALIBER_MIN_LEN:
        return False
    if not re.search(r"[\u4e00-\u9fa5]", p):
        return False
    for w in _CALIBER_STOPWORDS:
        if w in p:
            return False
    return True


def build_lexicon(dataset="B"):
    """从元数据字典 + 口径词表构建中文领域词表。

    词表不是手写的——手写词表会和真实 schema 脱节，改了 MDL 却忘了改词表，
    后果是"新字段永远匹配不上"。这里每次从库现读，schema 变则词表跟着变。
    """
    terms = []

    # 口径词表：术语本身就是业务语言（如「复购率」「坪效」）
    for g in db.query(
        "SELECT term, definition, formula, dataset, source FROM business_glossary WHERE dataset=%s OR dataset IS NULL",
        (dataset,),
    ):
        text = g["definition"] or ""
        # source='mdl' 的行不是术语词条，而是 **MDL 口径表达式的镜像**：
        # term 形如 `表.列`（如 `dws_store_daily_agg.area_sqm`），value 是该列的口径句
        # （如「坪效=销售额/面积」），由 `metadata_collect` 从 MDL 灌进来。
        #
        # 踩过的坑（2026-10-07 实测 DR-20261006-17YP）：原先不分来源一律按 glossary
        # 处理，于是
        #   ① `_term_variants` 把 `dws_store_daily_agg.area_sqm` 派生的「门店经营面积」等
        #      变体当**术语**入词表；其中「门店」又把 ads 表拽进主体候选。
        #   ② `_phrases_from_text('坪效=销售额/面积')` 抽出「销售额」，挂在 **area_sqm**
        #      的 locator 上 → 字段候选输出「销售额 -> dws_store_daily_agg.area_sqm」，
        #      把「销售额」指向了**面积**字段，属实打实的误导。
        # 正确的身份是 `caliber_expression`（字段身份的补充说明，P1-14 已定其垫底），
        # 且必须经 `_is_quality_caliber_phrase` 过滤，不能直接当字段。
        if (g["source"] or "") == "mdl" and _MDL_CALIBER_LOCATOR_RE.match(str(g["term"] or "")):
            # 这行的 term 本身就是 `表.列`，据此拆出归属，指标名才能挂到表上
            # （否则 `_tables_of` 反查不到表，这条命中会被丢掉）。
            _loc_table, _loc_col = str(g["term"]).split(".", 1)
            _metric_names = _metric_names_from_text(text)
            for name in _metric_names:
                terms.append(
                    {
                        "term": name,
                        "kind": "glossary",
                        "canonical": g["term"],
                        "label": name,
                        "table": _loc_table,
                        "column": _loc_col,
                        "definition": text,
                        "formula": g["formula"],
                        "level": "P6",
                        "source": "business_glossary",
                        "locator": g["term"],
                    }
                )
            # 其余词组只是口径里的成分说明，身份是 caliber_expression（垫底），
            # 且要过噪声过滤。
            for phrase in _phrases_from_text(text):
                if phrase in _metric_names:
                    continue  # 指标名已按 glossary 入表，不重复
                if _is_quality_caliber_phrase(phrase):
                    terms.append(
                        {
                            "term": phrase,
                            "kind": "caliber_expression",
                            "canonical": g["term"],
                            "label": str(g["term"]).split(".")[-1],
                            "table": _loc_table,
                            "column": _loc_col,
                            "definition": text,
                            "source_caliber": text,
                            "level": "P6",
                            "source": "mdl_caliber_expression",
                            "locator": g["term"],
                        }
                    )
            continue
        for t in _term_variants(g["term"]):
            terms.append(
                {
                    "term": t,
                    "kind": "glossary",
                    "canonical": g["term"],
                    "label": g["term"],
                    "definition": text,
                    "formula": g["formula"],
                    "level": "P6",
                    "source": "business_glossary",
                    "locator": g["term"],
                }
            )
        # 定义句里的核心词组也当词条（如「活跃用户数」「复购用户数」）
        for phrase in _phrases_from_text(text):
            # 过滤**对比语境里的被指对象**——实测踩过的真实案例：
            # 「下单天数频次」的定义写作「统计窗口内有下单的自然日天数。
            #  与购买频次（**订单笔数**）不同，一个会员一天多单只算 1 天。」
            # 括号里的「订单笔数」是**被拿来对比的另一个指标**，却被切成词条
            # 抢先匹配上 `SYNONYMS["订单数"]` 的目标位——于是真正的口语别名
            # 「订单笔数」的`synonym_of` 指向「下单天数频次」，完全指错字段。
            #
            # 判据（两个都算碎片）：短语自身含否定/比较词；或短语在**原句里**
            # 紧跟在括���之后并以「不同/区别」收尾。宁可少收词，也不要指错字段。
            if any(x in phrase for x in ("不是", "也不是", "可指", "不含", "不算",
                                          "而非", "区别", "不同于", "不是同一")):
                continue
            if _in_contrast_context(phrase, text):
                continue
            terms.append(
                {
                    "term": phrase,
                    "kind": "glossary_phrase",
                    "canonical": g["term"],
                    "label": phrase,
                    "definition": text,
                    "level": "P6",
                    "source": "business_glossary",
                    "locator": g["term"],
                }
            )

    import metadata as _md_ev  # noqa: E402

    # 字段中文名 + 口径
    for c in db.query(
        "SELECT table_name, column_name, column_label, data_type, is_sensitive, description "
        "FROM column_docs WHERE dataset=%s",
        (dataset,),
    ):
        label = (c["column_label"] or "").strip()
        descr = (c["description"] or "").strip()
        if c["is_sensitive"]:
            continue  # 未建模字段不进词表：它们对 AI 本就不可见
        _lbl, _cal = _md_ev.split_label_caliber(descr)
        for t in _term_variants(label):
            terms.append(
                {
                    "term": t,
                    "kind": "column",
                    "canonical": "%s.%s" % (c["table_name"], c["column_name"]),
                    "label": label,
                    "table": c["table_name"],
                    "column": c["column_name"],
                    "data_type": c["data_type"],
                    "definition": descr,
                    "caliber": _cal or "",
                    "level": "P6",
                    "source": "column_docs",
                    "locator": "%s.%s" % (c["table_name"], c["column_name"]),
                }
            )
        # P-C · 口径表达式里的业务词条也作为命中源
        # 典型：description = "会员消费金额；口径：近30天完成支付的订单金额合计，按支付日期统计"
        # → 口径里的「近30天」「订单金额」「支付日期」这些会被识别到本字段，而非仅 label "会员消费金额"
        if _cal:
            # 括号里是「美妆/个护/食品/…」这种**取值清单**时，按枚举值入词表，
            # 而不是当口径短语：取值是过滤条件，不是字段名（P1-14）。
            # 只认**斜杠分隔的纯取值清单**（美妆/个护/食品…）。若还夹别的描述
            # （如 "月粒度，存当月1日"）就不是清单，仍按口径短语处理。
            _parts = [clean_label(p) for p in re.split(r"[/／]", _cal)]
            _as_enum = (
                len(_parts) >= 2
                and all(2 <= len(p) <= 6 for p in _parts)
                and all(re.search(r"[\u4e00-\u9fa5]", p) for p in _parts)
                and len(_cal.strip()) <= sum(len(p) for p in _parts) + len(_parts) + 2
            )
            if _as_enum:
                for v in _parts:
                    terms.append(
                        {
                            "term": v,
                            "kind": "enum",
                            "canonical": "%s.%s=%s" % (c["table_name"], c["column_name"], v),
                            "label": v,
                            "table": c["table_name"],
                            "column": c["column_name"],
                            "definition": "取值清单（来自列口径括号）",
                            "level": "P6",
                            "source": "column_docs_caliber_enum",
                            "locator": "%s.%s=%s" % (c["table_name"], c["column_name"], v),
                        }
                    )
            else:
                # 指标名（如「坪效=销售额/面积」里的「坪效」）是真业务词，身份是
                # glossary；其余成分词走 caliber_expression 且要过噪声阈值。
                _metric_names = _metric_names_from_text(_cal)
                for name in _metric_names:
                    terms.append(
                        {
                            "term": name,
                            "kind": "glossary",
                            # canonical 必须带表.列，否则 `_tables_of` 取不到表名
                            # （它靠 `table` 或 `canonical.split('.')[0]` 反查），
                            # 这条命中会被整条丢掉，连带让该表的主体候选消失。
                            # 实测 DR-20261006-IAG7 / K5JI 因此回归。
                            "canonical": "%s.%s" % (c["table_name"], c["column_name"]),
                            "label": name,
                            "table": c["table_name"],
                            "column": c["column_name"],
                            "definition": _cal,
                            "formula": _cal,
                            "level": "P6",
                            "source": "business_glossary",
                            "locator": "%s.%s" % (c["table_name"], c["column_name"]),
                        }
                    )
                for phrase in _phrases_from_text(_cal):
                    if phrase in _metric_names:
                        continue
                    if not _is_quality_caliber_phrase(phrase):
                        continue
                    terms.append(
                        {
                            "term": phrase,
                            "kind": "caliber_expression",
                            "canonical": "%s.%s" % (c["table_name"], c["column_name"]),
                            "label": label,
                            "table": c["table_name"],
                            "column": c["column_name"],
                            "data_type": c["data_type"],
                            "definition": _cal,
                            "source_caliber": _cal,
                            "level": "P6",
                            "source": "column_docs_caliber",
                            "locator": "%s.%s" % (c["table_name"], c["column_name"]),
                        }
                    )
        # 枚举值（门店类型/会员等级/订单状态…）
        m = ENUM_RE.search(label or "") or ENUM_RE.search(descr)
        if m:
            for v in re.split(r"[/／、,，]", m.group(1)):
                # 枚举项同样要过 clean_label——否则「华南大区（无其他取值）」这种
                # 带括号尾注的取值会原样进词表
                v = clean_label(v)
                if len(v) >= 2:
                    terms.append(
                        {
                            "term": v,
                            "kind": "enum",
                            "canonical": "%s.%s=%s" % (c["table_name"], c["column_name"], v),
                            "label": v,
                            "table": c["table_name"],
                            "column": c["column_name"],
                            "definition": "枚举取值",
                            "level": "P6",
                            "source": "column_docs",
                            "locator": "%s.%s" % (c["table_name"], c["column_name"]),
                        }
                    )

    # 表颗粒度里的业务对象词（表注释的中文部分）
    for t in db.query(
        "SELECT table_name, table_label, grain, domain FROM table_docs WHERE dataset=%s",
        (dataset,),
    ):
        terms.append(
            {
                "term": t["table_name"],
                "kind": "table",
                "canonical": t["table_name"],
                "label": t["table_label"] or t["table_name"],
                "table": t["table_name"],
                "grain": t["grain"],
                "domain": t["domain"],
                "level": "P6",
                "source": "table_docs",
                "locator": t["table_name"],
            }
        )

    # 业务口语别名：这类词天然不在 schema 里——业务说「营业额」，字段叫「销售额」，
    # MDL 里永远查不到对应关系，只能人工维护。别名的 locator 仍指向真实字段，
    # 所以不会脱离 schema。业务人员提交的原文里，口语说法远比字段名常见。
    terms.extend(_expand_aliases(terms))

    # 去重：同 term 保留信息量最大的一条
    best = {}
    for t in terms:
        key = t["term"]
        if key not in best or _kind_prio(t) < _kind_prio(best[key]):
            best[key] = t
    return sorted(best.values(), key=lambda x: -len(x["term"]))


# 词条种类优先级：口径术语 > 口径短语 > 字段名 > 表 > 枚举 > 字段口径表达式
#
# 为什么 caliber_expression 现在垫底（P1-14）
# ------------------------------------------
# 早先把它排在 column(2) 之后、enum(4) 之前，结果 description 里写着的枚举取值
# （"美妆/个护/食品…"）被同源的 caliber_expression 顶掉 enum 身份，
# 于是问句里出现「美妆」时被当成**字段候选**（hit_by=mdl_caliber_expression），
# 而它其实是「过滤值」。字段的身份只应由 glossary / column / table 决定，
# 口径表达式只能做最后的补充说明，因此排在枚举之后（数值最大）。
_KIND_PRIO = {"glossary": 0, "glossary_phrase": 1, "column": 2, "table": 3,
              "enum": 4, "caliber_expression": 5}


def _kind_prio(t):
    return _KIND_PRIO.get((t.get("kind") or "").replace("_alias", ""), 9)


# 业务口语 → 标准概念的别名表。
# 键用「标准概念」而非精确词条名：解析时按**包含匹配**定位真实词条（见
# `_expand_aliases`）。若按精确词条名索引，标签写法微调一次（写全「门店经营面积」
# 还是写「经营面积」）整张别名表就失效——那种脆弱性不值得。
SYNONYMS = {
    "销售额": ["营业额", "营收", "销售收入", "销售业绩"],
    "坪效": ["门店效率", "单位面积产出", "每平方米销售额", "每平米销售额"],
    "会员": ["客户", "顾客", "用户"],
    "复购率": ["回购率", "二次购买率", "重复购买率"],
    "订单数": ["单量", "订单量", "订单笔数"],
    "活跃用户数": ["活跃会员数", "活跃人数", "活跃客户数"],
    "订单金额": ["成交金额", "订单总额"],
    "门店经营面积": ["营业面积", "门店面积", "经营面积"],
    "大区": ["区域", "片区"],
}


def _expand_aliases(terms):
    """把口语别名挂到真实词条上；找不到目标的关键词直接跳过（宁可少，不可错）。

    目标词条的选取分两档（实测踩过，两个方向都踩过）
    ----------------------------------------------
    · **优先真实列**（`kind` 以 `column` 开头）——最可靠，别名挂上去一定能落字段。
    · 列里找不到时，才允许用「概念完全相等」的 glossary 词条兜底。
      必须**完全相等**，不能用 `in` 包含匹配：否则 `当月订单数`（column）
      与 `也不是订单数`（glossary_phrase，是整句口径切出的半截短语）
      同时含「订单数」，按长度优先会选中后者——别名就挂到了一个**不是列**
      的词条上，于是「订单笔数」永远召不回真实字段。

    宁可少也不错：两类都找不到就不挂别名，宁可让人工确认，也不要指错字段。
    """
    out = []
    for concept, aliases in SYNONYMS.items():
        real = [
            t for t in terms
            if concept in t["term"]
            and t.get("locator")
            and not (t.get("kind") or "").endswith("_alias")
            and (t.get("kind") or "").startswith("column")
        ]
        exact = [
            t for t in terms
            if t["term"] == concept
            and t.get("locator")
            and not (t.get("kind") or "").endswith("_alias")
        ]
        cands = real or exact
        if not cands:
            continue
        cands.sort(key=lambda x: (_kind_prio(x), len(x["term"])))
        tgt = cands[0]
        for a in aliases:
            out.append(
                {**tgt, "term": a, "synonym_of": tgt["term"], "kind": tgt["kind"] + "_alias"}
            )
    return out


def _metric_names_from_text(text):
    """从口径公式里取**指标名本身**——公式的引导项。

    为什么单独开一条通道（2026-10-07）
    ---------------------------------
    `_is_quality_caliber_phrase` 用「≥3 字」挡掉 2 字噪声（「关联」「链接」这类
    套话），但指标名天然可以是 2 字：「坪效=销售额/面积」「面积」「客单」。
    实测漏掉的就是「坪效」——门禁 evidence_selftest 三项因此变红
    （关键标准词在册／口语别名指向／命中 坪效）。

    认两种结构（都是结构判定，不靠业务词表）：
      ① 等式：`坪效=销售额/面积` → 等号左边是指标名
      ② 否定式并列：`销量≠销售额(金额)≠订单数(笔数)` → 第一项是指标名，
         后面几项是**被排除**的口径。这里第一项只有 2 字（「销量」），
         同样过不了 ≥3 字的噪声阈值。

    而纯描述句里的「关联 dim_member」仍被噪声规则挡住——不靠放宽字数阈值换命中。
    """
    out = []
    for part in re.split(r"[；;，,。\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        # ① 等式左边的指标名
        m = re.match(r"^([A-Za-z\u4e00-\u9fa5_][A-Za-z0-9_\u4e00-\u9fa5]{1,7})[=＝]", part)
        if m:
            name = clean_label(m.group(1))
            if name and re.search(r"[\u4e00-\u9fa5]", name) and name not in out:
                out.append(name)
            continue
        # ② 否定式并列的第一项：`销量≠销售额(金额)≠订单数(笔数)`
        if "≠" in part or "!=" in part:
            first = re.split(r"[≠!]=?", part, maxsplit=1)[0].strip()
            name = clean_label(first)
            if name and re.search(r"[\u4e00-\u9fa5]", name) and name not in out:
                out.append(name)
    return out


def _phrases_from_text(text):
    """从口径定义句中切出 2–8 字的中文名词短语（如「活跃用户数」「复购用户数」）。

    切分符含 `≠`/`!=`：`销量≠销售额(金额)≠订单数(笔数)` 若不切，整句会成为一个
    「销量≠销售额」这种永不命中的伪词条，还会挤掉同名的真词条（实测挤掉「销售件数」，
    导致 dws_store_daily_agg 在主体候选里整个消失）。
    """
    out = []
    for seg in re.split(
        r"[，。；、（）()=＝≠!／/＋\+\-\*：:\s]+", text or ""
    ):
        seg = re.sub(r"^(SUM|COUNT|AVG|MAX|MIN)", "", seg.strip())
        if 2 <= len(seg) <= 8 and re.search(r"[\u4e00-\u9fa5]", seg):
            out.append(seg)
    return out


#: 对比语境标志：短语被这些词引导时，它不是独立术语，而是句子的成分。
#:
#: 实测踩过的真实案例：`下单天数频次` 的定义写作
#: 「统计窗口内有下单的自然日天数。与购买频次（订单笔数）不同，……」
#: 括号里的「订单笔数」是**被对比的另一个指标**，却被切成词条抢先占了
#: `SYNONYMS["订单数"]` 的目标位——真正的口语别名「订单笔数」于是被指向
#: 「下单天数频次」，完全指错字段。
_CONTRAST_CUES = ("不同", "区别", "不同于", "而非", "而不是", "不是", "不等于",
                  "≠", "!=", "相比", "对照", "vs")


def _in_contrast_context(phrase, text):
    """判断短语在原句里是否处于「被对比 / 被引用」的位置。

    三种形态（都算片段，不该成为术语）：
      ① 短语紧跟中文括号之后，句中随后出现「不同/区别」——典型的「与 X（短语）不同」；
      ② 短语前带「不是 / 不同于 / 而非」这类否定引导；
      ③ 短语后紧跟 `≠` / `!=`。

    **判不出来时一律返回 False**（当作正常术语收下）：误收的代价只是多一条
    弱匹配，误弃的代价是把真实术语丢掉——两相比较，宁可误收。
    """
    if not phrase or not text:
        return False
    i = text.find(phrase)
    if i < 0:
        return False

    after = text[i + len(phrase):]

    # ① 形如「（短语）不同」
    if text[max(0, i - 1):i] in ("（", "("):
        tail = after.lstrip("）) 、,，。；;")
        if any(cue in tail[:6] for cue in _CONTRAST_CUES):
            return True

    # ② 形如「不是短语」
    head = text[max(0, i - 4):i]
    if any(cue in head for cue in ("不是", "不同于", "而非", "而不是", "不等于")):
        return True

    # ③ 形如「短语≠…」
    if after[:2] in ("≠", "!="):
        return True
    return False


def match_terms(text, lexicon, min_len=2):
    """在文本中做最长优先、不重叠的词表命中，返回带位置的命中列表。"""
    text = text or ""
    hits, taken = [], []
    for t in lexicon:  # lexicon 已按词长降序
        term = t["term"]
        if len(term) < min_len:
            continue
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            span = (i, i + len(term))
            if not any(not (span[1] <= a or span[0] >= b) for a, b in taken):
                taken.append(span)
                hits.append({**t, "span": span, "matched": term})
            start = i + len(term)
    hits.sort(key=lambda x: x["span"][0])
    return hits


# ---------------------------------------------------------------------------
# 取证据
# ---------------------------------------------------------------------------
def collect(demand=None, demand_id=None, dataset="B", lexicon=None, knowledge_mod=None):
    """按 P1–P8 取证据，返回 {items, by_level, sources, status, matched, text}。

    每个来源单独记录 status——某个来源不可用时必须**显式说明取不到**，
    不能让"取不到"看起来像"没有这条证据"。
    """
    status, items = {}, []
    demand = demand or {}
    fields = ["title", "business_context", "description", "expected_output", "contact"]
    # 诉求字段 vs 背景字段：拼成一段平铺文本会丢掉"这个词出现在哪"。
    # 实测 DR-20261006-17YP（门店累计销量）里，「销售额」「订单数」只出现在
    # business_context 的**对比列举**（"区分销售额/销量/订单数三种口径"），
    # 而「销售件数」出现在 title/description/expected_output——那才是真正的诉求。
    # 两者混在一起当同等证据，主体表就会选错。记下每个字段的偏移区间即可回溯。
    _spans, _parts = {}, []
    _cur = 0
    for f in fields:
        v = str(demand.get(f) or "").strip()
        if v:
            _parts.append(v)
            _spans[f] = (_cur, _cur + len(v))
            _cur += len(v) + 1  # 与下面的 " " join 对齐
    text = " ".join(_parts).strip()
    if demand.get("time_range"):
        text += " " + str(demand["time_range"])

    # ---- P1 当前业务说明 ----
    for f in ("title", "business_context", "description", "expected_output"):
        v = str(demand.get(f) or "").strip()
        if v:
            items.append(
                item("P1", "需求单·%s" % f, v, locator="demand.%s" % f, field=f)
            )
    status["P1"] = {"ok": bool(text), "count": len(items)}

    # ---- P2 业务确认 ----
    n2 = 0
    if demand_id:
        try:
            rows = _current_answers(demand_id)
            for r in rows:
                items.append(
                    item(
                        "P2",
                        "业务确认·%s" % r["question_id"],
                        "%s → %s" % (r["question"], r["answer"]),
                        locator="confirmation:%s.v%d" % (r["question_id"], r["version"]),
                        question_id=r["question_id"],
                        answer=r["answer"],
                        answered_by=r["answered_by"],
                    )
                )
                n2 += 1
            status["P2"] = {"ok": True, "count": n2}
        except Exception as e:  # noqa: BLE001
            status["P2"] = {"ok": False, "count": 0, "error": str(e)[:160]}
    else:
        status["P2"] = {"ok": True, "count": 0, "note": "未给 demand_id，跳过"}

    # ---- P3 表样（附件） ----
    n3 = 0
    for a in demand.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        risk = a.get("risk") or {}
        items.append(
            item(
                "P3",
                "附件·%s" % (a.get("filename") or "未命名"),
                "类型=%s 可识别=%s 敏感风险=%s"
                % (a.get("content_type") or "?", a.get("recognizable"), risk.get("level") or "none"),
                locator="minio:%s" % (a.get("object_key") or "?"),
                filename=a.get("filename"),
            )
        )
        n3 += 1
    status["P3"] = {"ok": True, "count": n3}

    # ---- P4 / P5 知识库文档 ----
    n4 = n5 = 0
    citations_recorded_result = None
    if knowledge_mod is not None and text:
        try:
            ks = knowledge_mod.search(text[:200], top_k=6)
            for c in ks.get("citations") or []:
                name = c.get("document_name") or "未命名文档"
                level = "P4" if any(h in name for h in CASE_DOC_HINTS) else "P5"
                items.append(
                    item(
                        level,
                        "知识库·%s" % name,
                        (c.get("snippet") or "").strip(),
                        locator=c.get("locator"),
                        document_name=name,
                        similarity=c.get("similarity"),
                    )
                )
                if level == "P4":
                    n4 += 1
                else:
                    n5 += 1
            status["P4/P5"] = {
                "ok": True,
                "count": n4 + n5,
                "query": text[:200],
                "fallback_query": ks.get("fallback_query"),
                "documents": ks.get("source_names") or [],
            }
            # P-E · 主链路内部取证检索必须落 knowledge_citations 审计表
            # 「不伪装用户可见引用」实现方式：
            #   1) question 字段显式以 "AUTO_INTERNAL" 开头作 audit 语义解释
            #   2) dataset_id 写 "evidence_collect_stage"，round_no 未确定时 NULL
            #   3) 当且仅当 demand_id 真的传了（非 None 非空）才落表
            cits_auto = ks.get("citations") or []
            if demand_id and cits_auto and hasattr(knowledge_mod, "record_citations"):
                # P1-15 · 幂等：同一需求、同一检索结果只落一次审计行。
                # 指纹含 query + 命中引用 id 集合；内容没变就跳过，避免重跑线性膨胀。
                try:
                    _fp_ev = "q=%s;cits=%s" % (
                        (text or "")[:120],
                        "|".join(
                            sorted(
                                str(c.get("chunk_id") or c.get("document_id") or i)
                                for i, c in enumerate(cits_auto)
                            )
                        ),
                    )
                    if hasattr(knowledge_mod, "record_citations_once"):
                        citations_recorded_result = knowledge_mod.record_citations_once(
                            demand_id,
                            (text or "")[:120],
                            cits_auto,
                            stage="evidence_collect_stage",
                            actor="analysis_pipeline",
                            fingerprint=_fp_ev,
                            round_no=None,
                            sql_run_id=None,
                            dataset_id="evidence_collect_stage",
                        )
                    else:  # 注入的知识模块只有老接口时退回原写法
                        qtext = (
                            "AUTO_INTERNAL_EVIDENCE_COLLECT"
                            " · actor=analysis_pipeline"
                            " · stage=build_evidence_collect_P4P5"
                            " · query=%s"
                        ) % ((text or "")[:140])
                        citations_recorded_result = knowledge_mod.record_citations(
                            demand_id, qtext, cits_auto,
                            round_no=None, sql_run_id=None,
                            dataset_id="evidence_collect_stage",
                        )
                    status["P4/P5"]["citations_written"] = (
                        citations_recorded_result.get("inserted", 0)
                        if isinstance(citations_recorded_result, dict) else 0
                    )
                    if isinstance(citations_recorded_result, dict) and citations_recorded_result.get("skipped"):
                        status["P4/P5"]["citations_skipped"] = citations_recorded_result.get("skipped")
                except Exception:  # noqa: BLE001
                    # 审计表写入失败不影响主链路取证
                    status["P4/P5"]["citations_write_error"] = True
        except Exception as e:  # noqa: BLE001
            status["P4/P5"] = {"ok": False, "count": 0, "error": str(e)[:160]}
    else:
        status["P4/P5"] = {"ok": True, "count": 0, "note": "无检索文本或未启用"}

    # ---- P6 数据字典（定向取证：只取被业务文本命中的术语） ----
    n6 = 0
    hits = match_terms(text, lexicon or []) if lexicon else []
    # 回填每条命中落在哪个需求字段，并标注它是否出现在**诉求字段**里。
    # 诉求字段 = title / description / expected_output（业务真正在要什么）；
    # business_context / contact 是背景说明（常出现"区分 A/B/C 三种口径"这类
    # 对比列举，把所有候选词都提一遍，不代表主体倾向）。
    # 上层 `_demand_field_weight` 用它给命中加权，不在词表层做判断。
    for h in hits:
        span = h.get("span") or (0, 0)
        where = [
            f
            for f, (a, b) in _spans.items()
            if span[0] >= a and span[1] <= b
        ]
        h["demand_fields"] = where
        h["in_demand_text"] = bool(DEMAND_TEXT_FIELDS.intersection(where))
    for h in hits:
        items.append(
            item(
                "P6",
                "元数据字典·%s" % h["source"],
                "%s：%s" % (h["label"] or h["term"], h.get("definition") or ""),
                locator=h.get("locator"),
                term=h["term"],
                canonical=h.get("canonical"),
                kind=h.get("kind"),
                # 颗粒度随证据一起带出：上层据此判断"一行代表什么"，
                # 不必再回查字典（回查会让纯函数层被迫依赖数据库）
                grain=h.get("grain"),
                label=h.get("label"),
            )
        )
        n6 += 1
    status["P6"] = {"ok": True, "count": n6}

    # ---- P7 表关系（只取与命中表相关的关系） ----
    n7 = 0
    try:
        from registry import get as _get_dataset

        ds = _get_dataset(dataset)
        touched_tables = {h.get("table") for h in hits if h.get("table")}
        for r in ds.relationships:
            models = r.get("models") or []
            if not models or not touched_tables or (set(models) & touched_tables):
                items.append(
                    item(
                        "P7",
                        "MDL 关系·%s" % r.get("name"),
                        "%s（%s）" % (" ⟷ ".join(models), r.get("joinType") or "?"),
                        locator="mdl.relationships.%s" % r.get("name"),
                        join_type=r.get("joinType"),
                        condition=r.get("condition"),
                    )
                )
                n7 += 1
        status["P7"] = {"ok": True, "count": n7, "dataset": dataset}
    except Exception as e:  # noqa: BLE001
        status["P7"] = {"ok": False, "count": 0, "error": str(e)[:160]}

    # ---- P8 通用规则 ----
    for r in RULES:
        items.append(
            item("P8", "规则·%s" % r["id"], r["statement"], locator="rules.%s" % r["id"], rule=r)
        )
    status["P8"] = {"ok": True, "count": len(RULES)}

    ranked = rank(items)
    return {
        "items": ranked,
        "by_level": group_by_level(ranked),
        "sources": distinct_source_names(ranked),
        "status": status,
        "matched": hits,
        "text": text,
        "tables": _table_index(lexicon),
        "technical_terms": _technical_terms(dataset),
        "ladder": [{"code": c, "name": n, "from": s} for c, n, s in LADDER],
        "fallback_level": {"code": FALLBACK_LEVEL[0], "name": FALLBACK_LEVEL[1]},
    }


def _table_index(lexicon):
    """表名 → 表级元信息（中文标签 / 颗粒度 / 域）。

    为什么随证据包一起给上层：颗粒度要靠 `table_docs.grain` 判，但**表名本身
    通常不会出现在业务文本里**，所以它不会进 `matched` 命中集——上层若只从命中集
    找颗粒度，永远只能拿到兜底文案（实测踩到过，槽位显示成"X 的一行"）。
    """
    out = {}
    for t in lexicon or []:
        if t.get("kind") == "table":
            out[t["term"]] = {
                "table": t["term"],
                "label": t.get("label"),
                "grain": t.get("grain"),
                "domain": t.get("domain"),
            }
    return out


def _technical_terms(dataset):
    """物理表名与字段名清单，供「确认问题不许含技术词」的闸门使用。

    刻意**包含未建模字段**：闸门要拦的是"这个词出现在给业务看的问题里"，
    与该字段是否对 AI 可见无关——恰恰是敏感字段最不能被写进问题。
    """
    names = set()
    try:
        for r in db.query(
            "SELECT table_name, column_name FROM column_docs WHERE dataset=%s", (dataset,)
        ):
            names.add(r["table_name"])
            names.add(r["column_name"])
        for r in db.query("SELECT table_name FROM table_docs WHERE dataset=%s", (dataset,)):
            names.add(r["table_name"])
    except Exception:  # noqa: BLE001
        pass
    return sorted(n for n in names if n and len(n) >= 3)


def _current_answers(demand_id):
    """取每条问题的**当前版本**答复（版本号最大者）。"""
    return db.query(
        """
        SELECT c.question_id, c.question, c.answer, c.answered_by, c.version
        FROM confirmations c
        JOIN (
            SELECT question_id, max(version) AS v
            FROM confirmations WHERE demand_id = %s
            GROUP BY question_id
        ) m ON m.question_id = c.question_id AND m.v = c.version
        WHERE c.demand_id = %s AND c.answer IS NOT NULL AND c.answer <> ''
        ORDER BY c.question_id
        """,
        (demand_id, demand_id),
    )


# ---------------------------------------------------------------------------
# 通用规则集（P8 · Skill 4.7）
# ---------------------------------------------------------------------------
RULES = [
    {
        "id": "R1",
        "statement": "1:N 关系必须明确处理方式（去重 / 保留明细 / 取代表值），否则行数与金额会被放大",
        "severity": "blocking",
        "detect": "one_to_many",
    },
    {
        "id": "R2",
        "statement": "装载日期不得替代业务日期；分析一律使用业务日期字段",
        "severity": "blocking",
        "detect": "load_date_substitution",
    },
    {
        "id": "R3",
        "statement": "多值字段必须明确顺序与拼接方式",
        "severity": "warning",
        "detect": "multi_value",
    },
    {
        "id": "R4",
        "statement": "未确认的关键口径不得直接进入高置信输出",
        "severity": "blocking",
        "detect": "unconfirmed_caliber",
    },
    {
        "id": "R5",
        "statement": "标记为【待上架】的口径未完成治理，不可用于分析与出数",
        "severity": "blocking",
        "detect": "pending_caliber",
    },
    {
        "id": "R6",
        "statement": "比率类指标的分子分母必须成对出现，分母不得悬空（口径：复购率分母=活跃用户数）",
        "severity": "blocking",
        "detect": "dangling_denominator",
    },
    {
        "id": "R7",
        "statement": "未建模字段对 AI 不可见，不得出现在需求口径或输出字段中",
        "severity": "blocking",
        "detect": "unmodelled_field",
    },
]

RULE_BY_ID = {r["id"]: r for r in RULES}
PENDING_MARK = "【待上架】"
UNMODELLED_MARK = "【未建模"


# ---------------------------------------------------------------------------
# 冲突检测
# ---------------------------------------------------------------------------
def _source_specificity(source):
    """同级时的来源具体性：具体列 > 表级 > 术语词条 > 其他。

    排在前面的更「具体」，同级冲突时优先采信（避免用泛化词条压过真实字段）。
    """
    s = str(source or "")
    if "column_docs" in s or "table_docs" in s:
        return 0
    if "business_glossary" in s:
        return 1
    if "glossary" in s:
        return 2
    return 3


#: 同级冲突的 tiebreak 键序（同时用于 `_tiebreak_key` 与 note 文案，两者不许各写一份）
_TIEBREAK_FIELDS = ("置信度", "实词命中数", "区分度", "命中词数", "来源具体性")
_TIEBREAK_DESC = "→".join(_TIEBREAK_FIELDS)


def _tiebreak_key(c):
    """(优先级, -置信度, -实词命中数, -区分度, -命中词数, 来源具体性)。

    LEVEL_RANK 越小优先级越高；其后各项只在**同优先级**内起作用。

    「实词命中数」排在区分度之前：靠 2 个泛化派生词凑够 `GENERIC_ONLY_MIN`
    才入围的表（strong=0），证据强度低于有一条例实词命中的表（strong≥1）。

    用**命中词数**而不是 hit_count：hit_count 统计的是"命中在需求里出现了几次"，
    反映用词频次而非证据强度。实测 DR-20261006-17YP 中 ads 的「门店」在诉求里
    出现 4 次（hit=6），而真正持有 sales_qty 的 dws 只命中 1 次（hit=2）——
    按 hit_count 判胜负恰好选反。按词数则两表都是 2，如实进入打平分支。
    """
    return (
        LEVEL_RANK.get(c.get("level"), 99),
        -(c.get("confidence") or 0.0),
        -(c.get("strong_hits") or 0),
        -(c.get("discriminative_score") or 0.0),
        -len(c.get("matched_terms") or ()),
        _source_specificity(c.get("source")),
    )


def detect_conflicts(claims):
    """在同一槽位上，若不同优先级来源给出不同结论，判定为口径冲突。

    claims: [{slot, value, level, source, locator, confidence?, hit_count?, discriminative_score?}]
    返回 [{slot, winner, loser, winner_level, loser_level, note, tiebreak}]

    规则：**低优先级不得覆盖高优先级**（§2.3），冲突本身必须报出而不是静默择一。

    2026-10-07 修复：原先只按 LEVEL_RANK 排序，同优先级时 `sorted` 稳定排序
    等于「取先出现的那条」= 任意；而 note 又无条件写死「低优先级不得覆盖高优先级」，
    在同级场景下这句话本身就是错的，属于误导性输出（实测主体槽位因此选错表）。
    现改为：同优先级用 置信度 → 实词命中数 → 区分度 → 命中数 → 来源具体性 做确定性
    tiebreak，并让 note 如实说明是「高优先级覆盖」「同级 tiebreak」还是
    「各项打平、需业务确认」。

    打平时 `winner` 字段仍填排序首位（保持既有返回结构与下游读取不变），
    但 `tiebreak="ambiguous_needs_confirmation"` + `needs_confirmation=True`
    会明确告诉调用方：这个 winner **不是结论**，别当既定答案用。
    """
    by_slot = {}
    for c in claims:
        by_slot.setdefault(c["slot"], []).append(c)

    conflicts = []
    for slot, group in by_slot.items():
        distinct = {}
        for c in group:
            distinct.setdefault(_norm(c["value"]), []).append(c)
        if len(distinct) < 2:
            continue
        ordered = sorted(group, key=_tiebreak_key)
        winner = ordered[0]
        for norm, members in distinct.items():
            if norm == _norm(winner["value"]):
                continue
            loser = min(members, key=_tiebreak_key)
            same_level = LEVEL_RANK.get(winner["level"], 99) == LEVEL_RANK.get(loser["level"], 99)
            needs_conf = same_level and (
                winner.get("ambiguous") or loser.get("ambiguous")
                or _tiebreak_key(winner) == _tiebreak_key(loser)
            )
            if needs_conf:
                # 证据完全打平：分不出高下。此时**不能**靠列表顺序静默定赢家
                # （那正是本次修复前的原始缺陷），按 R4 如实报为需业务确认。
                note = (
                    "%s 与 %s 同为 %s 级，且「%s」各项信号完全相同、无法区分高下；"
                    "按 R4「未确认关键口径不得进入高置信输出」，需业务确认后再定，"
                    "此处仅列出候选：%s（%s）／%s（%s）"
                    % (
                        winner["level"],
                        loser["level"],
                        winner["level"],
                        _TIEBREAK_DESC,
                        winner["value"],
                        winner["source"],
                        loser["value"],
                        loser["source"],
                    )
                )
                tiebreak = "ambiguous_needs_confirmation"
            elif same_level:
                note = (
                    "%s 与 %s 同为 %s 级、无优先级差异，按「%s」tiebreak，"
                    "取 %s（%s）"
                    % (
                        winner["level"],
                        loser["level"],
                        winner["level"],
                        _TIEBREAK_DESC,
                        winner["value"],
                        winner["source"],
                    )
                )
                tiebreak = "same_level_tiebreak"
            else:
                note = "%s（%s）覆盖 %s（%s）：低优先级不得覆盖高优先级" % (
                    winner["level"],
                    winner["source"],
                    loser["level"],
                    loser["source"],
                )
                tiebreak = "level_precedence"
            conflicts.append(
                {
                    "slot": slot,
                    "winner": winner["value"],
                    "winner_level": winner["level"],
                    "winner_source": winner["source"],
                    "loser": loser["value"],
                    "loser_level": loser["level"],
                    "loser_source": loser["source"],
                    "note": note,
                    "tiebreak": tiebreak,
                    # 打平时为 True：winner 只是排序首位，**不是结论**，须业务确认
                    "needs_confirmation": needs_conf,
                }
            )
    return conflicts


def _norm(v):
    return re.sub(r"\s+", "", str(v or "")).lower()
