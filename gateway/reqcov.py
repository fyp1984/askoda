# -*- coding: utf-8 -*-
"""REQ-COVERAGE 阶段二 · 需求覆盖判定底座（**判据层**，零新增依赖）

这个模块是什么
--------------
REQ-COVERAGE 阶段一（已合入）只做「槽位齐备性」：**从不判红**。
需求要素（月度 / 订单数 / 已退款金额）被 SQL 丢掉时，五层门禁照样全绿、
``deliverable=true``、业务方拿到错数据、**全程零报错**。

阶段二做「覆盖判定」，但必须先解决一个**结构性**问题：判定会误拦。

⇒ 本模块严格只做**判据层**（纯函数、纯判定）：

    要素清单 + SqFacts  ⇒  每个要素的 verdict（已覆盖 / 未覆盖 / 不判）+ 整体 verdict

    判据层（纯判定）  ← 本模块：要素是否被覆盖 → verdict
    策略层（决定影响）  ← 第 4 步（队友做）：verdict 如何影响 status → blocking / warning / report-only

**为什么必须分层**：确定性规划器是当前唯一的 SQL 生成路径，若 L-REQ 一上来就
全量阻断 ⇒ 现网大面积打回 ⇒ 团队会因「天天红」而绕过门禁，**比不做更糟**；
但若一上来只 warning ⇒ 缺陷没真修，阶段二白做。
故：落地默认 ``report-only``（全 warning + 统计假阳性率与漏放清单），
策略层保留开关，随时能切到 ``blocking``。**判据层必须与策略层解耦**——
否则「默认不拦」会反过来把判据也一起放松掉。

本模块的硬约束
--------------
1. **零新增第三方依赖**：只用标准库 + ``sqlglot``（已在放行名单内）。
2. **纯函数**：无 IO、无 DB、无网络、无 LLM，可离线单测。
3. **契约冻结**：8 字段要素契约不多不少（见 :class:`ElementContract`）。
4. **复用现成能力**：归一化复用 ``rules._canonical_key`` / ``rules._select_alias_map``，
   **不另起一套**（另起一套会让「同一条 GROUP BY 在两处算得不一样」，
   而那正是阶段一踩过的 ``GROUP_BY_INCONSISTENT`` 误报坑）。
5. **粒度按「时间粒度类别」归类，不认具体函数名**：同一个「月度」需求，
   无论 SQL 写成 ``date_trunc('month', x)``、``to_char(x,'YYYY-MM')``、
   裸列 ``stat_month`` 还是 ``substr(x,1,7)``，都必须归到同一个类别。

四条判据的分工（**判据层 vs 策略层的边界就在这里**）
--------------------------------------------------
============================  ==========================================
本模块（判据层）              第 4 步（策略层，队友做）
============================  ==========================================
要素是否被覆盖 → verdict      verdict 如何影响 status
                               → blocking / warning / report-only
给出缺口明细 + 统计计数        统计假阳性率与漏放清单
                               （默认 report-only；开关切 blocking）
============================  ==========================================

⚠️ **本模块的输出不含「要不要判红」的策略**：``judge()`` 只回答
「覆盖了没有」，**不**回答「该不该拦」。任何把「红/黄/绿」写进本模块的改动
都是越界——它会让第 4 步的开关无处可插。
"""
import os
import sys

# 允许被 gateway 内其他模块 / tools 下自证脚本直接 import
# （与 rules.py / gates.py 的 import 约定一致：gateway 目录进 sys.path）
GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
if GATEWAY_DIR not in sys.path:
    sys.path.insert(0, GATEWAY_DIR)

import sqlglot
from sqlglot import exp

import rules as rules_mod  # noqa: E402  —— 复用 _canonical_key / _select_alias_map


# ===========================================================================
# 一、8 字段要素契约（**冻结**：不多、不少、不改名）
# ===========================================================================
#
# 契约形状（由需求方冻结）：
#   { "element": "月度", "slot": "granularity",
#     "provenance": "需求原文第2句：'各门店的月度销售额'",
#     "confidence": 0.85, "needs_confirmation": false, "mandatory": true,
#     "polarity": "must_cover",        // must_cover | may_omit
#     "matcher": "time_granularity:month" }
#
# 每个字段为什么存在（**不是凑数**，少一个就有一类真实缺陷抓不住）：
#   element            要覆盖的要素本身（人可读，用于报告）
#   slot               来自结构化需求的哪个槽位（用于回溯与定位）
#   provenance         需求原文出处（用于人工复核假阳性；**不是**置信度）
#   confidence         抽取置信度（决定能不能当「硬要求」用）
#   needs_confirmation  要素本身是否还需要人工确认（确认中 ⇒ 不能拿来阻断）
#   mandatory          业务上是否必答（**不参与**冻结三条件判定，见下方说明）
#   polarity           must_cover=必须覆盖 / may_omit=原文未提或已豁免
#   matcher            覆盖判定的**可执行描述**（kind:arg），见下文 MATCHER 说明
#
# ⚠️ 关于 mandatory 不参与判定：阶段一的教训是「结构化对象里『原文没提』与
#   『业务方显式豁免』都是 confidence=0，结构上无法区分」。
#   polarity 正是为区分这两者而引入的——**冻结的三条件只用 polarity 不用
#   mandatory**，两者的语义分工是：polarity 管「要不要覆盖」，
#   mandatory 管「业务上重不重要」（展示与统计用）。
#   两者若取值冲突，以 polarity 为准（它是判定用的那个）。
#
# ⚠️ provenance 与 confidence 不可互换：provenance 是**原文证据**（可人工复核），
#   confidence 是**抽取置信度**（模型自评）。阶段一的误拦正是拿 confidence=0
#   当「业务方没要求」而引发的。

#: 契约的 8 个字段名（顺序即契约顺序；**新增字段必须走契约变更流程**）
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

#: polarity 的合法取值
POLARITY_MUST_COVER = "must_cover"
POLARITY_MAY_OMIT = "may_omit"
POLARITIES = (POLARITY_MUST_COVER, POLARITY_MAY_OMIT)


class ElementContractError(ValueError):
    """要素契约非法（缺字段 / 类型错 / 取值非法 / 出现未知字段）。"""


class ElementContract(object):
    """需求要素的 8 字段契约容器（不可变）。

    只做两件事：**构造** 与 **校验**。
    ⚠️ **不引入任何新字段**——契约已冻结，见 :data:`CONTRACT_FIELDS`。
    ⚠️ **不引入 polarity 的判定逻辑**（那是实例层的活）：本类只校验
       ``polarity`` 的取值合法性，**不判断**一个要素该是 must_cover 还是 may_omit。

    校验为什么必须严格（而不是"宽容地补默认值"）
    --------------------------------------------
    宽容的校验 = 静默失效：上游漏传 ``matcher`` 时，本模块给个默认值，
    判定照跑、结论照出，**但结论是关于另一个问题的**——
    业务方看到「已覆盖」却根本没人检查过他要的东西。
    宁可**当场拒收**（调用方立刻炸），也不要产出一份看起来正常的错结论。
    """

    __slots__ = CONTRACT_FIELDS

    def __init__(self, element, slot, provenance, confidence,
                 needs_confirmation, mandatory, polarity, matcher):
        self.element = element
        self.slot = slot
        self.provenance = provenance
        self.confidence = confidence
        self.needs_confirmation = needs_confirmation
        self.mandatory = mandatory
        self.polarity = polarity
        self.matcher = matcher
        self.validate()

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_dict(cls, data):
        """从 dict 构造。**字段集合必须恰好等于契约的 8 个字段**。

        缺字段 → 拒收；多字段 → 拒收（多出来的字段说明上游用了旧版或自造变体，
        静默忽略它等于让「契约漂移」无声发生）。
        """
        if not isinstance(data, dict):
            raise ElementContractError(
                "要素必须是 dict，实际 %s" % type(data).__name__)
        got = set(data.keys())
        want = set(CONTRACT_FIELDS)
        missing = sorted(want - got)
        unknown = sorted(got - want)
        if missing:
            raise ElementContractError("要素缺少字段：%s" % "、".join(missing))
        if unknown:
            raise ElementContractError(
                "要素出现契约外字段：%s（契约已冻结，多余字段会让契约漂移无声发生）"
                % "、".join(unknown))
        return cls(**{k: data[k] for k in CONTRACT_FIELDS})

    # -- 校验 -------------------------------------------------------------
    def validate(self):
        """校验 8 个字段齐备且类型正确。任一不合规则抛 :class:`ElementContractError`。"""
        # 文本字段
        for name in ("element", "slot", "provenance", "polarity", "matcher"):
            val = getattr(self, name)
            if not isinstance(val, str):
                raise ElementContractError(
                    "字段 %s 必须是 str，实际 %s" % (name, type(val).__name__))
        for name in ("element", "slot", "matcher"):
            if not getattr(self, name).strip():
                raise ElementContractError("字段 %s 不得为空串" % name)
        # 布尔字段：必须是真 bool。⚠️ Python 里 bool 是 int 的子类，
        #   所以 isinstance(1, int) 为真——用 type(...) is bool 精确判定，
        #   否则 0/1 会被当成 False/True 混进来。
        for name in ("needs_confirmation", "mandatory"):
            val = getattr(self, name)
            if type(val) is not bool:
                raise ElementContractError(
                    "字段 %s 必须是 bool，实际 %s" % (name, type(val).__name__))
        # 置信度：数字（int/float），且排除 bool（bool 是 int 子类）
        conf = self.confidence
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            raise ElementContractError(
                "字段 confidence 必须是数字，实际 %s" % type(conf).__name__)
        if not (0.0 <= float(conf) <= 1.0):
            raise ElementContractError("字段 confidence 必须落在 [0,1]，实际 %r" % (conf,))
        # polarity 取值域
        if self.polarity not in POLARITIES:
            raise ElementContractError(
                "字段 polarity 必须是 %s 之一，实际 %r" % ("/".join(POLARITIES), self.polarity))
        # matcher：必须是 "kind:arg" 形态，kind 供策略层查表，arg 是判定参数
        if ":" not in self.matcher:
            raise ElementContractError(
                "字段 matcher 必须是 'kind:arg' 形态，实际 %r" % (self.matcher,))
        kind, _, arg = self.matcher.partition(":")
        if not kind.strip() or not arg.strip():
            raise ElementContractError(
                "字段 matcher 的 kind 与 arg 都不得为空，实际 %r" % (self.matcher,))
        return self

    # -- 输出 -------------------------------------------------------------
    def to_dict(self):
        """转回 dict（**恰好 8 个字段**，供 JSON 落库 / 报告输出）。"""
        return {k: getattr(self, k) for k in CONTRACT_FIELDS}

    def __eq__(self, other):
        if not isinstance(other, ElementContract):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self):
        return hash(tuple(str(getattr(self, k)) for k in CONTRACT_FIELDS))

    def __repr__(self):
        return "ElementContract(%s)" % ", ".join(
            "%s=%r" % (k, getattr(self, k)) for k in CONTRACT_FIELDS)


# ===========================================================================
# 二、粒度类别表（GRANULARITY_CLASSES）
# ===========================================================================
#
# 核心约束：**按「时间粒度类别」归类，不认具体函数名**。
#
# 同一条「月度」需求，SQL 有 4 种常见写法（架构师已实测 sqlglot 归一形态）：
#   date_trunc('month', order_date)  → exp.TimestampTrunc(unit=Var(MONTH))
#   to_char(order_date,'YYYY-MM')    → exp.TimeToStr(format=Literal('%Y-%m'))
#   stat_month（裸列）               → exp.Column(name='stat_month')
#   substr(order_date,1,7)          → exp.Substring
# 这 4 种**必须全部归到 month**——否则「同一需求换个写法就判不覆盖」，
#   假阳性率会高到没人信这门禁。
#
# 「判据骨架」与「实例规则」的分法（**本模块只出骨架 + 已实测的 4 条规则**）
# --------------------------------------------------------------------
# ⚠️ ``substr(x,1,7) → month`` 这条是**实例知识**：7 位截取在零售域只可能是
#   YYYY-MM，但**底座不该猜**（换个域 7 位可能是别的意思）。
#   故：底座提供「类别 + 判据骨架」（本表）+ **可注册的规则表**
#   （:func:`register_granularity_rule`），实例层据此登记自己的具体规则。
#
# 各判据字段的含义：
#   trunc_units      TimestampTrunc/DateTrunc 的 unit 参数（小写后匹配）
#   format_tokens    时间格式化串里的**记号**（"读 format 串，不读函数名"）
#   name_tokens      裸列名里的**词元**（ASCII 走分词相等，CJK 走包含）
#   prefix_lengths   定长截取的长度（"取前 N 位"）

#: 粒度类别表：类别 → 判据骨架
GRANULARITY_CLASSES = {
    "year": {
        "matcher": "time_granularity:year",
        "rank": 10,
        "trunc_units": ("year", "yyy", "yyyy", "y", "年"),
        "format_tokens": ("%Y", "%y", "YYYY", "YY"),
        "name_tokens": ("year", "年"),
        "prefix_lengths": (4,),
    },
    "quarter": {
        "matcher": "time_granularity:quarter",
        "rank": 20,
        "trunc_units": ("quarter", "qtr", "季"),
        "format_tokens": ("%Q", "Q"),
        "name_tokens": ("quarter", "季"),
        "prefix_lengths": (),
    },
    "month": {
        "matcher": "time_granularity:month",
        "rank": 30,
        "trunc_units": ("month", "mon", "mm", "月"),
        "format_tokens": ("%m", "MM"),
        "name_tokens": ("month", "月"),
        "prefix_lengths": (7,),
    },
    "week": {
        "matcher": "time_granularity:week",
        "rank": 40,
        "trunc_units": ("week", "wk", "周"),
        "format_tokens": ("%W", "IW"),
        "name_tokens": ("week", "周"),
        "prefix_lengths": (),
    },
    "day": {
        "matcher": "time_granularity:day",
        "rank": 50,
        "trunc_units": ("day", "dd", "日"),
        "format_tokens": ("%d", "DD"),
        "name_tokens": ("day", "日"),
        "prefix_lengths": (10,),
    },
    "hour": {
        "matcher": "time_granularity:hour",
        "rank": 60,
        "trunc_units": ("hour", "hr", "hh", "时"),
        "format_tokens": ("%H", "%I", "HH", "HH12", "HH24"),
        "name_tokens": ("hour", "时"),
        "prefix_lengths": (),
    },
}

#: 粒度类别名 → 类别定义（别名，便于外部按名取用）
GRANULARITY_BY_NAME = GRANULARITY_CLASSES


def _class_rank(class_name):
    """类别的精细度排名（越大越细）。用于「format 串里取最细的那一档」。"""
    spec = GRANULARITY_CLASSES.get(class_name)
    return spec["rank"] if spec else 0


def _class_with_rank(rank):
    """按 rank 反查类别名。"""
    best = None
    for name, spec in GRANULARITY_CLASSES.items():
        if spec["rank"] == rank:
            best = name
    return best


# -- 记号切分 ---------------------------------------------------------------
# strftime 风格（postgres 方言下 sqlglot 会把 'YYYY-MM' 规范化成 '%Y-%m'）：
#   ⚠️ 大小写有语义：%m=月、%M=分；%d=日、%H=时。
# Oracle/ANSI 风格（未规范化时）：YYYY=年、MM=月、DD=日、HH24=时、MI=分。
_FORMAT_TOKEN_RE_STRFTIME = None  # 惰性初始化，见 _format_tokens


def _format_tokens(fmt):
    """把时间格式化串切成记号列表。

    返回的记号带风格前缀（``%`` 开头 = strftime 风格；否则 = Oracle 风格），
    以便**精确**匹配 GRANULARITY_CLASSES 里的 ``format_tokens``——
    精确匹配是必须的：``%m``（月）与 ``%M``（分）只差大小写，
    用子串匹配会把「月」误判成「分」。
    """
    global _FORMAT_TOKEN_RE_STRFTIME
    if _FORMAT_TOKEN_RE_STRFTIME is None:
        import re as _re
        _FORMAT_TOKEN_RE_STRFTIME = _re.compile(r"%[A-Za-z]")
    if not fmt:
        return []
    if "%" in fmt:
        # strftime 风格：%Y / %m / %d ...
        return _FORMAT_TOKEN_RE_STRFTIME.findall(fmt)
    # Oracle / ANSI 风格：YYYY / MM / DD / HH24 ...
    import re as _re2
    return _re2.findall(r"YYYY|YY|MM|MI|DD|HH24|HH12|HH|SS|Q|WW|IW", fmt)


def _class_from_format(fmt):
    """由格式化串推出粒度类别：取串里**最细**的一档。

    例：``to_char(x,'YYYY-MM')`` 含年与月两档 ⇒ 取更细的 **month**。
    """
    tokens = _format_tokens(fmt)
    if not tokens:
        return None
    best_rank = 0
    for tok in tokens:
        for name, spec in GRANULARITY_CLASSES.items():
            if tok in spec["format_tokens"]:
                if spec["rank"] > best_rank:
                    best_rank = spec["rank"]
    return _class_with_rank(best_rank) if best_rank else None


def _class_from_name(col_name):
    """由裸列名推出粒度类别。

    ASCII 词元走**分词相等**（``stat_month`` → 词元 ``month`` 命中；
    ``monthly_amount`` → 词元 ``monthly`` **不**命中 ``month``，避免把
    「月度金额」这种指标列误当成粒度列）。
    CJK 词元走**包含**（中文没有词边界，``统计月份`` 含 ``月``）。
    """
    if not col_name:
        return None
    name = str(col_name).strip().lower()
    if not name:
        return None
    # ASCII 分词：非字母数字（含下划线）一律视为分隔符
    import re as _re
    words = set(w for w in _re.split(r"[^0-9a-z]+", name) if w)
    best_rank = 0
    for cls, spec in GRANULARITY_CLASSES.items():
        hit = False
        for tok in spec["name_tokens"]:
            if tok.isascii():
                if tok in words:
                    hit = True
                    break
            else:
                # CJK：包含即可（中文无词边界）
                if tok in name:
                    hit = True
                    break
        if hit and spec["rank"] > best_rank:
            best_rank = spec["rank"]
    return _class_with_rank(best_rank) if best_rank else None


def _class_from_trunc_unit(unit_name):
    """由 ``date_trunc``/``DATE_TRUNC`` 的 unit 参数推出粒度类别。"""
    if not unit_name:
        return None
    u = str(unit_name).strip().lower()
    for cls, spec in GRANULARITY_CLASSES.items():
        if u in spec["trunc_units"]:
            return cls
    return None


def _class_from_prefix_length(length):
    """由「取前 N 位」的长度推出粒度类别（``substr(x,1,7)`` → month）。"""
    try:
        n = int(length)
    except (TypeError, ValueError):
        return None
    for cls, spec in GRANULARITY_CLASSES.items():
        if n in spec["prefix_lengths"]:
            return cls
    return None


# -- 规则表（可注册：实例层登记自己的具体规则） -----------------------------
#
# 规则签名：``fn(expr) -> 类名 | None``。返回 None 表示「本规则不认这个表达式」。
# **按注册顺序求值，先命中先返回**——顺序即优先级。
# 底座自带 4 条已实测规则（架构师实测 4/4 正确归到 month/day/year）。

_GRANULARITY_RULES = []


def register_granularity_rule(name, fn, first=False):
    """注册一条粒度归类规则（实例层扩展点）。

    参数
    ----
    name : 规则名（仅用于报错定位与可读性）
    fn   : ``fn(expr) -> 类名 | None``；expr 是 sqlglot 表达式节点
    first: True ⇒ 插到最前（高优先级）；False ⇒ 追加到末尾（兜底）

    ⚠️ 实例层登记规则时**必须**保证「同一语义 ⇒ 同一类别」：
       判据层的一致性是门禁可信的前提，两套归类会让「覆盖了没有」变成薛定谔的。
    """
    if not callable(fn):
        raise ValueError("粒度规则必须是可调用对象")
    entry = (name, fn)
    if first:
        _GRANULARITY_RULES.insert(0, entry)
    else:
        _GRANULARITY_RULES.append(entry)
    return entry


def granularity_rules_snapshot():
    """返回当前规则表快照（只读副本），供自证脚本断言用。"""
    return list(_GRANULARITY_RULES)


# --- 底座自带的 4 条规则 ---

def _rule_timestamp_trunc(expr):
    """规则 1：``date_trunc(<unit>, x)`` → 由 **unit 参数**定类别。

    ⚠️ 读的是 unit（``MONTH``/``DAY``/``YEAR``…），**不是**函数名——
       「按类别归类，不认具体函数名」在此落地。
    """
    if not isinstance(expr, (exp.TimestampTrunc, exp.DateTrunc)):
        return None
    unit = expr.args.get("unit")
    if unit is None:
        return None
    unit_name = unit.name if hasattr(unit, "name") else str(unit)
    return _class_from_trunc_unit(unit_name)


def _rule_time_to_str(expr):
    """规则 2：``to_char(x, fmt)`` → 由 **format 串**定类别。

    ⚠️ 读的是 format 串，**不是**函数名——同一个 ``to_char``，
       ``'YYYY-MM'`` 是 month、``'YYYY-MM-DD'`` 是 day、``'YYYY'`` 是 year。
    """
    if not isinstance(expr, exp.TimeToStr):
        return None
    fmt = expr.args.get("format")
    if fmt is None:
        return None
    fmt_text = fmt.this if hasattr(fmt, "this") else str(fmt)
    if not isinstance(fmt_text, str):
        return None
    return _class_from_format(fmt_text)


def _rule_column_name(expr):
    """规则 3：裸列（``stat_month``）→ 由**列名**定类别。"""
    if not isinstance(expr, exp.Column):
        return None
    return _class_from_name(expr.name)


def _rule_substring_prefix(expr):
    """规则 4：定长截取（``substr(x,1,7)``）→ 由**截取长度**定类别。

    ⚠️ 只认「从第 1 位起取 N 位」（前缀语义）。``substr(x,3,7)`` 不是前缀，
       不套用本规则（那是另一种语义，不该被当成「月度」）。
    ⚠️ 长度→类别的对应是**实例知识**（7 位 = YYYY-MM 只在零售域成立）；
       实例层应据此登记自己的规则（见 :func:`register_granularity_rule`）。
    """
    if not isinstance(expr, exp.Substring):
        return None
    start = expr.args.get("start")
    length = expr.args.get("length")
    if start is None or length is None:
        return None
    try:
        if int(str(start.this)) != 1:
            return None
        n = int(str(length.this))
    except (TypeError, ValueError):
        return None
    return _class_from_prefix_length(n)


# 底座规则注册顺序 = 优先级：结构化表达式（trunc / format / 截取）先于
# 裸列名（列名判据最弱，误命中概率最高，放最后兜底）。
register_granularity_rule("timestamp_trunc_unit", _rule_timestamp_trunc)
register_granularity_rule("time_to_str_format", _rule_time_to_str)
register_granularity_rule("substring_prefix_length", _rule_substring_prefix)
register_granularity_rule("column_name_token", _rule_column_name)


def classify_granularity(expr):
    """把一个表达式归到时间粒度类别。无法判定时返回 None。

    ⚠️ **"无法判定"必须返回 None 而不是猜一个**——猜错会让「未覆盖」变成
       「已覆盖」（假阴性，缺陷放行），比误报更危险。
    """
    if expr is None:
        return None
    for _name, fn in _GRANULARITY_RULES:
        try:
            got = fn(expr)
        except Exception:
            # 单条规则出错不应让整条判定链崩掉（那会变成「门禁异常 ⇒ 放行」）
            got = None
        if got:
            return got
    return None


# ===========================================================================
# 三、SqFacts —— SQL AST 提取器
# ===========================================================================
#
# 产出三组**归一化**事实：
#   select_projections : [归一后的列键 + 别名 + 是否聚合]
#   group_by_keys      : [归一后的列键]
#   where_predicates   : [(列键, 运算符, 字面量)]
#
# 归一化为什么**必要**（不是洁癖）：
#   ``GROUP BY 1`` 与 ``GROUP BY store_name`` 必须归一到**同一个键**。
#   否则同一条 SQL 换个写法就判「未覆盖」⇒ 假阳性率飙升 ⇒ 门禁被绕过。
#   本模块的归一化**直接复用** ``rules._canonical_key``（已处理
#   Alias 剥离 / GROUP BY 序号解引用 / 别名解引用），
#   **不另起一套**——另起一套必然与 rules.py 的判据漂移。
#
# 附带产出 ``group_by_granularities``：列键 → 粒度类别的索引。
#   它**不是第四组事实**，而是 group_by_keys 的派生索引（判定时按类别查键）。
#   ⚠️ 为什么必须单独存：归一后的列键是**字符串**
#   （``date_trunc('month',order_date)``），类别信息在 AST 节点里，
#   字符串里推不出来（``to_char(x,'yyyy-mm')`` 的引号还会被 lower 掉）。

class SqFacts(object):
    """SQL 的归一化事实集合（不可变）。"""

    __slots__ = (
        "select_projections",
        "group_by_keys",
        "where_predicates",
        "group_by_granularities",
    )

    def __init__(self, select_projections, group_by_keys, where_predicates,
                 group_by_granularities=None):
        self.select_projections = tuple(select_projections)
        self.group_by_keys = tuple(group_by_keys)
        self.where_predicates = tuple(where_predicates)
        self.group_by_granularities = dict(group_by_granularities or {})
        self.validate()

    def validate(self):
        if any(not isinstance(k, str) for k in self.group_by_keys):
            raise ValueError("group_by_keys 必须全是 str")
        if any(not isinstance(p, tuple) or len(p) != 3 for p in self.where_predicates):
            raise ValueError("where_predicates 必须是 (列键, 运算符, 字面量) 三元组")
        if any(not isinstance(p, tuple) or len(p) != 3 for p in self.select_projections):
            raise ValueError("select_projections 必须是 (列键, 别名, 是否聚合) 三元组")
        return self

    def to_dict(self):
        """转成可 JSON 序列化的 dict（**键有序**，供逐字节比对用）。"""
        return {
            "select_projections": [list(p) for p in self.select_projections],
            "group_by_keys": list(self.group_by_keys),
            "where_predicates": [list(p) for p in self.where_predicates],
            "group_by_granularities": dict(self.group_by_granularities),
        }

    def granularity_classes(self):
        """返回本 SQL 里出现过的全部粒度类别（升序，无重复）。"""
        return sorted(set(self.group_by_granularities.values()))

    def has_granularity(self, class_name):
        """本 SQL 的 GROUP BY 里是否含指定粒度类别的键。"""
        return class_name in self.group_by_granularities.values()

    def __eq__(self, other):
        if not isinstance(other, SqFacts):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self):
        return "SqFacts(%r)" % (self.to_dict(),)


# -- 提取实现 ---------------------------------------------------------------

#: WHERE 子句里可识别的二元比较算子 → 规范算子名
_WHERE_BINARY_OPS = (
    (exp.EQ, "="),
    (exp.NEQ, "<>"),
    (exp.GT, ">"),
    (exp.GTE, ">="),
    (exp.LT, "<"),
    (exp.LTE, "<="),
    (exp.Like, "LIKE"),
    (exp.ILike, "ILIKE"),
)


def _literal_value(node):
    """把字面量节点转成 Python 值；非字面量返回 None。

    ⚠️ 数字字面量在 sqlglot 里 ``this`` 是**字符串**（``Literal(this='5')``），
       这里转成 int/float——否则「筛选值等于 5」与「筛选值等于 '5'」在下游
       没法区分，而那正是判断"筛选条件是否被满足"要用到的。
    """
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        if getattr(node, "is_string", False):
            return node.this
        text = str(node.this)
        try:
            return int(text)
        except (TypeError, ValueError):
            pass
        try:
            return float(text)
        except (TypeError, ValueError):
            return text
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Null):
        return None
    return None


def _column_key_of(node, alias_map, select_proj):
    """取表达式里的列并归一成列键；取不到列返回 None。"""
    if node is None:
        return None
    if isinstance(node, exp.Column):
        key = rules_mod._canonical_key(node, alias_map, select_proj)
        return key or None
    return None


def _extract_where_predicates(where_expr, alias_map, select_proj):
    """抽取 WHERE 里的谓词：``(列键, 运算符, 字面量)``。

    只抽「列 ⇄ 字面量」形态的谓词——只有它们能回答
    「这个筛选条件是不是被满足了」。列 ⇄ 列 / 列 ⇄ 子查询不抽
    （无法归一成字面量，纳入进来只会让判定看起来有依据、实际没有）。
    """
    out = []
    if where_expr is None:
        return out
    for node in where_expr.walk():
        op_name = None
        for cls, sym in _WHERE_BINARY_OPS:
            if isinstance(node, cls):
                op_name = sym
                break
        if op_name is not None:
            left, right = node.this, node.expression
            key = _column_key_of(left, alias_map, select_proj)
            val = _literal_value(right)
            if key is None and val is None:
                # 可能是 '字面量 = 列' 的反向写法
                key = _column_key_of(right, alias_map, select_proj)
                val = _literal_value(left)
                if key is None:
                    continue
                op_name = {"=": "=", "<>": "<>", ">": "<", ">=": "<=", "<": ">", "<=": ">="}.get(op_name, op_name)
            if key is None or val is None:
                continue
            out.append((key, op_name, val))
            continue
        if isinstance(node, exp.In):
            key = _column_key_of(node.this, alias_map, select_proj)
            if key is None:
                continue
            vals = []
            ok = True
            for item in (node.args.get("expressions") or []):
                v = _literal_value(item)
                if v is None:
                    ok = False
                    break
                vals.append(v)
            if ok and vals:
                out.append((key, "IN", tuple(vals)))
            continue
        if isinstance(node, exp.Between):
            key = _column_key_of(node.this, alias_map, select_proj)
            if key is None:
                continue
            lo = _literal_value(node.args.get("low"))
            hi = _literal_value(node.args.get("high"))
            if lo is None or hi is None:
                continue
            out.append((key, "BETWEEN", (lo, hi)))
            continue
        if isinstance(node, exp.Is):
            key = _column_key_of(node.this, alias_map, select_proj)
            if key is None:
                continue
            if not isinstance(node.expression, exp.Null):
                continue
            # ⚠️ `IS NOT NULL` 在 sqlglot 里是 **Is(negate=True)**，
            #    不是 Not(Is(...))——曾按 parent 判 Not 而漏掉 NOT 形态。
            #    （实测：sqlglot 30.20.0 的 args = {this, expression, negate}）
            negated = bool(node.args.get("negate"))
            out.append((key, "IS NOT NULL" if negated else "IS NULL", None))
    return out


def _effective_group_expr(raw_expr, alias_map, select_proj, proj_by_key):
    """取 GROUP BY 表达式的**有效表达式**（用于粒度归类）。

    为什么需要这一步（归一化与归类是两件事）
    ----------------------------------------
    ``GROUP BY 1`` 的 AST 是 ``exp.Literal(1)``，``GROUP BY 月份`` 的 AST 是
    ``exp.Column(月份)``——**都不含** ``date_trunc``，直接归类必然得到 None。
    而它们的**归一化列键**与 ``GROUP BY date_trunc('month', x)`` 完全相同
    （``rules._canonical_key`` 已做序号/别名解引用）。

    ⇒ 做法：用**归一化列键**把 GROUP BY 项与 SELECT 投影对上，
      取投影里的**原始 AST**（未被字符串化），再对它归类。
      这样既复用了现成的归一化能力，又不丢 AST 结构。

    ⚠️ ``to_char(x,'YYYY-MM')`` 的归一化列键是 ``to_char(x,'yyyy-mm')``
      （引号内小写化），**不能**靠字符串反推类别——所以必须走 AST。
    """
    if raw_expr is None:
        return None
    if isinstance(raw_expr, exp.Alias):
        return raw_expr.this
    key = rules_mod._canonical_key(raw_expr, alias_map, select_proj)
    hit = proj_by_key.get(key)
    if hit is not None:
        return hit.this if isinstance(hit, exp.Alias) else hit
    return raw_expr


def extract_facts(sql, dialect="postgres"):
    """把 SQL 抽成 :class:`SqFacts`。

    参数
    ----
    sql : 待分析的 SQL 文本
    dialect : sqlglot 方言，默认 ``postgres``（与 gates.py / rules.py 一致）

    ⚠️ 解析失败**不静默**：抛 :class:`ValueError`。
       解析不出 AST 就返回「空事实」，下游会据此判「什么都没覆盖」⇒ 全量缺口
       ⇒ 假阳性风暴。宁可让调用方明确知道「这条 SQL 我读不了」。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL 为空，无法提取事实")
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:
        raise ValueError("SQL 解析失败：%s" % exc)
    if ast is None:
        raise ValueError("SQL 解析结果为空")

    # WITH 语句：取主查询（With.this）；否则取语句本身
    stmt = ast
    if isinstance(stmt, exp.With):
        stmt = stmt.this if stmt.this is not None else stmt
    if not isinstance(stmt, exp.Select):
        # 非 SELECT（如 INSERT）：交由 L1/L3 层判，本模块不产出事实
        return SqFacts((), (), (), {})

    select_proj = list(stmt.expressions) if hasattr(stmt, "expressions") else []
    alias_map = rules_mod._select_alias_map(stmt)

    # -- 投影：(列键, 别名, 是否聚合) --
    projections = []
    proj_by_key = {}
    for expr in select_proj:
        if isinstance(expr, exp.Star):
            projections.append(("*", "", False))
            continue
        key = rules_mod._canonical_key(expr, alias_map, select_proj)
        alias = expr.alias if isinstance(expr, exp.Alias) else ""
        has_agg = any(isinstance(a, exp.AggFunc) for a in expr.walk())
        projections.append((key, alias, bool(has_agg)))
        if key and key not in proj_by_key:
            proj_by_key[key] = expr

    # -- GROUP BY：归一化列键 + 粒度类别 --
    gb_keys = []
    gb_gran = {}
    group = stmt.args.get("group")
    if group is not None:
        for raw in group.expressions:
            key = rules_mod._canonical_key(raw, alias_map, select_proj)
            if not key:
                continue
            if key not in gb_keys:
                gb_keys.append(key)
            eff = _effective_group_expr(raw, alias_map, select_proj, proj_by_key)
            cls = classify_granularity(eff)
            if cls:
                gb_gran[key] = cls

    # -- WHERE --
    where = stmt.args.get("where")
    predicates = _extract_where_predicates(
        where.this if where is not None else None, alias_map, select_proj)

    return SqFacts(projections, gb_keys, predicates, gb_gran)


# ===========================================================================
# 四、matcher 注册表（覆盖判定的可执行描述）
# ===========================================================================
#
# ``matcher`` 字段的形态是 ``"kind:arg"``：
#   kind = 判定方式（如 ``time_granularity``）
#   arg  = 判定参数（如 ``month``）
#
# 每个 kind 对应一个判定函数：``fn(arg, facts) -> 判定结果``。
# 判定结果是一个**三元组** ``(状态, 说明)``，状态是：
#   ``MATCHED``    已覆盖
#   ``UNMATCHED``  未覆盖（缺口）
#   ``UNDECIDABLE`` 无法判定（**不是缺口**，但必须显式留痕）
#
# 底座实现 ``time_granularity``（阶段二的主用例）；
# 其余 kind（指标、维度等）由实例层登记（见 :func:`register_matcher`）。
#
# ⚠️ **未注册的 kind 不得静默当作「已覆盖」**——那等于「没查却显示查过了」，
#    正是 REQ-COVERAGE 要修的那个病。judge() 对未注册 kind 判「不判」
#    并在统计里单列（unjudged_unknown_matcher），让缺席显式可见。
#
# ⚠️ 为什么用**三态**而不是「(bool, str)」：bool 无法表达「无法判定」，
#    而「无法判定」与「未覆盖」的处置**完全不同**——前者不该算缺口
#    （否则底座能力不足会变成假阳性风暴，进而让门禁被绕过），
#    但也**绝不能**当成已覆盖（那又回到静默失效）。
#    早先用「说明里含'无法判定'字样」来嗅探，是把**语义藏在字符串里**——
#    一旦有人改了措辞，判定就静默变样。改为显式三态。

MATCHED = "matched"
UNMATCHED = "unmatched"
UNDECIDABLE = "undecidable"

_MATCHER_KINDS = {}


def register_matcher(kind, fn):
    """注册一个 matcher kind（实例层扩展点）。

    ``fn(arg, facts) -> (状态, 说明)``：
      状态取 :data:`MATCHED` / :data:`UNMATCHED` / :data:`UNDECIDABLE`，
      说明是人可读的一句话（会进报告，供人工复核假阳性）。
    """
    if not kind or not isinstance(kind, str):
        raise ValueError("matcher kind 必须是非空 str")
    if not callable(fn):
        raise ValueError("matcher 判定函数必须可调用")
    _MATCHER_KINDS[kind] = fn
    return kind


def matcher_kinds_snapshot():
    """返回已注册 kind 的快照（只读副本）。"""
    return dict(_MATCHER_KINDS)


def _match_time_granularity(arg, facts):
    """``time_granularity:<类别>``：SQL 的 GROUP BY 里是否含该粒度类别。

    ⚠️ **按类别判，不按实现判**：``date_trunc('month',x)``、
       ``to_char(x,'YYYY-MM')``、裸列 ``stat_month``、``substr(x,1,7)``
       四种写法都归到 month，任一种出现即视为该粒度已被覆盖。
       否则「同一需求换个写法就判未覆盖」⇒ 假阳性率飙升。
    """
    cls = (arg or "").strip().lower()
    if not cls:
        return UNDECIDABLE, "matcher 未给出粒度类别，无法判定"
    if cls not in GRANULARITY_CLASSES:
        return UNDECIDABLE, "未知粒度类别 %s，无法判定" % cls
    if facts.has_granularity(cls):
        matched = sorted(k for k, c in facts.group_by_granularities.items() if c == cls)
        return MATCHED, "GROUP BY 含 %s 粒度（%s）" % (cls, "、".join(matched))
    present = facts.granularity_classes()
    if present:
        return UNMATCHED, "GROUP BY 粒度为 %s，未覆盖 %s" % ("、".join(present), cls)
    return UNMATCHED, "GROUP BY 未含任何时间粒度，未覆盖 %s" % cls


register_matcher("time_granularity", _match_time_granularity)


# ===========================================================================
# 五、judge() —— 分级判定（**纯函数**，判据层）
# ===========================================================================
#
# 冻结的**分级阻断口径**（需求方冻结，不可改）：
#
#   阻断 = (confidence ≥ 0.6) AND (needs_confirmation == False) AND (polarity == must_cover)
#   警告 = 其余全部
#   不判 = 原文未提出 / 业务方显式豁免
#
# ⚠️ 三者的**优先级**：不判 > 阻断 > 警告。
#   「不判」必须先判：原文没提 / 业务方已豁免的要素，**根本不该进入覆盖判定**
#   （它不是「没被覆盖」，是「不需要被覆盖」）。若先判阻断，
#   一个 confidence=0 的 may_omit 要素会被算成缺口 ⇒ 假阳性。
#
# ⚠️ 与阶段一误拦教训的对应：
#   阶段一把「原文没提时间」的需求判红了（requirement.py 的
#   ``{"value": "无候选结论…", "confidence": 0.0}`` 既是"槽位缺失"
#   又是"业务方没要求"的结构化表示）。
#   阶段二靠 ``polarity`` 把两者**结构上分开**：may_omit ⇒ 不判。
#
# 输出形状（**供第 4 步策略层消费**）：
#   {
#     "verdict": "已覆盖" | "未覆盖" | "不判",
#     "elements": [ {element, slot, matcher, verdict, severity, reason, ...}, ... ],
#     "gaps":     [ 缺口明细（未覆盖要素的完整信息） ],
#     "stats":    {统计计数（含假阳性/漏放统计所需的口径）},
#   }
#
# ⚠️ 输出**不含**"要不要判红"：verdict 说的是"覆盖了没有"，
#   "该不该拦"是第 4 步策略层的开关。这里多写一个 status 字段，
#   第 4 步就无处插开关。

#: verdict 取值
VERDICT_COVERED = "已覆盖"
VERDICT_UNCOVERED = "未覆盖"
VERDICT_NOT_JUDGED = "不判"

#: 严重度取值（**仅描述"如果拦会是什么级别"**，不表示"拦不拦"）
SEVERITY_BLOCKING = "blocking"
SEVERITY_WARNING = "warning"
SEVERITY_NONE = "none"

#: 冻结口径：置信度门槛
BLOCKING_CONFIDENCE_THRESHOLD = 0.6


def _severity_of(element):
    """按**冻结三条件**算严重度。

    返回 ``(severity, reason)``：
      * 阻断：confidence ≥ 0.6 且 needs_confirmation 为 False 且 polarity=must_cover
      * 警告：其余全部
    """
    if element.polarity != POLARITY_MUST_COVER:
        return SEVERITY_NONE, "polarity=%s，不进覆盖判定" % element.polarity
    if float(element.confidence) < BLOCKING_CONFIDENCE_THRESHOLD:
        return SEVERITY_WARNING, "confidence=%s < %s，按警告处理" % (
            element.confidence, BLOCKING_CONFIDENCE_THRESHOLD)
    if element.needs_confirmation:
        return SEVERITY_WARNING, "needs_confirmation=True，要素待人工确认，不作硬要求"
    return SEVERITY_BLOCKING, "满足冻结三条件（confidence=%s、待确认=False、must_cover）" % (
        element.confidence,)


def judge(elements, facts):
    """判定要素清单相对 SQL 的覆盖情况（**纯函数**）。

    参数
    ----
    elements : 要素清单。可传 :class:`ElementContract` 列表，或 dict 列表
               （dict 会被 :meth:`ElementContract.from_dict` 严格校验）。
    facts    : :class:`SqFacts`（由 :func:`extract_facts` 产出）

    返回
    ----
    dict：
      ``verdict``   整体 verdict（已覆盖 / 未覆盖 / 不判）
      ``elements``  逐要素判定明细
      ``gaps``      缺口明细（未覆盖的要素）
      ``stats``     统计计数

    ⚠️ **不判**的三种来源（都必须**显式**出现在明细与统计里，绝不静默）：
      ① ``polarity=may_omit``：原文未提 / 业务方已豁免 —— 不是缺口；
      ② matcher 的 kind 未注册：底座**没能力**判这一条 —— 不能当"已覆盖"，
         也不该算缺口（那会变成假阳性风暴），单列统计让缺席可见；
      ③ matcher 判定函数返回 :data:`UNDECIDABLE`：同上处理。
    """
    if facts is None:
        raise ValueError("judge() 需要 SqFacts，不能为 None")
    if not isinstance(facts, SqFacts):
        raise ValueError("judge() 的 facts 必须是 SqFacts，实际 %s" % type(facts).__name__)

    # -- 要素归一为契约对象 --
    contracts = []
    for idx, raw in enumerate(elements or []):
        if isinstance(raw, ElementContract):
            contracts.append(raw)
        elif isinstance(raw, dict):
            try:
                contracts.append(ElementContract.from_dict(raw))
            except ElementContractError as exc:
                raise ElementContractError("第 %d 个要素非法：%s" % (idx, exc))
        else:
            raise ElementContractError(
                "第 %d 个要素类型非法：%s" % (idx, type(raw).__name__))

    element_reports = []
    gaps = []
    counts = {
        "total": len(contracts),
        "covered": 0,
        "uncovered": 0,
        "not_judged": 0,
        "blocking": 0,
        "warning": 0,
        "unjudged_unknown_matcher": 0,
        "unjudged_undecidable": 0,
    }

    for ec in contracts:
        kind, _, arg = ec.matcher.partition(":")
        kind = kind.strip()
        arg = arg.strip()
        report = {
            "element": ec.element,
            "slot": ec.slot,
            "matcher": ec.matcher,
            "polarity": ec.polarity,
            "confidence": ec.confidence,
            "needs_confirmation": ec.needs_confirmation,
            "mandatory": ec.mandatory,
            "provenance": ec.provenance,
        }

        # ① 不判：polarity=may_omit（原文未提 / 业务方已豁免）
        if ec.polarity != POLARITY_MUST_COVER:
            report["verdict"] = VERDICT_NOT_JUDGED
            report["severity"] = SEVERITY_NONE
            report["reason"] = "polarity=may_omit（原文未提出或业务方显式豁免），不进覆盖判定"
            counts["not_judged"] += 1
            element_reports.append(report)
            continue

        # ② 未注册的 matcher kind：底座没能力判 ⇒ 不判（并单列统计）
        fn = _MATCHER_KINDS.get(kind)
        if fn is None:
            report["verdict"] = VERDICT_NOT_JUDGED
            report["severity"] = SEVERITY_NONE
            report["reason"] = "matcher kind=%r 未注册，底座无判定能力（缺席显式报未执行）" % kind
            counts["not_judged"] += 1
            counts["unjudged_unknown_matcher"] += 1
            element_reports.append(report)
            continue

        matched, detail = fn(arg, facts)
        if matched == MATCHED:
            report["verdict"] = VERDICT_COVERED
            report["severity"] = SEVERITY_NONE
            report["reason"] = detail
            counts["covered"] += 1
            element_reports.append(report)
            continue

        if matched == UNDECIDABLE:
            # 判定函数明确说"无法判定" ⇒ 不判（不是缺口），单列统计
            report["verdict"] = VERDICT_NOT_JUDGED
            report["severity"] = SEVERITY_NONE
            report["reason"] = detail
            counts["not_judged"] += 1
            counts["unjudged_undecidable"] += 1
            element_reports.append(report)
            continue

        # 缺口：按冻结三条件定严重度
        severity, sev_reason = _severity_of(ec)
        report["verdict"] = VERDICT_UNCOVERED
        report["severity"] = severity
        report["reason"] = "%s；%s" % (detail, sev_reason)
        counts["uncovered"] += 1
        if severity == SEVERITY_BLOCKING:
            counts["blocking"] += 1
        else:
            counts["warning"] += 1
        gaps.append(dict(report))
        element_reports.append(report)

    # -- 整体 verdict --
    if counts["uncovered"] > 0:
        overall = VERDICT_UNCOVERED
    elif counts["covered"] > 0:
        overall = VERDICT_COVERED
    else:
        # 全部不判（含要素清单为空）⇒ 不判
        overall = VERDICT_NOT_JUDGED

    return {
        "verdict": overall,
        "elements": element_reports,
        "gaps": gaps,
        "stats": counts,
    }
