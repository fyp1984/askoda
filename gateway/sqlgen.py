# -*- coding: utf-8 -*-
"""SQL 两段式生成（M5-2 · SQL生成链路设计 §4.7 – §4.9）

两段式设计的目的
----------------
一段 plan()：只做"选什么对象"的闭集决策，**不产出 SQL 文本**。未命中唯一解时标
「需人工审核」——分析师一眼能看出"要选哪张表、哪条关联路径"，不会被一段看起来
"很像对的 SQL"带着跑。

二段 generate()：拿到候选 SQL（来自客户端 Agent 或确定性 planner 兜底）产出真正
的 SQL 初稿，立即交 gates.review() 做五层门禁审查。

为什么 generate() 里允许传 candidate_sql
-----------------------------------------
硬约束里「网关侧不调用任何 LLM」——自由生成能力放在客户端 Agent，它会把
candidate_sql 作为参数回传给网关；网关只做"收+审+存"。客户端 Agent 挂掉时，
网关不会断链——此时 candidate_sql=None，回落 planner.plan(ds, nl) 确定性兜底。
两条路径的产出都要立刻过 gates.review()，保证"不管 SQL 从哪来，网关只放行同一套门禁"。
"""
import db
import registry
import sqlpack as sqlpack_mod
import planner as planner_mod
import gates as gates_mod
import requirement as requirement_mod
import fallback as fallback_mod

MISSING_MARK = "无候选结论（该槽位在本轮分析中为空）"
DRAFT_PASS = "通过"
DRAFT_HUMAN = "需人工审核"


def _pack_safe(demand_id, dataset):
    """try sqlpack.build；失败就返回 None 与错误串，不让 plan() 因为上下文包拿不到就崩。

    A 库场景下 structured_requirements 对象可能取不到（subject 缺失），sqlpack.build
    会返回 {"ok": False, ...}。plan() 此时必须正常返回草稿，draft_gate 标「需人工审核」，
    **绝不抛异常**（硬约束④）。
    """
    try:
        pack = sqlpack_mod.build(demand_id, dataset=dataset)
    except Exception as e:  # noqa: BLE001
        return None, "sqlpack.build 异常：%s: %s" % (type(e).__name__, str(e)[:200])
    if isinstance(pack, dict) and pack.get("ok") is False:
        return None, "sqlpack.build 失败：%s" % pack.get("error", "")
    if not isinstance(pack, dict):
        return None, "sqlpack.build 返回非对象：%s" % type(pack).__name__
    return pack, ""


def _requirement_safe(demand_id):
    """get requirement；失败返回 None（供 fallback nl 拼凑使用）。"""
    try:
        req = requirement_mod.get(demand_id)
    except Exception:
        return None
    if isinstance(req, dict) and req.get("ok") is False:
        return None
    if not isinstance(req, dict):
        return None
    return req


def _demand_title_desc(demand_id):
    """从 demand_requests 取 title+description，供 planner 兜底用的 nl 构造。

    查不到时返回 ("", "")——planner 会退到意图匹配失败的 _refuse，不会崩。
    不直接 import demand，避免循环 import。走 db.query。
    """
    try:
        row = db.query_one(
            "SELECT title, description FROM demand_requests WHERE demand_id=%s LIMIT 1",
            (demand_id,),
        )
    except Exception:
        return "", ""
    if not row:
        return "", ""
    return (row.get("title") or "", row.get("description") or "")


# ---------------------------------------------------------------------------
# 第一段：plan · 出计划草稿（不含 SQL 正文）
# ---------------------------------------------------------------------------
def plan(demand_id, dataset="B"):
    """出计划草稿（§4.7 · 一段式）：选表/选关联路径/选时间字段。

    draft_gate 是**闭集自检**：选定表 ∈ table_candidates、关联路径 ∈ join_candidates。
    五层门禁（L1-L5）此时还没有 SQL 文本，所以**不调用 gates.review**。
    """
    pack, pack_err = _pack_safe(demand_id, dataset)

    draft_gate_status = DRAFT_HUMAN
    draft_gate_detail_chunks = []
    if pack_err:
        # A 库/上下文包失败：其余字段尽量填，**不抛异常**（硬约束④）
        cover_line = ""
        if dataset.upper() == "A":
            cover_line = "（A 库字段 MDL 中文描述覆盖仅 6.9%，业务口吻难以命中；建议客户端 Agent 走英文意图或人工指定表）"
        draft_gate_detail_chunks.append(
            "上下文包不可用：%s%s" % (pack_err, cover_line)
        )
        pack_version = ""
    else:
        pack_version = pack.get("pack_version") or ""
        if pack.get("_debug"):
            cov = pack["_debug"].get("mdl_coverage_pct")
            mr = pack["_debug"].get("miss_reason") or ""
            if cov is not None and cov < 50.0:
                draft_gate_detail_chunks.append(
                    "MDL 列中文名覆盖率较低：%.1f%%，miss_reason=%s" % (cov, mr[:120])
                )

    # 取上下文包中的关键闭集容器；pack 为 None 时用空容器
    table_candidates = [] if not pack else list(pack.get("table_candidates") or [])
    # 【为什么先排序再选 top1/top2：上游 sqlpack.candidates 虽然自身已按 score 倒序，但 pack.build 透传、
    # 或假注入的 table_candidates 原始顺序可能打乱（如自证假 pack 3 张按 dim_store/dws/dwd 原序）。
    # 这里重排确保 top1/top2 必为 score 最高两表，避免 s1=3.0、s2=9.0 这种倒置导致唯一解判定漏触发。
    # 排序规则：score 大 → 小；同分按 table 名字典序，保证确定性。
    table_candidates.sort(
        key=lambda t: (-float(t.get("score", 0) or 0), (t.get("table") or t.get("model") or ""))
    )
    join_candidates = [] if not pack else list(pack.get("join_candidates") or [])
    _raw_time_candidates = [] if not pack else list(
        (pack.get("time_constraints") or {}).get("candidate_columns") or []
    )
    # P-B · 最终 time_field 强判据：先过一遍 is_eligible_time_field 再选
    import metadata as _md_sg  # noqa: E402
    time_candidates = []
    time_candidates_filtered = []
    for _tc in _raw_time_candidates:
        _model = _tc.get("model") or ""
        _name = _tc.get("name") or ""
        _loc = "%s.%s" % (_model, _name) if _model and _name else _name
        _label = _tc.get("label") or _name
        _type = _tc.get("type") or ""
        _eligible, _why = _md_sg.is_eligible_time_field(
            _loc, dataset=dataset, label_hint=_label, data_type_hint=_type
        )
        if _eligible:
            time_candidates.append({**_tc, "_time_eligibility": _why})
        else:
            time_candidates_filtered.append(
                {"loc": _loc, "label": _label, "type": _type, "why": _why}
            )
    requirement_version = None if not pack else pack.get("requirement_version")
    schema_version = None if not pack else pack.get("schema_version")
    agg_constraints = [] if not pack else list(pack.get("aggregation_constraints") or [])
    subj_def = {} if not pack else (pack.get("subject_definition") or {})
    gran_def = {} if not pack else (pack.get("granularity_definition") or {})
    req_fields = [] if not pack else list(pack.get("required_fields") or [])

    # 1. chosen_table：
    #    · 0 张  → 维持现状（需人工）；
    #    · 1 张  → 维持现状（自动选中）；
    #    · ≥2 张 → s1 >= 4.0 且 s1 >= 1.5 * s2 时自动选中 top1，否则列前 5 张 + 需人工。
    #    阈值 4.0 / 1.5 的取值依据见 PT-1 自证：实测「门店销售订单+销售额+订单数」下，
    #    dws_store_daily_agg(≥9) / ads_member_repurchase_di(≈4) 的倍率刚好 >1.5，
    #    而接近分布 5/4.2/3.5 的倍率 <1.5 不收敛——既不丢收敛，也不硬凑。
    chosen_table = ""
    chosen_table_reason = ""
    if len(table_candidates) == 0:
        draft_gate_detail_chunks.append("上下文包 table_candidates 为空：主题表闭集未命中任何 MDL 对象")
    elif len(table_candidates) == 1:
        chosen_table = table_candidates[0].get("table") or table_candidates[0].get("model") or ""
        hits = table_candidates[0].get("match_hits") or []
        chosen_table_reason = "table_candidates 仅 1 张（score=%.1f，首命中：%s）" % (
            float(table_candidates[0].get("score", 0) or 0),
            ("%s 命中 '%s' 在 %s" % (hits[0]["kind"], hits[0]["needle"], hits[0]["target"])) if hits else "no hits"
        )
    else:
        s1 = float(table_candidates[0].get("score", 0) or 0)
        s2 = float(table_candidates[1].get("score", 0) or 0) if len(table_candidates) >= 2 else 0.0
        ratio = (s1 / s2) if s2 > 0 else float("inf")
        if s1 >= 4.0 and s2 > 0 and s1 >= 1.5 * s2:
            t1 = table_candidates[0]
            t2 = table_candidates[1]
            chosen_table = t1.get("table") or t1.get("model") or ""
            chosen_table_reason = (
                "候选 %d 张，top1=%s(分=%.1f) 领先 top2=%s(分=%.1f) 达 %.2f 倍（阈值 s1>=4.0 且 >=1.5*s2）→ 判定唯一解"
                % (len(table_candidates),
                   chosen_table, s1,
                   t2.get("table") or t2.get("model") or "?", s2,
                   ratio)
            )
        else:
            top5 = [
                "%s(score=%.1f)" % ((t.get("table") or t.get("model") or "?"),
                                     float(t.get("score", 0) or 0))
                for t in table_candidates[:5]
            ]
            draft_gate_detail_chunks.append(
                "table_candidates=%d 张，s1=%.1f s2=%.1f ratio=%.2f 未达唯一解阈值（s1>=4.0 且 s1>=1.5*s2）：%s"
                % (len(table_candidates), s1, s2, ratio, "、".join(top5))
            )

    # 2. granularity：从 requirement / pack 结构化对象拿 gran_def.value
    granularity = gran_def.get("value")
    if granularity and MISSING_MARK not in str(granularity):
        granularity_reason = "来源：结构化需求 granularity_definition.value (confidence=%s)" % (
            gran_def.get("confidence", 0.0),
        )
    else:
        granularity = "" if granularity is None else granularity
        granularity_reason = "结构化需求颗粒度槽位为空或缺失占位"
        draft_gate_detail_chunks.append("颗粒度槽位无法确定（需人工确认）")

    # 3. join_path：join_candidates 中两端至少一端包含 chosen_table 的条目
    join_path = []
    join_path_reason = "join_candidates=%d 条" % len(join_candidates)
    if chosen_table and join_candidates:
        picked = [j for j in join_candidates if chosen_table in (j.get("models") or [])]
        if len(picked) == 0:
            join_path_reason = "join_candidates 中无任何一端是 chosen_table=%s" % chosen_table
        elif len(picked) == 1:
            j = picked[0]
            join_path = [{
                "relationship_index": j.get("relationship_index"),
                "name": j.get("name"),
                "models": j.get("models"),
                "joinType": j.get("joinType"),
                "condition": j.get("condition"),
            }]
            join_path_reason = (
                "join_candidates 仅 1 条路径包含 chosen_table=%s (relationship_index=%s, %s)"
                % (chosen_table, j.get("relationship_index"), "→".join(j.get("models") or []))
            )
        else:
            # 多条 → 不硬选，整段兜底
            join_path = [
                {
                    "relationship_index": j.get("relationship_index"),
                    "name": j.get("name"),
                    "models": j.get("models"),
                    "joinType": j.get("joinType"),
                    "condition": j.get("condition"),
                }
                for j in picked[:5]
            ]
            join_path_reason = "chosen_table=%s 可走 %d 条关联路径，需人工确认（未硬选）" % (chosen_table, len(picked))
            draft_gate_detail_chunks.append(
                "关联路径不唯一：%d 条路径命中 chosen_table" % len(picked)
            )
    elif not join_candidates:
        join_path_reason = "上下文包 join_candidates 为空"
        if chosen_table:
            draft_gate_detail_chunks.append(
                "chosen_table=%s 但 join_candidates 为空——可能为单表查询，需人工确认" % chosen_table
            )
    elif not chosen_table:
        join_path_reason = "chosen_table 未确定，跳过关联路径选择"

    # P1-13 · time_field 必须落在"本次需求相关的表"里
    # ------------------------------------------------
    # 旧实现在主题表没敛到唯一解时退化成「按候选顺序取首项」，实测把
    # 「会员复购率月度分析」的时间字段选到了 ads_coupon_order_di.stat_date
    # （券订单表，与会员复购主题无关）。
    # 修法：候选先收缩到相关表（chosen_table 优先，否则取得分最高的候选表）；
    # 相关表都定不下来就**留空**并在 reason 里写清楚，绝不跨业务域取列。
    _scope_models = []
    if chosen_table:
        _scope_models = [chosen_table]
    elif table_candidates:
        _m = table_candidates[0].get("table") or table_candidates[0].get("model")
        if _m:
            _scope_models.append(_m)
    time_field_scope_models = _scope_models
    if _scope_models:
        _out_scope = [c for c in time_candidates if (c.get("model") or "") not in _scope_models]
        if _out_scope:
            time_candidates = [c for c in time_candidates if (c.get("model") or "") in _scope_models]
            draft_gate_detail_chunks.append(
                "P1-13 · 时间字段候选限定在本次相关表（%s）内，跨表列 %d 条已剔除：%s"
                % ("、".join(_scope_models), len(_out_scope),
                   "、".join("%s.%s" % (c.get("model"), c.get("name")) for c in _out_scope[:5]))
            )
    else:
        draft_gate_detail_chunks.append(
            "P1-13 · 未确定本次相关表，跳过时间字段选取（不跨业务域猜列）"
        )

    # 4. time_field：time_constraints.candidate_columns 里选 1 条
    time_field = ""
    time_field_reason = "time_candidates=%d 列（前置过滤剔除 %d 列：%s）" % (
        len(time_candidates), len(time_candidates_filtered),
        "；".join("%s(%s)" % (x["loc"], x["why"][:40]) for x in time_candidates_filtered[:3]) or "无"
    )
    if time_candidates_filtered:
        draft_gate_detail_chunks.append(
            "P-B · 时间字段硬判据前置过滤剔除 %d 列：%s"
            % (len(time_candidates_filtered),
               "；".join("%s->%s" % (x["loc"], x["why"]) for x in time_candidates_filtered[:5]))
        )
    if len(time_candidates) == 0:
        if time_field_scope_models:
            draft_gate_detail_chunks.append(
                "时间字段候选为空：相关表 %s 内无通过类型/语义硬判据的列"
                % "、".join(time_field_scope_models)
            )
        else:
            draft_gate_detail_chunks.append(
                "时间字段待人工确认：本次未确定相关表，不跨业务域猜时间列"
            )
    elif len(time_candidates) == 1:
        c = time_candidates[0]
        time_field = "%s.%s" % (c.get("model"), c.get("name"))
        time_field_reason = "唯一命中：%s.%s（中文名=%s，hit_by=%s，eligibility=%s）" % (
            c.get("model"), c.get("name"), c.get("label"), c.get("hit_by"),
            c.get("_time_eligibility") or "",
        )
    else:
        # 多列：优先选列级名字直接含 date/dt/日期/时间 的第一列，列但标需人工
        c0 = time_candidates[0]
        time_field = "%s.%s" % (c0.get("model"), c0.get("name"))
        time_field_reason = "time_candidates=%d 列，取首项（需人工确认）：%s.%s（eligibility=%s）" % (
            len(time_candidates), c0.get("model"), c0.get("name"),
            c0.get("_time_eligibility") or "",
        )
        draft_gate_detail_chunks.append(
            "时间字段不唯一：候选列 %s" % "、".join(
                "%s.%s[%s]" % (c.get("model"), c.get("name"), c.get("_time_eligibility") or "")
                for c in time_candidates[:5]
            )
        )

    # 5. aggregate_fields：aggregation_constraints 全量（已派生好）
    aggregate_fields = [
        {
            "rule": a.get("rule"),
            "field": a.get("field"),
            "matched_keyword": a.get("matched_keyword"),
        }
        for a in (agg_constraints or [])
    ][:20]

    # 6. field_mapping：required_fields 各条 value 对候选表列的字面匹配
    field_mapping = []
    tables_in_scope = set()
    if chosen_table:
        tables_in_scope.add(chosen_table)
    for j in join_path or []:
        for m in (j.get("models") or []):
            tables_in_scope.add(m)
    # 从 registry 拿对应表列，不绕过 MDL
    cols_by_model = {}
    try:
        ds = registry.get(dataset)
        for m in ds.mdl.get("models", []) or []:
            mname = m.get("name")
            if mname not in tables_in_scope:
                continue
            cols = []
            for c in (m.get("columns") or []):
                import metadata as _md  # noqa: E402
                label, _cal = _md.split_label_caliber(c.get("description"))
                cols.append({
                    "name": c.get("name"),
                    "label": label,
                    "type": c.get("type"),
                })
            cols_by_model[mname] = cols
    except Exception:
        pass

    for i, f in enumerate(req_fields or []):
        vstr = "" if not isinstance(f, dict) else str(f.get("value") or "")
        hit_models = []
        for mname, cols in cols_by_model.items():
            for c in cols:
                n = (c.get("name") or "").lower()
                lab = (c.get("label") or "").lower()
                vv = vstr.lower()
                if vv and (vv in n or vv in lab or (n and n in vv) or (lab and lab in vv)):
                    hit_models.append({"model": mname, "name": c["name"], "label": c["label"]})
                    break
        field_mapping.append({
            "index": i,
            "required_value": vstr,
            "suggested_column": ("%s.%s" % (hit_models[0]["model"], hit_models[0]["name"])) if hit_models else None,
            "all_candidates": hit_models[:3],
            "needs_confirmation": len(hit_models) != 1,
        })
        if len(hit_models) != 1:
            draft_gate_detail_chunks.append(
                "required_fields[%d]='%s' 列映射候选=%d 个，需人工确认" % (i, vstr[:16], len(hit_models))
            )

    # 最终 draft_gate.status：有任何需要人工的 chunk → 需人工审核
    if not draft_gate_detail_chunks:
        draft_gate_status = DRAFT_PASS
        draft_gate_detail = "单一解：chosen_table=%s；granularity=%s；join_path 唯一；time_field=%s" % (
            chosen_table or "未选",
            granularity or "未选",
            time_field or "未选",
        )
    else:
        draft_gate_status = DRAFT_HUMAN
        draft_gate_detail = " ｜ ".join(draft_gate_detail_chunks)

    key_candidates = [] if not pack else list(pack.get("key_candidates") or [])
    out = {
        "demand_id": demand_id,
        "dataset": dataset,
        "pack_version": pack_version,
        "requirement_version": requirement_version,
        "schema_version": schema_version,
        "chosen_table": chosen_table,
        "chosen_table_reason": chosen_table_reason,
        "granularity": granularity,
        "granularity_reason": granularity_reason,
        "join_path": join_path,
        "join_path_reason": join_path_reason,
        "time_field": time_field,
        "time_field_reason": time_field_reason,
        "time_candidates": time_candidates,
        "time_candidates_filtered": time_candidates_filtered,
        "time_field_scope_models": time_field_scope_models,
        "aggregate_fields": aggregate_fields,
        "field_mapping": field_mapping,
        "draft_gate": {
            "status": draft_gate_status,
            "detail": draft_gate_detail,
        },
        "table_candidates": table_candidates,
        "key_candidates": key_candidates,
    }

    # P-E · SQL 生成阶段取证检索落 knowledge_citations（覆盖"SQL 这一时期的实际取证动作"）
    # 与 analysis 阶段同契约：AUTO_INTERNAL 开头作审计标记，不伪装用户可见引用
    _rq_title, _rq_desc = _demand_title_desc(demand_id)
    if demand_id and (chosen_table or str(_rq_title or "").strip() or str(_rq_desc or "").strip()):
        try:
            import knowledge as _k_sg  # noqa: E402
            if hasattr(_k_sg, "search") and hasattr(_k_sg, "record_citations_once"):
                q_sg = "SQL_PLAN 选表=%s 时间=%s 颗粒度=%s 需求=%s" % (
                    chosen_table or "",
                    time_field or "",
                    granularity or "",
                    (str(_rq_title) + " " + str(_rq_desc))[:100].strip() or "",
                )
                ks_sg = _k_sg.search(q_sg[:200], top_k=4)
                cits_sg = ks_sg.get("citations") if isinstance(ks_sg, dict) else []
                if cits_sg:
                    # P1-15 · 幂等键：本次 plan 的关键结论 + 命中引用 id 集合
                    _fp_ids = "|".join(
                        sorted(
                            str(c.get("chunk_id") or c.get("document_id") or idx)
                            for idx, c in enumerate(cits_sg)
                        )
                    )
                    _fp_sg = "ct=%s;tf=%s;gr=%s;mods=%s;cits=%s" % (
                        chosen_table or "", time_field or "", granularity or "",
                        ",".join(time_field_scope_models or []) or "", _fp_ids,
                    )
                    rc_res = _k_sg.record_citations_once(
                        demand_id,
                        q_sg[:140],
                        cits_sg,
                        stage="sqlgen_plan_stage",
                        actor="sqlgen_plan",
                        fingerprint=_fp_sg,
                        round_no=None,
                        sql_run_id=None,
                        dataset_id="sqlgen_plan_stage",
                    )
                    if isinstance(rc_res, dict):
                        out["_sql_plan_citations_written"] = rc_res.get("inserted", 0)
                        out["_sql_plan_citations_skipped"] = rc_res.get("skipped")
        except Exception:  # noqa: BLE001
            out["_sql_plan_citations_write_error"] = True

    return out


# ---------------------------------------------------------------------------
# 第二段：generate · 产出 SQL 初稿并立即审查
# ---------------------------------------------------------------------------
def generate(candidate_sql=None, demand_id=None, dataset="B", sql_plan=None):
    """生成 SQL 初稿（§4.8 二段式）。立即过 gates.review（L5 此时 skipped，正常）。

    M6-2 F3 增强：candidate_sql 支持 list[str]（多候选）。
      · 多候选：调 fallback.f3_candidate_diff(...)。若 hold=True →
        generated_sql 置空、保留 draft_gate/sql_plan 供人工选定（不下发 SQL），
        并追加键 hold、hold_reason、candidate_diff。
      · 不 hold → 选第一条候选作为 generated_sql，附 candidate_diff，
        不附 hold/hold_reason（避免下游读取混淆）。
      · candidate_sql 为 str（单候选）或 None → 行为与 M5-3 完全一致：
        输出键名、取值形状一个都不许变（保证 sqlgen_selftest 8/8 + m5_verify 62/0）。

    生成器优先级：
      candidate_sql 非空（客户端 Agent 产出）  → generator=client-agent
      candidate_sql 为空                      → generator=deterministic-planner
        (planner.plan(ds, nl)，ds=数据集对象；nl=需求标题+描述 or structured_requirement
         subject.value + output_fields.value 拼接)
    """
    field_mapping = []
    generation_notes = []
    generation_risks = []

    # 判定多候选（M6-2 F3）：非空 list/tuple[str] 且 len>=2 时，当作多候选分支
    is_multi = False
    if isinstance(candidate_sql, (list, tuple)):
        clean_list = [str(x).strip() for x in candidate_sql if isinstance(x, str) and str(x).strip()]
        if len(clean_list) >= 2:
            is_multi = True
        elif len(clean_list) == 1:
            # 退化单候选：按 str 路径，完全保持 M5-3 行为
            candidate_sql = clean_list[0]
        else:
            # 空列表 → 当 None，走确定性兜底
            candidate_sql = None

    sql_draft = None
    generator_label = None
    candidate_diff = None
    if is_multi:
        # M6-2 F3 分支：多条候选先判定差异再决定是否下发
        generator_label = "client-agent"
        candidate_diff = fallback_mod.f3_candidate_diff(clean_list)
        if candidate_diff.get("hold"):
            hold = candidate_diff.get("hold")
            md = candidate_diff.get("max_diff", 0.0)
            th = candidate_diff.get("threshold", 0.35)
            sql_draft = ""  # 不下发
            generation_notes.append(
                "多候选差异过大（max_diff=%.3f ≥ 阈值 %.3f）：进入 F3 hold，只保留计划草稿、不下发 SQL"
                % (float(md), float(th))
            )
            chosen = ""
        else:
            # 差异小：挑差异最小的第一条（= clean_list[0]，它是候选源顺序首条；若有对完全相同也不影响）
            chosen = clean_list[0]
            sql_draft = chosen
            generation_notes.append(
                "多候选差异较小（max_diff=%.3f < 阈值 %.3f）：下发候选首条，附 candidate_diff 供审计"
                % (float(candidate_diff.get("max_diff") or 0.0),
                   float(candidate_diff.get("threshold") or 0.35))
            )
    elif candidate_sql is not None and str(candidate_sql).strip():
        sql_draft = str(candidate_sql).strip()
        generator_label = "client-agent"
        generation_notes.append("SQL 由调用方传入（candidate_sql 参数）；网关只审不造")
    else:
        generator_label = "deterministic-planner"
        ds = registry.get(dataset)
        # 构造 nl：优先 demand_requests.title + description，否则拼结构化需求
        title, desc = ("", "")
        if demand_id:
            title, desc = _demand_title_desc(demand_id)
            req = _requirement_safe(demand_id)
            if not (title + desc).strip() and req:
                sub = (req.get("subject") or {}).get("value") or ""
                fields = "、".join(
                    str((f or {}).get("value") or "")
                    for f in (req.get("output_fields") or [])
                    if f and (f.get("value") or "") and MISSING_MARK not in str(f.get("value"))
                )
                title = sub
                desc = fields
        nl = (title.strip() + " " + desc.strip()).strip()
        if not nl:
            generation_risks.append("确定性兜底：构造 nl 为空 → planner 会返回 _refuse（不猜 SQL）")
            planned = planner_mod._refuse(
                dataset, "构造自然语言失败",
                "无法从 demand_requests / structured_requirements 构造出可读 nl",
                "先保证 demand_requests 有 title/description，或 requirement 槽位非空",
            )
        else:
            planned = planner_mod.plan(ds, nl)
        sql_draft = planned.get("sql") or ""
        if planned.get("blocked"):
            generation_risks.append(
                "确定性兜底 planner 拒绝生成：intent=%s reason=%s guide=%s" % (
                    planned.get("intent"),
                    (planned.get("reason") or "")[:200],
                    (planned.get("guide") or "")[:200],
                )
            )
        else:
            generation_notes.append(
                "确定性兜底命中 intent=%s；引用 MDL 对象 %d 个" % (
                    planned.get("intent"), len(planned.get("objects") or []),
                )
            )
        # sql_plan 提供 field_mapping 可复用
        if isinstance(sql_plan, dict):
            field_mapping = list(sql_plan.get("field_mapping") or [])

    # 立即 gates.review（L5 没有 exec_result → skipped，属预期）
    try:
        review = gates_mod.review(sql_draft or "", dataset=dataset)
    except Exception as e:  # noqa: BLE001
        review = {
            "status": "不通过",
            "layers": [{
                "layer": "L-GATEWAY", "name": "网关审查异常",
                "ok": False, "skipped": False,
                "detail": "gates.review 抛异常 %s: %s" % (type(e).__name__, str(e)[:240]),
            }],
            "syntax_issues": [],
            "semantic_issues": [],
            "rule_violations": [],
            "revised_sql": None,
            "review_notes": [],
        }

    out = {
        "sql_draft": sql_draft or "",
        "field_mapping": field_mapping,
        "generation_notes": generation_notes,
        "generation_risks": generation_risks,
        "generator": generator_label,
        "review": review,
    }
    # M6-2 F3 增强：多候选分支下，hold=True 时显式加 hold/hold_reason/candidate_diff；
    # 不 hold 时只加 candidate_diff，不加 hold（避免下游兼容旧输出麻烦）
    if is_multi and isinstance(candidate_diff, dict):
        if candidate_diff.get("hold"):
            out["hold"] = True
            out["hold_reason"] = (
                "候选 SQL 版本差异过大（max_diff=%.3f ≥ 阈值 %.3f），已保留计划草稿、不下发 SQL"
                % (float(candidate_diff.get("max_diff") or 0.0),
                   float(candidate_diff.get("threshold") or 0.35))
            )
        out["candidate_diff"] = candidate_diff
    return out
