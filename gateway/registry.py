# -*- coding: utf-8 -*-
"""数据集注册表

一个数据集 = 一套（Wren MCP 端点 + MDL 语义层）。换库只换注册项，不改网关代码
（对应 P0 硬指标「换库 0 行代码」）。
"""
import json
import os


class Dataset:
    def __init__(self, key, label, wren_url, mdl_path, domain="", pg_dsn=""):
        self.key = key
        self.label = label
        self.wren_url = wren_url
        self.mdl_path = mdl_path
        self.domain = domain
        # 数据源直连串（仅元数据采集用；查询一律走语义层，不绕过 MDL）
        self.pg_dsn = pg_dsn
        self._mdl = None

    @property
    def mdl(self):
        if self._mdl is None:
            with open(self.mdl_path, encoding="utf-8") as f:
                self._mdl = json.load(f)
        return self._mdl

    @property
    def models(self):
        return {m["name"]: m for m in self.mdl.get("models", [])}

    @property
    def relationships(self):
        return self.mdl.get("relationships", [])

    def describe(self):
        return {
            "key": self.key,
            "label": self.label,
            "domain": self.domain,
            "wren_endpoint": self.wren_url + "/mcp",
            "mdl_path": self.mdl_path,
            "data_source": _safe_dsn(self.pg_dsn),
            "models": len(self.mdl.get("models", [])),
            "fields": sum(len(m.get("columns", [])) for m in self.mdl.get("models", [])),
            "relationships": len(self.relationships),
        }


def _safe_dsn(dsn):
    """脱敏后的数据源标识：只留 host:port/db，不带账号口令。"""
    if not dsn:
        return None
    tail = dsn.split("@")[-1] if "@" in dsn else dsn.split("://")[-1]
    return tail


def _load():
    # 容器内走服务名；本地开发可经环境变量覆盖为 127.0.0.1:9000 / 9002
    reg = {
        "A": Dataset(
            key="A",
            label="A 库 · 电商域",
            domain="电商（订单 / 商品 / 客户 / 退款）",
            wren_url=os.getenv("WREN_A_URL", "http://127.0.0.1:9000"),
            mdl_path=os.getenv("MDL_A_PATH", "/workspace-a/mdl.json"),
            pg_dsn=os.getenv(
                "WREN_PG_A_DSN", "postgresql://test:test@127.0.0.1:15432/test"
            ),
        ),
        "B": Dataset(
            key="B",
            label="B 库 · 零售会员域",
            domain="零售（订单 / 门店 / 会员 / 复购 / 升降级）",
            wren_url=os.getenv("WREN_B_URL", "http://127.0.0.1:9002"),
            mdl_path=os.getenv("MDL_B_PATH", "/workspace-b/mdl.json"),
            pg_dsn=os.getenv(
                "WREN_PG_B_DSN", "postgresql://test:test@127.0.0.1:15433/retail"
            ),
        ),
    }
    return reg


DATASETS = _load()


def get(key):
    if key not in DATASETS:
        raise KeyError(
            "未注册的数据集「%s」；可选：%s" % (key, ", ".join(sorted(DATASETS)))
        )
    return DATASETS[key]
