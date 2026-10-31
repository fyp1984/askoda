#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库运维 CLI（走网关 MCP，与产品同一条链路）

为什么需要它
------------
CR009 定的是「客户端不设知识管理入口，知识收集/治理在 WeKnora 平台完成」。
但交付与验收时，总得有人把文档放进去，而且要能**当场证明**放进去了、解析完了、
搜得到了。原来的 `tools/ingest_knowledge.py` 直连 RAGFlow 原生接口，而
2026-10-30 知识底座已切到 WeKnora MCP——那个脚本现在只剩历史价值。

本 CLI 不复刻任何一家的原生接口，而是**调网关自己的工具**：
`knowledge_list_kbs` / `knowledge_upload` / `knowledge_documents` /
`knowledge_document_status` / `knowledge_search` / `knowledge_delete`。
好处是它验证的正是产品真实消费的那条路径（准入 → 解析 → 可检索），
不是另一条旁路。MCP 协议三步见 `bff/mcp_client.py`。

用法
----
    export PYTHONPATH=tools/_pyshim          # 本机 python 需要 tempfile 垫片
    python3 tools/kb_cli.py list             # 有哪些知识库
    python3 tools/kb_cli.py docs             # 默认库里有哪些文档、解析状态
    python3 tools/kb_cli.py upload knowledge/05-xxx.md --actor 张工
    python3 tools/kb_cli.py wait <doc_id>    # 轮询到 DONE（解析是异步的）
    python3 tools/kb_cli.py search "坪效怎么算"
    python3 tools/kb_cli.py delete <doc_id>  # 撤库（谨慎）

    # 多库时用 --kb 指定；不指定一律回落服务端默认库
    python3 tools/kb_cli.py docs --kb 77888776-...

退出码：0 成功；1 参数/本地错误；2 网关或工具报错。脚本可进 CI 断言。
"""
import argparse
import base64
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request

GATEWAY = os.environ.get("ASKODA_GATEWAY_URL", "http://127.0.0.1:18080/mcp")
PROTOCOL = os.environ.get("ASKODA_MCP_PROTOCOL", "2025-06-18")

# 本机有系统代理，127.0.0.1 会被代理吞掉 ⇒ 显式空 ProxyHandler。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class McpError(RuntimeError):
    pass


def _parse_sse(body):
    """网关回的是 SSE；取第一个 data: 行的 JSON。"""
    for line in body.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if raw:
                return json.loads(raw)
    raise McpError("响应体不是 SSE 格式：%s" % body[:200])


class Gateway:
    def __init__(self, url=GATEWAY, timeout=180):
        self.url = url
        self.timeout = timeout
        self.sid = None
        self.rid = 0

    def _post(self, payload, sid=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            headers["Mcp-Session-Id"] = sid
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode(errors="replace")
        except urllib.error.URLError as e:
            raise McpError("网关不可达（%s）：%s" % (self.url, e))

    def connect(self):
        if self.sid:
            return self.sid
        status, headers, body = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "askoda-kb-cli", "version": "1.0.0"}},
        })
        if status != 200:
            raise McpError("initialize 返回 %s：%s" % (status, body[:200]))
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if not sid:
            raise McpError("initialize 缺少 Mcp-Session-Id 响应头")
        # 通知不带 id；202 无 body 属正常，失败可忽略。
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        except McpError:
            pass
        self.sid = sid
        return sid

    def call(self, tool, args=None):
        sid = self.connect()
        self.rid += 1
        status, _, body = self._post({
            "jsonrpc": "2.0", "id": self.rid, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        }, sid)
        if status == 404:
            # 会话失效：重建一次。网关重启后旧 session 会 404。
            self.sid = None
            sid = self.connect()
            status, _, body = self._post({
                "jsonrpc": "2.0", "id": self.rid, "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}},
            }, sid)
        if status >= 400:
            raise McpError("%s 返回 %s：%s" % (tool, status, body[:300]))
        msg = _parse_sse(body)
        if "error" in msg:
            raise McpError("%s 调用失败：%s" % (tool, msg["error"]))
        result = msg.get("result", {})
        if result.get("isError"):
            content = result.get("content") or [{}]
            raise McpError("%s 报错：%s" % (tool, str(content[0].get("text", ""))[:300]))
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        content = result.get("content") or []
        if content:
            try:
                return json.loads(content[0].get("text", ""))
            except (json.JSONDecodeError, AttributeError):
                return content[0].get("text", "")
        return {}


# ---------------------------------------------------------------- 子命令
def cmd_list(gw, a):
    r = gw.call("knowledge_list_kbs")
    print("底座：%s" % r.get("base", "-"))
    print("知识库 %d 个：" % r.get("total", 0))
    for kb in r.get("kbs", []):
        mark = "  ← 默认" if kb.get("is_default") else ""
        print("  %s  %s%s" % (kb.get("id"), kb.get("name"), mark))
    return 0


def cmd_docs(gw, a):
    r = gw.call("knowledge_documents", {"limit": a.limit, "kb_id": a.kb})
    docs = r.get("documents", [])
    print("文档 %d 份（kb=%s）：" % (r.get("total", len(docs)), r.get("dataset_id", a.kb or "默认")))
    for d in docs:
        print("  %-36s %-6s %s" % (d.get("id"), d.get("run"), d.get("name")))
        if d.get("parse_error"):
            print("      ⚠ %s" % d["parse_error"])
    return 0


def cmd_upload(gw, a):
    path = a.file
    if not os.path.isfile(path):
        print("文件不存在：%s" % path, file=sys.stderr)
        return 1
    raw = open(path, "rb").read()
    ctype = mimetypes.guess_type(path)[0] or "text/markdown"
    r = gw.call("knowledge_upload", {
        "filename": os.path.basename(path),
        "content_base64": base64.b64encode(raw).decode(),
        "content_type": ctype,
        "actor": a.actor,
        "kb_id": a.kb,
    })
    if not r.get("ok", True):
        print("上传失败：%s" % json.dumps(r, ensure_ascii=False)[:400], file=sys.stderr)
        return 2
    # 网关返回的是 `document_ids`（列表，一次可传多份）；单份场景取第 0 个。
    ids = r.get("document_ids") or []
    doc_id = (ids[0] if ids else "") or r.get("document_id") or r.get("id") or ""
    print("已上传：%s（%d 字节）" % (os.path.basename(path), len(raw)))
    print("  document_id = %s" % (doc_id or "-"))
    if r.get("note"):
        print("  说明：%s" % r["note"])
    print("  ⚠ 解析是异步的，用 `wait %s` 等到 DONE 才算可检索。" % (doc_id or "<document_id>"))
    return 0


def cmd_wait(gw, a):
    for i in range(a.tries):
        r = gw.call("knowledge_document_status", {"document_id": a.doc_id, "limit": 50})
        docs = [d for d in r.get("documents", []) if d.get("id") == a.doc_id]
        run = (docs[0].get("run") if docs else "") or "?"
        print("  [%02d] run=%s" % (i + 1, run))
        if run in ("DONE", "FAIL"):
            return 0 if run == "DONE" else 2
        time.sleep(a.interval)
    print("超时未到 DONE（异步解析可能要更久，可加大 --tries）", file=sys.stderr)
    return 2


def cmd_search(gw, a):
    r = gw.call("knowledge_search", {"question": a.question, "top_k": a.top_k, "kb_id": a.kb})
    cites = r.get("citations") or r.get("items") or []
    # 网关返回的片段字段是 `snippet`（不是 content/text）；`similarity` 才是分数键。
    print("命中 %d 条（%sms）：" % (len(cites), r.get("elapsed_ms", "-")))
    for i, c in enumerate(cites, 1):
        print("  %d) %s  score=%s" % (i, c.get("document_name", "-"), c.get("similarity", "-")))
        text = (c.get("snippet") or c.get("content") or "").replace("\n", " ")
        print("     %s" % text[:160])
    if not cites:
        print("  （空结果：先确认文档 run=DONE，再确认问法与文档用词一致）")
        return 2
    return 0


def cmd_delete(gw, a):
    if not a.yes:
        print("撤库是不可逆动作。确认无误请加 --yes。", file=sys.stderr)
        return 1
    r = gw.call("knowledge_delete", {"document_id": a.doc_id, "actor": a.actor})
    print("已撤库：%s" % json.dumps(r, ensure_ascii=False)[:300])
    return 0


def main():
    p = argparse.ArgumentParser(description="Askoda 知识库运维 CLI（经网关 MCP）")
    p.add_argument("--gateway", default=GATEWAY, help="网关 MCP 地址")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="列出全部知识库")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("docs", help="列出文档与解析状态")
    s.add_argument("--kb", default="")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_docs)

    s = sub.add_parser("upload", help="上传一份文档并触发解析")
    s.add_argument("file")
    s.add_argument("--kb", default="")
    s.add_argument("--actor", default="kb-cli")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("wait", help="轮询解析进度直到 DONE")
    s.add_argument("doc_id")
    s.add_argument("--tries", type=int, default=30)
    s.add_argument("--interval", type=float, default=3.0)
    s.set_defaults(func=cmd_wait)

    s = sub.add_parser("search", help="检索知识并打印来源")
    s.add_argument("question")
    s.add_argument("--kb", default="")
    s.add_argument("--top-k", type=int, default=5)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("delete", help="撤库一份文档（需 --yes）")
    s.add_argument("doc_id")
    s.add_argument("--actor", default="kb-cli")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_delete)

    a = p.parse_args()
    gw = Gateway(a.gateway)
    try:
        return a.func(gw, a)
    except McpError as e:
        print("✗ %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
