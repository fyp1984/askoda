# -*- coding: utf-8 -*-
"""M5-1 · SQL 门禁（生成与审查分离）

设计说明
--------
按《实施推进与验收方案》§6 M5 与《调研-SQL测试与自动优化》§3.1 落地。
核心约束：
- 一期只新增 sqlglot 一个第三方依赖（SQLFluff / Great Expectations 留成可插拔插槽，默认关闭）
- 「生成」与「审查」严格分离：本模块只做审查，不生成 SQL
- 阻断 vs 警告分层：L1/L2(blocking 规则)/L3/L4 不通过则阻断，L2 其余规则只记警告不阻断，L5 无 exec_result 时 skipped
- REQ-COVERAGE 阶段一（2026-10-19）：新增 L-REQ「需求要素」层，排在 L1 之前。
  它审的是「结构化需求齐不齐备」，L1~L5 审的是「SQL 对不对」——两者答的不是
  同一个问题。**无需求输入（requirement 为 None / 取不到）时 L-REQ 判 skipped，
  绝不判红**，以免给现有不带 demand_id 的调用点造出全新失败模式。
- REQ-COVERAGE 阶段二（2026-10-20）：L-REQ 从「槽位齐备性」换成**需求要素覆盖判定**
  （需求要素清单 vs SQL 实际输出）。判据在 gateway/reqcov.py（judge，纯函数），
  **策略在本文件**：默认 report-only（有阻断级缺口也只留痕、不改 status），
  置 REQ_COVERAGE_MODE=blocking 即整体切到阻断。⚠️ 两个开关各管一件事：
  本文件的 blocking_layers 管「ok=False 是否传导到 status」，
  环境变量管「什么情况下置 ok=False」——详见 review() 里的注释。
- M6-1 改造：L2 10 类静态规则**整体搬迁**到 gateway/rules.py 的 REGISTRY；
  gates.py 只保留 L1~L5 接线层（+ is_write 辅助函数）。新增一条规则 = 只改 rules.py，
  不需要再进 gates.py 改 _static_checks。
- ⚠️ 层职责别按名字猜，按 layers[] 里的 ("layer","name") 字面为准：
  **L1=语法校验、L2=静态风险、L3=只读校验、L4=语义校验、L5=结果断言**。
  只读防线在 **L3**（判 _READONLY_ROOT 白名单）；**is_write 不是门禁的一层**，
  它与 L3 同判 _READONLY_ROOT 但只吐 bool，`review()` 主链路不调用它
  （详见其 docstring）。本模块的写节点黑名单常量已删除，只读判据唯一来源是
  _READONLY_ROOT。

插槽
----
- SQLFLUFF_ENABLED=1  启用 SQLFluff lint（一期未实现，留壳）
- GX_ENABLED=1       启用 Great Expectations 断言（一期未实现，留壳）
"""
import os
import sys

import sqlglot
from sqlglot import exp

# 确保 gateway/ 在 sys.path 里（与 rules.py / registry.py / metadata.py 的 import 一致）
GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
if GATEWAY_DIR not in sys.path:
    sys.path.insert(0, GATEWAY_DIR)

import rules as rules_mod  # noqa: E402

_SQLFLUFF_ON = os.getenv("SQLFLUFF_ENABLED", "").strip() in ("1", "true", "TRUE", "on", "ON")
_GX_ON = os.getenv("GX_ENABLED", "").strip() in ("1", "true", "TRUE", "on", "ON")


# L-REQ · 阶段二（REQ-COVERAGE）**策略层**配置。
#
# 🔴 本段属**策略层**，不是判据层。冻结的判据口径全部在 gateway/reqcov.py 里
#   （_severity_of / judge），本文件**不重算**严重度、不复制三条件。
#   分层的理由：reqcov_selftest.py 用断言钉死了「judge() 输出不含
#   status / blocked / should_block 等策略字段」——判据层一旦混入策略，
#   第4 步就无处插开关（这正是本文件存在的意义）。
#
# 判据层（reqcov.judge）出的是逐要素 verdict + gaps + stats；
# **「要不要判红」由本文件决定**，由下面这个开关切换。
#
# 🔴 为什么默认 report-only（不阻断）
# -------------------------------------------------------------------
# deterministic-planner 是当前**唯一**的 SQL 生成路径。覆盖判定一上来就全量阻断，
# 等于把现网所有「字段名对不上」的 SQL 全部打回——而实例层 matcher 对
# 指标/维度类的语义明确是**保守的**（判不了就报 UNDECIDABLE，见
# reqelem.register_instance_matchers 的 docstring），真正会红的主要是
# 「月度这类时间粒度没进 GROUP BY」。
#   ⚠️ 更现实的风险：**门禁天天红，团队就会绕过门禁**——那比不做门禁更糟。
#   ⇒ 默认只留痕（notes + requirement_gaps），不改变 status；
#     契约一旦稳定，用 REQ_COVERAGE_MODE=blocking 即可整体切到阻断。
#
# ⚠️ 取值**每次调用都重新读环境变量**（不在 import 时定死）：否则自证脚本
#   在同一进程里切不动模式，策略层就成了写死的假开关。
# ⚠️ 只认 blocking 一个真值；其余（含拼错、大小写、空串）一律落 report-only
#   ——「开关写错」绝不能等于「静默变成全量阻断」。
_REQ_COVERAGE_MODE_ENV = "REQ_COVERAGE_MODE"
_REQ_MODE_REPORT = "report-only"
_REQ_MODE_BLOCKING = "blocking"


def _req_coverage_mode():
    """读当前覆盖判定的策略模式。

    返回 **模式字符串**（``"report-only"`` 或 ``"blocking"``），不是 bool。
    ⚠️ 必须返回字符串而不是 bool：调用点是 `mode == _REQ_MODE_BLOCKING`
       这种**与常量比字符串**的写法；早先返回 bool 时该比较恒为 False，
       `blocking` 模式静默失效——**症状是「切了开关但门禁照旧不拦」**，
       没有任何报错。这正是本项目在修的那类静默失效，故在此显式记录。
    """
    v = (os.getenv(_REQ_COVERAGE_MODE_ENV, "") or "").strip().lower()
    return _REQ_MODE_BLOCKING if v == _REQ_MODE_BLOCKING else _REQ_MODE_REPORT

# L-REQ 每条 detail 的固定前缀：自述本层**查了什么、没查什么**。
#
# 为什么必须出现在 detail（用户可见）而不是只写在 docstring（代码注释）
# ---------------------------------------------------------------------
# REQ-COVERAGE 要修的病就是**静默失效**：requirement 恒 None、各层全绿、
# deliverable=true、业务方拿错数据、全程零报错。若L-REQ 静默通过却不自述
# 「我查的是覆盖判定、哪些没查」，等于把同一个洞挖在新位置——业务方看到
# L-REQ 绿了会以为「需求要素已全部落实」。
#
# 「缺席必须显式报未执行」这条原则，同样适用于**「在场但只做了一半」**：
# report-only 模式下有阻断级缺口却放行，必须在 detail 里说清「为什么没拦」
# （见 _req_gap_lines 里的模式说明行）。
#
# ⚠️ 措辞随阶段演进：阶段一前缀写的是「仅校验槽位齐备性…覆盖校验见阶段二」，
#   阶段二本层已换成覆盖判定，故前缀同步改成覆盖判定口径。
#   ⚠️ **常量名 _REQ_SCOPE_PREFIX 不要改名**：tools/gates_selftest.py 的 7b-6
#   按「源码里每个产出 detail 的 return 都引用它」做结构性扫描，改名会让那条
#   判据扫不到任何东西从而**静默失效**（不是变红，是再也不拦了）。
_REQ_SCOPE_PREFIX = (
    "本层校验需求要素覆盖判定（需求要素清单 vs SQL 实际输出：时间粒度看 GROUP BY、"
    "指标/维度看 SQL 中是否出现），阻断口径为「置信度≥0.6 且待确认=False 且 must_cover」；"
    "未查：SQL 写得对不对（语法/只读/语义分别由 L1/L3/L4 负责）。\n"
    "  "
)

# 只读白名单：L3 只读校验与 is_write() **判同一个东西**，都以「根节点是不是
# Select/With」为唯一依据。
#
# ⚠️ 这里曾有一个并列的「写节点黑名单」常量（Insert/Delete/Update/Drop/Alter/
#   Create/TruncateTable/Command），已删除——它**不是「清理未使用变量」，
#   而是移除一个已被同一套判据取代的旧策略残留**：
#   黑名单是「枚举已知写节点」，白名单是「除只读外皆拒」。后者覆盖前者——
#   写节点清单永远补不全（Command 可承载任意 DDL/DML），白名单不需要维护。
#   两处判定现在完全等价（本文件的 L3 与 is_write 同判下方这个元组），
#   故黑名单那份副本不再有任何信息量。
#   ⚠️ 别把它当冗余再加回来：加了只会让「读只认 Select/With」这条契约
#   出现第二处需要同步的定义，改一处忘一处就是静默漏拦。
_READONLY_ROOT = (exp.Select, exp.With)


def is_write(sql: str) -> bool:
    """判断 SQL 是否含写操作。

    ⚠️ **它不是门禁的一层**。门禁里的只读防线是 **L3「只读校验」**
       （见 review()，判据同为 _READONLY_ROOT，产出 layer 结构 + rule_violations）。
       本函数 `review()` 主链路**零调用**——全仓调用点只有本定义、
       文件头提及与 tools/gates_selftest.py 的自测。它是给调用方做
       **前置判断**用的：只要一个 bool，不参与门禁分层、不产出 detail。

    与 L3 的关系：**同源判据，但粒度更粗**，不是冗余（故不要合并/删除）
    ------------------------------------------------------------------
    · 同源：两者都只判 `isinstance(stmt, _READONLY_ROOT)`（本元组），
      判据完全一致——**不存在「两套标准」**，不会出现「is_write 放行但 L3 拦」。
    · 粒度更粗：L3 逐条语句遍历、**分别产出**违规类型并给出可读 detail
      （多语句 → `MULTI_STATEMENT`；非只读根 → `WRITE_OPERATION_<TYPE>`）；
      本函数只吐一个 bool，调用方拿不到「为什么被拒」也拿不到是哪一条语句。
    · ⚠️ **不要说成「等价」**：等价会诱导后人当冗余删掉。它不是冗余——
      L3 面向门禁层（要 layer 结构给 review_notes 拼串），本函数面向调用前置
      判断（只要 bool，不需要为它造一份 layers 条目）。

    三条边界（均已实测，行为已固化进 tools/gates_selftest.py 附加-8/8b）
    ------------------------------------------------------------------
    1. **多语句 → 直接返回 True，不区分读写两种违规**。
       ⚠️ L3 对同一输入会**分别产出两条** rule_violation：
       一条 `MULTI_STATEMENT`、一条 `WRITE_OPERATION_*`（`SELECT 1; SELECT 2`
       在 L3 眼里第二条也是违规）。本函数把两者压成同一个 True，
       调用方若要区分成因，**必须查 L3，不能靠本函数的返回值**。
    2. **空串 / 纯空白 / 纯注释 → 返回 True**。
    3. **解析异常 → 返回 True**。

    ⚠️ 边界 2 的成因**不是「保守设计」**，实测（门禁子进程 / askoda 容器
       两个环境 sqlglot 版本不同、但行为一致）：
           ''     -> len=1  stmts=[None]
           '   '  -> len=1  stmts=[None]
           '-- c' -> len=1  stmts=[None]
       即 `parse()` 返回的是**长度 1 的 `[None]`**（不是空列表），
       于是走到 `not isinstance(None, _READONLY_ROOT)` 分支——
       **成因是「None 不是只读节点」**，与「保守」无关。
    ⚠️ 由此推论：**若将来 _READONLY_ROOT 扩充**，边界 2 的行为会跟着变
       （例如把 None 纳入白名单，退化输入就会转为 False）。
       契约钉的是**当前行为**，不是某个设计意图——行为变了要先查清
       sqlglot 版本差异，再改这里的期望值，不要直接改契约迁就实现。
    """
    try:
        stmts = sqlglot.parse(sql, read="postgres")
    except Exception:
        return True
    if len(stmts) != 1:
        return True
    return not isinstance(stmts[0], _READONLY_ROOT)


def _lint_sqlfluff(sql: str) -> dict:
    """SQLFluff lint 插槽（一期未实现，留壳）。"""
    return {"skipped": True, "reason": "一期未启用"}


def _gx_assert(exec_result) -> dict:
    """Great Expectations 断言插槽（一期未实现，留壳）。"""
    return {"skipped": True, "reason": "一期未启用"}


# ---------------------------------------------------------------------------
# L-REQ · 阶段二：需求要素**覆盖判定**（判据层 reqcov + 策略层本文件）
# ---------------------------------------------------------------------------
#
# 🔴 分层纪律（本阶段唯一不许含糊的地方）
# ---------------------------------------------------------------------------
#   判据层 = gateway/reqcov.py：回答「这个要素**覆盖了没有**」。
#     · ElementContract（8 字段契约）、SqFacts（SQL 事实）、extract_facts、
#       judge()、_severity_of（冻结三条件）全在那儿。
#     · judge() **不含** status / blocked / should_block 等策略字段——
#       reqcov_selftest.py 有断言钉死这一点。
#   策略层 = 本文件：回答「覆盖没覆盖**要不要拦**」。
#     · 唯一开关 _req_coverage_mode()（默认 report-only）。
#     · 本文件**不重算严重度、不复制三条件**，只读 judge() 给的 severity。
#
#   ⚠️ **禁的是「在策略层重写三条件」，不是「`>= 0.6` 这个写法本身」**：
#       · `confidence >= 0.6` 本身是**冻结口径、是对的**（阻断级阈值 0.6，取等号即 ≥）。
#         本文件 L357 的「阻断 = (confidence ≥ 0.6) AND …」是**照抄口径的说明文字**，
#         不是可执行判定；写成 `>=` 才是对的（改成 `<` 就会把「恰好 0.6」漏出去）。
#       · 三条件的**唯一权威实现**在 `gateway/reqcov.py` 的 `_severity_of()`；
#         策略层（gates.py）**只读** judge() 给的 severity，不自己算、不复制条件。
#       · 真正的禁忌是：在本文件里**再写一遍** `confidence >= 0.6 and ...` 这类**可执行判定**——
#         那样判据与策略就焊在一起，改判据要改两处，两处可能不一致 ⇒ 静默漏拦。
#         ⚠️ 曾因把「禁重写」误读成「`>= 0.6` 写法本身是错的」，
#            导致 reqcov.py 的阈值判据被写成 `<=` ⇒ 置信度**恰好 0.6** 的要素
#            永远进不了阻断级 ⇒ 需求要素缺失在主链路上静默放过（已修正为 `<`）。

# may_omit 的两种成因文案（供详情行使用）
# ------------------------------------------------------------------------------
# ⚠️ 真正的区分依据是实例层 reqelem 产出的 `cause` 字段：judge() 对两种 may_omit
#   写的是**同一句**「polarity=may_omit（原文未提出或业务方显式豁免）」，
#   光看 judge() 输出分不开。而 `cause` 只存在于 build_elements_from_requirement
#   的返回里（to_contract_dicts 投影 8 字段时会被剥掉）
#   ⇒ **本模块必须用带 cause 的入口**。
# ⚠️ 兜底文案刻意用「未提及」而非「未提出」，与实例层真实 cause 区分词不同：
#   万一 cause 缺失，宁可文案不够准，也不能让「未提出」与「显式豁免」
#   撞成同一句——tools/gates_selftest.py 的 7c-2 / 7c-6 就是靠这两句
#   不同来钉契约的（assertNotEqual）。
_CAUSE_NOT_STATED = "原文未提及"
_CAUSE_FALLBACK = "%s该要素，不作要求（成因未知：实例层未给 cause）"
def _skipped(detail):
    """skipped 返回路径的统一收口。

    ⚠️ ok 恒为 True：skipped 的层不参与总状态（见 review() 的
      has_blocking_fail 同时要求 ok=False 且 skipped=False）。
      把 ok 写成 False 会让「没查」看起来像「查了且不合格」。
    """
    return {
        "ok": True, "skipped": True, "gaps": [],
        "detail": _REQ_SCOPE_PREFIX + detail,
    }


def _not_judged_lines(elements, reports, reqelem_mod):
    """「不判」要素逐条列出，**并按成因分流**（委托实例层reqelem.not_judged_reason）。

    ⚠️ 「不判」有**来源完全不同**的成因，混成一句话就是误导：
      ① 业务方**有意不要**（原文未提出 / 显式豁免，polarity=may_omit）
         —— 这是「我说的不要」，用实例层给的`cause` 说明。
      ② **我们没能力判**（matcher 判不了，UNDECIDABLE）
         —— 这是「没查成」，**不是**「不需要」。写成「不作要求」等于
         把「没查」谎报成「不需要」，那正是 REQ-COVERAGE 的原始病。
      ③ **原因不明**（第三种成因没被识别）⇒ 必须显式标出，**不混进任何一类**。

    🔴 **为什么委托 reqelem.not_judged_reason() 而不在这里自己判**
    -------------------------------------------------------------------
    分流依据必须是**结构化信号**（`polarity` + matcher kind），**不能嗅探reason 文本**。
    底座自己就批评过文本嗅探（「把语义藏在字符串里，一旦有人改了措辞，判定就静默变样」，
    见 reqcov.py 关于三态的注释）⇒ 我这里若写 `if "无法判定" in reason:`，
    队友改一次措辞，两类成因就会静默互换 —— 又一条永不触发的死规则。
    ⇒ 实例层把这条分流做成了**可测的纯函数**（并自证「改reason 文本不影响分类」）。

    ⚠️ 返回三元组 (waived, unable, unknown)，调用方**必须都消费**：
      只渲染 waived 会让「未判定能力」/「原因不明」整堆信息消失。
    """
    by_name = {e.get("element"): e for e in (elements or [])}
    waived, unable, unknown = [], [], []
    for rep in (reports or []):
        if rep.get("verdict") != "不判":
            continue
        name = rep.get("element")
        el = by_name.get(name) or {}
        category, text, basis = reqelem_mod.not_judged_reason(el, rep)
        line = "  - %s：%s" % (name, text)
        if category == "业务方有意不要":
            waived.append(line)
        elif category == "未判定能力":
            unable.append(line)
        else:
            #⚠️ 不塞进任何一类：混进去 = 谎报。显式说明「成因未识别」。
            unknown.append("  - %s：%s（依据：%s）" % (name, text, basis))
    return waived, unable, unknown


def _gap_lines(gaps):
    """缺口逐条。**不只报数量**——缺口要能被业务方直接拿去改 SQL。"""
    return [
        "  - [%s] %s（%s）：%s" % (
            "阻断" if g.get("severity") == "blocking" else "警告",
            g.get("element"), g.get("slot"), g.get("reason", ""))
        for g in (gaps or [])
    ]


def _not_judged_sections(waived, unable, unknown):
    """把「不判」的三类成因渲染成分节文案。

    ⚠️ 三节各自独立、不互相顶替：
      · 「业务方未要求」= 有意不要（may_omit）
      · 「未判定能力」   = 我们没能力判（UNDECIDABLE）—— **不是不需要**
      · 「成因未识别」   = 兜底：出现它就说明有第三种成因没被识别
        ⇒ **必须显式报出来**，混进上面任何一节都是谎报。
    ⚠️ 「未判定能力」为空时**也要说清有没有**：若判据层哪天把某个 kind 从已
      注册表里删掉，业务方会看到缺口从 N 条变成 0 条却看不到任何解释，
      那与静默失效同构。

    🔴 为什么「未判定能力」**逐项列出而不折叠成计数**
    ------------------------------------------------
    实例层（队友实测，DR-20261020-1P4K）：SQL 里写的是 `s.store_name`，
    而需求写的是「门店」⇒ 中文业务名与英文列名对不上 ⇒ 判 UNDECIDABLE。
    ⚠️ 但同一条 SQL 里 `AS 销售额` 的中文别名**能精确命中** ⇒ 判「已覆盖」。
    ⇒ 「未判定能力」这一节里，**有的其实已覆盖、只是认不出**。
    业务方看到「销售额明明在 SQL 里，怎么说没判」会困惑。

    ⇒ 解法是**在节标题后加一句限定**，把「判不了」的原因说清，
      **不是**折叠成计数（折叠会丢信息：业务方不知道该查哪几项；
      且若将来某个 kind 真返回 UNMATCHED，混在计数里看不见 ⇒ 又是静默失效）。
    ⇒ 这句限定是**必需**的：没有它，「未判定」会被读成「未覆盖」。
    """
    lines = []
    if waived:
        lines.append("以下要素业务方未要求（不进覆盖判定）：")
        lines.extend(waived)
    if unable:
        lines.append("以下要素**未判定能力**（底座 matcher 判不了，不算缺口也不等于已覆盖）：")
        # ⚠️ 这句限定**必须在这里、且必须在逐项清单之前**：
        #   少了它，业务方会把「判不了」读成「没覆盖」⇒ 去改本来正确的 SQL。
        lines.append(
            "⚠️ 说明：这里「判不了」多因**业务名与 SQL 里的列名/别名对不上**"
            "（例：需求写「门店」，SQL 里是 store_name）⇒ **不代表没覆盖**，"
            "也**不是缺口**；要确认请人工核对该要素对应的列。")
        lines.extend(unable)
    if unknown:
        # 🔴 显式标「成因未识别」：宁可让业务方困惑，也不能谎报成「不需要」。
        lines.append("⚠️ 以下要素判「不判」但**成因未识别**（可能是第三种成因，需人工复核）：")
        lines.extend(unknown)
    if not (waived or unable or unknown):
        lines.append("无「不判」要素。")
    return lines


def _requirement_check(requirement, sql=None):
    """L-REQ · 需求要素层（REQ-COVERAGE 阶段二：**覆盖判定** + 分级阻断）。

    返回 {"ok", "skipped", "detail", "gaps"}，形状与 L1-L5 对齐。

    为什么单独成层、且排在 L1 之前
    ------------------------------
    L1-L5 审的是「SQL 本身对不对」，答不了「需求要的东西在不在」——
    这正是 REQ-COVERAGE 缺陷能各层全绿的原因：SELECT 语法没错、只读、语义层认得、
    可 GROUP BY 与输出列一致，唯独「月度」「订单数」「已退款金额」三项
    从未进入过任何一层的视野。L-REQ 补的是这个洞，故排在最前。

    本函数做什么（判据层 → 策略层）
    ------------------------------
    1. 从结构化需求对象出发，经**实例层 reqelem** 产出 8 字段要素契约；
    2. 从 SQL 提取事实（reqcov.extract_facts）；
    3. 交判据层 reqcov.judge() 出逐要素 verdict + gaps（**纯函数，无策略**）；
    4. 策略层按 _req_coverage_mode() 决定 ok 是否为 False（默认 report-only）。

    🔴 三条硬约束（阶段一立下，阶段二继续有效）
    ----------------------------------------------
    1. **无可比对需求 ⇒ skipped，绝不判红**。归到 skipped 的情形：未传
       requirement / 类型异常 / requirement.get 取不到 / 无 SQL /
       实例层取不到要素或算出**空**清单。
       ⚠️ 尤其「要素清单为空」必须报出来——把「抽不出要素」显示成「全绿」
       正是 REQ-COVERAGE 的原始病。
    2. **只报告，不改写**。本层只产出 gaps，绝不触碰 revised_sql
       （红线 D1：门禁失败时自动改写 SQL 是被机械判据 check_r3_d1 盯着的）。
    3. **detail 必须自述范围**。每一条返回路径的 detail 都带 _REQ_SCOPE_PREFIX。

    🔴 阻断 vs 警告（三条件冻结，只在 reqcov._severity_of 里实现）
    --------------------------------------------------------
        阻断 = (confidence ≥ 0.6) AND (needs_confirmation == False) AND (polarity == must_cover)
        警告 = 其余全部
        不判 = 原文未提出 / 业务方显式豁免（polarity=may_omit）
    ⚠️ 本函数**照抄**这个口径会与判据层脱钩：改判据要改两处 ⇒ 静默漏拦。

    🔴 为什么 report-only 模式下「有阻断级缺口也不拦」
    --------------------------------------------------
    deterministic-planner 是当前唯一生成路径，一上来全量阻断会大面积打回现网，
    而实例层 matcher 对指标/维度明确保守（判不了 ⇒ UNDECIDABLE，不算缺口）。
    **门禁天天红，团队就会绕过门禁**——那比不做门禁更糟。
    ⇒ 缺口照样进 requirement_gaps 与 review_notes（不静默），只是不改 status。

    参数
    ----
    requirement : requirement.get() 的返回（结构化技术需求对象），或 None
    sql         : 待审查 SQL（用于提取 SqFacts）。None/空 ⇒ 无法比对 ⇒ skipped。

    ⚠️ 槽位口径（勿绕开）：传入对象字段名是设计文档口径
       subject / granularity / time_semantics / data_scope / output_fields /
       residual_risks（见 requirement.SLOT_MAP），**不是** M3 slots 的
       time / scope / fields / risks——直取后者会 KeyError。
       本函数**不直接读这些字段**（那会重演「订单数从字段槽消失」的老问题），
       一律委托实例层 reqelem 走bundle 重建路径。
    """
    # -- 需求不可用 → skipped（与「需求不合格」严格区分） --
    if requirement is None:
        return _skipped(
            "跳过原因：未提供结构化需求（调用方未传 demand_id），本层不参与总状态。")
    if not isinstance(requirement, dict):
        return _skipped(
            "跳过原因：结构化需求类型异常（%s），本层不参与总状态。"
            % type(requirement).__name__)
    if requirement.get("ok") is False:
        # requirement.get 查不到时返回的就是 {"ok": False, "error": ...}。
        # 这是「取不到」，不是「不合格」——skipped 并把原因写进 detail。
        return _skipped(
            "跳过原因：结构化需求不可用（%s），本层不参与总状态。"
            % str(requirement.get("error") or requirement.get("reason") or "")[:160])
    if sql is None or str(sql).strip() == "":
        # 无 SQL 可比对 ⇒ 判不了覆盖。这不是「覆盖通过」。
        return _skipped(
            "跳过原因：无待审查 SQL，无法提取 SqFacts 做覆盖比对，本层不参与总状态。")

    # -- 实例层（reqelem）：拿 8 字段契约要素。⚠️ 必须用带 cause 的入口 --
    #
    # ⚠️ 按需导入：reqelem 依赖 sqlglot（经reqcov），顶层导入会把
    #   「门禁子进程能 import gates.py」这条离线契约拖上数据库/词表依赖。
    try:
        import reqcov as reqcov_mod      # noqa: PLC0415
        import reqelem as reqelem_mod    # noqa: PLC0415
        # 实例侧 matcher **就地幂等注册**：不注册则 field_present 等 5 个 kind
        # 在 judge() 里全判「不判 + 未注册」，字段类要素永远进不了覆盖判定
        # ——且**不报错、门禁不红**，是静默失效的形态。
        # ⚠️ 用ensure_instance_matchers() 而不是 register_instance_matchers()：
        #   前者幂等，且会在登记前后守卫「底座自带的 time_granularity 没被覆盖」
        #   （那条按类别判粒度，是「换个写法就判未覆盖」的假阳性防火墙；
        #   被静默替换后不报错、极难定位）。详见该函数 docstring。
        # ⚠️ 返回 [] 表示「底座不可用」（裸解释器里 reqcov 依赖 sqlglot）——
        #   这种情况本层本就该skipped，不当成注册成功。
        _registered_kinds = reqelem_mod.ensure_instance_matchers()
        if not _registered_kinds:
            return _skipped(
                "跳过原因：覆盖判定底座不可用（reqcov 依赖 sqlglot，当前环境无法加载），"
                "本层不参与总状态。")
        got = reqelem_mod.build_elements_from_requirement(requirement)
    except Exception as e:  # noqa: BLE001
        # 取要素失败 ⇒ skipped，**绝不判红**：抽不出要素不等于需求没被满足。
        return _skipped(
            "跳过原因：要素清单构造失败（%s: %s），本层不参与总状态。"
            % (type(e).__name__, str(e)[:160]))
    if not isinstance(got, dict) or got.get("ok") is False:
        return _skipped(
            "跳过原因：取不到需求要素清单（%s），本层不参与总状态。"
            % str((got or {}).get("error") or "未知原因")[:200])
    elements = got.get("elements") or []
    if not elements:
        # ⚠️ 空要素清单是「没抽出来」，不是「需求没什么可要求」。
        # 报 skipped 是保守方向：不阻断、但把缺席显式写出来。
        return _skipped(
            "跳过原因：需求要素清单为空（本轮未能从需求原文抽出任何要素），"
            "无法做覆盖判定，本层不参与总状态。")

    # -- SQL 事实（解析失败由 L1 报红，本层不重复报、也不自己判红） --
    try:
        facts = reqcov_mod.extract_facts(sql)
    except Exception as e:  # noqa: BLE001
        return _skipped(
            "跳过原因：SQL 无法提取事实（%s: %s），覆盖判定未执行"
            "（语法问题请看 L1），本层不参与总状态。"
            % (type(e).__name__, str(e)[:160]))

    # -- 判据层：judge() 是纯函数，只出 verdict/gaps/stats，不含任何策略字段 --
    try:
        judgement = reqcov_mod.judge(reqelem_mod.to_contract_dicts(elements), facts)
    except Exception as e:  # noqa: BLE001
        return _skipped(
            "跳过原因：覆盖判定失败（%s: %s），本层不参与总状态。"
            % (type(e).__name__, str(e)[:160]))

    stats = judgement.get("stats") or {}
    gaps = list(judgement.get("gaps") or [])
    waived, unable, unknown = _not_judged_lines(
        elements, judgement.get("elements"), reqelem_mod)
    mode = _req_coverage_mode()

    if not gaps:
        # 无缺口 ⇒ 本层通过。仍要说清「判了什么、判了多少」，不能只写「通过」。
        body = [
            "覆盖判定通过：%d 项要素中已覆盖 %d、不判 %d、缺口 0。"
            % (stats.get("total", 0), stats.get("covered", 0), stats.get("not_judged", 0)),
        ]
        body.extend(_not_judged_sections(waived, unable, unknown))
        return {
            "ok": True, "skipped": False, "gaps": [],
            "detail": _REQ_SCOPE_PREFIX + "\n".join(body),
        }

    # -- 有缺口：策略层落地（唯一一处决定 ok 的地方） --
    body = [
        "检出覆盖缺口：阻断 %d / 警告 %d（共 %d 项要素，其中已覆盖 %d、不判 %d）。"
        % (stats.get("blocking", 0), stats.get("warning", 0),
           stats.get("total", 0), stats.get("covered", 0), stats.get("not_judged", 0)),
    ]
    body.extend(_gap_lines(gaps))
    body.extend(_not_judged_sections(waived, unable, unknown))
    # 🔴 只有 **severity==blocking** 的缺口才判红；warning 级缺口**永不**判红。
    #   ⚠️ 这里曾写成「只要 mode==blocking 就 ok=False」（判的是「有没有缺口」），
    #     那会让「待人工确认的要素未覆盖」这种**明确不作硬要求**的情形也拦下来
    #     —— 与冻结口径「警告 = 其余全部」直接冲突，且是 7c-4 实测抓到的。
    #   ⇒ 判据（哪个缺口该拦）在 reqcov._severity_of；本层只做「按它执行」。
    _blocking_gaps = [g for g in gaps if g.get("severity") == "blocking"]
    if mode == _REQ_MODE_BLOCKING and _blocking_gaps:
        body.append("策略：blocking —— 存在 %d 项阻断级缺口，本层判红（status=不通过）。"
                    % len(_blocking_gaps))
        ok = False
    elif mode == _REQ_MODE_BLOCKING:
        body.append("策略：blocking —— 检出 %d 项缺口，但**全部为警告级**"
                    "（置信度不足或要素待人工确认，不作硬要求），本层**不判红**。"
                    % len(gaps))
        ok = True
    else:
        body.append(
            "策略：report-only —— 存在 %d 项阻断级缺口但**本层未阻断**"
            "（缺口已进 requirement_gaps 与 review_notes，总状态不变）；"
            "置 %s=blocking 后此处将判红。" % (len(_blocking_gaps), _REQ_COVERAGE_MODE_ENV))
        ok = True

    # gaps 列表：给 review_notes 与 requirement_gaps 用。
    # ⚠️ 必须带severity 标记：策略层已判完，但下游 notes 要能区分
    #   「阻断级」与「警告级」，否则业务方分不清该先修哪个。
    gap_texts = ["[%s] %s：%s" % (
        "阻断" if g.get("severity") == "blocking" else "警告",
        g.get("element"), g.get("reason", "")) for g in gaps]

    return {
        "ok": ok, "skipped": False, "gaps": gap_texts,
        "detail": _REQ_SCOPE_PREFIX + "\n".join(body),
    }


def _static_checks(ast_stmt, ctx=None) -> list:
    """遍历 AST，顺序执行 rules.REGISTRY 中**全部**静态规则，结果按 REGISTRY 的 blocking 标记分级（blocking→阻断 / 其余→警告）。

    为什么加 ctx 参数：R1（1:N 关系）、R5（未建模对象）、R6（时间字段）三类规则
    必须拿到 MDL（relationships / models / columns / 时间候选列）才能判定；
    ctx 是从调用方（review 函数）把 dataset + mdl_index 透传到规则的唯一通道。

    返回形状与调用约定**一字未改**：
      返回 list[dict]，每项 = {"rule": str 规则 ID, "snippet": 原始可读 SQL 片段, "detail": 中文说明}
    """
    if ctx is None:
        ctx = {"dataset": "B", "mdl_index": None, "requirement": None}
    return rules_mod.evaluate_all(ast_stmt, ctx)


def _result_assertions(exec_result):
    """仅当 exec_result 非空时执行。返回 (ok, signals)。"""
    signals = []
    cols = exec_result.get("columns") or []
    data = exec_result.get("data") or []
    n = len(data)

    if n == 0:
        signals.append("结果集为空（0 行），请检查过滤条件或时间范围是否过窄")
    elif n > 100000:
        signals.append("结果集行数 %d 超过上限 100000，请增加过滤条件或下调颗粒度" % n)

    if n > 0 and cols:
        empty_risk_cols = []
        for i, col in enumerate(cols):
            all_null = True
            for row in data:
                if i < len(row):
                    v = row[i]
                    if v is not None and v != "":
                        all_null = False
                        break
            if all_null:
                empty_risk_cols.append(col)
        if empty_risk_cols:
            signals.append("以下列全部为空：" + "、".join(empty_risk_cols))

    amount_keywords = ("金额", "amount", "销售额")
    for i, col in enumerate(cols):
        col_l = str(col).lower()
        if any(k.lower() in col_l for k in amount_keywords):
            for row in data:
                if i < len(row):
                    v = row[i]
                    try:
                        num = float(v)
                        if num < 0:
                            signals.append("金额类列「%s」存在负值 %s，请确认是否为退款/冲销等合法场景" % (col, v))
                            break
                    except (TypeError, ValueError):
                        pass

    return (len(signals) == 0), signals


def review(
    sql: str,
    dataset: str = "B",
    requirement=None,
    exec_result=None,
    *,
    _dry_run_fn=None,
) -> dict:
    """门禁审查 SQL。

    参数
    ----
    sql : 待审查 SQL
    dataset : 数据集键，默认 B。用于 L4 语义门禁定位 Wren 客户端，也用于 L2 的 R1/R5/R6 三类
              需要 MDL 的规则——调用 rules.build_mdl_index(dataset) 现场建一次（如果
              ctx 没给 mdl_index）。
    requirement : 结构化技术需求对象（requirement.get 的返回值）。两个用途：
              ① L-REQ 需求要素层的唯一输入；② 原样透传给 L2 ctx。
              **传 None / 取不到时 L-REQ 判 skipped，绝不判红**。
    exec_result : 真实执行后的结果集，供 L5 断言。传 None 时 L5 标记 skipped
    _dry_run_fn : 仅自测注入用：替换 Wren dry_run 的假桩函数

    返回
    ----
    dict: {status, layers, syntax_issues, semantic_issues, rule_violations, revised_sql,
           review_notes, requirement_gaps}
    """
    layers = []
    syntax_issues = []
    semantic_issues = []
    rule_violations = []
    revised_sql = None
    notes = []

    # L-REQ 需求要素层：排在最前。
    #
    # 为什么不受 L1/L3/L4 级联跳过的约束：L-REQ 判的是「需求要素有没有在 SQL 里
    # 落实」，不依赖 SQL 能否解析——L1 挂了不影响它照样判（解析不了时它自己
    # 判 skipped，理由写进 detail，不重复报红）。
    # 反过来 L1~L5 也一样看不到需求，所以它排在级联链之前而非之后。
    #
    # ⚠️ 阶段二起**必须传 sql**：覆盖判定要比对「需求要素 vs SQL 实际输出」，
    #   没有 SQL 就无从比对 ⇒ _requirement_check 判 skipped。
    #   漏传的表现是 L-REQ 恒 skipped 且 detail 显示「未提供结构化需求」，
    #   与「调用方没接线」的症状一模一样 ⇒ 又一处静默失效。
    #
    # 只报告不改写（红线 D1）：本层只往 requirement_gaps 里写，绝不碰 revised_sql。
    _req = _requirement_check(requirement, sql)
    layers.append({
        "layer": "L-REQ",
        "name": "需求要素",
        "ok": _req["ok"],
        "skipped": _req["skipped"],
        "detail": _req["detail"],
    })
    requirement_gaps = list(_req["gaps"])

    l1_ok = True
    l1_skipped = False
    l1_detail = "语法解析通过（sqlglot postgres 方言）"
    ast_stmts = None
    try:
        ast_stmts = sqlglot.parse(sql, read="postgres")
        if not ast_stmts or any(s is None for s in ast_stmts):
            l1_ok = False
            l1_detail = "SQL 为空或未解析出有效语句（空串 / 纯空白 / 纯注释 / 纯分隔符）"
            syntax_issues.append(l1_detail)
    except sqlglot.errors.ParseError as e:
        l1_ok = False
        l1_detail = "语法错误：%s" % str(e)[:240]
        syntax_issues.append(l1_detail)
    except Exception as e:
        l1_ok = False
        l1_detail = "解析异常（%s）：%s" % (type(e).__name__, str(e)[:200])
        syntax_issues.append(l1_detail)

    layers.append({
        "layer": "L1",
        "name": "语法校验",
        "ok": l1_ok,
        "skipped": l1_skipped,
        "detail": l1_detail,
    })
    if not l1_ok:
        for ln, lv in [("L2", "静态风险"), ("L3", "只读校验"), ("L4", "语义校验"), ("L5", "结果断言")]:
            layers.append({
                "layer": ln,
                "name": lv,
                "ok": False,
                "skipped": True,
                "detail": "因 L1 语法错误跳过",
            })
        layers.append({
            "layer": "L-SQLFluff",
            "name": "SQLFluff 风格检查",
            "ok": False,
            "skipped": True,
            "detail": "因 L1 阻断 + 一期未启用",
        })
        layers.append({
            "layer": "L-GX",
            "name": "Great Expectations 断言",
            "ok": False,
            "skipped": True,
            "detail": "因 L1 阻断 + 一期未启用",
        })
        return {
            "status": "不通过",
            "layers": layers,
            "syntax_issues": syntax_issues,
            "semantic_issues": semantic_issues,
            "rule_violations": rule_violations,
            "revised_sql": revised_sql,
            "review_notes": "L1 语法校验未通过，已阻断：" + "；".join(syntax_issues),
            "requirement_gaps": requirement_gaps,
        }

    # L2 静态规则：一次性建 ctx，R1/R5/R6 若需要 mdl_index 可现场 build
    ctx_for_rules = {
        "dataset": dataset,
        "mdl_index": None,  # 懒加载：rules.evaluate_all 内 R5/R6 才会调 build_mdl_index
        "requirement": requirement,
    }
    l2_ok = True
    l2_skipped = False
    l2_detail_parts = []
    for stmt in ast_stmts:
        issues = _static_checks(stmt, ctx_for_rules)
        semantic_issues.extend(issues)
    # 分级阻断（2026-10-02）：按 REGISTRY 的 "blocking" 标记决定 L2 是否阻断。
    # 历史约束：M6-1 期 L2 全 warning、只提示不阻断；现把「引用未建模对象 / 无条件 JOIN /
    # 一对多放大 / 枚举值错」四类硬错误升级为 blocking，其余规则行为不变。
    blocking_rule_ids = set()
    try:
        for _r in (getattr(rules_mod, "REGISTRY", None) or []):
            if _r.get("blocking"):
                blocking_rule_ids.add(_r.get("id"))
    except Exception:
        blocking_rule_ids = set()
    blocking_issues = [i for i in semantic_issues if i.get("rule") in blocking_rule_ids]
    warning_issues = [i for i in semantic_issues if i.get("rule") not in blocking_rule_ids]
    if blocking_issues:
        l2_ok = False
    if semantic_issues:
        l2_detail_parts.append("检出 %d 项静态风险（阻断 %d / 警告 %d）"
                               % (len(semantic_issues), len(blocking_issues), len(warning_issues)))
        for it in semantic_issues[:6]:
            l2_detail_parts.append("  - %s：%s" % (it["rule"], it["snippet"][:80]))
        if len(semantic_issues) > 6:
            l2_detail_parts.append("  - ... 其余 %d 项略" % (len(semantic_issues) - 6))
    else:
        l2_detail_parts.append("未检出静态风险")
    layers.append({
        "layer": "L2",
        "name": "静态风险",
        "ok": l2_ok,
        "skipped": l2_skipped,
        "detail": "\n".join(l2_detail_parts),
    })
    if blocking_issues:
        notes.append("L2 阻断 %d 项（硬错误）：%s"
                     % (len(blocking_issues),
                        "、".join(sorted({i["rule"] for i in blocking_issues}))))
    if warning_issues:
        notes.append("L2 警告 %d 项（不阻断）：%s"
                     % (len(warning_issues),
                        "、".join(sorted({i["rule"] for i in warning_issues}))))

    l3_ok = True
    l3_skipped = False
    l3_detail_parts = []
    if len(ast_stmts) > 1:
        l3_ok = False
        msg = "检测到多语句提交（%d 条），只读门禁仅允许单语句" % len(ast_stmts)
        l3_detail_parts.append(msg)
        rule_violations.append({"layer": "L3", "type": "MULTI_STATEMENT", "detail": msg})
    for i, stmt in enumerate(ast_stmts):
        if not isinstance(stmt, _READONLY_ROOT):
            l3_ok = False
            tname = type(stmt).__name__
            msg = "语句[%d] 根节点类型 %s 不在只读白名单（Select/With）内，已阻断" % (i, tname)
            l3_detail_parts.append(msg)
            rule_violations.append({"layer": "L3", "type": "WRITE_OPERATION_%s" % tname.upper(), "detail": msg})
    if l3_ok:
        l3_detail_parts.append("只读校验通过（%d 条语句，全部为 Select/With）" % len(ast_stmts))
    layers.append({
        "layer": "L3",
        "name": "只读校验",
        "ok": l3_ok,
        "skipped": l3_skipped,
        "detail": "\n".join(l3_detail_parts),
    })
    if not l3_ok:
        layers.append({
            "layer": "L4",
            "name": "语义校验",
            "ok": False,
            "skipped": True,
            "detail": "因 L3 只读校验未通过跳过",
        })
        l5_skip_ok = exec_result is None
        layers.append({
            "layer": "L5",
            "name": "结果断言",
            "ok": l5_skip_ok,
            "skipped": True,
            "detail": "因 L3 只读校验未通过跳过" if not l5_skip_ok else "无 exec_result + L3 阻断跳过",
        })
        layers.append({
            "layer": "L-SQLFluff",
            "name": "SQLFluff 风格检查",
            "ok": False,
            "skipped": True,
            "detail": _lint_sqlfluff(sql)["reason"] if not _SQLFLUFF_ON else "一期未启用",
        })
        layers.append({
            "layer": "L-GX",
            "name": "Great Expectations 断言",
            "ok": False,
            "skipped": True,
            "detail": _gx_assert(exec_result)["reason"] if not _GX_ON else "一期未启用",
        })
        l3_msgs = [v["detail"] for v in rule_violations if v["layer"] == "L3"]
        return {
            "status": "不通过",
            "layers": layers,
            "syntax_issues": syntax_issues,
            "semantic_issues": semantic_issues,
            "rule_violations": rule_violations,
            "revised_sql": revised_sql,
            "review_notes": "L3 只读校验未通过，已阻断：" + "；".join(l3_msgs),
            "requirement_gaps": requirement_gaps,
        }

    l4_ok = True
    l4_skipped = False
    l4_detail = ""
    l4_msg = ""
    if _dry_run_fn is not None:
        dry = _dry_run_fn(sql)
    else:
        try:
            import registry as registry_mod
            import wren as wren_mod
            ds = registry_mod.get(dataset)
            client = wren_mod.WrenClient(ds.wren_url, name=ds.key)
            dry = client.dry_run(sql)
        except Exception as e:
            dry = {"ok": False, "message": "L4 语义门禁初始化失败（%s）：%s" % (type(e).__name__, str(e)[:200])}
    dry_ok = bool(dry.get("ok"))
    if not dry_ok:
        l4_ok = False
        l4_msg = dry.get("message") or "语义层校验未通过"
        l4_detail = "语义层 dry_run 未通过：%s" % l4_msg
        rule_violations.append({"layer": "L4", "type": "SEMANTIC_DRY_RUN", "detail": l4_msg})
    else:
        l4_detail = dry.get("message") or "语义层预演通过"
    layers.append({
        "layer": "L4",
        "name": "语义校验",
        "ok": l4_ok,
        "skipped": l4_skipped,
        "detail": l4_detail,
    })
    if not l4_ok:
        layers.append({
            "layer": "L5",
            "name": "结果断言",
            "ok": False if exec_result is not None else True,
            "skipped": True,
            "detail": "因 L4 语义校验未通过跳过" if exec_result is not None else "无 exec_result + L4 阻断跳过",
        })
        layers.append({
            "layer": "L-SQLFluff",
            "name": "SQLFluff 风格检查",
            "ok": False,
            "skipped": True,
            "detail": _lint_sqlfluff(sql)["reason"] if not _SQLFLUFF_ON else "一期未启用",
        })
        layers.append({
            "layer": "L-GX",
            "name": "Great Expectations 断言",
            "ok": False,
            "skipped": True,
            "detail": _gx_assert(exec_result)["reason"] if not _GX_ON else "一期未启用",
        })
        return {
            "status": "不通过",
            "layers": layers,
            "syntax_issues": syntax_issues,
            "semantic_issues": semantic_issues,
            "rule_violations": rule_violations,
            "revised_sql": revised_sql,
            "review_notes": "L4 语义校验未通过，已阻断：%s" % l4_msg,
            "requirement_gaps": requirement_gaps,
        }

    l5_ok = True
    l5_skipped = False
    l5_detail_parts = []
    if exec_result is None:
        l5_skipped = True
        l5_detail_parts.append("未提供 exec_result，结果断言跳过（不影响总状态）")
    else:
        res_ok, signals = _result_assertions(exec_result)
        if not res_ok:
            l5_ok = False
            for s in signals:
                l5_detail_parts.append("  - " + s)
                rule_violations.append({"layer": "L5", "type": "RESULT_ASSERTION", "detail": s})
        else:
            l5_detail_parts.append("结果断言通过")
    layers.append({
        "layer": "L5",
        "name": "结果断言",
        "ok": l5_ok,
        "skipped": l5_skipped,
        "detail": "\n".join(l5_detail_parts) if l5_detail_parts else "结果断言",
    })

    sf_res = _lint_sqlfluff(sql) if _SQLFLUFF_ON else {"skipped": True, "reason": "一期未启用"}
    layers.append({
        "layer": "L-SQLFluff",
        "name": "SQLFluff 风格检查",
        "ok": bool(sf_res.get("skipped")) or bool(sf_res.get("ok", False)),
        "skipped": bool(sf_res.get("skipped", False)),
        "detail": sf_res.get("reason", sf_res.get("detail", "")),
    })
    gx_res = _gx_assert(exec_result) if _GX_ON else {"skipped": True, "reason": "一期未启用"}
    layers.append({
        "layer": "L-GX",
        "name": "Great Expectations 断言",
        "ok": bool(gx_res.get("skipped")) or bool(gx_res.get("ok", False)),
        "skipped": bool(gx_res.get("skipped", False)),
        "detail": gx_res.get("reason", gx_res.get("detail", "")),
    })

    # 分级阻断（2026-10-02）：L2 纳入阻断层 —— 其 ok 已按规则的 blocking 标记算好
    # （只有 blocking 命中才为 False），故与 L1/L3/L4/L5 同列。
    #
    # REQ-COVERAGE 阶段一（2026-10-19）：L-REQ 纳入阻断层。
    #
    # 🔴 这里加不加 "L-REQ"，决定 L-REQ 是「真门禁」还是「只打分的旁观者」——
    # 漏加不会报错、不会告警，层照样出现在 layers 里、detail 照样写得漂亮，
    # 只是 ok=False 永远不改变 status。**改这一行等于决定门禁是否生效**。
    #
    # ⚠️ 阶段二（2026-10-20）：「不阻断」靠的是**策略层不置 ok=False**
    #   （默认 report-only，见 _req_coverage_mode），**不是**靠移出本元组。
    #   两个开关各管一件事，别改错：
    #     · 本元组管「ok=False 会不会传导到 status」（阶段一起就接好了）；
    #     · REQ_COVERAGE_MODE 管「什么情况下会置 ok=False」（阶段二新增）。
    #   ⇒ 切成 REQ_COVERAGE_MODE=blocking 时，本元组即刻生效，无需再改这里。
    # has_blocking_fail 同时要求 ok=False 且 skipped=False，所以「无需求输入
    # → skipped」这一路不会因为进了本元组就变成阻断（见 _requirement_check）。
    blocking_layers = [
        l for l in layers
        if l["layer"] in ("L-REQ", "L1", "L2", "L3", "L4", "L5")
    ]
    has_blocking_fail = any(
        (not l["ok"]) and (not l["skipped"]) for l in blocking_layers
    )
    if has_blocking_fail:
        final_status = "不通过"
    elif semantic_issues:
        final_status = "警告"
    else:
        final_status = "通过"

    # L-REQ 覆盖缺口必须留痕到 notes——否则「层显示通过、gaps 非空」又是一处静默失效。
    #
    # ⚠️ report-only 模式下这句 notes 是**唯一**能让业务方在 review_notes 里
    #   看到缺口的通道（status 仍是「通过」）。删掉它 ⇒ 覆盖缺口彻底静默。
    #   ⚠️ 必须写清当前模式，否则「status=通过 + 提示有阻断级缺口」看起来自相矛盾。
    if _req["gaps"]:
        _req_mode_desc = ("已阻断" if _req_coverage_mode() == _REQ_MODE_BLOCKING
                          else "仅提示不阻断（report-only）")
        notes.append("L-REQ 覆盖判定：%d 项需求要素未被 SQL 覆盖（%s）：%s"
                     % (len(_req["gaps"]), _req_mode_desc, "；".join(_req["gaps"][:3])))

    if final_status == "警告" and not notes:
        rules_set = sorted({i["rule"] for i in semantic_issues})
        notes.append("L2 警告 %d 项（不阻断）：%s" % (len(semantic_issues), "、".join(rules_set)))
    review_notes = "；".join(notes) if notes else ("门禁审查完成：状态「%s」" % final_status)

    return {
        "status": final_status,
        "layers": layers,
        "syntax_issues": syntax_issues,
        "semantic_issues": semantic_issues,
        "rule_violations": rule_violations,
        "revised_sql": revised_sql,
        "review_notes": review_notes,
        "requirement_gaps": requirement_gaps,
    }
