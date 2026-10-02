# -*- coding: utf-8 -*-
"""M6-1 · SQL 静态规则注册表（11 条规则：4 旧 + 5 新增 / 2 强化）

2026-10-02 变更：新增 R7 ENUM_VALUE_INVALID（枚举值校验）→ 第 11 条；
并引入 L2 分级阻断（REGISTRY 的 blocking 标记，见文件末与 gates.py）。

设计原则
--------
1. 纯函数模块：仅依赖 sqlglot.exp，无 IO、无 DB、无网络、无 LLM；可离线单测。
2. 规则可插拔：REGISTRY 是单一事实源，新增一条规则 = 追加一个 dict + 对应 evaluate。
3. 顺序稳定：evaluate_all 严格按 REGISTRY 的注册顺序产出结果，便于 diff / 回溯。
4. 规则粒度：一条规则只干一件事（单一职责），避免"一个函数同时报 GROUP BY 又报 DISTINCT"。
5. 输出形状统一：[{rule, snippet, detail}]，rule 是字符串 ID（与 id/name 两键一致），
   snippet 是原始可读 SQL 片段（人类直接能看懂，不出 canonical 键），detail 是中文说明。
6. 分级阻断：所有规则 severity 仍为 warning（不变）；但 REGISTRY 新增 "blocking" 标记，
   gates.review 的 L2 汇总按该标记决定是否阻断（见 gates.py）。
   现有 3 条硬错误规则（JOIN_WITHOUT_CONDITION / ONE_TO_MANY_UNHANDLED /
   UNMAPPED_OBJECT_REF）标记为 blocking，其余保持"只警告不阻断"。

ctx 约定
-------
evaluate_all 第二参数 ctx = {
    "dataset": "A" | "B",                 # 必填，R5/R6 用来定位 MDL
    "mdl_index": dict | None,             # 可选：已建好的 {models,tables,columns,rels,time_cols}；
                                          # 为 None 时若 R5/R6 触发会用 registry.get(dataset).mdl
                                          # 实时自建；但强烈建议调用方提前建好避免重复。
    "requirement": dict | None,           # 预留：结构化需求对象（R4 可能用到），一期未深度使用。
}
mdl_index 推荐形状（与 sqlpack._mdl_index 对齐）：
    {"models": {model_name: model_dict},
     "tables": {model_name: tableReference.table},
     "model_cols": {model_name: [{name,type,label,caliber,isPrimaryKey}]},
     "all_columns": {"model.col": True, "table.col": True, "col": True}  # 三层 R5 查表/列用
     "all_tables": {model_name, tableReference.table}  # 物理表名与模型名都能过
     "rels": mdl["relationships"]  # 原关系列表（R1 查 joinType 用）
     "time_cols": {"model.col": True, "table.col": True, "col": True}  # R6 查时间字段候选
     "enum_cols": {列名归一: 合法值集合}  # R7 枚举值校验：从列 description 的「枚举：A / B」提取
}
"""
import sys
import os

# 允许被 gates.py 从 gateway/ 目录直接 import（sys.path 已插 gateway）
GATEWAY_DIR = os.path.dirname(os.path.abspath(__file__))
if GATEWAY_DIR not in sys.path:
    sys.path.insert(0, GATEWAY_DIR)

import sqlglot
from sqlglot import exp


# ===========================================================================
# 一、GROUP_BY_INCONSISTENT 专用辅助（从 gates.py 整体搬迁；PT-2 已修好的 canonical 方案）
# ===========================================================================
def _norm_sql_name(s: str) -> str:
    """SQL 标识符规范化：去空白+小写+脱引号。别名/列名比较的统一基准。"""
    if not s:
        return ""
    x = str(s).strip().lower()
    while len(x) >= 2 and x[0] == x[-1] and x[0] in ("\"", "'", "`"):
        x = x[1:-1]
    return x.replace(" ", "")


def _select_alias_map(ast_stmt) -> dict:
    """从 SELECT 投影抽出「规范化别名 → 被别名定义的原始表达式（e.this）」映射。

    例：SELECT p.category_name AS 品类, SUM(x) AS amt → {"品类": p.category_name exp, "amt": sum exp}
    """
    aliases = {}
    if not hasattr(ast_stmt, "expressions"):
        return aliases
    for expr in ast_stmt.expressions:
        if isinstance(expr, exp.Alias):
            alias_tok = expr.args.get("alias")
            if alias_tok is None:
                continue
            alias_name = ""
            if isinstance(alias_tok, exp.Identifier):
                alias_name = alias_tok.name or (alias_tok.this.name if alias_tok.this else "")
            elif isinstance(alias_tok, exp.Literal) and alias_tok.is_string:
                alias_name = alias_tok.this.value or alias_tok.name
            alias_norm = _norm_sql_name(alias_name) if alias_name else ""
            if alias_norm:
                aliases[alias_norm] = expr.this
    return aliases


def _canonical_key(e, alias_map: dict, select_projections: list, depth=0):
    """把 GROUP BY / SELECT 非聚合投影表达式归一成可比字符串键。

    归约五步（对应 PT-2 六例去误报）：
      1) 剥 exp.Alias 递归；
      2) 整数 exp.Literal → 序号解引用到 SELECT 第 N 项再递归；
      3) 别名引用（Identifier 或 "品类" Column）→ alias_map 解引用再递归；
      4) exp.Column → 表名.列名（都小写、去空白），无表名则仅列名；
      5) 兜底：e.sql(postgres).lower().replace(" ", "")。

    只在 GROUP_BY_INCONSISTENT 内部比较时使用——**绝不**输出给用户，用户始终看原始 SQL 片段。
    """
    if e is None:
        return ""
    if depth > 6:
        return e.sql(dialect="postgres").strip().lower().replace(" ", "")
    # 1) 剥 Alias
    if isinstance(e, exp.Alias):
        return _canonical_key(e.this, alias_map, select_projections, depth + 1)
    # 2) 整数 Literal → GROUP BY 序号（1-based）
    if isinstance(e, exp.Literal) and getattr(e, "is_int", False):
        try:
            n = int(str(e.this))
        except Exception:
            n = 0
        if 1 <= n <= len(select_projections):
            return _canonical_key(select_projections[n - 1], alias_map, select_projections, depth + 1)
        return str(n)
    # 3/4) Identifier 或 Column
    if isinstance(e, (exp.Identifier, exp.Column)):
        name_str = ""
        if isinstance(e, exp.Identifier):
            if e.name:
                name_str = e.name
            elif hasattr(e, "this") and e.this:
                name_str = getattr(e.this, "name", "") or str(e.this)
        if isinstance(e, exp.Column):
            # 4) Column → 表名.列名 canonical
            tbl = ""
            tbl_tok = e.args.get("table")
            if tbl_tok is not None:
                tbl = (tbl_tok.name if isinstance(tbl_tok, exp.Identifier) else str(tbl_tok)) or ""
            col_name = ""
            col_tok = e.this
            if col_tok is not None:
                col_name = (col_tok.name if isinstance(col_tok, exp.Identifier) else str(col_tok)) or ""
            k = ((_norm_sql_name(tbl) + ".") if tbl else "") + _norm_sql_name(col_name)
            # 若是别名引用（如"品类"）→ 再解一次
            if k in alias_map:
                return _canonical_key(alias_map[k], alias_map, select_projections, depth + 1)
            return k
        # Identifier 分支：是否别名引用
        nm = _norm_sql_name(name_str)
        if nm and nm in alias_map:
            return _canonical_key(alias_map[nm], alias_map, select_projections, depth + 1)
        return nm
    # 5) 兜底
    raw = e.sql(dialect="postgres").strip()
    return raw.lower().replace(" ", "")


# ===========================================================================
# 二、mdl_index 构建器（给 R5/R6 用；调用方若事先没传 ctx.mdl_index 就现建）
# ===========================================================================
_TIME_KEYWORDS = [
    "日期", "时间", "date", "dt", "month", "月份",
    "统计月份", "统计日期", "时间戳", "timestamp",
]


def _is_time_label_match(label_norm: str, time_norms: list) -> bool:
    """label（列中文名）命中时间词的严格判定：以时间词**开头**或**整体全等**于某时间词，不做任意子串包含。

    设计动机：label 里经常写"当日下单会员数"这类叙述文本，若用子串包含会把"日"
    字误命中；严格匹配保证"统计日期"/"日期"开头才被认作时间候选。

    本轮再加"非时间后缀排除"：如果 label 以时间词开头，但紧随其后第一个非空字符
    就进入「类型 / 枚举 / 标志 / 标识 / 名称 / 说明 / 分类 / 编码 / 代号」等非时间语义词，
    说明这是"属性是*关于*时间的，而不是时间维度本身"（如 day_type="日期类型；枚举=工作日/周末/节假日"
    是"日期的类型"不是"日期本身"）→ 判 False，防止白名单混入。
    """
    if not label_norm:
        return False
    _NOT_TIME_SUFFIXES = ("类型", "枚举", "标志", "标识", "名称",
                          "说明", "分类", "编码", "代号")
    # 先把标点/符号/空格当切分用；_norm_sql_name 做过 lower/去空格，但中文符号保留
    # 为防 `日期类型` 连写，再做"从时间词尾向后看 0-1 个分隔符后是啥"
    import re as _re2
    for tn in time_norms:
        if not tn:
            continue
        if label_norm == tn:
            return True
        if label_norm.startswith(tn):
            # 取时间词之后的尾巴
            tail = label_norm[len(tn):]
            if not tail:
                return True
            # 跳过 0~2 个标点或空白
            stripped = _re2.split(r"[\s;；,，、:：\-_/\\]", tail, maxsplit=1)[0] if True else tail.lstrip(";；,，、:： _-/\\")
            if not stripped:
                return True
            # 看尾部前两字是否以任何一个非时间前缀开头
            for nts in _NOT_TIME_SUFFIXES:
                if stripped.startswith(nts):
                    # 命中"日期类型"→ 这是关于日期的属性（不是日期列本身）→ continue，不返回 True
                    break
            else:
                # 所有 _NOT_TIME_SUFFIXES 都没命中开头 → 合法时间 label（比如 "日期开始", "月份", "统计日期(归属)"）
                return True
    return False


def build_mdl_index(dataset: str) -> dict:
    """从 registry.get(dataset).mdl 构建 R5/R6 用的轻量索引。

    为什么只在规则命中时才建（或由调用方提前建一次）：
    - 单条 SQL 若不触发 R5/R6（80% 场景），建索引是白花钱；
    - mdl_index 对 gates.review 的同一批次调用可复用——强烈建议调用方在批前建一次
      并通过 ctx["mdl_index"] 传入。
    """
    try:
        import registry as registry_mod
        import metadata as metadata_mod
    except Exception:
        registry_mod = None
        metadata_mod = None
    if registry_mod is None:
        return {"models": {}, "tables": {}, "model_cols": {},
                "all_columns": set(), "all_tables": set(), "rels": [], "time_cols": set(),
                "enum_cols": {}}
    mdl = registry_mod.get(dataset).mdl
    models = mdl.get("models", []) or []
    out_models = {}
    out_tables = {}
    model_cols = {}
    all_columns = set()
    all_tables = set()
    time_cols = set()
    time_norms = [_norm_sql_name(w) for w in _TIME_KEYWORDS]
    import re as _re
    # 列名（英文技术名）匹配：正则 命中时间关键词的子串；去掉「日」「年」单字噪声
    col_time_pat = _re.compile(
        r"(日期|时间|date|dt|month|月份|统计月份|统计日期|timestamp|time_kw|_date$|_dt$|_month$|stat_date|stat_month|order_date|open_date|register_date|change_month)",
        _re.IGNORECASE,
    )
    for m in models:
        mname = m.get("name")
        out_models[mname] = m
        all_tables.add(_norm_sql_name(mname))
        tref = (m.get("tableReference") or {}).get("table")
        if tref:
            out_tables[mname] = tref
            all_tables.add(_norm_sql_name(tref))
        pk_raw = m.get("primaryKey")
        pk_list = [pk_raw] if isinstance(pk_raw, str) else list(pk_raw or [])
        cols = []
        for c in (m.get("columns") or []):
            cname = c.get("name")
            desc = c.get("description")
            label = None
            if metadata_mod is not None:
                label, _caliber = metadata_mod.split_label_caliber(desc)
            is_pk = bool(c.get("isPrimaryKey") or cname in pk_list)
            cols.append({"name": cname, "type": c.get("type") or "text",
                         "label": label, "isPrimaryKey": is_pk})
            # R5 三层查表/列
            n_cn = _norm_sql_name(cname)
            all_columns.add(n_cn)
            all_columns.add(_norm_sql_name(mname) + "." + n_cn)
            if tref:
                all_columns.add(_norm_sql_name(tref) + "." + n_cn)
            # R6 时间候选：两条件满足其一即可
            #   1) 列名（英文技术名）命中列名时间正则；
            #   2) 列中文名（label）严格以时间词开头或等于某个时间词（不做任意子串）。
            label_n = _norm_sql_name(label or "")
            hit_time = False
            if col_time_pat.search(cname or ""):
                hit_time = True
            if not hit_time and _is_time_label_match(label_n, time_norms):
                hit_time = True
            if hit_time:
                time_cols.add(n_cn)
                time_cols.add(_norm_sql_name(mname) + "." + n_cn)
                if tref:
                    time_cols.add(_norm_sql_name(tref) + "." + n_cn)
        model_cols[mname] = cols
    return {
        "models": out_models,
        "tables": out_tables,
        "model_cols": model_cols,
        "all_columns": all_columns,
        "all_tables": all_tables,
        "rels": list(mdl.get("relationships", []) or []),
        "time_cols": time_cols,
        "enum_cols": _extract_enum_cols(out_models),   # 新增：{列名归一: 合法值集合}
    }


_ENUM_RE = None


def _extract_enum_cols(models: dict) -> dict:
    """从 MDL 各列的 description 里提取「枚举值域」。

    只认 '枚举：A / B / C'（中文或英文冒号）这一种写法 —— 精确、不误判；
    后面若带 '（无其他取值）' 之类括号说明，先剥掉；遇 '。' / '；' / ';' 截止。
    返回 {列名归一: set(合法值)}；无声明的列不进结果（该列不校验，宁可少报）。
    """
    import re as _re
    global _ENUM_RE
    if _ENUM_RE is None:
        _ENUM_RE = _re.compile(r"枚举[:：]\s*([^。；;]+)")
    out = {}
    for _mn, _mm in (models or {}).items():
        for _c in (_mm.get("columns") or []):
            _desc = _c.get("description") or ""
            _mo = _ENUM_RE.search(_desc)
            if not _mo:
                continue
            _raw = _re.sub(r"[（(].*?[)）]", "", _mo.group(1))
            _vals = {v.strip() for v in _re.split(r"[/、，,]", _raw) if v.strip()}
            if _vals:
                out[_norm_sql_name(_c.get("name"))] = _vals
    return out


# ===========================================================================
# 三、11 条规则逐条 evaluate
# ===========================================================================
# ----------------------------
# 规则 1/9：JOIN_WITHOUT_CONDITION（老规则，行为一字不变）
# ----------------------------
def _ev_join_without_condition(ast_stmt, ctx):
    out = []
    for j in ast_stmt.find_all(exp.Join):
        on_ok = j.args.get("on") is not None
        using_ok = bool(j.args.get("using"))
        if not on_ok and not using_ok:
            # 修正（2026-10-02 误报修复）：逗号连接的子查询（如 FROM (sub) a, (sub) b）
            # 被 sqlglot 解析为 Join(kind=None, this=Subquery, on=None)，语义上确实是无条件连接，
            # 但业务上合法（分子 / 分母两个标量子查询做笛卡尔积是标准写法，如复购率）。
            # 因此连接对象是子查询时不报；只对真实表之间的隐式笛卡尔积报。
            if isinstance(j.this, exp.Subquery) or j.this.find(exp.Subquery) is not None:
                continue
            out.append({
                "rule": "JOIN_WITHOUT_CONDITION",
                "snippet": j.sql(dialect="postgres"),
                "detail": "检测到无连接条件的 JOIN（隐式笛卡尔积），请补充 ON 或 USING 子句",
            })
    return out


# ----------------------------
# 规则 2/9：SELECT_STAR（老规则，行为一字不变）
# ----------------------------
def _ev_select_star(ast_stmt, ctx):
    out = []
    for sel in ast_stmt.find_all(exp.Select):
        for expr in sel.expressions:
            if isinstance(expr, exp.Star):
                out.append({
                    "rule": "SELECT_STAR",
                    "snippet": expr.sql(dialect="postgres"),
                    "detail": "使用 SELECT * 返回未明确定义输出列，建议列出所需字段以降低耦合与数据泄露风险",
                })
                break
    return out


# ----------------------------
# 规则 3/9：GROUP_BY_INCONSISTENT（老规则，PT-2 修好的 canonical 方案整体搬迁）
# ----------------------------
def _ev_group_by_inconsistent(ast_stmt, ctx):
    out = []
    if not isinstance(ast_stmt, (exp.Select, exp.With)):
        # 非查询根跳过（WITH 内部会递归到各 Select）
        pass
    gb_expressions = list(ast_stmt.find_all(exp.Group))
    if not gb_expressions:
        return out
    select_proj = list(ast_stmt.expressions) if hasattr(ast_stmt, "expressions") else []
    alias_map = _select_alias_map(ast_stmt)
    gb_keys = set()
    for ge in gb_expressions:
        for e in ge.expressions:
            k = _canonical_key(e, alias_map, select_proj)
            if k:
                gb_keys.add(k)
    non_agg_keys = []
    non_agg_snippets = []
    for expr in select_proj:
        if isinstance(expr, exp.Star):
            continue
        has_agg = any(isinstance(a, exp.AggFunc) for a in expr.walk())
        inner = expr.this if isinstance(expr, exp.Alias) else expr
        is_literal = isinstance(inner, exp.Literal)
        if not has_agg and not is_literal:
            k = _canonical_key(expr, alias_map, select_proj)
            if k:
                non_agg_keys.append(k)
                snippet = expr.sql(dialect="postgres").strip()
                non_agg_snippets.append(snippet[:120])
    missing = [c for c in non_agg_keys if c not in gb_keys]
    if missing:
        gb_snippets = []
        for ge in gb_expressions:
            for e in ge.expressions:
                gb_snippets.append(e.sql(dialect="postgres").strip())
        out.append({
            "rule": "GROUP_BY_INCONSISTENT",
            "snippet": ("GROUP BY 列: " + ", ".join(gb_snippets)
                        + " / SELECT 非聚合列: " + ", ".join(non_agg_snippets)),
            "detail": "SELECT 中的非聚合列 [%s] 未全部出现在 GROUP BY 中（规范化键比较），可能导致结果不确定" % ", ".join(missing),
        })
    return out


# ----------------------------
# 规则 4/9：UNNECESSARY_DISTINCT（老规则 + M6 强化：UNION / 子查询场景更准）
# ----------------------------
def _ev_unnecessary_distinct(ast_stmt, ctx):
    """UNNECESSARY_DISTINCT：原单表无聚合无 JOIN 的基础上，强化 UNION/子查询场景判断。

    强化点：
      - 若外层是 SELECT DISTINCT，但 FROM 是子查询（且子查询本身是 UNION / 聚合 / 有 DISTINCT），
        则外层 DISTINCT 大概率冗余 → 也记一条。
      - 原判定保留（单表无 JOIN 无聚合）且优先级更高，不能漏。
    一期先 warning，不阻断。
    """
    out = []
    if not isinstance(ast_stmt, exp.Select):
        return out
    if ast_stmt.args.get("distinct") is None:
        return out

    # 先跑老判定（保留原行为，一字不变）
    has_join = len(list(ast_stmt.find_all(exp.Join))) > 0
    has_agg = len(list(ast_stmt.find_all(exp.AggFunc))) > 0
    from_multi = False
    from_exprs = list(ast_stmt.find_all(exp.From))
    for f in from_exprs:
        tbls = list(f.find_all(exp.Table))
        if len(tbls) > 1:
            from_multi = True
            break
    legacy_hit = (not has_join and not has_agg and not from_multi)

    # M6 强化：外层 DISTINCT，但 FROM 里有子查询是 UNION ALL / UNION / 聚合 / 本身 DISTINCT
    enhanced_hit = False
    enhanced_reason = ""
    if not legacy_hit:
        for sub in ast_stmt.find_all(exp.Subquery):
            # 是否是 FROM 从句下的子查询（不在 WHERE/HAVING 里）
            parent = getattr(sub, "parent", None)
            # 简单启发：如果 FROM 表达式里含这个 Subquery（直接用 find_all 重合判断）
            from_subs = set()
            for f in from_exprs:
                from_subs.update(id(s) for s in f.find_all(exp.Subquery))
            if id(sub) not in from_subs:
                continue
            inner = sub.this if isinstance(sub.this, exp.Select) else None
            if inner is None:
                continue
            # UNION / UNION ALL 外层 DISTINCT 必然冗余
            if isinstance(inner, exp.Union):
                enhanced_hit = True
                enhanced_reason = "外层 DISTINCT 套在 UNION/UNION ALL 子查询外大概率冗余（UNION 已去重）"
                break
            if isinstance(inner, exp.Select) and inner.args.get("distinct") is not None:
                enhanced_hit = True
                enhanced_reason = "子查询自身已带 DISTINCT，外层再 DISTINCT 大概率冗余"
                break
            if any(isinstance(a, exp.AggFunc) for a in inner.walk()):
                enhanced_hit = True
                enhanced_reason = "子查询含聚合（聚合结果天然唯一），外层 DISTINCT 冗余"
                break

    if legacy_hit:
        out.append({
            "rule": "UNNECESSARY_DISTINCT",
            "snippet": "SELECT DISTINCT（单表无聚合无 JOIN）",
            "detail": "DISTINCT 在无 JOIN、无聚合、单表场景下可能是冗余的，请确认是否确实需要去重",
        })
    elif enhanced_hit:
        out.append({
            "rule": "UNNECESSARY_DISTINCT",
            "snippet": "SELECT DISTINCT + FROM 子查询（%s）" % enhanced_reason[:40],
            "detail": enhanced_reason,
        })
    return out


# ----------------------------
# 规则 5/9：R1 ONE_TO_MANY_UNHANDLED（新增）
# ----------------------------
def _bfs_connected_components(nodes_set, edges_set):
    """无向图连通分量分解（BFS）。

    nodes: set[str]  节点集合
    edges: set[tuple[str,str]]  无向边集合（以 (a,b) 和 (b,a) 两种形式都存，保证 lookup 无需再翻）
    返回：list[set[str]]  每个连通分量的节点集合；顺序稳定（按节点字典序小到大的根节点）。
    """
    nodes = sorted(nodes_set or set())
    visited = set()
    comps = []
    for root in nodes:
        if root in visited:
            continue
        # BFS
        q = [root]
        visited.add(root)
        comp = {root}
        head = 0
        while head < len(q):
            u = q[head]
            head += 1
            for v in nodes:
                if v in visited:
                    continue
                if (u, v) in edges_set or (v, u) in edges_set:
                    visited.add(v)
                    comp.add(v)
                    q.append(v)
        comps.append(comp)
    # 每个分量内部按字典序排（输出稳定）；返回 list[set]，但 detail 输出时自己再 sort
    return comps


def _ev_one_to_many_unhandled(ast_stmt, ctx):
    """一对多未处理：SQL 有 JOIN 且关系是 1:N / M:N，且有聚合函数，且无 DISTINCT/预聚合子查询。

    两梯次（M6-1 返工第二轮：梯次(2) 从"两两 pair"改为"图连通性 BFS"，消除三表链式误报）：
      (1) 真放大（不变）：MDL 里存在 joinType ∈ {ONE_TO_MANY, MANY_TO_MANY} 的关系，
          且关系两端模型都出现在 SQL 的 FROM/JOIN 表集里 → 报。
      (2) 未声明关系通路（本轮改）：N = SQL 中所有能解析到 MDL 的模型；
          以 N 为节点、MDL 中所有已声明关系（任意 joinType）为无向边
          （两端必须都在 N 里才算边），BFS 求连通分量：
            · 若 |N| < 2 → 不报
            · 若连通分量数 == 1 → 不报（已被关系连成一片，粒度可推导）
            · 若连通分量数 ≥ 2 → 报，detail 指明分裂的两组表
      (X) 删除（保持）：原梯次(3)"≥2 表就报"和原梯次两两 pair 的噪声源。
    绝不抛异常；找不到就不报。
    """
    out = []
    joins = list(ast_stmt.find_all(exp.Join))
    if not joins:
        return out
    has_agg = any(isinstance(a, exp.AggFunc) for a in ast_stmt.walk())
    if not has_agg:
        return out
    has_distinct = isinstance(ast_stmt, exp.Select) and ast_stmt.args.get("distinct") is not None
    has_preagg = False
    for f in ast_stmt.find_all(exp.From):
        for sub in f.find_all(exp.Subquery):
            inner = getattr(sub, "this", None)
            if isinstance(inner, exp.Select) and any(isinstance(a, exp.AggFunc) for a in inner.walk()):
                has_preagg = True
                break
            if isinstance(inner, exp.Union):
                has_preagg = True
                break
        if has_preagg:
            break
    if has_distinct or has_preagg:
        return out
    mdl_index = (ctx or {}).get("mdl_index")
    if mdl_index is None and ctx and ctx.get("dataset"):
        mdl_index = build_mdl_index(ctx["dataset"])
    rels = (mdl_index or {}).get("rels") or []
    all_tables = (mdl_index or {}).get("all_tables") or set()
    model_to_phys = (mdl_index or {}).get("tables") or {}  # {model_name: phys_table_name}
    # SQL 里出现的表名（含模型名/物理表名），归一后 且 确实属于 MDL 的
    sql_tables_raw = set()
    sql_tables_aliases = {}  # {sql_norm_name: sql_raw_name} 仅用于 detail 输出
    for tbl in ast_stmt.find_all(exp.Table):
        nm = tbl.name or getattr(tbl.this, "name", "") if tbl.this else ""
        alias = tbl.alias_or_name
        if nm:
            sql_tables_raw.add(_norm_sql_name(nm))
            sql_tables_aliases[_norm_sql_name(nm)] = nm or alias
        if alias and alias != nm:
            sql_tables_raw.add(_norm_sql_name(alias))
    # 归一成「MDL 内的 SQL 表归一集合（同时兼容模型名 + 物理表名归一，统一翻成模型名归一，方便 rel.models 对比）
    #   建立：{normalized_sql_name -> normalized_model_name} ；
    #   先把 model_to_phys 转反过来：phys_norm → model_norm
    phys_to_model = {}
    for mn, pn in model_to_phys.items():
        if pn:
            phys_to_model[_norm_sql_name(pn)] = _norm_sql_name(mn)
    model_norm_set = {_norm_sql_name(mn) for mn in (model_to_phys.keys() or [])}
    # SQL→MDL模型 归一映射
    sql_to_model = {}
    for n in sql_tables_raw:
        if n in model_norm_set:
            sql_to_model[n] = n
        elif n in phys_to_model:
            sql_to_model[n] = phys_to_model[n]
    # 最终 mdltbl_in_sql = set(model-normalized names)
    mdltbl_in_sql = set(sql_to_model.values())
    if len(mdltbl_in_sql) < 2:
        return out  # 跨表不足两张（或均未建模），不判
    # 真放大判定（2026-10-02 改造）：原逻辑只认字面 joinType ∈ {ONE_TO_MANY, MANY_TO_MANY}，
    # 而本库 MDL 10 条关系 joinType 全部是 MANY_TO_ONE → 该分支实为死代码。
    # 现按「SQL 里的 JOIN 方向」反解：MANY_TO_ONE 声明「models[0]=多端, models[1]=一端」，
    # 若 SQL 中「一端」出现在「多端」之前（= 从一端发起 JOIN 到多端），
    # 即构成一对多放大（一端的一行对应多端多行，聚合被放大），应报。
    hit_rel = False
    hit_snippet = None
    hit_detail = ""
    # SQL 中 MDL 模型的出现顺序（用于方向反解；sql_to_model 已在本函数上文建好）
    _seq = []
    for _t in ast_stmt.find_all(exp.Table):
        _m = sql_to_model.get(_norm_sql_name(_t.name or ""))
        if _m and _m not in _seq:
            _seq.append(_m)
    _pos = {_m: _i for _i, _m in enumerate(_seq)}
    for r in rels:
        jt = (r.get("joinType") or "").upper()
        ms = [_norm_sql_name(x) for x in (r.get("models") or [])]
        if len(ms) < 2:
            continue
        if jt == "MANY_TO_ONE":
            multi_end, one_end = ms[0], ms[1]
        elif jt == "ONE_TO_MANY":
            multi_end, one_end = ms[1], ms[0]
        elif jt == "MANY_TO_MANY":
            # 多对多：两端都可能放大，只要两端都在 SQL 里就报（保持原意）
            if ms[0] in mdltbl_in_sql and ms[1] in mdltbl_in_sql:
                hit_rel = True
                hit_snippet = "JOIN %s<->%s（joinType=%s）+ 聚合 无 DISTINCT/预聚合" % (
                    r.get("models")[0], r.get("models")[1], jt)
                hit_detail = (
                    "MDL 关系 %s 为 %s，SQL 中有聚合且未加 DISTINCT，也无 FROM 聚合子查询；"
                    "可能因多对多放大导致重复计算，请确认是否先在明细侧预聚合或去重。"
                    % (r.get("name") or ",".join(r.get("models") or []), jt)
                )
                break
            continue
        else:
            continue
        # 方向反解：一端先于多端出现 → 一对多放大
        if multi_end in _pos and one_end in _pos and _pos[one_end] < _pos[multi_end]:
            hit_rel = True
            hit_snippet = (
                "JOIN 方向反用 %s<->%s（MDL 声明 %s：一端 %s 先出现，多端 %s 后被 JOIN）+ 聚合 无 DISTINCT/预聚合"
                % (r.get("models")[0], r.get("models")[1], jt, one_end, multi_end)
            )
            hit_detail = (
                "MDL 关系 %s 声明为 %s（多端=%s，一端=%s）；SQL 从「一端 %s」发起 JOIN 到「多端 %s」，"
                "形成一对多放大，且外有聚合又未加 DISTINCT / 未做 FROM 预聚合；"
                "请确认是否先在多端侧预聚合或对主键去重，避免计数/求和被放大。"
                % (r.get("name") or ",".join(r.get("models") or []), jt,
                   multi_end, one_end, one_end, multi_end)
            )
            break
    # (2) 图连通性：N = mdltbl_in_sql；边 = 所有 MDL 已声明 relationship（任意 joinType），
    #     且边的两端节点都必须落在 N 里（否则这条边跟本句 SQL 无关）
    if not hit_rel:
        N = mdltbl_in_sql
        edges = set()
        for r in rels or []:
            ms = [_norm_sql_name(x) for x in (r.get("models") or [])]
            if len(ms) >= 2:
                a, b = ms[0], ms[1]
                if a in N and b in N:
                    edges.add((a, b))
                    edges.add((b, a))
        comps = _bfs_connected_components(N, edges)
        if len(comps) >= 2:
            # 连通分量按大小降序；同大时按字典序
            comps_sorted = sorted(comps, key=lambda c: (-len(c), sorted(c)[0]))
            groups_txt = "；".join(
                "组分 %d={%s}" % (k + 1, ", ".join(sorted(comps_sorted[k])))
                for k in range(len(comps_sorted))
            )
            bridge1 = sorted(comps_sorted[0])[0]
            bridge2 = sorted(comps_sorted[1])[0]
            hit_rel = True
            hit_snippet = "MDL 关系不连通的跨表 JOIN（%s ↔ %s）+ 聚合，粒度未知，疑似放大" % (bridge1, bridge2)
            hit_detail = (
                "SQL 同时引用 MDL 模型 %s，但它们在 MDL relationships 的已声明关系图上分裂为 %d 个连通分量（%s）；"
                "跨分量 JOIN + 聚合又未做 DISTINCT 或预聚合时，可能因跨分量粒度不对称导致放大；"
                "建议在 MDL 中补建对应关系或确认 JOIN 粒度。"
                % (", ".join(sorted(N)), len(comps_sorted), groups_txt)
            )
    if hit_rel and hit_snippet:
        out.append({
            "rule": "ONE_TO_MANY_UNHANDLED",
            "snippet": hit_snippet,
            "detail": hit_detail,
        })
    return out


# ----------------------------
# 规则 6/9：R2 LOAD_DATE_SUBSTITUTION（新增）
# ----------------------------
_LOAD_DATE_RE = None  # 延迟编译（避免 import re 放在模块顶？其实 re 是标准库，随便用）


def _ev_load_date_substitution(ast_stmt, ctx):
    """装载日期代替业务日期：WHERE/GROUP BY/ORDER BY 里出现 装载/etl/load/dw_insert_time 等列。

    扫描范围：所有 Column（不限层级，子查询里也扫）；只要出现在 WHERE/HAVING/GROUP BY/ORDER BY
    的表达式树里就算命中，SELECT 投影里单独出现不算（只投影不分组/过滤是允许引用装载日期做审计的）。
    """
    out = []
    import re as _re
    pat = _re.compile(r"(装载|etl|load|dw_insert_time|dw_update_time|dw_load_time|数据日期|ods_)",
                      _re.IGNORECASE)
    # 收集 WHERE/HAVING/GROUP BY/ORDER BY 里的列
    target_cols = []
    for clause in list(ast_stmt.find_all(exp.Where)) + list(ast_stmt.find_all(exp.Having)) \
            + list(ast_stmt.find_all(exp.Group)) + list(ast_stmt.find_all(exp.Ordered)):
        for col in clause.find_all(exp.Column):
            target_cols.append(col)
    reported = set()  # 去重：同列多处出现只报一次
    for col in target_cols:
        col_name = ""
        tbl = ""
        tbl_tok = col.args.get("table")
        if tbl_tok is not None:
            tbl = (tbl_tok.name if isinstance(tbl_tok, exp.Identifier) else str(tbl_tok)) or ""
        col_tok = col.this
        if col_tok is not None:
            col_name = (col_tok.name if isinstance(col_tok, exp.Identifier) else str(col_tok)) or ""
        full = ((tbl + ".") if tbl else "") + col_name
        key = _norm_sql_name(full)
        if key in reported:
            continue
        if not col_name and not tbl:
            continue
        check_str = col_name + " " + tbl
        if pat.search(check_str):
            reported.add(key)
            out.append({
                "rule": "LOAD_DATE_SUBSTITUTION",
                "snippet": "WHERE/GROUP BY/ORDER BY 引用装载日期类列: " + full,
                "detail": "在分组/过滤/排序中使用了装载日期字段「%s」，通常应使用业务日期字段（如业务发生日期、统计日期、订单日期），装载日期仅用于数据运维审计场景。" % full,
            })
    return out


# ----------------------------
# 规则 7/9：R3 MULTI_VALUE_NO_ORDER（新增）
# ----------------------------
def _ev_multi_value_no_order(ast_stmt, ctx):
    """多值拼接未定义顺序：STRING_AGG / GROUP_CONCAT / ARRAY_AGG 内部无 ORDER BY 子句。"""
    out = []
    aggs = list(ast_stmt.find_all(exp.ArrayAgg))  # sqlglot 里 ARRAY_AGG = ArrayAgg
    # 兼容 STRING_AGG（GROUP_CONCAT）
    for fn in list(ast_stmt.find_all(exp.Anonymous)) + list(ast_stmt.find_all(exp.AggFunc)):
        fn_name = ""
        if isinstance(fn, exp.Anonymous):
            fn_name = (getattr(fn, "this", None) or "").upper()
        elif isinstance(fn, exp.AggFunc):
            fn_name = type(fn).__name__.upper()
            # sqlglot 把 STRING_AGG 也可能解析成 GroupConcat / StringAgg（不同版本）
        if fn_name in ("STRING_AGG", "GROUPCONCAT", "GROUP_CONCAT",
                       "ARRAYAGG", "ARRAY_AGG", "ARRAY_AGG()") or \
           any(k in fn_name for k in ("STRING_AGG", "GROUP_CONCAT", "ARRAY_AGG")):
            aggs.append(fn)
    reported_ids = set()
    for fn in aggs:
        if id(fn) in reported_ids:
            continue
        # 函数参数内是否含有 ORDER BY？
        has_order = False
        for sub in fn.walk():
            if isinstance(sub, exp.Ordered):
                has_order = True
                break
            # STRING_AGG(x, ',' ORDER BY y) 的 Order 在 args 里也能找
        # 兜底再查 fn.args 里有没有 order / within_group
        if not has_order:
            for k, v in (fn.args or {}).items():
                if "order" in k.lower() or isinstance(v, exp.Order):
                    has_order = True
                    break
        if not has_order:
            snippet = fn.sql(dialect="postgres")
            reported_ids.add(id(fn))
            out.append({
                "rule": "MULTI_VALUE_NO_ORDER",
                "snippet": snippet[:160],
                "detail": "该多值拼接聚合未指定 ORDER BY，结果顺序在不同执行计划中不稳定；建议在函数内显式写 ORDER BY 子句（或 WITHIN GROUP ORDER BY）以保证可复现。",
            })
    return out


# ----------------------------
# 规则 8/9：R4 DANGLING_DENOMINATOR（新增，一期启发式——分母悬空）
# ----------------------------
def _ev_dangling_denominator(ast_stmt, ctx):
    """比率分母悬空：SELECT 侧有"率/占比/复购率/转化率/留存率"等比率类输出，
    但 SQL 里看不到任何可作为"分母"的对象引用（表/列）。

    一期简化判定：列名或别名中含 率/占比/复购率/转化率/留存率 关键词 → 视为"比率字段"。
    再看 SQL 中 FROM/JOIN 是否引用了>=2 张表，或 SQL 中是否有除法表达式（/）。
    若两者都没有（单表且无除号）→ 判为「分母悬空」。

    启发式可能漏判（如子查询里已经算好了分母再外 SELECT 直接拿），但误报概率低，
    判 warning 不阻断——先提醒，后续可精化。
    """
    out = []
    if not isinstance(ast_stmt, exp.Select):
        return out
    ratio_keywords = ["率", "占比", "复购率", "转化率", "留存率", "渗透率", "利润率", "完成率"]
    # 找 SELECT 侧比率字段
    ratio_snippets = []
    for expr in ast_stmt.expressions:
        alias_name = ""
        if isinstance(expr, exp.Alias):
            a = expr.args.get("alias")
            if isinstance(a, exp.Identifier):
                alias_name = a.name or getattr(a.this, "name", "")
            elif isinstance(a, exp.Literal) and a.is_string:
                alias_name = a.this.value or a.name or ""
        inner = expr.this if isinstance(expr, exp.Alias) else expr
        expr_sql = inner.sql(dialect="postgres")
        whole = (alias_name or "") + " " + expr_sql
        if any(k in whole for k in ratio_keywords):
            ratio_snippets.append(expr.sql(dialect="postgres")[:80])
    if not ratio_snippets:
        return out
    # 是否有多表？
    from_tables = list(ast_stmt.find_all(exp.From))
    unique_tables = set()
    for f in from_tables:
        for tbl in f.find_all(exp.Table):
            nm = tbl.name or getattr(tbl.this, "name", "") if tbl.this else ""
            if nm:
                unique_tables.add(_norm_sql_name(nm))
    joins_count = len(list(ast_stmt.find_all(exp.Join)))
    multi_source = (len(unique_tables) >= 2) or (joins_count >= 1)
    # 是否有除法？
    has_div = False
    for b in ast_stmt.walk():
        if isinstance(b, exp.Div):
            has_div = True
            break
    if multi_source or has_div:
        # 看起来有"分母可能性"，不警告
        return out
    out.append({
        "rule": "DANGLING_DENOMINATOR",
        "snippet": "比率类输出: " + "、".join(ratio_snippets[:3]),
        "detail": "SELECT 中包含比率类字段（率/占比/复购率等），但当前查询未看到多表 JOIN 或除法表达式；若分母来自外部上下文或 CTE，请忽略；否则请补充分母来源并显式写 A/B 或 COUNT(DISTINCT x)/COUNT(*) 形式。",
    })
    return out


# ----------------------------
# 规则 9/9：R5 UNMAPPED_OBJECT_REF（新增，引用未建模对象）
# ----------------------------
def _collect_cte_names(ast_stmt) -> set:
    """收集 WITH 定义的 CTE 名（R5 排除用）。

    为什么用 find_all(exp.CTE) 而不是只判顶层 exp.With？
    sqlglot 里 WITH ... SELECT ... 的顶级 AST 类型是 exp.Select，
    它的 args["with"] 是 exp.With 但自身不一定继承 exp.With。
    find_all 能递归把所有 CTE 抓出来（包括子查询里的 WITH），避免误报。
    """
    names = set()
    for cte in ast_stmt.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(_norm_sql_name(alias))
    # 兜底：如果 ast_stmt 本身是 exp.With
    if isinstance(ast_stmt, exp.With):
        for cte in ast_stmt.expressions:
            if isinstance(cte, exp.CTE):
                alias = cte.alias
                if alias:
                    names.add(_norm_sql_name(alias))
    return names


def _collect_subquery_aliases(ast_stmt) -> set:
    """收集所有派生表别名：FROM (subquery) AS foo，以及 JOIN (subquery) AS bar。

    修正（2026-10-02 误报修复）：原实现只扫 exp.From 下的 Subquery，
    漏掉了逗号连接 / JOIN 里的子查询别名（它挂在 exp.Join 上、不在 From 下）。
    改为全局扫描 exp.Subquery，保证派生表别名一个不漏。
    """
    aliases = set()
    for sub in ast_stmt.find_all(exp.Subquery):
        alias = sub.alias_or_name
        if alias:
            aliases.add(_norm_sql_name(alias))
    return aliases


def _ev_unmapped_object_ref(ast_stmt, ctx):
    """引用未建模对象：SQL 使用的表/列不在 MDL 闭集内。

    严格排除（宁可少报，不可误报）：
      - CTE 名（WITH cte AS (...)）
      - FROM 子查询别名（FROM (SELECT ...) AS t）
      - SELECT 表达式纯别名（AS x）
      - 纯 Literal / 聚合函数内部的常量
      - 列若只有"纯列名"且 MDL 中有同名异表列 → 视为合法（无法分辨就放行）。
    """
    out = []
    if not isinstance(ast_stmt, (exp.Select, exp.With)):
        return out
    dataset = (ctx or {}).get("dataset")
    mdl_index = (ctx or {}).get("mdl_index")
    if mdl_index is None and dataset:
        mdl_index = build_mdl_index(dataset)
    if not mdl_index:
        return out
    all_tables = mdl_index.get("all_tables") or set()
    all_columns = mdl_index.get("all_columns") or set()
    cte_names = _collect_cte_names(ast_stmt)
    subq_aliases = _collect_subquery_aliases(ast_stmt)
    # 再把所有 Select/Alias 别名也当"可放行"（因为 SELECT a AS 品类，GROUP BY 品类 → 品类是别名）
    select_aliases = set()
    for sel in ast_stmt.find_all(exp.Select):
        for expr in sel.expressions:
            if isinstance(expr, exp.Alias):
                a = expr.args.get("alias")
                if isinstance(a, exp.Identifier):
                    nm = a.name or getattr(a.this, "name", "")
                elif isinstance(a, exp.Literal) and a.is_string:
                    nm = a.this.value or a.name or ""
                else:
                    nm = ""
                if nm:
                    select_aliases.add(_norm_sql_name(nm))
    excluded = cte_names | subq_aliases | select_aliases
    reported = set()
    # 表级：所有 Table
    for tbl in ast_stmt.find_all(exp.Table):
        nm = tbl.name or getattr(tbl.this, "name", "") if tbl.this else ""
        alias = tbl.alias_or_name
        key = _norm_sql_name(nm)
        if not key:
            continue
        if key in excluded:
            continue
        if key not in all_tables:
            rep_key = "T:" + key
            if rep_key in reported:
                continue
            reported.add(rep_key)
            out.append({
                "rule": "UNMAPPED_OBJECT_REF",
                "snippet": "引用未建模表: " + (nm or alias or ""),
                "detail": "SQL 中引用的表/别名「%s」既不是 MDL 中的模型名/物理表名，也非 CTE 或子查询别名；若是拼写错误或需新建模型，请在 MDL 中补全。" % (nm or alias or ""),
            })
    # 列级：所有 Column（且排除 Literal / 纯别名引用）
    for col in ast_stmt.find_all(exp.Column):
        tbl = ""
        tbl_tok = col.args.get("table")
        if tbl_tok is not None:
            tbl = (tbl_tok.name if isinstance(tbl_tok, exp.Identifier) else str(tbl_tok)) or ""
        col_tok = col.this
        col_name = ""
        if col_tok is not None:
            col_name = (col_tok.name if isinstance(col_tok, exp.Identifier) else str(col_tok)) or ""
        n_tbl = _norm_sql_name(tbl)
        n_col = _norm_sql_name(col_name)
        # 排除纯别名引用（SELECT a AS 品类 → GROUP BY 品类）
        if not n_tbl and n_col and n_col in select_aliases:
            continue
        if not n_tbl and not n_col:
            continue
        # 组合候选三层
        candidates = []
        if n_tbl and n_col:
            candidates.append(n_tbl + "." + n_col)
        if n_col:
            candidates.append(n_col)
        hit_any = any(c in all_columns for c in candidates)
        # 修正（2026-10-02 误报修复）：表前缀是 CTE 名 / 子查询别名（派生表）时，
        # 该列属于派生表内部，其真实来源已在子查询自身校验过，不应在此报"未建模列"。
        # 原逻辑只对"表前缀不在 MDL 且不在 excluded"跳过，导致 `a.c`（a 是子查询别名）
        # 反而绕过 continue 走进报错分支 —— 复购率类合法查询因此被误报。
        if n_tbl and n_tbl in excluded:
            continue
        # 如果带了表名且表名不在 MDL 中 → 前面表级已经报过，列级不重复
        if n_tbl and n_tbl not in all_tables and n_tbl not in excluded:
            continue
        if not hit_any and n_col:
            ck = "C:" + (n_tbl + "." if n_tbl else "") + n_col
            if ck in reported:
                continue
            reported.add(ck)
            full = ((tbl + ".") if tbl else "") + col_name
            out.append({
                "rule": "UNMAPPED_OBJECT_REF",
                "snippet": "引用未建模列: " + full,
                "detail": "SQL 中引用的列「%s」未出现在 MDL 的列名或列中文名闭集中；若为拼写错误或需新建字段，请在 MDL 中补全。" % full,
            })
    return out


# ----------------------------
# 规则 6/6 强化：R6 TIME_FIELD_SUSPECT（原强化规则编号）
# ----------------------------
def _ev_time_field_suspect(ast_stmt, ctx):
    """疑似用错时间字段：WHERE/GROUP BY 用的时间列不在 MDL 时间候选列集合内。

    候选集合 = 列名或列中文名命中「日期/时间/date/dt/month/月份/统计日期/统计月份」。
    一期仅对"明显是时间操作的场景"报：列名里带 time/日期/时间 关键词，但不在候选集合里——
    这样不会把 region_id 这种非时间列误判。
    """
    out = []
    dataset = (ctx or {}).get("dataset")
    mdl_index = (ctx or {}).get("mdl_index")
    if mdl_index is None and dataset:
        mdl_index = build_mdl_index(dataset)
    time_cols = (mdl_index or {}).get("time_cols") or set()
    if not time_cols:
        return out
    # 收集 WHERE/HAVING/GROUP BY/ORDER BY 里的 Column
    target_cols = []
    for clause in list(ast_stmt.find_all(exp.Where)) + list(ast_stmt.find_all(exp.Having)) \
            + list(ast_stmt.find_all(exp.Group)) + list(ast_stmt.find_all(exp.Ordered)):
        for col in clause.find_all(exp.Column):
            target_cols.append(col)
    import re as _re
    time_like = _re.compile(
        r"(日期|时间|date|dt|month|月份|统计月份|统计日期|时间戳|timestamp|stat_date|stat_month|order_date|change_month|open_date|register_date|load_time|insert_time|update_time|dw_insert|dw_update)",
        _re.IGNORECASE,
    )
    reported = set()
    for col in target_cols:
        tbl = ""
        tbl_tok = col.args.get("table")
        if tbl_tok is not None:
            tbl = (tbl_tok.name if isinstance(tbl_tok, exp.Identifier) else str(tbl_tok)) or ""
        col_tok = col.this
        col_name = ""
        if col_tok is not None:
            col_name = (col_tok.name if isinstance(col_tok, exp.Identifier) else str(col_tok)) or ""
        if not col_name:
            continue
        if not time_like.search(col_name):
            continue
        n_tbl = _norm_sql_name(tbl)
        n_col = _norm_sql_name(col_name)
        cand = []
        if n_tbl and n_col:
            cand.append(n_tbl + "." + n_col)
        cand.append(n_col)
        if any(c in time_cols for c in cand):
            continue
        key = (n_tbl + "." if n_tbl else "") + n_col
        if key in reported:
            continue
        reported.add(key)
        full = ((tbl + ".") if tbl else "") + col_name
        out.append({
            "rule": "TIME_FIELD_SUSPECT",
            "snippet": "WHERE/GROUP BY/ORDER BY 用了非标准时间列: " + full,
            "detail": "「%s」看起来像时间字段（列名命中时间关键词），但不在该 dataset 的 MDL「日期/时间/date/dt/month/月份/统计日期/统计月份」候选时间列集合内；请确认是否应改为业务日期字段（如 order_date / stat_date），而非装载日期或运维字段。" % full,
        })
    return out


# ----------------------------
# 规则 11/11：R7 ENUM_VALUE_INVALID（新增，枚举值校验）
# ----------------------------
def _ev_enum_value_invalid(ast_stmt, ctx):
    """枚举值校验：对「MDL 已声明枚举列」做等值 / IN 比较时，字面量不在声明的值域内 → 报。
    仅认字符串字面量；计算表达式 / 参数化不判（宁可少报）。
    """
    out = []
    mdl_index = (ctx or {}).get("mdl_index")
    if mdl_index is None and (ctx or {}).get("dataset"):
        mdl_index = build_mdl_index(ctx["dataset"])
    if not mdl_index:
        return out
    enum_cols = mdl_index.get("enum_cols")
    if enum_cols is None:
        enum_cols = _extract_enum_cols(mdl_index.get("models") or {})
    if not enum_cols:
        return out
    reported = set()

    def _lit_val(lit):
        t = lit.this
        if isinstance(t, str):
            return t
        return getattr(t, "name", "") or ""

    def _hit(col_expr, bad_val):
        cn = _norm_sql_name(col_expr.name)
        if cn not in enum_cols or bad_val in enum_cols[cn]:
            return
        key = cn + "=" + bad_val
        if key in reported:
            return
        reported.add(key)
        out.append({
            "rule": "ENUM_VALUE_INVALID",
            "snippet": "%s = '%s'" % (col_expr.name, bad_val),
            "detail": "列「%s」的合法取值为 %s；SQL 里出现的「%s」不在其中，请核对是否写错了枚举值。"
                      % (col_expr.name, " / ".join(sorted(enum_cols[cn])), bad_val),
        })

    # 等值比较（两侧都看，防止字面量写在左边）
    for eq in ast_stmt.find_all(exp.EQ):
        for col, lit in ((eq.left, eq.right), (eq.right, eq.left)):
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal) and lit.is_string:
                _hit(col, _lit_val(lit))
    # IN (...) 列表
    for inx in ast_stmt.find_all(exp.In):
        col = inx.this
        if isinstance(col, exp.Column):
            for v in (inx.expressions or []):
                if isinstance(v, exp.Literal) and v.is_string:
                    _hit(col, _lit_val(v))
    return out


# ===========================================================================
# 四、REGISTRY + evaluate_all（顺序稳定）
# ===========================================================================
REGISTRY = [
    {
        "id": "JOIN_WITHOUT_CONDITION",
        "name": "JOIN_WITHOUT_CONDITION",
        "statement": "JOIN 必须带 ON 或 USING 连接条件，禁止隐式笛卡尔积",
        "severity": "warning",
        "blocking": True,          # 新增：门禁分级 —— 本条命中即阻断（severity 语义不变）
        "detect": "join_condition",
        "evaluate": _ev_join_without_condition,
    },
    {
        "id": "SELECT_STAR",
        "name": "SELECT_STAR",
        "statement": "禁止 SELECT *，输出列必须显式枚举",
        "severity": "warning",
        "detect": "select_star",
        "evaluate": _ev_select_star,
    },
    {
        "id": "GROUP_BY_INCONSISTENT",
        "name": "GROUP_BY_INCONSISTENT",
        "statement": "SELECT 中非聚合列必须完整出现在 GROUP BY 中（别名/序号/别名引用均已规范化处理）",
        "severity": "warning",
        "detect": "group_by_consistency",
        "evaluate": _ev_group_by_inconsistent,
    },
    {
        "id": "UNNECESSARY_DISTINCT",
        "name": "UNNECESSARY_DISTINCT",
        "statement": "DISTINCT 不应出现在单表无聚合无 JOIN 场景，也不应重复套在 UNION/聚合子查询外层",
        "severity": "warning",
        "detect": "unnecessary_distinct",
        "evaluate": _ev_unnecessary_distinct,
    },
    {
        "id": "ONE_TO_MANY_UNHANDLED",
        "name": "ONE_TO_MANY_UNHANDLED",
        "statement": "存在一对多 / 多对多 JOIN 且有聚合时，必须做 DISTINCT 或明细侧预聚合，避免放大",
        "severity": "warning",
        "blocking": True,          # 新增：跨域无关系 JOIN / 一对多放大属硬错误 → 阻断
        "detect": "one_to_many",
        "evaluate": _ev_one_to_many_unhandled,
    },
    {
        "id": "LOAD_DATE_SUBSTITUTION",
        "name": "LOAD_DATE_SUBSTITUTION",
        "statement": "WHERE / GROUP BY / ORDER BY 禁止使用装载日期（etl / load / dw_insert_time 等）代替业务日期",
        "severity": "warning",
        "detect": "load_date_substitution",
        "evaluate": _ev_load_date_substitution,
    },
    {
        "id": "MULTI_VALUE_NO_ORDER",
        "name": "MULTI_VALUE_NO_ORDER",
        "statement": "STRING_AGG / GROUP_CONCAT / ARRAY_AGG 等多值拼接必须指定 ORDER BY 保证顺序可复现",
        "severity": "warning",
        "detect": "multi_value_order",
        "evaluate": _ev_multi_value_no_order,
    },
    {
        "id": "DANGLING_DENOMINATOR",
        "name": "DANGLING_DENOMINATOR",
        "statement": "比率类输出（率/占比/复购率等）必须有明确的分母来源（多表 JOIN 或 A/B 除法表达式）",
        "severity": "warning",
        "detect": "dangling_denominator",
        "evaluate": _ev_dangling_denominator,
    },
    {
        "id": "UNMAPPED_OBJECT_REF",
        "name": "UNMAPPED_OBJECT_REF",
        "statement": "SQL 引用的表/列必须在 MDL 闭集内（排除 CTE 名、子查询别名、SELECT 别名）",
        "severity": "warning",
        "blocking": True,          # 新增：引用不存在的表/列属硬错误 → 阻断
        "detect": "unmapped_object",
        "evaluate": _ev_unmapped_object_ref,
    },
    {
        "id": "TIME_FIELD_SUSPECT",
        "name": "TIME_FIELD_SUSPECT",
        "statement": "时间过滤/分组应优先使用 MDL 标准时间候选列（统计日期/业务日期等），避免装载日期",
        "severity": "warning",
        "detect": "time_field_suspect",
        "evaluate": _ev_time_field_suspect,
    },
    {
        "id": "ENUM_VALUE_INVALID",
        "name": "ENUM_VALUE_INVALID",
        "statement": "对 MDL 已声明枚举列的等值 / IN 比较，取值必须在声明的值域内",
        "severity": "warning",
        "blocking": True,          # 枚举值错误属硬错误 → 阻断
        "detect": "enum_value",
        "evaluate": _ev_enum_value_invalid,
    },
]


def evaluate_all(ast_stmt, ctx=None) -> list:
    """顺序执行 REGISTRY 中所有规则，返回 [{rule, snippet, detail}] 合并列表。

    顺序稳定：每条规则内部的多条结果按 AST 遍历顺序，规则之间按 REGISTRY 注册顺序。
    任何单条 evaluate 抛异常 → 该规则被跳过（不影响其他规则），不抛给调用方——
    原因：gates.review 的 L2 是"只警告不阻断"，若规则挂了整个审查也不能挂；
    但会在规则结果里追加一条内部诊断（方便自查）。
    """
    out = []
    c = ctx or {"dataset": "B", "mdl_index": None, "requirement": None}
    for rule in REGISTRY:
        fn = rule.get("evaluate")
        if not callable(fn):
            continue
        try:
            res = fn(ast_stmt, c)
            if isinstance(res, list):
                out.extend(res)
        except Exception as e:
            out.append({
                "rule": "RULE_INTERNAL_ERROR",
                "snippet": "规则 %s 执行异常（已跳过）" % rule.get("id"),
                "detail": "%s: %s" % (type(e).__name__, str(e)[:200]),
            })
    return out
