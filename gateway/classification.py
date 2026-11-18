# -*- coding: utf-8 -*-
"""分级分类自动脱敏（FR-7.8 / FR-7.9 · NFR-3.8）

落点：**应用端**，不在 SQL 执行端
--------------------------------
结果集是 SQL 执行端（Wren）原样返回的，**脱敏发生在应用端**：
网关/BFF 编排层拿到结果后，按分级分类策略对「展示与导出」做脱敏。
默认交付给客户端的「复制 SQL + 口径说明」这条路径**不携带脱敏逻辑**，
也**不得宣称已脱敏**（导读第 2 节第 5 条）。

失败即关闭（安全默认值，NFR-3.8）
--------------------------------
以下三种情况一律按**最高敏感级（L4 核心）**处理，即该列不展示（置空）：
  1. 策略源不可用（策略表读不到）；
  2. 字段未匹配到任何分级标准（未知 ⇒ 当作最敏感）；
  3. 分级命中但脱敏算法缺失/异常。
**不提供普通用户关闭该行为的开关**——安全默认值不可由业务侧关闭。

内置分级标准（可视为「默认策略集」）
------------------------------------
覆盖常见分析用语（度量/维度）与个人信息；未覆盖的列即视为「未匹配」，
按最高敏感级关闭，并在返回里显式列出 `closed`，供管理员到策略维护页补登记。
"""
import hashlib
import re

import db

# 分级（L1 最低 → L4 最高）
LEVELS = {
    "L1": "公开",
    "L2": "内部",
    "L3": "敏感",
    "L4": "核心",
}
_HIGHEST = "L4"

# 分类
CATEGORIES = ["组织信息", "经营指标", "个人信息", "联系方式", "身份标识", "财务信息"]

# 脱敏算法
ALGORITHMS = ["none", "mask", "hash", "truncate", "generalize", "nullify"]
_ALGO_LABEL = {
    "none": "不脱敏",
    "mask": "掩码",
    "hash": "哈希",
    "truncate": "截断",
    "generalize": "泛化",
    "nullify": "置空",
}

# 内置默认策略（按顺序匹配列名；第一条命中即生效）
_BUILTIN_POLICIES = [
    # 个人信息（强）
    {"level": "L4", "category": "个人信息", "pattern": r"身份证|证件号|id_?card|idcard", "algorithm": "mask",
     "description": "身份证号：保留首 6 末 4",
     "params": {"keep_head": 6, "keep_tail": 4}},
    {"level": "L4", "category": "财务信息", "pattern": r"银行卡|卡号|bank_?card|card_?no", "algorithm": "mask",
     "description": "银行卡号：仅尾 4 位可见",
     "params": {"keep_head": 0, "keep_tail": 4}},
    {"level": "L3", "category": "联系方式", "pattern": r"手机|电话|mobile|phone|tel", "algorithm": "mask",
     "description": "手机号：保留前 3 末 4",
     "params": {"keep_head": 3, "keep_tail": 4}},
    {"level": "L3", "category": "联系方式", "pattern": r"邮箱|e-?mail", "algorithm": "mask",
     "description": "邮箱：仅留首字母与域名", "params": {}},
    {"level": "L3", "category": "个人信息", "pattern": r"姓名|客户名|会员名|联系人|user_?name|member_?name",
     "algorithm": "mask", "description": "姓名：保留姓氏", "params": {"keep_head": 1, "keep_tail": 0}},
    {"level": "L3", "category": "个人信息", "pattern": r"地址|住址|address", "algorithm": "generalize",
     "description": "地址：泛化到市级", "params": {}},
    # 身份标识
    {"level": "L2", "category": "身份标识", "pattern": r"member_?id|customer_?id|user_?id|客户号|会员号|用户号",
     "algorithm": "hash", "description": "客户标识：哈希化（保留可关联性、去掉可识别性）", "params": {"len": 12}},
    # 经营指标（业务需要，不脱敏）
    {"level": "L1", "category": "经营指标",
     "pattern": r"金额|销售额|营收|单价|价格|成本|毛利|利润|坪效|数量|销量|件数|"
                r"amount|sales|revenue|price|cost|profit|qty|quantity|count|sum|avg|rate|pct|占比|环比|同比",
     "algorithm": "none", "description": "经营指标：数值本身不构成个人信息，正常展示", "params": {}},
    # 维度与时间（公开）
    {"level": "L1", "category": "组织信息",
     "pattern": r"门店|店铺|store|shop|商品|品类|分类|类目|品牌|渠道|区域|地区|城市|省份|部门|组织|"
                r"category|channel|region|city|province|dept|brand|product",
     "algorithm": "none", "description": "组织/商品维度：不含个人信息，正常展示", "params": {}},
    {"level": "L1", "category": "组织信息",
     "pattern": r"日期|时间|年月|月份|季度|周|date|time|month|quarter|week|year|day",
     "algorithm": "none", "description": "时间维度：正常展示", "params": {}},
]

# 供前端展示的等级图例
LEVEL_LEGEND = [{"level": k, "label": v} for k, v in LEVELS.items()]


class ClassificationError(RuntimeError):
    pass


def _token(value, algo, params):
    """按算法把单个值转成脱敏后的展示值。"""
    s = "" if value is None else str(value)
    if algo in (None, "none"):
        return value
    if algo == "nullify":
        return "***"
    if algo == "hash":
        n = int((params or {}).get("len", 12))
        return "H" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]
    if algo == "truncate":
        n = int((params or {}).get("len", 4))
        return s[:n] + "…"
    if algo == "generalize":
        # 地址泛化：取到「市」为止；其余退化为首个分隔段
        m = re.match(r"^([\u4e00-\u9fa5]{2,8}(?:省|自治区|市))?([\u4e00-\u9fa5]{2,8}市)?", s)
        if m and (m.group(1) or m.group(2)):
            return (m.group(1) or "") + (m.group(2) or "") + "***"
        return s[:2] + "***" if len(s) > 2 else "***"
    if algo == "mask":
        head = int((params or {}).get("keep_head", 3))
        tail = int((params or {}).get("keep_tail", 4))
        if "@" in s and not params:
            local = s.split("@")[0]
            return (local[:1] if local else "*") + "***@" + s.split("@")[-1]
        if len(s) <= head + tail:
            return "*" * len(s)
        return s[:head] + "*" * (len(s) - head - tail) + (s[-tail:] if tail else "")
    return "***"


# ---------------------------------------------------------------------------
# 策略读取
# ---------------------------------------------------------------------------
def policies(dataset=None, include_builtin=True):
    """列出分级分类策略。表中的（可维护）优先，内置默认策略作为兜底展示。"""
    items = []
    source_ok = True
    try:
        rows = db.query(
            "SELECT policy_id, dataset, level, category, pattern, algorithm, params, "
            "       description, enabled, updated_by, updated_at::text AS updated_at "
            "FROM classification_policies "
            + ("WHERE dataset=%s OR dataset='' OR dataset IS NULL" if dataset else "")
            + " ORDER BY level DESC, policy_id",
            ((dataset,) if dataset else ()),
        )
        for r in rows:
            r["source"] = "table"
            items.append(r)
    except Exception as e:  # noqa: BLE001
        source_ok = False
        seeded = "%s: %s" % (type(e).__name__, str(e)[:160])
    else:
        seeded = None

    if include_builtin:
        for i, p in enumerate(_BUILTIN_POLICIES):
            items.append({
                "policy_id": "BUILTIN-%02d" % (i + 1),
                "dataset": "", "level": p["level"], "category": p["category"],
                "pattern": p["pattern"], "algorithm": p["algorithm"],
                "params": p.get("params") or {}, "description": p["description"],
                "enabled": True, "source": "builtin",
            })

    return {
        "ok": source_ok,
        "sealed": not source_ok,          # 策略源不可用 ⇒ 标记「失败即关闭」
        "source_error": seeded,
        "count": len(items),
        "levels": LEVEL_LEGEND,
        "categories": CATEGORIES,
        "algorithms": [{"key": a, "label": _ALGO_LABEL[a]} for a in ALGORITHMS],
        "items": items,
        "note": "策略表为空/不可用时按最高敏感级处理（失败即关闭）；"
                "内置默认策略覆盖常见分析用语。",
    }


def save_policy(level, category, pattern, algorithm="mask", params=None,
                description="", dataset="", policy_id=None, actor="admin"):
    """新增/更新一条分级分类策略（数据管家动作）。"""
    if level not in LEVELS:
        return {"ok": False, "error": "level 必须是 %s 之一" % "、".join(LEVELS)}
    if algorithm not in ALGORITHMS:
        return {"ok": False, "error": "algorithm 必须是 %s 之一" % "、".join(ALGORITHMS)}
    if not str(pattern or "").strip():
        return {"ok": False, "error": "pattern（字段匹配表达式）必填"}
    try:
        re.compile(pattern)  # 提前校验正则，避免入库后每次脱敏才炸
    except re.error as e:
        return {"ok": False, "error": "pattern 不是合法正则：%s" % str(e)[:120]}

    pid = policy_id or ("CP-" + hashlib.sha256(
        ("%s|%s|%s" % (dataset, pattern, level)).encode("utf-8")).hexdigest()[:12])
    try:
        db.execute(
            """
            INSERT INTO classification_policies
                (policy_id, dataset, level, category, pattern, algorithm, params, description,
                 enabled, updated_by)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
            ON CONFLICT (policy_id) DO UPDATE SET
                dataset=EXCLUDED.dataset, level=EXCLUDED.level, category=EXCLUDED.category,
                pattern=EXCLUDED.pattern, algorithm=EXCLUDED.algorithm, params=EXCLUDED.params,
                description=EXCLUDED.description, enabled=true, updated_by=EXCLUDED.updated_by,
                updated_at=now()
            """,
            (pid, dataset or "", level, category, pattern, algorithm,
             db.dumps(params or {}), description or "", actor or "admin"),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "策略写入失败 %s: %s" % (type(e).__name__, str(e)[:200])}
    return {"ok": True, "policy_id": pid}


# ---------------------------------------------------------------------------
# 列分级
# ---------------------------------------------------------------------------
def _compiled_policies(dataset=None):
    """返回 [(compiled_regex, level, category, algorithm, params, description, source)]。

    表中策略在前（可覆盖内置），内置在后兜底。
    """
    out = []
    pol = policies(dataset, include_builtin=True)
    for p in pol["items"]:
        if not p.get("enabled", True):
            continue
        try:
            out.append((re.compile(p["pattern"], re.IGNORECASE), p["level"], p["category"],
                        p["algorithm"], p.get("params") or {}, p.get("description") or "",
                        p.get("source")))
        except re.error:
            continue
    return out, pol["sealed"]


def classify_columns(dataset, columns):
    """按策略对结果列分级。未匹配 ⇒ 最高敏感级 L4 且算法 nullify（失败即关闭）。"""
    rules, sealed = _compiled_policies(dataset)
    out = []
    for c in columns or []:
        name = str(c)
        hit = None
        for rx, level, category, algo, params, desc, source in rules:
            if rx.search(name):
                hit = (level, category, algo, params, desc, source)
                break
        if sealed:
            # 策略源不可用：全部按最高敏感级关闭
            out.append({
                "column": name, "level": _HIGHEST, "level_label": LEVELS[_HIGHEST],
                "category": "未知", "algorithm": "nullify", "algorithm_label": _ALGO_LABEL["nullify"],
                "matched": False, "source": "closed-by-default",
                "reason": "策略源不可用，按最高敏感级处理（失败即关闭）",
            })
        elif hit:
            level, category, algo, params, desc, source = hit
            out.append({
                "column": name, "level": level, "level_label": LEVELS.get(level, level),
                "category": category, "algorithm": algo, "algorithm_label": _ALGO_LABEL.get(algo, algo),
                "matched": True, "source": source,
                "description": desc, "params": params,
            })
        else:
            out.append({
                "column": name, "level": _HIGHEST, "level_label": LEVELS[_HIGHEST],
                "category": "未知", "algorithm": "nullify", "algorithm_label": _ALGO_LABEL["nullify"],
                "matched": False, "source": "closed-by-default",
                "reason": "字段未匹配到分级标准，按最高敏感级处理（失败即关闭）",
            })
    return out, sealed


def _apply_to_rows(columns, rows, per_column):
    algo_by_col = {p["column"]: (p["algorithm"], p.get("params") or {}) for p in per_column}
    out_rows = []
    for row in rows or []:
        new_row = []
        for idx, _c in enumerate(columns or []):
            value = row[idx] if idx < len(row) else None
            col = str(columns[idx]) if idx < len(columns or []) else ""
            algo, params = algo_by_col.get(col, ("nullify", {}))
            try:
                new_row.append(_token(value, algo, params))
            except Exception:  # noqa: BLE001 —— 算法异常 ⇒ 失败即关闭
                new_row.append("***")
        out_rows.append(new_row)
    return out_rows


def mask_rows(dataset, columns, rows, reveal=False):
    """对结果集做分级分类脱敏（应用端）。

    reveal=True 为受控查看（仍需授权），此时返回原始值，但**逐列标注**其等级——
    受控查看不是「不脱敏」，而是「脱敏可按授权放开」。
    """
    per_column, sealed = classify_columns(dataset, columns)
    masked_rows = _apply_to_rows(columns, rows, per_column)
    applied = [p for p in per_column if p["algorithm"] not in (None, "none")]
    closed = [p for p in per_column if p["source"] == "closed-by-default"]
    return {
        "ok": True,
        "sealed": sealed,
        "reveal": bool(reveal),
        "columns": list(columns or []),
        "rows": rows if reveal else masked_rows,
        "raw_rows": masked_rows if reveal else None,
        "per_column": per_column,
        "applied": [{"column": p["column"], "level": p["level"], "algorithm": p["algorithm"],
                     "algorithm_label": p["algorithm_label"], "category": p["category"]}
                    for p in applied],
        "closed": [{"column": p["column"], "reason": p.get("reason")} for p in closed],
        "levels": LEVEL_LEGEND,
        "location": "应用端（网关/BFF 编排层），不在 SQL 执行端",
        "note": "失败即关闭：策略源不可用或字段未匹配时按最高敏感级处理，业务侧不可关闭。",
    }


def preview(dataset, columns, rows):
    """脱敏预览：前（原始）/ 后（脱敏）对照 + 逐列分级。"""
    per_column, sealed = classify_columns(dataset, columns)
    after = _apply_to_rows(columns, rows, per_column)
    applied = [p for p in per_column if p["algorithm"] not in (None, "none")]
    closed = [p for p in per_column if p["source"] == "closed-by-default"]
    return {
        "ok": True,
        "sealed": sealed,
        "columns": list(columns or []),
        "before_rows": rows or [],
        "after_rows": after,
        "per_column": per_column,
        "applied": applied,
        "closed": closed,
        "levels": LEVEL_LEGEND,
        "location": "应用端（网关/BFF 编排层），不在 SQL 执行端",
        "boundary": "默认交付路径为「复制 SQL + 口径说明」到自有客户端执行，"
                    "该路径不携带脱敏逻辑、不得宣称已脱敏。",
    }
