# -*- coding: utf-8 -*-
"""知识检索（MCP 统一接入后端 · 2026-10-30）

定位
----
`knowledge.py` 的**可切换后端**之一：当 `KNOWLEDGE_BACKEND=mcp-*` 时，
`knowledge.py` 文件末尾的分发块把 `health / search / list_documents /
upload_document / document_status / delete_document` 六个后端耦合函数重绑到本模块，
公共函数面与返回形状逐键对齐 RAGFlow 直连版——所有消费方
（app.py / analysis.py / knowledge_backflow.py / tools/*）零改动。

产品架构对应（CR008）
----------------------
WeKnora（或其它知识库平台）为知识底座，应用经 **MCP** 接入：
底座侧建 MCP endpoint（WeKnora: `POST /api/v1/mcp-endpoints`，绑定知识库、
启用读写工具），应用侧只持有一个 MCP URL + Token，不直连底座 REST。

协议（streamable-http JSON-RPC，已在 WeKnora v0.8.3 实测）
------------------------------------------------------------
1. `initialize` → 若响应带 `Mcp-Session-Id` 则后续携带（WeKnora 无状态，不带）；
2. `notifications/initialized` → 仅在有会话时需要；
3. `tools/call` → 响应体为 SSE，需解析 `data:` 行取 JSON。
本机有系统代理：必须 `trust_env=False`，否则 127.0.0.1/host.docker.internal 会被代理吞掉
（与 bff/mcp_client.py 同一实测结论）。

Provider 差异（诚实标注）
--------------------------
- `weknora`：9+3 工具全部实测（检索/列表/读/写/wiki），本文件全函数可用；
- `ragflow`：官方 MCP 仅暴露 `retrieval` 检索工具（栈当前未运行，**未实测**），
  管理类操作如实抛 KnowledgeError 指回直连模式，不假装可用。

与 RAGFlow 直连版的契约差异（已映射）
--------------------------------------
- 鉴权：`Authorization: Bearer mcp_*`（非 X-API-Key）；
- 检索：`search_knowledge(query, knowledge_base_ids, limit, mode)`，
  返回 XML 文本（chunk 属性含 rank/chunk_id/knowledge_id/knowledge_title/score）；
  **无 threshold / vector_weight 入参**（WeKnora 混合检索内部自控，参数被记录不透传）；
- 引用：无页级坐标（positions 恒空，locator 如实标注 n/a）；
  无向量/词项分项（vector_similarity / term_similarity 置 None，下游无消费者）；
- 上传：`add_document` 一步入库即触发解析（无独立 parse 步），仅支持文本/markdown 内容；
- 删除：仅按 knowledge_id（UUID），不支持按名删除；
- 解析状态：`parse_status` 语义同名（pending/processing/finalizing/completed/failed）。
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

# ---------------------------------------------------------------------------
# 复用 knowledge.py 的本地通用件（本模块由 knowledge.py 末尾分发块导入，
# 此刻 knowledge 模块已完成除分发块外的全部定义，可安全引用）
# ---------------------------------------------------------------------------
import knowledge as _kf

KnowledgeError = _kf.KnowledgeError
extract_keywords = _kf.extract_keywords
scan_oversized_cells = _kf.scan_oversized_cells

# 后端标识（health() 的 base 字段；不与 RAGFlow 版 BASE_NAME 混用）
MCP_PROVIDER = os.getenv("KNOWLEDGE_MCP_PROVIDER", "weknora").strip().lower()
MCP_URL = os.getenv("KNOWLEDGE_MCP_URL", "").strip()
MCP_TOKEN = os.getenv("KNOWLEDGE_MCP_TOKEN", "").strip()
MCP_TIMEOUT = int(os.getenv("KNOWLEDGE_MCP_TIMEOUT", "60"))
# 默认知识库 ID：优先 KNOWLEDGE_KB_IDS（WeKnora knowledge_base_id，逗号分隔多个），
# 未配置时回退 KNOWLEDGE_DATASET_ID（后端为 RAGFlow 时两者同义的兼容口径）。
KB_IDS = [
    x.strip()
    for x in os.getenv("KNOWLEDGE_KB_IDS", os.getenv("KNOWLEDGE_DATASET_ID", "")).split(",")
    if x.strip()
]
BASE_NAME = os.getenv("KNOWLEDGE_BASE_NAME", "MCP 知识底座（%s）" % MCP_PROVIDER).strip()

DEFAULT_TOP_K = _kf.DEFAULT_TOP_K
SNIPPET_CHARS = _kf.SNIPPET_CHARS
ALLOWED_SUFFIXES = _kf.ALLOWED_SUFFIXES
MAX_UPLOAD_BYTES = _kf.MAX_UPLOAD_BYTES
MAX_CELL_REPORT = _kf.MAX_CELL_REPORT

# provider → 工具名映射。ragflow 官方 MCP 只暴露 retrieval（检索）。
_TOOLS = {
    "weknora": {
        "search": "search_knowledge",
        "list_documents": "list_documents",
        "read_document": "read_document",
        "add_document": "add_document",
        "delete_document": "delete_document",
        "list_knowledge_bases": "list_knowledge_bases",
    },
    "ragflow": {"search": "retrieval"},
}


# ---------------------------------------------------------------------------
# MCP 客户端（streamable-http JSON-RPC，带会话探测与单次重连）
# ---------------------------------------------------------------------------
class _McpClient:
    """最小 MCP 客户端：只实现本适配层需要的 initialize / tools/call / tools/list。"""

    def __init__(self) -> None:
        self._session_id = ""
        self._ready = False

        # 空代理表 = trust_env=False 等价物：本机系统代理会吞掉
        # 127.0.0.1 / host.docker.internal（与 bff/mcp_client.py 同一实测结论）。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _headers(self) -> dict:
        h = {
            "Authorization": "Bearer %s" % MCP_TOKEN,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    @staticmethod
    def _parse_body(raw: str) -> dict:
        """SSE 或纯 JSON 响应统一解析出 JSON-RPC 信封。"""
        for line in raw.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        return json.loads(raw)

    def _post(self, payload: dict, timeout: float | None = None) -> tuple:
        """POST JSON-RPC，返回 (status_code, headers, text)。"""
        if not MCP_URL:
            raise KnowledgeError("KNOWLEDGE_MCP_URL 未配置，无法连接 MCP 知识底座")
        if not MCP_TOKEN:
            raise KnowledgeError("KNOWLEDGE_MCP_TOKEN 未配置，MCP endpoint 鉴权失败")
        req = urllib.request.Request(
            MCP_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=timeout or MCP_TIMEOUT) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), (e.read() or b"").decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise KnowledgeError("MCP 底座不可达：URLError: %s" % str(e.reason)[:180])
        except TimeoutError:
            raise KnowledgeError("MCP 底座响应超时（%ss）" % (timeout or MCP_TIMEOUT))

    def _rpc(self, payload: dict, timeout: float | None = None) -> dict:
        status, headers, text = self._post(payload, timeout=timeout)
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if status == 404 and self._session_id:
            # 会话失效：重置后单次重试（与 bff/mcp_client.py 同策略）
            self._session_id = ""
            self._ready = False
            status, headers, text = self._post(payload, timeout=timeout)
        if status >= 400:
            raise KnowledgeError("MCP 请求被拒 HTTP %s：%s" % (status, text[:200]))
        envelope = self._parse_body(text)
        if envelope.get("error"):
            err = envelope["error"]
            raise KnowledgeError(
                "MCP 工具调用失败 code=%s：%s" % (err.get("code"), str(err.get("message"))[:300])
            )
        return envelope.get("result") or {}

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        self._rpc({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "askoda-gateway", "version": "1.0"},
            },
        })
        if self._session_id:
            # 有状态服务端才需要 initialized 通知（无 id 字段；202 无 body 属正常）
            try:
                self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except KnowledgeError:
                pass
        self._ready = True

    def call(self, tool: str, arguments: dict, timeout: float | None = None) -> dict:
        """调用 MCP 工具，返回 result（含 content / isError）。"""
        self._ensure_ready()
        return self._rpc({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }, timeout=timeout)

    def call_text(self, tool: str, arguments: dict, timeout: float | None = None) -> str:
        """调用工具并取首个文本 content；工具级失败（isError）如实抛错。"""
        result = self.call(tool, arguments, timeout=timeout)
        if result.get("isError"):
            texts = [c.get("text", "") for c in (result.get("content") or [])]
            raise KnowledgeError("MCP 工具 %s 执行失败：%s" % (tool, "；".join(texts)[:300]))
        for c in (result.get("content") or []):
            if c.get("type") == "text" or isinstance(c.get("text"), str):
                return c.get("text", "")
        return ""

    def call_json(self, tool: str, arguments: dict, timeout: float | None = None) -> Any:
        """调用工具并把文本 content 解析成 JSON（WeKnora 结构化工具均回 JSON 文本）。"""
        text = self.call_text(tool, arguments, timeout=timeout)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise KnowledgeError("MCP 工具 %s 返回非 JSON：%s…（%s）" % (tool, text[:120], e))


def _tool(name: str) -> str:
    mapping = _TOOLS.get(MCP_PROVIDER)
    if mapping is None:
        raise KnowledgeError("未知 KNOWLEDGE_MCP_PROVIDER=%s（支持 weknora / ragflow）" % MCP_PROVIDER)
    t = mapping.get(name)
    if not t:
        raise KnowledgeError(
            "provider=%s 的 MCP 未提供 %s 能力（官方仅暴露检索类工具）；"
            "该操作请改用对应底座的直连模式" % (MCP_PROVIDER, name)
        )
    return t


def _resolve_kb_ids(dataset_ids=None) -> list:
    ids = [str(x).strip() for x in (dataset_ids or KB_IDS) if str(x).strip()]
    if not ids:
        # 未配置时探测：取 endpoint 作用域内第一个知识库（list_knowledge_bases 本身
        # 就只返回已绑定到该 endpoint 的库，作用域天然收敛）
        data = _CLIENT.call_json(_tool("list_knowledge_bases"), {})
        kbs = data.get("knowledge_bases") or []
        ids = [k.get("id") for k in kbs if k.get("id")]
    if not ids:
        raise KnowledgeError("未指定 knowledge_base_ids，且未配置 KNOWLEDGE_DATASET_ID")
    return ids


_CLIENT = _McpClient()


# ---------------------------------------------------------------------------
# 后端函数（公共函数面与返回形状对齐 knowledge.py 的 RAGFlow 直连版）
# ---------------------------------------------------------------------------
def health():
    """MCP 底座连通性 + 作用域内知识库可见性。"""
    t0 = time.time()
    try:
        data = _CLIENT.call_json(_tool("list_knowledge_bases"), {})
        kbs = data.get("knowledge_bases") or []
        target = None
        for kb in kbs:
            if not KB_IDS or kb.get("id") in KB_IDS:
                target = kb
                break
        if target is None and kbs:
            target = kbs[0]
        docs = None
        if target and "list_documents" in _TOOLS.get(MCP_PROVIDER, {}):
            try:
                d = _CLIENT.call_json(_tool("list_documents"), {
                    "knowledge_base_id": target["id"], "page": 1, "page_size": 1,
                })
                docs = d.get("total")
            except KnowledgeError:
                docs = None
        return {
            "ok": True,
            "base": BASE_NAME,
            "endpoint": MCP_URL,
            "ms": int((time.time() - t0) * 1000),
            "datasets_visible": len(kbs),
            "dataset": (
                {
                    "id": target.get("id"),
                    "name": target.get("name"),
                    "documents": docs,
                    "chunk_method": None,
                    "embedding_model": None,
                }
                if target
                else None
            ),
        }
    except KnowledgeError as e:
        return {
            "ok": False,
            "base": BASE_NAME,
            "endpoint": MCP_URL,
            "error": str(e)[:200],
            "ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:  # 与 RAGFlow 版一致：health 不抛，如实报 ok=False
        return {
            "ok": False,
            "base": BASE_NAME,
            "endpoint": MCP_URL,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "ms": int((time.time() - t0) * 1000),
        }


def list_kbs():
    """列出 MCP 端点作用域内**全部**可见知识库（不过滤，供客户端选库）。

    与 health 的差异：health 只报默认作用域（KNOWLEDGE_KB_IDS）命中那个库；
    本函数把 token 可见的库全部列出并标注 is_default，客户端据此做
    「指定知识库 ID」的逻辑隔离与引用授权选择。
    """
    t0 = time.time()
    try:
        data = _CLIENT.call_json(_tool("list_knowledge_bases"), {})
        kbs = data.get("knowledge_bases") or []
        rows = [
            {
                "id": kb.get("id"),
                "name": kb.get("name"),
                "is_default": (kb.get("id") in KB_IDS) if KB_IDS else False,
                "description": kb.get("description") or "",
            }
            for kb in kbs
            if kb.get("id")
        ]
        return {
            "ok": True,
            "base": BASE_NAME,
            "endpoint": MCP_URL,
            "ms": int((time.time() - t0) * 1000),
            "total": len(rows),
            "kbs": rows,
        }
    except KnowledgeError as e:
        return {"ok": False, "base": BASE_NAME, "error": str(e)[:200]}
    except Exception as e:
        return {
            "ok": False,
            "base": BASE_NAME,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
        }


def _parse_search_xml(text: str) -> list:
    """解析 search_knowledge 的 XML 结果为 chunk 字典列表。

    形态（实测）：<search_results count mode><query/><chunk rank chunk_id
    chunk_index knowledge_id knowledge_base_id knowledge_title score>
    <match_snippet/><content/></chunk>…。XML 解析失败时回退正则抽属性
    （小模型场景下不排除上游输出转义不严格）。
    """
    chunks = []
    try:
        root = ET.fromstring(text)
        for node in root.iter("chunk"):
            chunks.append({
                "rank": node.get("rank"),
                "chunk_id": node.get("chunk_id"),
                "chunk_index": node.get("chunk_index"),
                "knowledge_id": node.get("knowledge_id"),
                "knowledge_base_id": node.get("knowledge_base_id"),
                "knowledge_title": node.get("knowledge_title"),
                "score": node.get("score"),
                "match_snippet": (node.findtext("match_snippet") or ""),
                "content": (node.findtext("content") or ""),
            })
        return chunks
    except ET.ParseError:
        pass
    # 回退：逐 <chunk …> … </chunk> 正则抽取（属性 + 两个子元素）
    for m in re.finditer(r"<chunk\s+([^>]*?)(?:/>|>(.*?)</chunk>)", text, re.S):
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1) or ""))
        body = m.group(2) or ""
        snippet = re.search(r"<match_snippet>(.*?)</match_snippet>", body, re.S)
        content = re.search(r"<content>(.*?)</content>", body, re.S)
        chunks.append({
            "rank": attrs.get("rank"),
            "chunk_id": attrs.get("chunk_id"),
            "chunk_index": attrs.get("chunk_index"),
            "knowledge_id": attrs.get("knowledge_id"),
            "knowledge_base_id": attrs.get("knowledge_base_id"),
            "knowledge_title": attrs.get("knowledge_title"),
            "score": attrs.get("score"),
            "match_snippet": snippet.group(1) if snippet else "",
            "content": content.group(1) if content else "",
        })
    return chunks


def _citations_from_chunks(chunks: list, max_snippet: int) -> tuple:
    """把 MCP chunk 列表映射成 askoda 引用形状 + 文档聚合。"""
    citations = []
    aggs = {}
    for i, c in enumerate(chunks, 1):
        content = (c.get("content") or "").strip()
        kid = c.get("knowledge_id")
        aggs[kid] = aggs.get(kid, {"doc_id": kid, "doc_name": c.get("knowledge_title"), "count": 0})
        aggs[kid]["count"] += 1
        try:
            score = round(float(c.get("score") or 0), 4)
        except (TypeError, ValueError):
            score = 0.0
        citations.append({
            "n": i,
            "document_id": kid,
            "document_name": c.get("knowledge_title"),
            "chunk_id": c.get("chunk_id"),
            "similarity": score,
            # WeKnora MCP 不分项给向量/词项分；下游无消费者，如实置 None
            "vector_similarity": None,
            "term_similarity": None,
            "positions": [],
            "position_available": False,
            "locator": "chunk=%s position=n/a(MCP后端不返回页坐标)" % c.get("chunk_id"),
            "snippet": content[:max_snippet] + ("…" if len(content) > max_snippet else ""),
            "chars": len(content),
        })
    return citations, list(aggs.values())


def search(
    question,
    dataset_ids=None,
    top_k=DEFAULT_TOP_K,
    threshold=None,        # MCP 后端不透传（WeKnora 混合检索内部自控），仅为函数面兼容保留
    vector_weight=None,    # 同上
    max_snippet=SNIPPET_CHARS,
):
    """引用式检索（MCP 版）：search_knowledge(hybrid) → askoda 引用形状。

    零命中回退策略与 RAGFlow 版一致：去疑问词重试一次（业务口语问句整体
    召回失败是关键词检索通病，MCP 底座同样存在）。
    """
    question = (question or "").strip()
    if not question:
        raise KnowledgeError("question 不能为空")
    ids = _resolve_kb_ids(dataset_ids)
    tool = _tool("search")
    t0 = time.time()

    def _query(q):
        text = _CLIENT.call_text(tool, {
            "query": q,
            "knowledge_base_ids": ids,
            "limit": max(int(top_k), 1),
            "mode": "hybrid",
        })
        chunks = _parse_search_xml(text)
        cites, aggs = _citations_from_chunks(chunks, max_snippet)
        return cites, aggs, len(chunks)

    citations, doc_aggs, total = _query(question)

    fallback_query = None
    if not citations:
        kw = extract_keywords(question)
        if kw and kw != question:
            citations, doc_aggs, total = _query(kw)
            fallback_query = kw

    return {
        "ok": True,
        "query": question,
        "fallback_query": fallback_query,
        "dataset_ids": ids,
        "params": {
            "top_k": max(int(top_k), 1),
            "mode": "hybrid",
            # 如实标注：以下两个参数在 MCP 后端不生效（与 RAGFlow 直连版的行为差异）
            "similarity_threshold": None,
            "vector_similarity_weight": None,
        },
        "total": total if total is not None else len(citations),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "citations": citations,
        "doc_aggs": doc_aggs,
        "source_names": sorted({c["document_name"] for c in citations if c["document_name"]}),
        "answer_context": _kf._context(citations),
    }


def list_documents(limit=50, dataset_ids=None):
    """列出知识库文档（MCP 版：title/file_name/parse_status 映射到 run/progress 语义）。

    `dataset_ids`（2026-10-30 追加，末尾追加不改既有位置）：客户端指定知识库
    ID 时按传入库列出；为空回落 KNOWLEDGE_KB_IDS 默认作用域。
    """
    ids = _resolve_kb_ids(dataset_ids)
    if len(ids) > 1:
        # 多库场景下逐库列出；前端消费形状不变（dataset_id 取首个，total 为合计）
        rows, total = [], 0
        for kb in ids:
            d = _CLIENT.call_json(_tool("list_documents"), {
                "knowledge_base_id": kb, "page": 1, "page_size": min(int(limit), 100),
            })
            total += int(d.get("total") or 0)
            rows.extend(_doc_rows(d, kb))
        return {"ok": True, "dataset_id": ids[0], "total": total, "documents": rows[:limit]}
    d = _CLIENT.call_json(_tool("list_documents"), {
        "knowledge_base_id": ids[0], "page": 1, "page_size": min(int(limit), 100),
    })
    return {
        "ok": True,
        "dataset_id": ids[0],
        "total": int(d.get("total") or 0),
        "documents": _doc_rows(d, ids[0]),
    }


def _doc_rows(data: dict, kb_id: str) -> list:
    rows = []
    for r in (data.get("documents") or []):
        status = r.get("parse_status") or ""
        # run 语义对齐 RAGFlow：RUNNING/DONE/FAIL（前端「解析中/已完成/失败」三态渲染）
        run = {"completed": "DONE", "failed": "FAIL"}.get(status, "RUNNING")
        rows.append({
            "id": r.get("id"),
            "name": r.get("title") or r.get("file_name"),
            "run": run,
            "progress": status,
            "chunk_count": None,   # MCP list_documents 不返回 chunk 计数
            "size": None,
            "created_at": r.get("created_at"),
            "parse_error": "",
        })
    return rows


def upload_document(filename, content, content_type="", dataset_id=""):
    """上传一份 Markdown 文档（MCP 版：add_document 一步入库并自动解析）。

    与 RAGFlow 直连版的差异：仅支持文本内容（add_document 的 content 通道），
    二进制文件（pdf/xlsx 等）请走底座控制台或后续扩展的 URL 导入。
    准入预检（扩展名 / 大小 / 超长单元格）与防重语义保持一致。
    """
    name = str(filename or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in ALLOWED_SUFFIXES:
        return {
            "ok": False,
            "error": "不支持的文件类型 %s；允许：%s"
            % (suffix or "(无扩展名)", "、".join(ALLOWED_SUFFIXES)),
        }
    if not content:
        return {"ok": False, "error": "文件内容为空"}
    if len(content) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": "文件超过上限 %d MB" % (MAX_UPLOAD_BYTES // 1024 // 1024)}
    if suffix not in (".md", ".markdown", ".txt"):
        return {
            "ok": False,
            "error": "MCP 后端的 add_document 仅支持文本类文件（md/markdown/txt）；"
                     "%s 请走底座控制台上传或改用直连模式" % suffix,
        }
    kb_id = (dataset_id or (KB_IDS[0] if KB_IDS else "")).strip()
    if not kb_id:
        ids = _resolve_kb_ids()
        kb_id = ids[0] if ids else ""
    if not kb_id:
        return {"ok": False, "error": "无法确定知识库 ID"}

    # 防重：与 RAGFlow 版同一纪律——同一口径只留一份，重复入库会产生互相矛盾的证据
    try:
        listed = list_documents(limit=100)
        dup = next(
            (d for d in (listed.get("documents") or []) if (d.get("name") or "") == name),
            None,
        )
    except KnowledgeError:
        dup = None
        dedup_note = "知识库不可达，本次未做重名检查（可能重复入库）"
    else:
        dedup_note = ""
    if dup:
        return {
            "ok": False,
            "error": "库中已有同名文档《%s》；知识库同一口径只留一份，"
                     "请先撤库再上传，或改用带版本号的文件名" % name,
            "duplicate": True,
            "document_id": (dup or {}).get("id", ""),
            "run": (dup or {}).get("run", ""),
        }

    # 超长单元格预检对 MCP 底座同样有价值（同一 embedding 通道）
    scan = scan_oversized_cells(name, content)
    precheck_note = scan.get("note") or ""
    if not scan.get("ok"):
        cells = scan.get("cells") or []
        detail = "；".join(
            "%s（%d 字符）" % (c.get("location"), c.get("chars")) for c in cells[:5]
        )
        tail = "（仅列出前 %d 处）" % len(cells) if len(cells) >= MAX_CELL_REPORT else ""
        return {
            "ok": False,
            "error": "文件里有超长单元格（单格超过 %d 字符），会导致入库时 embedding "
                     "卡死、整份文档解析失败且看不到原因。%s%s。"
                     "请先把这些单元格拆成多行或多列再上传。"
                     % (scan.get("limit"), detail or "（位置未能定位）", tail),
            "oversized": True,
            "cells": cells,
            "limit": scan.get("limit"),
        }

    out = _CLIENT.call_json(_tool("add_document"), {
        "knowledge_base_id": kb_id,
        "title": name,
        "content": content,
        "publish": True,
    })
    doc = out.get("document") or {}
    doc_id = doc.get("id")
    if not doc_id:
        return {"ok": False, "error": "上传成功但未返回文档 id，无法继续跟踪解析"}
    notes = [x for x in (out.get("note") or "", dedup_note, precheck_note) if x]
    return {
        "ok": True,
        "dataset_id": kb_id,
        "name": doc.get("title") or name,
        "document_ids": [doc_id],
        "size": len(content),
        # add_document 即触发异步解析，无需单独 parse 步
        "parse_started": True,
        "dedup_checked": not dedup_note,
        "note": "；".join(notes),
        "status_hint": "解析为异步，请用 document_status 查询进度",
    }


def document_status(document_id="", limit=50):
    """查文档解析状态（前端轮询用），形状对齐 RAGFlow 版。"""
    listed = list_documents(limit=limit)
    if not listed.get("ok"):
        return listed
    docs = listed["documents"]
    if document_id:
        docs = [d for d in docs if d.get("id") == document_id]
        if not docs:
            return {"ok": False, "error": "未找到文档 %s" % document_id}
        return {"ok": True, "dataset_id": listed.get("dataset_id"), "documents": docs}
    return {
        "ok": True,
        "dataset_id": listed.get("dataset_id"),
        "total": listed["total"],
        "documents": docs,
    }


def delete_document(document_id, actor=""):
    """撤库（MCP 版：delete_document 仅按 knowledge_id）。"""
    did = str(document_id or "").strip()
    if not did:
        return {"ok": False, "error": "缺少 document_id"}
    out = _CLIENT.call_json(_tool("delete_document"), {"knowledge_id": did})
    if not out.get("deleted"):
        return {"ok": False, "error": "撤库未确认：%s" % json.dumps(out, ensure_ascii=False)[:200]}
    return {"ok": True, "dataset_id": None, "document_id": did, "actor": actor}
