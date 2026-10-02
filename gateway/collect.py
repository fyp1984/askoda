# -*- coding: utf-8 -*-
"""元数据采集实现（M2）

两个后端，产出同一份结构，写入同一批元数据表：

| 后端 | 实现 | 依赖 | 适用 |
|---|---|---|---|
| `native` | 直连 PG 查 `information_schema` + `pg_catalog` | psycopg | 任何环境（网关容器内亦可） |
| `schemacrawler` | 调 `schemacrawler/schemacrawler` 容器 `--command=schema --output-format=json` | 本机 docker + 镜像 | 宿主机 CLI，镜像可用时 |

**为什么两个都在**：SchemaCrawler 是方案指定的标准采集工具（多数据库方言、输出契约稳定）。
本机 Docker Hub 与 DaoCloud 镜像白名单均无法取得该镜像（实测见 M2 验收单），
故 M2 先用 native 后端完成采集与验收；镜像可用后 `--engine schemacrawler` 直接切换，
下游（元数据字典、语义理解）无需改动——两者产出的 `tables[]` 结构完全一致。
"""
import json
import os
import subprocess

import metadata
import registry

SCHEMACRAWLER_IMAGE = os.getenv("SCHEMACRAWLER_IMAGE", "schemacrawler/schemacrawler:v16.21.2")

# 物理结构查询（native 后端）
SQL_TABLES = """
SELECT c.relname AS table_name, obj_description(c.oid) AS comment, c.reltuples::bigint AS reltuples
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY c.relname
"""

SQL_COLUMNS = """
SELECT c.table_name, c.column_name, COALESCE(c.data_type, c.udt_name) AS data_type,
       (c.is_nullable = 'YES') AS nullable, c.column_default, c.ordinal_position
FROM information_schema.columns c
WHERE c.table_schema = 'public'
ORDER BY c.table_name, c.ordinal_position
"""

SQL_PK = """
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
"""

SQL_COMMENTS = """
SELECT c.relname AS table_name, a.attname AS column_name, d.description
FROM pg_description d
JOIN pg_class c ON c.oid = d.objoid
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
"""


# ---------------------------------------------------------------------------
# 后端一 · native
# ---------------------------------------------------------------------------
def collect_native(dsn, exact_count=True):
    """直连 PG 采集物理结构。

    `exact_count=True` 用精确 `count(*)`——模拟库体量小（百行级），精确值可直接对账；
    真实库请改 `False`，走 `reltuples` 统计估算，避免全表扫描。
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_TABLES)
            tables = cur.fetchall()

            cur.execute(SQL_PK)
            pks = {}
            for t, c in cur.fetchall():
                pks.setdefault(t, set()).add(c)

            cur.execute(SQL_COMMENTS)
            comments = {}
            for t, c, d in cur.fetchall():
                comments[(t, c)] = d

            cur.execute(SQL_COLUMNS)
            cols = {}
            for t, cn, dtype, nullable, default, _pos in cur.fetchall():
                cols.setdefault(t, []).append(
                    {
                        "name": cn,
                        "type": dtype,
                        "nullable": bool(nullable),
                        "default": default,
                        "comment": comments.get((t, cn)),
                    }
                )

            out = []
            for tname, comment, reltuples in tables:
                rows = None
                if exact_count:
                    try:
                        cur.execute('SELECT count(*) FROM "%s"' % tname)
                        rows = cur.fetchone()[0]
                    except Exception:
                        rows = int(reltuples) if reltuples and reltuples >= 0 else None
                else:
                    rows = int(reltuples) if reltuples and reltuples >= 0 else None
                out.append(
                    {
                        "table_name": tname,
                        "comment": comment,
                        "row_estimate": rows,
                        "columns": [
                            {**c, "primary_key": c["name"] in pks.get(tname, set())}
                            for c in cols.get(tname, [])
                        ],
                    }
                )
    return out


# ---------------------------------------------------------------------------
# 后端二 · schemacrawler
# ---------------------------------------------------------------------------
def collect_schemacrawler(target, workdir):
    """调 SchemaCrawler 容器采集，并把结果归一化成与 native 同构的 tables[]。

    两个实测踩到的点（都记在这里，免得下次重踩）：
    1. **必须显式 `--entrypoint sh` 并自行调 `/opt/schemacrawler/bin/schemacrawler.sh`**。
       镜像默认 ENTRYPOINT 是 CA 证书脚本，它 `exec "$@"`，直接传 `--server=...` 会被
       shell 当成自身选项，报 `illegal option --`。
    2. **`--command=schema` 不支持 JSON 输出**（官方仅 text/html）。要结构化数据必须用
       `--command=serialize --output-format=json`，输出的是一份 UUID 引用式对象图：
       表/列首次出现时内联完整对象，重复出现只留 UUID 字符串。
       因此**不能按 catalog.tables 顺序取列**——列对象统一在顶层 `all-table-columns` 里，
       用 `full-name`（`schema.table.column`）反推归属表最稳。
    """
    os.makedirs(workdir, exist_ok=True)
    script = (
        "/opt/schemacrawler/bin/schemacrawler.sh --server=postgresql "
        "--host=host.docker.internal --port=%d --database=%s --user=%s --password=%s "
        "--info-level=standard --command=serialize --output-format=json "
        "--output-file=/out/schema.json" % (
            target["port"], target["dbname"], target["user"], target["password"]
        )
    )
    cmd = [
        "docker", "run", "--rm",
        "--entrypoint", "sh",
        "-v", "%s:/out" % workdir,
        "--add-host", "host.docker.internal:host-gateway",
        SCHEMACRAWLER_IMAGE,
        "-c", script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            "SchemaCrawler 执行失败（exit %s）：%s"
            % (proc.returncode, (proc.stderr or proc.stdout or "")[-500:])
        )
    return _parse_schemacrawler_json(os.path.join(workdir, "schema.json"))


def _parse_schemacrawler_json(path):
    """把 SchemaCrawler 的 serialize JSON 归一化成 tables[]。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    # 列的 `column-data-type` 同样可能是 UUID 引用，先用 catalog 里的类型表建索引
    type_index = {}
    for t in (raw.get("catalog") or {}).get("column-data-types") or []:
        if isinstance(t, dict) and t.get("@uuid"):
            type_index[t["@uuid"]] = t.get("database-specific-type-name") or t.get("name")

    tables = {}
    for c in raw.get("all-table-columns") or []:
        if not isinstance(c, dict):
            continue
        parts = (c.get("full-name") or "").split(".")
        if len(parts) < 3:
            continue
        tname = parts[-2]
        dt = c.get("column-data-type")
        if isinstance(dt, dict):
            dtype = dt.get("database-specific-type-name") or dt.get("name")
        elif isinstance(dt, str):
            dtype = type_index.get(dt)
        else:
            dtype = None
        entry = tables.setdefault(
            tname, {"table_name": tname, "comment": None, "row_estimate": None, "columns": []}
        )
        entry["columns"].append(
            {
                "name": c.get("name"),
                "type": dtype,
                "nullable": c.get("nullable"),
                "default": c.get("default-value"),
                "comment": (c.get("remarks") or None),
                "primary_key": bool(c.get("part-of-primary-key")),
            }
        )

    # 表级附加信息（注释 / 基数）从 catalog.tables 里内联的完整对象取
    for t in (raw.get("catalog") or {}).get("tables") or []:
        if not isinstance(t, dict):
            continue
        entry = tables.get(t.get("name"))
        if not entry:
            continue
        entry["comment"] = t.get("remarks") or None
        card = (t.get("attributes") or {}).get("CARDINALITY")
        entry["row_estimate"] = int(card) if str(card).isdigit() else None

    for e in tables.values():
        e["columns"].sort(key=lambda x: (not x["primary_key"], x["name"]))
    return sorted(tables.values(), key=lambda x: x["table_name"])


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def collect(dataset_key, engine="native", seed_mdl=True, seed_glossary=False, workdir="/tmp/schemacrawler-out"):
    """采集并写入元数据字典，返回结果摘要（含一致性自检）。"""
    ds = registry.get(dataset_key)
    result = {"dataset": dataset_key, "engine": engine}

    if seed_mdl:
        result["mdl_seed"] = metadata.seed_from_mdl(dataset_key)
    if seed_glossary:
        result["glossary_seed"] = metadata.seed_glossary_from_mdl()

    if engine == "native":
        tables = collect_native(ds.pg_dsn)
    else:
        target = _target_from_dsn(ds.pg_dsn)
        tables = collect_schemacrawler(target, workdir)
        # 两条独立路径都跑一遍：SchemaCrawler 给结构与主键，native 给类型名并做交叉核对。
        # 这样每次采集都自带一次一致性验证，任一路径异常都会被立刻发现。
        ref = collect_native(ds.pg_dsn)
        result["cross_check"] = cross_check(ref, tables)
        filled = fill_types_from_ref(tables, ref)
        result["types_filled_from_native"] = filled

    result["import"] = metadata.import_physical(dataset_key, tables)

    # 一致性自检：MDL 可见字段是否都能在物理库里找到（找不到说明 MDL 写错了表名/字段名）
    physical = {(t["table_name"], c["name"]) for t in tables for c in t["columns"]}
    missing = []
    for m in ds.mdl.get("models", []):
        for c in m.get("columns", []):
            if (m.get("name"), c.get("name")) not in physical:
                missing.append("%s.%s" % (m.get("name"), c.get("name")))
    result["consistency"] = {
        "mdl_visible_fields": sum(len(m.get("columns", [])) for m in ds.mdl.get("models", [])),
        "missing_in_physical": missing,
        "ok": not missing,
    }
    return result


def cross_check(ref_tables, sc_tables):
    """两条独立采集路径的结构比对（表/列集合 + 主键判定）。"""
    ref = {(t["table_name"], c["name"]) for t in ref_tables for c in t["columns"]}
    sc = {(t["table_name"], c["name"]) for t in sc_tables for c in t["columns"]}
    ref_pk = {
        (t["table_name"], c["name"])
        for t in ref_tables for c in t["columns"] if c.get("primary_key")
    }
    sc_pk = {
        (t["table_name"], c["name"])
        for t in sc_tables for c in t["columns"] if c.get("primary_key")
    }
    only_nat = sorted("%s.%s" % k for k in ref - sc)
    only_sc = sorted("%s.%s" % k for k in sc - ref)
    pk_diff = sorted("%s.%s" % k for k in (ref_pk ^ sc_pk))
    return {
        "native_columns": len(ref),
        "schemacrawler_columns": len(sc),
        "only_in_native": only_nat,
        "only_in_schemacrawler": only_sc,
        "primary_key_mismatch": pk_diff,
        "consistent": not only_nat and not only_sc and not pk_diff,
        "note": "类型名以 native(information_schema) 为准；SchemaCrawler 的 serialize 格式类型对象为外部引用，不内联",
    }


def fill_types_from_ref(tables, ref_tables):
    """类型名缺失时用 native 结果补全，并标注来源。"""
    idx = {(t["table_name"], c["name"]): c.get("type") for t in ref_tables for c in t["columns"]}
    n = 0
    for t in tables:
        for c in t["columns"]:
            if not c.get("type"):
                v = idx.get((t["table_name"], c["name"]))
                if v:
                    c["type"] = v
                    c["type_source"] = "native(information_schema)"
                    n += 1
    return n


def _target_from_dsn(dsn):
    """postgresql://user:pwd@host:port/db → dict"""
    rest = dsn.split("://", 1)[-1]
    creds, hostpart = rest.split("@", 1)
    user, password = (creds.split(":", 1) + [""])[:2]
    hostport, dbname = hostpart.split("/", 1)
    host, port = (hostport.split(":", 1) + ["5432"])[:2]
    return {
        "user": user,
        "password": password,
        "host": host,
        "port": int(port),
        "dbname": dbname.split("?")[0],
    }
