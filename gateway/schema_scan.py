# -*- coding: utf-8 -*-
"""Schema 扫描与稳定版本化（M4-1）

为什么单独一层
--------------
后续所有 SQL 生成链路的溯源字段（requirement_version / schema_version / pack_version）
必须指向"哪一版 Schema 下生成了这条 SQL"。若没有稳定的 schema_version，
两次相同 SQL 对不上 Schema 变更，复现口径就无从谈起——所以版本号
必须是"纯内容哈希"，不含时间戳、采集顺序等随机成分。

版本号规则（硬约束）
-------------------
对按表名排序、按列名排序的 "表名.列名:类型" 逐行拼接后取 sha256，
前 8 位 = schema_version，完整 64 位 = digest。

为什么不引入 SchemaCrawler
--------------------------
M4 一期只在本地 PG 上跑，native 后端（information_schema）已能给出类型与主键，
而 MDL 有中文名和颗粒度。两套合流产出的结构，足够支撑 sql_context_pack
的主题表/关联路径/时间字段候选，不必再加一阶段。
"""
import datetime as dt
import hashlib

import db
import collect as collect_mod
import metadata as metadata_mod
import registry


def _merge(dataset_key):
    """合流 MDL（中文名、主键、颗粒度）与 native（类型、行数）。
    MDL 里有的表按 MDL 主键/中文名优先；仅物理存在但未建模的表追加在后面。
    """
    ds = registry.get(dataset_key)

    native_tables = collect_mod.collect_native(ds.pg_dsn, exact_count=False)
    native_index = {}
    for t in native_tables:
        col_idx = {}
        for c in t["columns"]:
            col_idx[c["name"]] = c
        t["_cols"] = col_idx
        native_index[t["table_name"]] = t

    mdl_models = ds.mdl.get("models", [])
    mdl_index = {}
    for m in mdl_models:
        mname = m.get("name")
        pk_raw = m.get("primaryKey")
        pk = [pk_raw] if isinstance(pk_raw, str) else list(pk_raw or [])
        cols_idx = {}
        for c in m.get("columns", []):
            cols_idx[c["name"]] = c
        mdl_index[mname] = {
            "pk": pk,
            "cols": cols_idx,
            "label": ds.label + " . " + mname,
            "grain": "一行 = 一条 %s 记录（主键 %s）" % (mname, "、".join(pk) if pk else "未声明"),
        }

    out = []
    seen = set()

    for mname in sorted(mdl_index):
        m = mdl_index[mname]
        nt = native_index.get(mname)
        row_estimate = nt["row_estimate"] if nt else None
        label = m["label"]

        cols_out = []
        for cname in sorted(m["cols"]):
            c = m["cols"][cname]
            nc = nt["_cols"].get(cname, {}) if nt else {}
            label_c, _caliber = metadata_mod.split_label_caliber(c.get("description"))
            cols_out.append({
                "name": cname,
                "type": (nc.get("type") or c.get("type") or "text"),
                "label": label_c or cname,
                "is_pk": bool(c.get("isPrimaryKey") or cname in m["pk"]),
            })
        out.append({
            "name": mname,
            "label": label,
            "grain": m["grain"],
            "row_estimate": row_estimate,
            "columns": cols_out,
        })
        seen.add(mname)

    for tname in sorted(native_index):
        if tname in seen:
            continue
        nt = native_index[tname]
        cols_out = []
        for cname in sorted(nt["_cols"]):
            nc = nt["_cols"][cname]
            cols_out.append({
                "name": cname,
                "type": nc.get("type") or "text",
                "label": cname,
                "is_pk": bool(nc.get("primary_key")),
            })
        out.append({
            "name": tname,
            "label": tname,
            "grain": None,
            "row_estimate": nt.get("row_estimate"),
            "columns": cols_out,
        })

    out.sort(key=lambda x: x["name"])
    return out


def _digest(tables):
    """按表名升序、列名升序的 "表名.列名:类型" 逐行拼接算 sha256。
    顺序与分隔符都是版本号稳定性的关键：一旦变了 digest 就变。
    """
    lines = []
    for t in sorted(tables, key=lambda x: x["name"]):
        for c in sorted(t["columns"], key=lambda x: x["name"]):
            lines.append("%s.%s:%s" % (t["name"], c["name"], c.get("type") or "text"))
    blob = "\n".join(lines)
    h = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return h[:8], h


def latest_version(dataset_key):
    """查 schema_snapshots 表最近一条（按 collected_at 倒序）。
    没有任何快照时返回 None。
    """
    row = db.query_one(
        "SELECT dataset, schema_version, digest, source, collected_at::text AS collected_at, "
        "       jsonb_array_length(coalesce(payload->'tables','[]'::jsonb)) AS table_count, "
        "       (SELECT coalesce(sum(jsonb_array_length(t->'columns')),0) "
        "        FROM jsonb_array_elements(coalesce(payload->'tables','[]'::jsonb)) t) AS column_count "
        "FROM schema_snapshots WHERE dataset=%s ORDER BY collected_at DESC LIMIT 1",
        (dataset_key,),
    )
    if not row:
        return None
    return {
        "dataset": row["dataset"],
        "schema_version": row["schema_version"],
        "digest": row["digest"],
        "source": row["source"],
        "collected_at": row["collected_at"],
        "counts": {
            "tables": row["table_count"] or 0,
            "columns": row["column_count"] or 0,
        },
    }


def scan(dataset_key, persist=True):
    """采集 Schema 快照并算稳定版本号。

    同一库两次调用 digest 必定相同（纯内容哈希，不含时间/采集顺序）。
    persist=True 时写入 schema_snapshots，主键 dataset+schema_version，重复扫描幂等。
    """
    tables = _merge(dataset_key)
    schema_version, digest = _digest(tables)
    collected_at = dt.datetime.now().isoformat()
    counts = {
        "tables": len(tables),
        "columns": sum(len(t["columns"]) for t in tables),
    }

    result = {
        "dataset": dataset_key,
        "schema_version": schema_version,
        "digest": digest,
        "source": "mdl+native",
        "collected_at": collected_at,
        "tables": tables,
        "counts": counts,
    }

    if persist:
        db.execute(
            """
            INSERT INTO schema_snapshots
                (dataset, schema_version, digest, payload, source, collected_at)
            VALUES (%s,%s,%s,%s,%s,now())
            ON CONFLICT (dataset, schema_version) DO UPDATE SET
                digest = EXCLUDED.digest,
                payload = EXCLUDED.payload,
                source = EXCLUDED.source,
                collected_at = now()
            """,
            (
                dataset_key,
                schema_version,
                digest,
                db.dumps({"tables": tables, "counts": counts}),
                "mdl+native",
            ),
        )

    return result
