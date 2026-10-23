# -*- coding: utf-8 -*-
"""可配置的大模型客户端（OpenAI 兼容 /chat/completions）。

定位
----
本模块只负责「把一组 messages 发给模型、拿回文本」。它**不做任何业务判断**，
也不改变既有确定性链路：未配置（缺 base_url / api_key / model）即视为禁用，
`is_enabled()` 返回 False，调用方据此回落确定性规划器。

设计约束（与项目铁律一致）
--------------------------
- **不新增第三方依赖**：用标准库 urllib；容器/离线环境缺包也能导入。
- **永不向调用方抛异常**：失败返回 `{"ok": False, "error": ...}`，由调用方决定回退，
  绝不让一次模型抖动把整条生成链路带崩。
- **可观测**：返回 `latency_ms` / `status` / `model`，便于留痕与排障。
- **超时与重试可配**：避免长任务把 MCP 请求拖死（与 BFF 的 `ASKODA_MCP_TIMEOUT` 同思路）。

环境变量
--------
| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | 空 | OpenAI 兼容端点根，如 `https://api.deepseek.com/v1`（也支持 DashScope 兼容模式） |
| `LLM_API_KEY` | 空 | 鉴权密钥 |
| `LLM_MODEL` | 空 | 模型名，如 `deepseek-chat` / `qwen-max` |
| `LLM_ENABLED` | `auto` | `auto`=配齐即启用；`0/false/off` 强制禁用 |
| `LLM_TIMEOUT` | `60` | 单次调用超时（秒） |
| `LLM_MAX_RETRIES` | `1` | 网络层重试次数（不含业务修复轮） |
| `LLM_TEMPERATURE` | `0` | 采样温度（SQL 生成建议 0，保确定性） |

三项（base_url/api_key/model）任一缺失即 `configured=False`、`enabled=False`。

配置从哪来（优先级，勿猜）
--------------------------
1. **元数据库 `llm_settings` 有行** ⇒ 整行生效（即前端「系统设置」里保存的配置，
   见 `gateway/llm_settings.py`）。终端用户改模型不需要重建镜像。
2. **没有行** ⇒ 本模块下表的环境变量（部署期配置，容器/离线环境照旧可用）。

两条路径解析同一组字段，故 `llm_settings` 复用本模块的 `_env_config()`，
避免「环境变量怎么解析」出现第二份实现而逐渐漂移。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

_OFF_VALUES = {"0", "false", "off", "no", "disabled"}


def _int_env(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _float_env(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_config():
    """环境变量口径（不含持久化配置）。保留为独立函数：`llm_settings` 复用它，
    避免「环境变量到底怎么解析」出现第二份实现而逐渐漂移。"""
    base_url = (os.getenv("LLM_BASE_URL") or "").strip().rstrip("/")
    api_key = (os.getenv("LLM_API_KEY") or "").strip()
    model = (os.getenv("LLM_MODEL") or "").strip()
    flag = (os.getenv("LLM_ENABLED") or "auto").strip().lower()
    configured = bool(base_url and api_key and model)
    enabled = configured and flag not in _OFF_VALUES
    return {
        "configured": configured,
        "enabled": enabled,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout": _int_env("LLM_TIMEOUT", 60),
        "max_retries": max(0, _int_env("LLM_MAX_RETRIES", 1)),
        "temperature": _float_env("LLM_TEMPERATURE", 0.0),
        "source": "env",
    }


def config():
    """返回 LLM 配置与启用状态。

    取值顺序（见 `llm_settings` 文件头）：
      1. 元数据库 `llm_settings` 里有行 ⇒ 整行生效（前端「系统设置」保存的）；
      2. 没有行 ⇒ 环境变量 `LLM_*`（部署期配置）。
    读库失败一律回落环境变量，绝不因为元数据库抖动把问数链路带崩。
    """
    cfg = _env_config()
    try:
        import llm_settings as _settings  # 延迟导入：避免模块级循环依赖

        eff = _settings.effective()
    except Exception:  # noqa: BLE001  配置层任何异常都不应影响可用性
        return cfg

    if eff.get("source") != "db":
        return cfg

    cfg.update(
        {
            "configured": bool(eff.get("configured")),
            "enabled": bool(eff.get("enabled")),
            "base_url": eff.get("base_url") or "",
            "api_key": eff.get("api_key") or "",
            "model": eff.get("model") or "",
            "timeout": eff.get("timeout") or cfg["timeout"],
            "max_repair": eff.get("max_repair"),
            "temperature": eff.get("temperature")
            if eff.get("temperature") is not None
            else cfg["temperature"],
            "source": "db",
        }
    )
    return cfg


def is_enabled():
    """是否可用（配置齐全且未被显式关闭）。调用方据此决定是否走大模型路径。"""
    return config()["enabled"]


def completions_url(base_url):
    """把根地址规范成 `.../chat/completions`（已在则原样返回）。"""
    u = (base_url or "").rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u
    return u + "/chat/completions"


def chat(messages, *, json_mode=False, timeout=None, max_retries=None, temperature=None, cfg=None):
    """调用 chat/completions。

    返回（**永不抛异常**）：
      · 成功：`{"ok": True, "content": str, "model": str, "latency_ms": int}`
      · 失败：`{"ok": False, "error": str, "disabled": bool?, "status": int?}`
    4xx（鉴权/参数错误）不重试；5xx / 网络错误按 `max_retries` 重试。

    `cfg` 入参：传一份临时配置即可「按这份配置发一次请求」，不改动全局状态。
    这是「测试连接」必须的能力——用户在界面上填了新密钥想先试通再保存，
    不能为了试一次就把进程里的生效配置改掉（并发场景下等于给线上换了个模型）。
    """
    if cfg is None:
        cfg = config()
        ready = bool(cfg.get("enabled"))
    else:
        # 显式传入的临时配置：只看三项是否配齐（它代表「用户想试这一套」，
        # 不该被库里的开关状态挡住，否则「改了密钥想先测一下」永远测不通）。
        ready = bool(cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"))
    if not ready:
        return {
            "ok": False,
            "disabled": True,
            "error": "LLM 未启用（配置不完整，或 LLM_ENABLED=0 / 界面开关已关闭）",
        }

    url = completions_url(cfg["base_url"])
    # 用 get 兜底：`cfg` 可能是「测试连接」传进来的临时配置，字段不保证齐全。
    to = cfg.get("timeout", 60) if timeout is None else timeout
    retries = max(0, cfg.get("max_retries", 0)) if max_retries is None else max(0, max_retries)
    temp = cfg.get("temperature", 0.0) if temperature is None else temperature

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temp,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer %s" % cfg["api_key"],
    }

    last_err = ""
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8", "replace")
            latency = int((time.time() - t0) * 1000)
            try:
                data = json.loads(raw)
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError):
                return {
                    "ok": False,
                    "error": "响应格式不符：%s" % raw[:300],
                    "latency_ms": latency,
                }
            return {
                "ok": True,
                "content": content,
                "model": data.get("model") or cfg["model"],
                "latency_ms": latency,
            }
        except urllib.error.HTTPError as e:
            status = getattr(e, "code", 0)
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            last_err = "HTTP %s：%s" % (status, detail)
            # 4xx 属参数/鉴权问题，重试无意义
            if 400 <= status < 500:
                return {"ok": False, "error": last_err, "status": status}
        except Exception as e:  # noqa: BLE001  超时 / 连接 / DNS 等
            last_err = "%s: %s" % (type(e).__name__, str(e)[:240])
        if attempt < retries:
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
    return {"ok": False, "error": last_err or "LLM 调用失败"}
