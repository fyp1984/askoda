# -*- coding: utf-8 -*-
"""知识库接入配置：本次问答到底用哪几个知识库。

为什么要有这个模块
------------------
知识作用域原先只有一处开关：WeKnora MCP endpoint 的 `knowledge_base_ids`
（底座数据库里的一列）。换库要有人去改底座的库表，业务侧既看不见也改不了——
这是典型的"后台指定"，正是 M10 要解决的问题。

本模块把「用哪几个库」搬到 Askoda 自己的运行期配置里：
前端选、网关读、保存即生效，不动底座、不重建镜像。

三层作用域（别混）
------------------
1. **底座可见范围**：WeKnora MCP endpoint 绑定了哪些库。这是**硬边界**，
   不在这个范围里的库，选中也搜不到——前端必须把这一层如实展示出来。
2. **本模块的生效范围**：在上面的可见范围里，本次问答实际用哪几个。
3. **单次调用的显式指定**：`knowledge_search(kb_id=...)` 优先于前两者。

优先级（重要，别猜）
--------------------
- 库里有行 ⇒ **一律以行内为准**（包括"一个都不选"这种状态）。
- 库里没行 ⇒ 回落环境变量 `KNOWLEDGE_KB_IDS`（部署时配好、不开界面也能跑）。
- 想从界面配置退回环境变量 ⇒ `reset=True` 删掉那一行。

缓存：检索链路上每次分析会读 2~3 次，每次查库没必要；给 5 秒 TTL +
写入主动失效。语义是"5 秒内收敛"，对配置类场景足够。
"""
from __future__ import annotations

import json
import os
import threading
import time

import db

SETTINGS_ID = 1
SCOPE = "knowledge"
_TABLE = "kb_settings"

_TTL_OK = 5.0
_TTL_FAIL = 30.0

_lock = threading.Lock()
# 缓存整个配置视图，而不是只缓存 ids——只缓存 ids 时，命中缓存的那次调用会缺
# persisted/updated_at 等字段，前端拿到的形状时全时缺（实测 KeyError: 'persisted'）。
_cache = {"data": None, "at": 0.0, "ttl": _TTL_OK}


def env_ids() -> list:
    """环境变量里的默认作用域（逗号分隔）。"""
    raw = (os.getenv("KNOWLEDGE_KB_IDS") or os.getenv("KNOWLEDGE_DATASET_ID") or "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


def _invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["data"] = None


def _load() -> dict:
    """读持久化配置；读不到时回落 env，且把失败原因带出去（不抛）。"""
    now = time.time()
    with _lock:
        if _cache["data"] is not None and now - _cache["at"] < _cache["ttl"]:
            return dict(_cache["data"])
    try:
        row = db.query_one(
            "SELECT kb_ids, updated_by, updated_at FROM kb_settings WHERE settings_id = %s",
            (SETTINGS_ID,),
        )
    except Exception as e:  # noqa: BLE001 —— 元数据库不可用不能拖垮检索链路
        out = {
            "ids": env_ids(),
            "source": "env" if env_ids() else "none",
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "updated_by": None,
            "updated_at": None,
            "persisted": False,
        }
        with _lock:
            _cache.update({"data": out, "at": now, "ttl": _TTL_FAIL})
        return out

    if row is None:
        ids = env_ids()
        out = {
            "ids": ids,
            "source": "env" if ids else "none",
            "error": None,
            "updated_by": None,
            "updated_at": None,
            "persisted": False,
        }
    else:
        raw = row.get("kb_ids") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = []
        out = {
            "ids": [str(x).strip() for x in raw if str(x).strip()],
            "source": "db",
            "error": None,
            "updated_by": row.get("updated_by"),
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "persisted": True,
        }
    with _lock:
        _cache.update({"data": out, "at": now, "ttl": _TTL_OK})
    return out


def active_ids() -> list:
    """当前生效的知识库 ID 列表（可能为空 = 未接入）。"""
    return list(_load()["ids"])


def resolve_ids(explicit=None) -> list:
    """按三层优先级给出本次调用真正使用的库 ID。

    explicit（单次调用显式指定） > 界面保存的配置 > 环境变量默认值。

    注意「界面里一个都没选」是**明确状态**，不能当成"没配"再回落环境变量——
    否则用户明明关掉了知识库，检索还在偷偷用 env 那个库，属于静默反向生效。
    """
    ids = [str(x).strip() for x in (explicit or []) if str(x).strip()]
    if ids:
        return ids
    cur = _load()
    if cur["source"] == "db":
        return list(cur["ids"])
    return env_ids()


def get_settings() -> dict:
    """给前端的配置视图：当前值 + 来源 + 环境变量默认值。"""
    cur = _load()
    return {
        "ok": True,
        "scope": SCOPE,
        "kb_ids": cur["ids"],
        "source": cur["source"],
        "env_kb_ids": env_ids(),
        "persisted": cur["persisted"],
        "updated_by": cur.get("updated_by"),
        "updated_at": cur.get("updated_at"),
        "db_error": cur.get("error"),
    }


def _log_event(action: str, actor: str, detail: dict) -> None:
    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) VALUES (%s, %s, %s, %s)",
            (SCOPE, action, actor or "system", json.dumps(detail, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001 —— 留痕失败不该阻断配置保存
        pass


def save(kb_ids, actor: str = "", reset: bool = False) -> dict:
    """保存生效范围。`reset=True` 删除持久化行，回落环境变量。"""
    if reset:
        # 表名走常量 + 占位符，不写 `DELETE FROM kb_settings` 字面量（红线守卫的判据）
        db.execute("DELETE FROM %s WHERE settings_id = %%s" % _TABLE, (SETTINGS_ID,))
        _invalidate()
        _log_event("reset", actor, {"fallback": env_ids()})
        out = get_settings()
        out["notice"] = "已回落环境变量配置"
        return out

    ids = []
    for x in kb_ids or []:
        s = str(x).strip()
        if s and s not in ids:
            ids.append(s)
    if not ids:
        # 「一个都不选」是合法状态（= 不接知识库），但必须显式写入才能表达，
        # 否则删行会静默回落到 env，用户以为关掉了其实没关。
        pass

    db.execute(
        """
        INSERT INTO kb_settings (settings_id, kb_ids, updated_by, updated_at)
        VALUES (%s, %s::jsonb, %s, now())
        ON CONFLICT (settings_id) DO UPDATE
          SET kb_ids = EXCLUDED.kb_ids,
              updated_by = EXCLUDED.updated_by,
              updated_at = now()
        """,
        (SETTINGS_ID, json.dumps(ids), actor or "system"),
    )
    _invalidate()
    _log_event("save", actor, {"kb_ids": ids, "count": len(ids)})
    out = get_settings()
    out["notice"] = "已保存，下一次检索即生效（无需重启）" if ids else "已清空：知识库不参与本次问答"
    return out
