# -*- coding: utf-8 -*-
"""BFF 出参的「模型端点放行」规则（纯函数，无框架依赖，可离线自证）。

为什么需要这一步
----------------
`bff/sanitize.py` 会对 `*url*` 类字段做**按键脱敏**（把值换成占位符），并把
`*_endpoint` / `*_path` 这类键直接删掉——对「内部组件地址」（网关、Wren、对象存储）
这是对的，前端不该知道它们在哪。

但大模型配置页里有一个字段的性质完全不同：**模型端点由管理员自己填**。
实测（2026-10-22，真实链路跑 `GET /api/v1/settings/llm`）：
不加这一步时 `base_url` 返回占位符 ⇒ 页面上的输入框是空的 ⇒
用户想改模型名，一保存就把端点丢了（`custom` 供应商下等于把配置弄坏）。

所以对**这一个语义**做显式放行。范围用清单写死（顶层 / config / settings / providers），
不用递归通配——将来新增别的 URL 字段不会被顺带放出去。

它为什么不是新的信息面
----------------------
读回的是管理员**自己刚写进去的那份**。要利用它得知内部拓扑，前提是已经知道拓扑
并能写进这个字段——没有信息增益。红线 R2 禁止的是「本栈开源组件的地址与端口」
出现在前端，模型端点不属于本栈组件；`tools/arch_r2_leak_check.py` 仍会拦住任何
硬编码地址，本模块里没有一个字面量地址。
"""
from __future__ import annotations

from typing import Any, Callable

#: 端点长度上限。超长的一律不回显——正常端点不会超过这个量级，
#: 超了更可能是有人在字段里塞了别的东西。
ENDPOINT_MAX = 300

#: 需要放行的位置（写死清单，别改成递归）。
_CONTAINERS = ("", "config", "settings")


def safe_endpoint(value: Any) -> str:
    """只放行形如 `http(s)://host[/path]` 且不带账号口令的地址；其余返回空串。"""
    s = value.strip() if isinstance(value, str) else ""
    if not s or len(s) > ENDPOINT_MAX:
        return ""
    if not (s.startswith("http://") or s.startswith("https://")):
        return ""
    rest = s.split("://", 1)[1]
    authority = rest.split("/", 1)[0]
    if not authority:
        return ""
    if "@" in authority:  # http://user:pass@host/... 不回显
        return ""
    return s


def _put(dst: Any, src: Any) -> None:
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return
    if "base_url" not in src:
        return
    # **无条件覆盖**（允许是空串），而不是「有值才覆盖」：
    # sanitize 会把空串也换成占位符时，界面就会把「[地址已脱敏]」当成真实地址显示出来，
    # 那比空更糟。空串不泄露任何信息，占位符会误导。
    dst["base_url"] = safe_endpoint(src.get("base_url"))


def _put_all(dst: Any, src: Any) -> None:
    if not isinstance(dst, dict) or not isinstance(src, dict):
        return
    for key in _CONTAINERS:
        if key == "":
            _put(dst, src)
        else:
            _put(dst.get(key), src.get(key))
    rp, cp = src.get("providers"), dst.get("providers")
    if isinstance(rp, list) and isinstance(cp, list):
        for i, item in enumerate(rp):
            if i < len(cp):
                _put(cp[i], item)


def apply(raw: Any, clean: Any) -> Any:
    """把「模型端点」补回已脱敏的对象。`clean` 必须是 `sanitize(raw)` 的结果。

    **顺序不可反**：先脱敏、再按清单放行，才能保证除这几处之外的一切仍走脱敏。
    """
    if not isinstance(raw, dict) or not isinstance(clean, dict):
        return clean
    _put_all(clean, raw)
    # 保存 / 恢复默认的返回体把视图嵌在 `settings` 里，那一层同样要补
    _put_all(clean.get("settings"), raw.get("settings"))
    return clean


def view(raw: Any, sanitizer: Callable[[Any], Any]) -> Any:
    """便捷入口：`view(payload, sanitize)`。"""
    return apply(raw, sanitizer(raw))
