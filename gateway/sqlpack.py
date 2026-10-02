# -*- coding: utf-8 -*-
"""SQL 上下文包（M4-3 · SQL生成链路设计 §4.5 – 4.6）

设计动机
--------
网关侧不跑 LLM，所以下游（sqlgen / planner）不会收到"一段自然语言描述需求"——
它拿到的是一份**确定、可回溯、可审计**的"结构化输入 + Schema 闭集候选"。
这份上下文包是 M4 和 M5 之间的唯一契约。包一旦生成，pack_version 五维哈希
（schema + requirement + rule + pack 结构）锁死版本；同一输入两次 pack_version
相同 = 生成链路可复现。

为什么候选只能在 MDL 闭集内产生
--------------------------------
硬约束：**绝不绕过 MDL 直接连物理库搜字段**。这是"敏感字段不建模"这一治理决定
在生成链路里的落地——物理存在但 MDL 未建模 = 对 AI 不可见。如果 sqlpack 偷偷去
查 information_schema，就等于给下游开了"能看到敏感列"的口子，M2 整层治理就失效了。
所以候选仅取四闭集：(1)模型名 (2)tableReference.table (3)列名 (4)列中文名。

两套字段名的显式改名（容易漏）
------------------------------
candidates() 的输出键是 subject_tables / join_paths（贴近"候选来源"语义）；
build() 的包字段是 table_candidates / join_candidates（对下游是"候选清单"）。
名字不同——build 内部显式写 table_candidates = cand["subject_tables"]
和 join_candidates = cand["join_paths"]。不要直接写 cand.get("table_candidates")，
会取到 None，下游还以为"没有候选"。

pack_version 稳定哈希
---------------------
sha256(schema_version + requirement_version + rules_version)[:8]，三要素任何一变
版本号就变：schema 变了（库表改了）、需求改了（重新 build 过 structured_requirement）、
规则集改了（R1-R7 条目或内容变了，任何变化 d(rules_version)=1）。
"""
import hashlib
import json
import inspect

import registry
import metadata as metadata_mod
import evidence as evidence_mod
import rules as rules_mod
import schema_scan as schema_scan_mod
import requirement as requirement_mod

# 缺失占位串 —— 来自 M4-2 requirement.build 里的 placeholder value；上游没分析出结论就写这个。
# 这是本模块"跳过匹配"的唯一判定依据；不能用"空字符串"，因为空串也可能是合法 value 的退化。
MISSING_MARK = "无候选结论（该槽位在本轮分析中为空）"

# 时间字段候选词（大小写不敏感）。中英文混合，覆盖 MDL 里的 date/dt/日期/时间四种写法。
TIME_WORDS = [
    "日期", "时间", "date", "dt", "month",
    "月份", "day", "日", "年", "统计月份", "统计日期",
]

# 主键候选词：列级 isPrimaryKey = true。这里只保留"命中主键列才叫 key candidate"，
# 不做"ID 后缀猜测"——Wren MDL 明确写了 isPrimaryKey，就按它来。
# filter_field_candidates 是"候选表里列/中文名含筛选词"的列：范围/状态/类型/等级
FILTER_WORDS = [
    "状态", "类型", "等级", "门店", "大区", "品类", "类目", "归属",
    "日期", "月份", "级别", "门店类型", "会员等级", "会员状态",
    "status", "type", "grade", "level", "region", "store", "category",
]

STATUS_ENUM = ("待审核", "已通过", "已退回")


# ---------------------------------------------------------------------------
# 辅助：把 MDL 模型转成索引
# ---------------------------------------------------------------------------
def _mdl_index(mdl):
    """为 MDL 建 6 份索引，避免 candidates 里每次匹配重扫。

    返回：
    - models_by_name: {model_name: model dict}
    - model_pks: {model_name: [pk_col,...]}
    - model_tables: {model_name: tableReference.table str or None}
    - model_cols: {model_name: [{name, type, label, caliber, isPrimaryKey}]}
      label = split_label_caliber 拆出的中文名；MDL 没写 description 则为 None
    - rels: mdl["relationships"] 原文（保留了 relationship_index = 列表下标）
    """
    models = mdl.get("models", []) or []
    by_name = {}
    pks = {}
    tables = {}
    cols = {}
    for m in models:
        name = m.get("name")
        by_name[name] = m
        pk_raw = m.get("primaryKey")
        pk_list = [pk_raw] if isinstance(pk_raw, str) else list(pk_raw or [])
        pks[name] = pk_list
        tables[name] = (m.get("tableReference") or {}).get("table")
        c_out = []
        for c in m.get("columns", []) or []:
            cname = c.get("name")
            desc = c.get("description")
            label, caliber = metadata_mod.split_label_caliber(desc)
            is_pk = bool(c.get("isPrimaryKey") or cname in pk_list)
            c_out.append({
                "name": cname,
                "type": c.get("type") or "text",
                "label": label,
                "caliber": caliber,
                "isPrimaryKey": is_pk,
                "_raw_type": c.get("type"),
            })
        cols[name] = c_out
    return {
        "by_name": by_name,
        "pks": pks,
        "table_refs": tables,
        "cols": cols,
        "rels": mdl.get("relationships", []) or [],
    }


def _norm(text):
    """规范化匹配：大小写不敏感、去空白、去下划线。

    这一层规范化的理由：英文表名/列名、下划线命名与中文需求用下划线切割，
    能提高命中。例如 dim_store 拆出 store 后，「门店」的英文 store 也能命中。
    """
    if text is None:
        return ""
    s = str(text).lower().replace(" ", "").replace("_", "")
    return s


def _hit_tokens(text):
    """从需求的一句话里拆出 token（中文字词 2-4 字 + 英文连续字母串）。

    为什么不直接全串匹配：需求里是「本月各门店销售额合计」这类完整句子，
    用「门店」「销售额」「合计」几个分词去撞；全串直接等于「门店」显然没命中。
    """
    import re as _re
    t = str(text or "")
    if not t:
        return []
    toks = set()
    # 2-4 字中文组合（sliding window）
    chinese = "".join(ch for ch in t if "\u4e00" <= ch <= "\u9fff")
    for length in (2, 3, 4):
        for i in range(max(0, len(chinese) - length + 1)):
            w = chinese[i:i + length]
            if w:
                toks.add(w)
    # 英文连续字母串
    for w in _re.findall(r"[A-Za-z][A-Za-z_0-9]{1,}", t):
        norm = _norm(w)
        if len(norm) >= 2:
            toks.add(norm)
    # 整条原文的 norm 也加进去——全靠滑窗会漏掉「dwd_order_di」这种带下划线的整词
    toks.add(_norm(t))
    # 原文去掉「合计 / 本月 / 近 / 各 / 排名」这类常见统计词再滑
    drop_words = {"合计", "总计", "合计", "本月", "上月", "本季", "上季", "近", "今年", "去年",
                  "各", "分别", "排名", "平均", "均值", "比率", "占比"}
    t2 = t
    for dw in drop_words:
        t2 = t2.replace(dw, "")
    chinese2 = "".join(ch for ch in t2 if "\u4e00" <= ch <= "\u9fff")
    for length in (2, 3, 4):
        for i in range(max(0, len(chinese2) - length + 1)):
            w = chinese2[i:i + length]
            if w:
                toks.add(w)
    return [tok for tok in toks if tok]


def _match_hits(inputs_raw, idx):
    """用需求侧的一组字符串，逐个模型扫四层闭集：模型名/表引用/列名/列中文名。

    inputs_raw 要先滤掉占位串（MISSING_MARK）与空值。返回：
    - subject_tables: [{table, model, match_hits, score, top_needles}]
      match_hits = [{kind, needle, target, weight}] 每条具体命中；
      score = 各条命中的「长度权重 × 判别力权重」之和，保留 1 位小数；
      top_needles = 贡献最大的最多 3 条 {needle, weight}（新增字段，用于人类快速
                    查看"为什么这张表排到 top"；不删已有键，不打乱既有字段的排序）。
    - matched_models: set of matched model names（供 join_paths 过滤）
    - hits_log: 匹配日志明细（供 miss_reason 写理由）

    为什么要加「判别力权重」（PT-1 改动 1）
    ----------------------------------------
    原实现 score = 命中条数——「门店」「销售」「订单」这种跨表高频词（df 很大）
    会把七八张表全部顶到候选池，且任何一张都不会显著领先——下游 sqlgen.plan 的
    "恰好 1 张才自动选表"逻辑永远不触发，draft_gate 恒「需人工审核」。
    改法：
      ① 第一轮先统计每个 needle 的 df = 命中它的模型（表）数；
      ② 每条命中权重 = 长度权重 × 判别力权重
            长度权重：命中的是模型名/物理表名且 needle 与 target 全等（双向包含）→ 3.0
                      其余子串命中 → float(len(needle))：2.0 / 3.0 / 4.0
            判别力权重 = 1.0 / (1.0 + 0.5 * (df - 1))
                 df=1 → 1.00,  df=2 → 0.67,  df=3 → 0.50,  df=5 → 0.33
      ③ 每张表 score = Σ 去重命中的权重；按 score 倒序；不删表（只降权不删）。
    """
    by_name = idx["by_name"]
    tables = idx["table_refs"]
    cols = idx["cols"]

    # 过滤真实输入：缺失占位 / 空字符串 / 非字符串一概视为"无输入"
    inputs = []
    for raw in inputs_raw or []:
        if raw is None:
            continue
        v = str(raw)
        if not v.strip():
            continue
        if MISSING_MARK in v:
            continue
        inputs.append(v)

    # ------------------------------------------------------------------
    # 第一轮：收集全部 (needle, model_name) 对，用于 df 统计（needle 命中过多少不同模型）
    # ------------------------------------------------------------------
    hits_log = []
    raw_by_model = {}  # model_name -> list[{kind, needle, target, is_exact_model_ref: bool}]
    for input_text in inputs:
        tokens = _hit_tokens(input_text)
        if not tokens:
            hits_log.append({"input": input_text[:60], "tokens": []})
            continue
        hits_log.append({"input": input_text[:60], "tokens": tokens})
        for model_name, model_dict in by_name.items():
            # 闭集 1/2：模型名 / 物理表名
            n_targets = [(model_name, _norm(model_name), "模型名")]
            tbl = tables.get(model_name)
            if tbl:
                n_targets.append((tbl, _norm(tbl), "物理表名"))
            for disp_name, norm_name, kind in n_targets:
                for tok in tokens:
                    if not tok:
                        continue
                    tok_in = tok in norm_name
                    name_in = norm_name in tok
                    if not (tok_in or name_in):
                        continue
                    exact = (
                        kind in ("模型名", "物理表名")
                        and len(tok) == len(norm_name)
                        and tok == norm_name
                    )
                    raw_by_model.setdefault(model_name, []).append({
                        "kind": kind,
                        "needle": tok,
                        "target": disp_name,
                        "is_exact_model_ref": bool(exact),
                    })
            # 闭集 3/4：列名 / 列中文名
            for c in cols.get(model_name, []):
                ctargets = [(c["name"], _norm(c["name"]), "列名")]
                if c["label"]:
                    ctargets.append((c["label"], _norm(c["label"]), "列中文名"))
                for disp_name, norm_name, kind in ctargets:
                    for tok in tokens:
                        if not tok:
                            continue
                        if tok in norm_name:
                            raw_by_model.setdefault(model_name, []).append({
                                "kind": kind,
                                "needle": tok,
                                "target": disp_name,
                                "is_exact_model_ref": False,
                            })

    # df[needle] = 该 needle 命中过多少不同模型
    df = {}
    needles_per_model = {}
    for m, hs in raw_by_model.items():
        seen_needles = set()
        for h in hs:
            seen_needles.add(h["needle"])
        needles_per_model[m] = seen_needles
        for n in seen_needles:
            df[n] = df.get(n, 0) + 1

    # ------------------------------------------------------------------
    # 第二轮：对每个模型按去重三元组合并权重，产出最终候选
    # ------------------------------------------------------------------
    # 去重 (kind, needle, target) 三元组：同一字段多次命中算一次
    table_out = []
    matched_models = set()
    for model_name in sorted(raw_by_model.keys()):
        raw = raw_by_model[model_name]
        dedup_rows = {}
        for h in raw:
            key = (h["kind"], h["needle"], h["target"])
            if key in dedup_rows:
                # 同一三元组保留 is_exact_model_ref=True 的那条（权重更高）
                prev = dedup_rows[key]
                if bool(h["is_exact_model_ref"]) and not bool(prev.get("is_exact_model_ref")):
                    dedup_rows[key] = dict(h)
                continue
            dedup_rows[key] = dict(h)

        if not dedup_rows:
            continue
        # 计算每条的权重、求和得分
        match_hits = []
        needle_sum = {}  # needle → 累计权重（用于 top_needles）
        score = 0.0
        for h in dedup_rows.values():
            tok = h["needle"]
            tok_len = max(1, min(4, len(tok))) if tok and "\u4e00" <= tok[0] <= "\u9fff" else len(tok or "")
            tok_len_float = float(max(2, min(4, tok_len))) if "\u4e00" in (tok or "") else float(max(2, len(tok or "")))
            # 长度权重：全等命中模型名/物理表名 → 3.0；其余子串按 len(tok) 算（2/3/4）
            if h.get("is_exact_model_ref"):
                length_w = 3.0
            else:
                length_w = float(len(tok)) if (tok and 2 <= len(tok) <= 4) else tok_len_float
                if length_w < 2.0:
                    length_w = 2.0
                if length_w > 4.0:
                    length_w = 4.0
            d = df.get(tok, 1)
            disc_w = 1.0 / (1.0 + 0.5 * (d - 1))
            weight = round(length_w * disc_w, 3)
            h_out = {
                "kind": h["kind"],
                "needle": tok,
                "target": h["target"],
                "weight": weight,
            }
            match_hits.append(h_out)
            score += weight
            needle_sum[tok] = needle_sum.get(tok, 0.0) + weight
        score = round(score, 1)
        # top_needles：needle 累计权重排序，取前 3
        tops = sorted(needle_sum.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        top_needles = [
            {"needle": n, "weight": round(w, 2)} for n, w in tops
        ]
        table_out.append({
            "table": model_name,
            "model": model_name,
            "table_reference": tables.get(model_name),
            "match_hits": match_hits,
            "score": score,
            "top_needles": top_needles,
        })
        matched_models.add(model_name)
    # 排序按 score 倒序；同分按模型名字典序（稳定可复现）
    table_out.sort(key=lambda t: (-t["score"], t["model"]))
    return table_out, matched_models, hits_log


def _collect_key_candidates(idx, matched_models):
    """key_candidates = 候选表里 isPrimaryKey=True 的列集合（列级）。

    列级（不是表级）：必须按契约里 {name, type, isPrimaryKey} 形状输出，
    携带 model 给下游定位是哪张表的主键。
    """
    out = []
    for model_name in sorted(matched_models):
        for c in idx["cols"].get(model_name, []):
            if c["isPrimaryKey"]:
                out.append({
                    "model": model_name,
                    "name": c["name"],
                    "type": c["type"],
                    "isPrimaryKey": True,
                })
    return out


def _collect_time_field_candidates(idx, matched_models):
    """time_field_candidates = 候选表里列名/列中文名 命中 TIME_WORDS 的列。

    规则不做"猜测型"扩展：只有字面命中，未命中就不进候选。
    这样下游如果出现"找不到日期字段"，它能看到候选列表确实是空的，
    不会误判成候选列表有而 sqlgen 没用。
    """
    out = []
    time_norms = [_norm(w) for w in TIME_WORDS]
    for model_name in sorted(matched_models):
        for c in idx["cols"].get(model_name, []):
            name_norm = _norm(c["name"])
            label_norm = _norm(c["label"])
            for tn in time_norms:
                if not tn:
                    continue
                hit_name = tn in name_norm
                hit_label = tn in label_norm and c["label"] is not None
                if hit_name or hit_label:
                    out.append({
                        "model": model_name,
                        "name": c["name"],
                        "label": c["label"],
                        "type": c["type"],
                        "hit_by": ("name" if hit_name else "") + (
                            "+label" if hit_label and not hit_name else ("label" if hit_label else "")
                        ),
                    })
                    break
    return out


def _collect_filter_field_candidates(idx, matched_models):
    """filter_field_candidates = 候选表里列名/列中文名 命中 FILTER_WORDS 的列。

    与 time_field_candidates 同口径：字面命中。不额外加"枚举列就进候选"——
    下游如果想用到"未命中但高价值列"，它可以去表 schema 自己取，本层只管
    "需求侧暗示了范围/状态/类型"的强信号列。
    """
    out = []
    filter_norms = [_norm(w) for w in FILTER_WORDS]
    for model_name in sorted(matched_models):
        for c in idx["cols"].get(model_name, []):
            name_norm = _norm(c["name"])
            label_norm = _norm(c["label"])
            for fn in filter_norms:
                if not fn:
                    continue
                hit_name = fn in name_norm
                hit_label = fn in label_norm and c["label"] is not None
                if hit_name or hit_label:
                    out.append({
                        "model": model_name,
                        "name": c["name"],
                        "label": c["label"],
                        "type": c["type"],
                        "hit_by": ("name" if hit_name else "") + (
                            "+label" if hit_label and not hit_name else ("label" if hit_label else "")
                        ),
                    })
                    break
    return out


def _collect_join_paths(idx, matched_models):
    """join_paths = mdl["relationships"] 中「两端模型都在候选集合」的条目，
    原样保留 relationship_index（= 列表下标），作为可回溯证据。

    下标必须保留——mdl.relationships 是有顺序的，下游想复现"哪条关系被选了"时，
    不能靠 name 去反推（name 可能重复），只能靠整数下标精确定位。
    """
    out = []
    rels = idx["rels"]
    for i, r in enumerate(rels):
        models_in_rel = list(r.get("models") or [])
        if len(models_in_rel) < 2:
            continue
        if not set(models_in_rel).issubset(matched_models):
            continue
        out.append({
            "relationship_index": i,
            "name": r.get("name"),
            "models": models_in_rel,
            "joinType": r.get("joinType"),
            "condition": r.get("condition"),
        })
    return out


# ---------------------------------------------------------------------------
# 1. candidates(dataset_key, requirement) -> dict
# ---------------------------------------------------------------------------
def candidates(dataset_key, requirement):
    """在 MDL 闭集内生成 Schema 候选（不连物理库）。

    返回形状：
    {"subject_tables":[], "join_paths":[], "key_candidates":[],
     "time_field_candidates":[], "filter_field_candidates":[], "miss_reason": str}
    """
    ds = registry.get(dataset_key)
    mdl = ds.mdl
    idx = _mdl_index(mdl)

    miss_lines = []

    # 需求侧候选文本：subject.value + 每个 output_fields[i].value（跳过缺失占位）
    # 类型守卫（健壮性）：requirement 必须是 dict。MCP 路径下恒为 requirement.get() 的 dict
    # 返回；但若被误传非 dict（如字符串），原写法 `requirement or {}` 会把它原样留下并触发
    # AttributeError。非 dict 一律视为「无输入」，走 miss_reason 分支，绝不抛异常。
    req = requirement if isinstance(requirement, dict) else {}
    subj_val = (req.get("subject") or {}).get("value") if isinstance(req.get("subject"), dict) else None
    field_vals = []
    for f in (req.get("output_fields") or []):
        if isinstance(f, dict):
            field_vals.append(f.get("value"))
    inputs_raw = [subj_val] + list(field_vals)
    cleaned = [x for x in inputs_raw if x is not None and MISSING_MARK not in str(x)]

    if subj_val is None or MISSING_MARK in str(subj_val):
        miss_lines.append("上游 subject 槽位为空（value 为缺失占位串），未参与匹配")
    if not field_vals or all(
        v is None or MISSING_MARK in str(v) for v in field_vals
    ):
        miss_lines.append("上游 output_fields 为空，未参与匹配")
    if not cleaned:
        miss_lines.append(
            "需求侧无任何有效候选词输入（subject + output_fields 全为缺失占位）"
        )

    # 匹配主体表
    subj_tables, matched_models, hits_log = _match_hits(cleaned, idx)

    # 描述覆盖率统计（写入 miss_reason 方便 A 库场景解释）
    total_cols = sum(len(cs) for cs in idx["cols"].values())
    labeled_cols = sum(1 for cs in idx["cols"].values() for c in cs if c["label"])
    coverage = (labeled_cols / total_cols * 100.0) if total_cols else 0.0

    if not subj_tables:
        miss_lines.append(
            "在 MDL 闭集（模型名/物理表名/列名/列中文名）内未匹配到任何主体表。"
            "候选词 token=%s；匹配日志=%s；MDL 列中文名覆盖率=%.1f%% (%d/%d)。"
            % (
                sorted(set(tok for h in hits_log for tok in h.get("tokens", [])))[:40],
                hits_log[:6],
                coverage, labeled_cols, total_cols,
            )
        )
    else:
        # 即便主体表命中，也记录覆盖率备查（A 库场景下即便命中 1 张表，
        # 列级中文名也稀缺——这是预期现象，写进 miss_reason 不写在 error 里）
        if coverage < 50.0:
            miss_lines.append(
                "MDL 列中文名覆盖率=%.1f%%（%d/%d），低于 50%%；列级候选主要靠英文列名命中。"
                % (coverage, labeled_cols, total_cols)
            )

    join_paths = _collect_join_paths(idx, matched_models)
    keys = _collect_key_candidates(idx, matched_models)
    time_cols = _collect_time_field_candidates(idx, matched_models)
    filter_cols = _collect_filter_field_candidates(idx, matched_models)

    return {
        "subject_tables": subj_tables,
        "join_paths": join_paths,
        "key_candidates": keys,
        "time_field_candidates": time_cols,
        "filter_field_candidates": filter_cols,
        "miss_reason": " ｜ ".join(miss_lines),
        "_coverage_pct": round(coverage, 1),
    }


# ---------------------------------------------------------------------------
# 2. build(demand_id, dataset="B") -> dict
# ---------------------------------------------------------------------------
def _sha8(*parts):
    """sha256(parts 按序拼接) 前 8 位十六进制。"""
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p if p is not None else "").encode("utf-8"))
    return h.hexdigest()[:8]


def _rules_version():
    """evidence.RULES 治理规则 + rules.REGISTRY 静态规则（元数据 + 实现源码指纹）的联合哈希。

    为什么把 evaluate 函数的源码指纹也纳入哈希？
    M6-1 返工暴露了一个缺陷：只哈希 id/name/severity/statement，但 R1/R6 的实现逻辑修复时，
    **口径明显变了**（比如删了梯次(3)这个噪声源），而 4 元组没变 → pack_version 没变
    就等于"审查口径变了但版本号没变"，违反"口径一变、版本必动"的溯源要求。
    所以每条规则再加 `source_sha8 = sha256(inspect.getsource(evaluate))[:8]`，
    只要 evaluate 里改了一个字符，source_sha8 就变 → rules_version 变 → pack_version 变。

    稳定性：
    - evidence.RULES 走 json.dumps(sort_keys=True, ensure_ascii=False)
    - REGISTRY 的 digest 每条规则 5 元组（id, name, severity, statement, source_sha8）
      按 REGISTRY 原生顺序 + sort_keys=True dumps；
    - 最终统一用 sort_keys=True + ensure_ascii=False 序列化；
    - 返回值仍是 8 位十六进制 sha256。
    """
    registry_digest = []
    for r in rules_mod.REGISTRY:
        fn = r.get("evaluate")
        src_sha = "00000000"
        if callable(fn):
            try:
                src = inspect.getsource(fn)
                src_sha = hashlib.sha256(src.encode("utf-8")).hexdigest()[:8]
            except (OSError, TypeError):
                # inspect.getsource 失败（比如 pyc 无源码环境）→ 兜底 0
                src_sha = "00000000"
        registry_digest.append({
            "id": r["id"],
            "name": r["name"],
            "severity": r["severity"],
            "statement": r["statement"],
            "source_sha8": src_sha,
        })
    blob = json.dumps(
        [evidence_mod.RULES, registry_digest],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def _rule_constraints_from_req(requirement):
    """从 structured_requirement 抽取规则约束：residual_risks 里带 rule_id 的项。

    shape = [{rule_id, statement, severity, value}]。下游 SQL 生成链路用它做
    R1-R7 的"阻断提示"——M5 gates 做真检测，这里只做结构化带过。
    """
    rules_idx = {r["id"]: r for r in evidence_mod.RULES}
    out = []
    for risk in (requirement or {}).get("residual_risks") or []:
        if not isinstance(risk, dict):
            continue
        rid = risk.get("rule_id")
        if not rid:
            continue
        rule = rules_idx.get(rid) or {}
        out.append({
            "rule_id": rid,
            "statement": rule.get("statement") or risk.get("value"),
            "severity": rule.get("severity") or risk.get("severity") or "warning",
            "value": risk.get("value"),
        })
    # 去重（同一 rule_id 不重复入约束列表）
    seen_ids = set()
    dedup = []
    for item in out:
        if item["rule_id"] in seen_ids:
            continue
        seen_ids.add(item["rule_id"])
        dedup.append(item)
    return dedup


def _agg_constraints_from_req(requirement):
    """从 requirement.aggregation_rules 直接拿（build 里已是派生好的结构）。

    若结构化对象没写 aggregation_rules，就从 output_fields 再派生一次——
    防止 requirement 是旧版本（当时派生逻辑没跑）时这一项为 None。
    """
    req = requirement or {}
    existing = req.get("aggregation_rules")
    if isinstance(existing, list) and existing:
        return list(existing)
    # 兜底：从 output_fields.value 关键词派生（与 M4-2 同源逻辑）
    import requirement as rmod
    return rmod._derive_aggregation(req.get("output_fields") or [])


def build(demand_id, dataset="B"):
    """组装 SQL 上下文包（SQL生成链路设计 §4.6 字段 + 五维溯源 + pack_version）。

    成功：返回包对象（顶层无 ok；契约后续会 validate）。
    失败：{"ok": False, "error": "..."}
    """
    # 1. 取结构化需求对象
    req = requirement_mod.get(demand_id)
    if isinstance(req, dict) and req.get("ok") is False:
        # get 返回的是标准失败包装
        return {"ok": False, "error": "取不到结构化需求：%s" % req.get("error", "未知错误")}
    if not isinstance(req, dict):
        return {"ok": False, "error": "结构化需求对象类型异常：%s" % type(req).__name__}

    # 2. 取 schema_version（取不到显式报错，不编）
    lv = schema_scan_mod.latest_version(dataset)
    if lv is None or not lv.get("schema_version"):
        return {
            "ok": False,
            "error": "数据集 %s 尚无 schema 快照，请先执行 schema_scan.scan" % dataset,
        }
    schema_version = lv["schema_version"]
    requirement_version = req.get("version")
    if not isinstance(requirement_version, int) or requirement_version < 1:
        return {"ok": False, "error": "结构化需求 version 非法：%r" % (requirement_version,)}

    # 3. candidates() 产出 6 键
    cand = candidates(dataset, req)
    # 两套名显式改名（见模块级 docstring 说明：两套字段名不同，需改名）
    table_candidates = cand["subject_tables"]
    join_candidates = cand["join_paths"]

    # 4. 组装 §4.6 字段
    #  4a. 定义类：主体/颗粒度/输出字段（直接从结构化需求取 value）
    subj_claim = req.get("subject") or {}
    gran_claim = req.get("granularity") or {}
    subject_definition = {
        "value": subj_claim.get("value"),
        "confidence": subj_claim.get("confidence", 0.0),
        "evidence": subj_claim.get("evidence") or [],
    }
    granularity_definition = {
        "value": gran_claim.get("value"),
        "confidence": gran_claim.get("confidence", 0.0),
        "evidence": gran_claim.get("evidence") or [],
    }
    required_fields = []
    for f in (req.get("output_fields") or []):
        if not isinstance(f, dict):
            continue
        required_fields.append({
            "value": f.get("value"),
            "confidence": f.get("confidence", 0.0),
            "evidence": f.get("evidence") or [],
        })

    #  4b. 时间约束：取 time_semantics.value + time_field_candidates（结构化对象槽位 + 候选列）
    ts = req.get("time_semantics") or {}
    time_constraints = {
        "semantic_value": ts.get("value"),
        "confidence": ts.get("confidence", 0.0),
        "evidence": ts.get("evidence") or [],
        "candidate_columns": cand["time_field_candidates"],
    }
    #  4c. 过滤约束：取 data_scope.value + filter_field_candidates
    ds2 = req.get("data_scope") or {}
    filter_constraints = {
        "scope_value": ds2.get("value"),
        "confidence": ds2.get("confidence", 0.0),
        "evidence": ds2.get("evidence") or [],
        "candidate_columns": cand["filter_field_candidates"],
    }
    #  4d. 聚合约束：aggregation_rules（结构化对象或派生兜底）
    aggregation_constraints = _agg_constraints_from_req(req)
    #  4e. SQL 模板：一期固定空数组（planner.py 没有可枚举的意图表）
    sql_templates = []
    #  4f. 规则约束：residual_risks 里带 rule_id 的条目（R1-R7 命中提示）
    rule_constraints = _rule_constraints_from_req(req)
    #  4g. dialect 固定 postgres
    dialect = "postgres"

    # 5. 溯源 & pack_version
    rules_version = _rules_version()
    pack_version = _sha8(schema_version, str(requirement_version), rules_version)

    package = {
        "demand_id": demand_id,
        "dataset": dataset,
        "schema_version": schema_version,
        "requirement_version": requirement_version,
        "pack_version": pack_version,
        "rules_version": rules_version,
        "subject_definition": subject_definition,
        "granularity_definition": granularity_definition,
        "required_fields": required_fields,
        "table_candidates": table_candidates,
        "join_candidates": join_candidates,
        "key_candidates": cand["key_candidates"],
        "time_constraints": time_constraints,
        "filter_constraints": filter_constraints,
        "aggregation_constraints": aggregation_constraints,
        "sql_templates": sql_templates,
        "rule_constraints": rule_constraints,
        "dialect": dialect,
        "_debug": {
            "miss_reason": cand["miss_reason"],
            "mdl_coverage_pct": cand.get("_coverage_pct"),
        },
    }
    return package
