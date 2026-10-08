#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示环境重置：清掉验收/回归跑出来的现场痕迹。

为什么需要这个工具
------------------
`tools/m2_verify.py` 每跑一次就会往元数据库写 4–6 条需求单、往对象存储写若干附件。
多跑几轮之后，「需求看板」里全是 `待分析` 的测试单，演示时看着像真业务数据，容易误读。
知识库侧同理：链路验证用的夹具文档若与正式治理文档内容重叠，会与真文档抢召回位次。

这个工具把「演示前把环境擦干净」变成一条可复跑的命令。

安全设计
--------
- **默认 dry-run**：不带 `--apply` 时只报告将要删除什么，一个字都不改。
  也可显式写 `--dry-run`（与不写等价），便于在脚本里表达意图。
- **只删两类东西**：元数据库的**需求域派生表**（`demand_requests` 及其
  6 张下游表）与知识库中名称匹配夹具前缀的文档。
- **元数据域默认不碰**：`table_docs` / `column_docs` / `business_glossary` /
  `schema_snapshots` 没有 demand_id 列，与需求单无关联，也不是「现场痕迹」——
  它们是语义层的地基（B 库字段描述覆盖 100% 靠的就是 column_docs）。
  清掉不会产生孤儿，只会让环境不可用。确需重置用 `--reset-metadata`，
  且事后必须重跑 `tools/collect_metadata.py --dataset A --dataset B --seed-glossary`。
- **不清附件**：MinIO 里的附件对象保留（对象键含随机段，无法从需求单反推，且无害）。
  如需一并清理，用 `--purge-attachments` 显式指定。

用法
----
    # 1) 先看会删什么（默认，不改动）
    python3 tools/reset_demo_data.py
    python3 tools/reset_demo_data.py --dry-run      # 与上等价，显式声明

    # 2) 确认后执行
    python3 tools/reset_demo_data.py --apply

    # 3) 只清知识库夹具，保留需求单
    python3 tools/reset_demo_data.py --apply --keep-demands

    # 4) 只清需求单，保留知识库夹具
    python3 tools/reset_demo_data.py --apply --keep-fixtures

    # 5) 连元数据域一起重置（会清空字段描述！事后必须重跑 collect_metadata.py）
    python3 tools/reset_demo_data.py --apply --reset-metadata

依赖：`psycopg`（宿主机需装，见 README 的隔离虚拟环境说明）。
"""

import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 夹具文档的识别前缀。命中即视为「可清的测试文件」，正式治理文档不在此列。
FIXTURE_PREFIXES = ("kb-e2e-test", "kb-smoke-", "test-fixture-")


# --------------------------------------------------------------------------
# 清理清单（按删除顺序排列，**顺序不可随意调整**）
# --------------------------------------------------------------------------
# 需求域派生表：都带 demand_id 列，靠 demand_id 挂在需求单上。
# 顺序 = 删除顺序（子表在前、主表在后）：
#   · knowledge_citations 同时引用 demand_id 与 sql_run_id，必须排在 sql_runs 之前，
#     否则它自己会变成指向已删 sql_run 的孤儿；
#   · 其余表直接引用 demand_id，只要排在 demand_requests 之前即可。
DEMAND_SCOPED_TABLES = (
    ("knowledge_citations", "知识引用留痕"),
    ("confirmations", "口径确认答复"),
    ("analysis_rounds", "分析轮次"),
    ("demand_events", "需求事件"),
    ("sql_runs", "SQL 执行记录"),
    ("structured_requirements", "结构化技术需求"),
    ("demand_requests", "需求单（主表）"),
)

# 元数据域资产：实测确认这四张表**没有 demand_id 列**，与需求单不存在任何关联。
# 它们是演示的**地基**而不是现场痕迹——B 库「字段描述覆盖 100%（51/51）」、
# 「business_glossary 5 条」都靠它们。清掉它们不会产生孤儿（没有孤儿可产生），
# 只会让语义层退化成「不认识任何字段」，演示当场跑不动。
# 因此默认一律不动；确需重置时用 --reset-metadata 显式开启，
# 且必须事后重跑 tools/collect_metadata.py 重建，否则环境不可用。
METADATA_TABLES = (
    ("table_docs", "表文档"),
    ("column_docs", "字段文档"),
    ("business_glossary", "业务术语表"),
    ("schema_snapshots", "Schema 快照"),
)


def load_env(path=None):
    """读项目 .env（不覆盖已存在的环境变量）。"""
    path = path or os.path.join(ROOT, ".env")
    env = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    for k, v in env.items():
        os.environ.setdefault(k, v)
    return env


# --------------------------------------------------------------------------
# 元数据库
# --------------------------------------------------------------------------

def db_dsn():
    return os.getenv("ASSISTANT_DB_DSN",
                     "postgresql://assistant:assistant@127.0.0.1:15434/assistant")


def _conn():
    try:
        import psycopg
    except ImportError:
        print("[x] 宿主机缺少 psycopg。请先安装依赖再运行：")
        print("    pip install \"psycopg[binary]\"")
        print("  或者在网关容器内执行该脚本（容器已自带依赖）：")
        print("    docker exec -it askoda sh -c \"cd /app && python tools/reset_demo_data.py\"")
        sys.exit(3)
    return psycopg.connect(db_dsn(), connect_timeout=10)


def _table_exists(cur, name):
    cur.execute("select to_regclass(%s)", ("public." + name,))
    return cur.fetchone()[0] is not None


def _count(cur, name):
    """取行数；表不存在返回 None（老库上这些表可能尚未建）。"""
    if not _table_exists(cur, name):
        return None
    cur.execute("select count(*) from %s" % name)
    return cur.fetchone()[0]


def survey_db(reset_metadata=False):
    """盘点元数据库：需求域派生表 + （可选）元数据域资产。

    2026-10-07 补测发现原实现只盘 demand_requests/demand_events 两张表，
    漏掉了 sql_runs（3112 行）/ structured_requirements（834 行）/
    knowledge_citations（1947 行）——而这三张表都带 demand_id，
    删掉需求单后就是数千行孤儿。实测库里当时已存在：
      · structured_requirements 1 行孤儿（demand DR-20261007-L70G）
      · knowledge_citations 6 行孤儿（同一 demand）
    本函数因此改为按 DEMAND_SCOPED_TABLES 全量盘点并顺带检出既有孤儿。
    """
    with _conn() as con, con.cursor() as cur:
        counts = {}
        for t, _label in DEMAND_SCOPED_TABLES:
            counts[t] = _count(cur, t)
        meta = {}
        if reset_metadata:
            for t, _label in METADATA_TABLES:
                meta[t] = _count(cur, t)

        # 分组统计与样本（这两项只对主表有意义，缺失时降级为空）
        by_status, by_event, sample = [], [], []
        if counts.get("demand_requests") is not None:
            cur.execute("select status, count(*) from demand_requests group by status order by 2 desc")
            by_status = cur.fetchall()
        if counts.get("demand_events") is not None:
            cur.execute("select event_type, count(*) from demand_events group by event_type order by 2 desc")
            by_event = cur.fetchall()
        if counts.get("demand_requests") is not None:
            cur.execute("select demand_id, title from demand_requests order by created_at desc limit 5")
            sample = cur.fetchall()

        # 既有孤儿体检：本次清理**之前**库里就已经有的孤儿，用来判断
        # 「清理后仍不为 0」到底是清漏了还是本来就有。
        orphans = {}
        if counts.get("demand_requests") is not None:
            for t in ("sql_runs", "structured_requirements", "knowledge_citations",
                      "analysis_rounds", "confirmations", "demand_events"):
                if counts.get(t) is None:
                    continue
                cur.execute(
                    "select count(*) from %s s where not exists "
                    "(select 1 from demand_requests d where d.demand_id = s.demand_id)" % t)
                n = cur.fetchone()[0]
                if n:
                    orphans[t] = n
    return {
        "counts": counts,
        "meta": meta,
        "orphans_before": orphans,
        "by_status": by_status,
        "by_event": by_event,
        "sample": sample,
    }


def clear_db(reset_metadata=False):
    """清空需求单及其全部派生数据。

    清理顺序有讲究：所有以 demand_id 关联的**派生数据**必须先删，
    否则会留下指向已删需求单的孤儿行。2026-09-30 实测踩到过——
    清理只删了 demand_requests 与 demand_events，留下 5 条分析轮次和 10 条
    确认记录挂在已不存在的需求单上，表规模统计因此失真。

    2026-10-07 补：原先也漏了 sql_runs / structured_requirements /
    knowledge_citations 三张表（合计约 5900 行）。这三张表同样带 demand_id，
    漏删的后果更严重——`sql_run_replay` 按 demand_id 查执行记录，
    孤儿行会让「审计回放」列出根本不属于任何需求单的版本。

    删除顺序见 DEMAND_SCOPED_TABLES：knowledge_citations 引用了 sql_run_id，
    必须排在 sql_runs 之前。
    """
    with _conn() as con, con.cursor() as cur:
        # 先把 demand_id 收下来：附件对象键以 demand_id 打头，
        # 需求单一旦删除就再也推不出它对应哪些对象了。
        cur.execute("select demand_id from demand_requests")
        demand_ids = [r[0] for r in cur.fetchall()]

        before = {t: _count(cur, t) for t, _ in DEMAND_SCOPED_TABLES}
        deleted = {}
        # 派生数据先删（引用方），再删 requests（被引用方）
        for t, _label in DEMAND_SCOPED_TABLES:
            if _table_exists(cur, t):
                cur.execute("delete from %s" % t)
                deleted[t] = cur.rowcount
            else:
                deleted[t] = None
        meta_deleted = {}
        if reset_metadata:
            for t, _label in METADATA_TABLES:
                if _table_exists(cur, t):
                    cur.execute("delete from %s" % t)
                    meta_deleted[t] = cur.rowcount
                else:
                    meta_deleted[t] = None
        con.commit()
    return {"before": before, "deleted": deleted,
            "meta_deleted": meta_deleted, "demand_ids": demand_ids}



# --------------------------------------------------------------------------
# 知识库
# --------------------------------------------------------------------------

def kb_client():
    base = os.getenv("KNOWLEDGE_API_URL", "http://127.0.0.1:19380/api/v1")
    # 宿主机上跑：容器内的服务名解析不了，统一换回回环地址
    base = base.replace("host.docker.internal", "127.0.0.1").replace("assistant-minio", "127.0.0.1")
    key = os.getenv("KNOWLEDGE_API_KEY", "")
    if not key:
        kf = os.getenv("KNOWLEDGE_API_KEY_FILE", "")
        if kf and os.path.exists(kf):
            key = open(kf, encoding="utf-8").read().strip()
    ds = os.getenv("KNOWLEDGE_DATASET_ID", "")
    if not key or not ds:
        return None, None, None
    return base.rstrip("/"), key, ds


def kb_api(base, key, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = opener.open(req, timeout=40).read().decode("utf-8", "replace")
    resp = json.loads(raw, strict=False) if raw.strip() else {}
    # 关键：非 0 的 code 必须抛出来。
    # 踩过的坑——传 page_size=200 时 RAGFlow 返回 code=100（"page_size must be <= 100"），
    # 响应里没有 data.docs。若这里静默返回空 dict，调用方会得出「库里没有文档」的
    # 结论，于是清理工具报告"无需清理"、实际一份也没删。错误必须显式失败，
    # 不能伪装成"一切正常，无事可做"。
    code = resp.get("code")
    if code not in (0, None):
        raise RuntimeError("知识库接口返回 code=%s：%s（%s %s）"
                           % (code, resp.get("message"), method, path))
    return resp


def survey_kb():
    base, key, ds = kb_client()
    if not base:
        return None, "未配置 KNOWLEDGE_API_KEY / KNOWLEDGE_DATASET_ID"
    try:
        # page_size 上限是 100：RAGFlow 对 >100 直接返回 code=100 且不带 data，实测踩过。
        resp = kb_api(base, key, "/datasets/%s/documents?page=1&page_size=100" % ds)
    except Exception as exc:  # noqa: BLE001
        return None, "知识库不可达或接口报错：%s" % exc
    docs = (resp.get("data") or {}).get("docs") or []
    total = (resp.get("data") or {}).get("total")
    if total is not None and len(docs) < int(total):
        # 分页截断时明确告警，避免"只看见前 100 份"被误读成"库里就这些"
        print("  [!] 数据集共 %s 份文档，本次仅取到前 %d 份，请分批处理" % (total, len(docs)))
    keep, fixtures = [], []
    for d in docs:
        (fixtures if str(d.get("name", "")).startswith(FIXTURE_PREFIXES) else keep).append(d)
    return {"base": base, "key": key, "dataset": ds,
            "keep": keep, "fixtures": fixtures}, None


def clear_kb(scope):
    ids = [d.get("id") for d in scope["fixtures"] if d.get("id")]
    if not ids:
        return 0
    kb_api(scope["base"], scope["key"], "/datasets/%s/documents" % scope["dataset"],
           method="DELETE", body={"ids": ids})
    return len(ids)


# --------------------------------------------------------------------------
# 附件对象存储
# --------------------------------------------------------------------------

def _minio():
    try:
        from minio import Minio
    except ImportError:
        return None, "宿主机缺少 minio 包，请用隔离虚拟环境运行本工具"
    endpoint = os.getenv("MINIO_ENDPOINT", "127.0.0.1:19000")
    # 容器内服务名在宿主机解析不了，换回回环地址
    endpoint = endpoint.replace("assistant-minio", "127.0.0.1")
    cli = Minio(endpoint,
                access_key=os.getenv("MINIO_ACCESS_KEY", "assistant"),
                secret_key=os.getenv("MINIO_SECRET_KEY", "assistant123"),
                secure=os.getenv("MINIO_SECURE", "false").lower() in ("1", "true", "yes"))
    return cli, None


def list_attachments(demand_ids):
    """列举桶内全部附件，并按"是否仍归属于现存需求单"分成两类。

    为什么要全量列举而不能只按 demand_id 前缀扫：
    上一轮清理把需求单删掉后，那些附件对象就成了**无主对象**——它们的前缀再也
    对不上任何现存 demand_id，按前缀扫永远扫不到。只按前缀清会留下永久垃圾。
    """
    cli, err = _minio()
    if err:
        return None, err
    bucket = os.getenv("MINIO_BUCKET", "askoda-attachments")
    known = set(demand_ids)
    try:
        if not cli.bucket_exists(bucket):
            return {"bucket": bucket, "owned": [], "orphan": []}, None
        owned, orphan = [], []
        for obj in cli.list_objects(bucket, recursive=True):
            prefix = (obj.object_name or "").split("/", 1)[0]
            (owned if prefix in known else orphan).append(obj)
        return {"bucket": bucket, "owned": owned, "orphan": orphan}, None
    except Exception as exc:  # noqa: BLE001
        return None, "附件存储不可达：%s" % exc


def purge_attachments(info):
    """删除已盘点的全部附件（含无主对象）。"""
    if not info:
        return 0, "无可清理的附件盘点结果"
    cli, err = _minio()
    if err:
        return 0, err
    n = 0
    for obj in list(info["owned"]) + list(info["orphan"]):
        cli.remove_object(info["bucket"], obj.object_name)
        n += 1
    return n, None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="演示环境重置（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正执行清理；不加则只报告")
    ap.add_argument("--dry-run", action="store_true",
                    help="显式声明只报告不改动（默认行为，此参数仅供脚本里表达意图，"
                         "与 --apply 同时给出时以 --apply 为准）")
    ap.add_argument("--keep-demands", action="store_true", help="保留需求单，只清知识库夹具")
    ap.add_argument("--keep-fixtures", action="store_true", help="保留知识库夹具，只清需求单")
    ap.add_argument("--purge-attachments", action="store_true",
                    help="同时删除这些需求单在对象存储中的附件（默认保留）")
    ap.add_argument("--reset-metadata", action="store_true",
                    help="连元数据域（table_docs/column_docs/business_glossary/"
                         "schema_snapshots）一起清空。⚠ 这些表是语义层地基，"
                         "清空后必须重跑 tools/collect_metadata.py 重建")
    args = ap.parse_args()

    # --apply 与 --dry-run 同时给出 = 调用方自相矛盾，**直接拒绝执行**。
    #
    # 2026-10-07 实测踩过：原实现只打印一行告警然后「以 --apply 为准」，
    # 结果一次只想验证参数组合的调用，真的把 1438 条需求单及其约 1.6 万行
    # 派生数据清空了，且本机 postgres 未开 archive_mode，无法时间点恢复。
    #
    # 同一个命令行里同时要求「真删」和「别删」，唯一正确的解释是调用方搞错了。
    # 猜意图并执行破坏性操作，是这类工具最不该做的事——宁可拒绝，不能赌。
    if args.apply and args.dry_run:
        print("[x] 同时给了 --apply 和 --dry-run，二者含义相反，已拒绝执行。")
        print("    只想看会删什么 → 去掉 --apply")
        print("    真的要执行清理 → 去掉 --dry-run")
        return 2

    load_env()
    mode = "执行" if args.apply else "试运行（dry-run，不改动任何数据）"
    print("=" * 68)
    print("演示环境重置 · %s" % mode)
    print("=" * 68)

    # --- 元数据库 ---
    print()
    print("【元数据库】%s" % db_dsn())
    if args.keep_demands:
        print("  按 --keep-demands 跳过")
        db_info = None
    else:
        db_info = survey_db(reset_metadata=args.reset_metadata)
        counts = db_info["counts"]
        print("  将按下列顺序清理（子表 → 主表），括号内为**将要删除的行数**：")
        for t, label in DEMAND_SCOPED_TABLES:
            n = counts.get(t)
            mark = "  (主表)" if t == "demand_requests" else ""
            print("      %-26s %-14s %s%s"
                  % (t, label, "表不存在" if n is None else n, mark))
        if args.reset_metadata:
            print("  元数据域（--reset-metadata 已开启，⚠ 清空后需重跑 collect_metadata.py）：")
            for t, label in METADATA_TABLES:
                n = db_info["meta"].get(t)
                print("      %-26s %-14s %s"
                      % (t, label, "表不存在" if n is None else n))
        else:
            print("  元数据域（默认保留，是语义层地基）：%s"
                  % "、".join(t for t, _ in METADATA_TABLES))
        total_rows = sum(v for v in counts.values() if v)
        print("  合计将删除 %d 行需求域数据" % total_rows)
        for s, n in db_info["by_status"]:
            print("      状态 %-10s %d" % (s, n))
        for e, n in db_info["by_event"]:
            print("      事件 %-18s %d" % (e, n))
        if db_info["sample"]:
            print("  最近 5 条需求单（将被删除）：")
            for did, title in db_info["sample"]:
                print("      %s  %s" % (did, (title or "")[:34]))
        if db_info["orphans_before"]:
            print("  \033[33m[!] 清理前已存在孤儿行（引用了不存在的需求单）："
                  "%s\033[0m"
                  % "、".join("%s %d 行" % (t, n)
                              for t, n in db_info["orphans_before"].items()))
        else:
            print("  清理前孤儿自检：无孤儿")

    # --- 知识库 ---
    print()
    print("【知识库】")
    scope, err = survey_kb()
    if err:
        print("  [!] %s" % err)
        scope = None
    else:
        print("  数据集 %s" % scope["dataset"])
        print("  正式文档 %d 份（保留）：" % len(scope["keep"]))
        for d in scope["keep"]:
            print("      ✓ %s" % d.get("name"))
        print("  测试夹具 %d 份（待清）：" % len(scope["fixtures"]))
        for d in scope["fixtures"]:
            print("      ✗ %s  (id=%s, chunks=%s)" % (d.get("name"), d.get("id"), d.get("chunk_count")))
        if not scope["fixtures"]:
            print("      （无）")

    # --- 附件（仅在显式要求清理时盘点）---
    att_info = None
    if args.purge_attachments and not args.keep_demands:
        print()
        print("【附件对象存储】")
        with _conn() as con, con.cursor() as cur:
            cur.execute("select demand_id from demand_requests")
            att_ids = [r[0] for r in cur.fetchall()]
        att_info, aerr = list_attachments(att_ids)
        if aerr:
            print("  [!] %s" % aerr)
        else:
            allobj = list(att_info["owned"]) + list(att_info["orphan"])
            print("  桶 %s，共 %d 个（随现存需求单 %d 个 / 无主 %d 个）"
                  % (att_info["bucket"], len(allobj),
                     len(att_info["owned"]), len(att_info["orphan"])))
            for o in allobj[:10]:
                print("      ✗ %s  (%d 字节)" % (o.object_name, o.size or 0))
            if len(allobj) > 10:
                print("      … 另有 %d 个" % (len(allobj) - 10))

    if not args.apply:
        print()
        print("以上为试运行结果。**未删除任何数据。** 确认无误后加 --apply 执行。")
        return 0

    # --- 执行 ---
    print()
    print("-" * 68)
    done = []
    if db_info is not None:
        got = clear_db(reset_metadata=args.reset_metadata)
        parts = []
        for t, label in DEMAND_SCOPED_TABLES:
            parts.append("%s %s 行" % (label, got["deleted"].get(t)))
        done.append("需求域：" + " / ".join(parts))
        if args.reset_metadata:
            mparts = ["%s %s 行" % (label, got["meta_deleted"].get(t))
                      for t, label in METADATA_TABLES]
            done.append("元数据域：" + " / ".join(mparts))
            done.append("⚠ 请立即重跑 tools/collect_metadata.py --dataset A "
                        "--dataset B --seed-glossary 重建元数据，否则环境不可用")
        if args.purge_attachments:
            n_att, aerr = purge_attachments(att_info)
            done.append(("附件 %d 个" % n_att) if not aerr else ("附件清理失败：%s" % aerr))
    if scope and not args.keep_fixtures:
        n = clear_kb(scope)
        done.append("知识库夹具 %d 份" % n)
    print("已清理：")
    for d in done:
        print("      · %s" % d)

    # --- 复核 ---
    print()
    print("【复核】")
    if db_info is not None:
        after = survey_db(reset_metadata=args.reset_metadata)
        for t, label in DEMAND_SCOPED_TABLES:
            n = after["counts"].get(t)
            print("  %-26s %-14s %s" % (t, label, "表不存在" if n is None else n))
        if args.reset_metadata:
            for t, label in METADATA_TABLES:
                n = after["meta"].get(t)
                print("  %-26s %-14s %s" % (t, label, "表不存在" if n is None else n))
        # 关键复核：清理后不得残留任何孤儿。原实现只看 analysis_rounds +
        # confirmations 两张表，漏掉了行数最多的 sql_runs / structured_requirements /
        # knowledge_citations——正是这三张表撑出了「清完留下数千行孤儿」的问题。
        if after["orphans_before"]:
            print("  \033[31m[!] 仍有孤儿行：%s —— 清理不完整\033[0m"
                  % "、".join("%s %d 行" % (t, n)
                              for t, n in after["orphans_before"].items()))
        else:
            print("  孤儿自检：0 行（全部派生表均无悬挂引用）")
    if scope and not args.keep_fixtures:
        s2, e2 = survey_kb()
        if not e2:
            print("  知识库文档 %d 份：%s" % (len(s2["keep"]),
                                          "、".join(d.get("name", "") for d in s2["keep"])))
    print()
    print("完成。")
    return 0



if __name__ == "__main__":
    sys.exit(main())
