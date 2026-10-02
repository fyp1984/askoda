# -*- coding: utf-8 -*-
"""M6-3 验收方独立复扫（不共享交付方任何代码路径）。

交付方 tools/robustness_verify.py 有 15 条断言，但覆盖有三处明显空白，
本脚本专治这三点，并补两处打桩对抗：

  A') 环境变量超时链 —— 交付方 A 段用「显式传参 timeout=0.001」，
      从未验证 WREN_{NAME}_TIMEOUT / WREN_TIMEOUT / 60 的回落链，
      也没验证「环境变量真的传导到网络层」。本段补齐并实测生效。
  B') 读/写重试分支 —— 交付方 C 段测的是 db.execute（元数据库写入，
      与 M6-3 无关），从未直接验证 wren.call_raw 的
      「tool ∈ READ_TOOLS ? RETRY : 1」分支。本段用坏端点直接打这两个分支。
  C') 并发不串数据 —— 交付方 D 段只验「条数 = 20 / run_id 唯一」，
      20 次用的是同一条 SQL，即便串了也看不出。本段并发发不同 SQL，
      用返回结果里的 tag 指纹断言「请求-响应」一一对应。
  E') 异常类型规范化（打桩 socket.timeout / URLError(timeout) / URLError(conn refused)）
  F') session 失效重握手仍工作（原实现递归 call_raw(_retry=False)，新实现内联）
  G') sqlrun 的 WREN_ERROR 失败包装真链路透出（M6-3 对 sqlrun 的唯一改动）

退出码 0 = 全过，1 = 有硬失败。
"""
import io
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GW = os.path.join(ROOT, "gateway")
if GW not in sys.path:
    sys.path.insert(0, GW)

os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))

import db  # noqa: E402
import demand as demand_mod  # noqa: E402
import registry  # noqa: E402
import sqlrun as sqlrun_mod  # noqa: E402
from wren import WrenClient, WrenError, WrenTimeout, READ_TOOLS, READ_RETRIES, RETRY_BACKOFF  # noqa: E402

FAIL = []


def expect(name, cond, detail=""):
    if cond:
        print("  [OK]", name)
    else:
        msg = "  [FAIL] %s %s" % (name, ("｜ " + detail) if detail else "")
        print(msg)
        FAIL.append(msg)


def note(name, text):
    print("  [NOTE]", name, "→", text)


# ---------------------------------------------------------------------------
# A') 环境变量超时链（交付方完全没测）
# ---------------------------------------------------------------------------
def test_A_env_timeout_chain():
    print()
    print("[A'] 环境变量超时链：WREN_{NAME}_TIMEOUT 优先 → WREN_TIMEOUT → 60；并实测生效")
    url = registry.get("B").wren_url
    saved = {k: os.environ.get(k) for k in ("WREN_TIMEOUT", "WREN_A_TIMEOUT", "WREN_B_TIMEOUT")}

    def set_env(**kw):
        for k in ("WREN_TIMEOUT", "WREN_A_TIMEOUT", "WREN_B_TIMEOUT"):
            os.environ.pop(k, None)
        for k, v in kw.items():
            if v is not None:
                os.environ[k] = v

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    try:
        # a1 全无 → 60
        set_env()
        c = WrenClient(url, name="B")
        print("  a1 无 env → timeout = %s" % c.timeout)
        expect("A'-a1 全无环境变量 → 默认 60.0", abs(c.timeout - 60.0) < 1e-9, "实际=%s" % c.timeout)

        # a2 只有 WREN_TIMEOUT
        set_env(WREN_TIMEOUT="3.5")
        c = WrenClient(url, name="B")
        print("  a2 WREN_TIMEOUT=3.5 → timeout = %s" % c.timeout)
        expect("A'-a2 WREN_TIMEOUT 生效", abs(c.timeout - 3.5) < 1e-9, "实际=%s" % c.timeout)

        # a3 专用优先于通用
        set_env(WREN_TIMEOUT="3.5", WREN_B_TIMEOUT="7.5")
        cb = WrenClient(url, name="B")
        ca = WrenClient(url, name="A")
        print("  a3 WREN_TIMEOUT=3.5 + WREN_B_TIMEOUT=7.5 → B=%s / A=%s" % (cb.timeout, ca.timeout))
        expect("A'-a3 name=B 取专用 WREN_B_TIMEOUT（7.5）", abs(cb.timeout - 7.5) < 1e-9, "B=%s" % cb.timeout)
        expect("A'-a3 name=A 无专用 → 回落 WREN_TIMEOUT（3.5）", abs(ca.timeout - 3.5) < 1e-9, "A=%s" % ca.timeout)

        # a4 显式参数最优先
        set_env(WREN_B_TIMEOUT="7.5")
        c = WrenClient(url, name="B", timeout=11.25)
        print("  a4 显式 timeout=11.25 + WREN_B_TIMEOUT=7.5 → timeout = %s" % c.timeout)
        expect("A'-a4 显式传参优先于环境变量", abs(c.timeout - 11.25) < 1e-9, "实际=%s" % c.timeout)

        # a5 实测生效：环境变量真的传导到网络层（此处最容易「读到了值但没传下去」）
        set_env(WREN_B_TIMEOUT="0.002")
        c = WrenClient(url, name="B")  # 不传 timeout，纯走环境变量
        err = None
        try:
            c.query("SELECT 1")
        except WrenTimeout as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = "%s:%s" % (type(e).__name__, str(e)[:120])
        print("  a5 WREN_B_TIMEOUT=0.002 实测：timeout=%s  结果=%s" % (c.timeout, (err or "")[:130]))
        expect("A'-a5 环境变量超时实测生效（抛 WrenTimeout 且 message 含 timeout=0.002s）",
               bool(err) and "timeout=0.002s" in err and not err.startswith("socket."),
               "err=%s" % (err or "无异常"))
        expect("A'-a5 该异常不是裸 socket.timeout / URLError",
               bool(err) and not err.startswith("socket.") and not err.startswith("URLError"),
               "err=%s" % (err or ""))
    finally:
        restore()


# ---------------------------------------------------------------------------
# B') 读/写重试分支（交付方测的是 db.execute，打偏了）
# ---------------------------------------------------------------------------
def test_B_retry_branch():
    print()
    print("[B'] 重试分支：tool ∈ READ_TOOLS → 重试 %d 次；非读工具 → 仅 1 次" % READ_RETRIES)
    print("      READ_TOOLS = %s" % sorted(READ_TOOLS))
    bad = WrenClient("http://127.0.0.1:59999", name="DEAD", timeout=0.3)

    # b1 读工具 query
    err = None
    try:
        bad.call_raw("query", {"sql": "SELECT 1"})
    except Exception as e:  # noqa: BLE001
        err = e
    print("  b1 tool=query（读）→ attempts=%d  type=%s" % (getattr(bad, "last_attempts", -1), type(err).__name__))
    expect("B'-b1 读工具重试 %d 次" % READ_RETRIES,
           getattr(bad, "last_attempts", -1) == READ_RETRIES,
           "attempts=%d" % getattr(bad, "last_attempts", -1))
    expect("B'-b1 读工具最终抛 WrenTimeout", isinstance(err, WrenTimeout), "type=%s" % type(err).__name__)

    # b2 非读工具（写类）：不得重试
    bad2 = WrenClient("http://127.0.0.1:59999", name="DEAD", timeout=0.3)
    err2 = None
    try:
        bad2.call_raw("deploy_manifest", {"mdl": {}})  # 不在 READ_TOOLS
    except Exception as e:  # noqa: BLE001
        err2 = e
    print("  b2 tool=deploy_manifest（非读）→ attempts=%d  type=%s" % (getattr(bad2, "last_attempts", -1), type(err2).__name__))
    expect("B'-b2 非读工具只尝试 1 次（不重试）",
           getattr(bad2, "last_attempts", -1) == 1,
           "attempts=%d" % getattr(bad2, "last_attempts", -1))
    expect("B'-b2 非读工具失败亦抛 WrenTimeout（统一封装）", isinstance(err2, WrenTimeout),
           "type=%s" % type(err2).__name__)

    # b3 非读工具不 sleep
    sleeps = []
    orig_sleep = time.sleep
    time.sleep = lambda t: sleeps.append(t)
    try:
        bad3 = WrenClient("http://127.0.0.1:59999", name="DEAD", timeout=0.3)
        try:
            bad3.call_raw("deploy", {})
        except Exception:  # noqa: BLE001
            pass
    finally:
        time.sleep = orig_sleep
    print("  b3 非读工具失败 → sleep 调用次数 = %d" % len(sleeps))
    expect("B'-b3 非读工具不 backoff（sleep 0 次）", len(sleeps) == 0, "sleeps=%s" % sleeps)


# ---------------------------------------------------------------------------
# C') backoff 时长（交付方只测了次数，没测时长）
# ---------------------------------------------------------------------------
def test_C_backoff_values():
    print()
    print("[C'] backoff 时长：读侧重试的 sleep 参数应为 RETRY_BACKOFF 前两段")
    bad = WrenClient("http://127.0.0.1:59999", name="DEAD", timeout=0.3)
    sleeps = []
    orig_sleep = time.sleep
    time.sleep = lambda t: sleeps.append(t)
    try:
        try:
            bad.call_raw("query", {"sql": "SELECT 1"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        time.sleep = orig_sleep
    print("  sleep 参数 = %s（期望前两段 %s）" % (sleeps, list(RETRY_BACKOFF[:READ_RETRIES - 1])))
    expect("C'-1 backoff 序列 == RETRY_BACKOFF[:2]",
           sleeps == list(RETRY_BACKOFF[:READ_RETRIES - 1]),
           "实际=%s" % sleeps)


# ---------------------------------------------------------------------------
# D') 并发不串数据（交付方完全没测「不串」）
# ---------------------------------------------------------------------------
B1 = "SELECT 'TAG_B1' AS tag, store_id FROM dws_store_daily_agg LIMIT 3"
B2 = "SELECT 'TAG_B2' AS tag, COUNT(*) AS n FROM dws_store_daily_agg"
A1 = "SELECT 'TAG_A1' AS tag, order_id FROM orders LIMIT 3"
A2 = "SELECT 'TAG_A2' AS tag, COUNT(*) AS n FROM orders"
CASES = [("B", B1, "TAG_B1"), ("B", B2, "TAG_B2"), ("A", A1, "TAG_A1"), ("A", A2, "TAG_A2")]


def test_D1_client_no_crosstalk():
    print()
    print("[D'-1] WrenClient 层不串：并发 24 次发 4 种不同 SQL（A/B 混合），用 tag 指纹断言请求-响应一一对应")
    clients = {k: WrenClient(registry.get(k).wren_url, name=k) for k in ("A", "B")}
    jobs = []
    for i in range(24):
        jobs.append(CASES[i % 4])

    def _one(ds, sql, tag):
        try:
            r = clients[ds].query(sql)
            got = (r.get("data") or [[None]])[0][0]
            return (ds, tag, got, None)
        except Exception as e:  # noqa: BLE001
            return (ds, tag, None, "%s:%s" % (type(e).__name__, str(e)[:80]))

    mism = []
    errs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_one, ds, sql, tag) for (ds, sql, tag) in jobs]
        for f in as_completed(futs):
            ds, tag, got, e = f.result()
            if e:
                errs.append((ds, tag, e))
            elif got != tag:
                mism.append((ds, tag, got))
    print("  完成 %d 次｜错配 %d｜异常 %d" % (len(jobs), len(mism), len(errs)))
    if mism:
        print("     错配示例 =", mism[:3])
    if errs:
        print("     异常示例 =", errs[:3])
    expect("D'-1 24 次并发无异常", not errs, "errs=%s" % errs[:2])
    expect("D'-1 返回 tag 与请求 100% 对应（不串）", not mism, "mism=%s" % mism[:3])


def test_D2_execute_readonly_no_crosstalk():
    print()
    print("[D'-2] execute_readonly 层不串：4 个不同 demand 各配不同 SQL，并发 16 次，断言 sql_runs 留痕一一对应")
    # 每条 SQL 先用预审查干跑一次，blocked 的标记 skip（避免门禁差异误判为串）
    plans = []
    for tag, (ds, sql, t) in zip(["DB1", "DB2", "DA1", "DA2"], CASES):
        d = demand_mod.create(
            {"title": "M6-3 不串压测 %s" % tag, "business_context": "并发不串验证",
             "description": "验证留痕不串 %s" % tag, "expected_output": "tag,n",
             "contact": "验收方-13900000000"}, actor="verifier")
        did = d["demand_id"] if isinstance(d, dict) else d
        plans.append({"tag": tag, "dataset": ds, "sql": sql, "did": did})

    print("  4 个 demand =", [p["did"] for p in plans])

    def _one(p):
        r = sqlrun_mod.execute_readonly(p["sql"], dataset=p["dataset"], demand_id=p["did"])
        return (p["did"], p["sql"], r.get("ok") is not False, r.get("error"), r.get("sql_run_id"))

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_one, plans[i % 4]) for i in range(16)]
        for f in as_completed(futs):
            results.append(f.result())

    ok_n = sum(1 for r in results if r[2])
    print("  16 次中成功 %d（其余为预审查阻断或执行失败，逐条核对）" % ok_n)
    bad_runs = [r for r in results if not r[2]]
    if bad_runs:
        print("     非成功示例 =", [(r[0], str(r[3])[:70]) for r in bad_runs[:3]])

    # 核心断言：每个 demand 的 sql_runs 里的 generated_sql 集合，必须 == 该 demand 唯一那条 SQL
    polluted = []
    empty_demands = []
    for p in plans:
        rows = db.query("SELECT DISTINCT generated_sql FROM sql_runs WHERE demand_id=%s", (p["did"],))
        got = set(r["generated_sql"].strip() for r in rows)
        if not got:
            empty_demands.append(p["tag"])
        elif got != {p["sql"].strip()}:
            polluted.append((p["tag"], sorted(got)))
    print("  留痕核对：空 demand = %s｜被污染 demand = %s" % (empty_demands, [x[0] for x in polluted]))
    expect("D'-2 无跨 demand 留痕串写（无污染）", not polluted, "polluted=%s" % polluted[:2])
    expect("D'-2 成功路径全部落痕（成功的都查得到）",
           len(empty_demands) == 0,
           "空 demand=%s（可能因门禁阻断，见上）" % empty_demands)


# ---------------------------------------------------------------------------
# E') 异常类型规范化（打桩）
# ---------------------------------------------------------------------------
class _FakeOpener:
    def __init__(self, exc_factory):
        self._f = exc_factory
        self.n = 0

    def open(self, req, timeout=None):
        self.n += 1
        raise self._f()


def test_E_exception_normalization():
    print()
    print("[E'] 异常规范化（打桩 _opener.open）：超时类统一 WrenTimeout，禁裸抛")
    url = registry.get("B").wren_url

    def run(exc_factory, label):
        c = WrenClient(url, name="P", timeout=0.2)
        c._opener = _FakeOpener(exc_factory)
        err = None
        try:
            c.query("SELECT 1")
        except Exception as e:  # noqa: BLE001
            err = e
        print("  %s → type=%s  attempts=%d" % (label, type(err).__name__, getattr(c, "last_attempts", -1)))
        return c, err

    # e1 裸 socket.timeout
    c1, e1 = run(lambda: socket.timeout("timed out"), "e1 socket.timeout")
    expect("E'-e1 socket.timeout 被规范化为 WrenTimeout", isinstance(e1, WrenTimeout), "type=%s" % type(e1).__name__)
    expect("E'-e1 不是裸 socket.timeout", not isinstance(e1, socket.timeout))

    # e2 URLError(reason=socket.timeout)
    c2, e2 = run(lambda: urllib.error.URLError(socket.timeout("timed out")), "e2 URLError(socket.timeout)")
    expect("E'-e2 URLError(超时) 被规范化为 WrenTimeout", isinstance(e2, WrenTimeout), "type=%s" % type(e2).__name__)

    # e3 URLError(ConnectionRefused)
    c3, e3 = run(lambda: urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), "e3 URLError(refused)")
    expect("E'-e3 连接拒绝（读侧）最终 WrenTimeout", isinstance(e3, WrenTimeout), "type=%s" % type(e3).__name__)

    # e4 类型层次
    expect("E'-e4 WrenTimeout 是 WrenError 子类", issubclass(WrenTimeout, WrenError))


# ---------------------------------------------------------------------------
# F') session 失效重握手（原实现递归 → 新实现内联，行为须不变）
# ---------------------------------------------------------------------------
def test_F_session_rehandshake():
    print()
    print("[F'] session 失效重握手：第一次 HTTP 400『session expired』→ 重握手再调一次 → 成功")
    c = WrenClient(registry.get("B").wren_url, name="B")
    calls = {"n": 0}

    def fake_do_call(tool, args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "http://x", 400, "session expired", {}, io.BytesIO(b"session expired")
            )
        return {"result": {"content": [{"text": "OK"}]}}

    c._do_call = fake_do_call
    r = None
    err = None
    try:
        r = c.call_raw("query", {"sql": "SELECT 1"})
    except Exception as e:  # noqa: BLE001
        err = e
    print("  _do_call 调用次数 = %d  返回 = %s  异常 = %s" % (calls["n"], r, type(err).__name__ if err else None))
    expect("F'-1 首次 400(session) 触发重握手，_do_call 共调 2 次", calls["n"] == 2, "n=%d" % calls["n"])
    expect("F'-2 重握手后返回成功结果", isinstance(r, dict) and r.get("result"), "r=%r" % (r,))


# ---------------------------------------------------------------------------
# G') sqlrun 的 WREN_ERROR 失败包装真链路透出（M6-3 对 sqlrun 的唯一改动）
# ---------------------------------------------------------------------------
def test_G_sqlrun_wren_error():
    print()
    print("[G'] sqlrun WREN_ERROR 分支真链路：塞坏 client 到 _WREN_CLIENTS[B] → execute_readonly 透出 fallback 块")
    sql_ok = "SELECT store_id, SUM(sales_amount) AS gmv FROM dws_store_daily_agg GROUP BY store_id"
    d = demand_mod.create(
        {"title": "M6-3 WREN_ERROR 真链路", "business_context": "验证失败包装",
         "description": "门店GMV按门店汇总", "expected_output": "store_id,gmv",
         "contact": "验收方-13900000000"}, actor="verifier")
    did = d["demand_id"] if isinstance(d, dict) else d

    saved = sqlrun_mod._WREN_CLIENTS.get("B")
    sqlrun_mod._WREN_CLIENTS["B"] = WrenClient("http://127.0.0.1:59999", name="B", timeout=0.3)
    res = None
    try:
        res = sqlrun_mod.execute_readonly(sql_ok, dataset="B", demand_id=did)
    finally:
        if saved is not None:
            sqlrun_mod._WREN_CLIENTS["B"] = saved
        else:
            sqlrun_mod._WREN_CLIENTS.pop("B", None)

    fb = (res or {}).get("fallback") or {}
    we = fb.get("wren_error") or {}
    print("  ok=%s  fallback.kind=%s  wren_error.type=%s  attempts=%s" % (
        (res or {}).get("ok"), fb.get("kind"), we.get("type"), we.get("attempts")))
    print("  error[:150] = %s" % str((res or {}).get("error"))[:150])
    expect("G'-1 execute_readonly 返回 ok=False", (res or {}).get("ok") is False)
    expect("G'-2 fallback.kind == 'WREN_ERROR'", fb.get("kind") == "WREN_ERROR", "kind=%s" % fb.get("kind"))
    expect("G'-3 wren_error.type == 'WrenTimeout'（读侧重试后）", we.get("type") == "WrenTimeout", "type=%s" % we.get("type"))
    expect("G'-4 wren_error.attempts == %d" % READ_RETRIES, we.get("attempts") == READ_RETRIES,
           "attempts=%s" % we.get("attempts"))
    expect("G'-5 error 文本含类型与 attempts", "WrenTimeout" in str((res or {}).get("error"))
           and "attempts=3" in str((res or {}).get("error")),
           str((res or {}).get("error"))[:120])
    expect("G'-6 review 字段保留（预审查包）", isinstance((res or {}).get("review"), dict))


def main():
    print("M6-3 验收方独立复扫。")
    print("  READ_TOOLS =", sorted(READ_TOOLS))
    test_A_env_timeout_chain()
    test_B_retry_branch()
    test_C_backoff_values()
    test_D1_client_no_crosstalk()
    test_D2_execute_readonly_no_crosstalk()
    test_E_exception_normalization()
    test_F_session_rehandshake()
    test_G_sqlrun_wren_error()
    print()
    print("==================== 总览 ====================")
    if not FAIL:
        print("全部通过（0 硬失败）")
        return 0
    print("%d 条硬失败：" % len(FAIL))
    for f in FAIL:
        print(" ", f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
