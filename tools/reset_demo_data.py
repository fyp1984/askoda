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
- **只删两类东西**：元数据库的 `demand_requests` / `demand_events`（业务表一律不碰），
  以及知识库中名称匹配夹具前缀的文档。
- **不清附件**：MinIO 里的附件对象保留（对象键含随机段，无法从需求单反推，且无害）。
  如需一并清理，用 `--purge-attachments` 显式指定。

用法
----
    # 1) 先看会删什么（默认，不改动）
    python3 tools/reset_demo_data.py

    # 2) 确认后执行
    python3 tools/reset_demo_data.py --apply

    # 3) 只清知识库夹具，保留需求单
    python3 tools/reset_demo_data.py --apply --keep-demands

    # 4) 只清需求单，保留知识库夹具
    python3 tools/reset_demo_data.py --apply --keep-fixtures

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
        print("[x] 宿主机缺少 psycopg。请用隔离虚拟环境运行：")
        print("    /Users/FYP/.workbuddy/binaries/python/envs/default/bin/python tools/reset_demo_data.py")
        sys.exit(3)
    return psycopg.connect(db_dsn(), connect_timeout=10)


def survey_db():
    """盘点元数据库里现存的需求单与事件（含 M3 的分析轮次与确认问答）。"""
    with _conn() as con, con.cursor() as cur:
        cur.execute("select count(*) from demand_requests")
        n_req = cur.fetchone()[0]
        cur.execute("select count(*) from demand_events")
        n_evt = cur.fetchone()[0]
        cur.execute("select status, count(*) from demand_requests group by status order by 2 desc")
        by_status = cur.fetchall()
        cur.execute("select event_type, count(*) from demand_events group by event_type order by 2 desc")
        by_event = cur.fetchall()
        cur.execute("select demand_id, title from demand_requests order by created_at desc limit 5")
        sample = cur.fetchall()
        # M3 两张表按需创建，老库上可能还不存在——取不到就记 None，不当作错误
        n_rounds = n_conf = None
        try:
            cur.execute("select count(*) from analysis_rounds")
            n_rounds = cur.fetchone()[0]
            cur.execute("select count(*) from confirmations")
            n_conf = cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            pass
    return {"requests": n_req, "events": n_evt, "rounds": n_rounds, "confirmations": n_conf,
            "by_status": by_status, "by_event": by_event, "sample": sample}


def clear_db(keep_events=True):
    """清空需求单及其派生数据。

    清理顺序有讲究：`confirmations` / `analysis_rounds` / `demand_events` 都是
    以 demand_id 关联的**派生数据**，必须先删，否则会留下指向已删需求单的孤儿行。
    2026-09-30 实测踩到过——清理只删了 demand_requests 与 demand_events，
    留下 5 条分析轮次和 10 条确认记录挂在已不存在的需求单上，
    表规模统计因此失真，下一轮验收也从脏状态起跑。
    """
    with _conn() as con, con.cursor() as cur:
        cur.execute("select count(*) from demand_events")
        n_evt = cur.fetchone()[0]
        cur.execute("select count(*) from demand_requests")
        n_req = cur.fetchone()[0]
        n_rounds = n_conf = 0
        try:
            cur.execute("select count(*) from analysis_rounds")
            n_rounds = cur.fetchone()[0]
            cur.execute("select count(*) from confirmations")
            n_conf = cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            pass
        # 先把 demand_id 收下来：附件对象键以 demand_id 打头，
        # 需求单一旦删除就再也推不出它对应哪些对象了。
        cur.execute("select demand_id from demand_requests")
        demand_ids = [r[0] for r in cur.fetchall()]
        # 派生数据先删（引用方），再删 requests（被引用方）
        for t in ("confirmations", "analysis_rounds", "demand_events"):
            try:
                cur.execute("delete from %s" % t)
            except Exception:  # noqa: BLE001
                pass
        cur.execute("delete from demand_requests")
        con.commit()
    return {"requests": n_req, "events": n_evt, "rounds": n_rounds,
            "confirmations": n_conf, "demand_ids": demand_ids}


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
    bucket = os.getenv("MINIO_BUCKET", "demand-attachments")
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
    ap.add_argument("--keep-demands", action="store_true", help="保留需求单，只清知识库夹具")
    ap.add_argument("--keep-fixtures", action="store_true", help="保留知识库夹具，只清需求单")
    ap.add_argument("--purge-attachments", action="store_true",
                    help="同时删除这些需求单在对象存储中的附件（默认保留）")
    args = ap.parse_args()

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
        db_info = survey_db()
        print("  需求单 %d 条 / 事件 %d 条" % (db_info["requests"], db_info["events"]))
        if db_info.get("rounds") is not None:
            print("  分析轮次 %d 条 / 确认问答 %d 条（M3，随需求单一起清）"
                  % (db_info["rounds"], db_info["confirmations"]))
        for s, n in db_info["by_status"]:
            print("      状态 %-10s %d" % (s, n))
        for e, n in db_info["by_event"]:
            print("      事件 %-18s %d" % (e, n))
        if db_info["sample"]:
            print("  最近 5 条：")
            for did, title in db_info["sample"]:
                print("      %s  %s" % (did, (title or "")[:34]))

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
        print("以上为试运行结果。确认无误后加 --apply 执行。")
        return 0

    # --- 执行 ---
    print()
    print("-" * 68)
    done = []
    if db_info is not None:
        got = clear_db()
        done.append("需求单 %d 条 / 事件 %d 条 / 分析轮次 %d 条 / 确认问答 %d 条"
                    % (got["requests"], got["events"], got["rounds"], got["confirmations"]))
        if args.purge_attachments:
            n_att, aerr = purge_attachments(att_info)
            done.append(("附件 %d 个" % n_att) if not aerr else ("附件清理失败：%s" % aerr))
    if scope and not args.keep_fixtures:
        n = clear_kb(scope)
        done.append("知识库夹具 %d 份" % n)
    print("已清理：" + ("；".join(done) if done else "（无）"))

    # --- 复核 ---
    print()
    print("【复核】")
    if db_info is not None:
        after = survey_db()
        print("  需求单 %d 条 / 事件 %d 条" % (after["requests"], after["events"]))
        if after.get("rounds") is not None:
            print("  分析轮次 %d 条 / 确认问答 %d 条" % (after["rounds"], after["confirmations"]))
            orphan = (after["rounds"] or 0) + (after["confirmations"] or 0)
            if after["requests"] == 0 and orphan > 0:
                print("  \033[31m[!] 仍有 %d 条孤儿派生数据，清理不完整\033[0m" % orphan)
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
