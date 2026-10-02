# -*- coding: utf-8 -*-
"""Wren MCP 客户端（streamable-http）

设计说明
--------
按《实施推进与验收方案》§12「待实测项 1」的降级条款实现：FastMCP 4.x 已移除
`create_proxy`，因此不走代理挂载，改用 POC 阶段已验证的手写 streamable-http
client（该模式在 demo-server 与 poc_eval 中针对 Wren v1.27.0 实测通过）。

要点
----
- 协议：initialize → notifications/initialized → tools/call
- 会话：响应头 `Mcp-Session-Id` 全程透传；session 失效自动重握手重试一次
- 响应：streamable-http 以 SSE 返回，取 `data:` 行解析 JSON
- 网络：显式绕过系统代理（回环 / 容器内网直连）
"""
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-03-26"

# 重试只属读侧，写侧（落库留痕）在 db 层，保持无重试。
# 读操作幂等：同一条 SQL / manifest / relationships / dry_run（语义预演不改 MDL）
# 多次调用结果一致，故可重试。
READ_TOOLS = {
    "dry_run",
    "query",
    "get_manifest",
    "get_relationships",
    "health_check",
    "get_sqlglot",
    "get_registry",
}
READ_RETRIES = 3
RETRY_BACKOFF = (0.5, 1.0, 2.0)


class WrenError(RuntimeError):
    pass


class WrenTimeout(WrenError):
    """Wren 端网络超时：socket.timeout / URLError(timeout) 统一封装。"""
    pass


class WrenClient:
    """单个 Wren MCP 端点的客户端。线程安全（内部加锁）。"""

    def __init__(self, base_url: str, name: str = "wren", timeout=None):
        self.name = name
        self.endpoint = base_url.rstrip("/") + "/mcp"
        # timeout 显式传入 → 用它；否则依次读 WREN_{NAME}_TIMEOUT / WREN_TIMEOUT；都无=60
        if timeout is not None:
            self.timeout = float(timeout)
        else:
            spec = os.environ.get("WREN_%s_TIMEOUT" % name.upper())
            fallback = os.environ.get("WREN_TIMEOUT")
            raw = spec if spec else fallback
            self.timeout = float(raw) if raw else 60.0
        self.last_attempts = 0  # 可观测：最近一次 call_raw 的实际网络尝试次数
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._sid = None
        self._lock = threading.Lock()

    # -- 底层 RPC ---------------------------------------------------------
    def _rpc(self, payload, sid=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), headers=headers
        )
        new_sid = None
        body = ""
        try:
            resp = self._opener.open(req, timeout=self.timeout)
            new_sid = resp.headers.get("Mcp-Session-Id")
            body = resp.read().decode()
        except socket.timeout as e:
            raise WrenTimeout(
                "[%s] tool=<pending> timeout=%ss: %s"
                % (self.name, self.timeout, str(e)[:200])
            )
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, socket.timeout):
                raise WrenTimeout(
                    "[%s] tool=<pending> timeout=%ss: %s"
                    % (self.name, self.timeout, str(e)[:200])
                )
            raise
        # SSE 解析与 JSON 反序列化留在 try 之外，避免把 JSONDecodeError 误当成超时
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), new_sid
        return None, new_sid

    def handshake(self):
        res, sid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "demand-gateway", "version": "0.1.0"},
                },
            }
        )
        self._sid = sid
        if sid:
            try:
                self._rpc(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid
                )
            except Exception:
                pass
        return res

    def reconnect(self):
        with self._lock:
            self._sid = None
            return self.handshake()

    def _do_call(self, tool, args):
        """单次网络调用：捕获 HTTPError / WrenTimeout / URLError，包装或原样抛出。"""
        res, sid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args or {}},
            },
            self._sid,
        )
        if sid:
            self._sid = sid
        return res

    # -- 工具调用 ---------------------------------------------------------
    def call_raw(self, tool, args=None, _retry=True):
        """返回原始 JSON-RPC 响应。

        重试规则（网络层）：tool ∈ READ_TOOLS 时，遇 WrenTimeout / URLError
        按 RETRY_BACKOFF 最多 READ_RETRIES 次，仍失败抛 WrenTimeout（注明尝试次数）。
        原 _retry 形参用于"session 失效重握手一次"，本函数内保留使用。
        """
        with self._lock:
            max_n = READ_RETRIES if tool in READ_TOOLS else 1
            last_err = None
            self.last_attempts = 0
            for i in range(max_n):
                self.last_attempts += 1
                try:
                    if self._sid is None:
                        self.handshake()
                    return self._do_call(tool, args)
                except urllib.error.HTTPError as e:
                    detail = ""
                    try:
                        detail = e.read().decode()
                    except Exception:
                        pass
                    if _retry and ("session" in detail.lower() or e.code in (400, 404)):
                        self._sid = None
                        try:
                            return self._do_call(tool, args)
                        except Exception as inner:
                            last_err = inner
                            if i + 1 < max_n and tool in READ_TOOLS:
                                time.sleep(RETRY_BACKOFF[i])
                                continue
                            raise
                    raise WrenError(
                        "[%s] MCP 调用失败 HTTP %s: %s"
                        % (self.name, e.code, detail[:200])
                    )
                except WrenTimeout as e:
                    last_err = e
                    if i + 1 < max_n:
                        time.sleep(RETRY_BACKOFF[i])
                        continue
                    raise WrenTimeout(
                        "[%s] tool=%s timeout=%ss: 已重试 %d 次仍失败 %s"
                        % (
                            self.name,
                            tool,
                            self.timeout,
                            max_n,
                            (": %s" % str(last_err)[:200]) if last_err else "",
                        )
                    )
                except urllib.error.URLError as e:
                    last_err = e
                    if i + 1 < max_n and tool in READ_TOOLS:
                        time.sleep(RETRY_BACKOFF[i])
                        continue
                    raise WrenTimeout(
                        "[%s] tool=%s timeout=%ss: 已重试 %d 次仍失败（URLError %s）"
                        % (self.name, tool, self.timeout, max_n, str(e)[:200])
                    )
            if isinstance(last_err, (WrenTimeout, urllib.error.URLError)):
                raise WrenTimeout(
                    "[%s] tool=%s timeout=%ss: 已重试 %d 次仍失败 %s"
                    % (
                        self.name,
                        tool,
                        self.timeout,
                        max_n,
                        (": %s" % str(last_err)[:200]) if last_err else "",
                    )
                )
            raise last_err if last_err else WrenError(
                "[%s] call_raw 未发起网络调用（tool=%s）" % (self.name, tool)
            )

    def call_text(self, tool, args=None):
        """调用工具并返回拼接后的文本内容。"""
        res = self.call_raw(tool, args)
        if not res:
            raise WrenError("[%s] 空响应（tool=%s）" % (self.name, tool))
        if "error" in res:
            raise WrenError(
                "[%s] %s" % (self.name, json.dumps(res["error"], ensure_ascii=False)[:200])
            )
        result = res.get("result", {})
        if result.get("isError"):
            txt = "".join(c.get("text", "") for c in result.get("content", []))
            raise WrenError("[%s] %s" % (self.name, txt[:300] or "工具返回错误"))
        return "".join(c.get("text", "") for c in result.get("content", []))

    # -- 语义化封装 -------------------------------------------------------
    def health(self):
        return self.call_text("health_check")

    def manifest(self):
        return json.loads(self.call_text("get_manifest") or "{}")

    def relationships(self):
        return json.loads(self.call_text("get_relationships") or "[]")

    def dry_run(self, sql):
        """语义层门禁预演。返回 {ok, message}。"""
        try:
            res = self.call_raw("dry_run", {"sql": sql})
        except WrenError as e:
            return {"ok": False, "message": str(e)[:300]}
        if not res or "error" in res:
            return {
                "ok": False,
                "message": json.dumps(res.get("error", {}) if res else {}, ensure_ascii=False)[:300],
            }
        result = res.get("result", {})
        txt = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError") or "error" in txt.lower():
            return {"ok": False, "message": txt[:300] or "语义层校验未通过"}
        return {"ok": True, "message": "语义层预演通过（引用对象均 ∈ MDL 可见闭集）"}

    def query(self, sql):
        """真实执行，返回结构化结果 {columns, data, dtypes}。"""
        txt = self.call_text("query", {"sql": sql})
        try:
            return json.loads(txt)
        except Exception:
            return {"columns": ["result"], "data": [[txt]], "dtypes": {"result": "string"}}
