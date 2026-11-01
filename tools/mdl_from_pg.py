# -*- coding: utf-8 -*-
"""从真实 PostgreSQL 库反向生成 Wren MDL

为什么要有这个脚本
------------------
`mdl.json` 一旦与真实表结构脱节，`rules.evaluate_all()` 会静默跳过不存在的列，
L2 静态规则整层失效却没人知道（见 gateway/mdl_check.py 的注释）。手抄 8 张表
几十个字段，抄错一个字段名就是一次静默失效。所以 MDL 一律从 information_schema
反向生成，表结构改了就重新生成，不靠人记。

用法
----
    PYTHONPATH=$PWD/tools/_pyshim:$PWD:$PYTHONPATH python tools/mdl_from_pg.py \
        --dsn "postgresql://test:test@127.0.0.1:15432/credit" \
        --catalog credit \
        --out wren-docker-c/workspace/mdl.json

    # 只预览不落盘
    ... --dry-run

字段说明来源优先级：列注释（COMMENT ON COLUMN）> 表注释 > 空。
所以建表时把中文名写进注释，这里就能直接生成可读的 MDL。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None


# PG 类型 → Wren 类型（Wren 只认少数几种，其余归一化到 varchar/numeric）
_TYPE_MAP = (
    ("int", "integer"),
    ("serial", "integer"),
    ("numeric", "numeric"),
    ("decimal", "numeric"),
    ("real", "numeric"),
    ("double", "numeric"),
    ("float", "numeric"),
    ("timestamp", "timestamp"),
    ("date", "date"),
    ("bool", "boolean"),
)


def _map_type(pg_type: str) -> str:
    t = (pg_type or "").lower()
    for needle, mapped in _TYPE_MAP:
        if needle in t:
            return mapped
    return "varchar"


_SQL_TABLES = """
SELECT n.nspname AS schema_name,
       c.relname AS table_name,
       obj_description(c.oid) AS table_comment
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = ANY(%(schemas)s)
   AND c.relkind = 'r'
 ORDER BY n.nspname, c.relname
"""

_SQL_COLUMNS = """
SELECT n.nspname AS schema_name,
       c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       col_description(c.oid, a.attnum) AS column_comment,
       a.attnotnull AS not_null,
       pg_get_expr(d.adbin, d.adrelid) AS default_expr
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid
  LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
 WHERE n.nspname = ANY(%(schemas)s)
   AND c.relkind = 'r'
   AND a.attnum > 0
   AND NOT a.attisdropped
 ORDER BY n.nspname, c.relname, a.attnum
"""

_SQL_PK = """
SELECT n.nspname AS schema_name,
       c.relname AS table_name,
       a.attname AS column_name
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
 WHERE n.nspname = ANY(%(schemas)s)
 ORDER BY n.nspname, c.relname, a.attnum
"""

# 外键 → Wren relationship。joinType 决定生成 SQL 时是 JOIN 还是 LEFT JOIN 的默认倾向。
_SQL_FK = """
SELECT con.conname AS name,
       tc.relname AS child_table,
       cs.nspname AS child_schema,
       ac.attname AS child_column,
       tp.relname AS parent_table,
       ps.nspname AS parent_schema,
       ap.attname AS parent_column
  FROM pg_constraint con
  JOIN pg_class tc ON tc.oid = con.conrelid
  JOIN pg_namespace cs ON cs.oid = tc.relnamespace
  JOIN pg_class tp ON tp.oid = con.confrelid
  JOIN pg_namespace ps ON ps.oid = tp.relnamespace
  JOIN pg_attribute ac ON ac.attrelid = tc.oid AND ac.attnum = con.conkey[1]
  JOIN pg_attribute ap ON ap.attrelid = tp.oid AND ap.attnum = con.confkey[1]
 WHERE con.contype = 'f'
   AND cs.nspname = ANY(%(schemas)s)
 ORDER BY tc.relname, con.conname
"""


def build(dsn: str, catalog: str, schemas: list[str], relations_csv: str | None = None) -> dict:
    if psycopg is None:
        raise SystemExit("psycopg 不可用，无法连库")
    params = {"schemas": schemas}

    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL_TABLES, params)
            tables = cur.fetchall()
            cur.execute(_SQL_COLUMNS, params)
            columns = cur.fetchall()
            cur.execute(_SQL_PK, params)
            pks = cur.fetchall()
            cur.execute(_SQL_FK, params)
            fks = cur.fetchall()

    pk_map: dict[tuple[str, str], list[str]] = {}
    for sname, tname, cname in pks:
        pk_map.setdefault((sname, tname), []).append(cname)

    col_map: dict[tuple[str, str], list[tuple]] = {}
    for row in columns:
        col_map.setdefault((row[0], row[1]), []).append(row)

    models = []
    for sname, tname, tcomment in tables:
        cols = col_map.get((sname, tname), [])
        if not cols:
            continue
        pk_cols = pk_map.get((sname, tname), [])
        entry_cols = []
        for _s, _t, cname, dtype, ccomment, not_null, default_expr in cols:
            desc = ccomment or ""
            bits = []
            if desc:
                bits.append(desc)
            if not_null:
                bits.append("非空")
            item = {
                "name": cname,
                "type": _map_type(dtype),
            }
            if cname in pk_cols:
                item["isPrimaryKey"] = True
            item["description"] = "；".join(bits) if bits else cname
            entry_cols.append(item)

        model = {
            "name": tname,
            "tableReference": {
                "catalog": catalog,
                "schema": sname,
                "table": tname,
            },
            "columns": entry_cols,
            "description": tcomment or tname,
        }
        if pk_cols:
            model["primaryKey"] = pk_cols[0]
        models.append(model)

    relationships = []
    for name, child_t, child_s, child_c, parent_t, parent_s, parent_c in fks:
        relationships.append(
            {
                "name": name,
                "models": [child_t, parent_t],
                "joinType": "MANY_TO_ONE",
                "condition": "%s.%s = %s.%s" % (child_t, child_c, parent_t, parent_c),
            }
        )

    # 库里没有物理外键时（本项目的 credit 库就是这种），关系来自知识库 03_表关联知识库，
    # 由 tools/credit_ddl_from_kb.py --relations-out 导出成 CSV 再喂进来。
    if relations_csv and os.path.exists(relations_csv):
        import csv
        model_names = {m["name"] for m in models}
        seen = set()
        with open(relations_csv, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                lt = row["left_table"].split(".")[-1]
                rt = row["right_table"].split(".")[-1]
                if lt not in model_names or rt not in model_names:
                    continue
                lc, rc = row["left_column"], row["right_column"]
                key = (lt, lc, rt, rc)
                if key in seen:
                    continue
                seen.add(key)
                relationships.append({
                    "name": "%s_%s" % (lt, rt),
                    "models": [lt, rt],
                    "joinType": row.get("join_type") or "MANY_TO_ONE",
                    "condition": "%s.%s = %s.%s" % (lt, lc, rt, rc),
                })

    return {
        "catalog": catalog,
        "schema": schemas[0],
        "dataSource": "postgres",
        "models": models,
        "relationships": relationships,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 PostgreSQL 反向生成 Wren MDL")
    ap.add_argument("--dsn", required=True, help="postgresql://user:pwd@host:port/dbname")
    ap.add_argument("--catalog", required=True, help="MDL catalog 名，一般与库名一致")
    ap.add_argument("--schemas", default="public", help="逗号分隔，如 public,dwd,dwm")
    ap.add_argument("--relations-csv", default="", help="知识库导出的关系 CSV")
    ap.add_argument("--out", help="输出 mdl.json 路径；不给则打印到 stdout")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    schemas = [x.strip() for x in args.schemas.split(",") if x.strip()]
    mdl = build(args.dsn, args.catalog, schemas, args.relations_csv or None)
    n_models = len(mdl["models"])
    n_cols = sum(len(m["columns"]) for m in mdl["models"])
    n_rel = len(mdl["relationships"])

    if n_models == 0:
        print("❌ 库里没有任何表（schemas=%s），拒绝生成空 MDL" % ",".join(schemas))
        return 2

    print("表 %d 张 / 字段 %d 个 / 关系 %d 条" % (n_models, n_cols, n_rel))
    for m in mdl["models"]:
        print("  - %-28s %2d 列  %s" % (m["name"], len(m["columns"]), m["description"][:30]))

    if args.dry_run:
        print("（dry-run，未写文件）")
        return 0

    text = json.dumps(mdl, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("已写入 %s" % args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
