# -*- coding: utf-8 -*-
"""口径的存取与生效（库为真源）。

真源为什么是库，不是文件
------------------------
网关的规划器读的是 `dataset_registry.mdl_json`（见 `registry.py`），**从来不读仓库里的文件**。
所以文件方案下「改完文件不跑脚本」会静默不生效——这正是 2026-10-31 之前反复踩的坑。
界面要能改，真源就必须在库上；`db/credit/04_metrics.json` 降级为**初始种子**
（重建库、换环境时用它导入，平时不参与运行时）。

一次保存做三件事，顺序不可反
----------------------------
  1. 校验：`metric_spec.static_check` —— 视图/列/模型/时间语义，错一条就不落库；
  2. 部署：`deploy_manifest` 热更新引擎（不重启容器）。**先部署再落库**：
     部署失败时库里还是旧口径，用户看到的仍是能跑的旧版本，不会出现
     「库里是新口径、引擎还是旧的」这种最难查的状态；
  3. 落库：写 `mdl_json` + 记 `config_events`。

试跑（`preview`）为什么单独一个动作
----------------------------------
口径改完最想知道的是「这句话现在能不能出数」。试跑把「命中哪条口径 + 生成什么 SQL +
返回几行」一次摊开，比回到需求页走一遍五步快得多，也不会往 `demands` 里塞测试数据。
"""
from __future__ import annotations

import json

import db
import dataset_store
import metric_spec


class MetricError(Exception):
    """口径操作失败（给界面的可读原因）。"""


def _parse_mdl(row: dict) -> dict:
    mdl = row.get("mdl_json")
    if isinstance(mdl, str):
        try:
            mdl = json.loads(mdl)
        except json.JSONDecodeError as e:
            raise MetricError("该数据集的 MDL 不是合法 JSON：%s" % str(e)[:120])
    if not isinstance(mdl, dict) or not mdl.get("models"):
        raise MetricError("该数据集没有内联 MDL，无法登记口径（先在数据源页把 MDL 登记进去）")
    return mdl


def _row(dataset: str) -> dict:
    key = (dataset or "").strip().upper()
    if not key:
        raise MetricError("dataset 不能为空")
    row = dataset_store.raw_row(key)
    if not row:
        raise MetricError("数据集「%s」未登记" % key)
    return row


def _next_version(version) -> str:
    """版本号自增：`v3` → `v4`；认不出就退化成时间戳，绝不覆盖成空串。

    ⚠️ 入参可能是整数：种子文件里写的是 `"version": 1`。以前这里直接 `.strip()`
    会抛 `'int' object has no attribute 'strip'`，而报错被兜成「校验未通过」，
    用户看到的是「按提示改完再保存」但没有任何提示——所以这里一律先转字符串。
    """
    from datetime import datetime

    v = str(version if version is not None else "").strip()
    if v.startswith("v") and v[1:].isdigit():
        return "v%d" % (int(v[1:]) + 1)
    if v.isdigit():
        return str(int(v) + 1)
    return "v" + datetime.now().strftime("%Y%m%d%H%M%S")


def _fingerprint(section: dict) -> str:
    """口径内容的指纹：用来判断「这次保存到底改没改」。

    版本号必须由服务端说了算：界面每次都会把当前版本号一起提交，
    若照抄，改十次也还是同一个版本，事后没人说得清线上是哪一版。
    但内容完全相同也没必要升版，否则版本号全是噪音。
    """
    import hashlib

    body = json.dumps({"views": [v.get("name") for v in section.get("views") or []],
                       "metrics": section.get("metrics") or []},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def load(dataset: str) -> dict:
    """读该数据集当前生效的口径。"""
    key = (dataset or "").strip().upper()
    row = _row(key)
    mdl = _parse_mdl(row)
    sec = metric_spec.section_of(mdl)
    return {
        "ok": True,
        "dataset": key,
        "version": sec.get("version") or "",
        "views": sec.get("views") or [],
        "metrics": sec.get("metrics") or [],
        "models": len(mdl.get("models") or []),
        "updated_by": row.get("updated_by") or "",
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def save(dataset: str, payload: dict, actor: str = "", dry_run: bool = False) -> dict:
    """保存口径：校验 → 热部署 → 落库。`dry_run=True` 只校验并返回差异。"""
    key = (dataset or "").strip().upper()
    row = _row(key)
    mdl = _parse_mdl(row)
    before = metric_spec.section_of(mdl)

    metrics = metric_spec.normalize(payload, key, before)

    # 视图语句兜底：界面可能只提交「名称 + 列」（视图语句属于数据侧资产），
    # 这时沿用库里同名视图的原始语句。少了这一步，保存一次就会把语句清空，
    # 而引擎那边只会报「视图没有 statement」，看不出是这次保存弄没的。
    existing_views = {v.get("name"): v for v in (mdl.get("views") or [])}
    for v in metrics["views"]:
        if not v.get("statement"):
            v["statement"] = (existing_views.get(v["name"]) or {}).get("statement") or ""

    # 版本号由服务端定：内容真变了才升版。
    # 比之前先把「库里那份」也过一遍同样的规整（补默认值、去空白），
    # 否则缺省字段（definition / caveats）会让「没改」也判成「改了」。
    before_cmp = metric_spec.normalize(before, key, before)
    unchanged = _fingerprint(metrics) == _fingerprint(before_cmp)
    metrics["version"] = (before.get("version") or "") if unchanged else _next_version(before.get("version"))

    problems = metric_spec.static_check(metrics, mdl)
    if problems:
        return {
            "ok": False,
            "code": "METRIC_INVALID",
            "problems": problems,
            "error": "口径校验未通过（%d 处）" % len(problems),
            "notice": "按下面的逐条原因改完再保存；口径是数据，写错了不会报错，只会算错。",
        }

    added = sorted({m["key"] for m in metrics["metrics"]} - {m.get("key") for m in before.get("metrics") or []})
    removed = sorted({m.get("key") for m in before.get("metrics") or []} - {m["key"] for m in metrics["metrics"]})
    out = {
        "ok": True,
        "dataset": key,
        "version": metrics["version"],
        "views": len(metrics["views"]),
        "metrics": len(metrics["metrics"]),
        "added": added,
        "removed": removed,
        "dry_run": bool(dry_run),
    }
    if dry_run:
        out["notice"] = "校验通过（未保存）：%d 视图 / %d 口径" % (len(metrics["views"]), len(metrics["metrics"]))
        return out

    new_mdl = metric_spec.build_mdl(mdl, metrics)
    try:
        _deploy(row, new_mdl)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "METRIC_DEPLOY_FAILED",
            "error": "引擎装载失败：%s: %s" % (type(e).__name__, str(e)[:240]),
            "notice": "口径没有保存——引擎没能装载新视图。检查引擎是否在跑，再重试；库里的旧口径仍然可用。",
        }
    # 引擎启动时读的是工作区文件：不写回，重启就会回滚成旧口径
    persist_warn = _persist_workspace(key, new_mdl)

    db.execute(
        "UPDATE dataset_registry SET mdl_json = %s::jsonb, updated_by = %s, updated_at = now() "
        "WHERE dataset_key = %s",
        (json.dumps(new_mdl, ensure_ascii=False), (actor or "system")[:80], key),
    )
    try:
        db.execute(
            "INSERT INTO config_events (scope, action, actor, detail) VALUES (%s, %s, %s, %s)",
            ("metric", "save", (actor or "system")[:80],
             json.dumps({"dataset": key, "version": metrics["version"],
                         "views": len(metrics["views"]), "metrics": len(metrics["metrics"]),
                         "added": added, "removed": removed}, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001  —— 审计写不进去不该让保存失败
        pass
    out["unchanged"] = unchanged
    out["persist_warning"] = persist_warn
    out["notice"] = (
        "口径没有变化，未升版（%s）；引擎已按同一份定义重新装载。" % metrics["version"]
        if unchanged else
        "已保存并生效：%d 视图 / %d 口径（%s）" % (
            len(metrics["views"]), len(metrics["metrics"]), metrics["version"])
    )
    if persist_warn:
        out["notice"] += "。⚠️ 但没写回引擎启动文件：%s —— 引擎重启后会回滚成旧口径。" % persist_warn
    return out


def _deploy(row: dict, mdl: dict) -> str:
    """热部署到该数据集自己的引擎。地址只从登记行取，不从页面来。"""
    from wren import WrenClient

    url = (row.get("wren_url") or "").strip()
    if not url:
        raise MetricError("该数据集没有引擎地址（wren_url），无法部署")
    cli = WrenClient(url, name=row.get("dataset_key") or "ds", timeout=120)
    return (cli.call_text("deploy_manifest", {"mdl": mdl}) or "").strip()[:200]


def _persist_workspace(dataset_key: str, mdl: dict) -> str:
    """把同一份 MDL 写回引擎的启动文件，返回「没写成」的原因（空串=写成）。

    为什么非写不可：引擎**启动时读的是工作区里的 mdl.json**，热部署只改了进程内存。
    实测（2026-11-01）保存成功后该文件 mtime 没变 ⇒ 引擎一重启，
    界面刚改的口径会**静默回滚**，而且没有任何报错：口径还在，只是又变回旧的。

    目录从环境变量取（`ASKODA_ENGINE_WORKSPACE_<代号>`），不在代码里写死路径。
    写不成不阻断保存（内存里已生效），但必须把原因带回给界面——
    不告诉用户「重启会回滚」，等于埋了一个定时炸弹。
    """
    import os

    d = (os.getenv("ASKODA_ENGINE_WORKSPACE_%s" % dataset_key.upper()) or "").strip()
    if not d:
        return "未配置持久化目录（ASKODA_ENGINE_WORKSPACE_%s）" % dataset_key.upper()
    if not os.path.isdir(d):
        return "持久化目录不存在：%s" % d
    path = os.path.join(d, "mdl.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mdl, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return "写不进去（%s）" % str(e)[:120]
    return ""
