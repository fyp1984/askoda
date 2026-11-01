# -*- coding: utf-8 -*-
"""大模型配置：持久化、生效解析、连通性自测。

为什么要有这个模块
------------------
`gateway/llm.py` 只认环境变量，于是「换个模型」必须改 compose 再重建镜像——
这是运维动作，不是产品能力。终端客户（尤其私有化部署的甲方）需要在界面上
自己选供应商、填密钥、点一下「测试连接」就能确认通不通。

本模块把配置从「部署期」搬到「运行期」，并守住三条边界：

1. **密钥不出网关**：`get_settings()` 只回掩码（`sk-…3f2a`）与「是否已设置」，
   明文密钥不经过任何出参、不写日志、不进审计详情。
2. **未配置就等于没接**：三项（base_url / api_key / model）没配齐 →
   `enabled=False` → `sqlgen` 回落确定性规划器，行为与引入大模型之前**逐字一致**。
3. **元数据库不可用不能拖垮主链路**：`effective()` 永不抛异常；读库失败按
   「无持久化配置」处理并回落环境变量，同时把失败原因带出去供排障。

优先级（重要，别猜）
--------------------
- 元数据库里**存在**这一行 ⇒ **一律以行内为准**（含「把密钥清空」这种状态）。
- 库里没有行 ⇒ 用环境变量（`LLM_*`），即「部署时配好、不开界面也能跑」。
- 想从「界面配置」退回「环境变量配置」⇒ `reset=True` 删掉那一行。

不做字段级混合：一半来自库、一半来自环境变量的组合没人能解释清楚，
出问题时也分不清是谁覆盖了谁。要么整行生效，要么整组环境变量生效。

「谁说了算」还可以再钉死：`LLM_SETTINGS_SOURCE=env` ⇒ **完全不读元数据库**，
只认环境变量。这不是给测试留的后门，是两个真实需求：

1. 部署侧需要。K8s / 一体机里配置由 ConfigMap 下发，运营人员在界面上点一下
   就能改掉生产配置，这个口子必须能被关上。
2. 门禁必须和「本机库里碰巧存了什么」解耦。实测踩过：本机库里留了一条已启用的
   配置行，`llm_sqlgen_selftest` 里「未配置应禁用」的用例就变红了——同一份代码
   在别人机器上是绿的。这种红灯排查成本极高，必须根治。

环境变量
--------
仍保留 `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_ENABLED` /
`LLM_TIMEOUT` / `LLM_MAX_RETRIES` / `LLM_MAX_REPAIR` / `LLM_TEMPERATURE`
（语义见 `gateway/llm.py` 文件头），此处只负责读取与归一。
"""
from __future__ import annotations

import os
import threading
import time

import db

SETTINGS_ID = 1
SCOPE = "llm"

_OFF_VALUES = {"0", "false", "off", "no", "disabled"}

# 缓存：`llm.config()` 会在一次生成中被调用 2~3 次（is_enabled + chat），
# 每次都读库没必要；而「改完立即生效」又是必须的——所以给一个很短的 TTL +
# 写入时主动失效。真正的语义是「5 秒内收敛」，对配置类场景足够。
_TTL_OK = 5.0
_TTL_FAIL = 30.0
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "val": None, "ttl": _TTL_OK}

# `LLM_SETTINGS_SOURCE=env` ⇒ 只认环境变量，连库都不读（理由见文件头）
SOURCE_ENV_VAR = "LLM_SETTINGS_SOURCE"
_ENV_AUTHORITATIVE = {"env", "environment", "env-only", "envonly"}


def env_authoritative():
    """是否由环境变量独占配置（不看元数据库）。"""
    try:
        v = (os.getenv(SOURCE_ENV_VAR) or "").strip().lower().replace("_", "-")
    except Exception:  # noqa: BLE001
        return False
    return v in _ENV_AUTHORITATIVE


# ---------------------------------------------------------------------------
# 供应商预设
#
# 只放**确定可用**的 OpenAI 兼容端点：全部走 /chat/completions + Bearer 鉴权，
# 不需要为本模块引入任何厂商 SDK（红线五：不新增运行时依赖）。
# 新增一家 = 这里加一条，前端下拉自动出现（预设由后端下发，不在前端硬编码）。
# ---------------------------------------------------------------------------
PROVIDERS = [
    {
        "key": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "note": "性价比高，SQL 生成常用；deepseek-chat 已足够。",
    },
    {
        "key": "dashscope",
        "label": "阿里云百炼 · 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "note": "走百炼的 OpenAI 兼容模式；国内网络可达性好。",
    },
    {
        "key": "zhipu",
        "label": "智谱 · GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash"],
        "note": "glm-4-flash 免费额度较多，适合先试通链路。",
    },
    {
        "key": "moonshot",
        "label": "月之暗面 · Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "note": "长上下文友好，适合把整份语义层塞进 prompt。",
    },
    {
        "key": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o"],
        "note": "境内访问通常需要自建代理，代理地址填在「接口地址」里。",
    },
    {
        "key": "custom",
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "models": [],
        "note": "任何兼容 /chat/completions 的服务，含 vLLM / Ollama / 一体机。",
    },
]

_PROVIDER_BY_KEY = {p["key"]: p for p in PROVIDERS}


def providers():
    """预设供应商清单（供前端下拉；深拷贝，避免调用方改到全局）。"""
    return [
        {
            "key": p["key"],
            "label": p["label"],
            "base_url": p["base_url"],
            "models": list(p["models"]),
            "note": p["note"],
        }
        for p in PROVIDERS
    ]


def provider_of(key):
    """按 key 取预设；未知 key 返回 None（不做「猜一个最像的」）。"""
    p = _PROVIDER_BY_KEY.get((key or "").strip())
    return dict(p) if p else None


def guess_provider(base_url):
    """按接口地址反推预设（用于老数据/环境变量配置回填下拉）。

    只做**精确匹配**（归一化后比较），不做模糊匹配——猜错供应商比猜不出来更糟：
    用户会以为「系统认得我的配置」，实际却把模型名按别家的习惯填进去。
    """
    b = _norm_base_url(base_url)
    if not b:
        return ""
    for p in PROVIDERS:
        if p["base_url"] and _norm_base_url(p["base_url"]) == b:
            return p["key"]
    return ""


# ---------------------------------------------------------------------------
# 归一与校验
# ---------------------------------------------------------------------------
def _norm_base_url(v):
    return (v or "").strip().rstrip("/")


def _bool(v, default=True):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "auto"):
        return True
    if s in _OFF_VALUES:
        return False
    return default


def _num(v, default, lo=None, hi=None, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        x = cast(default)
    if lo is not None and x < lo:
        x = lo
    if hi is not None and x > hi:
        x = hi
    return x


def mask(value):
    """密钥掩码：只露头 3 位与尾 4 位。

    故意不返回长度、不返回完整串——掩码的作用是「让你确认填的是哪一把」，
    不是「让你把密钥抄回去」。短密钥整串打星，避免掩码反而泄露全部。
    """
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v)
    return v[:3] + "…" + v[-4:]


def _from_env():
    """环境变量口径的配置（与 `llm.config()` 同源，避免两处解析漂移）。"""
    base_url = _norm_base_url(os.getenv("LLM_BASE_URL"))
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    model = (os.getenv("LLM_MODEL") or "").strip()
    flag = (os.getenv("LLM_ENABLED") or "auto").strip().lower()
    return {
        "source": "env",
        "provider": guess_provider(base_url),
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "requested_enabled": flag not in _OFF_VALUES,
        "temperature": _num(os.getenv("LLM_TEMPERATURE"), 0.0, 0.0, 2.0),
        "timeout": _num(os.getenv("LLM_TIMEOUT"), 60, 5, 600, int),
        "max_repair": _num(os.getenv("LLM_MAX_REPAIR"), 1, 0, 3, int),
    }


def _from_row(row):
    return {
        "source": "db",
        "provider": (row.get("provider") or "").strip(),
        "base_url": _norm_base_url(row.get("base_url")),
        "api_key": (row.get("api_key") or "").strip(),
        "model": (row.get("model") or "").strip(),
        "requested_enabled": bool(row.get("enabled", True)),
        "temperature": _num(row.get("temperature"), 0.0, 0.0, 2.0),
        "timeout": _num(row.get("timeout"), 60, 5, 600, int),
        "max_repair": _num(row.get("max_repair"), 1, 0, 3, int),
        "updated_by": row.get("updated_by") or "",
        "updated_at": _iso(row.get("updated_at")),
    }


def _iso(v):
    if not v:
        return ""
    try:
        return v.strftime("%Y-%m-%d %H:%M:%S")
    except AttributeError:
        return str(v)


def _finalize(cfg):
    """补上派生字段：configured / enabled / 缺什么。"""
    missing = []
    if not cfg.get("base_url"):
        missing.append("base_url")
    if not cfg.get("api_key"):
        missing.append("api_key")
    if not cfg.get("model"):
        missing.append("model")
    cfg["configured"] = not missing
    cfg["missing"] = missing
    cfg["enabled"] = bool(cfg.get("requested_enabled")) and not missing
    return cfg


# ---------------------------------------------------------------------------
# 读库
# ---------------------------------------------------------------------------
_SELECT = """
    SELECT provider, base_url, api_key, model, enabled, temperature, timeout,
           max_repair, updated_by, updated_at
      FROM llm_settings WHERE settings_id = %s
"""


def _row():
    """取持久化配置行；无行返回 None，库不可用抛异常（由调用方决定怎么兜）。"""
    return db.query_one(_SELECT, (SETTINGS_ID,))


def invalidate():
    """配置写入后主动失效缓存（否则用户点保存后最多要等 TTL 才生效）。"""
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["val"] = None
        _cache["ttl"] = _TTL_OK


def effective():
    """**生效配置**（`llm.config()` 的唯一来源）。永不抛异常。

    返回体里带 `source`（`db` / `env`）与 `db_error`（读库失败原因），
    让「为什么我的配置没生效」能一眼定位，而不是靠翻日志猜。
    """
    if env_authoritative():
        # 显式指定过「只认环境变量」⇒ 不读库、不写缓存（环境变量本来就随时可变，
        # 而且这条路径必须是最可预测的一条：出问题时不该有 5 秒的旧值干扰判断）。
        cfg = _finalize(_from_env())
        cfg["source"] = "env"
        cfg["forced"] = "env"
        cfg["db_error"] = ""
        cfg["db_available"] = False  # 不是"库坏了"，而是"本次不查库"，见 forced
        return cfg

    now = time.time()
    with _cache_lock:
        cached = _cache["val"]
        if cached is not None and (now - _cache["at"]) < _cache["ttl"]:
            return dict(cached)

    db_error = ""
    try:
        row = _row()
    except Exception as e:  # noqa: BLE001  库不可用 → 回落环境变量，绝不打断主链路
        row = None
        db_error = "%s: %s" % (type(e).__name__, str(e)[:200])

    if row:
        cfg = _finalize(_from_row(row))
    else:
        cfg = _finalize(_from_env())
        if db_error:
            cfg["db_error"] = db_error
            cfg["source"] = "env"
    cfg["db_available"] = not db_error

    ttl = _TTL_OK if not db_error else _TTL_FAIL
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["val"] = dict(cfg)
        _cache["ttl"] = ttl
    return cfg


# ---------------------------------------------------------------------------
# 面向界面的读（只回掩码）
# ---------------------------------------------------------------------------
def _recent_changes(limit=5):
    """最近若干次配置变更（审计用）。读不到就返回空列表，不报错。"""
    try:
        rows = db.query(
            """
            SELECT action, actor, detail, created_at FROM config_events
             WHERE scope = %s ORDER BY event_id DESC LIMIT %s
            """,
            (SCOPE, max(1, int(limit))),
        )
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        detail = r.get("detail") or {}
        if isinstance(detail, str):
            try:
                import json

                detail = json.loads(detail)
            except ValueError:
                detail = {}
        out.append(
            {
                "action": r.get("action"),
                "actor": r.get("actor") or "",
                "at": _iso(r.get("created_at")),
                # 审计详情里**永远不出现密钥**，只记「是否换过」
                "credential_changed": bool(detail.get("credential_changed")),
                "model": detail.get("model") or "",
            }
        )
    return out


def get_settings(include_presets=True, history=5):
    """给界面的配置视图。**明文密钥不出现在返回体里的任何位置。**"""
    key = ""
    try:
        row = _row()
    except Exception as e:  # noqa: BLE001
        row = None
        key = "%s: %s" % (type(e).__name__, str(e)[:200])

    eff = effective()
    env_cfg = _from_env()

    view = {
        "ok": not key,
        "source": eff.get("source"),
        "provider": eff.get("provider") or guess_provider(eff.get("base_url")),
        "base_url": eff.get("base_url") or "",
        "model": eff.get("model") or "",
        "enabled": bool(eff.get("requested_enabled")),
        "effective": bool(eff.get("enabled")),
        "configured": bool(eff.get("configured")),
        "missing": eff.get("missing") or [],
        "temperature": eff.get("temperature"),
        "timeout": eff.get("timeout"),
        "max_repair": eff.get("max_repair"),
        "persisted": bool(row),
        "updated_by": eff.get("updated_by") or "",
        "updated_at": eff.get("updated_at") or "",
        # 凭据只给「有没有」和「尾号」，不给明文
        "has_credential": bool(eff.get("api_key")),
        "credential_masked": mask(eff.get("api_key")),
        # 环境变量侧的状态：让用户知道「就算我清空界面配置，系统还有兜底」
        "env_configured": bool(env_cfg["base_url"] and env_cfg["api_key"] and env_cfg["model"]),
        "env_model": env_cfg["model"] if env_cfg["model"] else "",
    }
    if key:
        view["db_error"] = key
        view["ok"] = False
    if include_presets:
        view["providers"] = providers()
    if history:
        view["recent_changes"] = _recent_changes(history)
    return view


# ---------------------------------------------------------------------------
# 写（save / reset）
# ---------------------------------------------------------------------------
_UPSERT = """
    INSERT INTO llm_settings (
        settings_id, provider, base_url, api_key, model, enabled,
        temperature, timeout, max_repair, updated_by, updated_at
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
    ON CONFLICT (settings_id) DO UPDATE SET
        provider = EXCLUDED.provider,
        base_url = EXCLUDED.base_url,
        api_key  = EXCLUDED.api_key,
        model    = EXCLUDED.model,
        enabled  = EXCLUDED.enabled,
        temperature = EXCLUDED.temperature,
        timeout  = EXCLUDED.timeout,
        max_repair = EXCLUDED.max_repair,
        updated_by = EXCLUDED.updated_by,
        updated_at = now()
"""


def _audit(action, actor, detail):
    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) VALUES (%s,%s,%s,%s)",
            (SCOPE, action, actor or "system", db.dumps(detail)),
        )
    except Exception:  # noqa: BLE001  审计写失败不能反噬业务
        pass


def save_settings(payload, actor=""):
    """保存配置（部分更新）。

    密钥语义（最容易被误解，写清楚）：
      · `api_key` 为**非空字符串** ⇒ 覆盖为新值；
      · `api_key` 缺省 / 空串 / None ⇒ **保持库里原值不变**（界面上回显的是掩码，
        用户不可能把掩码当密钥提交回来，所以「空 = 不改」是唯一安全的解释）；
      · 要清空密钥 ⇒ 显式传 `clear_credential=True`（界面上是「清除密钥」按钮）。

    返回 `{ok, changed:[...], warnings:[...], settings:{...}}`：
    只回显**改了什么**，让操作者能确认「我这一下确实只动了模型名」。
    """
    payload = payload or {}
    try:
        cur_row = _row()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "元数据库不可用，配置无法持久化：%s" % str(e)[:200]}

    cur = _finalize(_from_row(cur_row)) if cur_row else _finalize(_from_env())

    provider = (payload.get("provider") or cur.get("provider") or "deepseek").strip()
    preset = provider_of(provider)
    base_url = _norm_base_url(payload.get("base_url"))
    if not base_url and preset and preset["base_url"]:
        # 选了预设且地址留空 ⇒ 用预设地址；显式填了就以填的为准（代理/私有化场景）
        base_url = preset["base_url"]
    if not base_url:
        base_url = cur.get("base_url") or ""

    model = (payload.get("model") or "").strip() or cur.get("model") or ""

    # 「留空 = 沿用当前」对 api_key 是**必须**的（界面回显的是掩码，用户不可能把掩码
    # 当密钥提交回来）。但 MCP 工具分不清「没传」与「传了空串」，于是模型名/地址也被
    # 一并沿用——用户在界面上清空模型名再保存，系统悄悄用回旧值，他会以为改成功了。
    # 处置：沿用可以，但**必须说出来**（进 warnings），并在前端把「必填为空」挡在提交前。
    kept = []
    if not (payload.get("model") or "").strip() and cur.get("model"):
        kept.append(("模型名", str(cur.get("model"))))
    if not _norm_base_url(payload.get("base_url")) and not (preset and preset["base_url"]) and cur.get("base_url"):
        kept.append(("接口地址", str(cur.get("base_url"))))

    clear_cred = _bool(payload.get("clear_credential"), False)
    raw_key = payload.get("api_key")
    raw_key = raw_key.strip() if isinstance(raw_key, str) else ""
    if clear_cred:
        api_key = ""
    elif raw_key:
        api_key = raw_key
    else:
        api_key = cur.get("api_key") or ""

    enabled = _bool(payload.get("enabled"), cur.get("requested_enabled", True))
    temperature = _num(payload.get("temperature"), cur.get("temperature", 0.0), 0.0, 2.0)
    timeout = _num(payload.get("timeout"), cur.get("timeout", 60), 5, 600, int)
    max_repair = _num(payload.get("max_repair"), cur.get("max_repair", 1), 0, 3, int)

    errors, warnings = [], []
    if not base_url:
        errors.append("接口地址不能为空（选「自定义」时必须自己填，如 https://your-llm/v1）")
    elif not (base_url.startswith("http://") or base_url.startswith("https://")):
        errors.append("接口地址必须是 http(s):// 开头的完整地址")
    if not model:
        errors.append("模型名不能为空")
    if enabled and not api_key:
        errors.append("开启大模型必须先填 API Key")
    if enabled and base_url and base_url.startswith("http://") and "127.0.0.1" not in base_url and "localhost" not in base_url:
        warnings.append("接口地址用的是 http（非加密），密钥会以明文经过网络；内网可接受，公网建议换 https。")
    for label, val in kept:
        warnings.append("%s 未填写，已沿用当前值「%s」；要改成别的请直接填。" % (label, val))
    if errors:
        # 同时给 `errors`（逐条渲染）与 `error`（单句，供上游统一错误体使用）：
        # BFF 的 `_tool_rejected` 认的是 `error`，只给 errors 会被当成 200 正常返回，
        # 前端就得在两套约定之间做判断——接口契约不该有第二种形态。
        return {"ok": False, "error": "；".join(errors), "errors": errors, "warnings": warnings}

    new_row = {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "enabled": enabled,
        "temperature": temperature,
        "timeout": timeout,
        "max_repair": max_repair,
    }
    changed = []
    for f in ("provider", "base_url", "model", "enabled", "temperature", "timeout", "max_repair"):
        if cur.get(f) != new_row[f]:
            changed.append(f)
    key_changed = (cur.get("api_key") or "") != api_key
    if key_changed:
        changed.append("api_key")

    try:
        db.execute(
            _UPSERT,
            (
                SETTINGS_ID,
                provider,
                base_url,
                api_key,
                model,
                enabled,
                temperature,
                timeout,
                max_repair,
                actor or "system",
            ),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "写入配置失败：%s" % str(e)[:200]}

    _audit(
        "save",
        actor,
        {
            "changed": [c for c in changed if c != "api_key"],
            "credential_changed": key_changed,
            "model": model,
            "provider": provider,
            "base_url": base_url,
            "enabled": enabled,
        },
    )
    invalidate()

    view = get_settings(include_presets=False, history=0)
    return {
        "ok": True,
        "changed": changed,
        "warnings": warnings,
        "effective": bool(view.get("effective")),
        "settings": view,
        "notice": ("配置已保存，已生效。" if view.get("effective") else "配置已保存，但当前未生效（%s）。" % _why_not(view)),
    }


def _why_not(view):
    """把「为什么没生效」翻译成一句人话（前端直接展示，不需要再拼）。"""
    if not view.get("enabled"):
        return "大模型开关是关的"
    miss = view.get("missing") or []
    if miss:
        label = {"base_url": "接口地址", "api_key": "API Key", "model": "模型名"}
        return "还缺 " + "、".join(label.get(m, m) for m in miss)
    return "未知原因"


def reset_settings(actor=""):
    """删除持久化配置 ⇒ 回落到环境变量口径（不再是「留一条空配置」）。"""
    try:
        n = db.execute("DELETE FROM llm_settings WHERE settings_id = %s", (SETTINGS_ID,))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "删除配置失败：%s" % str(e)[:200]}
    _audit("reset", actor, {"deleted": bool(n)})
    invalidate()
    view = get_settings(include_presets=False, history=0)
    return {
        "ok": True,
        "settings": view,
        "notice": (
            "已恢复为系统默认（环境变量配置）。"
            if view.get("env_configured")
            else "已清除界面配置；当前没有可用的大模型，SQL 生成走确定性规划器。"
        ),
    }


# ---------------------------------------------------------------------------
# 连通性自测
# ---------------------------------------------------------------------------
def _chat(messages, **kw):
    """间接调用 `llm.chat`，留出替换点（自测用桩替换，不打网络）。"""
    import llm

    return llm.chat(messages, **kw)


def _candidate(payload):
    """把「界面上的草稿」合成一份可测试的配置（未填的字段沿用当前生效值）。"""
    payload = payload or {}
    cur = effective()
    preset = provider_of((payload.get("provider") or "").strip())
    base_url = _norm_base_url(payload.get("base_url")) or (
        preset["base_url"] if preset and preset["base_url"] else ""
    ) or cur.get("base_url") or ""
    model = (payload.get("model") or "").strip() or cur.get("model") or ""
    raw = payload.get("api_key")
    raw = raw.strip() if isinstance(raw, str) else ""
    # 测试用的是「已保存的密钥」还是「刚输入的密钥」必须说清楚，
    # 否则用户改了密钥点测试通过、保存后却不通，会以为系统在骗他。
    used_saved_credential = not raw
    api_key = raw or cur.get("api_key") or ""
    return (
        {
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "temperature": cur.get("temperature", 0.0),
            "timeout": cur.get("timeout", 60),
            "max_retries": 0,
        },
        used_saved_credential,
    )


def _list_models(cfg, timeout):
    """间接调用 `llm.list_models`，同样留替换点（自测不打网络）。"""
    import llm

    return llm.list_models(cfg, timeout=timeout)


def _probe(cfg, timeout):
    """第一段：只问「地址通不通、密钥认不认、模型在不在」——纯元数据，秒级。

    2026-11-01 事故：推理模型（deepseek-r1:7b）连「回复两个字」都要思考 359 个
    token、跑 23 秒，而测试动作只等 20 秒 ⇒ 报「地址不通或网络被拦」。
    实测容器到该端口 0.06 秒就 200，网络完全没问题——**是模型慢，不是不通**。
    所以探活必须单独做一段：拿不到清单才叫不通，拿得到清单就只剩慢。
    """
    res = _list_models(cfg, timeout)
    names = res.get("models") or []
    listed = None
    try:
        import llm

        listed = llm.model_listed(cfg.get("model"), names)
    except Exception:  # noqa: BLE001
        listed = None
    err = res.get("error") or ""
    low = err.lower()
    # 「这段结论能不能定案」：不是所有失败都等于「地址错」。
    # 401/403、连接被拒、DNS 失败、超时 ⇒ 定案；
    # 404 / 清单格式不认识 ⇒ **定不了**（很多自建代理压根不实现 /models），
    # 这种情况必须放行到第二段，否则会把「能用但没清单接口」的代理误判成不通。
    conclusive = bool(res.get("ok")) or bool(res.get("status") in (401, 403)) or (
        "timed out" in low
        or "timeout" in low
        or "refused" in low
        or "getaddrinfo" in low
        or "name or service not known" in low
        or "connectionreset" in low
    )
    return {
        "reachable": bool(res.get("ok")),
        "status": res.get("status"),
        "model_count": len(names),
        "model_listed": listed,
        "conclusive": conclusive,
        "error": err,
    }


def test_connection(payload=None, timeout=None):
    """验证「地址 / 密钥 / 模型名」三件事，并分清「不通」与「太慢」。

    分两段，因为这两件事的修法完全不同：
      ① 清单探测（秒级）：`GET {base}/models`。地址错、密钥错、模型名错，
         在这一步就能定死，且**不会因为模型慢而误判**。
      ② 生成探测：真发一句「回复两个字：可用」。验证端到端能出内容，
         这一步受模型速度支配，超时只说明「这台机器跑这个模型跟不上」。

    返回里 `probe` 段把两段的结论都摊开，`notice` 只讲人话。
    """
    cfg, used_saved = _candidate(payload)
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return {
            "ok": False,
            "error": "配置不完整，无法测试：请先填好接口地址、API Key、模型名。",
            "config": _safe_cfg(cfg),
        }
    # 超时不再写死：跟随「这份草稿 / 已保存配置」里的单次调用超时。
    # 旧代码是 min(20, cfg["timeout"])，页面上填 90 也照样 20 秒判死。
    # timeout 传 0/负数 表示「没指定」⇒ 用配置里的值。
    want = timeout if (timeout or 0) > 0 else cfg["timeout"]
    to = _num(want, cfg["timeout"], 3, 300, int)

    probe = _probe(cfg, min(8, to))
    out = {
        "ok": False,
        "config": _safe_cfg(cfg),
        "used_saved_credential": used_saved,
        "probe": probe,
    }

    # 探活就把话说死了的，别再去等生成 —— 等下去只会得到一条误导人的超时。
    # 但「定不了案」的失败（如自建代理不实现 /models）必须放行到第二段。
    if not probe["reachable"] and probe.get("conclusive"):
        reason = _diagnose(
            {"ok": False, "status": probe.get("status"), "error": probe.get("error")},
            cfg,
            probe,
        )
        out["error"] = probe.get("error") or "模型清单接口不可用"
        out["notice"] = reason
        return out
    if probe.get("model_listed") is False:
        out["error"] = "模型清单里没有「%s」" % cfg["model"]
        out["notice"] = _diagnose({"ok": False, "status": 404}, cfg, probe)
        return out

    t0 = time.time()
    res = _chat(
        [
            {"role": "system", "content": "你是联通性探针，只按用户要求回一个词。"},
            {"role": "user", "content": "回复两个字：可用"},
        ],
        cfg=cfg,
        timeout=to,
        max_retries=0,
    )
    ms = int((time.time() - t0) * 1000)
    out["latency_ms"] = res.get("latency_ms", ms)

    if res.get("ok"):
        reply = str(res.get("content") or "").strip()
        out["ok"] = True
        out["reply"] = reply[:80]
        out["model_reported"] = res.get("model") or cfg["model"]
        out["notice"] = "连通正常，可以保存。" if used_saved else "连通正常（用的是这次填的密钥）。"
        if not reply:
            # 推理模型会把 token 全用在思考上，返回体里 content 是空的。
            # 这是「通但答非所问」，不拦保存，但要让人心里有数。
            out["notice"] += " 注意：这次模型没有输出可见内容（推理模型的思考过程不计入正文），生成 SQL 时可能偏慢。"
    else:
        out["error"] = res.get("error") or "调用失败"
        out["notice"] = _diagnose(res, cfg, probe)
    return out


def _safe_cfg(cfg):
    """回显被测配置（密钥打码），让用户确认「测的是哪一套」。"""
    return {
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "credential_masked": mask(cfg.get("api_key")),
    }


def _diagnose(res, cfg, probe=None):
    """把底层报错翻译成「该改哪里」。**不暴露任何内部地址与堆栈。**

    `probe` 是第一段（模型清单）的结论。它决定「超时」这句话该怎么说：
    探得通 ⇒ 地址与密钥都没问题，是**模型慢**；探不通 ⇒ 才是地址/网络问题。
    旧版本一律说「地址不通或网络被拦」，2026-11-01 因此把人带去查网络。
    """
    status = res.get("status")
    err = str(res.get("error") or "")
    probe = probe or {}
    reachable = bool(probe.get("reachable"))
    listed = probe.get("model_listed")
    count = probe.get("model_count") or 0

    if status in (401, 403):
        return "鉴权被拒（HTTP %s）：API Key 不对、已过期，或没有该模型的权限。换一把密钥试试。" % status
    if status == 404 and reachable and listed is False:
        return "模型名不对：这个地址下没有「%s」。从清单里挑一个（共 %d 个），或先在上面拉取模型再选。" % (
            cfg.get("model") or "-",
            count,
        )
    if status == 404:
        return "地址不对（HTTP 404）：接口地址通常要带 /v1 或 /compatible-mode/v1 这类后缀，检查一下再试。"
    if status == 429:
        return "被限流（HTTP 429）：密钥可用但额度/并发超了，稍后重试或换模型。"
    if status and 400 <= status < 500:
        return "请求被拒（HTTP %s）：多为模型名不存在或参数不被支持。核对模型名。" % status
    if "timed out" in err.lower() or "timeout" in err.lower():
        if reachable:
            return (
                "通路正常，是模型太慢：地址和密钥都对（已识别到 %d 个模型），"
                "但这次生成在超时内没回完。把「单次调用超时」调大，或换一个更快的模型"
                "（推理型模型答一句话也要几十秒，用来生成 SQL 常常不够）。" % count
            )
        msg = "连接超时：地址不通或网络被拦。若用自建代理，确认地址与端口可从服务器访问。"
        if probe and probe.get("conclusive") is False:
            msg += "（这个地址没有提供模型清单，无法先单独确认可达性。）"
        return msg
    if "Name or service not known" in err or "getaddrinfo" in err or "NameError" in err:
        return "域名解析失败：接口地址的域名拼写有问题，或服务器没有外网 DNS。"
    if "Connection refused" in err or "ConnectionReset" in err:
        return "连不上端口：服务未启动或端口不对。"
    if "SSL" in err or "certificate" in err:
        return "TLS 握手失败：证书不被信任，或代理劫持。自建服务可临时用 http，公网请修证书。"
    return "调用失败，详见错误信息。可对照「预设供应商」里该家的接口地址检查。"


def status_line():
    """一句话状态（给 /healthz 与首页用）。不含任何地址与密钥。"""
    eff = effective()
    if eff.get("enabled"):
        return "大模型已启用（%s，来源：%s）" % (
            eff.get("model") or "-",
            "界面配置" if eff.get("source") == "db" else "环境变量",
        )
    if eff.get("configured") and not eff.get("requested_enabled"):
        return "大模型已配置但被关闭，SQL 生成走确定性规划器"
    return "大模型未配置，SQL 生成走确定性规划器（可在「系统设置」里配置）"
