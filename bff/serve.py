"""BFF 启动入口。

    python bff/app.py            # 起在 18081
    ASKODA_BFF_PORT=19002 python bff/app.py

BFF 只经MCP 网关触达后端能力，不直连 Wren / 业务 PG。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

from config import BFF_PORT

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="127.0.0.1",
        port=BFF_PORT,
        log_level="info",
        reload=False,
    )