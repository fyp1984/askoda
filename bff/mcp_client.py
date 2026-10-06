"""MCP 网关客户端（streamable-http JSON-RPC）。

协议三步（已在askoda 网关 4.0.10 实测通过）：
  1. `initialize`               → 从响应 header 取 `Mcp-Session-Id`
  2. `notifications/initialized` → **不带 id 字段**的通知，返回 202 无 body 属正常
  3. `tools/call`               → 带 `Mcp-Session-Id` header

要点：
- 响应体是 SSE 格式，需解析 `data:` 行取 JSON。
- 本机有系统代理，必须绕过（trust_env=False），否则 127.0.0.1 会被代理吞掉。
- 会话按进程复用；失效时自动重连一次。

红线：本模块是**唯一**持有网关地址的地方，前端与其它层不得直连网关。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from config import GATEWAY_URL, MCP_PROTOCOL_VERSION, MCP_TIMEOUT_SECONDS

log = logging.getLogger("bff.mcp")

_ACCEPT = "application/json, text/event-stream"


class McpError(RuntimeError):
    """MCP 调用失败（协议层错误、工具不存在、网关不可达等）。"""

    def __init__(self, message: str, *, tool: str | None = None, kind: str = "upstream"):
        super().__init__(message)
        self.tool = tool
        self.kind = kind


def _parse_sse(body: str) -> Any:
    """从 SSE 响应体里取出第一个 `data:` 行的 JSON。"""
    for line in body.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise McpError(f"SSE data 行不是合法 JSON: {raw[:200]}") from exc
    raise McpError(f"响应体不是预期的 SSE 格式: {body[:200]}")


class McpGatewayClient:
    """带会话复用的 MCP 网关客户端。"""

    def __init__(self, url: str = GATEWAY_URL, timeout: int = MCP_TIMEOUT_SECONDS):
        self._url = url
        self._timeout = timeout
        self._session_id: str | None = None
        self._lock = asyncio.Lock()
        # 同一会话上的工具调用必须串行（网关不支持并发复用同一 session）。
        self._call_lock = asyncio.Lock()
        # 网关响应带 `Connection: close` + chunked，keep-alive 复用会踩
        # RemoteProtocolError(incomplete chunked read)。聚合接口会并发发多次调用，
        # 所以这里禁用连接池复用：每次请求用独立短连接。
        self._client = httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            limits=httpx.Limits(
                max_keepalive_connections=0,
                max_connections=16,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict, session_id: str | None) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return await self._client.post(self._url, json=payload, headers=headers)

    async def _ensure_session(self) -> str:
        """建立会话（幂等）。"""
        async with self._lock:
            if self._session_id:
                return self._session_id
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "askoda-bff", "version": "1.0.0"},
                },
            }
            try:
                resp = await self._post(init, None)
            except httpx.HTTPError as exc:
                raise McpError(f"网关不可达（{self._url}）: {exc}") from exc
            if resp.status_code != 200:
                raise McpError(f"initialize 返回 {resp.status_code}: {resp.text[:200]}")
            sid = resp.headers.get("mcp-session-id")
            if not sid:
                raise McpError("initialize 响应缺少 Mcp-Session-Id header")
            self._session_id = sid
            # 第二步：通知，不带 id；202 无 body 属正常。
            notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            try:
                await self._post(notify, sid)
            except httpx.HTTPError as exc:
                log.warning("notifications/initialized 失败（可忽略）: %s", exc)
            return sid

    async def _rpc(self, method: str, params: dict, rpc_id: int) -> Any:
        payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        resp = await self._post(payload, await self._ensure_session())
        if resp.status_code == 404 and self._session_id:
            # 会话失效，重连一次。
            log.info("会话失效，重建后重试")
            async with self._lock:
                self._session_id = None
            resp = await self._post(payload, await self._ensure_session())
        if resp.status_code >= 400:
            raise McpError(f"{method} 返回 {resp.status_code}: {resp.text[:200]}")
        body = resp.text
        if not body.strip():
            raise McpError(f"{method} 返回空响应体")
        return _parse_sse(body)

    async def list_tools(self) -> list[dict]:
        """返回网关工具清单（含 inputSchema）。"""
        msg = await self._rpc("tools/list", {}, 2)
        if "error" in msg:
            raise McpError(f"tools/list 失败: {msg['error']}")
        return msg.get("result", {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用一个 MCP 工具，返回 structuredContent（无则退回 content 文本解析）。

        对瞬时传输错误做一次重试：网关是 SSE + chunked 响应，
        并发调用时偶发 `incomplete chunked read`，重试即可恢复。
        """
        last: Exception | None = None
        for attempt in range(2):
            try:
                return await self._call_tool_once(name, arguments)
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                last = exc
                log.warning("工具 %s 第 %d 次传输失败，重试: %s", name, attempt + 1, exc)
                # 传输层失败时丢弃会话，避免复用半死的连接
                async with self._lock:
                    self._session_id = None
                await asyncio.sleep(0.4)
        raise McpError(f"工具 {name} 传输失败（已重试 1 次）: {last}", tool=name)

    async def _call_tool_once(self, name: str, arguments: dict | None) -> Any:
        """调用一个 MCP 工具，返回 structuredContent（无则退回 content 文本解析）。

        网关的 streamable-http 会话不支持并发复用：同一个 Mcp-Session-Id 上并行
        发多个 tools/call，响应 SSE 流会互相截断（incomplete chunked read），
        表现为请求直接挂住。因此这里用一把调用锁把同一会话上的调用串行化。
        聚合接口的 6 个子调用本就要依次等网关，串行化换来的是稳定而非更慢。
        """
        async with self._call_lock:
            params = {"name": name, "arguments": arguments or {}}
            msg = await self._rpc("tools/call", params, 3)
            if "error" in msg:
                raise McpError(f"工具 {name} 调用失败: {msg['error']}", tool=name)
            result = msg.get("result", {})
            if result.get("isError"):
                detail = ""
                content = result.get("content") or []
                if content and isinstance(content[0], dict):
                    detail = content[0].get("text", "")
                raise McpError(f"工具 {name} 返回错误: {detail}", tool=name, kind="tool")
            if "structuredContent" in result and result["structuredContent"] is not None:
                return result["structuredContent"]
            content = result.get("content") or []
            if content and isinstance(content[0], dict):
                text = content[0].get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"__text__": text}
            return result


_client: McpGatewayClient | None = None


def get_client() -> McpGatewayClient:
    global _client
    if _client is None:
        _client = McpGatewayClient()
    return _client