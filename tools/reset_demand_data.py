#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收前重置「需求域过程数据」（不动资产与配置）。

为什么要有这个工具
------------------
人工验收必须从**干净起点**开始。历史测试数据不清会带来三个具体问题：
  ① 需求列表里 184 条同名记录 ⇒ 业务方第一眼看到的就是"这系统很乱"；
  ② 「相似需求预检」会命中一堆历史 ⇒ 新需求一提交就弹出无关的相似项；
  ③ 看板/统计口径被历史污染 ⇒ 说不清"今天新提了几条"。

⚠️ 只清「过程数据」，**绝不动资产与配置**：
  · 过程数据（清）：demand_requests / demand_events / analysis_rounds /
    confirmations / structured_requirements / sql_runs / knowledge_citations
  · 资产与配置（保留）：business_glossary（业务口径）/ column_docs + table_docs（数据字典）/
    schema_snapshots（语义层版本）/ config_events（配置审计）/ llm_settings（大模型配置）
  理由：资产是**知识沉淀**，清掉等于把已完成的治理工作倒回原点；
        过程数据是**运行痕迹**，只对当次验收有意义。

🔴 这张库里**没有任何外键约束**（实测 information_schema 返回 0 条）
⇒ 删主表不会级联删从属表，只会留下**孤儿数据**（看起来清干净了，其实没清）。
  所以本工具**逐表删**，并在删完做三件事：
  ① 复查七张表行数必须为 0；
  ② 用 `NOT EXISTS` 反查孤儿（从属表里指向不存在需求的记录）；
  ③ 两件都过才算成功 —— 任一条不满足，整体回滚（一个事务内）。

用法
----
    python3 tools/reset_demand_data.py              # 只预览，什么都不删
    python3 tools/reset_demand_data.py --yes        # 确认后真删（单事务）
    python3 tools/reset_demand_data.py --yes --keep-sample   # 保留每类各 1 条做对照
"""
import argparse
import os
import sys

# 过程表：按「从属 → 主表」的顺序清（无外键，顺序只影响孤儿窗口期，不影响结果）
PROCESS_TABLES = [
    "knowledge_citations",
    "sql_runs",
    "structured_requirements",
    "confirmations",
    "analysis_rounds",
    "demand_events",
    "demand_requests",
]

# 资产与配置表：本工具**永不**触碰，仅用于最后自证"一行没少"
ASSET_TABLES = [
    "business_glossary",
    "column_docs",
    "table_docs",
    "schema_snapshots",
    "config_events",
    "llm_settings",
]

DEFAULT_DSN = "postgresql://assistant:assistant@127.0.0.1:15434/assistant"


def _dsn():
    return (os.getenv("ASSISTANT_DB_DSN") or DEFAULT_DSN).strip()


def _counts(cur, tables):
    out = {}
    for t in tables:
        cur.execute('SELECT count(*) FROM public."%s"' % t)
        out[t] = cur.fetchone()[0]
    return out


def _orphans(cur):
    """从属表里指向「已不存在的需求」的记录数。

    ⚠️ 这是本工具存在的核心理由：没有外键，删除"成功"了也可能留一地孤儿。
    """
    checks = {
        "demand_events": "SELECT count(*) FROM demand_events e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
        "analysis_rounds": "SELECT count(*) FROM analysis_rounds e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
        "confirmations": "SELECT count(*) FROM confirmations e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
        "structured_requirements": "SELECT count(*) FROM structured_requirements e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
        "sql_runs": "SELECT count(*) FROM sql_runs e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
        "knowledge_citations": "SELECT count(*) FROM knowledge_citations e WHERE NOT EXISTS (SELECT 1 FROM demand_requests d WHERE d.demand_id = e.demand_id)",
    }
    out = {}
    for k, sql in checks.items():
        cur.execute(sql)
        out[k] = cur.fetchone()[0]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="验收前重置需求域过程数据")
    ap.add_argument("--yes", action="store_true", help="真正执行（不带此参数只预览）")
    ap.add_argument("--keep-sample", action="store_true",
                    help="保留最近 1 条已通过的需求及其从属记录，便于对照演示")
    args = ap.parse_args(argv)

    try:
        import psycopg
    except ImportError:
        print("缺少 psycopg：本工具需在具备 psycopg 的解释器下运行", file=sys.stderr)
        return 2

    dsn = _dsn()
    with psycopg.connect(dsn) as conn:
        cur = conn.cursor()
        before = _counts(cur, PROCESS_TABLES)
        before_asset = _counts(cur, ASSET_TABLES)
        before_orphan = _orphans(cur)

        print("=" * 74)
        print("验收前重置 · 需求域过程数据")
        print("=" * 74)
        print("将清空的表：")
        for t in PROCESS_TABLES:
            print("  %-26s %6d 行" % (t, before[t]))
        print("  ── 合计 %d 行" % sum(before.values()))
        print()
        print("保持不动（资产与配置）：")
        for t in ASSET_TABLES:
            print("  %-26s %6d 行" % (t, before_asset[t]))
        if any(before_orphan.values()):
            print()
            print("⚠️ 当前已存在的孤儿数据（指向不存在需求的记录）：")
            for k, v in before_orphan.items():
                if v:
                    print("    %-26s %d 行" % (k, v))

        if not args.yes:
            print()
            print("—— 以上为预览。加 --yes 才会真正删除（单事务，失败整体回滚）。")
            print("—— 备份命令（清理前务必先跑一次）：")
            print("   pg_dump '%s' -Fc -f <备份目录>/meta_full.dump" % dsn)
            return 0

        keep_ids = []
        if args.keep_sample:
            cur.execute("SELECT demand_id FROM demand_requests WHERE status='已通过' "
                        "ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                keep_ids = [row[0]]
                print()
                print("保留样本：%s" % keep_ids[0])

        try:
            for t in PROCESS_TABLES:
                if keep_ids and t in ("demand_requests", "demand_events", "analysis_rounds",
                                      "confirmations", "structured_requirements", "sql_runs",
                                      "knowledge_citations"):
                    # ⚠️ `demand_id IS NULL` 必须显式包含：NULL <> ALL(...) 结果是 NULL（不是
                    #   true），那些行会**静默留下来**。这种"删了但没删干净"正是本工具要防的。
                    cur.execute('DELETE FROM public."%s" WHERE demand_id IS NULL '
                                'OR demand_id <> ALL(%%s)' % t, (keep_ids,))
                else:
                    cur.execute('DELETE FROM public."%s"' % t)
                print("  已清 %-26s 影响 %d 行" % (t, cur.rowcount))

            # 自证 ①：七张表必须归零（保留样本时，只剩该样本的行）
            after = _counts(cur, PROCESS_TABLES)
            after_asset = _counts(cur, ASSET_TABLES)
            after_orphan = _orphans(cur)

            problems = []
            for t in PROCESS_TABLES:
                if not keep_ids and after[t] != 0:
                    problems.append("%s 仍有 %d 行" % (t, after[t]))
            for t in ASSET_TABLES:
                if after_asset[t] != before_asset[t]:
                    problems.append("资产表 %s 行数从 %d 变成 %d（不该变）"
                                    % (t, before_asset[t], after_asset[t]))
            # 自证 ②：孤儿必须为 0
            for k, v in after_orphan.items():
                if v:
                    problems.append("%s 存在 %d 条孤儿记录（指向不存在的需求）" % (k, v))

            if problems:
                conn.rollback()
                print()
                print("❌ 自证未通过，已整体回滚：")
                for p in problems:
                    print("   - %s" % p)
                return 1

            conn.commit()
            print()
            print("✅ 自证通过：过程表已清空、资产表一行未动、孤儿记录 0 条")
            print("   保留样本：%s" % (keep_ids[0] if keep_ids else "无"))
            return 0
        except Exception:
            conn.rollback()
            raise


if __name__ == "__main__":
    sys.exit(main())
