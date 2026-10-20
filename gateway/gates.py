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
  阶段一只做接线与齐备性校验；要素提取与覆盖判定在阶段二。
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


# L-REQ · 阶段一（REQ-COVERAGE）判据：结构化需求里必须「有结论」的槽位。
#
# 🔴 本段是**临时口径，阶段二会整体替换**，且**阶段一上线期间会真实跑在生产链路上**
#   —— 它不是死代码，是会判结果的活代码（只是不阻断，见 _requirement_check）。
#
# 为什么是这四个单值槽位 + output_fields 非空（都是齐备性，非覆盖判定）
# -------------------------------------------------------------------
# 结构化需求对象共 6 个槽位字段（requirement.REQUIRED_TOP）：
#   subject / granularity / time_semantics / data_scope / output_fields / residual_risks
# · residual_risks 是「残余风险集合」——**空是正常的**（无风险），拿它判缺口会
#   把「一切都好」误报成问题。
# · output_fields 只判**空数组**：空 = 下游无列可选、SQL 根本写不出来，是齐备性问题
#   （requirement.py:259-262 已有先例与现成文案）。
#   ⚠️ **只判空数组，不判「字段是否在 SQL 里出现」**——后者是覆盖判定，归阶段二。
# · 四枚单值槽位判「有无结论」：注意 build() 对无候选槽位写的占位项 value 非空
#   （是一句中文说明）而 confidence=0.0，**两者都要看**。
#
# 第二阶段替换什么
# --------------
# 整段替换为「需求要素 vs SQL 实际输出」的**覆盖判定**，届时引入 8 字段契约
# （element / slot / provenance / confidence / needs_confirmation / mandatory /
#   polarity / matcher）。⚠️ 其中 `polarity` 是关键：结构化对象里「原文没提」与
# 「原文显式豁免」都是 confidence=0，**结构上无法区分**，只有 may_cover/must_cover
#   这个字段能分开——不引入它就会把「业务方主动豁免」误拦成「需求未覆盖」。
_REQ_REQUIRED_SLOTS = ("subject", "granularity", "time_semantics", "data_scope")

# L-REQ 每条 detail 的固定前缀：自述本层**只查了什么、没查什么**。
#
# 为什么必须出现在 detail（用户可见）而不是只写在 docstring（代码注释）
# ---------------------------------------------------------------------
# REQ-COVERAGE 要修的病就是**静默失效**：requirement 恒 None、各层全绿、
# deliverable=true、业务方拿错数据、全程零报错。若 L-REQ 静默通过却不自述
# 「我只查了齐备性、没查覆盖」，等于把同一个洞挖在新位置——业务方看到 L-REQ 绿了
# 会以为「需求要素已校验通过」。
#
# 「缺席必须显式报未执行」这条原则，同样适用于**「在场但只做了一半」**。
#
# 前半句防「以为查了覆盖判定」，后半句防「以为 output_fields 有值就是指标已齐」。
_REQ_SCOPE_PREFIX = (
    "本阶段仅校验槽位齐备性（槽位有无结论、output_fields 是否为空）；"
    "未做需求要素覆盖判定（未核对 SQL 是否体现需求要求的指标与维度）；覆盖校验见阶段二。\n"
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


def _requirement_check(requirement):
    """L-REQ · 需求要素层（REQ-COVERAGE 阶段一：只接线 + 齐备性，**不判红**）。

    返回 {"ok", "skipped", "detail", "gaps"}，形状与 L1-L5 对齐。

    为什么单独成层、且排在 L1 之前
    ------------------------------
    L1-L5 审的是「SQL 本身对不对」，答不了「需求要的东西在不在」——
    这正是 REQ-COVERAGE 缺陷能各层全绿的原因：SELECT 语法没错、只读、语义层认得、
    可 GROUP BY 与输出列一致，唯独「月度」「订单数」「已退款金额」三项
    从未进入过任何一层的视野。L-REQ 补的是这个洞，故排在最前。

    🔴 阶段一为什么**绝不判 ok=False**（临时口径，误拦风险已知）
    -------------------------------------------------------
    阶段一只判**齐备性**（槽位有没有结论），不判**覆盖判定**（有结论但 SQL 没体现）。
    齐备性缺口离「判定需求是否被满足」还差一整个阶段，**不构成阻断理由**。

    误拦是**已实测复现**的，不是理论风险：`requirement.py:237-241` 对无候选槽位写的是
    `{"value": "无候选结论（该槽位在本轮分析中为空）", "confidence": 0.0}`，
    而「各门店总销售额」这类**原文根本没提时间**的需求，其 `time_semantics`
    正是这个形态⇒ 若据此判红，业务方任何没提时间的正常提问都会被拦。

    根因值得记住：**「槽位缺失」与「业务方没要求」在结构化需求对象里是同一个信号
    （都是 `confidence=0`），结构上无法区分。** 这正是阶段二契约必须引入 `polarity`
    的原因——`may_omit`（显式豁免）与「原文未提」（沉默缺失）只有加上这个字段才分得开。

    阶段一的四条硬约束
    ------------------
    1. **无需求输入一律 skipped，绝不判红**。调用方不传 requirement 时
       （现有 sqlgen / sqlrun 三处调用都不传），若判红就是给全链路造出
       一个全新失败模式——这比原缺陷更难查。
    2. **只报告，不改写**。本层只产出 gaps，绝不触碰改写字段
       （红线 D1：门禁失败时自动改写 SQL 是被机械判据 check_r3_d1 盯着的）。
    3. **detail 必须自述范围**。本函数每一条返回路径的 detail 都带
       _REQ_SCOPE_PREFIX——业务方在界面上看不到代码注释，若静默通过却不自述
       「只查了齐备性、没查覆盖」，等于把 REQ-COVERAGE 要修的那个洞挖在新位置。
    4. **⚠️ 本函数在阶段二会被整体替换**（含 gaps 判定与本段判据）。

    槽位口径（勿绕开）
    ------------------
    传入对象是 requirement.build/get 产出的**结构化技术需求对象**，其字段名
    是设计文档口径：subject / granularity / time_semantics / data_scope /
    output_fields / residual_risks（见 requirement.SLOT_MAP）。
    **不是** M3 slots 的 time / scope / fields / risks——直取后者会 KeyError。
    """
    # 需求不可用（未传 / 取不到 / 模块未就绪）→ skipped，不判红。
    # 三种情形都归到这里，因为它们回答的都是「没有可比对的需求」，
    # 而不是「需求不合格」。
    if requirement is None:
        return {
            "ok": True, "skipped": True, "gaps": [],
            "detail": _REQ_SCOPE_PREFIX
                      + "跳过原因：未提供结构化需求（调用方未传 demand_id），本层不参与总状态。",
        }
    if not isinstance(requirement, dict):
        return {
            "ok": True, "skipped": True, "gaps": [],
            "detail": _REQ_SCOPE_PREFIX
                      + "跳过原因：结构化需求类型异常（%s），本层不参与总状态。"
                      % type(requirement).__name__,
        }
    if requirement.get("ok") is False:
        # requirement.get 查不到时返回的就是 {"ok": False, "error": ...}。
        # 这是「取不到」，不是「不合格」——skipped 并把原因写进 detail。
        return {
            "ok": True, "skipped": True, "gaps": [],
            "detail": _REQ_SCOPE_PREFIX
                      + "跳过原因：结构化需求不可用（%s），本层不参与总状态。"
                      % str(requirement.get("error") or requirement.get("reason") or "")[:160],
        }

    gaps = []
    for slot in _REQ_REQUIRED_SLOTS:
        claim = requirement.get(slot)
        if not isinstance(claim, dict):
            gaps.append("槽位「%s」缺失或类型异常（实际 %s）"
                        % (slot, type(claim).__name__))
            continue
        value = claim.get("value")
        confidence = claim.get("confidence")
        # build() 对取不到候选的槽位写的是占位项：value 明写「无候选结论」且
        # confidence=0.0（见 requirement.build 的 absent 分支）。所以
        # 「值空」与「confidence 为 0」两个信号等价于「这一槽位没有结论」。
        if value is None or str(value).strip() == "":
            gaps.append("槽位「%s」无结论（value 为空）" % slot)
        elif isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and confidence <= 0:
            gaps.append("槽位「%s」无结论（confidence=0，本轮未产出可信候选）" % slot)

    # output_fields 空数组 = 齐备性问题（下游无列可选，SQL 根本写不出来），
    # 不是覆盖判定。requirement.py:259-262 已有先例与现成文案。
    # ⚠️ 只判「空数组」，**不判「字段是否在 SQL 里出现」**——后者是覆盖判定，归阶段二。
    of = requirement.get("output_fields")
    if not isinstance(of, list):
        gaps.append("output_fields 缺失或类型异常（实际 %s），下游无可选输出列"
                    % type(of).__name__)
    elif not of:
        gaps.append("output_fields 为空数组，下游无可选输出列（SQL 无列可 SELECT）")

    if gaps:
        # 注意 ok=True：**阶段一有缺口也不阻断**，只报warning。
        # 阻断能力完整留给阶段二（那时才引入 polarity 区分 must_cover / may_omit）。
        return {
            "ok": True, "skipped": False, "gaps": gaps,
            "detail": _REQ_SCOPE_PREFIX
                      + "检出 %d 项齐备性缺口（仅提示，不阻断）：\n" % len(gaps)
                      + "\n".join("  - " + g for g in gaps),
        }
    return {
        "ok": True, "skipped": False, "gaps": [],
        "detail": _REQ_SCOPE_PREFIX
                  + "齐备性通过：%d 项必备槽位均有结论、output_fields 非空。"
                  % len(_REQ_REQUIRED_SLOTS),
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
    # 为什么不受 L1/L3/L4 级联跳过的约束：L-REQ 判的是「结构化需求本身齐不齐备」，
    # 既不依赖 SQL 文本，也不依赖 SQL 能否解析——L1 挂了不影响它照样判。
    # 反过来 L1~L5 也一样看不到需求，所以它排在级联链之前而非之后。
    #
    # 只报告不改写（红线 D1）：本层只往 requirement_gaps 里写，绝不碰 revised_sql。
    _req = _requirement_check(requirement)
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
    # ⚠️ 阶段一 `_requirement_check` 从不置 ok=False（有缺口也只提示），所以本元组里
    # 的 "L-REQ" 在阶段一**不会真的触发阻断**——这是刻意的：阶段一只判齐备性。
    # **「不阻断」靠的是不置 ok=False，不是靠移出本元组**，两者不冲突，别改错。
    # 阶段二引入 polarity 覆盖判定后，本元组即刻生效，无需再改这里。
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

    # L-REQ 齐备性缺口：阶段一**只提示不阻断**（_requirement_check 已保证 ok=True），
    # 但缺口必须留痕到 notes——否则「层显示通过、gaps 非空」又是一处静默失效。
    if _req["gaps"]:
        notes.append("L-REQ 提示 %d 项齐备性缺口（阶段一不阻断，覆盖校验见阶段二）：%s"
                     % (len(_req["gaps"]), "；".join(_req["gaps"][:3])))

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
