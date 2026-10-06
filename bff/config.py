"""BFF 配置。

红线 R2：前端代码里不得出现任何开源组件的地址或端口。
因此网关地址只允许出现在本文件（服务端），由 BFF 独占持有。
前端永远只知道 BFF 自己的相对路径 `/api/v1/...`。
"""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# MCP 网关端点（streamable-http）。仅 BFF 持有。
GATEWAY_URL = os.environ.get("ASKODA_GATEWAY_URL", "http://127.0.0.1:18080/mcp")

# MCP 协议版本
MCP_PROTOCOL_VERSION = os.environ.get("ASKODA_MCP_PROTOCOL", "2025-06-18")

# 单次 MCP 调用超时（秒）。SQL 生成 / 语义预演是长任务，必须给足。
MCP_TIMEOUT_SECONDS = _int_env("ASKODA_MCP_TIMEOUT", 180)

# BFF 自身监听端口。避开已占用端口（15432-15434/9000-9003/18000-18001/18080/19000-19001/8080）。
BFF_PORT = _int_env("ASKODA_BFF_PORT", 18081)

# 前端构建产物目录（存在则由 BFF 静态托管）。相对 bff/ 目录。
STATIC_DIR = os.environ.get(
    "ASKODA_STATIC_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

# 是否允许 BFF 直连静态产物（同源托管，避免前端出现绝对地址）
SERVE_STATIC = os.environ.get("ASKODA_SERVE_STATIC", "1") not in ("0", "false", "False")