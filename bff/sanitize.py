"""出参脱敏层（R2 红线的执行点）。

背景（实测证据）：网关 `datasets` / `gateway_health` 的返回里带开源组件内部地址，
例如：
    "wren_endpoint": "http://wren-mcp-a:9000/mcp"
    "data_source":  "wren-postgres-a:5432/test"
    "endpoint":     "assistant-minio:9000"
    "endpoint":     "http://host.docker.internal:19380/api/v1"
这些字段一旦原样透传到浏览器，就等于「前端出现了开源组件的地址与端口」，
直接违反 R2。所以 BFF 在出参统一做两件事：
  1. **按键删除**：把已知的内部地址类键整个丢掉。
  2. **按值兜底**：正则抹掉任何残留的 `host:port` /内网URL 字样。

前端只应看到业务语义字段。
"""

from __future__ import annotations

import re
from typing import Any

# 需要整个键删掉的字段名（大小写不敏感，按子串匹配）
_INTERNAL_KEYS = (
    "wren_endpoint",
    "wren_url",
    "mdl_path",
    "data_source",
    "mcp_endpoint",
    "endpoint",
    "gateway_url",
    "gateway_endpoint",
    "service_endpoint",
    "dsn",
    "conn_str",
    "connection_string",
    "password",
    "secret",
    "token",
    "api_key",
)

# 值的兜底清洗：把 `scheme://host:port` 与 `host:port` 形态的内部地址抹掉
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'<>]+")
_HOSTPORT_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d{2,5})?\b", re.I
)
_CONTAINERISH = ("wren-mcp", "wren-postgres", "assistant-postgres", "assistant-minio",
                 "ragflow", "host.docker.internal", "gateway:8080")


def _looks_internal(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _CONTAINERISH)


def _scrub_value(value: Any) -> Any:
    """按值兜底清洗：把内部地址形态替换为占位说明。"""
    if isinstance(value, str):
        if _looks_internal(value):
            return "[内部地址已脱敏]"
        if _URL_RE.search(value):
            return _URL_RE.sub("[地址已脱敏]", value)
        return value
    if isinstance(value, list):
        return [_scrub_value(v) for v in value]
    if isinstance(value, dict):
        return sanitize(value)
    return value


def sanitize(payload: Any) -> Any:
    """递归脱敏：删内部地址类键 + 清洗残留值。"""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for key, val in payload.items():
            low = str(key).lower()
            if any(bad in low for bad in _INTERNAL_KEYS):
                # 整个键丢掉，前端不需要也不该知道
                continue
            if _looks_internal(str(key)):
                out[key] = sanitize(val)
                continue
            # 只有「标量值本身就是内部地址」才丢键。
            # 不能对 list/dict 用 str() 判定——容器里任一元素含内部地址就会把整个键丢掉。
            if isinstance(val, str) and _looks_internal(val):
                continue
            out[key] = sanitize(val)
        return out
    if isinstance(payload, list):
        return [sanitize(v) for v in payload]
    return _scrub_value(payload)


def redact_mcp_tool(raw: str) -> str:
    """把工具错误文本里的内部地址抹掉后再返回给前端。"""
    if _URL_RE.search(raw):
        raw = _URL_RE.sub("[地址已脱敏]", raw)
    return _HOSTPORT_RE.sub("[地址已脱敏]", raw) if _looks_internal(raw) else raw