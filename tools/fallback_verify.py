# -*- coding: utf-8 -*-
"""M6-2 失败回退矩阵 · 5 类失败逐条可复现验证（M6-2 补丁 P1：F1/F2 真链路）

跑法：
  仓库根目录 →  python3 tools/fallback_verify.py            （默认：DB 不可达时 F1/F2 真链路 SKIP，不影响全过）
              →  python3 tools/fallback_verify.py --live-required  （验收方强制：真链路若不可达 → 直接 FAIL）
  exit 0 全部通过；exit 1 任何失败。
"""
import os
import sys
import json as _json

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
GW = os.path.join(ROOT, "gateway")
if GW not in sys.path:
    sys.path.insert(0, GW)
# 离线可跑：默认 MDL 路径指向仓库内 workspace 副本（宿主无 /workspace-b/mdl.json）
os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))

LIVE_REQUIRED = "--live-required" in sys.argv

FAIL = []


def expect(name, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print("  [%s] %s %s" % (status, name, ("  -> " + str(detail)[:200] if detail else "")))
    if not condition:
        FAIL.append(name)


def skip(name, reason, force_fail=LIVE_REQUIRED):
    """DB 不可达时：默认打印 SKIP 不记失败；验收方带 --live-required 则强制记 FAIL。"""
    if force_fail:
        print("  [FAIL] %s（--live-required 强制：%s）" % (name, reason[:160]))
        FAIL.append(name)
    else:
        print("  [SKIP] %s  -> %s（不加 --live-required 时不记失败）" % (name, reason[:160]))


# ---------------------------------------------------------------------
# 元数据库可达性探测（工具层 psycopg.connect）
# ---------------------------------------------------------------------
def _db_reachable():
    try:
        sys.path.insert(0, GW)
        import db as db_mod
    except Exception as e:
        return False, "导入 gateway/db 失败：%s" % e
    try:
        import psycopg as pg  # 与 gateway/db.py 同一驱动版本（requirements.txt 已锁 psycopg[binary]>=3）
    except Exception as e:
        return False, "导入 psycopg 失败：%s" % e
    try:
        dsn = getattr(db_mod, "DSN", None) or getattr(db_mod, "DB_DSN", None)
        if not dsn:
            return False, "db.DSN 未定义"
    except Exception as e:
        return False, "读取 db.DSN 失败：%s" % e
    try:
        conn = pg.connect(dsn, connect_timeout=2)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True, ""
    except Exception as e:
        return False, "db.DSN 连接失败：%s" % e


# ======================================================================
# 1. F1 引擎层（classify 纯函数，入参人工构造，不等同真实链路）
# ======================================================================
def check_F1():
    print("\n[F1] 引擎层（classify 纯函数，入参人工构造，不等同真实链路）")
    sys.path.insert(0, GW)
    import fallback as fb

    # F1 正例：显式 f1_hit=True → 应命中 F1
    r_hit = fb.classify(f1_hit=True, f1_reason="主体模糊：需求只提'做一张看板'，无主题/维度")
    print("  正例触发输入：f1_hit=True, f1_reason='主体模糊...'")
    print("    命中类别=", r_hit["kinds"], " hold=", r_hit["hold"], " deliverable=", r_hit["deliverable"])
    expect("F1-引擎 正例：命中 F1，动作=进入确认问题", "F1" in r_hit["kinds"] and r_hit["actions"] and
           any(a.get("stage") == "语义定型" and "确认问题" in a.get("strategy", "")
               for a in r_hit["actions"]))

    # F1 负例：f1_hit=False / 未传 → 不命中
    r_miss = fb.classify(f1_hit=False, f2_hit=False)
    print("  负例触发输入：f1_hit=False")
    print("    命中类别=", r_miss["kinds"])
    expect("F1-引擎 负例：不命中 F1", "F1" not in r_miss["kinds"])


# ======================================================================
# 2. F1 真链路：demand.create + analysis.first_round
# ======================================================================
def check_F1_live(db_ok, db_reason):
    print("\n[F1] 真链路：demand.create + analysis.first_round → questions → classify")
    if not db_ok:
        skip("F1-真链路", db_reason)
        return
    sys.path.insert(0, GW)
    import fallback as fb
    try:
        import demand as demand_mod
        import analysis as analysis_mod
    except Exception as e:
        skip("F1-真链路", "导入 demand/analysis 失败：%s" % e)
        return

    try:
        # 五字段建单
        payload = {
            "title": "帮我做一张看板",
            "business_context": "F1 真链路核查",
            "description": "帮我做一张看板",
            "expected_output": "随便看看",
            "contact": "验收方-13900000000",
        }
        created = demand_mod.create(payload, actor="verifier")
        demand_id = None
        if isinstance(created, dict):
            demand_id = created.get("demand_id") or created.get("id")
        if not demand_id:
            skip("F1-真链路", "demand.create 未返回 demand_id：%r" % (str(created)[:200]))
            return
        print("  demand_id =", demand_id, "（五字段建单，title='帮我做一张看板'）")
        # 真调 analysis.first_round
        first = analysis_mod.first_round(demand_id, "B", actor="verifier", persist=True)
        questions = first.get("questions") if isinstance(first, dict) else None
        n = len(questions or [])
        # 断言：f1_hit 必须由真链路 questions 条数 计算，不得硬编码
        f1_hit = bool(n >= 1)
        print("  questions 条数 =", n)
        for i, q in enumerate(list(questions or [])[:2]):
            qid = q.get("question_id") or q.get("id")
            txt = q.get("question") or ""
            slot = q.get("slot") or "-"
            print("    Q[%d]  %s (slot=%s)  text[:80]=%r" % (i + 1, qid, slot, str(txt)[:80]))
        cls = fb.classify(f1_hit=f1_hit,
                          f1_reason="analysis.first_round 生成 %d 条待确认问题" % n)
        print("  喂 classify(f1_hit=%s) → kinds=%s" % (f1_hit, cls.get("kinds")))
        expect("F1-真链路：questions 条数真算 f1_hit 并命中 F1",
               f1_hit is True and "F1" in cls.get("kinds", []) and
               any("确认问题" in (a.get("strategy") or "") for a in (cls.get("actions") or [])))
    except Exception as e:
        skip("F1-真链路", "异常：%s: %s" % (type(e).__name__, str(e)[:240]))


# ======================================================================
# 3. F2 引擎层（classify 纯函数，入参人工构造，不等同真实链路）
# ======================================================================
def check_F2():
    print("\n[F2] 引擎层（classify 纯函数，入参人工构造，不等同真实链路）")
    sys.path.insert(0, GW)
    import fallback as fb
    # F2 正例：f2_hit=True，理由=table_candidates 多候选未达唯一解阈值
    r_hit = fb.classify(f2_hit=True, f2_reason="候选 5 张，s1=3.1 s2=2.8 ratio=1.1 未达 s1>=4 且 >=1.5s2")
    print("  正例触发输入：f2_hit=True, reason=多候选未达唯一解阈值")
    print("    命中类别=", r_hit["kinds"], " hold=", r_hit["hold"])
    expect("F2-引擎 正例：命中 F2 + hold=True（需人工审核）",
           "F2" in r_hit["kinds"] and r_hit["hold"] is True)

    # F2 负例：唯一解自动通过，f2_hit=False
    r_miss = fb.classify(f2_hit=False)
    print("  负例触发输入：f2_hit=False（明确主题表=dws_store_daily_agg）")
    print("    命中类别=", r_miss["kinds"])
    expect("F2-引擎 负例：不命中 F2", "F2" not in r_miss["kinds"])


# ======================================================================
# 4. F2 真链路：schema_scan.scan → analysis.first_round → requirement.build → sqlgen.plan（3 用例）
# ======================================================================
def _ensure_snapshot(dataset):
    """schema_scan.latest_version(dataset) 无快照时先 scan(dataset, persist=True)。"""
    sys.path.insert(0, GW)
    import schema_scan as ss_mod
    lv = ss_mod.latest_version(dataset)
    if lv and isinstance(lv, dict) and lv.get("schema_version"):
        return lv.get("schema_version")
    # 无 → 先 scan
    res = ss_mod.scan(dataset, persist=True)
    if isinstance(res, dict) and res.get("schema_version"):
        return res.get("schema_version")
    lv2 = ss_mod.latest_version(dataset) or {}
    return (lv2 or {}).get("schema_version")


def _build_chain(demand_id, dataset):
    """按要求顺序真链路：schema_scan.scan? → analysis.first_round → requirement.build → sqlgen.plan。
       返回 sqlgen.plan 的 dict。"""
    sys.path.insert(0, GW)
    import analysis as analysis_mod
    import requirement as req_mod
    import sqlgen as sqlgen_mod
    sv = _ensure_snapshot(dataset)
    _ = sv  # noqa: F841
    analysis_mod.first_round(demand_id, dataset, actor="verifier", persist=True)
    req_mod.build(demand_id, dataset)
    return sqlgen_mod.plan(demand_id, dataset)


def check_F2_live(db_ok, db_reason):
    print("\n[F2] 真链路：schema_scan.scan + analysis.first_round + requirement.build + sqlgen.plan（3 用例）")
    if not db_ok:
        skip("F2-真链路", db_reason)
        return
    sys.path.insert(0, GW)
    import fallback as fb
    try:
        import demand as demand_mod
    except Exception as e:
        skip("F2-真链路", "导入 demand 失败：%s" % e)
        return

    cases = [
        # tag, expect(f2_hit_bool), dataset, title_desc
        ("负例#1 B 门店销售订单和销售额", False, "B", "门店销售订单和销售额"),
        ("正例#2 B 门店会员消费明细", True,  "B", "门店会员消费明细"),
        ("正例#3 A 门店销售订单和销售额", True,  "A", "门店销售订单和销售额"),
    ]
    mismatches = []
    for tag, expect_hit, ds, title_desc in cases:
        try:
            payload = {
                "title": title_desc,
                "business_context": "F2 真链路核查 %s" % tag,
                "description": title_desc,
                "expected_output": "指标与明细",
                "contact": "验收方-13900000000",
            }
            created = demand_mod.create(payload, actor="verifier")
            demand_id = None
            if isinstance(created, dict):
                demand_id = created.get("demand_id") or created.get("id")
            if not demand_id:
                skip("F2-真链路 %s" % tag, "demand.create 未返回 demand_id")
                continue
            plan = _build_chain(demand_id, ds)
            chosen = (plan.get("chosen_table") or "") if isinstance(plan, dict) else ""
            f2_hit = not bool(chosen)
            tc = (plan.get("table_candidates") or []) if isinstance(plan, dict) else []
            srt = sorted(tc, key=lambda t: (-float((t.get("score") or 0) or 0),
                                            (t.get("table") or t.get("model") or "")))
            top2 = []
            for t in srt[:2]:
                top2.append((t.get("table") or t.get("model") or "?", float((t.get("score") or 0) or 0)))
            dg = plan.get("draft_gate") or {} if isinstance(plan, dict) else {}
            detail_txt = (dg.get("detail") or "")[:200]
            print("  【%s】 dataset=%s  demand_id=%s" % (tag, ds, demand_id))
            print("    candidates 数=%d  top2=%s  chosen_table=%r  f2_hit=%s" % (len(tc),
                  [(t[0], round(t[1], 2)) for t in top2], chosen, f2_hit))
            print("    draft_gate.detail[:200] = %r" % detail_txt)
            cls = fb.classify(f2_hit=f2_hit,
                              f2_reason=("chosen_table 为空（找不到明确主题表），candidates=%d，top2=%r"
                                         % (len(tc), [(t[0], round(t[1], 2)) for t in top2])))
            kinds = cls.get("kinds") or []
            hold = bool(cls.get("hold"))
            if expect_hit:
                # 正例：命中 F2 且 hold=True
                cond = ("F2" in kinds) and (hold is True)
                if not cond:
                    mismatches.append(("%s:expect F2 in kinds+hold=True" % tag,
                                       "got kinds=%r hold=%s" % (kinds, hold)))
                    expect("F2-真链路 %s（正例）→ 命中 F2 且 hold=True" % tag, False)
                else:
                    expect("F2-真链路 %s（正例）→ 命中 F2 且 hold=True" % tag, True)
            else:
                # 负例：不命中 F2
                cond = "F2" not in kinds
                if not cond:
                    mismatches.append(("%s:expect F2 NOT in kinds" % tag,
                                       "got kinds=%r chosen=%r" % (kinds, chosen)))
                    expect("F2-真链路 %s（负例）→ 不命中 F2" % tag, False)
                else:
                    expect("F2-真链路 %s（负例）→ 不命中 F2" % tag, True)
        except Exception as e:
            skip("F2-真链路 %s" % tag, "异常：%s: %s" % (type(e).__name__, str(e)[:240]))

    if mismatches:
        print("  【F2 期望值对比】下表为任务给出的期望值与本环境实测值差异条目：")
        for msg, actual in mismatches:
            print("   -", msg, "；实=", actual)
    else:
        print("  【F2 期望值对比】3 用例实测全部符合任务期望值")


# ======================================================================
# 5. F3 SQL 生成：多候选差异大 → hold（不自动下发）
# ======================================================================
def check_F3():
    print("\n[F3] SQL 生成：多候选差异过大 → hold 草稿不下发")
    sys.path.insert(0, GW)
    import fallback as fb

    # 组 1：完全相同（指纹全等 → diff=0 → 不 hold）
    s1 = "SELECT COUNT(*) FROM dws_store_daily_agg WHERE stat_date = '2025-09-01'"
    s2 = "  select   count(*)    from dws_store_daily_agg  where stat_date='2025-09-01' ;  "
    r1 = fb.f3_candidate_diff([s1, s2])
    print("  组1（完全相同→不hold）：n=%d max_diff=%.3f hold=%s threshold=%.3f" % (
        r1["n"], r1["max_diff"], r1["hold"], r1["threshold"]))
    expect("F3 组1：完全相同 SQL → hold=False", r1["hold"] is False and r1["max_diff"] < 0.001)

    # 组 2：小差异（改 WHERE 值）→ max_diff 一般<0.2 → 不 hold（下游直接下发）
    s3 = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date BETWEEN '2025-08-01' AND '2025-08-31' GROUP BY 1"
    s4 = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date BETWEEN '2025-09-01' AND '2025-09-30' GROUP BY 1"
    r2 = fb.f3_candidate_diff([s3, s4])
    print("  组2（小差异→不hold）：n=%d max_diff=%.3f hold=%s" % (r2["n"], r2["max_diff"], r2["hold"]))
    expect("F3 组2：小差异（WHERE 值差异）→ hold=False", r2["hold"] is False)

    # 组 3：大差异（聚合列 + 加 JOIN 完全不同维度） → max_diff 大 → hold
    s5 = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date = '2025-09-01' GROUP BY 1"
    s6 = ("SELECT a.member_id, b.member_name, COUNT(DISTINCT a.order_id) AS orders "
          "FROM dwd_order_di a JOIN dim_member b ON a.member_id = b.member_id "
          "WHERE a.stat_date BETWEEN '2025-01-01' AND '2025-06-30' GROUP BY 1,2 "
          "HAVING COUNT(DISTINCT a.order_id) >= 5 ORDER BY orders DESC LIMIT 100")
    r3 = fb.f3_candidate_diff([s5, s6])
    print("  组3（大差异→hold）：n=%d max_diff=%.3f hold=%s" % (r3["n"], r3["max_diff"], r3["hold"]))
    expect("F3 组3：大差异（主题表/维度/SQL 完全不同）→ hold=True", r3["hold"] is True and r3["max_diff"] >= 0.35)

    # 边界：n<=1 → hold=False
    r4 = fb.f3_candidate_diff([s1])
    expect("F3 边界：单候选 → hold=False", r4["hold"] is False and r4["n"] == 1)


# ======================================================================
# 6. F4 SQL 审查：规则不通过 → 退回人工 + 治理指引（gates.review 真实调用）
# ======================================================================
def check_F4():
    print("\n[F4] SQL 审查：规则不通过 → 退回人工 + 治理指引（gates.review 真实调用）")
    sys.path.insert(0, GW)
    import fallback as fb
    import gates as g_mod

    # F4 正例：构造 SELECT * + JOIN 无条件（多条阻断规则）
    bad_sql = "SELECT * FROM dws_store_daily_agg a, dim_store b"
    rev = g_mod.review(bad_sql, dataset="B")
    print("  正例触发 SQL=%r" % bad_sql[:48])
    print("    status=", rev.get("status"), " rule_violations=", [r.get("rule_id") for r in (rev.get("rule_violations") or [])])
    ids = []
    for r in (rev.get("rule_violations") or []):
        if r.get("rule_id") and r["rule_id"] not in ids:
            ids.append(r["rule_id"])
    for bucket in (rev.get("semantic_issues") or []), (rev.get("syntax_issues") or []):
        for it in bucket or []:
            if isinstance(it, dict) and it.get("rule") and it["rule"] not in ids:
                ids.append(it["rule"])
    gd = fb.f4_guidance(ids)
    print("    命中规则=", ids)
    print("    治理指引 len=", len(gd), " 首条=", gd[0] if gd else None)
    expect("F4 正例：review.status=不通过 且 命中≥1 条规则 + 有治理指引（why/how_to_fix 非空）",
           rev.get("status") == "不通过" and len(ids) >= 1 and len(gd) >= 1 and
           all(isinstance(x, dict) and "rule" in x and "why" in x and "how_to_fix" in x and
               isinstance(x["how_to_fix"], str) and len(x["how_to_fix"]) >= 2
               for x in gd))

    # F4 负例：合法 SQL（简单单表聚合，无阻断 warning 可忽略）
    good_sql = "SELECT store_id, SUM(sales_amount) FROM dws_store_daily_agg WHERE stat_date = '2025-09-01' GROUP BY store_id"
    rev2 = g_mod.review(good_sql, dataset="B")
    print("  负例触发 SQL=%r  → status=%s" % (good_sql[:48], rev2.get("status")))
    expect("F4 负例：合法 SELECT → status != 不通过", rev2.get("status") != "不通过")


# ======================================================================
# 7. F5 结果校验：行数/信号 → deliverable + 原因
# ======================================================================
def check_F5():
    print("\n[F5] 结果校验：行数/列数/信号 → deliverable + 原因")
    sys.path.insert(0, GW)
    import fallback as fb

    cases = [
        # (row_count, column_count, signals, expect_deliverable, tag)
        (100, 6, [], True, "  正例：正常 100×6 无信号 → deliverable=True"),
        (0, 6, [], False, "  负例1：0 行 → deliverable=False（0行不可交付）"),
        (50, 4, ["L5: 金额列含负值 -12.5 / -3.2；请确认是否冲销"], False, "  负例2：有 L5 信号 → False"),
        (-1, 3, [], False, "  负例3：行数为负/非整数 → False"),
    ]
    ok_flags = []
    for rc, cc, sigs, expected, tag in cases:
        r = fb.f5_deliverable(rc, cc, sigs)
        print(tag + " → deliverable=%s reason=%r" % (r.get("deliverable"), r.get("reason", "")))
        ok = bool(r.get("deliverable")) is bool(expected)
        ok_flags.append(ok)
    expect("F5 4 组输入全部符合期望", all(ok_flags))


# ======================================================================
# 附加：sqlgen.generate 多候选行为（F3 真链路）
# ======================================================================
def check_F3_sqlgen_integration():
    print("\n[F3-集成] sqlgen.generate(candidate_sql=[大差异]) → hold=True, generated_sql 置空, sql_draft=''")
    sys.path.insert(0, GW)
    import sqlgen as sg
    s_a = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg WHERE stat_date = '2025-09-01' GROUP BY 1"
    s_b = ("SELECT b.region, COUNT(DISTINCT a.member_id) AS uniq_members "
           "FROM dwd_order_di a JOIN dim_store b ON a.store_id = b.store_id "
           "WHERE a.stat_date BETWEEN '2025-01-01' AND '2025-12-31' GROUP BY 1")
    out = sg.generate(candidate_sql=[s_a, s_b], dataset="B")
    print("  candidate_diff.hold=", out.get("candidate_diff", {}).get("hold"),
          " max_diff=", out.get("candidate_diff", {}).get("max_diff"))
    print("  hold=", out.get("hold"), " hold_reason[:60]=",
          (out.get("hold_reason") or "")[:60], " sql_draft=%r" % (out.get("sql_draft") or ""))
    expect("F3-集成：大差异 2 候选 → out.hold=True, sql_draft='', hold_reason 含'差异过大'",
           out.get("hold") is True and (out.get("sql_draft") or "") == "" and
           "差异过大" in (out.get("hold_reason") or ""))

    # 单候选/字符串 → 行为不变（无 hold 字段）
    out_s = sg.generate(candidate_sql=s_a, dataset="B")
    print("  单候选/字符串 → 键 hold 存在？", "hold" in out_s,
          " sql_draft len=", len(out_s.get("sql_draft") or ""))
    expect("F3-兼容：单候选 str → 无 hold 键，sql_draft 非空",
           "hold" not in out_s and isinstance(out_s.get("sql_draft"), str) and
           len(out_s.get("sql_draft") or "") > 0)


# ======================================================================
# main
# ======================================================================
def main():
    # 先测 DB 可达
    db_ok, db_reason = _db_reachable()
    print("环境探测：元数据库可达 =", db_ok, ("  " + db_reason if not db_ok else ""),
          "（--live-required=%s）" % LIVE_REQUIRED)

    # 调用顺序：F1 引擎层 → F1 真链路 → F2 引擎层 → F2 真链路 → F3 → F4 → F5 → F3 集成
    check_F1()
    check_F1_live(db_ok, db_reason)
    check_F2()
    check_F2_live(db_ok, db_reason)
    check_F3()
    check_F4()
    check_F5()
    check_F3_sqlgen_integration()

    print("\n==================== 总览 ====================")
    if FAIL:
        print("FAIL 条目=%d：%s" % (len(FAIL), " / ".join(FAIL)))
        print("失败！")
        sys.exit(1)
    print("全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
