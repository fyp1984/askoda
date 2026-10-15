"""MDL 可读性启动自检（C 方案：读不到就拒绝启动，不静默降级）

## 为什么要有这个模块

2026-10-15 实测到的故障：`/workspace-a/mdl.json`、`/workspace-b/mdl.json` 是 bind mount，
容器内**以root 也可能读不到**（`PermissionError: [Errno 1] Operation not permitted`）。
一旦读不到，`rules.evaluate_all()` 会把异常吞成一条 `RULE_INTERNAL_ERROR` 提示后继续，
`sql_review` 照样返回「警告」——**L2 静态规则整层失效且无人知道**。

这类故障之所以危险，正是因为它「静默」。本模块把��沉默」改成「显式」：
启动时逐个数据集校验 MDL 是否可读、能否建出规则索引，任一失败即拒绝启动。

## 判据

- MDL 文件存在且可读（非空 JSON）
- `rules.build_mdl_index(dataset)` 能成功建出索引

两项任一不满足 → 记一条 failure，带数据集 key、路径、异常类型与原文。

## 边界

- 只做**本地文件**与**规则索引**校验，不碰网络（Wren 探活由 /healthz 负责）。
- 不改任何工具入参，不进 MCP 工具面（工具面仍为 48，见 docs/MCP_TOOLS.md）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple


def _default_index_builder(key: str, path: str) -> Any:
    """默认索引构建器：走真实 rules.build_mdl_index。

    注意它内部经registry.get(key).mdl **每次重读文件、无内存缓存**，
    所以本判据与「L2 规则运行时实际读的那份文件」严格同源。
    """
    import rules  # 延迟导入：宿主直跑时 sys.path 未必含 gateway/

    return rules.build_mdl_index(key)


def _check_one(key: str, path: str, index_builder=None) -> Dict[str, Any]:
    """校验单个数据集的 MDL。返回 {ok, key, path, error}，不抛异常。

    index_builder 仅供自证注入假实现用，生产路径固定走 _default_index_builder。
    """
    item: Dict[str, Any] = {"ok": False, "key": key, "path": path, "error": ""}

    if not path:
        item["error"] = "MDL 路径为空（环境变量未配置？）"
        return item
    if not os.path.exists(path):
        item["error"] = "MDL 文件不存在：%s" % path
        return item

    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:  # noqa: BLE001 — 自检要把任何异常都转成结论
        item["error"] = "MDL 不可读或不是合法 JSON（%s: %s）" % (type(e).__name__, str(e)[:200])
        return item

    if not isinstance(payload, dict) or not payload:
        item["error"] = "MDL 内容为空或结构异常（顶层类型=%s）" % type(payload).__name__
        return item

    # 第二道判据：规则索引能否建出来。文件可读≠ 规则可用。
    try:
        builder = index_builder or _default_index_builder
        idx = builder(key, path)
        item["models"] = len(idx.get("models") or {}) if isinstance(idx, dict) else None
    except Exception as e:  # noqa: BLE001
        item["error"] = "规则索引构建失败（%s: %s）" % (type(e).__name__, str(e)[:200])
        return item

    item["ok"] = True
    return item


def selfcheck(datasets: Optional[List[Tuple[str, str]]] = None,
              index_builder=None) -> Dict[str, Any]:
    """对给定 (key, mdl_path) 列表做自检。

    datasets 为 None 时，从 registry.DATASETS 取全量。
    返回 {"ok": bool, "checked": n, "failures": [...], "items": [...]}。
    """
    if datasets is None:
        try:
            import registry

            datasets = [(ds.key, ds.mdl_path) for ds in registry.DATASETS.values()]
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "checked": 0,
                "items": [],
                "failures": [{"key": "-", "path": "-", "error": "读取数据集清单失败（%s: %s）" % (type(e).__name__, str(e)[:200])}],
            }

    items = [_check_one(key, path, index_builder=index_builder) for key, path in datasets]
    failures = [i for i in items if not i["ok"]]
    return {"ok": not failures, "checked": len(items), "items": items, "failures": failures}


def render(result: Dict[str, Any]) -> str:
    """把自检结果渲染成人能直接读懂的启动日志。"""
    if result.get("ok"):
        detail = "、".join(
            "%s=%s" % (i["key"], i.get("models") if i.get("models") is not None else "OK")
            for i in result.get("items", [])
        )
        return "MDL 自检通过（%d 个数据集：%s）" % (result.get("checked", 0), detail or "-")

    lines = ["🔴 MDL 自检失败（%d/%d 个数据集不可用）——已拒绝启动。"
             % (len(result.get("failures", [])), result.get("checked", 0))]
    for f in result.get("failures", []):
        lines.append("   - 数据集 %s @ %s" % (f.get("key"), f.get("path")))
        lines.append("     原因：%s" % f.get("error"))
    lines.append("   L2 静态规则依赖 MDL；MDL 不可用时规则会被静默跳过，")
    lines.append("   宁可启动失败，也不要让 sql_review 假装规则跑过了。")
    lines.append("   排查：① 确认镜像内 /app/mdl/ 已随构建带入（docker compose build gateway）")
    lines.append("        ② 确认 MDL_A_PATH / MDL_B_PATH 指向镜像内路径")
    lines.append("        ③ 若改回 bind mount，先在容器内验证 open(MDL_PATH) 可读")
    return "\n".join(lines)
