# -*- coding: utf-8 -*-
"""合规预检（FR-4.11）· 可满足性预检（FR-4.10）· 危险语句拦截（FR-7.5）

三道闸门，位置不同、判据不同，**不合并、不复用判据**
-----------------------------------------------------
  闸门①  合规预检     本模块 precheck()        语义分析阶段，查「该不该答」
  闸门②  可满足性预检 本模块 satisfiability()  结构化后 / 检索前，查「能不能答」
  闸门③  危险语句拦截 本模块 danger_guard()    SQL 生成后，查「语句危不危险」

为什么必须分开（导读第 2 节第 3 条）
------------------------------------
三者回答的是三个不同问题，合并会让「合规风险」与「能力缺口」混成一句错误提示：
  · 该不该答 —— 命中敏感明文 / 越权 / 违规意图 / 提示注入 ⇒ 拦截 + 合规改写建议；
  · 能不能答 —— 字段未建模 / 方言不支持 ⇒ 明确「不支持」与可替代方案，不给出错误 SQL；
  · 危不危险 —— 非只读 / 多语句 / 无边界全表扫描 ⇒ 硬拦截，绝不下发执行。

判据来自哪
----------
全部**规则式**（正则 / 关键词 / 白名单），不调用任何大模型——私有化部署下
「数据不出网」是硬约束（NFR-3.6／3.7），预检链路也不得出网。

数字口径（数字纪律）
--------------------
本模块对外返回的比率（误拦率／命中率）一律标注为**目标值 / 演示参数**，
不得读成实测结果（PRD 中带 `[假设]`）。
"""
import datetime as dt
import re

import db
import masking as masking_mod

try:
    import demand as demand_mod
except Exception:  # noqa: BLE001 —— 模块缺失时预检退化为「只按传入文本判」，不阻断服务
    demand_mod = None

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # noqa: BLE001
    sqlglot = None
    exp = None


# ---------------------------------------------------------------------------
# 规则集（正则可读性优先，宁可多报不可漏报——与入口侧 masking 的取向一致）
# ---------------------------------------------------------------------------
# 提示注入：篡改系统指令 / 关闭留痕与门禁
_INJECTION_RULES = [
    (r"忽略(之前|以上|前面|上述)的?(所有)?(指令|规则|设定|提示)", "提示注入"),
    (r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions", "提示注入"),
    (r"(系统|system)\s*prompt", "提示注入"),
    (r"你现在是|请?扮演(一个)?|越狱模式|DAN\s*模式|开发者模式", "提示注入"),
    (r"(不要|无需|跳过|绕过)(记录|留痕|审计|审核|门禁|校验|复核)", "提示注入"),
]

# 越权：跨出授权范围访问
_OVERREACH_RULES = [
    (r"其他(部门|门店|区域|公司|商家|品牌|分公司)", "越权访问"),
    (r"竞争对手|友商|竞品|同业", "越权访问"),
    (r"(全部|所有|全量)(用户|客户|会员)(的)?(明细|清单|信息|手机号|身份证|名单)", "越权访问"),
    (r"未授权|绕过权限|超级权限|管理员权限|提升权限", "越权访问"),
]

# 违规意图：目的本身就违规
_VIOLATION_RULES = [
    (r"(导出|下载|拉取|获取|提供)\s*(全部|所有|全量)?\s*(用户|客户|会员|公民)"
     r"(个人信息|隐私|数据|名单|手机号|身份证)", "违规意图"),
    (r"(售卖|出售|交易|贩卖|倒卖).{0,8}(数据|信息|名单|用户)", "违规意图"),
    (r"(人肉|开盒|定位|追踪).{0,6}(个人|某人|用户|客户|特定人)", "违规意图"),
    (r"(删除|清空|抹掉)(审计|日志|留痕|记录|轨迹)", "违规意图"),
]

_RULES = (
    [("提示注入", re.compile(p), t, "blocking") for p, t in _INJECTION_RULES]
    + [("越权访问", re.compile(p), t, "blocking") for p, t in _OVERREACH_RULES]
    + [("违规意图", re.compile(p), t, "blocking") for p, t in _VIOLATION_RULES]
)

# 命中后的合规改写建议（给分析师看的「怎么改能合规」）
_SUGGESTIONS = {
    "敏感明文": "正文不要直接携带手机号 / 身份证 / 银行卡等明文；改用脱敏后的标识（如客户号）描述口径。",
    "提示注入": "需求描述里不要出现「忽略系统指令 / 跳过审核留痕」这类内容；本系统不接受任何关闭门禁或留痕的指令。",
    "越权访问": "把统计范围收敛到本业务域内；跨部门 / 跨品牌 / 友商数据不在授权范围，需走数据授权流程。",
    "违规意图": "该目的不属于数据分析授权范围。请改写为合规的统计口径（如聚合到门店 / 品类 / 时段），不接受个人级明细导出。",
}

# 数字纪律：以下为**目标值 / 演示参数**，非实测
_TARGETS = {
    "正常需求误拦率": "≤2%（目标值 / 演示参数，非实测）",
    "风险需求命中率": "100%（目标值 / 演示参数，非实测）",
}

# 方言不承诺范围（NFR-1.3）：命中即明确「不支持」，不给出错误 SQL
_DIALECT_UNSUPPORTED = [
    ("存储过程", r"CREATE\s+(OR\s+REPLACE\s+)?PROCEDURE|CALL\s+\w+\s*\("),
    ("复杂 UDF", r"CREATE\s+(OR\s+REPLACE\s+)?FUNCTION|自定义函数"),
    ("动态 SQL", r"EXECUTE\s+IMMEDIATE|PREPARE\s+\w+\s+FROM|sp_executesql"),
    ("跨库联邦查询", r"\b(fdw|foreign\s+data\s+wrapper|dblink|postgres_fdw|mysql_fdw)\b|@\w+\.\w+"),
]


class ComplianceError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 需求正文（供预检；用**掩码版**，与语义分析链路一致）
# ---------------------------------------------------------------------------
def _demand_text(demand_id):
    if not demand_id or demand_mod is None:
        return ""
    try:
        d = demand_mod.get(demand_id)  # 默认掩码版
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(d, dict):
        return ""
    parts = [
        d.get("title") or "",
        d.get("business_context") or "",
        d.get("description") or "",
        d.get("expected_output") or "",
    ]
    return "\n".join([p for p in parts if p])


def _persist(stage, demand_id, dataset, verdict, payload, actor):
    """合规留痕（best-effort：写不进去不影响预检结论返回）。"""
    try:
        db.execute(
            """
            INSERT INTO compliance_checks
                (stage, demand_id, dataset, verdict, payload, actor)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (stage, demand_id, dataset, verdict, db.dumps(payload), actor or "system"),
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 闸门① 合规预检（FR-4.11）—— 语义分析阶段，**早于任何 SQL 生成**
# ---------------------------------------------------------------------------
def precheck(text="", demand_id=None, dataset="B", actor=""):
    """合规预检：命中敏感明文 / 越权 / 违规意图 / 提示注入即拦截告警。

    返回
    ----
    dict: {ok, verdict（拦截/告警/通过）, stage, risks[], rewrite_suggestion,
           targets{...}, checked_at}
      · verdict='拦截' ⇒ 调用方**必须**在生成 SQL 之前停下，不接受继续；
      · risks[] 逐条给 {type, severity, matched, rule_id, why, suggestion}。
    """
    body = (text or "").strip()
    if not body and demand_id:
        body = _demand_text(demand_id)

    risks = []

    # 1) 敏感明文：复用入口侧 masking 的识别（口径唯一，不另立一套）
    hits, summary = masking_mod.scan(body)
    if hits:
        types = "、".join(sorted(summary.keys()))
        risks.append({
            "type": "敏感明文",
            "severity": "blocking",
            "rule_id": "COMP-PII",
            "matched": [h.get("preview") for h in hits[:5]],
            "why": "需求正文里出现了未脱敏的个人敏感信息（%s）。" % types,
            "suggestion": _SUGGESTIONS["敏感明文"],
        })

    # 2) 提示注入 / 越权 / 违规意图
    for label, pattern, rule_type, severity in _RULES:
        for m in pattern.finditer(body):
            frag = m.group(0)[:40]
            risks.append({
                "type": label,
                "severity": severity,
                "rule_id": "COMP-" + {"提示注入": "INJ", "越权访问": "OVER", "违规意图": "VIO"}[label],
                "matched": frag,
                "why": "命中%s特征：%s" % (label, frag),
                "suggestion": _SUGGESTIONS[label],
            })

    blocking = [r for r in risks if r["severity"] == "blocking"]
    if blocking:
        verdict = "拦截"
    elif risks:
        verdict = "告警"
    else:
        verdict = "通过"

    out = {
        "ok": True,
        "verdict": verdict,
        "stage": "语义分析（早于任何 SQL 生成）",
        "demand_id": demand_id,
        "dataset": dataset,
        "risk_count": len(risks),
        "blocking_count": len(blocking),
        "risks": risks,
        "rewrite_suggestion": "；".join(
            dict.fromkeys(r["suggestion"] for r in risks)
        ) if risks else "",
        "targets": _TARGETS,
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    _persist("precheck", demand_id, dataset, verdict, {
        "verdict": verdict,
        "risk_count": len(risks),
        "risks": risks,
    }, actor)
    return out


# ---------------------------------------------------------------------------
# 闸门② 可满足性预检（FR-4.10）—— 结构化后 / 检索前，查「能不能答」
# ---------------------------------------------------------------------------
_DIALECT_SCOPE = {
    "supported": ["SELECT / JOIN / GROUP BY / 窗口函数 / CTE", "标准聚合与时间函数"],
    "unsupported": ["存储过程", "复杂 UDF", "动态 SQL", "跨库联邦查询"],
}

# 结构化需求槽位的中文名（与 analysis 六槽位口径一致，仅取 requirement 对象里出现的四个单值槽位 + 输出字段）
_SLOT_LABELS = {
    "subject": "统计主体",
    "granularity": "统计颗粒度",
    "time_semantics": "时间口径",
    "data_scope": "范围条件",
    "output_fields": "输出字段",
}


def satisfiability(demand_id=None, dataset="B", text="", actor=""):
    """可满足性预检：需求要素能不能在当前数据源 / 方言范围内被满足。

    与合规预检的分工：合规判「该不该答」，这里判「能不能答」。
    判不了的（未建模）**明确说不支持并给可替代方案**，绝不硬凑一条错误 SQL。
    """
    reasons = []
    missing = {"tables": [], "columns": [], "terms": []}
    alternatives = []
    satisfiable = True

    body = (text or "").strip() or _demand_text(demand_id)

    # a) 方言不承诺范围（NFR-1.3）
    for label, pattern in _DIALECT_UNSUPPORTED:
        if re.search(pattern, body, re.IGNORECASE):
            satisfiable = False
            reasons.append("需求涉及「%s」，不在方言兼容范围。" % label)
            missing["terms"].append(label)
            alternatives.append({
                "kind": "dialect",
                "label": "改用标准 SQL 等价表达",
                "detail": "把%s改写为标准 SELECT / JOIN / 聚合实现；本系统只承诺标准只读查询。"
                          "若确实需要，请走人工口径治理流程。" % label,
            })

    # b) 结构化需求要素：槽位缺候选 / 输出字段为空 → 不可满足（返回「不支持」而非错误 SQL）
    #
    # 判据取自 requirement.get() 的真实字段：四个单值槽位（subject / granularity /
    # time_semantics / data_scope）+ 数组槽位 output_fields。槽位取不到时 build() 会写
    # value="无候选结论…" 且 confidence=0、并另立一条带 missing_field 的阻断级 residual_risk
    # ——这里认这两类标记，不臆造字段名。
    if demand_id and demand_mod is not None:
        try:
            import requirement as requirement_mod
            req = requirement_mod.get(demand_id)
        except Exception:  # noqa: BLE001
            req = None
        if isinstance(req, dict) and req.get("ok") is not False:
            missing_fields = []
            for f in ("subject", "granularity", "time_semantics", "data_scope"):
                claim = req.get(f)
                if not isinstance(claim, dict):
                    missing_fields.append(f)
                    continue
                if float(claim.get("confidence") or 0) == 0.0 \
                        or "无候选" in str(claim.get("value") or ""):
                    missing_fields.append(f)
            if not (req.get("output_fields") or []):
                missing_fields.append("output_fields")
            for r in (req.get("residual_risks") or []):
                if isinstance(r, dict) and r.get("missing_field"):
                    missing_fields.append(r["missing_field"])
            # 去重保序
            missing_fields = list(dict.fromkeys(missing_fields))
            for f in missing_fields:
                label = _SLOT_LABELS.get(f, f)
                missing["terms"].append(label)
                reasons.append("需求要素「%s」未取到结论，系统无法在缺要素的情况下生成可执行 SQL。" % label)
            if missing_fields:
                satisfiable = False
                alternatives.append({
                    "kind": "governance",
                    "label": "先补口径再生成",
                    "detail": "到「口径维护」登记缺失口径（或到「业务说法字典」补字段映射），"
                              "补齐后重新生成即可命中。",
                })

    if not satisfiable:
        verdict = "不支持"
    elif reasons:
        verdict = "部分支持"
    else:
        verdict = "可满足"

    out = {
        "ok": True,
        "verdict": verdict,
        "satisfiable": bool(satisfiable and not reasons),
        "stage": "结构化后 / 检索前（早于 SQL 生成）",
        "demand_id": demand_id,
        "dataset": dataset,
        "reasons": reasons,
        "missing": missing,
        "alternatives": alternatives,
        "dialect_scope": _DIALECT_SCOPE,
        "policy": "判不了就说「不支持」并给可替代方案，不给出错误 SQL。",
    }
    _persist("satisfiability", demand_id, dataset, verdict, {
        "verdict": verdict, "reasons": reasons, "missing": missing,
    }, actor)
    return out


# ---------------------------------------------------------------------------
# 闸门③ 危险语句拦截（FR-7.5）—— SQL 生成后，查「语句危不危险」
# ---------------------------------------------------------------------------
# EXPLAIN 阻断阈值：`[待核实 per DB]`（NFR-4.5），Demo 中作为**演示参数**展示
_EXPLAIN_ROW_LIMIT = 10_000_000

_DANGER_GUIDES = {
    "非只读语句": "本系统只接受只读查询（SELECT / WITH）。写操作请走人工 DDL 变更流程。",
    "多语句": "一次只允许一条语句；多语句可能被用于绕过只读校验，已拦截。",
    "无边界全表扫描": "补上时间或维度过滤条件，或加 LIMIT，避免全表扫描拖垮数据源。",
    "EXPLAIN 估算超阈值": "预估扫描行数超过阈值（演示参数 1000 万行，`[待核实 per DB]`），请收窄过滤条件。",
    "方言不支持": "语句含方言不承诺范围（存储过程 / UDF / 动态 SQL / 跨库联邦），已按「不支持」拦截。",
}


def danger_guard(sql, dataset="B", demand_id=None, explain_rows=None, actor=""):
    """危险语句拦截：非只读 / 多语句 / 无边界全表 / 超阈值扫描 / 方言不支持。

    `explain_rows` 为调用方（可选）传入的 EXPLAIN 估算行数；不传则不评估该项，
    并在 `not_evaluated` 里显式声明「未评估」——**缺席必须显式报未执行**，
    不得静默读成「没问题」。
    """
    text = (sql or "").strip()
    reasons = []
    not_evaluated = []

    if not text:
        out = {
            "ok": True, "blocked": False, "verdict": "通过",
            "sql": "", "reasons": [],
            "not_evaluated": ["语句为空，无需拦截"],
            "thresholds": {"explain_rows": _EXPLAIN_ROW_LIMIT, "note": "[待核实 per DB]（演示参数）"},
        }
        _persist("danger_guard", demand_id, dataset, "通过", {"sql": "", "blocked": False}, actor)
        return out

    # a) 多语句（分号分隔的非空语句 > 1）
    stripped = re.sub(r"--[^\n]*", "", text)
    statements = [s for s in stripped.split(";") if s.strip()]
    if len(statements) > 1:
        reasons.append(("多语句", "检测到 %d 条语句" % len(statements)))

    # b) 只读白名单 + 方言范围
    if sqlglot is not None:
        try:
            parsed = sqlglot.parse(text, read="postgres")
            stmts = [s for s in parsed if s is not None]
            if stmts:
                root = stmts[0]
                if not isinstance(root, (exp.Select, exp.With)):
                    reasons.append(("非只读语句", "根节点为 %s" % type(root).__name__))
                else:
                    # 无边界全表扫描：无 WHERE、无 LIMIT、无 GROUP BY（聚合全表另算）
                    has_where = root.find(exp.Where) is not None
                    has_limit = root.find(exp.Limit) is not None
                    has_group = root.find(exp.Group) is not None
                    joins = list(root.find_all(exp.Join))
                    if not has_where and not has_limit and not has_group and len(joins) >= 1:
                        reasons.append(("无边界全表扫描", "多表关联且无 WHERE / LIMIT / GROUP BY"))
        except Exception as e:  # noqa: BLE001
            # 解析不了不在这里报红（语法归 L1），但要显式记「未评估」
            not_evaluated.append("语法解析失败（%s），只读与扫描面判定未执行" % type(e).__name__)
    else:
        not_evaluated.append("sqlglot 不可用，只读与扫描面判定未执行")

    for label, pattern in _DIALECT_UNSUPPORTED:
        if re.search(pattern, text, re.IGNORECASE):
            reasons.append(("方言不支持", label))

    # c) EXPLAIN 估算（可选）
    if explain_rows is None:
        not_evaluated.append(
            "EXPLAIN 估算未提供，未评估行数阈值（阈值 %d 行为演示参数，`[待核实 per DB]`）"
            % _EXPLAIN_ROW_LIMIT
        )
    else:
        try:
            est = int(explain_rows)
            if est > _EXPLAIN_ROW_LIMIT:
                reasons.append(("EXPLAIN 估算超阈值",
                                "预估 %d 行 > %d 行" % (est, _EXPLAIN_ROW_LIMIT)))
        except Exception:  # noqa: BLE001
            not_evaluated.append("explain_rows 非整数，未评估")

    blocked = bool(reasons)
    out = {
        "ok": True,
        "blocked": blocked,
        "verdict": "拦截" if blocked else "通过",
        "demand_id": demand_id,
        "dataset": dataset,
        "sql": text,
        "reasons": [
            {"type": t, "detail": d, "guide": _DANGER_GUIDES.get(t, "")}
            for t, d in reasons
        ],
        "not_evaluated": not_evaluated,
        "thresholds": {"explain_rows": _EXPLAIN_ROW_LIMIT, "note": "[待核实 per DB]（演示参数）"},
    }
    _persist("danger_guard", demand_id, dataset, out["verdict"], {
        "sql": text[:2000], "blocked": blocked,
        "reasons": [t for t, _ in reasons],
    }, actor)
    return out
