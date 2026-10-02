# -*- coding: utf-8 -*-
"""元数据字典（M2 · PRD §10.2 / 最终技术方案 §4.1）

两张来源合流
------------
| 来源 | 提供什么 | 采集方式 |
|---|---|---|
| **MDL 语义层** | 中文名、口径、颗粒度、关联关系（**AI 可见闭集**） | 直接读 `mdl.json`，`seed_from_mdl()` |
| **物理库结构** | 数据类型、可空性、真实字段全集 | SchemaCrawler 采集，`import_physical()` |

为什么要合流：MDL 里**没有**的字段，是项目刻意不对 AI 开放的（敏感字段未建模）。
只看 MDL 会以为"表就这些列"；只看物理库会把敏感列当成普通列。合流后才能回答
「这张表有哪些列、哪些列对 AI 可见、为什么不可见」。

按表查中文名与口径（M2 验收标准之一）
-------------------------------------
`lookup(table="dwd_order_di", dataset="B")` 返回表中文名、颗粒度、字段清单
（含中文名 / 口径 / 类型 / 是否对 AI 可见 / 敏感标记）与关联路径。
"""
import db
import registry

# 口径分隔符：MDL description 里用「；口径：」把"中文名(单位)"与"计算口径"分开
CALIBER_MARKERS = ["；口径：", "; 口径：", "；口径:", "口径："]


def split_label_caliber(desc):
    """把 MDL 的 description 拆成 (中文名, 口径)。"""
    if not desc:
        return None, None
    for mk in CALIBER_MARKERS:
        if mk in desc:
            head, tail = desc.split(mk, 1)
            return head.strip(), tail.strip()
    return desc.strip(), None


# ---------------------------------------------------------------------------
# 播种：MDL → 字典
# ---------------------------------------------------------------------------
def seed_from_mdl(dataset_key):
    """把 MDL 的语义（中文名/口径/颗粒度）写入字典。幂等：同键覆盖语义列，不动物理列。"""
    ds = registry.get(dataset_key)
    mdl = ds.mdl
    models = mdl.get("models", [])
    relationships = mdl.get("relationships", [])

    n_t, n_c = 0, 0
    for m in models:
        tname = m.get("name")
        cols = m.get("columns", [])
        # 表级：颗粒度由主键推定。MDL 的 primaryKey 既可能是字符串（单主键），
        # 也可能是数组（复合主键）——统一成列表，否则 "order_id" 会被逐字符拆开
        pk_raw = m.get("primaryKey")
        pk = [pk_raw] if isinstance(pk_raw, str) else list(pk_raw or [])
        grain = "一行 = 一条 %s 记录（主键 %s）" % (tname, "、".join(pk) if pk else "未声明")
        desc_parts = [
            c.get("description", "") for c in cols[:3] if c.get("description")
        ]
        db.execute(
            """
            INSERT INTO table_docs (dataset, table_name, table_label, domain, grain, description, source)
            VALUES (%s,%s,%s,%s,%s,%s,'mdl')
            ON CONFLICT (dataset, table_name) DO UPDATE SET
                table_label = COALESCE(EXCLUDED.table_label, table_docs.table_label),
                domain      = COALESCE(EXCLUDED.domain, table_docs.domain),
                grain       = EXCLUDED.grain,
                description = COALESCE(EXCLUDED.description, table_docs.description)
            """,
            (
                dataset_key,
                tname,
                ds.label + " · " + tname,
                ds.domain,
                grain,
                "字段摘要：" + "；".join(desc_parts)[:300],
            ),
        )
        n_t += 1
        for c in cols:
            label, caliber = split_label_caliber(c.get("description"))
            db.execute(
                """
                INSERT INTO column_docs
                    (dataset, table_name, column_name, column_label, data_type, nullable,
                     is_sensitive, is_primary_key, description, source)
                VALUES (%s,%s,%s,%s,%s,%s,false,%s,%s,'mdl')
                ON CONFLICT (dataset, table_name, column_name) DO UPDATE SET
                    column_label = EXCLUDED.column_label,
                    data_type    = COALESCE(EXCLUDED.data_type, column_docs.data_type),
                    is_primary_key = EXCLUDED.is_primary_key,
                    is_sensitive = false,
                    description  = EXCLUDED.description,
                    source       = CASE WHEN column_docs.source = 'schemacrawler'
                                        THEN 'mdl+schemacrawler' ELSE 'mdl' END
                """,
                (
                    dataset_key,
                    tname,
                    c.get("name"),
                    label,
                    c.get("type"),
                    True,  # MDL 未声明 nullable，占位为 True，物理采集会覆盖
                    bool(c.get("isPrimaryKey") or c.get("name") in pk),
                    c.get("description"),
                ),
            )
            n_c += 1

    # 关联关系写入表级 description 尾部，便于按表查关系
    rel_by_table = {}
    for r in relationships:
        cond = r.get("condition", "")
        for t in r.get("models", []):
            rel_by_table.setdefault(t, []).append(
                "%s(%s)" % (r.get("name"), r.get("joinType", ""))
            )
    for t, rels in rel_by_table.items():
        db.execute(
            "UPDATE table_docs SET description = COALESCE(description,'') || %s "
            "WHERE dataset=%s AND table_name=%s",
            (" ｜ 关联：" + "、".join(rels[:6]), dataset_key, t),
        )
    return {"dataset": dataset_key, "tables": n_t, "columns": n_c, "relationships": len(relationships)}


# ---------------------------------------------------------------------------
# 导入：SchemaCrawler → 字典
# ---------------------------------------------------------------------------
def import_physical(dataset_key, tables):
    """导入物理结构。

    tables 形如：
      [{"table_name": "...", "row_estimate": 123, "comment": "...",
        "columns": [{"name","type","nullable","default","comment","primary_key"}]}, ...]

    **关键判定**：物理存在但 MDL 未建模的列 → 标记 `is_sensitive = true`，
    并在 description 注明「未建模：对 AI 不可见」。这是项目「敏感字段不建模」
    约定的元数据侧体现，也是 §9.2 入口风控的字典依据。
    """
    ds = registry.get(dataset_key)
    visible = set()
    for m in ds.mdl.get("models", []):
        for c in m.get("columns", []):
            visible.add((m.get("name"), c.get("name")))

    n_t, n_c, n_hidden = 0, 0, 0
    for t in tables:
        tname = t["table_name"]
        db.execute(
            """
            INSERT INTO table_docs (dataset, table_name, row_estimate, collected_at, source)
            VALUES (%s,%s,%s,now(),'schemacrawler')
            ON CONFLICT (dataset, table_name) DO UPDATE SET
                row_estimate = COALESCE(EXCLUDED.row_estimate, table_docs.row_estimate),
                collected_at = now(),
                source = CASE WHEN table_docs.source LIKE '%%mdl%%'
                              THEN 'mdl+schemacrawler' ELSE 'schemacrawler' END
            """,
            (dataset_key, tname, t.get("row_estimate")),
        )
        n_t += 1
        for c in t.get("columns", []):
            cname = c.get("name")
            hidden = (tname, cname) not in visible
            note = c.get("comment")
            if hidden:
                n_hidden += 1
                note = (note + " " if note else "") + "【未建模：对 AI 不可见】"
            db.execute(
                """
                INSERT INTO column_docs
                    (dataset, table_name, column_name, data_type, nullable, is_sensitive,
                     is_primary_key, description, collected_at, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),'schemacrawler')
                ON CONFLICT (dataset, table_name, column_name) DO UPDATE SET
                    data_type    = EXCLUDED.data_type,
                    nullable     = EXCLUDED.nullable,
                    is_primary_key = EXCLUDED.is_primary_key,
                    is_sensitive = CASE WHEN column_docs.source LIKE '%%mdl%%'
                                        THEN column_docs.is_sensitive
                                        ELSE EXCLUDED.is_sensitive END,
                    description  = COALESCE(column_docs.description, EXCLUDED.description),
                    collected_at = now(),
                    source = CASE WHEN column_docs.source LIKE '%%mdl%%'
                                  THEN 'mdl+schemacrawler' ELSE 'schemacrawler' END
                """,
                (
                    dataset_key,
                    tname,
                    cname,
                    c.get("type"),
                    c.get("nullable"),
                    hidden,
                    bool(c.get("primary_key") or c.get("primaryKey")),
                    note,
                ),
            )
            n_c += 1
    return {
        "dataset": dataset_key,
        "tables": n_t,
        "columns": n_c,
        "not_modelled_hidden": n_hidden,
    }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def lookup(table=None, dataset=None, keyword=None, include_hidden=False):
    """按表或关键词查元数据字典。

    - `table` 给定时：返回该表的表级信息 + 字段清单（默认**只返回对 AI 可见的列**，
      `include_hidden=True` 才带出未建模列，且明确标注其对 AI 不可见）。
    - `keyword` 给定时：在表名/中文名/字段名/中文名/口径里模糊匹配。
    """
    params, where = [], []
    if dataset:
        params.append(dataset)
        where.append("dataset = %s")
    if table:
        params.append(table)
        where.append("table_name = %s")
    if keyword:
        params.append("%" + keyword + "%")
        where.append(
            "(table_name ILIKE %s OR COALESCE(table_label,'') ILIKE %s "
            "OR COALESCE(description,'') ILIKE %s)"
        )
        params.extend(["%" + keyword + "%"] * 2)

    sql = "SELECT * FROM table_docs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY dataset, table_name"
    tables = db.query(sql, params)

    out = []
    for t in tables:
        cwhere = ["dataset = %s", "table_name = %s"]
        cparams = [t["dataset"], t["table_name"]]
        if not include_hidden:
            cwhere.append("NOT (COALESCE(description,'') LIKE '%%未建模：对 AI 不可见%%')")
        cols = db.query(
            "SELECT column_name, column_label, data_type, nullable, is_sensitive, "
            "       is_primary_key, description, source "
            "FROM column_docs WHERE %s ORDER BY is_primary_key DESC, column_name"
            % " AND ".join(cwhere),
            cparams,
        )
        for c in cols:
            c["ai_visible"] = "未建模：对 AI 不可见" not in (c.get("description") or "")
        out.append(
            {
                "dataset": t["dataset"],
                "table_name": t["table_name"],
                "table_label": t["table_label"],
                "domain": t["domain"],
                "grain": t["grain"],
                "row_estimate": t["row_estimate"],
                "source": t["source"],
                "columns": cols,
                "column_count": len(cols),
                "hidden_column_count": sum(1 for c in cols if not c["ai_visible"]),
            }
        )
    return {"ok": True, "returned": len(out), "tables": out}


def glossary(term=None, keyword=None, dataset=None):
    """业务口径词表查询。"""
    where, params = [], []
    if term:
        where.append("term = %s")
        params.append(term)
    if keyword:
        where.append("(term ILIKE %s OR definition ILIKE %s)")
        params.extend(["%" + keyword + "%"] * 2)
    if dataset:
        where.append("(dataset = %s OR dataset IS NULL)")
        params.append(dataset)
    sql = "SELECT term, definition, formula, dataset, source FROM business_glossary"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY term"
    return {"ok": True, "terms": db.query(sql, params)}


def upsert_glossary(term, definition, formula=None, dataset=None, source="manual"):
    db.execute(
        """
        INSERT INTO business_glossary (term, definition, formula, dataset, source, updated_at)
        VALUES (%s,%s,%s,%s,%s,now())
        ON CONFLICT (term) DO UPDATE SET
            definition = EXCLUDED.definition,
            formula    = COALESCE(EXCLUDED.formula, business_glossary.formula),
            dataset    = COALESCE(EXCLUDED.dataset, business_glossary.dataset),
            source     = EXCLUDED.source,
            updated_at = now()
        """,
        (term, definition, formula, dataset, source),
    )
    return {"ok": True, "term": term}


def seed_glossary_from_mdl():
    """从 MDL 的字段口径里抽取含"口径："的条目，播种业务口径词表。"""
    n = 0
    for key in registry.DATASETS:
        ds = registry.get(key)
        for m in ds.mdl.get("models", []):
            for c in m.get("columns", []):
                _label, caliber = split_label_caliber(c.get("description"))
                if caliber and len(caliber) >= 6:
                    upsert_glossary(
                        term="%s.%s" % (m.get("name"), c.get("name")),
                        definition=caliber,
                        dataset=key,
                        source="mdl",
                    )
                    n += 1
    return {"seeded": n}
