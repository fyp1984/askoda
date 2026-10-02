# -*- coding: utf-8 -*-
"""失败回退矩阵（M6-2 · SQL生成链路设计 §6，5 类失败 + 既定策略）

本模块定位：纯函数模块——
  · 只依赖 Python 标准库（os / difflib / json / hashlib / re / functools）+ sqlglot（已装）+ 只读引用 rules.REGISTRY；
  · 无 IO、无 db、无网络、不 import gates/sqlgen/sqlrun（避免循环依赖）。

5 类失败分类（KINDS 顺序 = 链路顺序）：
  F1 语义定型   —— 主体/颗粒度不清 / 意图槽位空，进确认问题环节
  F2 Schema 筛选 —— 找不到明确主题表（table_candidates 空 or 多候选未达唯一解阈值），输出候选+人工
  F3 SQL 生成    —— 候选多版本差异过大，hold 草稿不下发 SQL
  F4 SQL 审查    —— 规则未通过（L1/L3/L4/L5 阻断 or warning 叠加规则不通过）退回人工+治理指引
  F5 结果校验    —— 行数/样本/信号异常，标记不可交付需复核

全部函数绝不抛异常：任何输入缺失、类型不对、解析失败、rules.REGISTRY 读不到
都走"不报=可交付/不判"兜底。
"""
import os
import difflib
import json as _json

try:
    import sqlglot
except Exception:  # pragma: no cover - 环境缺时不崩
    sqlglot = None

# ==========================================================================
# 一、类别 + 策略表（STRATEGY）与 G_FIX：纯静态、可查
# ==========================================================================
KINDS = ("F1", "F2", "F3", "F4", "F5")

STRATEGY = {
    "F1": {
        "stage": "语义定型",
        "failure": "主体/颗粒度不清",
        "strategy": "进入确认问题环节",
    },
    "F2": {
        "stage": "Schema 筛选",
        "failure": "找不到明确主题表",
        "strategy": "输出候选集合，进入人工审核",
    },
    "F3": {
        "stage": "SQL 生成",
        "failure": "多版本候选差异大",
        "strategy": "只保留计划草稿，不下发 SQL（hold）",
    },
    "F4": {
        "stage": "SQL 审查",
        "failure": "规则不通过",
        "strategy": "退回人工分析 + 治理指引（不自动改写）",
    },
    "F5": {
        "stage": "结果校验",
        "failure": "行数/样本异常",
        "strategy": "标记不可交付并要求复核",
    },
}

# F4 治理指引的「怎么改」映射：必须覆盖 rules.REGISTRY 全部 10 条 id
# （未知 id 给兜底文案，绝不抛异常）
G_FIX = {
    "JOIN_WITHOUT_CONDITION": "为每一张 JOIN 的表补上明确的 ON / USING 关联条件，避免笛卡尔积",
    "SELECT_STAR": "把 SELECT * 展开为显式列清单，只保留下游确实需要的字段",
    "GROUP_BY_INCONSISTENT": "把 SELECT 中的非聚合列全部补进 GROUP BY，或对未分组列加上 MIN/MAX/ANY_VALUE 等聚合",
    "UNNECESSARY_DISTINCT": "如果 DISTINCT 只是为了掩盖 JOIN 放大，请先在明细侧预聚合或修正 JOIN 粒度，再考虑去掉 DISTINCT",
    "ONE_TO_MANY_UNHANDLED": "先在明细侧预聚合或加 DISTINCT，确认 JOIN 两表粒度一致后再做聚合；若为 MANY_TO_ONE 事实⋈维表可直接确认粒度",
    "LOAD_DATE_SUBSTITUTION": "改用业务日期列（如 stat_date / order_date / register_date）过滤与分组，不要用 etl/load/dw_insert_time 类装载日期",
    "MULTI_VALUE_NO_ORDER": "对 LISTAGG / GROUP_CONCAT / ARRAY_AGG 类多值拼接函数显式指定 WITHIN GROUP / ORDER BY，保证结果稳定可比对",
    "DANGLING_DENOMINATOR": "比率/占比类指标必须同时输出分子与分母，或在 SELECT 里同时出现；避免只看结果值无法验算合理性",
    "UNMAPPED_OBJECT_REF": "在 FROM/JOIN/子查询中引用的表、列名，确认该库 MDL 是否确实建模；未上架对象需先进入 Schema 流程补建",
    "TIME_FIELD_SUSPECT": "改用该库的标准时间候选列（如 stat_date / stat_month / order_date / register_date / change_month）过滤分组，避免使用装载时间或非时间语义列",
}


# ==========================================================================
# 二、F3：SQL 多候选差异判定 —— f3_candidate_diff
# ==========================================================================
def _hold_threshold():
    """环境变量 F3_DIFF_THRESHOLD 覆盖；默认 0.35；越界值夹到 [0.001, 0.999]。"""
    raw = os.environ.get("F3_DIFF_THRESHOLD", "")
    try:
        v = float(raw) if raw else 0.35
    except (TypeError, ValueError):
        v = 0.35
    if v <= 0:
        v = 0.001
    if v >= 1:
        v = 0.999
    return v


def _norm_sql_fingerprint(sql_text):
    """用 sqlglot 归一化作为指纹：大小写/空白/括号风格差异视为同一 SQL。
    解析失败或 sqlglot 缺失时退为「去空白 + 去大小写」。绝不抛异常。
    """
    if not isinstance(sql_text, str):
        return ""
    s = sql_text.strip()
    if not s:
        return ""
    if sqlglot is None:
        return "".join(s.split()).lower()
    try:
        tree = sqlglot.parse_one(s, read="postgres")
    except Exception:
        return "".join(s.split()).lower()
    try:
        return tree.sql(dialect="postgres", pretty=False).strip()
    except Exception:
        return "".join(s.split()).lower()


def f3_candidate_diff(candidates):
    """多候选 SQL 两两指纹比对；返回 n / max_diff / pairs / hold / threshold。

    规格：
      · candidates: list[str]（>=1；不是 list 时先转为 [str(candidates)] 兜底）
      · diff = 1 - SequenceMatcher(None, a_norm, b_norm).ratio()  ∈ [0, 1]
      · 指纹完全相等（同字符串）→ diff=0
      · 阈值 HOLD_THRESHOLD 默认 0.35（F3_DIFF_THRESHOLD 可覆盖）
      · n <= 1 → hold=False（只有一份候选择无可比对）
    """
    # 入参鲁棒化：非 list → 包一层；元素不是 str → 转 str；None / 空串 → 视为无
    if not isinstance(candidates, (list, tuple)):
        candidates = [candidates]
    cleaned = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str) and not c.strip():
            continue
        cleaned.append(str(c))
    n = len(cleaned)
    if n <= 1:
        return {
            "n": n,
            "max_diff": 0.0,
            "pairs": [],
            "hold": False,
            "threshold": _hold_threshold(),
        }
    fps = [_norm_sql_fingerprint(x) for x in cleaned]
    threshold = _hold_threshold()
    pairs = []
    max_diff = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            a, b = fps[i], fps[j]
            if a == b:
                d = 0.0
            else:
                try:
                    ratio = difflib.SequenceMatcher(None, a, b).ratio()
                except Exception:
                    ratio = 0.0
                d = 1.0 - ratio
            if d < 0:
                d = 0.0
            if d > 1:
                d = 1.0
            if d > max_diff:
                max_diff = d
            pairs.append([int(i), int(j), round(float(d), 4)])
    # hold：任一对 d >= threshold（不是 max_diff >= 阈值；避免 diff=0.36 的对 + diff=0.05 对同时存在时 max 0.36 被平均成小值）
    hold = any(p[2] >= threshold for p in pairs)
    # 若只有一对并且 d < 0.001，视为改写等价不 hold（与指纹全等语义一致）
    if n == 2 and max_diff < 0.001:
        hold = False
    return {
        "n": n,
        "max_diff": round(float(max_diff), 4),
        "pairs": pairs,
        "hold": bool(hold),
        "threshold": threshold,
    }


# ==========================================================================
# 三、F5：结果校验 —— f5_deliverable
# ==========================================================================
def f5_deliverable(row_count, column_count, signals):
    """行数/列数/信号 -> deliverable + 原因。

    命中任一即 False：
      · row_count 不是 int 或为负 → "行数异常"
      · row_count == 0              → "结果集为 0 行，无法交付"
      · signals 非空                → 拼接 signals 原文（L5 suspicious_signals）
    否则 True："行数/样本正常（N 行 × M 列）"
    """
    # 鲁棒化：signals 不是 list/tuple 时，如为 str 包一层，否则当空
    if isinstance(signals, str):
        signals_norm = [signals] if signals.strip() else []
    elif isinstance(signals, (list, tuple)):
        signals_norm = [str(x) for x in signals if (x is not None and str(x).strip() != "")]
    else:
        signals_norm = []

    # 列数：不是 int 则视为 0（至少不让类型判定导致误报）
    try:
        cc = int(column_count) if column_count is not None else 0
    except (TypeError, ValueError):
        cc = 0
    if cc < 0:
        cc = 0

    # 行数：必须是 int，且 >=0
    rc_ok = True
    if not isinstance(row_count, int) or isinstance(row_count, bool):
        rc_ok = False
    elif row_count < 0:
        rc_ok = False
    if not rc_ok:
        return {"deliverable": False, "reason": "行数异常"}
    rc = row_count

    if rc == 0:
        return {"deliverable": False, "reason": "结果集为 0 行，无法交付"}
    if signals_norm:
        # 拼接：超过 5 条时前 5 + "等 N 条"
        head = signals_norm[:5]
        joined = "；".join(head)
        if len(signals_norm) > 5:
            joined += "（等 %d 条）" % len(signals_norm)
        return {"deliverable": False, "reason": joined}
    return {
        "deliverable": True,
        "reason": "行数/样本正常（%d 行 × %d 列）" % (rc, cc),
    }


# ==========================================================================
# 四、F4：治理指引 —— f4_guidance（只读引用 rules.REGISTRY）
# ==========================================================================
def _statement_for(rule_id):
    """从 rules.REGISTRY 按 id 取 statement；失败返回空串。
    只读引用，绝不修改 rules.py 任何对象。
    """
    try:
        import rules as rules_mod  # local import，避免模块加载顺序干扰
    except Exception:
        return ""
    try:
        for r in (getattr(rules_mod, "REGISTRY", None) or []):
            if (r.get("id") or "") == rule_id:
                return str(r.get("statement") or "")
    except Exception:
        return ""
    return ""


def f4_guidance(rule_ids):
    """命中的规则 id 列表 -> 每条 {rule, why, how_to_fix}。"""
    out = []
    if not rule_ids:
        return out
    # 鲁棒化：单 str 包一层
    if isinstance(rule_ids, str):
        rule_ids = [rule_ids]
    try:
        ids = [str(x) for x in rule_ids if x is not None and str(x).strip() != ""]
    except Exception:
        ids = []
    for rid in ids:
        why = _statement_for(rid)
        how = G_FIX.get(rid)
        if how is None:
            how = "请对照 MDL 与业务口径确认"
        out.append({
            "rule": rid,
            "why": why,
            "how_to_fix": how,
        })
    return out


# ==========================================================================
# 五、汇总 classify（能判就判，缺输入就不判，绝不因缺失抛异常）
# ==========================================================================
def classify(**ctx):
    """汇总 F1–F5 命中种类与对应策略；返回
    {kinds:[], actions:[], hold:bool, deliverable:bool, detail:{}}

    一期只做"输入足够就判，缺就不判"，不做任何缺省推断。
    接收关键字段：
      f1_hit / f1_reason             （bool + str，一般由 analysis 给出）
      f2_hit / f2_reason             （bool + str，由 sqlgen.plan draft_gate 给出）
      f3_diff                        （dict，f3_candidate_diff 返回；或 candidate_sqls=list）
      f4_rule_ids                    （list[str]，review 命中规则 id）
      f5 / f5_rowcount, f5_colcount, f5_signals  （直接传 row_count/col_count/signals）
    """
    kinds = []
    actions = []
    detail = {}

    # F1
    try:
        if bool(ctx.get("f1_hit")):
            r = ctx.get("f1_reason") or ""
            kinds.append("F1")
            actions.append(dict(STRATEGY["F1"], reason=str(r)[:200]))
            detail["F1"] = str(r)[:200]
    except Exception:
        pass

    # F2
    try:
        if bool(ctx.get("f2_hit")):
            r = ctx.get("f2_reason") or ""
            kinds.append("F2")
            actions.append(dict(STRATEGY["F2"], reason=str(r)[:200]))
            detail["F2"] = str(r)[:200]
    except Exception:
        pass

    # F3：两种传法 —— 直接传 f3_diff dict；或传 candidate_sqls=list 我自己跑 f3_candidate_diff
    try:
        f3 = ctx.get("f3_diff")
        if not isinstance(f3, dict) and (ctx.get("candidate_sqls") is not None):
            f3 = f3_candidate_diff(ctx.get("candidate_sqls"))
        if isinstance(f3, dict) and bool(f3.get("hold")):
            kinds.append("F3")
            actions.append(dict(STRATEGY["F3"], max_diff=f3.get("max_diff"),
                                threshold=f3.get("threshold"),
                                pairs_count=len(f3.get("pairs") or [])))
            detail["F3"] = {
                "max_diff": f3.get("max_diff"),
                "threshold": f3.get("threshold"),
                "n": f3.get("n"),
            }
    except Exception:
        pass

    # F4
    try:
        rids = ctx.get("f4_rule_ids")
        if rids:
            if isinstance(rids, str):
                rids = [rids]
            ids = [str(x) for x in rids if (x is not None and str(x).strip() != "")]
            if ids:
                kinds.append("F4")
                actions.append(dict(STRATEGY["F4"], rules=list(ids)[:20]))
                detail["F4"] = list(ids)[:20]
    except Exception:
        pass

    # F5：两种传法 —— 直接传 f5 dict；或传 (f5_rowcount, f5_colcount, f5_signals) 三值
    try:
        f5_out = None
        if isinstance(ctx.get("f5"), dict) and "deliverable" in ctx["f5"]:
            f5_out = dict(ctx["f5"])
        else:
            if ctx.get("f5_rowcount") is not None:
                f5_out = f5_deliverable(
                    ctx.get("f5_rowcount"),
                    ctx.get("f5_colcount", 0),
                    ctx.get("f5_signals", []),
                )
        if f5_out is not None and (not f5_out.get("deliverable")):
            kinds.append("F5")
            actions.append(dict(STRATEGY["F5"], reason=str(f5_out.get("reason") or "")[:200]))
            detail["F5"] = str(f5_out.get("reason") or "")[:200]
    except Exception:
        pass

    # 布尔汇总：hold = F2 or F3；deliverable = 没命中 F5
    hold = any(k in ("F2", "F3") for k in kinds)
    deliverable = "F5" not in kinds
    # 同时命中 F4（审查不通过）也不可交付
    if "F4" in kinds:
        deliverable = False

    return {
        "kinds": kinds,
        "actions": actions,
        "hold": bool(hold),
        "deliverable": bool(deliverable),
        "detail": detail,
    }
