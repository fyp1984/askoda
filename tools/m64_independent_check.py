# -*- coding: utf-8 -*-
"""M6-4 独立复扫（验收方资产 · 容器内运行）。

与交付方 tools/audit_verify.py 的**分工**：
  · audit_verify.py     —— 交付方自证：四类留痕逐条取证 + 回放 + 身份透传（"我做到了"）。
  · 本脚本              —— 验收方独立复扫：**独立重算**，不复用交付方的用例与断言，
                          专挑边界与误判面（"你真的做到了吗，换个数呢"）。

刻意与交付方不同的三处做法：
  ① 身份透传**走 MCP HTTP 真实接口**（不是进程内直接调库函数）——证明身份经产品接口可达；
     并用**独立 SQL 回查 sql_runs.actor**，不采信返回值自述。
  ② 列表接口的 adopted / 字段截断 / 筛选，全部**用原始表独立重算**后逐条比对。
  ③ 回放接口的 pack_version 与结构化需求版本体，**独立按主键取行比对**；
     取不到时必须落进 gaps（不许静默 null）。

运行（容器内）：
  docker cp tools/m64_independent_check.py demand-gateway:/app/tools/
  docker exec -e PYTHONPATH=/app demand-gateway python /app/tools/m64_independent_check.py

退出码 0 = 全过，1 = 有失败。
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GW = os.path.join(ROOT, "gateway")
if not os.path.isdir(GW):
    GW = ROOT          # 容器内代码平铺在 /app，没有 gateway/ 子目录
if GW not in sys.path:
    sys.path.insert(0, GW)

import db  # noqa: E402

MCP_URL = os.environ.get("M64_MCP_URL", "http://127.0.0.1:8080/mcp")
GOOD_SQL = "SELECT 1 AS n"          # 实测：过五层门禁、row_count=1
VERIFIER = "m64-verifier-A"

FAIL = []
SID = None


def expect(name, cond, detail=""):
    if cond:
        print("  [OK]", name)
    else:
        msg = "  [FAIL] %s%s" % (name, (" | " + str(detail)) if detail else "")
        print(msg)
        FAIL.append(msg)


def info(name, value):
    print("  [--] %s: %s" % (name, value))


# ---------------------------------------------------------------------------
# MCP HTTP（真实接口）
# ---------------------------------------------------------------------------
def _rpc(method, params=None):
    global SID
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or {}}).encode()
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if SID:
        h["Mcp-Session-Id"] = SID
    req = urllib.request.Request(MCP_URL, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
        SID = r.headers.get("Mcp-Session-Id") or SID
    for line in raw.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


def mcp_call(name, args):
    r = _rpc("tools/call", {"name": name, "arguments": args})
    if r is None or "result" not in r:
        return {"_rpc_error": r}
    try:
        return json.loads(r["result"]["content"][0]["text"])
    except Exception as e:  # noqa: BLE001
        return {"_parse_error": "%s: %s" % (type(e).__name__, e),
                "_raw": str(r)[:400]}


def mcp_init():
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "m64-independent", "version": "1.0"}})
    _rpc("notifications/initialized", {})


# ---------------------------------------------------------------------------
# 独立取数（直接查库，不经过被测接口）
# ---------------------------------------------------------------------------
def pick_sample():
    row = db.query_one(
        "SELECT demand_id FROM sql_runs ORDER BY created_at DESC, sql_run_id DESC LIMIT 1"
    )
    return row["demand_id"] if row else None


def runs_of(demand_id):
    return db.query(
        "SELECT sql_run_id, created_at::text AS created_at, final_delivery_sql, "
        "       review_status, pack_version, requirement_version, schema_version, actor, "
        "       generated_sql, validation_result "
        "FROM sql_runs WHERE demand_id=%s ORDER BY created_at, sql_run_id",
        (demand_id,),
    )


# ---------------------------------------------------------------------------
# A · 身份透传（走 MCP 真实接口 + 独立回查）
# ---------------------------------------------------------------------------
def part_a_identity(sample):
    print("\n[A] 身份透传（MCP 真实接口 → 独立回查 sql_runs.actor）")

    out = mcp_call("sql_execute_readonly",
                   {"demand_id": sample, "dataset": "B", "sql": GOOD_SQL, "actor": VERIFIER})
    rid = out.get("sql_run_id")
    if not rid:
        expect("A1 能拿到 sql_run_id（SQL 真跑通）", False, out)
        return
    expect("A1 能拿到 sql_run_id（SQL 真跑通）", True)
    row = db.query_one("SELECT actor, demand_id, generated_sql FROM sql_runs WHERE sql_run_id=%s",
                       (rid,))
    expect("A2 独立回查：该次 sql_runs.actor == 传入的 %r" % VERIFIER,
           bool(row) and row["actor"] == VERIFIER,
           "实际=%r" % (row or {}).get("actor"))

    # 对照：不传 actor → 必须仍落默认 'system'（证明不是"把默认值改花"）
    out2 = mcp_call("sql_execute_readonly",
                    {"demand_id": sample, "dataset": "B", "sql": GOOD_SQL})
    rid2 = out2.get("sql_run_id")
    row2 = db.query_one("SELECT actor FROM sql_runs WHERE sql_run_id=%s", (rid2,)) if rid2 else None
    expect("A3 对照：不传 actor 时仍落默认 'system'（默认值未被改写）",
           bool(row2) and row2["actor"] == "system",
           "实际=%r" % (row2 or {}).get("actor"))

    # 对抗：伪造一个不存在的身份也要如实落库（不许白名单、不许回落默认）
    out3 = mcp_call("sql_execute_readonly",
                    {"demand_id": sample, "dataset": "B", "sql": GOOD_SQL,
                     "actor": "m64-verifier-B/带中文/../?&"})
    rid3 = out3.get("sql_run_id")
    row3 = db.query_one("SELECT actor FROM sql_runs WHERE sql_run_id=%s", (rid3,)) if rid3 else None
    expect("A4 对抗：含特殊字符的 actor 原样落库（不回落默认、不被截断/清洗）",
           bool(row3) and row3["actor"] == "m64-verifier-B/带中文/../?&",
           "实际=%r" % (row3 or {}).get("actor"))


# ---------------------------------------------------------------------------
# B · sql_run_list（独立重算比对）
# ---------------------------------------------------------------------------
def part_b_list(sample):
    print("\n[B] sql_run_list —— 独立重算比对（total / adopted / 截断 / 筛选）")

    out = mcp_call("sql_run_list", {"limit": 1000})
    if not isinstance(out, dict) or "items" not in out:
        expect("B0 sql_run_list 可调用且返回 items", False, out)
        return
    total = out.get("total")
    items = out.get("items") or []
    db_total = db.query_one("SELECT count(*) AS n FROM sql_runs")["n"]
    expect("B1 total 与独立 count(*) 相符（limit 足够大时）",
           int(total or -1) == int(db_total), "接口=%s 独立=%s" % (total, db_total))
    expect("B2 items 长度 == total（未被静默截断）",
           len(items) == int(total or -1), "len(items)=%d total=%s" % (len(items), total))

    # adopted 逐条独立重算
    bad = []
    for it in items[:200]:
        rid = it.get("sql_run_id")
        r = db.query_one("SELECT final_delivery_sql, review_status FROM sql_runs WHERE sql_run_id=%s",
                         (rid,))
        if not r:
            bad.append((rid, "not-in-db", it.get("adopted")))
            continue
        want = bool(r["final_delivery_sql"]) and r["review_status"] != "不通过"
        if bool(it.get("adopted")) != want:
            bad.append((rid, "want=%s" % want, "got=%s" % it.get("adopted")))
    expect("B3 adopted 判据逐条独立重算一致（final_delivery_sql 非空 且 review_status != 不通过）",
           not bad, bad[:5])

    # SQL 预览键：提示词原文是「generated_sql 前 200 字符」，实现命名为
    # generated_sql_preview —— 两种命名都可接受，取实际存在的那个做后续断言
    preview_key = next(
        (k for k in ("generated_sql_preview", "generated_sql") if items and k in items[0]), None
    )
    info("SQL 预览键（实现命名）", preview_key)

    # 截断：预览键的值不得是全长 SQL（应 ~200 字符）
    if preview_key:
        longg = [it for it in items if len(str(it.get(preview_key) or "")) > 203]
        expect("B4 %s 已截断到 ~200 字符" % preview_key, not longg,
               [(it.get("sql_run_id"), len(str(it.get(preview_key) or ""))) for it in longg[:3]])
    else:
        expect("B4 存在 SQL 预览键（generated_sql_preview / generated_sql）", False,
               sorted(items[0].keys()) if items else [])

    # 必含字段（SQL 预览键单独判，见上）
    need = {"sql_run_id", "demand_id", "created_at", "generator_model", "review_status",
            "adopted", "pack_version", "requirement_version", "schema_version", "actor"}
    missing = [k for k in sorted(need) if items and k not in items[0]]
    expect("B5 条目字段齐全（10 个基准键 + SQL 预览键）",
           (not missing) and preview_key is not None, "缺=%s preview_key=%s" % (missing, preview_key))

    # 按 demand_id 筛选
    o2 = mcp_call("sql_run_list", {"demand_id": sample, "limit": 1000})
    its2 = (o2 or {}).get("items") or []
    db_n = db.query_one("SELECT count(*) AS n FROM sql_runs WHERE demand_id=%s", (sample,))["n"]
    expect("B6 demand_id 筛选：total == 该需求独立计数",
           int((o2 or {}).get("total") or -1) == int(db_n),
           "接口=%s 独立=%s" % ((o2 or {}).get("total"), db_n))
    expect("B7 demand_id 筛选：返回条目确实全属该需求",
           all(it.get("demand_id") == sample for it in its2), "混入=%s" %
           [it.get("demand_id") for it in its2 if it.get("demand_id") != sample][:3])

    # 按 dataset 筛选（sql_runs 无 dataset 列，须经 structured_requirements 反查）
    o3 = mcp_call("sql_run_list", {"dataset": "A", "limit": 1000})
    if o3 is None or "items" not in (o3 or {}):
        expect("B8 dataset 筛选可调用", False, o3)
    else:
        its3 = o3.get("items") or []
        # 独立反查：该数据集下出现过的 demand_id 集合（取每个需求最新一版结构化需求所在数据集）
        rows = db.query(
            "SELECT demand_id, (array_agg(dataset ORDER BY version DESC))[1] AS ds "
            "FROM structured_requirements GROUP BY demand_id"
        )
        ds_map = {r["demand_id"]: r["ds"] for r in rows}
        wrong = [it.get("demand_id") for it in its3
                 if ds_map.get(it.get("demand_id")) not in (None, "A")]
        expect("B8 dataset=A 筛选：返回条目的需求数据集确实为 A（独立反查）",
               not wrong, "混入=%s" % wrong[:5])
        expect("B9 dataset 筛选生效：A 的结果数 < 不筛的总数",
               int(o3.get("total") or 0) < int(total or 0),
               "A=%s 全量=%s" % (o3.get("total"), total))
        info("dataset_filter_note", (o3 or {}).get("dataset_filter_note"))

    # 对抗：**不存在的 dataset** —— 必须返回空，绝不降级成全量
    # （这是 P0-2 的核心防线：缺陷版的行为是"反查失败 → 静默返回全量"）
    o4 = mcp_call("sql_run_list", {"dataset": "__NO_SUCH_DS__", "limit": 1000})
    if o4 is None or "items" not in (o4 or {}):
        expect("B10 对抗：不存在的 dataset 可调用（不抛异常）", False, o4)
    else:
        # ⚠ 零值陷阱：绝不能写 `int(o4.get("total") or -1)` —— total=0 是合法期望值，
        # 而 `0 or -1` 在 Python 里求值为 -1，会把「正确返回 0」误判为 FAIL。
        t4 = o4.get("total")
        expect("B10 对抗：不存在的 dataset → total==0 且 items 为空（绝不降级成全量）",
               t4 is not None and int(t4) == 0 and not (o4.get("items") or []),
               "total=%r items=%d" % (t4, len(o4.get("items") or [])))
        expect("B11 不存在的 dataset 也施加了筛选（dataset_filter_applied 为 True）",
               o4.get("dataset_filter_applied") is True,
               "值=%r" % o4.get("dataset_filter_applied"))
        info("dataset='__NO_SUCH_DS__' 返回（去 items）",
             json.dumps({k: v for k, v in o4.items() if k != "items"}, ensure_ascii=False)[:320])


# ---------------------------------------------------------------------------
# C · sql_run_replay（五段链条 + 独立比对 + 负例）
# ---------------------------------------------------------------------------
def part_c_replay(sample):
    print("\n[C] sql_run_replay —— 五段链条齐全 + 独立比对 pack_version / 版本体")

    rs = runs_of(sample)
    if not rs:
        expect("C0 样本需求有 sql_runs", False, sample)
        return
    latest = rs[-1]                      # 独立取：created_at 最大（并列时取末位）
    earliest = rs[0]

    out = mcp_call("sql_run_replay", {"demand_id": sample})
    if not isinstance(out, dict) or not out.get("sql_run_id"):
        expect("C1 replay 可调用并返回 sql_run_id", False, out)
        return
    expect("C1 replay 可调用并返回 sql_run_id", True)

    max_ts = max(r["created_at"] for r in rs)
    tied = {r["sql_run_id"] for r in rs if r["created_at"] == max_ts}
    expect("C2 version=None 指向最近一次运行（created_at 最大者）",
           out["sql_run_id"] in tied, "返回=%s 期望∈%s" % (out["sql_run_id"], sorted(tied)))

    seg = out.get("input") or {}
    expect("C3 链条已含 5 段",
           all(k in out for k in ("input", "sql", "review", "result")),
           sorted(out.keys()))
    expect("C4 input.pack_version 与独立取行一致",
           seg.get("pack_version") == latest["pack_version"],
           "接口=%r 独立=%r" % (seg.get("pack_version"), latest["pack_version"]))
    expect("C5 input.requirement_version 与独立取行一致",
           str(seg.get("requirement_version")) == str(latest["requirement_version"]),
           "接口=%r 独立=%r" % (seg.get("requirement_version"), latest["requirement_version"]))
    expect("C6 input.schema_version 与独立取行一致",
           seg.get("schema_version") == latest["schema_version"],
           "接口=%r 独立=%r" % (seg.get("schema_version"), latest["schema_version"]))

    sql_seg = out.get("sql") or {}
    expect("C7 sql.segment 的 generated_sql 与留痕行一致",
           (sql_seg.get("generated_sql") or "").strip() == (latest["generated_sql"] or "").strip(),
           "%r vs %r" % ((sql_seg.get("generated_sql") or "")[:40],
                         (latest["generated_sql"] or "")[:40]))
    expect("C8 sql.adopted 与独立重算一致",
           bool(sql_seg.get("adopted")) == bool(latest["final_delivery_sql"]
                                                and latest["review_status"] != "不通过"),
           "接口=%s 独立=%s" % (sql_seg.get("adopted"),
                                bool(latest["final_delivery_sql"])))

    rev = out.get("review") or {}
    expect("C9 review.status 与留痕行一致",
           rev.get("status") == latest["review_status"],
           "接口=%r 独立=%r" % (rev.get("status"), latest["review_status"]))

    # 结构化需求版本体：要么与独立取行一致，要么为 null 且 gaps 里有说明
    body = seg.get("structured_requirement")
    gaps = out.get("gaps")
    if latest["requirement_version"] is None:
        expect("C10 无 requirement_version 时版本体为 null 且 gaps 非空（不静默）",
               body is None and bool(gaps), "body=%r gaps=%r" % (str(body)[:60], gaps))
    else:
        row = db.query_one(
            "SELECT payload FROM structured_requirements WHERE demand_id=%s AND version=%s",
            (sample, latest["requirement_version"]))
        if row is None:
            expect("C10 取不到版本体时必须写进 gaps（不编造）",
                   body is None and bool(gaps), "body=%r gaps=%r" % (str(body)[:60], gaps))
        else:
            # 独立比对：取一个稳定字段（dataset）与 version 对齐，避免 JSONB 键序差异
            want_ds = (row["payload"] or {}).get("dataset")
            got = body if isinstance(body, dict) else {}
            expect("C10 版本体与 structured_requirements 独立取行一致（dataset/version）",
                   got.get("dataset") == want_ds
                   and str(got.get("version") or latest["requirement_version"])
                   == str(latest["requirement_version"]),
                   "接口 dataset=%r 独立 dataset=%r" % (got.get("dataset"), want_ds))

    # version 参数：不能被忽略
    o1 = mcp_call("sql_run_replay", {"demand_id": sample, "version": 1})
    ids = {r["sql_run_id"] for r in rs}
    expect("C11 version=1 指向该需求的某次真实运行（version 参数未被忽略）",
           bool(o1) and o1.get("sql_run_id") in ids,
           "返回=%r ∈ %s" % ((o1 or {}).get("sql_run_id"), sorted(ids)))
    if len(rs) >= 2:
        expect("C12 该需求有多次运行时，version=1 与 version=None 指向不同的行",
               (o1 or {}).get("sql_run_id") != out.get("sql_run_id"),
               "v1=%s latest=%s" % ((o1 or {}).get("sql_run_id"), out.get("sql_run_id")))
        info("version 映射（交由验收方判读方向）", "v1=%s / earliest=%s / latest=%s"
             % ((o1 or {}).get("sql_run_id"), earliest["sql_run_id"], latest["sql_run_id"]))

    # 负例：不存在的需求
    neg = mcp_call("sql_run_replay", {"demand_id": "DR-NOT-EXIST-M64"})
    expect("C13 负例：不存在的需求返回 ok=False 而非抛裸异常",
           isinstance(neg, dict) and neg.get("ok") is False and "error" in neg,
           str(neg)[:200])


# ---------------------------------------------------------------------------
# D · 知识引用留痕（独立复算 + 对抗）
# ---------------------------------------------------------------------------
def part_d_citations(sample):
    print("\n[D] 知识引用留痕 —— 独立复算 + 对抗用例")
    try:
        import knowledge
    except Exception as e:  # noqa: BLE001
        expect("D0 可导入 knowledge", False, "%s: %s" % (type(e).__name__, e))
        return
    for fn in ("record_citations", "retire_citation"):
        if not hasattr(knowledge, fn):
            expect("D0 knowledge.%s 存在" % fn, False, "属性缺失")
            return

    before = db.query_one("SELECT count(*) AS n FROM knowledge_citations")["n"]

    cites = [
        {"document_id": "DOC-V1", "document_name": "M64独立复扫-样例A.pdf",
         "chunk_id": "CK-1", "positions": [[1, 10.0, 20.0, 30.0, 40.0]]},
        {"document_id": "DOC-V1", "document_name": "M64独立复扫-样例A.pdf",
         "chunk_id": "CK-2", "positions": []},
    ]
    r1 = knowledge.record_citations(sample, "M64独立复扫：口径怎么算", cites)
    after = db.query_one("SELECT count(*) AS n FROM knowledge_citations")["n"]
    expect("D1 record_citations 返回 inserted==2",
           int(r1.get("inserted", -1)) == 2, r1)
    expect("D2 独立计数：knowledge_citations 行数 +2",
           int(after) - int(before) == 2, "before=%s after=%s" % (before, after))
    cids = r1.get("citation_ids") or []
    expect("D3 citation_ids 全部可在库中回查到",
           len(cids) == 2 and all(
               db.query_one("SELECT citation_id FROM knowledge_citations WHERE citation_id=%s", (c,))
               for c in cids), cids)
    expect("D4 引用的 document/chunk 字段如实落库（抽查 1 条）",
           (db.query_one(
               "SELECT document_id, document_name, chunk_id FROM knowledge_citations "
               "WHERE citation_id=%s", (cids[0],)) if cids else None) ==
           {"document_id": "DOC-V1", "document_name": "M64独立复扫-样例A.pdf", "chunk_id": "CK-1"},
           db.query_one("SELECT document_id, document_name, chunk_id FROM knowledge_citations "
                        "WHERE citation_id=%s", (cids[0],)) if cids else None)

    # 对抗 1：缺字段 / 类型不对的 citation —— 不许抛异常，缺的落 NULL
    try:
        r2 = knowledge.record_citations(sample, "M64对抗：脏引用",
                                        [{"document_name": "只有名字"},
                                         {"chunk_id": "CK-ONLY", "positions": "not-a-list"},
                                         {}])
        ok2, err2 = True, r2
    except Exception as e:  # noqa: BLE001
        ok2, err2 = False, "%s: %s" % (type(e).__name__, e)
    expect("D5 对抗：citation 缺字段/类型异常时**不抛异常**（审计表宁可缺列不能丢行）",
           ok2, err2)
    if ok2:
        expect("D6 对抗：脏引用仍如实入库（inserted==3）",
               int((r2 or {}).get("inserted", -1)) == 3, r2)

    # 对抗 2：空 citations
    try:
        r3 = knowledge.record_citations(sample, "M64对抗：空引用", [])
        ok3 = int((r3 or {}).get("inserted", -1)) == 0
    except Exception as e:  # noqa: BLE001
        ok3, r3 = False, "%s: %s" % (type(e).__name__, e)
    expect("D7 对抗：空 citations → inserted==0 且不抛异常", ok3, r3)

    # retire
    r4 = knowledge.retire_citation(cids[0], "M64独立复扫：测试下架")
    row = db.query_one("SELECT retired_at::text AS ra, retired_reason FROM knowledge_citations "
                       "WHERE citation_id=%s", (cids[0],))
    expect("D8 retire_citation 后 retired_at 非空", bool(row and row["ra"]), row)
    expect("D9 retired_reason 如实写入",
           bool(row) and row["retired_reason"] == "M64独立复扫：测试下架", row)
    expect("D10 retire 返回 updated==1", int((r4 or {}).get("updated", -1)) == 1, r4)

    # 对抗 3：retire 不存在的 citation
    try:
        r5 = knowledge.retire_citation("KC-NOT-EXIST-M64", "noop")
        ok5 = int((r5 or {}).get("updated", -1)) == 0
    except Exception as e:  # noqa: BLE001
        ok5, r5 = False, "%s: %s" % (type(e).__name__, e)
    expect("D11 对抗：retire 不存在的 citation → updated==0 且不抛异常", ok5, r5)


def main():
    print("=" * 70)
    print("M6-4 独立复扫（验收方资产 · 容器内）")
    print("  MCP =", MCP_URL)
    print("  gateway =", GW)
    try:
        mcp_init()
    except Exception as e:  # noqa: BLE001
        print("  [FATAL] MCP 初始化失败：%s: %s" % (type(e).__name__, e))
        return 1
    sample = pick_sample()
    print("  样本需求 =", sample)
    if not sample:
        print("  [FATAL] sql_runs 为空，无法取样本")
        return 1
    print("=" * 70)
    part_a_identity(sample)
    part_b_list(sample)
    part_c_replay(sample)
    part_d_citations(sample)
    print("\n==================== 总览 ====================")
    if not FAIL:
        print("全部通过")
        return 0
    print("%d 条失败：" % len(FAIL))
    for f in FAIL:
        print(" ", f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
