# -*- coding: utf-8 -*-
"""数据集注册表

一个数据集 = 一套（Wren MCP 端点 + MDL 语义层）。换库只换注册项，不改网关代码
（对应 P0 硬指标「换库 0 行代码」）。

两部分来源（M10 起）
--------------------
1. **环境变量装配**（A/B 两个写死）：部署期确定、随镜像走，改它要重建。
2. **运行期登记**（`dataset_registry` 表，见 `dataset_store.py`）：前端「取数通道 ·
   数据集登记」写入，TTL 5 秒内自动出现，**不重建镜像**。

为什么 MDL 可以内联
-------------------
`Dataset.mdl` 优先用内联 JSON（登记表里的 `mdl_json`），其次才读文件。
起因是实测：bind mount 源在 ~/Documents 下时容器内即使 root 也可能读不到
（PermissionError，2026-10-15），而 MDL 读不到会让 L2 静态规则被**静默跳过**——
"门禁全绿但口径错"就是这么来的。新库一律走内联，绕开这条暗礁。
"""
import json
import os
import threading
import time


class Dataset:
    def __init__(self, key, label, wren_url, mdl_path, domain="", pg_dsn="", mdl_data=None):
        self.key = key
        self.label = label
        self.wren_url = wren_url
        self.mdl_path = mdl_path
        self.domain = domain
        # 数据源直连串（仅元数据采集用；查询一律走语义层，不绕过 MDL）
        self.pg_dsn = pg_dsn
        # 内联 MDL（运行期登记的数据集走这里，不依赖容器内文件）
        self._inline_mdl = mdl_data if isinstance(mdl_data, dict) else None
        self._mdl = None

    @property
    def mdl(self):
        if self._mdl is None:
            if self._inline_mdl is not None:
                self._mdl = self._inline_mdl
            else:
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
            "mdl_path": self.mdl_path or "(内联 MDL)",
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
    #
    # `pg_dsn` 说明：它只被**元数据采集**用到（collect.py 直连 PG 读表结构与字段），
    # 语义层取数走 Wren HTTP、不碰它。所以这里**不再硬编码明文口令默认值**——
    # 未配置就留空，让用到它的采集步骤报「该配 WREN_PG_A_DSN」，
    # 而不是在任何用到 DSN 的地方都默默连上我们那个库。
    reg = {
        "A": Dataset(
            key="A",
            label="A 库 · 电商域",
            domain="电商（订单 / 商品 / 客户 / 退款）",
            wren_url=os.getenv("WREN_A_URL", "http://127.0.0.1:9000"),
            mdl_path=os.getenv("MDL_A_PATH", "/workspace-a/mdl.json"),
            pg_dsn=(os.getenv("WREN_PG_A_DSN") or "").strip(),
        ),
        "B": Dataset(
            key="B",
            label="B 库 · 零售会员域",
            domain="零售（订单 / 门店 / 会员 / 复购 / 升降级）",
            wren_url=os.getenv("WREN_B_URL", "http://127.0.0.1:9002"),
            mdl_path=os.getenv("MDL_B_PATH", "/workspace-b/mdl.json"),
            pg_dsn=(os.getenv("WREN_PG_B_DSN") or "").strip(),
        ),
    }
    return reg


DATASETS = _load()

# ---------------------------------------------------------------------------
# 运行期登记集（来自 dataset_registry 表）
# ---------------------------------------------------------------------------
_TTL = 5.0
_lock = threading.Lock()
_state = {"at": 0.0, "keys": set()}


def refresh(force: bool = False) -> list:
    """把登记表里的数据集合并进 DATASETS（TTL 5 秒）。

    失败一律静默——元数据库不可用时，环境变量那两个数据集仍然要能用；
    把配置读取失败升级成"整个网关不可用"是拿小故障换大故障。
    """
    now = time.time()
    with _lock:
        if not force and now - _state["at"] < _TTL:
            return sorted(_state["keys"])
        _state["at"] = now
    try:
        import dataset_store  # 延迟导入：registry 被门禁脚本单独 import 时不该拉 DB

        rows = dataset_store.list_rows(enabled_only=True)
    except Exception:  # noqa: BLE001
        return sorted(_state["keys"])

    live = set()
    for row in rows:
        key = row.get("key")
        if not key:
            continue
        # 明文 DSN 只在这里取一次，不进 describe()
        raw = dataset_store.raw_row(key) or {}
        mdl = raw.get("mdl_json")
        if isinstance(mdl, str):
            try:
                mdl = json.loads(mdl)
            except json.JSONDecodeError:
                mdl = None
        ds = Dataset(
            key=key,
            label=row.get("label") or key,
            domain=row.get("domain") or "",
            wren_url=row.get("wren_url") or "",
            mdl_path=row.get("mdl_path") or "",
            pg_dsn=raw.get("pg_dsn") or "",
            mdl_data=mdl,
        )
        # 环境变量装配的同名项以环境变量为准：那是部署方的显式声明，
        # 不该被界面里一行残留配置悄悄覆盖。
        if key in _ENV_KEYS:
            continue
        DATASETS[key] = ds
        live.add(key)

    # 被删掉/停用的登记项要消失，否则"删了还能在下拉里看到"
    for k in list(_state["keys"] - live):
        DATASETS.pop(k, None)
    with _lock:
        _state["keys"] = live
    return sorted(live)


_ENV_KEYS = set(DATASETS)


def all(force: bool = False) -> dict:
    """全量数据集（环境变量 + 运行期登记）。"""
    refresh(force=force)
    return dict(DATASETS)


def get(key):
    refresh()
    if key not in DATASETS:
        raise KeyError(
            "未注册的数据集「%s」；可选：%s" % (key, ", ".join(sorted(DATASETS)))
        )
    return DATASETS[key]
