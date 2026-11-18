# -*- coding: utf-8 -*-
"""多轮追问（FR-4.8 · P2）

导读第 1 节：Demo 范围为全量 60 条 FR，**P2 三条 Demo 中照常呈现**。
本模块落地 FR-4.8：「同一会话内可『再加一个维度 / 换个时间范围』而不丢失上下文」。

怎么做到「不丢失上下文」
------------------------
三处留痕共同构成会话上下文，**只增不改**，任一轮都可回放：
  1. 需求原文 —— 追问以 `【追问·第N轮】…` 段落**追加**到 `demand_requests.description`（不覆盖）；
  2. 分析轮次 —— 每轮追问重跑语义分析 = `analysis_rounds` 新增一个 `round_no`，旧轮原样保留；
  3. 结构化版本 —— 每轮追问重新合成结构化需求 = `structured_requirements` 新增 `version+1`。

于是「追问第 2 轮的产物」既能向前对齐（同 demand_id、同会话），又能与第 1 轮并排比对
（两个 round / 两个 version 都在），上下文在链路里，不靠模型记忆。

意图识别
--------
追问指令按**规则**归类（不加维度/换时间/加过滤/排序取前排/其它），归类结果只作**提示**，
真正生效的是把整句追加进需求后重跑的确定性分析——不与生成链路争判据。
"""
import re

import db

try:
    import demand as demand_mod
except Exception:  # noqa: BLE001
    demand_mod = None

try:
    import analysis as analysis_mod
except Exception:  # noqa: BLE001
    analysis_mod = None

try:
    import requirement as requirement_mod
except Exception:  # noqa: BLE001
    requirement_mod = None


# 追问意图识别（仅作界面提示，不参与生成判据）
_INTENT_RULES = [
    ("加维度", r"(再加|加一个|增加|加个)?(一个)?(维度|分组|分列)|按.{1,8}(拆分|分组|维度)|各.{1,8}(分别)"),
    ("换时间范围", r"(换|改|调整|修改).{0,3}(时间|区间|范围|周期)|(近|最近|过去)\s*\d+\s*(天|周|月|季|年)|本月|上月|上季度|年初至今|YTD"),
    ("加过滤", r"(只看|只保留|过滤|筛选|排除|剔除|限定|仅统计|仅看)"),
    ("排序取前排", r"(按.{1,8}排序|排名|排行|前\s*\d+|top\s*\d+|最高|最低|降序|升序)"),
    ("换口径", r"(口径|算法|公式|计算方式).{0,4}(改|换|调整|变为|改成)"),
]


def parse_intent(instruction):
    """把一句追问归类成有序的意图标签（可命中多条）。返回 [{kind, snippet}]。"""
    text = (instruction or "").strip()
    hits = []
    for kind, pattern in _INTENT_RULES:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            hits.append({"kind": kind, "snippet": m.group(0)})
    if not hits:
        hits.append({"kind": "其它补充", "snippet": text[:24]})
    return hits


def _turns(demand_id):
    """会话轮次：原需求为第 1 轮，其后每轮追问各成一轮（对齐 analysis_rounds）。"""
    try:
        rounds = db.query(
            "SELECT round_no, actor, created_at::text AS created_at "
            "FROM analysis_rounds WHERE demand_id=%s ORDER BY round_no",
            (demand_id,),
        )
    except Exception:  # noqa: BLE001
        rounds = []
    try:
        reqs = db.query(
            "SELECT version, source_round FROM structured_requirements "
            "WHERE demand_id=%s ORDER BY version",
            (demand_id,),
        )
    except Exception:  # noqa: BLE001
        reqs = []
    # 版本对齐按 `source_round`（该版结构化需求由哪一轮分析产出），**不按位置下标**：
    # 轮次与版本数并不总是一一对应（早期轮次可能没产出结构化需求），
    # 按下标对齐会把版本标到错误的轮次上。
    ver_by_round = {r["source_round"]: r["version"] for r in reqs if r["source_round"] is not None}
    try:
        events = db.query(
            "SELECT detail, created_at::text AS created_at FROM demand_events "
            "WHERE demand_id=%s AND event_type='followup' ORDER BY event_id",
            (demand_id,),
        )
    except Exception:  # noqa: BLE001
        events = []

    instr_by_turn = {}
    for i, ev in enumerate(events):
        detail = ev.get("detail") or {}
        if isinstance(detail, str):
            try:
                import json
                detail = json.loads(detail)
            except Exception:  # noqa: BLE001
                detail = {}
        # 优先用事件里**自己记的轮次号**（写事件时已确定），不靠事件序号倒推——
        # 序号只在「每次追问都成功落事件」时才与轮次对齐，历史数据不满足该前提。
        try:
            turn_key = int(detail.get("turn"))
        except Exception:  # noqa: BLE001
            turn_key = i + 1
        instr_by_turn[turn_key] = {
            "instruction": detail.get("instruction", ""),
            "intents": detail.get("intents", []),
        }

    turns = []
    for idx, r in enumerate(rounds):
        turn_no = idx + 1
        meta = instr_by_turn.get(turn_no) if turn_no > 1 else None
        turns.append(
            {
                "turn": turn_no,
                "kind": "原始需求" if turn_no == 1 else "多轮追问",
                "instruction": (meta or {}).get("instruction", ""),
                "intents": (meta or {}).get("intents", []),
                "round_no": r["round_no"],
                "requirement_version": ver_by_round.get(r["round_no"]),
                "created_at": r["created_at"],
            }
        )
    return turns


def conversation(demand_id, dataset="B"):
    """返回该会话的全部轮次（供「多轮追问」卡展示上下文链）。"""
    if demand_mod is None:
        return {"ok": False, "error": "demand 模块不可用"}
    try:
        demand = demand_mod.get(demand_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}
    turns = _turns(demand_id)
    return {
        "ok": True,
        "demand_id": demand_id,
        "dataset": dataset,
        "title": demand.get("title"),
        "status": demand.get("status"),
        "turn_count": len(turns),
        "turns": turns,
        "context_policy": "上下文 = 需求原文（追加不覆盖）+ 历轮分析（analysis_rounds）+ "
                          "历版结构化需求（structured_requirements），只增不改、任一轮可回放。",
    }


def followup(demand_id, instruction, dataset="B", actor="analyst"):
    """追加一轮追问：整句追加进需求 → 重跑语义分析 → 重新合成结构化需求（version+1）。

    返回新一轮的轮次号、对应的分析轮次与结构化版本，以及命中的意图标签。
    """
    text = (instruction or "").strip()
    if not text:
        return {"ok": False, "error": "追问内容不能为空"}
    if demand_mod is None or analysis_mod is None:
        return {"ok": False, "error": "analysis / demand 模块不可用"}

    try:
        demand_mod.get(demand_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])}

    before = _turns(demand_id)
    turn_no = len(before) + 1
    intents = parse_intent(text)

    # 1. 追问整句**追加**到需求描述（append=True，不覆盖原文）
    try:
        demand_mod.update(
            demand_id,
            {"description": "【追问·第%d轮】%s" % (turn_no, text)},
            actor=actor or "analyst",
            note="多轮追问（FR-4.8）：第 %d 轮" % turn_no,
            append=True,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "追加追问失败：%s" % str(e)[:200]}

    # 2. 重跑语义分析（新 round_no，旧轮保留）
    round_no = None
    try:
        bundle = analysis_mod.first_round(demand_id, dataset=dataset, actor=actor or "analyst")
        round_no = (bundle or {}).get("round_no")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "重跑分析失败：%s" % str(e)[:200]}

    # 3. 重新合成结构化需求（新 version，旧版保留）
    version = None
    if requirement_mod is not None:
        try:
            req = requirement_mod.build(demand_id, dataset=dataset)
            if isinstance(req, dict) and req.get("ok") is not False:
                version = req.get("version")
        except Exception:  # noqa: BLE001 —— 结构化失败不掩盖追问已落痕的事实
            version = None

    # 4. 事件留痕（供 _turns 复原「第几轮追问了什么」）
    try:
        demand_mod._event(
            demand_id,
            "followup",
            actor or "analyst",
            {
                "turn": turn_no,
                "instruction": text,
                "intents": intents,
                "dataset": dataset,
                "round_no": round_no,
                "requirement_version": version,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "demand_id": demand_id,
        "dataset": dataset,
        "turn": turn_no,
        "instruction": text,
        "intents": intents,
        "round_no": round_no,
        "requirement_version": version,
        "turns": _turns(demand_id),
        "context_retained": "本轮未丢弃任何上下文：原文追加、旧轮次与旧版本结构化需求均保留。",
        "next": "如本轮产物需复核，请到「人工复核」入队；需要交付请到「出数与交付」。",
    }
