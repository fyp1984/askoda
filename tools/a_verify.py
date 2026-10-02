#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A 库验收专项：5 条业务口吻样例跑完整链路（经真实 MCP），为《A 库验收单》回填区取证。

口径：样例按业务口吻编写，**非真实客户需求**；真实业务场景验收留 M9。
链路：需求受理 → 语义分析 → 结构化需求 → Schema 候选 → 上下文包 → 计划草稿
      → SQL 生成 → 五层门禁 → 只读执行 → 九字段留痕回放。
原则：**只打印真实执行输出**，拿不到就如实写"未生成/拒绝"，不补不猜。

用法（宿主）：
    python3 tools/a_verify.py
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from m4_verify import McpClient  # noqa: E402

SAMPLES = [
    {"n": 1, "title": "库存低于 50 的商品都有哪些？",
     "desc": "找出库存低于 50 的商品",
     "expect": "products 单表过滤（最低门槛）"},
    {"n": 2, "title": "统计上个月每个客户的订单总金额，按金额从高到低排",
     "desc": "按订单总金额排序看客户",
     "expect": "customers ← orders；中文时间口径 + 分组聚合 + 排序"},
    {"n": 3, "title": "退款金额最高的 10 个订单是哪些？",
     "desc": "退款金额 Top10 订单",
     "expect": "refunds → orders；Top-N 截断与金额字段选择"},
    {"n": 4, "title": "各商品类目的销售额排名",
     "desc": "按商品类目统计销售额并排名",
     "expect": "order_items → products；多表 JOIN 与类目归属"},
    {"n": 5, "title": "各客户分层的平均订单金额是多少？",
     "desc": "按客户分层看平均订单金额",
     "expect": "customers.segment 语义未定义；是否报语义缺口而非自行假设"},
]

TRACE9 = ["demand_id", "requirement_version", "schema_version", "pack_version",
          "generator_model", "generated_sql", "review_status", "validation_result",
          "final_delivery_sql"]


def main():
    url = os.getenv("GATEWAY_MCP_URL", "http://127.0.0.1:18080/mcp")
    cli = McpClient(url)
    print("=" * 78)
    print("A 库验收专项 · 5 条业务口吻样例全链路（dataset=A）")
    print("端点：%s" % url)
    print("=" * 78)
    made = []
    for s in SAMPLES:
        print("\n" + "-" * 74)
        print("样例 %d：%s" % (s["n"], s["title"]))
        print("  意图：%s" % s["expect"])
        payload = {
            "title": s["title"],
            "business_context": "电商平台日常经营分析（裸库场景）",
            "description": s["desc"],
            "expected_output": s["expect"],
            "contact": "验收组",
        }
        d = cli.call("demand_create", payload)
        did = d.get("demand_id")
        if not did:
            print("  ✗ demand_create 失败：%s" % json.dumps(d, ensure_ascii=False)[:160])
            continue
        made.append(did)
        print("  demand_id = %s" % did)

        a = cli.call("analysis_first_round", {"demand_id": did, "dataset": "A"})
        print("  1) 语义分析 ok=%s" % a.get("ok"))

        rs = cli.call("requirement_structured", {"demand_id": did, "dataset": "A"})
        print("  2) 结构化需求 contract_ok=%s  version=%s" % (rs.get("contract_ok"), rs.get("version")))

        sc = cli.call("schema_candidates", {"demand_id": did, "dataset": "A"})
        st = sc.get("subject_tables") or []
        print("  3) SCHEMA 候选表=%d 项；miss_reason=%s" % (len(st), str(sc.get("miss_reason"))[:110]))

        pack = cli.call("sql_context_pack", {"demand_id": did, "dataset": "A"})
        print("  4) 上下文包 pack_version=%s；table_candidates=%d；溯源=%s / r%s / rule_constraints=%d" % (
            pack.get("pack_version"), len(pack.get("table_candidates") or []),
            pack.get("schema_version"), pack.get("requirement_version"),
            len(pack.get("rule_constraints") or [])))

        pl = cli.call("sql_plan", {"demand_id": did, "dataset": "A"})
        print("  5) 计划草稿 chosen_table=%r；draft_gate=%s" % (
            pl.get("chosen_table"), (pl.get("draft_gate") or {}).get("status")))

        gen = cli.call("sql_generate", {"demand_id": did, "dataset": "A", "candidate_sql": None})
        sql = gen.get("sql_draft") or ""
        rv = gen.get("review") or {}
        print("  6) SQL 生成：%d 字符；门禁=%s；generator=%s" % (
            len(sql), rv.get("status"), gen.get("generator")))
        if sql:
            print("     SQL: %s" % sql[:160])
        if gen.get("generation_risks"):
            print("     风险/拒绝: %s" % str(gen.get("generation_risks"))[:160])
        if not sql:
            print("  7) 未生成 SQL —— 链路止于生成（如实记录，不补）")
            continue

        ex = cli.call("sql_execute_readonly", {"demand_id": did, "dataset": "A", "sql": sql})
        print("  7) 只读执行 row_count=%s；suspicious=%s" % (
            ex.get("row_count"), str(ex.get("suspicious_signals"))[:90]))

        rg = cli.call("sql_run_get", {"demand_id": did})
        runs = rg.get("runs") or []
        if runs:
            miss = [k for k in TRACE9 if runs[0].get(k) in (None, "", [], {})]
            print("  8) 留痕 %d 条；九字段缺失=%s" % (len(runs), miss or "无（9/9 齐全）"))

    print("\n" + "=" * 78)
    print("清理演示数据（%d 条）" % len(made))
    for did in made:
        try:
            r = cli.call("demand_set_status",
                         {"demand_id": did, "status": "已退回", "note": "A 库验收现场清理"})
            print("  %s -> %s" % (did, r.get("status")))
        except Exception as e:  # noqa: BLE001
            print("  %s 清理失败：%s" % (did, str(e)[:120]))


if __name__ == "__main__":
    main()
