# -*- coding: utf-8 -*-
"""知识底座链路独立复核（门禁 G2）

为什么值得单独立一份
--------------------
2026-10-16 实测到的盲区：把知识引擎重建之后，第一次调 `knowledge_search`
返回 **0 条**、`ok=false`，而 **G0/G1/G2 全部全绿**。原因是

- G2 的两个脚本一个验工具面契约、一个验 `sql_review`，**都不覆盖知识检索**；
- 而 RAGFlow 的容器健康探针只 `curl http://127.0.0.1/`，**不探 ES 索引与租户表**，
  重建后有一段预热期，容器已经是 healthy 但检索还不能用。

也就是说：**「容器 healthy」和「门禁全绿」都不等于「功能可用」。**
本脚本把「知识侧真的能用」变成门禁的一条判据。

判据分层（避免一种故障伪装成另一种）
------------------------------------
    L1 底座可达      knowledge_health.ok 且 datasets_visible ≥ 1
    L2 有文档必可检索  documents ≥ 1 ⇒ knowledge_search 必须 ok 且引用非空
    L3 附件存储可用  attachments.ok（对象存储刚从自建 MinIO 切到 EKOS 的 ekos-minio，
                      同属"换了的外部依赖"，门禁此前同样不覆盖）

两个刻意的设计
--------------
1. **预热期不是缺陷**：实测 RAGFlow 从重启到可用需 **约 173 秒**，
   所以底座探测（L1）带 **约 210 秒**的重试窗口；全窗口都没恢复才判红，
   并在结论里写明"判定为真故障而非预热"。否则每次重启知识栈都会假红一次——
   **假红比不判更糟，大家会习惯性忽略门禁**。正常时第 1 次探测即通过，门禁仍是 6 秒内跑完。
2. **参数名从 tools/list 读，不写死**：`knowledge_search` 的入参名是 `question`
   （不是 `query`，写错会报 pydantic validation error）。从 `tools/list` 的
   inputSchema 里取第一个 required 字段，将来改参数名不会让门禁假红。

用法：
    python tools/knowledge_link_check.py [http://127.0.0.1:18080/mcp]
退出码 0 = 全过，1 = 有失败，2 = 环境不满足（网关不可达，无法判定）。
"""
import json
import sys
import time
import urllib.error
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18080/mcp"
_SID = {"v": None}
_RID = {"v": 0}

# 预热重试节奏（秒）。**实测 RAGFlow 从 restart 到 knowledge_health.ok=true 约 173 秒**
# （2026-10-16，5 秒一采样的逐秒观测），所以窗口必须 ≥ 3 分钟，否则每次重启知识栈
# 门禁都会假红一次 —— 假红比不判更糟，大家会习惯性忽略门禁。
# 合计等待 195 秒 + 探测开销 ≈ 210 秒，门禁单脚本上限是 300 秒（SINGLE_TIMEOUT），留得住余量。
# 正常情况下第 1 次探测就通过，**整个门禁仍是 6 秒内跑完**，慢路径只在真出问题时才走。
RETRY_DELAYS = (15, 30, 60, 90)


# ---------------------------------------------------------------- MCP 基础
def rpc(method, params=None, notify=False):
    _RID["v"] += 1
    payload = {"jsonrpc": "2.0", "method": method}
    if not notify:
        payload["id"] = _RID["v"]
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(URL, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    if _SID["v"]:
        req.add_header("Mcp-Session-Id", _SID["v"])
    with urllib.request.urlopen(req, timeout=90) as resp:
        sid = resp.headers.get("mcp-session-id")
        if sid:
            _SID["v"] = sid
        body = resp.read().decode()
    if notify:
        return None
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def payload_of(res):
    """兼容 fastmcp 的 structuredContent / content[0].text 两种回传形态。"""
    r = (res or {}).get("result", {})
    sc = r.get("structuredContent")
    if sc:
        return sc.get("result", sc)
    for c in r.get("content", []) or []:
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except Exception:
                return c["text"]
    return r


def call(name, args):
    return payload_of(rpc("tools/call", {"name": name, "arguments": args}))


def search_arg_name(tools):
    """从 tools/list 的 inputSchema 里读出检索工具的必填参数名，不写死。"""
    for t in tools:
        if t.get("name") != "knowledge_search":
            continue
        req = ((t.get("inputSchema") or {}).get("required") or [])
        if req:
            return req[0]
        props = list(((t.get("inputSchema") or {}).get("properties") or {}).keys())
        return props[0] if props else "question"
    return "question"


# ---------------------------------------------------------------- 判据
def check_health(components):
    """L1 底座可达（单次判定，不含重试）。返回 (ok, 说明, 诊断线索)。"""
    k = (components or {}).get("knowledge") or {}
    if not k:
        return False, "健康探针里没有 knowledge 分量", "网关版本可能不含知识探针"
    if not k.get("ok"):
        return False, "knowledge_health.ok=false（base=%s endpoint=%s）" % (
            k.get("base"), k.get("endpoint")), (
            "① 19380 入口是否可达：curl -o /dev/null -w '{http_code}' "
            "http://127.0.0.1:19380/api/v1/datasets（401 才是正常）；"
            "② RAGFlow 容器健康不等于可检索，看 knowledge-overlay/ragflow-upstream/"
            "ragflow-logs 里的 task_executor 日志；"
            "③ 刚重建的话先等 1-2 分钟预热")
    vis = k.get("datasets_visible")
    if not vis:
        return False, "知识库不可见（datasets_visible=%s）" % vis, (
            "检查 RAGFlow 的 API Key / 租户配置，网关侧 KNOWLEDGE_API_KEY")
    ds = k.get("dataset") or {}
    return True, "可达：知识库「%s」%s 篇文档（%sms）" % (
        ds.get("name"), ds.get("documents"), k.get("ms")), ""


def probe_health_with_retry(log):
    """L1 带预热期重试。

    为什么重试必须在 L1 而不是只在 L2：RAGFlow 重建后**先是 knowledge_health.ok=false**，
    预热完成后才转true。若 L1 不重试，重启知识栈后第一次跑门禁必然假红，
    重试机制形同虚设。实测：RAGFlow 刚启动时 L1 立刻失败、耗时仅 1 秒。
    """
    last = (False, "未探测", "")
    for i, delay in enumerate([0] + list(RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
            log("   …L1 预热重试 %d/%d（再等 %ds；实测预热约 173s，本窗口共 %ds）"
                % (i, len(RETRY_DELAYS), delay, sum(RETRY_DELAYS)))
        r = check_health((call("gateway_health", {}) or {}).get("components") or {})
        if r[0]:
            return r
        last = r
    # 整个窗口（约 3.5 分钟）都没恢复 ⇒ 不再是预热，是真故障。
    last = (False, last[1] + "（已按 %ds 窗口重试完，判定为真故障而非预热）" % sum(RETRY_DELAYS),
            last[2])
    return last


def check_search(tools, docs, log, base_ok=True):
    """L2 有文档就必须能检索到。带预热期重试。

    base_ok=False（底座已判失败）时**不重试**：底座都不可达，重试 17 秒纯属浪费，
    且会把"底座挂了"与"检索坏了"两种故障混成一个结论。
    """
    if not docs:
        # 注意：docs=0 有两种成因 ——「知识库本来就是空的」与「底座挂了读不到」。
        # 只有底座可达时，docs=0 才真的意味着没有数据；底座已判失败时必须判红，
        # 否则「知识侧整个挂了」会被伪装成「没数据所以跳过」。
        if not base_ok:
            return False, "无法判定：知识底座不可达，文档数读不到（不是「无文档」）", (
                "L1 已判失败，此项依赖它的结论；修好 L1 后本项自动恢复")
        print("   [SKIP] 知识库暂无文档，不做检索判据（不是缺陷）")
        return True, "无文档，跳过", ""
    arg = search_arg_name(tools)
    tries = [0] + (list(RETRY_DELAYS) if base_ok else [])
    last = None
    for i, delay in enumerate(tries):
        if delay:
            time.sleep(delay)
            log("   …预热重试 %d/%d（再等 %ds）" % (i, len(RETRY_DELAYS), delay))
        d = call("knowledge_search", {arg: "会员等级"})
        cites = (d or {}).get("citations") or []
        if (d or {}).get("ok") and cites:
            top = cites[0]
            return True, "命中 %s 条，引用 %d 条，首条相似度 %s（%s）" % (
                (d or {}).get("total"), len(cites),
                top.get("similarity"), top.get("document_name")), ""
        last = d
    return False, "检索失败：ok=%s 引用=%d 条 原始返回=%s" % (
        (last or {}).get("ok"), len((last or {}).get("citations") or []),
        str(last)[:160]), (
        "底座可达但检索不到，多半是 embedding 链路或 ES 索引："
        "① 本机 TEI 是否活着（askoda-tei-embedding:18002）；"
        "② RAGFlow 租户的 base_url 是否指向它；"
        "③ ES 索引是否已建（docker logs 看 HEAD .../ragflow_doc_meta_* 的 status）")


def check_attachments(components):
    """L3 附件存储可用。"""
    a = (components or {}).get("attachments") or {}
    if not a:
        return False, "健康探针里没有 attachments 分量", "网关版本可能不含附件探针"
    if not a.get("ok"):
        return False, "attachments.ok=false（endpoint=%s）" % a.get("endpoint"), (
            "对象存储已从自建 MinIO 切到 EKOS 的 ekos-minio（宿主 19000）："
            "确认 ekos-minio 在跑，且 .env 里的 MINIO_* 已填")
    if not a.get("bucket_exists"):
        return False, "桶不存在（bucket=%s）" % a.get("bucket"), "先建桶再判过"
    return True, "可用：endpoint=%s bucket=%s" % (a.get("endpoint"), a.get("bucket")), ""


# ---------------------------------------------------------------- 主流程
def main():
    log = lambda s: print(s, flush=True)

    print("=" * 62)
    print("知识底座链路独立复核 · %s" % URL)
    print("=" * 62)

    try:
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "knowledge-link", "version": "1.0"}})
        rpc("notifications/initialized", notify=True)
    except Exception as e:
        print("网关不可达（%s: %s）" % (type(e).__name__, str(e)[:120]))
        print("→ 无法判定知识侧是否可用。请先起网关：docker compose up -d gateway")
        return 2

    tools = sorted((rpc("tools/list")["result"]["tools"] or []), key=lambda t: t["name"])
    for need in ("gateway_health", "knowledge_search"):
        if need not in {t["name"] for t in tools}:
            print("❌ 工具面缺少 %s，本项无法判定" % need)
            return 1

    print("\n--- L1 知识底座可达（带预热重试）---")
    ok1, msg1, hint1 = probe_health_with_retry(log)
    print("   [%s] %s" % ("OK" if ok1 else "FAIL", msg1))

    docs = 0
    try:
        kh = call("knowledge_health", {}) or {}
        docs = ((kh.get("dataset") or {}).get("documents")) or 0
    except Exception:
        pass

    print("\n--- L2 知识检索（参数名从 tools/list 读：%s）---" % search_arg_name(tools))
    ok2, msg2, hint2 = check_search(tools, docs, log, base_ok=ok1)
    if not ok1:
        msg2 += "（底座已判失败，本项未重试）"
    print("   [%s] %s" % ("OK" if ok2 else "FAIL", msg2))

    print("\n--- L3 附件存储 ---")
    ok3, msg3, hint3 = check_attachments((call("gateway_health", {}) or {}).get("components") or {})
    print("   [%s] %s" % ("OK" if ok3 else "FAIL", msg3))

    fails = []
    if not ok1:
        fails.append("L1 知识底座不可达：%s" % msg1)
    if not ok2:
        fails.append("L2 知识检索失败：%s" % msg2)
    if not ok3:
        fails.append("L3 附件存储不可用：%s" % msg3)

    print("\n" + "=" * 62)
    print("知识链路复核：%s（失败 %d 项）" % ("全部通过" if not fails else "存在问题", len(fails)))
    for i, f in enumerate(fails):
        print("  - " + f)
        hint = {0: hint1, 1: hint2, 2: hint3}.get(i, "")
        if hint:
            print("      排查：%s" % hint)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())