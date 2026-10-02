# -*- coding: utf-8 -*-
"""网关自有元数据库（PostgreSQL）访问层

承载什么
--------
1. **需求单与流转事件**：`demand_requests` / `demand_events`（M2 需求受理落库）。
2. **轻量元数据字典**：`table_docs` / `column_docs` / `business_glossary`
   （M2 元数据采集，供语义理解与口径解释引用）。

为什么不放进模拟库 A/B
----------------------
A / B 是**被分析的业务数据源**，本库是网关自身的**运行元数据**。两者混在一起，
P0 硬指标「换库 0 行代码」立刻失效——换数据源时会把网关自己的单据一起搬走。

连接策略
--------
按线程缓存连接并带健康检查；PG 未就绪时给明确报错而不是抛裸异常，便于 `/healthz`
把元数据库也纳入探活。
"""
import json
import os
import threading
import time

try:
    import psycopg
except ImportError:  # 本地无驱动时允许模块导入，功能调用时才报错
    psycopg = None


DSN = os.getenv(
    "ASSISTANT_DB_DSN",
    "postgresql://assistant:assistant@127.0.0.1:15434/assistant",
)

_local = threading.local()


class DBError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------
def _connect():
    if psycopg is None:
        raise DBError("未安装 psycopg（容器内应随 requirements.txt 安装）")
    return psycopg.connect(DSN, autocommit=True, connect_timeout=10)


def conn():
    """取当前线程的连接；已断开则重连。"""
    c = getattr(_local, "conn", None)
    if c is None or getattr(c, "closed", True):
        c = _connect()
        _local.conn = c
        return c
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        c = _connect()
        _local.conn = c
    return c


def query(sql, params=None):
    with conn().cursor() as cur:
        cur.execute(sql, params or ())
        if cur.description is None:
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    with conn().cursor() as cur:
        cur.execute(sql, params or ())
        return cur.rowcount


def healthy():
    t0 = time.time()
    try:
        row = query_one("SELECT current_database() AS db, version() AS v")
        return {
            "ok": True,
            "database": row["db"],
            "endpoint": DSN.split("@")[-1],
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "endpoint": DSN.split("@")[-1],
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "ms": int((time.time() - t0) * 1000),
        }


# ---------------------------------------------------------------------------
# 表结构（幂等）
# ---------------------------------------------------------------------------
DDL = [
    # 需求单主表。正文保存原文（受控），普通读取返回掩码版（见 demand.get）
    """
    CREATE TABLE IF NOT EXISTS demand_requests (
        demand_id           TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        business_context    TEXT NOT NULL,
        description         TEXT NOT NULL,
        expected_output     TEXT NOT NULL,
        contact             TEXT NOT NULL,
        time_range          TEXT,
        expected_finish_at  TEXT,
        status              TEXT NOT NULL DEFAULT '待分析',
        sensitivity         JSONB NOT NULL DEFAULT '{}'::jsonb,
        masked_fields       JSONB NOT NULL DEFAULT '[]'::jsonb,
        attachments         JSONB NOT NULL DEFAULT '[]'::jsonb,
        validation          JSONB NOT NULL DEFAULT '{}'::jsonb,
        similar_requests    JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        version             INTEGER NOT NULL DEFAULT 1
    )
    """,
    # 流转与访问留痕。回退确认必须保留问题/答复/时间/版本，不允许覆盖历史
    """
    CREATE TABLE IF NOT EXISTS demand_events (
        event_id    BIGSERIAL PRIMARY KEY,
        demand_id   TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        actor       TEXT NOT NULL DEFAULT 'system',
        detail      JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_demand_events_did ON demand_events (demand_id, event_id)",
    "CREATE INDEX IF NOT EXISTS idx_demand_status ON demand_requests (status, created_at DESC)",
    # 元数据字典 · 表级
    """
    CREATE TABLE IF NOT EXISTS table_docs (
        dataset      TEXT NOT NULL,
        table_name   TEXT NOT NULL,
        table_label  TEXT,
        domain       TEXT,
        grain        TEXT,
        description  TEXT,
        row_estimate BIGINT,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        source       TEXT NOT NULL DEFAULT 'manual',
        PRIMARY KEY (dataset, table_name)
    )
    """,
    # 元数据字典 · 字段级
    """
    CREATE TABLE IF NOT EXISTS column_docs (
        dataset      TEXT NOT NULL,
        table_name   TEXT NOT NULL,
        column_name  TEXT NOT NULL,
        column_label TEXT,
        data_type    TEXT,
        nullable     BOOLEAN,
        is_sensitive BOOLEAN NOT NULL DEFAULT false,
        is_primary_key BOOLEAN NOT NULL DEFAULT false,
        description  TEXT,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        source       TEXT NOT NULL DEFAULT 'manual',
        PRIMARY KEY (dataset, table_name, column_name)
    )
    """,
    # 业务口径词表
    """
    CREATE TABLE IF NOT EXISTS business_glossary (
        term        TEXT PRIMARY KEY,
        definition  TEXT NOT NULL,
        formula     TEXT,
        dataset     TEXT,
        source      TEXT,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # 第一轮语义分析结果（M3）。**只增不改**：重跑分析 = 新 round_no，旧轮次原样保留。
    # 主键 (demand_id, round_no) 而非自增 ID，是为了让"第几轮"成为业务语义的一部分——
    # 回退确认后重跑的结论必须能并排比对，而不是覆盖掉上一轮的判断。
    """
    CREATE TABLE IF NOT EXISTS analysis_rounds (
        demand_id   TEXT NOT NULL,
        round_no    INTEGER NOT NULL,
        dataset     TEXT NOT NULL DEFAULT 'B',
        summary     JSONB NOT NULL DEFAULT '{}'::jsonb,
        slots       JSONB NOT NULL DEFAULT '{}'::jsonb,
        evidence    JSONB NOT NULL DEFAULT '[]'::jsonb,
        risks       JSONB NOT NULL DEFAULT '{}'::jsonb,
        questions   JSONB NOT NULL DEFAULT '[]'::jsonb,
        engine      TEXT NOT NULL DEFAULT 'deterministic',
        actor       TEXT NOT NULL DEFAULT 'system',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (demand_id, round_no)
    )
    """,
    # 确认问答留痕（M3）。**版本化、不可覆盖**（PRD §9.3 业务规则 2）：
    # 同一问题再次答复不 UPDATE 原行，而是插入 version+1 且 supersedes 指向前一版，
    # 于是"问题 / 答复 / 时间 / 版本"四要素全量保留，历史可回放。
    """
    CREATE TABLE IF NOT EXISTS confirmations (
        confirmation_id TEXT PRIMARY KEY,
        demand_id       TEXT NOT NULL,
        round_no        INTEGER NOT NULL,
        question_id     TEXT NOT NULL,
        slot            TEXT,
        question        TEXT NOT NULL,
        reason          TEXT,
        options         JSONB NOT NULL DEFAULT '[]'::jsonb,
        impact_scope    TEXT,
        answer          TEXT,
        answered_by     TEXT,
        answered_at     TIMESTAMPTZ,
        version         INTEGER NOT NULL DEFAULT 1,
        supersedes      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_analysis_rounds_did "
    "ON analysis_rounds (demand_id, round_no DESC)",
    "CREATE INDEX IF NOT EXISTS idx_confirmations_did "
    "ON confirmations (demand_id, question_id, version DESC)",
    # Schema 快照（M4-1）。主键 (dataset, schema_version)：同一库重复采集幂等；
    # 且"哪一版 Schema 下生成了哪条 SQL"可经 schema_version 回溯。
    """
    CREATE TABLE IF NOT EXISTS schema_snapshots (
        dataset         TEXT NOT NULL,
        schema_version  TEXT NOT NULL,
        digest          TEXT NOT NULL,
        payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
        source          TEXT NOT NULL DEFAULT 'native',
        collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (dataset, schema_version)
    )
    """,
    # 结构化技术需求对象（M4-2）。**只增不改**：同一需求每次合成 = version+1，旧版保留。
    """
    CREATE TABLE IF NOT EXISTS structured_requirements (
        demand_id       TEXT NOT NULL,
        version         INTEGER NOT NULL,
        dataset         TEXT NOT NULL,
        schema_version  TEXT NOT NULL,
        source_round    INTEGER,
        payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
        status          TEXT NOT NULL DEFAULT '待审核',
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (demand_id, version)
    )
    """,
    # SQL 生成与执行留痕（M5-2）。字段名对齐《SQL生成链路设计》§7 的溯源字段；
    # 三个版本号（requirement/schema/pack）串进来，使任何口径变化都能定位到"哪条链路受影响"。
    """
    CREATE TABLE IF NOT EXISTS sql_runs (
        sql_run_id          TEXT PRIMARY KEY,
        demand_id           TEXT NOT NULL,
        requirement_version INTEGER,
        schema_version      TEXT,
        pack_version        TEXT,
        generator_model     TEXT,
        generated_sql       TEXT NOT NULL,
        review_status       TEXT,
        review_detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
        validation_result   JSONB NOT NULL DEFAULT '{}'::jsonb,
        final_delivery_sql  TEXT,
        actor               TEXT NOT NULL DEFAULT 'system',
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_schema_snapshots_ds "
    "ON schema_snapshots (dataset, collected_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_structured_req_did "
    "ON structured_requirements (demand_id, version DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sql_runs_did "
    "ON sql_runs (demand_id, created_at DESC)",
    # 知识引用留痕（M6-4 · PRD §13 第③类「哪版知识被引用、何时被下架或回退」）。
    # 独立成表而非塞进 sql_runs 的 JSONB：一次生成可能引用 0~N 个片段，且同一片段会被
    # 多条需求复用——独立表才能双向查（按需求查引用 / 按片段查影响面）。
    # retired_at / retired_reason 为「下架或回退」留痕，NULL = 仍在架。
    """
    CREATE TABLE IF NOT EXISTS knowledge_citations (
        citation_id     TEXT PRIMARY KEY,
        demand_id       TEXT,
        round_no        INTEGER,
        sql_run_id      TEXT,
        question        TEXT NOT NULL,
        dataset_id      TEXT,
        document_id     TEXT,
        document_name   TEXT,
        chunk_id        TEXT,
        positions       TEXT,
        retired_at      TIMESTAMPTZ,
        retired_reason  TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_knowledge_citations_did "
    "ON knowledge_citations (demand_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_citations_doc "
    "ON knowledge_citations (document_id)",
]


def init_schema():
    """建表（幂等）。返回已就绪的表清单。"""
    for stmt in DDL:
        execute(stmt)
    rows = query(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
        """
    )
    return [r["table_name"] for r in rows]


def table_stats():
    """元数据库自身规模，供 /healthz 展示。"""
    out = {}
    for t in (
        "demand_requests",
        "demand_events",
        "table_docs",
        "column_docs",
        "business_glossary",
        "analysis_rounds",
        "confirmations",
        "schema_snapshots",
        "structured_requirements",
        "sql_runs",
        "knowledge_citations",
    ):
        try:
            out[t] = query_one("SELECT count(*) AS n FROM %s" % t)["n"]
        except Exception:
            out[t] = None
    return out


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False)
