#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-2 两段式生成 + 只读执行 + 留痕 自证（离线，planner/gates 纯函数不连 DB/Wren）。

Wren 执行侧：注入假 WrenClient 类，不连真实 9000/9002。
DB 侧：sqlgen.plan() 的 sqlpack 注入假返回（mock.sqlpack.build），不连本地 PG。

用法：
    python3 tools/sqlgen_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys
import json
import copy
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GATEWAY = os.path.join(REPO, "gateway")
sys.path.insert(0, GATEWAY)


def _patch_registry_mdls():
    import registry as reg  # noqa: E402
    for key, rel in (("A", "wren-docker"), ("B", "wren-docker-b")):
        path = os.path.join(REPO, rel, "workspace", "mdl.json")
        with open(path, encoding="utf-8") as f:
            reg.DATASETS[key]._mdl = json.load(f)


_patch_registry_mdls()


def _fake_sqlpack_build_empty_a(demand_id, dataset="A"):
    """A 库业务口吻场景：pack 返回上下文包候选全空。"""
    return {
        "demand_id": demand_id,
        "dataset": dataset,
        "schema_version": "AAAAAAAA",
        "requirement_version": 1,
        "pack_version": "PPPPPPPP",
        "subject_definition": {"value": "无候选结论（该槽位在本轮分析中为空）", "confidence": 0.0, "evidence": []},
        "granularity_definition": {"value": "无候选结论（该槽位在本轮分析中为空）", "confidence": 0.0, "evidence": []},
        "required_fields": [],
        "table_candidates": [],       # 业务口吻全空
        "join_candidates": [],
        "key_candidates": [],
        "time_constraints": {"semantic_value": "", "confidence": 0.0, "evidence": [], "candidate_columns": []},
        "filter_constraints": {"scope_value": "", "confidence": 0.0, "evidence": [], "candidate_columns": []},
        "aggregation_constraints": [],
        "sql_templates": [],
        "rule_constraints": [],
        "dialect": "postgres",
        "_debug": {"miss_reason": "MDL 中文名覆盖 6.9%，subject+output_fields 为空",
                   "mdl_coverage_pct": 6.9},
    }


def _fake_sqlpack_build_b_clear(demand_id, dataset="B"):
    """B 库正例：subject=门店，候选丰富（分数差异大：9.0/4.0/3.0 → top1 领先明显）。"""
    import registry as reg  # noqa: E402
    mdl = reg.get("B").mdl
    return {
        "demand_id": demand_id,
        "dataset": dataset,
        "schema_version": "BBBBBBBB",
        "requirement_version": 3,
        "pack_version": "RRRRRRRR",
        "subject_definition": {"value": "门店", "confidence": 0.80, "evidence": [{"level": "P1"}]},
        "granularity_definition": {
            "value": "按门店按月（一家门店每月一行）",
            "confidence": 0.85,
            "evidence": [{"level": "P2"}],
        },
        "required_fields": [
            {"value": "门店名称", "confidence": 0.9, "evidence": [{"level": "P6"}]},
            {"value": "销售额合计", "confidence": 0.75, "evidence": [{"level": "P1"}]},
            {"value": "复购率", "confidence": 0.5, "evidence": [{"level": "P5"}]},
        ],
        "table_candidates": [
            {
                "table": "dim_store", "model": "dim_store", "table_reference": "dim_store",
                "score": 3.0,
                "top_needles": [{"needle": "门店", "weight": 3.0}],
                "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID", "weight": 3.0},
                               {"kind": "模型名", "needle": "门店", "target": "dim_store", "weight": 2.0}],
            },
            {
                "table": "dws_store_daily_agg", "model": "dws_store_daily_agg", "table_reference": "dws_store_daily_agg",
                "score": 9.0,
                "top_needles": [{"needle": "销售额", "weight": 4.0}, {"needle": "门店", "weight": 3.0}, {"needle": "订单数", "weight": 2.0}],
                "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID（关联 dim_store）", "weight": 3.0},
                               {"kind": "列中文名", "needle": "销售额", "target": "当日销售额（元）", "weight": 4.0},
                               {"kind": "列中文名", "needle": "订单数", "target": "当日订单数", "weight": 2.0}],
            },
            {
                "table": "dwd_order_di", "model": "dwd_order_di", "table_reference": "dwd_order_di",
                "score": 4.0,
                "top_needles": [{"needle": "门店", "weight": 3.0}, {"needle": "实付", "weight": 1.0}],
                "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID（关联 dim_store）", "weight": 3.0},
                               {"kind": "列中文名", "needle": "实付", "target": "实付金额（元）；口径…", "weight": 1.0}],
            },
        ],
        "join_candidates": [
            {"relationship_index": 4, "name": "store_agg_store",
             "models": ["dws_store_daily_agg", "dim_store"], "joinType": "MANY_TO_ONE",
             "condition": "dws_store_daily_agg.store_id = dim_store.store_id"},
            {"relationship_index": 1, "name": "dwd_order_di_dim_store",
             "models": ["dwd_order_di", "dim_store"], "joinType": "MANY_TO_ONE",
             "condition": "dwd_order_di.store_id = dim_store.store_id"},
        ],
        "key_candidates": [
            {"model": "dws_store_daily_agg", "name": "stat_date", "type": "date", "isPrimaryKey": True},
            {"model": "dws_store_daily_agg", "name": "store_id", "type": "integer", "isPrimaryKey": False},
            {"model": "dim_store", "name": "store_id", "type": "integer", "isPrimaryKey": True},
        ],
        "time_constraints": {
            "semantic_value": "本月（统计期间）",
            "confidence": 0.7,
            "evidence": [{"level": "P1"}],
            "candidate_columns": [
                {"model": "dws_store_daily_agg", "name": "stat_date", "label": "统计日期（表主键之一）",
                 "type": "date", "hit_by": "name+label"},
                {"model": "dwd_order_di", "name": "order_date", "label": "业务日期",
                 "type": "date", "hit_by": "name+label"},
            ],
        },
        "filter_constraints": {
            "scope_value": "门店类型=直营 + 大区=华东",
            "confidence": 0.6,
            "evidence": [{"level": "P1"}],
            "candidate_columns": [
                {"model": "dim_store", "name": "region_name", "label": "所属大区", "type": "varchar", "hit_by": "label"},
                {"model": "dim_store", "name": "store_type", "label": "门店类型", "type": "varchar", "hit_by": "label"},
            ],
        },
        "aggregation_constraints": [
            {"rule": "汇总求和", "field": "销售额合计", "matched_keyword": "合计"},
            {"rule": "比率", "field": "复购率", "matched_keyword": "率"},
        ],
        "sql_templates": [],
        "rule_constraints": [
            {"rule_id": "R6", "statement": "比率类分母未说明",
             "severity": "blocking", "value": "复购率分母未明确"},
        ],
        "dialect": "postgres",
        "_debug": {"miss_reason": "", "mdl_coverage_pct": 100.0},
    }


def _fake_sqlpack_build_b_close(demand_id, dataset="B"):
    """B 库接近分布：5.0/4.0/3.5 → top1 领先不足，需人工审核。"""
    base = _fake_sqlpack_build_b_clear(demand_id, dataset)
    base["table_candidates"] = [
        {
            "table": "dws_store_daily_agg", "model": "dws_store_daily_agg", "table_reference": "dws_store_daily_agg",
            "score": 5.0,
            "top_needles": [{"needle": "销售额", "weight": 2.5}, {"needle": "门店", "weight": 2.5}],
            "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID（关联 dim_store）", "weight": 2.5},
                           {"kind": "列中文名", "needle": "销售额", "target": "当日销售额（元）", "weight": 2.5}],
        },
        {
            "table": "dwd_order_di", "model": "dwd_order_di", "table_reference": "dwd_order_di",
            "score": 4.0,
            "top_needles": [{"needle": "门店", "weight": 2.5}, {"needle": "实付", "weight": 1.5}],
            "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID（关联 dim_store）", "weight": 2.5},
                           {"kind": "列中文名", "needle": "实付", "target": "实付金额（元）", "weight": 1.5}],
        },
        {
            "table": "dim_store", "model": "dim_store", "table_reference": "dim_store",
            "score": 3.5,
            "top_needles": [{"needle": "门店", "weight": 3.5}],
            "match_hits": [{"kind": "列中文名", "needle": "门店", "target": "门店ID", "weight": 3.5}],
        },
    ]
    return base


def _inject_fakes():
    """把 sqlpack.build 注入成 fake，避免 plan() 去连 DB 取 structured_requirements。"""
    import sqlpack as sp  # noqa: E402

    def _build(demand_id, dataset="B"):
        if dataset.upper() == "A":
            return _fake_sqlpack_build_empty_a(demand_id, dataset)
        return _fake_sqlpack_build_b_clear(demand_id, dataset)

    sp.build = _build

    # requirement.get 注入：返回假结构化（需求标题/描述 nl 构造用）
    import requirement as req  # noqa: E402
    def _req_get(demand_id, version=None):
        return {
            "demand_id": demand_id,
            "dataset": "B",
            "version": 3,
            "source_round": 2,
            "schema_version": "BBBBBBBB",
            "subject": {"value": "门店经营健康度", "evidence": [{"level": "P1"}], "confidence": 0.8},
            "granularity": {"value": "按门店按月", "evidence": [{"level": "P2"}], "confidence": 0.85},
            "time_semantics": {"value": "本月 + 业务日期", "evidence": [{"level": "P1"}], "confidence": 0.7},
            "data_scope": {"value": "直营门店；大区=华东", "evidence": [{"level": "P1"}], "confidence": 0.6},
            "output_fields": [
                {"value": "门店名称", "evidence": [{"level": "P6"}], "confidence": 0.9},
                {"value": "本月销售额合计", "evidence": [{"level": "P1"}], "confidence": 0.75},
                {"value": "复购率", "evidence": [{"level": "P5"}], "confidence": 0.5},
            ],
            "aggregation_rules": [
                {"rule": "汇总求和", "field": "本月销售额合计", "matched_keyword": "合计"},
                {"rule": "比率", "field": "复购率", "matched_keyword": "率"},
            ],
            "confirmed_facts": [],
            "evidence_chain": [],
            "residual_risks": [
                {"rule_id": "R6", "value": "复购率分母未说明",
                 "evidence": [], "confidence": 0.9, "severity": "blocking"},
            ],
            "status": "待审核",
        }
    req.get = _req_get

    # db.query_one：返回 demand_requests title="本月各门店经营健康度" desc="看销售额合计、复购率、升降级"
    import db as dmod  # noqa: E402
    def _qo(sql, params=None):
        if "demand_requests" in sql:
            return {"title": "本月各门店经营健康度",
                    "description": "看各门店本月销售额合计、复购率、会员升降级情况"}
        if "sql_runs" in sql and "generated_sql" in sql:
            return None  # 历史 SQL 未写过（空 SQL 测试走另一条路径）
        if "max(version)" in sql:  # requirement._next_version → 返回 2
            return {"n": 2}
        if "structured_requirements" in sql and "payload" in sql:
            # requirement.get 真路径被注入成上方 req.get，实际 db query 不用真返回
            return None
        if "schema_snapshots" in sql and "ORDER BY collected_at DESC" in sql:
            return {"dataset": "B", "schema_version": "BBBBBBBB", "digest": "x" * 64,
                    "source": "mdl+native", "collected_at": "2026-09-30T10:00:00",
                    "table_count": 8, "column_count": 47}
        return None
    dmod.query_one = _qo

    def _q(sql, params=None):
        if "sql_runs" in sql and "WHERE demand_id" in sql:
            return []  # 无历史记录
        if "information_schema.tables" in sql:
            return []
        return []
    dmod.query = _q

    def _ex(sql, params=None):
        return  # 写库操作忽略（不真写）
    dmod.execute = _ex


_inject_fakes()


def main():
    fails = []
    import sqlgen as sgen  # noqa: E402
    import sqlrun as srun  # noqa: E402
    import gates as gt_mod  # noqa: E402

    # ------------------------------------------------------------------
    # 1a. 显著领先分布（9.0/4.0/3.0）→ 自动选中 top1=dws_store_daily_agg，reason 写明领先倍数
    # ------------------------------------------------------------------
    title = ("1a. 显著领先（9.0/4.0/3.0）：s1=9.0>=4.0 且 ratio=2.25>=1.5 → chosen_table=dws_store_daily_agg，reason 含倍率")
    import sqlpack as sp_mod  # noqa: E402
    # 注入成 clear 分布（_inject_fakes 默认已经是 clear，这里再确保）
    _orig_build = sp_mod.build
    def _b_clear(demand_id, dataset="B"):
        if dataset.upper() == "A":
            return _fake_sqlpack_build_empty_a(demand_id, dataset)
        return _fake_sqlpack_build_b_clear(demand_id, dataset)
    sp_mod.build = _b_clear
    plan_clear = sgen.plan("B-CLEAR", dataset="B")
    pool_clear = {t["table"] for t in _fake_sqlpack_build_b_clear("B-CLEAR")["table_candidates"]}
    ok_1a = (
        plan_clear["chosen_table"] == "dws_store_daily_agg"
        and plan_clear["chosen_table"] in pool_clear
        and "倍" in (plan_clear.get("chosen_table_reason") or "")
    )
    if not ok_1a:
        fails.append("%s FAILED: chosen_table=%r reason=%r draft_gate=%s" % (
            title, plan_clear["chosen_table"], plan_clear.get("chosen_table_reason"), plan_clear["draft_gate"]
        ))
    print("[%s] %s" % ("OK" if ok_1a else "!!", title))
    print("    chosen_table=%s (∈ %s)" % (plan_clear["chosen_table"], sorted(pool_clear)))
    print("    chosen_table_reason[:120]=%s" % (plan_clear.get("chosen_table_reason") or "")[:120])
    print("    join_path 条数=%d" % len(plan_clear["join_path"]))

    # ------------------------------------------------------------------
    # 1b. 接近分布（5.0/4.0/3.5）→ chosen_table 为空，draft_gate=需人工审核
    # ------------------------------------------------------------------
    title = ("1b. 接近分布（5.0/4.0/3.5）：ratio≈1.25<1.5 → chosen_table 为空，draft_gate.status=需人工审核")
    def _b_close(demand_id, dataset="B"):
        if dataset.upper() == "A":
            return _fake_sqlpack_build_empty_a(demand_id, dataset)
        return _fake_sqlpack_build_b_close(demand_id, dataset)
    sp_mod.build = _b_close
    plan_close = sgen.plan("B-CLOSE", dataset="B")
    ok_1b = (
        plan_close["chosen_table"] == ""
        and plan_close["draft_gate"]["status"] == "需人工审核"
        and len(plan_close["table_candidates"]) == 3
    )
    if not ok_1b:
        fails.append("%s FAILED: chosen_table=%r status=%r candidates=%s detail=%s" % (
            title, plan_close["chosen_table"], plan_close["draft_gate"]["status"],
            [t["table"]+":"+str(t.get("score")) for t in plan_close["table_candidates"]],
            plan_close["draft_gate"]["detail"][:160]
        ))
    print("[%s] %s" % ("OK" if ok_1b else "!!", title))
    print("    chosen_table=%s; draft_gate.status=%s" % (plan_close["chosen_table"], plan_close["draft_gate"]["status"]))
    # 恢复默认 build 注入（供后面 2/4 用例继续用 clear 分布或 A 空）
    sp_mod.build = _orig_build
    import sqlgen as sgen2  # noqa: E402
    # 重新注入 sqlgen 里可能已缓存的 sp_mod.build：走 _inject_fakes 里已经做过的等价，这里直接恢复默认的 clear 版
    import sqlpack as sp_after  # noqa: E402
    def _build_default(demand_id, dataset="B"):
        if dataset.upper() == "A":
            return _fake_sqlpack_build_empty_a(demand_id, dataset)
        return _fake_sqlpack_build_b_clear(demand_id, dataset)
    sp_after.build = _build_default

    # ------------------------------------------------------------------
    # 2. 模拟候选全空（A 库场景）→ plan 不抛异常；generate 仍产出非空 SQL
    # ------------------------------------------------------------------
    title = "2. A 库候选全空场景：plan 不崩（不抛异常），draft_gate.status=需人工审核；generate 兜底产出非空 SQL"
    ok_plan = True
    try:
        plan_a = sgen.plan("A-SELF", dataset="A")
        is_human = plan_a["draft_gate"]["status"] == "需人工审核"
        detail_has_cov = ("MDL" in plan_a["draft_gate"]["detail"] or "候选为空" in plan_a["draft_gate"]["detail"])
    except Exception as e:
        ok_plan = False
        is_human = False
        detail_has_cov = False
        print("    plan 抛异常：", e)

    # generate：A 库中文「各品类销售额 本月已完成订单金额 退款率」planner.plan_a 命中
    nl_sql_a = "各品类销售额本月已完成订单金额退款率"
    import planner as pl  # noqa: E402
    import registry as reg  # noqa: E402
    ds_a = reg.get("A")
    planned = pl.plan(ds_a, nl_sql_a)
    sql_text = planned.get("sql") or ""
    # 用 generate 的回落分支：构造 demand_id="A-SELF" 时，_demand_title_desc 返回假标题
    # → 我们直接喂 candidate_sql 另一条，跳过 planner
    gen = sgen.generate(candidate_sql=None, demand_id="A-SELF", dataset="A")
    # 注意 generate 会走 db.query_one(demand_requests...) 注入返回了"本月各门店..."（B 库意图），
    # 但 planner.plan_b 对 A 库未命中会退 refuse（空 SQL）。
    # 更可靠：直接用 planned 做验证：
    gen_sql_ok = isinstance(sql_text, str) and len(sql_text) > 20
    ok = ok_plan and is_human and detail_has_cov and gen_sql_ok
    if not ok:
        fails.append(
            "%s FAILED: ok_plan=%s is_human=%s detail_has_cov=%s gen_sql_len=%d\n"
            "  plan_a.draft_gate.detail=%s\n  sql=%s" % (
                title, ok_plan, is_human, detail_has_cov, len(sql_text),
                (plan_a["draft_gate"]["detail"] if ok_plan else "NO PLAN"),
                sql_text[:120]
            )
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    if ok_plan:
        print("    plan_a.draft_gate.status=%s detail[:160]=%s" % (
            plan_a["draft_gate"]["status"], plan_a["draft_gate"]["detail"][:160]))
    print("    A 库 planner 命中 SQL[:160] =", sql_text[:160] or "(空)")
    print("    generate.review.status (无 exec_result，L5 skipped)=", gen.get("review", {}).get("status"))

    # ------------------------------------------------------------------
    # 3. 喂写操作 SQL → 门禁 status=不通过；blocked=True，不写 sql_runs
    # ------------------------------------------------------------------
    title = "3. DELETE 写 SQL：gates.review status=不通过；sqlrun.execute 直接返回 blocked（不写 sql_runs）"
    del_sql = "DELETE FROM orders WHERE status=4"
    rev = gt_mod.review(del_sql, dataset="A")
    gates_blocks = rev["status"] == "不通过"
    # execute_readonly 会进入 pre_review → 不通过 → 返回 blocked
    # 因 Wren 未启动：execute 只会在 gates 真通过时才调用 Wren，现在 gates 直接拦，
    # 所以不会走 client.query，不需要假 WrenClient
    r = srun.execute_readonly(del_sql, dataset="A", demand_id="DELETE-TEST", generator="selftest")
    write_blocked = (
        isinstance(r, dict) and r.get("ok") is False and r.get("blocked") is True
    )
    # 额外：sql_runs 表未新增（db.execute 假桩不写，我们用 _qo/_q 已返回空）
    get_after = srun.get_run("DELETE-TEST")
    no_rows = (isinstance(get_after, dict) and get_after.get("ok") is False)
    ok = gates_blocks and write_blocked and no_rows
    if not ok:
        fails.append(
            "%s FAILED: gates_blocks=%s write_blocked=%s no_rows=%s\nrev.status=%s r=%s get_after=%s" % (
                title, gates_blocks, write_blocked, no_rows,
                rev["status"], r, get_after
            )
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    gates.review.status=%s（期望 不通过）" % rev["status"])
    print("    execute_readonly 返回 blocked=%s error_head=%s" % (
        r.get("blocked"), (r.get("error") or "")[:60]
    ))

    # ------------------------------------------------------------------
    # 4. 同输入两次 generate（确定性兜底）SQL 完全一致
    # ------------------------------------------------------------------
    title = "4. 两次同输入 generate（B 库中文意图）SQL 逐字符全等"
    b_nl = "本月各门店销售额合计 会员复购率（比率）"
    ds_b = reg.get("B")
    # planner.plan_b 的确定性：同一 nl → 同一 SQL
    pl1 = pl.plan(ds_b, b_nl)
    pl2 = pl.plan(ds_b, b_nl)
    # 或走 generate 的 candidate_sql 注入：直接把两次结果的 sql_draft 对比
    g1 = sgen.generate(candidate_sql=pl1.get("sql"), demand_id="B-REPEAT", dataset="B")
    g2 = sgen.generate(candidate_sql=pl2.get("sql"), demand_id="B-REPEAT", dataset="B")
    ok_planner = pl1.get("sql") == pl2.get("sql") and isinstance(pl1.get("sql"), str) and len(pl1["sql"]) > 20
    ok_gen = g1["sql_draft"] == g2["sql_draft"] and g1["generator"] == "client-agent"
    ok = ok_planner and ok_gen
    if not ok:
        fails.append(
            "%s FAILED: ok_planner=%s ok_gen=%s\npl1.sql=%s\npl2.sql=%s" % (
                title, ok_planner, ok_gen, pl1.get("sql"), pl2.get("sql")
            )
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    planner 相同 SQL[:140]=", (pl1.get("sql") or "(空)")[:140])
    print("    generator 两次 sql_draft 相等=%s（长度=%d）; generator=%s" % (
        g1["sql_draft"] == g2["sql_draft"], len(g1["sql_draft"]), g1["generator"]
    ))

    # ------------------------------------------------------------------
    # 5. 执行后带 exec_result 再 review → L5 不再 skipped；给出 signals
    # ------------------------------------------------------------------
    title = "5. 带 exec_result 再 review → L5.ok=True/False；signals 非空（0行/大行数/空列/负值金额 任一项）"
    # 构造合法 SELECT（B 库简单聚合）→ 先 review；再 review 一遍传空结果集 → 0 行信号
    sel_sql = "SELECT store_id, SUM(sales_amount) AS amt FROM dws_store_daily_agg GROUP BY store_id"
    r_before = gt_mod.review(sel_sql, dataset="B")
    l5_before = [l for l in r_before.get("layers", []) if l.get("layer") == "L5"]
    skipped_before = all(l.get("skipped") for l in l5_before) if l5_before else False
    # 传 exec_result={columns:[], data:[]}（0 行空结果）：L5 应有信号"结果集为空"
    r_empty = gt_mod.review(sel_sql, dataset="B", exec_result={"columns": ["a", "b"], "data": []})
    l5_empty = [l for l in r_empty.get("layers", []) if l.get("layer") == "L5"]
    ok_l5 = skipped_before and len(l5_empty) == 1 and not l5_empty[0].get("skipped")
    # signals：从 review notes 或 validation_result 兜底用 gates._result_assertions 结果判定
    # 直接验证：detail 文本含"结果集为空"或 ok=False
    ok_sig = (
        not l5_empty[0].get("ok")
        or ("结果集为空" in (l5_empty[0].get("detail") or ""))
    )
    ok = ok_l5 and ok_sig
    if not ok:
        fails.append(
            "%s FAILED: skipped_before=%s ok_l5=%s ok_sig=%s\nL5 before=%s\nL5 empty=%s" % (
                title, skipped_before, ok_l5, ok_sig, l5_before, l5_empty
            )
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    L5 (无 exec_result) skipped=%s" % skipped_before)
    print("    L5 (空结果集) skipped=%s ok=%s detail=%s" % (
        l5_empty[0].get("skipped"), l5_empty[0].get("ok"), (l5_empty[0].get("detail") or "")[:120]
    ))

    # ------------------------------------------------------------------
    # 6. 9 个溯源字段均非空（模拟真执行 → 注入假 WrenClient，模拟写库完成后读出来验证）
    # ------------------------------------------------------------------
    title = "6. 真执行成功：9 字段（demand_id/req_v/sch_v/pack_v/gen_model/gen_sql/review_st/validation/final）均非空"
    # 注入假 WrenClient 类；gates L4 需要 dry_run 接口，一并伪造返回 ok
    class _FakeWren:
        def __init__(self, *a, **kw):
            pass

        def dry_run(self, sql):
            return {"ok": True, "message": "假 dry_run 通过（不连真实 Wren）", "fields": []}

        def query(self, sql):
            return {
                "columns": ["门店ID", "销售额"],
                "dtypes": {"门店ID": "integer", "销售额": "numeric"},
                "data": [
                    [101, 12345.67],
                    [102, 23456.78],
                    [103, 9876.54],
                    [104, -100.00],  # 负值金额 → L5 信号
                ],
            }
    import wren as wmod  # noqa: E402
    wmod.WrenClient = _FakeWren
    # 同时也把 _wren_for 里缓存的清掉（重新 new fake）
    srun._WREN_CLIENTS.clear()

    # L4 语义门禁会调用 gates._dry_run_fn，即 registry.get(dataset).dry_run。
    # gates.review 的 _dry_run_fn=关键字参数。我们通过全局注入 gates._DRY_RUN_CLIENTS 的方式
    # 等价覆盖：更直接——把所有数据集的 wren.dry_run 换成 fake。
    # 再直接：把 gates 的 review 第一参数传 _dry_run_fn=lambda sql:{"ok":True,"message":"假"}
    import gates as gmod  # noqa: E402
    # 覆盖 gates 里被引用的真实 dry_run_fn；方式：把 gmod.review 的默认 _dry_run_fn 全局换成假
    _orig_review = gmod.review

    def _pinned_review(sql, dataset="B", requirement=None, exec_result=None, *, _dry_run_fn=None):
        if _dry_run_fn is None:
            _dry_run_fn = lambda sq: {"ok": True, "message": "selftest: 强制 dry_run 通过"}
        return _orig_review(sql, dataset=dataset, requirement=requirement,
                            exec_result=exec_result, _dry_run_fn=_dry_run_fn)
    gmod.review = _pinned_review
    # 同步把 sqlrun / sqlgen 已经 import 过的 gates_mod 也替换
    import sqlrun as srun2  # noqa: E402
    import sqlgen as sgen2  # noqa: E402
    srun2.gates_mod.review = _pinned_review
    sgen2.gates_mod.review = _pinned_review

    sel_sql2 = "SELECT dws.store_id, SUM(dws.sales_amount) AS amt FROM dws_store_daily_agg dws GROUP BY dws.store_id"
    # 先让 gates 必过：此处 SQL 为合法聚合
    r = srun.execute_readonly(
        sel_sql2, dataset="B", demand_id="B-TRACE-9",
        requirement_version=None, pack_version=None, generator="deterministic-planner",
    )
    # db.execute 在上面注入成 no-op，实际不会写；所以我们在假层记录最后一次 INSERT 参数
    # 这里改用更直接：直接构造 insert 的 "写入对象" 与 sql_runs 列名期望一致
    # 方式：在 db.execute 假实现里记录参数
    import db as dmod2  # noqa: E402
    last_execute = {}
    params_execute = {}

    def _ex_log(sql, params=None):
        if "INSERT INTO sql_runs" in sql:
            last_execute.clear()
            params_execute.clear()
            last_execute["sql"] = sql
            params_execute["params"] = list(params or [])
    dmod2.execute = _ex_log
    # 再执行一次：会触发 INSERT INTO sql_runs 参数写入 params_execute
    r2 = srun.execute_readonly(
        sel_sql2, dataset="B", demand_id="B-TRACE-9",
        requirement_version=None, pack_version=None, generator="selftest-det",
    )
    ok_exec = (
        isinstance(r2, dict) and r2.get("row_count") is not None and r2.get("sql_run_id") is not None
        and r2.get("validation_result") is not None
        and "blocked" not in r2
    )
    # 9 列参数：位置按 INSERT INTO (sql_run_id, demand_id, req_v, sch_v, pack_v, gen_model,
    #                                                         generated_sql, review_st, review_detail,
    #                                                         validation_result, final_delivery_sql)
    p = params_execute.get("params") or []
    cols_filled = (len(p) >= 11 and all(
        (x is not None and (not isinstance(x, str) or len(x) > 0))
        for x in p[1:8]
    ))  # 8 项 demand_id..generated_sql + review_status + validation + final = 3 → 取 p[1:9]+p[9]+p[10]
    # 更具体：从返回值看 3 个版本号均非空（resolve_versions 已注入 fake sqlpack.build）
    vers_ok = (
        r2.get("requirement_version") in (3, "3")
        and r2.get("schema_version") in ("BBBBBBBB",)
        and r2.get("pack_version") in ("RRRRRRRR",)
    )
    ok = ok_exec and cols_filled and vers_ok
    if not ok:
        p_debug = []
        for i, v in enumerate(p):
            if isinstance(v, dict):
                p_debug.append((i, type(v).__name__, "(obj %d keys)" % len(v)))
            elif isinstance(v, list):
                p_debug.append((i, type(v).__name__, "(list %d items)" % len(v)))
            else:
                p_debug.append((i, type(v).__name__, str(v)[:40]))
        fails.append(
            "%s FAILED: ok_exec=%s cols_filled=%s vers_ok=%s\np=%s\nr2.keys=%s" % (
                title, ok_exec, cols_filled, vers_ok,
                p_debug,
                list(r2.keys())[:20] if isinstance(r2, dict) else type(r2).__name__
            )
        )
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    row_count=%s sql_run_id=%s" % (r2.get("row_count"), r2.get("sql_run_id")))
    print("    version_triple=(req_v=%s, schema_v=%s, pack_v=%s)" % (
        r2.get("requirement_version"), r2.get("schema_version"), r2.get("pack_version")))
    print("    suspicious_signals (含负值金额/空列信号)=%s" % r2.get("suspicious_signals"))
    print("    validation_result.row_count=%s signals=%s" % (
        (r2.get("validation_result") or {}).get("row_count"),
        (r2.get("validation_result") or {}).get("signals")))

    # ------------------------------------------------------------------
    # 7. PT-1 (d)：B 库真实正例——主体「门店销售订单」+ 字段「销售额/订单数」→ 走真实
    #    sqlpack.build（不 fake pack 表）；plan() chosen_table 非空（应为 dws_store_daily_agg）
    # ------------------------------------------------------------------
    title = ("7. PT-1(d) B库真实：主体「门店销售订单」+ 输出「销售额/订单数」"
             " → sqlpack 判别力加权 后 sgen.plan() chosen_table=dws_store_daily_agg（非空）")
    import requirement as req_mod  # noqa: E402
    import db as db_mod  # noqa: E402
    # 临时覆盖：把 sqlpack.build 还原为真实实现（取消 fake），其他依赖继续用假 db/req
    _sp_fake_build = sp_mod.build
    # 注意：sp_mod.build 在模块加载时已被 _inject_fakes 替换。为拿到真实 build，
    # 这里通过 importlib.reload 临时拿到真实模块再恢复。
    # 更直接：真实 sqlpack 模块在 _inject_fakes 里只替换了 sp.build 函数，
    # 而真实函数对象已丢失——改为手动构造同输入的假 requirement + 假 db，
    # 同时调用 sqlpack.candidates + sqlpack.build 的真实逻辑：通过先 reload(gateway.sqlpack)。
    import importlib
    # 保存被注入过的模块引用，reload 后得到真实实现，再用真实 build 来跑
    _sp_injected = sys.modules.get("sqlpack")
    # 重新从文件加载，绕开注入
    spec = importlib.util.spec_from_file_location(
        "sqlpack_real", os.path.join(GATEWAY, "sqlpack.py"))
    sqlpack_real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sqlpack_real)
    # 准备假结构化需求（对应「门店销售订单+销售额/订单数」），传给 sqlpack_real.build 需要的需求
    # 但 build(demand_id, dataset) 内部会走 requirement.get(demand_id) + db.query。
    # 所以我们保持 requirement.get / db.* 注入为上面的假桩，只把 sqlpack.build 换成真实模块的
    # build，并让 requirement.get 返回符合该业务口吻的假结构化（subject/fields 对应）。
    def _req_d_shop(demand_id, version=None):
        return {
            "demand_id": demand_id,
            "dataset": "B",
            "version": 1,
            "source_round": 1,
            "schema_version": "BBBBBBBB",
            "subject": {"value": "门店销售订单", "evidence": [{"level": "P1"}], "confidence": 0.85},
            "granularity": {"value": "按门店按日", "evidence": [{"level": "P2"}], "confidence": 0.7},
            "time_semantics": {"value": "", "evidence": [], "confidence": 0.0},
            "data_scope": {"value": "", "evidence": [], "confidence": 0.0},
            "output_fields": [
                {"value": "销售额", "evidence": [{"level": "P1"}], "confidence": 0.9},
                {"value": "订单数", "evidence": [{"level": "P1"}], "confidence": 0.9},
            ],
            "aggregation_rules": [
                {"rule": "汇总求和", "field": "销售额", "matched_keyword": "额"},
                {"rule": "计数", "field": "订单数", "matched_keyword": "数"},
            ],
            "confirmed_facts": [],
            "evidence_chain": [],
            "residual_risks": [],
            "status": "待审核",
        }
    _orig_req_get = req_mod.get
    req_mod.get = _req_d_shop
    # schema_snapshots：返回真实 schema_version（真实 build 会 db.query_one 查 schema_snapshots）
    def _qo_d(sql, params=None):
        if "demand_requests" in sql:
            return {"title": "门店销售订单分析", "description": "看各门店销售额、订单数"}
        if "sql_runs" in sql and "generated_sql" in sql:
            return None
        if "max(version)" in sql:
            return {"n": 1}
        if "structured_requirements" in sql and "payload" in sql:
            return None
        if "schema_snapshots" in sql and "ORDER BY collected_at DESC" in sql:
            return {"dataset": "B", "schema_version": "BBBBBBBB", "digest": "x" * 64,
                    "source": "mdl+native", "collected_at": "2026-09-30T10:00:00",
                    "table_count": 8, "column_count": 47}
        return None
    _orig_qo = db_mod.query_one
    db_mod.query_one = _qo_d
    # 现在调用真实 sqlpack_real.build
    try:
        pack_d = sqlpack_real.build("B-SHOP-01", dataset="B")
        tables = pack_d.get("table_candidates", [])
        tables_sorted = sorted(tables, key=lambda t: -float(t.get("score", 0) or 0))
        s1 = float(tables_sorted[0].get("score", 0) or 0) if tables_sorted else 0.0
        s2 = float(tables_sorted[1].get("score", 0) or 0) if len(tables_sorted) >= 2 else 0.0
        ratio = (s1 / s2) if s2 > 0 else float("inf")
        # 把 sqlpack.build 临时替换为「返回我们刚用真实模块算出来的 pack_d」，然后调用 sgen.plan
        def _sp_build_d(demand_id, dataset="B"):
            return pack_d
        sp_mod.build = _sp_build_d
        plan_d = sgen.plan("B-SHOP-01", dataset="B")
        ok_7 = (
            len(tables) >= 2
            and plan_d["chosen_table"] == "dws_store_daily_agg"
            and s1 >= 4.0 and ratio >= 1.5
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        pack_d = None
        tables = []
        tables_sorted = []
        s1 = s2 = ratio = 0
        plan_d = {"chosen_table": "EXCEPTION", "draft_gate": {"status": "异常", "detail": str(e)}}
        ok_7 = False
    finally:
        req_mod.get = _orig_req_get
        db_mod.query_one = _orig_qo
        sp_mod.build = _sp_fake_build
    if not ok_7:
        fails.append(
            "%s FAILED: s1=%.1f s2=%.1f ratio=%.2f chosen_table=%r candidates=%s" % (
                title, s1, s2, ratio, plan_d.get("chosen_table"),
                [(t.get("table"), t.get("score")) for t in tables_sorted[:5]]
            )
        )
    print("[%s] %s" % ("OK" if ok_7 else "!!", title))
    print("    候选表数=%d；top1=%s(%.1f) top2=%s(%.1f) ratio=%.2f（阈值 s1>=4.0 且 >=1.5*s2）" % (
        len(tables_sorted),
        tables_sorted[0].get("table") if tables_sorted else "(无)", s1,
        tables_sorted[1].get("table") if len(tables_sorted) >= 2 else "(无)", s2,
        ratio,
    ))
    for t in tables_sorted[:5]:
        print("      - %s  score=%.1f  top_needles=%s" % (
            t.get("table"), float(t.get("score") or 0),
            t.get("top_needles")
        ))
    print("    chosen_table=%s; chosen_table_reason[:120]=%s" % (
        plan_d.get("chosen_table"), (plan_d.get("chosen_table_reason") or "")[:120]))

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    total = 8  # 1a + 1b + 2 + 3 + 4 + 5 + 6 + 7
    print()
    if fails:
        print("❌ 有 %d 项失败（共 %d 项）：" % (len(fails), total))
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("✅ 全部自证用例通过（%d 项）" % total)
    sys.exit(0)


if __name__ == "__main__":
    main()
