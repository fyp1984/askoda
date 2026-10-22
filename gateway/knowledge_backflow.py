# -*- coding: utf-8 -*-
"""交付 → 候选知识回流（PRD ⑭ · 最小闭环）

为什么需要它
----------
业务方每拿到一条「已交付的分析 SQL + 口径说明」，本质是沉淀了一条
「这个需求该怎么取数」的资产。把它回流为**候选知识**，后续相似需求就能在
知识检索阶段直接命中，减少重复分析与口径漂移。

设计边界（最小闭环，不越界）
--------------------------
· 本模块**只产出候选知识**，不直接进正式知识库：候选文档写入一个独立的
  「候选」数据集（KNOWLEDGE_CANDIDATE_DATASET_ID），经人工复核转正前，
  正式库不被污染——避免「一条可能错的 SQL 直接污染正式知识库」这种比
  「没回流」更糟的静默失效。
· 回流是**可选开关**：环境变量 KNOWLEDGE_BACKFLOW_ENABLED=1 才触发，默认关闭。
  这样即便有 bug，也绝不影响主交付链路（execute_readonly 调用点用 try/except
  全包住，失败只记 warning 不阻断）。
· 纯构建函数 build_candidate_doc 不依赖任何外部服务，离线可测；真正入库的
  propose_candidate_knowledge 在「开关未开 / 未配置知识库」时优雅 SKIP，
  且**绝不抛异常**——调用点再兜底一层，回流的任何失败都不会反噬交付。
"""
import os
import time


def _requirement_to_text(req):
    """把结构化需求（str 或 dict）摊平成纯文本，缺字段用空串，绝不抛异常。"""
    if req is None:
        return ""
    if isinstance(req, str):
        return req.strip()
    if isinstance(req, dict):
        # 取最常见的几个需求字段拼接；都没有则退化为 JSON（保留溯源信息）。
        parts = []
        for k in ("title", "business_context", "description", "expected_output", "time_range"):
            v = req.get(k)
            if isinstance(v, str) and v.strip():
                parts.append("%s：%s" % (k, v.strip()))
        if parts:
            return "\n".join(parts)
        try:
            import json as _json
            return _json.dumps(req, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            return ""
    return str(req)


def build_candidate_doc(delivery):
    """把一次交付构建成候选知识文档（markdown 字符串，纯函数，离线可测）。

    delivery 字段（均可缺，缺则用占位符，绝不抛异常）：
      demand_id       需求单号（溯源）
      dataset         数据集（A/B）
      requirement     str 或 dict：原始结构化需求
      final_sql       最终交付 SQL
      caliber_notes   口径说明（口径 / 定义 / 注意事项）
      generator       生成器标识（如 deterministic-planner）
    """
    demand_id = str(delivery.get("demand_id") or "未知需求")
    dataset = str(delivery.get("dataset") or "未知数据集")
    requirement = _requirement_to_text(delivery.get("requirement"))
    if not requirement:
        requirement = "（未提供需求描述）"
    final_sql = str(delivery.get("final_sql") or "（未提供 SQL）")
    caliber = str(delivery.get("caliber_notes") or "（未提供口径说明）")
    generator = str(delivery.get("generator") or "unknown")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        "# 候选知识 · {demand_id}\n\n"
        "> 来源：交付回流（自动生成，待人工复核后转正）\n"
        "> 数据集：{dataset} ｜ 生成器：{generator} ｜ 回流时间：{ts}\n\n"
        "## 需求描述\n\n{requirement}\n\n"
        "## 最终交付 SQL\n\n```sql\n{final_sql}\n```\n\n"
        "## 口径说明\n\n{caliber}\n\n"
        "---\n"
        "⚠️ 本条目为候选知识，未经人工复核前**不得**直接当作正式口径引用。"
    ).format(
        demand_id=demand_id, dataset=dataset, requirement=requirement,
        final_sql=final_sql, caliber=caliber, generator=generator, ts=ts,
    )


def propose_candidate_knowledge(delivery, *, dataset_id=None, api_url=None, api_key=None):
    """把交付回流为候选知识。

    返回 dict：{ok, skipped, reason, doc?}。**绝不抛异常**。
      · 开关 KNOWLEDAGE_BACKFLOW_ENABLED 未开 ⇒ skipped（默认关闭，不影响主链路）
      · 未配置候选数据集 / 知识库 API ⇒ skipped（如实说明，不伪装成功）
      · 否则经 knowledge.upload_document 入库；异常被捕获并记 reason
    """
    if os.getenv("KNOWLEDGE_BACKFLOW_ENABLED", "").strip() not in ("1", "true", "TRUE", "on", "ON"):
        return {"ok": False, "skipped": True,
                "reason": "回流开关未开（KNOWLEDGE_BACKFLOW_ENABLED）"}
    doc = build_candidate_doc(delivery)
    api_url = api_url or os.getenv("KNOWLEDGE_API_URL")
    api_key = api_key or os.getenv("KNOWLEDGE_API_KEY")
    dataset_id = dataset_id or os.getenv("KNOWLEDGE_CANDIDATE_DATASET_ID") \
        or os.getenv("KNOWLEDGE_DATASET_ID")
    if not (api_url and api_key and dataset_id):
        return {"ok": False, "skipped": True,
                "reason": "未配置知识库 API / 候选数据集", "doc": doc}
    try:
        # 延迟导入：只有真正入库时才引 knowledge（其依赖仅为标准库，
        # 但延迟导入让本模块在主链路 import 阶段零耦合）。
        from knowledge import upload_document  # noqa: E402
        demand_id = str(delivery.get("demand_id") or "unknown")
        fname = "candidate-%s-%s.md" % (demand_id, time.strftime("%Y%m%d%H%M%S"))
        rid = upload_document(fname, doc, dataset_id=dataset_id)
        return {"ok": True, "skipped": False, "reason": "已回流为候选知识",
                "doc_id": rid, "doc": doc}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "skipped": True,
                "reason": "回流入库失败（已忽略，不影响交付）：%s: %s"
                         % (type(e).__name__, str(e)[:200]), "doc": doc}
