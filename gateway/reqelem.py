# -*- coding: utf-8 -*-
"""REQ-COVERAGE 阶段二 · 第 3 步（实例层）：需求要素抽取

本模块是什么
------------
底座（``gateway/reqcov.py``）判「要素被 SQL 覆盖了没有」；**本模块产出被判的那些要素**。

要素只有一个权威来源：**需求原文**——需求方自己写的字。
表级元数据登记的是"表长什么样"，不是"业务方要什么"；
拿元数据当需求，就是阶段一那个病的根源（要素清单天生漏项 ⇒ 漏掉的要素
连清单里都没有，谈不上"被 SQL 丢掉"）。

为什么不能只靠元数据词表
------------------------
实测：真实 161 条词表对「退款额」「月度」**零命中**（只命中 门店/销售额/订单数）。
只靠词表命中 ⇒ 用例 1「逐项列出缺失」根本不成立 ⇒ 必须有一条兜底路径：
把原文里词表没命中、但需求方确实说了的业务名词捞出来。

本模块做四件事
--------------
1. ``sentence_split()``  —— 原文切句（带偏移），provenance 的「第N句」由此而来；
2. ``SlotMapper``        —— M3 六槽位 → 要素候选（槽位口径走 ``requirement.SLOT_MAP``）；
3. 未登录业务名词兜底提取；
4. ``polarity`` 三值判定（must_cover / may_omit×2）。

⚠️ 本模块**不碰** ``gateway/evidence.py``（M3 公共模块，零侵入）。
"""
import re
import sys

import requirement as requirement_mod
import semantics as semantics_mod

#: 期间/时点遮蔽，直接复用语义层那一道闸门（不另立第三套时间词表）。
#: 单独起个本地名，是为了让下面 :meth:`SlotMapper._fragments` 的意图自明：
#: 这里的调用不是"顺便借用"，而是兜底路径**必须**过的关。
_mask_period_spans = semantics_mod.mask_period_spans

#: 单字时间词（"月""日""天"…）左边的**时点指示字**——照抄语义层那一道闸门，
#: 不另立第四套。左边有它就不是指标（"上月""当日""昨天"），左边没有才是粒度。
_POINTING_PREFIX_CHARS = semantics_mod.GRAIN_PREV_REJECT

#: 兜底路径要额外排掉的时点/期间词：PERIOD_RE 认的是"期间"（近6月/本月/同比），
#: 「当日」「昨日」「上个月」这类**时点**它不认，可它们照样会混进碎片
#: （"当日"过了业务名词形态那一关，"上个月"以"个"收尾也在集合里）。
#: 这里补的是 PERIOD_RE 的盲区，不是另立一套——判据仍是"左字是时点指示字"，
#: 且整词只有时点字 + 方位字构成，没有任何业务名（"上月销售额"是业务名，不排）。
_POINTING_ONLY_RE = re.compile(
    r"^(?:当|昨|今|明|次|前|上|本|近|这|那)(?:个)?[日月天年季](?:初|中|末|间|里|份)?$"
)

# ---------------------------------------------------------------------------
# 契约镜像（底座 ``reqcov`` 的冻结契约在本模块的离线投影）
# ---------------------------------------------------------------------------
#: 8 字段要素契约的字段名。权威定义在底座 ``reqcov.CONTRACT_FIELDS``；
#: 本处是同一份契约的离线投影，供底座不可导入时（无 sqlglot 的解释器）也能用。
#: ``tools/semantics_selftest.py`` 用 AST 直读底座源码**逐字段核对**——漂移即红。
CONTRACT_FIELDS = (
    "element",
    "slot",
    "provenance",
    "confidence",
    "needs_confirmation",
    "mandatory",
    "polarity",
    "matcher",
)

#: polarity 取值域（底座 ``reqcov.POLARITIES`` 的离线投影）
POLARITY_MUST_COVER = "must_cover"
POLARITY_MAY_OMIT = "may_omit"

#: 底座已注册的 matcher kind —— 粒度类由底座判（类别级比对，不会误报）
MATCHER_KIND_TIME_GRANULARITY = "time_granularity"

#: 实例层自有的 matcher kind。语义一律**保守**：
#: 能确认已覆盖 ⇒ 已覆盖；确认不了 ⇒ 不判（绝不报"未覆盖"）。
#: 理由：业务名↔列的映射在实例层还不可靠，误报缺口会变成假拦截，
#: 而假拦截比漏报更危险（团队会因天天红而绕过门禁）。
MATCHER_KIND_FIELD = "field_present"
MATCHER_KIND_TIME = "time_caliber"
MATCHER_KIND_SCOPE = "scope_condition"
MATCHER_KIND_SUBJECT = "subject_table"
MATCHER_KIND_SLOT = "slot_requirement"

# ---------------------------------------------------------------------------
# 槽位口径
# ---------------------------------------------------------------------------
#: 不参与要素清单的槽位：残余风险是"结论"不是"要求"，把它当要素会让门禁
#: 拿风险条目当缺口去比对 SQL，语义就是错的。
#: ⚠️ ``aggregation_rules`` 不在 ``requirement.SLOT_MAP`` 里 ⇒ 天然不参与；
#: 它是 ``requirement._derive_aggregation`` 从 output_fields **派生**的，
#: 用它判 output_fields 是循环论证。
SLOTS_NOT_PARTICIPATING = ("residual_risks",)

#: 豁免句式：先"不…"，再说被豁免的对象。对象词决定**豁免哪个槽位**——
#: 这就是"豁免词必须与槽位词同句共现"的实现：槽位词由对象词定位，
#: 别的槽位不会因为同一句话里出现了豁免词而被整槽豁免。
#: 例：「不限时间范围，但要看月度趋势」只豁免时间口径，粒度口径照旧 must_cover。
_EXEMPT_PREFIX = (
    r"(?:不限|不再|不考虑|不要求|不需要|无需|不设|不区分|不看|不关注|忽略|不涉及)"
)
_EXEMPT_FILLER = r"(?:任何|所有|额外|进一步|再|考虑|要求)?"


def _slot_exempt_words():
    """各槽位"通用"的豁免对象词（豁免整个槽位的那类说法）。

    例：时间槽的"不限时间"豁免的是**整个时间口径**；
    而"不限销售额"只豁免销售额那一个要素（见 :func:`_exempt_pattern_for`）。
    """
    return {
        "subject": ("主体", "对象"),
        "granularity": ("粒度", "维度", "颗粒度"),
        "time_semantics": ("时间", "日期", "时点", "期间"),
        "data_scope": ("范围", "口径", "筛选", "过滤"),
        "output_fields": ("字段", "指标"),
    }


def _class_exempt_words(cls):
    """某个粒度类自己的豁免对象词（"不限月度"只豁免月度，不豁免日度）。

    从 ``semantics`` 的粒度词表派生（同一份口径，不另立第二套）。
    """
    if not cls:
        return ()
    return tuple(w for w in semantics_mod.GRANULARITY_TEXT_PATTERNS.get(cls, ()) if len(w) > 1)


# ---------------------------------------------------------------------------
# 兜底提取用的形态判据
# ---------------------------------------------------------------------------
#: 业务名词常见的结尾字。兜底候选必须以这些字收尾——
#: 否则"统计各""复盘""对账"这类动词/连接词碎片会变成要素。
#: 业务名↔列的映射尚不可靠，宁可多提（低置信降级为警告），不可漏提。
_BUSINESS_SUFFIXES = set("额量数率比价金费值单次件人天月年日季表行列项类型户员店码度份笔组批款名差")

#: 纯单位词/泛词：本身不是一个业务口径，当要素会误导。
_GENERIC_UNITS = set("金额数量总数占比比例数值数据结果口径情况内容信息状态类型级别名称规模区间")

#: 🔴 **时间名词**（"月份""季度""年度""天数"…）：它们是**粒度/期间**的说法，
#: 不是业务口径（"一行代表什么" ≠ "月份"这个维度名）。
#: ⚠️ 实测撞出来的：真实需求 ``expected_output="门店、月份、销售额、订单数、退款额"``
#:    兜底提取把「月份」当成了一个输出字段要素（它以「月」收尾，过了业务名词形态那一关）。
#:    粒度词表（``semantics.GRANULARITY_TEXT_PATTERNS``）里只有「月度」「按月」，
#:    **没有「月份」** ⇒ 光靠"挖掉粒度词"堵不住它。
#: 口径从粒度词表**派生**（单一事实源，不另立第四套时间词表）：
#: 粒度类的多字词 + 以时间字收尾的"X份/X度"式说法。
_TIME_NOUNS = set()
for _cls, _words in semantics_mod.GRANULARITY_TEXT_PATTERNS.items():
    for _w in _words:
        if len(_w) > 1:
            _TIME_NOUNS.add(_w)
_TIME_NOUNS.update(("月份", "季度", "年度", "月度", "日度", "周度", "时段", "区间"))

#: 🔴 **行数/结构说明**（"一行""每行""多行"…）：它们说的是**结果集有几行**，
#: 不是业务口径。
#: ⚠️ 实测撞出来的：真实需求 ``expected_output="每门店每月一行：门店、月份、销售额…"``
#:    兜底提取把「一行」当成了一个输出字段要素（它以「行」收尾，过了业务名词形态那一关）。
#: ⇒ 判据是"整体就是行数说明"，不是"含行字"（"销售额行数"是真业务名，不能误伤）。
#: ⚠️⚠️ **必须用 tuple，不能写 set("一行 多行 …")** ——
#:    Python 的 ``set("abc")`` 是**按字符拆分**的！写成 set("一行 多行") 会得到
#:    ``{'一','行',' ','多',…}`` ⇒ 判据完全失效。
#:    （实测踩到过：``_ROW_STRUCTURE`` 被拆成 26 个单字，"一行"这条判据形同虚设。）
#:    ⇒ 这里要的是**词组集合**，必须用逗号分隔的元组。
_ROW_STRUCTURE = frozenset((
    "一行", "多行", "若干行", "每行", "各行", "行数", "数据行", "记录行",
))

#: 🔴 **计算公式连接词**（"退款率**按退款**订单数"）：碎片里带"按…算/计算"结构 ⇒
#: 它是**公式说明**不是指标名。
#: 实测撞出来的：真实需求 ``description="…退款率按退款订单数占总订单数计算…"
#:    ⇒ 兜底提取把「退款率按退款」当成了一个输出字段要素。
#: ⚠️ 只在"含按/由…计算"这类**连接结构**时判，不在单个"按"字上判
#:    （"按月销售额"是业务名，"按退款"是连接）。
_FORMULA_MARKERS = ("按", "由", "以", "根据", "根据口径", "占", "除以", "乘以", "计算")


def _is_formula_frag(frag):
    """这个碎片是不是**计算公式说明**（而不是指标名）。

    🔴 实测撞出来的：真实需求
       ``description="按门店按月统计销售额与退款率，退款率按退款订单数占总订单数计算"``
       ⇒ 兜底提取把「退款率按退款」当成了一个输出字段要素。

    ⚠️ **为什么不能只看"含按字"**：「按月销售额」是业务名（含"按"），
    「退款率按退款」是公式（含"按"且"按"在**中间**）。
    ⇒ 判据：碎片**含公式连接词**，且该连接词**不在开头**（开头的是限定语，
    由 :meth:`_strip_leading` 处理），且碎片**不以连接词结尾**。
    """
    if len(frag) < 4:
        return False
    body = frag[1:]          # 开头由 _strip_leading 处理过，这里只看中间/结尾
    for mk in _FORMULA_MARKERS:
        if mk in body and not body.endswith(mk):
            return True
    return False

#: 通用尾词：业务名后面的这些词属于"流程/说明"，不属于业务名本身。
#: 从碎片尾部剥掉它们，剩下的才可能是业务名（如"已退款金额结果集" → "已退款金额"）。
_GENERIC_TAILS = (
    "结果集", "结果", "统计", "口径", "情况", "数据", "明细", "报表", "列表",
    "汇总", "分析", "复盘", "对账", "维度",
)

#: 首尾要剥掉的虚词/连接词（字符串形式：str.strip 只认字符串，不认集合）
_TRIM_CHARS = "的和与及或是在把被从到以用按每各该本其中对给并而但因所于就都也还"

#: 句子切分符（lead 指定：。；！？\n）
_SENTENCE_DELIMS = "。；！？\n"

#: 遮蔽哨兵符：被"已认领"的词在兜底提取时替换成它，切碎片时一并当分隔符
_MASK_CHAR = "\x00"

#: 碎片切分符：标点 + 连接词（"订单数和退款额"要切成两片）+ 遮蔽哨兵
#: ⚠️ 「的」也在里面：它是**定语结构**的分界（"统计各渠道**的**转化率"），
#:    不是连接词。但中文里"的"也可能是词的一部分（"目的""有的"）——
#:    ⇒ 只在**碎片过长时**用它二次切分（见 :meth:`SlotMapper._fragments`），
#:    不作为首轮切分符（首轮切会把"目的"切成两半）。
_FRAG_SPLIT_RE = re.compile(
    r"[，。；、（）()\[\]【】《》「」『』“”\"'：:，,;!?！？\s/／\\|]+|[和与及或]|" + _MASK_CHAR + "+"
)

#: 二次切分符：只在碎片过长（疑似含定语结构）时启用
_FRAG_SPLIT_DE = re.compile(r"的")

#: 🔴 **动词性前缀**：碎片**开头**是这些字 ⇒ 它是"要做什么"的动作短语，
#: 不是业务口径（"统计各门店""需要每个门店""汇总各渠道"）。
#: ⚠️ 只判**开头**：中文里动词可以在中间（"门店销售额"里的"售"不是动词前缀），
#:    判中间会误伤真业务名。
#: ⚠️ 这些词在**词表命中**的路径上已被挖掉；这里挡的是"词表没命中的那部分"。
_VERB_PREFIXES = ("需要", "同时要", "要各", "要每", "要按", "要看", "要查", "要导",
                  "统计", "汇总", "分析", "复盘", "对账", "导出", "输出", "列出",
                  "展示", "监控", "跟踪", "对比", "帮我", "给我", "提供", "生成",
                  "请给", "想把", "想要", "查询", "查一下", "看", "把", "将")

#: 🔴 **定语标记**：量词/指示字（"各""每""个""该""本""此"）——
#: 它们标记的是"要哪些东西"，不是业务名本身。
#: 「统计**各**门店」剥掉动词后剩「各门店」⇒ 剥掉「各」剩「门店」——
#: 但「门店」是词表已认领的词，兜底路径本就不该再提它。
_DETERMINER_CHARS = set("各每个该本此这那")

#: 🔴 **通用维度名**（"门店""渠道""用户""商品"…）：它们是"按什么拆"的说法，
#: 不是业务口径（业务口径是"门店**销售额**"里的"销售额"）。
#: ⚠️ 这条判据**不依赖词表** —— 判"动作短语"若依赖"词表是否已认领"，
#:    同一段原文在"词表命中"与"词表未命中"两种情形下会得到不同结果
#:    （实测：空词表时「汇总各门店」被提成「门店」）⇒ 口径随环境漂移 = 静默失效。
#:    ⇒ 动作短语的识别必须**自足**：不靠外部状态。
#: ⚠️⚠️ **绝不能把业务名词放进这张表**（"销售额""退款额"是业务口径，
#:    不是维度名）—— 放进来会把真业务名词一起挡掉。
#:    判据：这个词单独说出来，业务方会不会觉得"这是我要的一个数？"不会 ⇒ 才是维度名。
_GENERIC_DIMENSIONS = set(
    "门店 渠道 用户 客户 会员 商品 产品 品类 类目 地区 区域 省份 城市 城市名 "
    "店铺 商家 品牌 部门 团队 人员 员工 订单 订单项 明细 记录 条目 项目 "
    "国家 性别 年龄 层级 级别 状态 类型 类别 来源 场景 时间 日期 月份 "
    "字段 指标 维度 口径 范围 条件".split()
)

#: 🔴 **含/带 这类限定动词 + 短词**：「含退款」「含销量」——
#: 它们是"把哪些东西算进来"的说法，不是业务口径。
#: ⚠️ 但「含退款**金额**」是**真业务名**（限定词 + 业务名），必须保留！
#: ⇒ 判据不是"含X 一律丢"，而是"剥掉限定词后剩下的**太短**（≤2 字）就不成词"：
#:    含退款 → 剥"含" → "退款"（2 字）⇒ 泛词 ⇒ 丢
#:    含退款金额 → 剥"含" → "退款金额"（4 字）⇒ 是业务名 ⇒ 保留
#: 这条口径与「宁可多提、不可漏提」一致：宁可多留一个「含退款金额」，
#: 也不能把真业务名剥没了。
_LIMITING_VERBS = ("含", "带", "含了", "带有", "有", "是", "为")


# ---------------------------------------------------------------------------
# 1. 原文切句
# ---------------------------------------------------------------------------
def sentence_split(text):
    """把一段需求原文切成句子。

    参数
    ----
    text : 需求原文片段

    返回
    ----
    list of dict：``{"index": 1, "text": "…", "start": 0, "end": 12}``
    ``index`` 从 1 起（与 provenance「需求原文第N句」对齐）；
    ``start``/``end`` 是该句在**入参 text** 中的字符偏移，供 span 回溯。

    切分符：``。；！？\\n``（lead 指定）。
    ⚠️ 逗号**不切**：中文需求里逗号是并列连接（"销售额、订单数和退款额"），
    切开会把一个并列清单拆成三个不可比的句子。
    """
    out = []
    if not text:
        return out
    start = 0
    n = len(text)
    i = 0
    while i <= n:
        if i == n or text[i] in _SENTENCE_DELIMS:
            seg = text[start:i]
            lead = len(seg) - len(seg.lstrip())
            tail = len(seg) - len(seg.rstrip())
            s = start + lead
            e = i - tail
            if s < e:
                out.append({
                    "index": len(out) + 1,
                    "text": text[s:e],
                    "start": s,
                    "end": e,
                })
            start = i + 1
        i += 1
    return out


def _text_segments(text, p1_items):
    """把需求原文按字段切成片段 [(起, 止, 内容, 证据项 or None)]。

    为什么按字段切、而不是对拼好的 ``bundle["text"]`` 整串切：
    ``bundle["text"]`` 是各字段用空格拼起来的，字段之间没有句号，
    对整串切会得到一个横跨三四个字段的"句子"，provenance 就没法看了。

    片段来源有两处，**都要**：
      · P1 证据项（title/business_context/description/expected_output）——带字段出处；
      · 片段之间的**残余**（contact / time_range 等没有 P1 证据项的字段）——
        漏了它们，原文里的时间范围就没有句子可落，provenance 会指不到处。
    """
    segs = []
    if not text:
        return segs
    cursor = 0
    pending = []
    for it in p1_items or []:
        content = it.get("content") or ""
        if not content.strip():
            continue
        j = text.find(content, cursor)
        if j < 0:
            pending.append((it, content, -1))
            continue
        if j > cursor:
            segs.append((cursor, j, text[cursor:j], None))
        segs.append((j, j + len(content), content, it))
        cursor = j + len(content)
    if cursor < len(text):
        segs.append((cursor, len(text), text[cursor:], None))
    for it, content, _ in pending:
        segs.append((-1, -1, content, it))
    return segs


def split_demand_sentences(p1_items, text=None):
    """把需求原文切成句子，**全局连续编号**，偏移相对 ``text``（需求原文整体）。

    偏移换算：P1 字段内容在 ``text`` 里的位置用**顺序查找**定位
    （与 ``evidence.collect`` 拼 ``text`` 的方式一致：字段值去空白后用单空格相连）。
    定位不到就退化成"该字段无偏移"（provenance 仍能给出句子与编号）。

    返回的每句额外带 ``field``（字段出处）、``item``（证据项）与
    ``text_start``/``text_end``（在需求原文整体里的偏移，供 span 回溯）。
    """
    out = []
    for base, end, content, it in _text_segments(text or "", p1_items or []):
        for s in sentence_split(content):
            s = dict(s)
            s["index"] = len(out) + 1
            s["field"] = (it or {}).get("locator") or (it or {}).get("field")
            s["item"] = it
            s["text_start"] = (base + s["start"]) if base >= 0 else -1
            s["text_end"] = (base + s["end"]) if base >= 0 else -1
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# 2. SlotMapper：六槽位 → 要素候选
# ---------------------------------------------------------------------------
class SlotMapper(object):
    """M3 六槽位 → 需求要素候选。

    参数
    ----
    bundle : 证据包（``evidence.collect`` 的返回）。用到 ``text`` / ``by_level`` / ``matched``。
    slots   : M3 槽位字典（``semantics.first_round`` 的 ``slots``）。

    槽位口径走 ``requirement.SLOT_MAP``（唯一映射点），不另写一套。
    """

    def __init__(self, bundle, slots):
        self.bundle = bundle or {}
        self.slots = slots or {}
        self.text = self.bundle.get("text") or ""
        self.p1_items = (self.bundle.get("by_level") or {}).get("P1") or []
        self.hits = self.bundle.get("matched") or []
        self.sentences = split_demand_sentences(self.p1_items, self.text)
        self._covered = None

    # -- 基础查询 --------------------------------------------------------
    def _slot_candidates(self, obj_field):
        """取某槽位（M4 字段名）的 M3 候选。"""
        m3_key = requirement_mod.SLOT_MAP.get(obj_field)
        if not m3_key:
            return []
        slot = self.slots.get(m3_key) or {}
        return [c for c in (slot.get("candidates") or []) if isinstance(c, dict)]

    def _slot_label(self, obj_field):
        """槽位中文名（M3 的 SLOT_LABELS，与界面口径一致）。"""
        m3_key = requirement_mod.SLOT_MAP.get(obj_field)
        return semantics_mod.SLOT_LABELS.get(m3_key, m3_key or obj_field)

    def _sentence_of(self, anchors):
        """按锚点词找到它所在的那一句（全局编号）。找不到返回 None。"""
        for a in anchors or []:
            if not a:
                continue
            for s in self.sentences:
                if a in s["text"]:
                    return s
        return None

    def _provenance(self, sent):
        """provenance 文案：需求原文第N句：'…'（N 与切句编号一致）。"""
        if sent is None:
            return "需求原文（全文未出现相关表述，无对应句）"
        return "需求原文第%d句：'%s'" % (sent["index"], sent["text"])

    # -- 豁免判定 --------------------------------------------------------
    def _exempt_pattern(self, obj_field):
        """**槽位级**豁免句式：只含该槽位的通用对象词。"""
        words = _slot_exempt_words().get(obj_field) or ()
        if not words:
            return None
        return re.compile(
            _EXEMPT_PREFIX + _EXEMPT_FILLER + "(?:%s)" % "|".join(words))

    def _exempt_pattern_for(self, obj_field, element, cls=None):
        """**元素级**豁免句式：只豁免这一个要素（粒度则只豁免这一个粒度类）。

        为什么必须按要素而不是按槽位：豁免的范围由"被豁免对象的具体程度"决定。
          · 「不限销售额」豁免销售额这一个要素 —— 同句里的订单数/退款额照旧 must_cover；
          · 「不限月度」只豁免月度 —— 同句里的日度照旧 must_cover；
          · 「不限时间」「不限粒度」这类**通用**说法才豁免整个槽位。
        ⚠️ 反过来写（只按槽位判）会把「不限月度」读成"整个粒度口径都不要"，
        而业务方明明还要日度 ⇒ 门禁静默放过日度缺口。这跟阶段二那个病同构。

        对象词三处来源，合并去重（长词优先，避免「月度」被「月」截断）：
          ① 该要素自己的名字；② 该粒度类自己的词（从 semantics 词表派生）；
          ③ 该槽位的通用词（「不限粒度」才豁免整个粒度槽位）。
        """
        words = []
        if element and len(element) > 1:
            words.append(element)
        words.extend(_class_exempt_words(cls))
        words.extend(_slot_exempt_words().get(obj_field) or ())
        uniq = sorted(set(w for w in words if w), key=len, reverse=True)
        if not uniq:
            return None
        return re.compile(
            _EXEMPT_PREFIX + _EXEMPT_FILLER + "(?:%s)"
            % "|".join(re.escape(w) for w in uniq))

    def _exempt_in_sentence(self, obj_field, sent, element=None, cls=None):
        """该要素在**指定句**里是否被豁免（同句共现）。

        ⚠️ 同句共现是硬要求：句号/分号才是"一件事"的边界，
        逗号在中文里是并列连接（「不限时间范围，但要看月度趋势」是一句话里的两件事），
        所以豁免范围**不跨句**。
        """
        if sent is None:
            return None
        pat = (self._exempt_pattern_for(obj_field, element, cls) if element
               else self._exempt_pattern(obj_field))
        if pat is None:
            return None
        m = pat.search(sent["text"])
        return m.group(0) if m else None

    def _exemption(self, obj_field):
        """该槽位在需求里被显式豁免的位置 → (句子, 豁免短语) 或 None。"""
        pat = self._exempt_pattern(obj_field)
        if pat is None:
            return None
        for s in self.sentences:
            m = pat.search(s["text"])
            if m:
                return s, m.group(0)
        return None

    # -- 覆盖与遮蔽（兜底提取用） ----------------------------------------
    def _field_hit_terms(self):
        """词表命中里属于「输出字段」的那些词（口径来自 semantics.FIELD_HIT_KINDS）。"""
        out = []
        for h in self.hits:
            if h.get("kind") not in semantics_mod.FIELD_HIT_KINDS:
                continue
            for k in ("term", "matched", "label"):
                v = h.get(k)
                if v:
                    out.append(str(v))
        return out

    def _grain_terms(self):
        """粒度词（多字的那批）——兜底提取时必须先挖掉。

        为什么只挖多字的：多字粒度词（月度/按月/季度/年度…）是**粒度**不是指标，
        让它掉进兜底路径会凭空多出一个"指标要素"；
        而单字（月/日/年/季/天）太可能是指标名的一部分（"月销量""日流水"），
        挖掉会把真指标截断成碎片——两害相权，宁可让单字留着。
        """
        out = []
        for words in semantics_mod.GRANULARITY_TEXT_PATTERNS.values():
            out.extend([w for w in words if len(w) > 1])
        return out

    def _all_known_terms(self):
        """全部"已被认领"的词（词表命中的所有词 + M3 槽位候选 + 粒度词）。"""
        if self._covered is not None:
            return self._covered
        terms = set()
        for h in self.hits:
            for k in ("matched", "term", "label"):
                v = h.get(k)
                if v:
                    terms.add(str(v))
        for obj_field in requirement_mod.SLOT_MAP:
            for c in self._slot_candidates(obj_field):
                v = c.get("value")
                if v:
                    terms.add(str(v))
                for v in (c.get("matched_terms") or []):
                    terms.add(str(v))
        for words in semantics_mod.GRANULARITY_TEXT_PATTERNS.values():
            terms.update(words)
        self._covered = terms
        return terms

    def _mask(self, text, terms):
        """把给定词从 text 里挖掉（换成哨兵符，之后按分隔符切掉）。

        为什么用哨兵符而不是空格：空格是合法的中文分词边界，
        留着它会让碎片里混进空白，导致"这个碎片不是词"的误判。
        """
        out = text
        for t in sorted(set(terms), key=len, reverse=True):
            if t:
                out = out.replace(t, _MASK_CHAR * len(t))
        return out

    def _strip_leading(self, frag):
        """剥掉碎片**开头**的动词/限定词/定语标记，返回剩余部分。

        ⚠️ **两条路径必须共用这一个实现**（动词分支与通用分支）——
        各写一份必然漂移，漂移表现为"同一段原文在两条路径下结果不同"。

        剥的顺序（每一步都可剥多次）：
          ① 动词前缀（"统计""汇总""需要""看""要"…）—— 动作短语不是业务口径；
          ② 定语标记（"各""每""个""该""本"）—— "各门店"是"要哪些东西"；
          ③ 限定动词（"含""带""有"）—— "含退款"是"含了哪些"。
        ⚠️ ③ 只在剥完**仍够长**时才剥：否则"含退款金额"会被剥成"退款金额"…
        呃，那是对的；但"含退款"剥成"退款"（2 字）就不成词了 ⇒ 短的**不剥**。
        """
        out = frag
        # ① 动词前缀（可能连着来："需要看每个门店" = 需要 + 看）
        changed = True
        while changed:
            changed = False
            for vp in _VERB_PREFIXES:
                if out.startswith(vp) and len(out) > len(vp):
                    out = out[len(vp):].strip(_TRIM_CHARS)
                    changed = True
                    break
        # ② 定语标记（量词/指示字）
        while out[:1] in _DETERMINER_CHARS:
            out = out[1:].strip(_TRIM_CHARS)
        # ③ 限定动词：剥完必须还剩 ≥3 字，否则剥了也不成词
        #    "含退款" → 剥"含" → "退款"（2 字）⇒ 太短 ⇒ **不剥** ⇒ 整块「含退款」
        #    仍以"款"收尾、过了形态关 ⇒ 得靠下面的"限定动词整块丢"判据挡掉。
        for lv in _LIMITING_VERBS:
            if out.startswith(lv) and len(out) > len(lv) + 2:
                out = out[len(lv):].strip(_TRIM_CHARS)
                break
        return out

    def _fragments(self, text):
        """把残余文本切成候选碎片（剥首尾虚词与通用尾词，去重，保序）。

        ⚠️ 这里必须先挖掉**期间/时点**片段再切：兜底路径不看词表，
        「上月的销售额」会切出「上月」——它以「月」收尾、过了业务名词形态那一关，
        于是被当成一个叫「上月」的指标要素提出来。
        复用 ``semantics`` 既有的两道闸门（``PERIOD_RE`` + ``_PERIOD_SPAN_RE``
        遮蔽、单字左右文闸门），**不另立第三套时间词表**——
        否则同一个「上月」在粒度判定里被排除、在兜底提取里却被当成指标，两处口径打架。
        """
        out = []
        for raw in _FRAG_SPLIT_RE.split(_mask_period_spans(text)):
            for frag in self._split_long(raw):
                changed = True
                while changed:
                    changed = False
                    for tail in _GENERIC_TAILS:
                        if frag.endswith(tail) and len(frag) > len(tail):
                            frag = frag[: -len(tail)].strip(_TRIM_CHARS)
                            changed = True
                            break
                if not (2 <= len(frag) <= 8):
                    continue
                if not re.match(r"^[一-龥]+$", frag):
                    continue
                if frag in _GENERIC_UNITS:
                    continue
                if frag in _TIME_NOUNS:
                    # 时间名词（"月份"/"季度"/"年度"…）：是粒度/期间的说法，不是业务口径
                    continue
                if _POINTING_ONLY_RE.match(frag):
                    continue
                if frag.startswith(_VERB_PREFIXES):
                    # 🔴 动词性前缀（"统计各门店""汇总各渠道转化率"）：**剥离**前缀再判，
                    #    不是整块丢弃 ——
                    #    整块丢会把前缀后面的真业务名一起丢掉（"汇总各渠道**转化率**"
                    #    ⇒ 「转化率」消失 ⇒ 漏提 = 阶段二那个病的同构）。
                    stripped = self._strip_leading(frag)
                    if not (2 <= len(stripped) <= 8):
                        # 剥完没有可判的定语段 ⇒ 整块是动作短语，丢弃
                        continue
                    # 🔴 **限定动词 + 短词**（"需要看含退款的销售口径" → 剥动词后剩
                    #    "含退款"）：剥掉限定词后只剩 2 字 ⇒ 不是业务口径。
                    #    ⚠️ 与通用路径同一条判据（两处必须一致）。
                    if stripped[:1] in _LIMITING_VERBS and len(stripped) <= 3:
                        continue
                    # ⚠️ 形态判据走**同一个** :meth:`_accept_frag`（口径只有一处）
                    if not self._accept_frag(stripped):
                        continue
                    if stripped not in out:
                        out.append(stripped)
                    continue
                # -- 通用路径：也可能带限定动词/定语标记（"含退款""各渠道"） --
                # ⚠️ 与动词分支**共用同一套判据**（:meth:`_strip_leading` + :meth:`_accept_frag`），
                #    否则两条路径口径会漂移（"含退款"在一条路径被剥、另一条不被剥）。
                cand = self._strip_leading(frag)
                if cand != frag:
                    # 剥过之后按剥完的形态重判一遍（同一套判据）
                    if not self._accept_frag(cand):
                        continue
                    if cand not in out:
                        out.append(cand)
                    continue
                # 🔴 **限定动词 + 短词**（"含退款""含订单数""含销量"）：
                #    剥掉限定词后只剩 2 字（"退款""订单""销量"）⇒ 不是业务口径，
                #    是"把哪些东西算进来"的说法。
                #    ⚠️ 判据是"剥完**太短**"，不是"含X 一律丢"——
                #    "含退款金额"剥完是 4 字 ⇒ 是业务名 ⇒ 保留（上一条分支处理）。
                if frag[:1] in _LIMITING_VERBS and len(frag) <= 3:
                    continue
                if (frag[-1] in "月日天年季" and len(frag) > 1
                        and frag[-2] in _POINTING_PREFIX_CHARS):
                    # 单字时间词且左字是时点指示字（"上月"/"当日"）：时点，不是指标
                    continue
                if not self._accept_frag(frag):
                    continue
                if frag in out:
                    continue
                out.append(frag)
        return out

    def _accept_frag(self, frag):
        """这个碎片算不算业务名词（**两条路径共用的唯一判据**）。

        ⚠️ **必须只有一份实现**：我第一版把形态判据写成两套（动词分支一套、
        通用分支一套），自证立刻就红了 ——「含退款」在一条路径被剥、在另一条不被剥。
        **口径分叉 = 静默失效**（同一段原文在两条路径下结果不同）。

        判据顺序（先便宜后昂贵）：
          ① 长度：太短不是词，太长不是业务名（8 字上限）；
          ② 纯中文；
          ③ 泛词 / 时间名词 / 行数说明 / 通用维度名；
          ④ 时点词（"上月"/"当日"）；
          ⑤ 公式连接结构（"退款率按退款"）；
          ⑥ 收尾字是业务字；
          ⑦ 词表已认领的词（兜底不重复提）。
        """
        if not (2 <= len(frag) <= 8):
            return False
        if not re.match(r"^[一-龥]+$", frag):
            return False
        if frag in _GENERIC_UNITS:
            return False
        if frag in _TIME_NOUNS:
            # 时间名词（"月份"/"季度"/"年度"…）：是粒度/期间的说法，不是业务口径
            return False
        if frag in _ROW_STRUCTURE:
            # 行数/结构说明（"一行""行数"…）：说结果集有几行，不是业务口径
            return False
        if frag in _GENERIC_DIMENSIONS:
            # 通用维度名（"门店""渠道""订单"…）：是"按什么拆"，不是业务口径
            return False
        if (frag[-1] in "月日天年季" and len(frag) > 1
                and frag[-2] in _POINTING_PREFIX_CHARS):
            # 单字时间词且左字是时点指示字（"上月"/"当日"）：时点，不是指标
            return False
        if _is_formula_frag(frag):
            return False
        if frag[-1] not in _BUSINESS_SUFFIXES:
            return False
        if frag in self._all_known_terms():
            # 词表已认领的词（"统计各门店"→"门店"）：兜底不重复提
            # （兜底的职责是"补词表没命中的"，不是复述词表）
            return False
        return True

    def _split_long(self, raw):
        """碎片过长时按「的」二次切分（提���定语后面的业务名词）。

        🔴 **为什么必须做**（实测撞出来的漏提）
        -----------------------------------------
        「统计各渠道**的**转化率」是一整块 9 字，超过 8 字上限 ⇒ 整块被丢
        ⇒ **「转化率」这个真业务名词提不出来**。
        而"转化率"不在 161 条词表里 ⇒ 兜底路径是它**唯一**的出路 ⇒
        漏掉它 = 业务方要的指标从清单里消失 = 阶段二那个病的同构。

        ⚠️ 为什么**不在首轮**就按「的」切：中文里「的」也可能是词的一部分
        （"目的""有的""我的"）⇒ 首轮切会把"目的"切成两半、造出垃圾碎片。
        ⇒ 只在**碎片过长**（说明它多半是"定语+中心语"结构）时二次切分。

        二次切分后：
          · 每一段都过一遍形态判据（由调用方做）；
          · 定语段（"统计各渠道"）通常以动词/指示字收尾 ⇒ 形态判据会滤掉；
          · 中心语段（"转化率"）以业务字收尾 ⇒ 保留。
        """
        frag = raw.strip(_TRIM_CHARS)
        if len(frag) <= 8 or "的" not in frag:
            return [frag]
        parts = [p.strip(_TRIM_CHARS) for p in _FRAG_SPLIT_DE.split(frag)]
        return [p for p in parts if p]

    def _fallback_field_elements(self):
        """兜底提取：词表没命中、但需求方确实说了的业务名词。

        做法：逐个 P1 字段做——
          ① 挖掉**输出字段类**的已认领词（字段词 + 粒度词）；
          ② 在残余文本里按标点/连接词切片；
          ③ 剥掉通用尾词，剩下的若是业务名词形态 ⇒ 候选。
        ⚠️ 枚举词（如「已退款」）**不挖**：它是限定词不是业务名，
        挖掉会把「已退款金额」拆成没意义的「金额」（泛词，会被滤掉），
        真正的业务名反而丢了。
        """
        out = []
        seen = set()
        for it in self.p1_items:
            content = it.get("content") or ""
            if not content.strip():
                continue
            masked = self._mask(
                content, self._field_hit_terms() + self._grain_terms())
            for frag in self._fragments(masked):
                if frag in seen or frag in self._all_known_terms():
                    continue
                seen.add(frag)
                sent = self._sentence_of([frag])
                out.append({
                    "element": frag,
                    "slot": "output_fields",
                    "provenance": self._provenance(sent),
                    "confidence": 0.45,
                    "needs_confirmation": True,
                    "mandatory": False,
                    "polarity": POLARITY_MUST_COVER,
                    "matcher": "%s:%s" % (MATCHER_KIND_FIELD, frag),
                    # -- 实例层自有（不进契约） --
                    "cause": None,
                    "source": "fallback_noun",
                    "sentence_index": sent["index"] if sent else None,
                })
        return out

    # -- 主流程 ----------------------------------------------------------
    def elements(self):
        """产出全部要素候选（8 字段契约投影 + 实例层自有信息）。"""
        out = []
        for obj_field in requirement_mod.SLOT_MAP:
            if obj_field in SLOTS_NOT_PARTICIPATING:
                continue
            out.extend(self._elements_of_slot(obj_field))
        out.extend(self._fallback_field_elements())
        # 同名要素只留一条（先出现的赢：槽位顺序里 granularity 在 output_fields 之前，
        # 所以"月度"归粒度槽位，不会被兜底再提一遍）
        dedup = []
        names = set()
        for e in out:
            if e["element"] in names:
                continue
            names.add(e["element"])
            dedup.append(e)
        return dedup

    def _elements_of_slot(self, obj_field):
        """单个槽位的要素候选。"""
        label = self._slot_label(obj_field)
        cands = self._slot_candidates(obj_field)
        out = []

        if obj_field == "output_fields":
            # 输出字段以**词表命中**为准，不以 M3 字段槽为准：
            # M3 字段槽按 locator 去重，会把"销售额/订单数"这类同 locator 的命中丢掉
            # （实测订单数因此从字段槽消失 ⇒ 要素清单天生漏项 ⇒ 用例 1 不成立）。
            for h in self.hits:
                if h.get("kind") not in semantics_mod.FIELD_HIT_KINDS:
                    continue
                name = str(h.get("term") or h.get("matched") or "").strip()
                if not name:
                    continue
                out.append(self._element(
                    obj_field=obj_field,
                    element=name,
                    anchors=[str(h.get("matched") or name)],
                    confidence=0.8,
                    matcher="%s:%s" % (MATCHER_KIND_FIELD, name),
                    source="lexicon_hit",
                ))
            for c in cands:
                name = str(c.get("value") or "").strip()
                if not name or any(e["element"] == name for e in out):
                    continue
                out.append(self._element(
                    obj_field=obj_field,
                    element=name,
                    anchors=[str(v) for v in (c.get("matched_terms") or [])] or [name],
                    confidence=float(c.get("confidence") or 0.5),
                    matcher="%s:%s" % (MATCHER_KIND_FIELD, name),
                    source="slot_candidate",
                ))
            return out

        if obj_field == "granularity":
            # 粒度只认**需求原文里说的**那一条（元数据登记的表行定义不是需求方要的粒度）
            for c in cands:
                cls = c.get("granularity_class")
                if not cls:
                    continue
                name = semantics_mod.GRANULARITY_CLASS_LABELS.get(cls, cls)
                out.append(self._element(
                    obj_field=obj_field,
                    element=name,
                    anchors=[str(v) for v in (c.get("matched_terms") or [])] or [name],
                    confidence=float(c.get("confidence") or 0.6),
                    matcher="%s:%s" % (MATCHER_KIND_TIME_GRANULARITY, cls),
                    source="demand_text",
                    cls=cls,
                ))
            if not out:
                out.append(self._slot_level_element(obj_field, label))
            return out

        # 其余槽位（subject / time_semantics / data_scope）
        for c in cands:
            name = str(c.get("value") or "").strip()
            if not name:
                continue
            anchors = [str(v) for v in (c.get("matched_terms") or [])] or [name]
            if not any(a in self.text for a in anchors):
                # 该候选不是需求方说的（表级兜底推出来的）⇒ 不当要素
                continue
            out.append(self._element(
                obj_field=obj_field,
                element=name,
                anchors=anchors,
                confidence=float(c.get("confidence") or 0.5),
                matcher=self._plain_matcher(obj_field, name),
                source="slot_candidate",
            ))
        if not out:
            out.append(self._slot_level_element(obj_field, label))
        return out

    def _plain_matcher(self, obj_field, name):
        kind = {
            "subject": MATCHER_KIND_SUBJECT,
            "time_semantics": MATCHER_KIND_TIME,
            "data_scope": MATCHER_KIND_SCOPE,
            "output_fields": MATCHER_KIND_FIELD,
            "granularity": MATCHER_KIND_TIME_GRANULARITY,
        }.get(obj_field, MATCHER_KIND_SLOT)
        return "%s:%s" % (kind, name)

    def _element(self, obj_field, element, anchors, confidence, matcher,
                 source, cls=None):
        """造一条"有原文依据"的要素，并定它的 polarity。

        豁免按**元素级**判（见 :meth:`_exempt_pattern_for`）：粒度候选要把
        自己的 ``cls`` 传进来，「不限月度」才只豁免月度而不豁免日度。
        """
        sent = self._sentence_of(anchors)
        exempt = self._exempt_in_sentence(obj_field, sent, element=element,
                                          cls=cls)
        if exempt:
            polarity = POLARITY_MAY_OMIT
            cause = "业务方显式豁免 %s" % element
        else:
            polarity = POLARITY_MUST_COVER
            cause = None
        return {
            "element": element,
            "slot": obj_field,
            "provenance": self._provenance(sent),
            "confidence": confidence,
            "needs_confirmation": False,
            "mandatory": polarity == POLARITY_MUST_COVER,
            "polarity": polarity,
            "matcher": matcher,
            # -- 实例层自有（不进契约） --
            "cause": cause,
            "source": source,
            "sentence_index": sent["index"] if sent else None,
        }

    def _slot_level_element(self, obj_field, label):
        """槽位级要素：该槽位在原文里没被提出（或被显式豁免）。

        两种成因的文案**必须不同**——"没提"与"说了不用"是两件事：
        前者业务方还没表态，后者业务方已经表态了。混成一句话，
        报告里就分不清"漏提"和"豁免"，假阳性排查也就无从下手。
        """
        exempt = self._exemption(obj_field)
        if exempt:
            sent, phrase = exempt
            return {
                "element": label,
                "slot": obj_field,
                "provenance": self._provenance(sent),
                "confidence": 0.45,
                "needs_confirmation": False,
                "mandatory": False,
                "polarity": POLARITY_MAY_OMIT,
                "matcher": "%s:%s" % (MATCHER_KIND_SLOT, obj_field),
                # -- 实例层自有（不进契约） --
                "cause": "业务方显式豁免 %s" % label,
                "source": "exempted",
                "sentence_index": sent["index"] if sent else None,
            }
        return {
            "element": label,
            "slot": obj_field,
            "provenance": self._provenance(None),
            "confidence": 0.45,
            "needs_confirmation": False,
            "mandatory": False,
            "polarity": POLARITY_MAY_OMIT,
            "matcher": "%s:%s" % (MATCHER_KIND_SLOT, obj_field),
            # -- 实例层自有（不进契约） --
            "cause": "需求未提出 %s，不作要求" % label,
            "source": "not_stated",
            "sentence_index": None,
        }


# ---------------------------------------------------------------------------
# 3. 便捷入口
# ---------------------------------------------------------------------------
def build_elements(bundle, slots):
    """产出要素候选列表（实例层入口）。

    返回的每条含 8 个契约字段 + 实例层自有信息（``cause`` / ``source`` /
    ``sentence_index``）。要喂底座时用 :func:`to_contract_dicts` 投影。
    """
    return SlotMapper(bundle, slots).elements()


def to_contract_dicts(elements):
    """投影成底座 8 字段契约（**恰好 8 个字段**，多一个都会被拒收）。

    🔴 **``cause`` 只存在于产出层，不进入 8 字段契约，也不参与判定**
    --------------------------------------------------------------
    ``cause``（"需求未提出 X" / "业务方显式豁免 X"）是**实例层自有字段**，
    只在 :func:`build_elements_from_requirement` 的产出里存在，供**详情展示**
    区分「原文未提出」与「业务方显式豁免」这两种 ``may_omit``。
    ⇒ **本投影会把它剥掉，且这是有意的**：
      · 底座 ``ElementContract.from_dict`` **拒收契约外字段**（契约冻结为 8 字段）
        ⇒ 加第 9 个字段会直接让底座拒收整批要素；
      · 底座 ``judge()`` 对两种 ``may_omit`` 写的是**同一句** reason
        （"polarity=may_omit（原文未提出或业务方显式豁免），不进覆盖判定"）
        ⇒ **这是有意为之**：判定层不关心"为什么不要"，只关心"要不要判"。
    ⇒ 报告层要区分这两种成因，走这条路：
        elements = build_elements_from_requirement(requirement)   # 带 cause
        contract = to_contract_dicts(elements)                     # 喂 judge()
        # 详情行用 elements（带 cause），用 :func:`not_judged_reason` 分流
    ⚠️ 混成一句的代价 = **把"没查"谎报成"不需要"** ——
    业务方看到"不作要求"会以为是自己说的，实际是系统没能力判 ⇒
    正是 REQ-COVERAGE 那个原始病（"没查"看起来像"没问题"）。
    """
    return [{k: e[k] for k in CONTRACT_FIELDS} for e in (elements or [])]


# ---------------------------------------------------------------------------
# 3b. 从「结构化需求对象」出发的入口
# ---------------------------------------------------------------------------
#: 🔴 **为什么需要这一层**（第 4a 步的真正技术障碍）
#: ------------------------------------------------------
#: ``requirement.get(demand_id, version)`` 从 DB 的 payload JSON 解析，**不调
#: ``evidence.collect``** ⇒ 结构化需求对象里**没有 bundle**。
#: 而 ``gateway/gates.py`` 手里只有 requirement 对象（``_requirement_check(requirement)``），
#: 拿不到原始 demand ⇒ 光有 requirement 对象无法直接做要素抽取。
#:
#: 实测（本轮，容器内 DR-20261020-EUQC）：
#:   requirement 对象顶层键 = aggregation_rules / confirmed_facts / data_scope /
#:     dataset / demand_id / evidence_chain / granularity / output_fields /
#:     residual_risks / schema_version / source_round / status / subject /
#:     time_semantics / version
#:   ⇒ **含 ``demand_id`` 与 ``dataset``** ⇒ 入口可以从这两个键反查回 demand 与
#:     词表，重建 bundle。这是选 A 方案（而非 B「调用方拆开传」）的依据：
#:     B 方案要求调用方先有 bundle，而调用方恰恰没有。
#:
#: ⚠️ **要素抽取不需要 P4/P5 知识证据**（实测）：
#:   ``collect`` 的 1.7s 几乎全在 ``knowledge.search``（P4/P5）上；
#:   而要素只吃 ``text`` / ``matched`` / P1 三样。实测跳过知识检索后：
#:     text 相同 ✓ / matched 相同 ✓（12 条逐项一致）/ by_level.P1 相同 ✓
#:     ⇒ M3 六个槽位候选**逐槽相同** ⇒ 要素清单**逐项相同**。
#:   所以本入口走 ``knowledge_mod=None`` 的快路径：collect 1.72s → 0.001s。
#:   ⚠️ 这不是"偷懒跳过"，是**只要要素就不要证据链**——省下的时间是真省。
#:   但 P4/P5 不参与本入口，调用方若还要证据链，请另走 ``evidence.collect``。
#:
#: ⚠️ **依赖纪律**：本函数**按需 import** ``evidence`` / ``demand`` / ``knowledge`` /
#:   ``semantics``，**不在模块顶层 import**。理由：
#:     ① 顶层 import 会让裸解释器（无 sqlglot）也去拉 DB 依赖链——虽然这些模块
#:        都不依赖 sqlglot，但它们 import ``db``，裸解释器下 ``db`` 需要环境变量，
#:        会把"裸解释器可自证"这个优势弄丢；
#:     ② 底座 ``reqcov`` 依赖 sqlglot，本模块**任何位置**都不得顶层 import 它。
#:
#: ⚠️ **脱敏口径**：走 ``demand.get(demand_id)`` 默认**掩码版**，
#:   与 ``analysis.build`` 同口径（分析链路不需要原文，展开原文是入口侧脱敏的延伸）。
#:   掩码掉的是手机号这类值，不影响"手机号"这个词本身出现 ⇒ R7 判定照常生效。

#: 进程内 bundle 缓存。键 = (demand_id, dataset, 需求原文指纹)。
#: ⚠️ **为什么按原文指纹而不是按 requirement_version**：
#:    requirement version 只标识"结构化需求对象第几版"，而 bundle 取决于
#:    **需求原文 + 词表**——原文改了但还没重新生成 requirement 对象时，
#:    同一 version 对应的原文可能已变。按 version 缓存会把**旧原文的要素**
#:    用到**新需求**上 ⇒ 静默给出错的缺口清单。原文指纹才对应 bundle 的真实输入。
_BUNDLE_CACHE = {}
_BUNDLE_CACHE_MAX = 32


def _demand_fingerprint(demand):
    """需求原文指纹（bundle 的真实输入）。

    取 P1 四字段 + time_range 的内容与长度——``evidence.collect`` 拼 ``text``
    用的就是这几个（见 ``evidence.py:685-701``），它们变了 bundle 就变。
    """
    parts = []
    for f in ("title", "business_context", "description",
              "expected_output", "contact", "time_range"):
        v = str((demand or {}).get(f) or "").strip()
        if v:
            parts.append("%s=%d:%s" % (f, len(v), v))
    return "|".join(parts)


def collect_bundle(demand_id, dataset="B", with_knowledge=False,
                   demand=None, lexicon=None):
    """重建某需求的 bundle（要素抽取用）。

    参数
    ----
    demand_id : 需求单号
    dataset   : 数据集代号（"A"/"B"），决定用哪套词表
    with_knowledge : 是否连 P4/P5 知识证据一起取。
    demand : **可选**。已备好的需求单（dict）；传了就不查库。
    lexicon : **可选**。已备好的词表（list）；传了就不查库。

    ⚠️ 默认 **False**（快路径）：实测 collect 的 1.72s 几乎全在知识检索上，
    而要素抽取只吃 text / matched / P1 ⇒ 跳过不影响结果（已逐项实测）。
    确需完整证据链时传 True。

    🔴 **``demand`` / ``lexicon`` 这两个注入点是给离线自证用的**
    ---------------------------------------------------------
    实测（容器内，清空 DSN）：
      · ``evidence.collect(demand=..., lexicon=...)`` **离线可跑** ✅
        （它是纯函数：吃 demand + lexicon 出 bundle，不查库）
      · 但 ``evidence.build_lexicon(dataset)`` **必须连库** ❌
        （它每次从库里现读词表——这是对的设计：手写词表会和真实 schema 脱节）
      · 且 ``semantics.first_round`` **必须连库** ❌
        （``time_semantics`` 里的 ``_db_sem.query`` 查时间字段兜底）
    ⇒ 离线自证要跑通**真实**要素（而不是手搓 bundle），正确姿势是：
      **传 ``demand`` + ``lexicon`` 进 ``collect``，再把 ``slots`` 一并注入**
      （slots 由 ``build_elements_from_requirement(bundle=..., slots=...)`` 接收）。
    ⚠️ 这样得到的是**真实代码路径产出的真实要素**（不是合成的）——
      合成 bundle 一旦形状与真实不符，测的就不是线上那条路了。

    返回
    ----
    dict：bundle；取不到时返回 ``{"ok": False, "error": "..."}``（**不抛异常**）——
    与 ``requirement.get`` 的失败口径一致：取不到是"没取到"，不是"不合格"。
    """
    try:
        import evidence  # noqa: PLC0415  —— 按需导入，见上方「依赖纪律」
        import demand as demand_mod  # noqa: PLC0415
    except ImportError as e:
        return {"ok": False, "error": "证据模块不可导入：%s" % e}

    d = demand
    if d is None:
        try:
            d = demand_mod.get(demand_id)
        except Exception as e:  # noqa: BLE001  —— 取不到需求是常态，不该炸掉整条门禁
            return {"ok": False, "error": "取不到需求 %s：%s" % (demand_id, str(e)[:160])}
    if not isinstance(d, dict) or not d:
        return {"ok": False, "error": "需求 %s 不存在或为空" % demand_id}

    knowledge_mod = None
    if with_knowledge:
        try:
            import knowledge  # noqa: PLC0415
            knowledge_mod = knowledge
        except ImportError as e:
            return {"ok": False, "error": "知识模块不可导入：%s" % e}

    try:
        lex = lexicon
        if lex is None:
            lex = evidence.build_lexicon(dataset)
        return evidence.collect(
            demand=d, demand_id=demand_id, dataset=dataset,
            lexicon=lex, knowledge_mod=knowledge_mod,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "取证据失败：%s" % str(e)[:160]}


def build_elements_from_requirement(requirement, with_knowledge=False,
                                    use_cache=True, bundle=None, slots=None,
                                    demand=None, lexicon=None):
    """**从结构化需求对象**产出要素候选（给 ``gates.py`` 用的主入口）。

    参数
    ----
    requirement : ``requirement.get()`` 的返回（结构化技术需求对象）
    with_knowledge : 见 :func:`collect_bundle`
    use_cache : 是否走进程内 bundle 缓存
    bundle : **可选**。已备好的证据包；传了就直接用，**不反查、不连库**。
    slots  : **可选**。M3 槽位候选；不传则由 ``bundle`` 现算。
    demand : **可选**。已备好的需求单；传了则不查库（见 :func:`collect_bundle`）。
    lexicon : **可选**。已备好的词表；传了则不查库（见 :func:`collect_bundle`）。

    返回
    ----
    dict：``{"ok": bool, "elements": [...], "error": str, "cached": bool}``

    ⚠️ **为什么返回 dict 而不是 list**：门禁层要能区分
    "要素清单为空（业务方确实什么都没要）" 与 "取不到要素（环境/数据问题）"——
    前者是正常态，后者必须报出来。把两者混成空列表，
    就会重演阶段一那个病：**"取不到"看起来像"没问题"**。

    🔴 **``bundle`` 旁路为什么必须有**（不是可有可无的便利）
    ------------------------------------------------------
    生产路径（``gates.py``）手里只有 requirement 对象，**必须**走反查；
    但**离线自证**场景连不上 DB ⇒ 反查必然失败 ⇒ 入口恒返回 ``ok=False``
    ⇒ 覆盖判定永远 skipped ⇒ **判据层在离线环境里一行都测不到**。
    传 bundle 进来，这条路径就变成纯函数，可离线断言。
    ⚠️ 因此：**``bundle`` 与 ``slots`` 必须成对传**——
    只传 bundle 不传 slots 也能工作（slots 会现算，需要 bundle 完整），
    但只传 slots 不传 bundle 是无意义的（没有 bundle 就没有原文）。

    🔴🔴 **两路一致性契约（本模块最重要的不变式）**
    -------------------------------------------------
    本入口有**两条**产出要素的路径：

      · **生产路径**（``requirement`` 里有 ``demand_id``/``dataset``）
        ⇒ 反查需求原文 + 词表 → ``collect_bundle`` → ``semantics.first_round``
        → ``_elements_from_bundle``
      · **旁路**（显式传 ``bundle``，或传 ``demand``+``lexicon``）
        ⇒ 跳过反查，直接 ``_elements_from_bundle``

    ⚠️ **两路必须产出**逐项相同**的要素清单**（element/slot/polarity/confidence/
    matcher 全部一致）。理由：
      · 两条路径若各写一份出要素的逻辑，离线自证过了**线上可能不一样**
        ⇒ 离线自证就成了假绿；
      · 尤其「口径分叉」是**静默失效**：同一段原文在两条路径下结果不同，
        而报告里看不出任何异常。

    ⇒ 核心逻辑已抽成**唯一实现** ``_elements_from_bundle()``，两路共用它；
      且**必须**由自证钉住（当前：``tools/semantics_selftest.py`` 的 **J-7**
      「bundle 旁路产出与反查路径逐项相同（两条路径口径必须一致）」，
      另有 J-8「喂 demand+lexicon ⇒ 不连库也能出要素」）。
      ⚠️ **改动本入口的出要素逻辑时，那条断言必须仍然绿**——
      红了说明两路分了叉，不是「断言过时了」。

    槽位口径
    --------
    要素的槽位候选来自 **M3 slots**（``semantics.first_round`` 的 ``slots``），
    而结构化需求对象的槽位是 **M4 字段名**（subject/granularity/time_semantics/…）
    且**只有最终选中的那一个值**（``requirement.build`` 取 ``candidates[0]``）——
    它丢掉了被去重掉的候选（如同 locator 的多个字段命中），
    所以**不能**拿 requirement 对象当槽位候选来源：会重演"订单数从字段槽消失"。
    ⇒ 本入口用 ``requirement`` 里的 ``demand_id``/``dataset`` 重建 bundle，
      再用 M3 槽位（口径与 ``analysis.build`` 完全一致）做映射。
    """
    if bundle is not None:
        # -- 旁路：已备好 bundle，纯本地计算（不反查、不连库） --
        return _elements_from_bundle(bundle, slots, dataset=(
            (requirement or {}).get("dataset") if isinstance(requirement, dict)
            else None) or "B")

    # -- 旁路之二：已备好 demand + lexicon ⇒ 自己 collect（离线自证用） --
    # ⚠️ 为什么单独一条：``evidence.collect`` 是纯函数（吃 demand + lexicon 出
    #    bundle，不查库）⇒ 离线自证可以喂真实需求原文 + 真实词表，
    #    走**真实代码路径**产出**真实要素**，而不是手搓一个形状可能对不上的 bundle。
    if demand is not None:
        _ds = ((requirement or {}).get("dataset")
               if isinstance(requirement, dict) else None) or "B"
        _bid = ((requirement or {}).get("demand_id")
                if isinstance(requirement, dict) else None) or "offline"
        _b = collect_bundle(_bid, _ds, with_knowledge=with_knowledge,
                            demand=demand, lexicon=lexicon)
        if isinstance(_b, dict) and _b.get("ok") is False:
            return {"ok": False, "elements": [], "error": _b.get("error"),
                    "cached": False}
        return _elements_from_bundle(_b, slots, dataset=_ds)

    if not isinstance(requirement, dict) or requirement.get("ok") is False:
        return {
            "ok": False, "elements": [],
            "error": "结构化需求不可用：%s"
                     % str((requirement or {}).get("error")
                           or (requirement or {}).get("reason") or "")[:160],
            "cached": False,
        }
    demand_id = requirement.get("demand_id")
    dataset = requirement.get("dataset") or "B"
    if not demand_id:
        return {
            "ok": False, "elements": [],
            "error": "结构化需求缺 demand_id，无法反查原文（键=%s）"
                     % sorted(requirement.keys()),
            "cached": False,
        }

    # -- 取 bundle（带缓存） --
    cached = False
    bundle = None
    if use_cache:
        import demand as demand_mod  # noqa: PLC0415
        try:
            d = demand_mod.get(demand_id)
        except Exception as e:  # noqa: BLE001
            d = None
        fp = _demand_fingerprint(d) if isinstance(d, dict) else ""
        key = (demand_id, dataset, bool(with_knowledge), fp)
        hit = _BUNDLE_CACHE.get(key)
        if hit is not None:
            bundle, cached = hit, True
    if bundle is None:
        bundle = collect_bundle(demand_id, dataset, with_knowledge=with_knowledge)
        if isinstance(bundle, dict) and bundle.get("ok") is False:
            return {"ok": False, "elements": [], "error": bundle.get("error"),
                    "cached": False}
        if use_cache and len(_BUNDLE_CACHE) >= _BUNDLE_CACHE_MAX:
            _BUNDLE_CACHE.clear()   # 简单的容量闸门，避免长驻进程无限增长
        if use_cache:
            import demand as demand_mod  # noqa: PLC0415
            try:
                d = demand_mod.get(demand_id)
            except Exception as e:  # noqa: BLE001
                d = None
            fp = _demand_fingerprint(d) if isinstance(d, dict) else ""
            _BUNDLE_CACHE[(demand_id, dataset, bool(with_knowledge), fp)] = bundle

    # -- M3 槽位（口径与 analysis.build 一致）--
    try:
        import semantics  # noqa: PLC0415
        slots = (semantics.first_round(bundle, region_label=dataset) or {}).get("slots") or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "elements": [],
                "error": "M3 槽位计算失败：%s" % str(e)[:160], "cached": cached}

    return _elements_from_bundle(bundle, slots, dataset=dataset, cached=cached)


def _elements_from_bundle(bundle, slots, dataset="B", cached=False):
    """从 bundle + M3 槽位出要素（**两条入口路径共用的核心**）。

    ⚠️ 为什么要抽出来：``build_elements_from_requirement`` 有两条路径
    （反查 bundle / 调用方直传 bundle），若各写一份出要素的逻辑，
    两条路径的产出就可能不一致 ⇒ "离线自证过了、线上不一样"。
    共用一份 ⇒ 口径只有一处。
    """
    if slots is None:
        try:
            import semantics  # noqa: PLC0415
            slots = (semantics.first_round(bundle, region_label=dataset) or {}).get("slots") or {}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "elements": [],
                    "error": "M3 槽位计算失败：%s" % str(e)[:160], "cached": cached}
    try:
        elements = build_elements(bundle, slots)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "elements": [],
                "error": "要素抽取失败：%s" % str(e)[:160], "cached": cached}
    return {
        "ok": True,
        "elements": elements,
        "error": None,
        "cached": cached,
    }


#: 🔴 **粒度 matcher 守卫的失败记录**（每次守卫触发追加一条消息）。
#: ⚠️ **这是「进程内」记录**：门禁把每个自测跑在子进程里 ⇒ 父进程读不到子进程的记录。
#:   它的用途是：同一个进程内，调用方/自证能查"守卫报过什么"，
#:   而不必去翻日志。
#: ⚠️ 守卫**同时**写 stderr（见 :func:`_assert_base_granularity_intact`）——
#:   因为抛出的异常可能被调用方的 ``except Exception`` 降级成"静默跳过"，
#:   只靠异常不足以保证"判据被静默废掉这件事不能静默"。
GRANULARITY_GUARD_FAILURES = []

#: 🔴 **实例层自有的 matcher kind**（走我这 5 个 kind 的要素）。
#: ⚠️ 它们的判定函数**只会返回 MATCHED 或 UNDECIDABLE，从不返回 UNMATCHED**
#:    （见 :func:`register_instance_matchers` 的 docstring：业务名↔列的映射不可靠，
#:    报"未覆盖"等于撒谎）⇒ **凡走这些 kind 的要素，在 ``judge()`` 里判「不判」时
#:    必然是 UNDECIDABLE**（"我们没能力判"），而**不可能**是 may_omit。
#:    而 may_omit 在判据层第 ① 步就 ``continue`` 了，压根走不到 matcher。
#:
#: ⇒ 有了这张表，报告层就能**不反查 stats** 就把两类「不判」分开：
#:     may_omit        ⇒ 业务方**有意不要**（未提出 / 显式豁免）⇒ 用 ``cause``
#:     其余（UNDECIDABLE）⇒ 我们**没能力判**                ⇒ 显式标「未判定能力」
#:    ⚠️ 混成一句 = **把"没查"谎报成"不需要"** ⇒ 正是 REQ-COVERAGE 的原始病。
INSTANCE_MATCHER_KINDS = (
    MATCHER_KIND_FIELD, MATCHER_KIND_TIME, MATCHER_KIND_SCOPE,
    MATCHER_KIND_SUBJECT, MATCHER_KIND_SLOT,
)

#: 🔴 **底座自有的 matcher kind**（``reqcov.py`` import 时注册）。
#: ⚠️ 它们的判定函数**只会返回 MATCHED 或 UNMATCHED，从不返回 UNDECIDABLE**
#:    （例：``_match_time_granularity`` 认得粒度类别就 MATCHED，
#:    认不出的类别才 UNDECIDABLE）⇒ **走这些 kind 的要素判「不判」时
#:    只可能是 may_omit**（判据层第 ① 步就 continue 了，走不到 matcher）。
#: ⇒ 报告层若看到"走底座 kind 的要素判了不判"，那是**异常**，
#:    归入「原因不明」而不是硬塞进任何一类（塞进去 = 谎报）。
BASE_MATCHER_KINDS = (MATCHER_KIND_TIME_GRANULARITY,)


def not_judged_reason(element, judge_report=None):
    """这条要素判「不判」的**成因**（报告层分流用，不必反查 ``stats``）。

    返回三元组 ``(类别, 文案, 依据)``：
      · ``("业务方有意不要", cause, "polarity=may_omit")``
        —— 原文没提 / 业务方显式豁免。**这是"我说的不要"**。
      · ``("未判定能力", reason, "matcher 判不了")``
        —— 我们没能力判这一条。**这是"没查成"，不是"不需要"**。
      · ``("原因不明", <fallback>, "既非 may_omit 也未匹配已知 kind")``
        —— 兜底：**不静默**。出现它说明有第三种成因没被识别 ⇒ 报告层要显式说明，
        而不是把它混进任何一类（混进去 = 谎报）。

    ⚠️ 为什么要这个函数（而不是让报告层自己判断）：
    判据层 ``judge()`` 把两种「不判」归到**同一个** ``verdict="不判"`` +
    ``severity="none"``，只有 ``reason`` 文本不同 ⇒ **靠文本嗅探是脆的**
    （底座自己就批评过这个做法："把语义藏在字符串里，一旦有人改了措辞，
    判定就静默变样"）。
    ⇒ 本函数用**结构化信号**（``polarity`` + matcher kind）来分，不嗅探文本。

    参数
    ----
    element : 要素 dict（带 ``polarity`` / ``matcher`` / ``cause``）
    judge_report : ``judge()`` 对这条要素的明细（可选；给了就用它的 ``reason``）
    """
    el = element or {}
    if el.get("polarity") == POLARITY_MAY_OMIT:
        cause = el.get("cause")
        return (
            "业务方有意不要",
            str(cause) if cause else "需求未提出，不作要求",
            "polarity=may_omit",
        )
    kind = str(el.get("matcher") or "").partition(":")[0].strip()
    if kind in INSTANCE_MATCHER_KINDS:
        reason = (judge_report or {}).get("reason") or (
            "无法确认「%s」是否被覆盖（可能用了别的别名/列），不作缺口"
            % el.get("element", "")
        )
        return ("未判定能力", reason, "matcher=%s 判不了" % kind)
    # 兜底：显式说"原因不明"，不混进任何一类
    # ⚠️ 走底座 kind（time_granularity）的要素落到这里，说明它判了「不判」——
    #    而底座那条只会 MATCHED/UNMATCHED，**不可能**是 UNDECIDABLE ⇒ 这是异常。
    #    显式标"原因不明"而不是硬塞进某一类（塞进去 = 谎报）。
    reason = (judge_report or {}).get("reason") or "未提供判定明细"
    if kind in BASE_MATCHER_KINDS:
        return (
            "原因不明",
            reason,
            "底座 kind=%s 只会 MATCHED/UNMATCHED，判『不判』属异常" % kind,
        )
    return ("原因不明", reason, "既非 may_omit 也未匹配已知实例 kind")


def build_contract_from_requirement(requirement, with_knowledge=False,
                                    use_cache=True, bundle=None, slots=None,
                                    demand=None, lexicon=None):
    """同 :func:`build_elements_from_requirement`，但直接投影成底座 8 字段契约。

    给底座 ``reqcov.judge()`` 用（它只收 8 字段，多一个都拒收）。

    🔴🔴 **投影后 ``cause`` 永久丢失 —— 用本函数就拿不到成因**
    ------------------------------------------------------
    ``cause``（"需求未提出 X" / "业务方显式豁免 X"）是**实例层自有字段**，
    **不在** 8 字段契约里 ⇒ 本函数投影时把它剥掉 ⇒ **拿不回来了**。
    而底座 ``judge()`` 对**两种** ``may_omit``（未提出 / 显式豁免）写的是**同一句**：
        "polarity=may_omit（原文未提出或业务方显式豁免），不进覆盖判定"
    ⇒ **任何走本函数的调用方，报告里两种 may_omit 会长得一模一样**，
    分不出"业务方说了不要"与"业务方压根没提"。

    ⚠️ **这不是本函数的缺陷，是 8 字段契约的边界**（契约冻结，多一个字段就拒收）。
    ⇒ **要成因就别用本函数**：用 :func:`build_elements_from_requirement`
    （带 ``cause``）→ 自己调 :func:`to_contract_dicts` 投影给 ``judge()``，
    报告层另用带 ``cause`` 的那份做分流。
    ⚠️ 混成一句的代价 = **把"没查"谎报成"不需要"** ——
    业务方看到"不作要求"会以为是自己说的，实际是系统没能力判 ⇒
    正是 REQ-COVERAGE 那个原始病（"没查"看起来像"没问题"）。
    ⇒ 若报告层要把这两类分节渲染，**必须**走上面那条"两个入口"的路。
    """
    res = build_elements_from_requirement(
        requirement, with_knowledge=with_knowledge, use_cache=use_cache,
        bundle=bundle, slots=slots, demand=demand, lexicon=lexicon)
    if not res.get("ok"):
        return res
    return {
        "ok": True,
        "elements": to_contract_dicts(res["elements"]),
        "error": None,
        "cached": res.get("cached", False),
    }


# ---------------------------------------------------------------------------
# 4. 实例侧 matcher 登记（扩展点，不改底座骨架）
# ---------------------------------------------------------------------------
def register_instance_matchers():
    """把实例层自有的 matcher kind 登记到底座。

    语义一律**保守**：能确认已覆盖 ⇒ 已覆盖；确认不了 ⇒ 不判。
    ⚠️ 绝不报"未覆盖"——业务名↔列的映射在实例层还不可靠，
    误报缺口会变成假拦截（比漏报更危险：团队会因天天红而绕过门禁）。

    底座（``reqcov``）依赖 sqlglot，故此函数按需导入：
    实例层的其余部分（含本模块的导入）不依赖 sqlglot，裸解释器也能 import 与自证。
    """
    import reqcov  # noqa: PLC0415  —— 底座扩展点，按需导入

    def _norm(v):
        return re.sub(r"\s+", "", str(v or "")).lower()

    def _all_text(facts):
        out = []
        for key, alias, _agg in (facts.select_projections or []):
            out.append(str(alias or ""))
            out.append(str(key or ""))
        for key, _op, val in (facts.where_predicates or []):
            out.append(str(key or ""))
            out.append(str(val if val is not None else ""))
        out.extend(str(k or "") for k in (facts.group_by_keys or []))
        return [_norm(t) for t in out if t]

    def _has(facts, arg):
        needle = _norm(arg)
        if not needle:
            return False
        for t in _all_text(facts):
            if needle in t or t in needle:
                return True
        return False

    def _make(kind):
        def _fn(arg, facts):
            if _has(facts, arg):
                return reqcov.MATCHED, "SQL 中出现「%s」" % arg
            return (
                reqcov.UNDECIDABLE,
                "无法确认「%s」是否被覆盖（可能用了别的别名/列），不作缺口" % arg,
            )
        _fn.__name__ = "match_%s" % kind
        return _fn

    for kind in (
        MATCHER_KIND_FIELD,
        MATCHER_KIND_TIME,
        MATCHER_KIND_SCOPE,
        MATCHER_KIND_SUBJECT,
        MATCHER_KIND_SLOT,
    ):
        reqcov.register_matcher(kind, _make(kind))
    return sorted(reqcov.matcher_kinds_snapshot().keys())


def ensure_instance_matchers():
    """**幂等**地确保实例侧 matcher 已登记到底座；返回已登记的实例 kind 列表。

    为什么要有这个"确保"版
    ----------------------
    底座的 matcher 注册表是**进程内**的模块级 dict（``reqcov._MATCHER_KINDS``），
    ``judge()`` 直接查它 ⇒ **注册即被消费，没有额外的接线步骤**。
    但反过来：**没注册就不判**，且底座走的是"未注册 kind ⇒ 不判（并单列
    ``unjudged_unknown_matcher``）"那条分支 —— **不报错、门禁不红，
    但要素一条也没被判**。这就是静默失效的形态。

    所以调用方（``gates.py``）不必记得调 ``register_instance_matchers()``：
    调本函数即可，重复调用无害（已存在的不重注册）。

    🔴 **守卫底座的粒度 matcher 不被静默覆盖**
    -----------------------------------------
    底座自带 ``time_granularity``（``reqcov.py`` import 时注册），它按**类别**判
    （``date_trunc('month',x)`` / ``to_char(x,'YYYY-MM')`` / 裸列 ``stat_month`` /
    ``substr(x,1,7)`` 四种写法都归 month）—— 这是"同一需求换个写法就判未覆盖"
    这类假阳性的防火墙。
    ⚠️ 而 ``register_matcher`` 是 **dict 赋值 = 覆盖** ⇒ 若有人"顺手"把粒度
    也加进实例侧的 kind 列表，底座那条就被**静默替换** ⇒ 防火墙失效、
    且**不报错、不告警**。
    ⇒ 本函数在登记**前后**各查一次：``time_granularity`` 若已存在且**不是底座那个
    函数对象**，就抛错（不是警告 —— 警告会被忽略，而这是"判据被静默废掉"）。

    ⚠️ 返回值语义：``[]`` 表示**底座不可用**（``reqcov`` 依赖 sqlglot，
    裸解释器里 import 不了）—— 调用方须能区分"没注册"与"底座不在"，
    否则又是一次"取不到看起来像没问题"。
    """
    try:
        import reqcov  # noqa: PLC0415  —— 底座扩展点，按需导入
    except ImportError:
        return []
    snap = reqcov.matcher_kinds_snapshot()
    _assert_base_granularity_intact(reqcov, snap)
    mine = (MATCHER_KIND_FIELD, MATCHER_KIND_TIME, MATCHER_KIND_SCOPE,
            MATCHER_KIND_SUBJECT, MATCHER_KIND_SLOT)
    missing = [k for k in mine if k not in snap]
    if missing:
        register_instance_matchers()
    _assert_base_granularity_intact(reqcov, reqcov.matcher_kinds_snapshot())
    return sorted(k for k in mine if k in reqcov.matcher_kinds_snapshot())


def _assert_base_granularity_intact(reqcov, snap):
    """守卫：底座自带的 ``time_granularity`` matcher 没被覆盖。

    🔴 **为什么必须守卫**：``register_matcher`` 是 dict 赋值 = 覆盖。
    底座那条 ``time_granularity`` 按**类别**判粒度（4 种 SQL 写法都归 month），
    是"换个写法就判未覆盖"这类假阳性的防火墙。
    若被实例层"顺手"注册的同名 kind 覆盖 ⇒ 防火墙静默失效，
    表现为"某些 SQL 突然开始报粒度缺口" ⇒ **极难定位**（因为不报错）。
    ⇒ 在登记**前后**各查一次，变了就抛错。

    判据：函数对象的 ``__name__`` + ``__module__`` 必须仍是底座那个
    （``reqcov._match_time_granularity``）。
    ⚠️ 不用"签名相同"之类更弱的判据 —— 那是"看起来一样"，
    而我们要的是"就是那一个"。
    """
    fn = snap.get(MATCHER_KIND_TIME_GRANULARITY)
    if fn is None:
        # 底座没注册（正常不会发生：它在 import 时就注册了）
        return
    if (getattr(fn, "__name__", "") == "_match_time_granularity"
            and getattr(fn, "__module__", "") == "reqcov"):
        return
    msg = (
        "底座自带的 %r matcher 已被覆盖（当前实现：%s.%s）——"
        "它按类别判粒度（date_trunc/to_char/裸列/substr 四种写法都归 month），"
        "被替换会让『同一需求换个写法就判未覆盖』的假阳性防火墙静默失效。"
        "实例层**不得**注册这个 kind（底座已自带）。"
        % (MATCHER_KIND_TIME_GRANULARITY,
           getattr(fn, "__module__", "?"), getattr(fn, "__name__", "?"))
    )
    # 🔴 **留下两处痕迹**（抛异常可能被调用方降级成静默跳过，见下）：
    #   ① 模块级列表：可被同进程内的调用方/自证查询（不依赖日志）；
    #   ② stderr 告警：即使异常被 except 吞掉，**日志里仍有明确痕迹**。
    # ⚠️ 为什么要两处：守卫的意图是"判据被静默废掉这件事**不能静默**"，
    #   但当前调用点（``gates.py`` 的 ``_requirement_check``）把
    #   ``ensure_instance_matchers()`` 放在 **try 块内** ⇒ 这里的 RuntimeError
    #   会被那个 except 捕获 ⇒ 退化成 ``_skipped("要素清单构造失败…")``
    #   ⇒ **L-REQ 静默 skipped、门禁不红** —— 守卫的"响亮失败"被降级成了"静默跳过"。
    #   ⇒ 抛异常这一侧不足以保证"不能静默"，必须另有痕迹。
    GRANULARITY_GUARD_FAILURES.append(msg)
    sys.stderr.write(
        "\n[reqelem] 🔴 粒度 matcher 守卫触发：%s\n"
        "          ⇒ 底座的粒度判定防火墙已失效（『同一需求换个写法就判未覆盖』的假阳性）。\n"
        "          ⇒ 这条是**响亮失败**，不要把它当成普通异常忽略。\n" % msg)
    raise RuntimeError(msg)

