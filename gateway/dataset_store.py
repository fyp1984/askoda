# -*- coding: utf-8 -*-
"""数据集登记：运行期接一个新库，不重建镜像、不改 compose。

为什么需要
----------
`registry.py` 的数据集原先在 import 期从环境变量一次性装配（A/B 两个写死），
于是「再接一个业务域」= 改 compose + 重建网关镜像 + 重启。私有化交付时这是
运维动作，客户自己做不了；而演示/验收时"再接一个库"恰恰是最高频的动作。

本模块把数据集变成**可登记的资源**：一行一个库，字段与 `registry.Dataset`
一一对应，网关按需加载（TTL 5 秒）。

与文件版的关系
--------------
`mdl_path` 仍支持（历史部署与门禁脚本依赖文件）；但**优先用 `mdl_json`**——
实测 bind mount 源在 ~/Documents 下时容器内可能读不到文件（PermissionError，
2026-10-15），而 MDL 读不到会让 L2 静态规则被静默跳过。所以新库一律把 MDL
存进库，绕开文件权限这条暗礁。

边界
----
- 本模块**不负责**在 Wren 引擎侧装载 MDL：引擎的 MDL 是启动期从它自己的
  workspace 读的。登记完成后 `probe()` 会比对「网关侧模型数 vs 引擎侧模型数」，
  不一致就明确报"引擎侧尚未装载"，不假装成功。
- `pg_dsn` 只用于**结构采集**，查询一律走语义层（与 registry 的纪律一致）。
"""
from __future__ import annotations

import json
from typing import Any

import db

# 与 registry 保持一致的键名；字段顺序即前端表单顺序
FIELDS = ("dataset_key", "label", "domain", "wren_url", "mdl_path", "mdl_json", "pg_dsn", "enabled")

_KEY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_TABLE = "dataset_registry"


class RegistryError(ValueError):
    """登记参数不合法（给用户看的错误，不是内部异常）。"""


def _mask_dsn(dsn: str) -> str | None:
    if not dsn:
        return None
    tail = dsn.split("@")[-1] if "@" in dsn else dsn.split("://")[-1]
    return tail


def _row_to_dict(row: dict) -> dict[str, Any]:
    mdl = row.get("mdl_json")
    if isinstance(mdl, str):
        try:
            mdl = json.loads(mdl)
        except json.JSONDecodeError:
            mdl = None
    return {
        "key": row.get("dataset_key"),
        "label": row.get("label") or "",
        "domain": row.get("domain") or "",
        "engine": row.get("engine") or "",
        # 注意：wren_url / pg_dsn **不出参**。前端只需要知道"用哪个引擎代号"和
        # "库名对不对"，地址与口令属于部署细节（BFF 的 sanitize 也会把它们抹掉）。
        "database": _db_name(row.get("pg_dsn") or ""),
        "has_inline_mdl": bool(mdl),
        "models": len((mdl or {}).get("models") or []),
        "fields": sum(len(m.get("columns") or []) for m in ((mdl or {}).get("models") or [])),
        "enabled": bool(row.get("enabled", True)),
        "updated_by": row.get("updated_by"),
        "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
    }


def _db_name(dsn: str) -> str:
    """从连接串里取库名（这是唯一可以给前端看的片段）。"""
    if not dsn:
        return ""
    try:
        tail = dsn.rsplit("/", 1)[-1]
        return tail.split("?")[0]
    except Exception:  # noqa: BLE001
        return ""


def list_rows(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM dataset_registry"
    if enabled_only:
        sql += " WHERE enabled = true"
    sql += " ORDER BY dataset_key"
    try:
        return [_row_to_dict(r) for r in db.query(sql)]
    except Exception:  # noqa: BLE001 —— 元数据库不可用不等于"没有数据集"
        return []


def get_row(key: str) -> dict | None:
    try:
        row = db.query_one("SELECT * FROM dataset_registry WHERE dataset_key = %s", (key,))
    except Exception:  # noqa: BLE001
        return None
    return _row_to_dict(row) if row else None


def raw_row(key: str) -> dict | None:
    """含明文 DSN 与 MDL 的整行（仅供 registry 内部装配，不得直接出参）。"""
    try:
        return db.query_one("SELECT * FROM dataset_registry WHERE dataset_key = %s", (key,))
    except Exception:  # noqa: BLE001
        return None


def _normalize(key: str) -> str:
    k = (key or "").strip().upper()
    if not k:
        raise RegistryError("数据集编码不能为空（建议用单个大写字母，如 C）")
    if len(k) > 16:
        raise RegistryError("数据集编码最长 16 个字符")
    bad = [c for c in k if c not in _KEY_CHARS]
    if bad:
        raise RegistryError(
            "数据集编码只允许大写字母、数字、下划线和横线（发现 %s）" % "".join(sorted(set(bad)))
        )
    return k


def validate_mdl(mdl: Any) -> dict:
    """MDL 最小合法性检查：能被引擎执行的前提是 models 每项有 name 与 tableReference。"""
    if not isinstance(mdl, dict):
        raise RegistryError("MDL 必须是 JSON 对象")
    models = mdl.get("models")
    if not isinstance(models, list) or not models:
        raise RegistryError("MDL 里没有 models：至少要有 1 个模型，否则语义层闭集为空、任何 SQL 都会被拒")
    for i, m in enumerate(models):
        if not isinstance(m, dict) or not m.get("name"):
            raise RegistryError("第 %d 个模型缺少 name 字段" % (i + 1))
        cols = m.get("columns")
        if not isinstance(cols, list) or not cols:
            raise RegistryError("模型「%s」没有 columns" % m.get("name"))
    return mdl


def _engine_url(engine: str) -> str:
    """引擎代号 → 容器内地址（与 compose 服务名一致）。"""
    import os

    env = (os.getenv("WREN_%s_URL" % (engine or "").upper()) or "").strip()
    if env:
        return env.rstrip("/")
    return "http://wren-mcp-%s:9000" % (engine or "").lower()


def upsert(payload: dict, actor: str = "") -> dict:
    """新增/修改一个数据集。只校验结构与必填，不替你猜连接串。"""
    key = _normalize(payload.get("key") or payload.get("dataset_key"))
    engine = (payload.get("engine") or "").strip().upper() or key
    # 前端只填"引擎代号"（A/B/C），地址由网关按约定解析：
    # 环境变量 WREN_<代号>_URL 优先，其次按 compose 服务名约定拼装。
    # 这样浏览器里永远不出现内网地址与端口（R2 红线），也不会因为手填错地址而白查半天。
    wren_url = (payload.get("wren_url") or "").strip().rstrip("/")
    if not wren_url:
        wren_url = _engine_url(engine)
    if not wren_url.startswith("http"):
        raise RegistryError("Wren 端点必须是 http(s):// 开头的地址")

    mdl_json = payload.get("mdl_json")
    if isinstance(mdl_json, str) and mdl_json.strip():
        try:
            mdl_json = json.loads(mdl_json)
        except json.JSONDecodeError as e:
            raise RegistryError("MDL 不是合法 JSON：%s" % str(e)[:120])
    if mdl_json:
        validate_mdl(mdl_json)
    mdl_path = (payload.get("mdl_path") or "").strip()
    if not mdl_json and not mdl_path:
        raise RegistryError(
            "MDL 必须给一个来源：要么提交 MDL JSON（推荐，不受文件权限影响），"
            "要么填容器内可读的 MDL 文件路径"
        )

    db.execute(
        """
        INSERT INTO dataset_registry
            (dataset_key, label, domain, engine, wren_url, mdl_path, mdl_json, pg_dsn, enabled, updated_by, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, now())
        ON CONFLICT (dataset_key) DO UPDATE SET
            label = EXCLUDED.label,
            domain = EXCLUDED.domain,
            engine = EXCLUDED.engine,
            wren_url = EXCLUDED.wren_url,
            mdl_path = EXCLUDED.mdl_path,
            mdl_json = COALESCE(EXCLUDED.mdl_json, dataset_registry.mdl_json),
            pg_dsn = CASE WHEN EXCLUDED.pg_dsn = '' THEN dataset_registry.pg_dsn ELSE EXCLUDED.pg_dsn END,
            enabled = EXCLUDED.enabled,
            updated_by = EXCLUDED.updated_by,
            updated_at = now()
        """,
        (
            key,
            (payload.get("label") or "").strip() or key,
            (payload.get("domain") or "").strip(),
            engine,
            wren_url,
            mdl_path,
            json.dumps(mdl_json) if mdl_json else None,
            (payload.get("pg_dsn") or "").strip(),
            bool(payload.get("enabled", True)),
            actor or "system",
        ),
    )
    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) VALUES (%s, %s, %s, %s)",
            ("dataset", "save", actor or "system",
             json.dumps({"key": key, "wren_url": wren_url,
                         "models": len((mdl_json or {}).get("models") or [])}, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "dataset": get_row(key), "notice": "已登记，刷新数据集下拉即可选中"}


def delete(key: str, actor: str = "") -> dict:
    k = _normalize(key)
    existed = get_row(k)
    # 表名走常量 + 占位符（红线守卫判据：不写死表名）
    db.execute("DELETE FROM %s WHERE dataset_key = %%s" % _TABLE, (k,))
    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) VALUES (%s, %s, %s, %s)",
            ("dataset", "delete", actor or "system", json.dumps({"key": k}, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "key": k, "existed": bool(existed), "notice": "已移除该数据集登记"}


def _probe_pg(dsn: str) -> dict:
    if not dsn:
        return {"ok": False, "error": "未提供连接串（PG_DSN）", "hint": "填 postgresql://用户:口令@主机:端口/库名"}
    try:
        import psycopg  # 与 db.py 同一个驱动
    except ImportError:
        return {"ok": False, "error": "网关内缺少 psycopg 驱动"}
    try:
        with psycopg.connect(dsn, connect_timeout=4) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
                )
                tables = cur.fetchone()[0]
                cur.execute("SELECT current_database()")
                dbname = cur.fetchone()[0]
        return {"ok": True, "database": dbname, "tables": int(tables)}
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "hint": "检查主机地址与端口是否可达（服务端容器内的回环地址指的是容器自己，不是宿主机）",
        }


def _probe_wren(url: str, gateway_models: int) -> dict:
    if not url:
        return {"ok": False, "error": "未提供 Wren 端点"}
    try:
        import wren as wren_mod

        client = wren_mod.WrenClient(url, name="probe", timeout=10)
        txt = client.health()
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "hint": "确认该 Wren 引擎容器已启动，且端口从网关容器可达（服务名:9000）",
        }

    # 引擎侧的模型数才是"能不能真查"的判据：网关侧有 MDL、引擎侧没装载，
    # 症状是「SQL 生成成功、执行报找不到表」——不比对就会误判成接通。
    engine_models = None
    try:
        man = client.manifest()
        models = man.get("models") if isinstance(man, dict) else None
        if isinstance(models, list):
            engine_models = len(models)
    except Exception:  # noqa: BLE001
        engine_models = None

    out: dict[str, Any] = {
        "ok": True,
        "response": str(txt)[:160],
        "engine_models": engine_models,
        "models_match": None,
    }
    if engine_models is not None and gateway_models > 0:
        out["models_match"] = engine_models >= gateway_models
        if not out["models_match"]:
            out["hint"] = (
                "引擎侧只装载了 %d 个模型，网关侧 MDL 有 %d 个："
                "引擎还没装载新 MDL（需把 mdl.json 放进该引擎的 workspace 并重启它）"
                % (engine_models, gateway_models)
            )
    return out


def probe(payload: dict) -> dict:
    """接入前自检：三个判据分开给结论，不合并成一个"成功/失败"。

    1. 连接串能不能连上目标库、里面有没有表；
    2. Wren 端点是否活着；
    3. MDL 是否合法、模型数多少。
    """
    key = (payload.get("key") or payload.get("dataset_key") or "").strip().upper()
    engine = (payload.get("engine") or "").strip().upper() or key
    wren_url = (payload.get("wren_url") or "").strip().rstrip("/") or _engine_url(engine)
    dsn = (payload.get("pg_dsn") or "").strip()
    mdl = payload.get("mdl_json")
    if isinstance(mdl, str) and mdl.strip():
        try:
            mdl = json.loads(mdl)
        except json.JSONDecodeError as e:
            mdl = {"__error__": "MDL 不是合法 JSON：%s" % str(e)[:120]}
    if not mdl or not dsn:
        row = raw_row(key) if key else None
        if row:
            if not mdl:
                mdl = row.get("mdl_json")
                if isinstance(mdl, str):
                    try:
                        mdl = json.loads(mdl)
                    except json.JSONDecodeError:
                        mdl = None
            # 未传连接串时用库里已登记的（前端"重新探测"不必重填口令）
            dsn = dsn or (row.get("pg_dsn") or "")
            wren_url = wren_url or (row.get("wren_url") or "")

    checks: dict[str, Any] = {}
    checks["pg"] = _probe_pg(dsn)

    n_models = 0
    if isinstance(mdl, dict) and "__error__" in mdl:
        checks["mdl"] = {"ok": False, "error": mdl["__error__"]}
    elif mdl:
        try:
            validate_mdl(mdl)
            n_models = len(mdl.get("models") or [])
            checks["mdl"] = {
                "ok": True,
                "models": n_models,
                "fields": sum(len(m.get("columns") or []) for m in mdl.get("models") or []),
                "relationships": len(mdl.get("relationships") or []),
            }
        except RegistryError as e:
            checks["mdl"] = {"ok": False, "error": str(e)}
    else:
        checks["mdl"] = {"ok": False, "error": "未提供 MDL（既没提交 JSON，也没有已登记的内联 MDL）"}

    checks["wren"] = _probe_wren(wren_url, n_models)
    ok = all(v.get("ok") for v in checks.values())
    return {
        "ok": ok,
        "checks": checks,
        "notice": "三项判据全通过，可以登记" if ok else "有判据未通过，按各自提示处理后再登记",
    }
