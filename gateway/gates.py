# -*- coding: utf-8 -*-
"""M5-1 · 五层 SQL 门禁（生成与审查分离）

设计说明
--------
按《实施推进与验收方案》§6 M5 与《调研-SQL测试与自动优化》§3.1 落地。
核心约束：
- 一期只新增 sqlglot 一个第三方依赖（SQLFluff / Great Expectations 留成可插拔插槽，默认关闭）
- 「生成」与「审查」严格分离：本模块只做审查，不生成 SQL
- 阻断 vs 警告分层：L1/L2(blocking 规则)/L3/L4 不通过则阻断，L2 其余规则只记警告不阻断，L5 无 exec_result 时 skipped
- M6-1 改造：L2 10 类静态规则**整体搬迁**到 gateway/rules.py 的 REGISTRY；
  gates.py 只保留 is_write / L2~L5 接线层。新增一条规则 = 只改 rules.py，
  不需要再进 gates.py 改 _static_checks。

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


_READONLY_ROOT = (exp.Select, exp.With)
_WRITE_ROOT = (
    exp.Insert,
    exp.Delete,
    exp.Update,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Command,
)


def is_write(sql: str) -> bool:
    """判断 SQL 是否含写操作（供 M5-2 的前置拦截复用）。

    解析失败时保守返回 True（宁可拦错不可漏拦）。
    多语句也算写。
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
    """五层门禁审查 SQL。

    参数
    ----
    sql : 待审查 SQL
    dataset : 数据集键，默认 B。用于 L4 语义门禁定位 Wren 客户端，也用于 L2 的 R1/R5/R6 三类
              需要 MDL 的规则——调用 rules.build_mdl_index(dataset) 现场建一次（如果
              ctx 没给 mdl_index）。
    requirement : 结构化技术需求对象（一期未深度使用；原样透传给 L2 ctx）
    exec_result : 真实执行后的结果集，供 L5 断言。传 None 时 L5 标记 skipped
    _dry_run_fn : 仅自测注入用：替换 Wren dry_run 的假桩函数

    返回
    ----
    dict: {status, layers, syntax_issues, semantic_issues, rule_violations, revised_sql, review_notes}
    """
    layers = []
    syntax_issues = []
    semantic_issues = []
    rule_violations = []
    revised_sql = None
    notes = []

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
    blocking_layers = [l for l in layers if l["layer"] in ("L1", "L2", "L3", "L4", "L5")]
    has_blocking_fail = any(
        (not l["ok"]) and (not l["skipped"]) for l in blocking_layers
    )
    if has_blocking_fail:
        final_status = "不通过"
    elif semantic_issues:
        final_status = "警告"
    else:
        final_status = "通过"

    if final_status == "警告" and not notes:
        rules_set = sorted({i["rule"] for i in semantic_issues})
        notes.append("L2 警告 %d 项（不阻断）：%s" % (len(semantic_issues), "、".join(rules_set)))
    review_notes = "；".join(notes) if notes else ("五层门禁审查完成：状态「%s」" % final_status)

    return {
        "status": final_status,
        "layers": layers,
        "syntax_issues": syntax_issues,
        "semantic_issues": semantic_issues,
        "rule_violations": rule_violations,
        "revised_sql": revised_sql,
        "review_notes": review_notes,
    }
