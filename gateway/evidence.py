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
                    "level": "P6",
                    "source": "column_docs",
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


# 词条种类优先级：口径类 > 字段类 > 表类 > 枚举。用于同名词条去重。
_KIND_PRIO = {"glossary": 0, "glossary_phrase": 1, "column": 2, "table": 3, "enum": 4}


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
    """把口语别名挂到真实词条上；找不到目标的关键词直接跳过（宁可少，不可错）。"""
    out = []
    for concept, aliases in SYNONYMS.items():
        cands = [
            t
            for t in terms
            if concept in t["term"] and t.get("locator") and not (t.get("kind") or "").endswith("_alias")
        ]
        if not cands:
            continue
        cands.sort(key=lambda x: (_kind_prio(x), len(x["term"])))
        tgt = cands[0]
        for a in aliases:
            out.append(
                {**tgt, "term": a, "synonym_of": tgt["term"], "kind": tgt["kind"] + "_alias"}
            )
    return out


def _phrases_from_text(text):
    """从口径定义句中切出 2–6 字的中文名词短语（如「活跃用户数」「复购用户数」）。"""
    out = []
    for seg in re.split(r"[，。；、（）()=＝/／\+\-\*：:\s]+", text or ""):
        seg = re.sub(r"^(SUM|COUNT|AVG|MAX|MIN)", "", seg.strip())
        if 2 <= len(seg) <= 8 and re.search(r"[\u4e00-\u9fa5]", seg):
            out.append(seg)
    return out


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
    text = " ".join(str(demand.get(f) or "") for f in fields).strip()
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
        except Exception as e:  # noqa: BLE001
            status["P4/P5"] = {"ok": False, "count": 0, "error": str(e)[:160]}
    else:
        status["P4/P5"] = {"ok": True, "count": 0, "note": "无检索文本或未启用"}

    # ---- P6 数据字典（定向取证：只取被业务文本命中的术语） ----
    n6 = 0
    hits = match_terms(text, lexicon or []) if lexicon else []
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
def detect_conflicts(claims):
    """在同一槽位上，若不同优先级来源给出不同结论，判定为口径冲突。

    claims: [{slot, value, level, source, locator}]
    返回 [{slot, winner, loser, winner_level, loser_level, note}]
    规则：**低优先级不得覆盖高优先级**（§2.3），冲突本身必须报出而不是静默择一。
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
        ordered = sorted(group, key=lambda x: LEVEL_RANK.get(x["level"], 99))
        winner = ordered[0]
        for norm, members in distinct.items():
            if norm == _norm(winner["value"]):
                continue
            loser = members[0]
            conflicts.append(
                {
                    "slot": slot,
                    "winner": winner["value"],
                    "winner_level": winner["level"],
                    "winner_source": winner["source"],
                    "loser": loser["value"],
                    "loser_level": loser["level"],
                    "loser_source": loser["source"],
                    "note": "%s（%s）覆盖 %s（%s）：低优先级不得覆盖高优先级"
                    % (winner["level"], winner["source"], loser["level"], loser["source"]),
                }
            )
    return conflicts


def _norm(v):
    return re.sub(r"\s+", "", str(v or "")).lower()
