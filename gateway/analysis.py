# -*- coding: utf-8 -*-
"""语义分析与确认闭环的编排与持久化（M3）

分层
----
    evidence.py   取证据、排优先级、找冲突        （不碰库）
    semantics.py  五个 Skill，纯函数              （不碰库）
    analysis.py   编排 + 落库 + 状态流转           ← 本文件
    app.py        MCP 工具面

为什么单独一层
--------------
因为「确认记录版本化、不可覆盖」是 M3 的验收硬指标。把写入逻辑集中在一处，
才能保证没有任何路径会 UPDATE 掉一条历史答复——散在各工具里迟早会漏。

三条不可动摇的写入纪律
----------------------
1. `analysis_rounds` **只增不改**：重跑分析 = 新 round_no。
2. `confirmations` **只增不改**：再答一次 = version+1 + supersedes 指向旧版。
3. 问题文本变了（重跑分析导致同一 question_id 的问法变化）时，**旧答复不再当数**：
   插入一版 answer=NULL 的新行，并在事件表记 `confirmation_invalidated`。
   旧答复仍留在历史里可查——它不是被删了，只是不再是当前版本。
"""
import datetime as dt

import db
import demand as demand_mod
import evidence
import knowledge
import semantics


class AnalysisError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 组装（取证 → 分析）
# ---------------------------------------------------------------------------
def build(demand_id, dataset="B"):
    """取需求（**掩码版**）→ 证据编排 → 第一轮分析。返回 (demand, bundle, result)。

    刻意用掩码版需求做分析：分析链路不需要原文，把原文留在库里、只在受控取原文时
    才展开，是入口侧脱敏设计的延伸。手机号被掩掉不影响"手机号"这个词本身出现，
    所以 R7 的判定照常生效。
    """
    try:
        d = demand_mod.get(demand_id)  # 默认掩码版
    except demand_mod.DemandError as e:
        raise AnalysisError(str(e))
    lex = evidence.build_lexicon(dataset)
    bundle = evidence.collect(
        demand=d, demand_id=demand_id, dataset=dataset, lexicon=lex, knowledge_mod=knowledge
    )
    result = semantics.first_round(bundle, region_label=dataset)
    return d, bundle, result


def slim_evidence(items, limit=240):
    """压缩证据链供对外返回：保留定位与判断所需信息，正文截断。"""
    out = []
    for it in items:
        c = it.get("content") or ""
        out.append({**it, "content": c if len(c) <= limit else c[:limit] + "…"})
    return out


# ---------------------------------------------------------------------------
# 轮次号
# ---------------------------------------------------------------------------
def _next_round(demand_id):
    row = db.query_one(
        "SELECT coalesce(max(round_no), 0) AS n FROM analysis_rounds WHERE demand_id=%s",
        (demand_id,),
    )
    return int(row["n"]) + 1


def rounds(demand_id):
    """列出该需求的全部分析轮次（只给元信息，不给完整证据链）。"""
    rows = db.query(
        "SELECT round_no, dataset, engine, actor, created_at::text AS created_at, "
        "       jsonb_array_length(evidence) AS evidence_count, "
        "       jsonb_array_length(questions) AS question_count, "
        "       (slots->'subject'->'candidates'->0->>'value') AS top_subject, "
        "       (slots->'risks'->'candidates') AS risks, "
        "       jsonb_array_length(coalesce(summary->'degraded_sources','[]'::jsonb)) "
        "           AS degraded_count, "
        "       (summary->>'confidence_overall')::numeric AS confidence_overall "
        "FROM analysis_rounds WHERE demand_id=%s ORDER BY round_no",
        (demand_id,),
    )
    for r in rows:
        risks = r.pop("risks") or []
        r["blocking_count"] = len([x for x in risks if x.get("severity") == "blocking"])
        # numeric 在 psycopg 里是 Decimal，直接回给 MCP 序列化会炸，这里转 float
        if r.get("confidence_overall") is not None:
            r["confidence_overall"] = float(r["confidence_overall"])
    return {"demand_id": demand_id, "count": len(rows), "rounds": rows}


def get_round(demand_id, round_no=None):
    """取某一轮完整结果（默认最新一轮）。"""
    if round_no is None:
        row = db.query_one(
            "SELECT max(round_no) AS n FROM analysis_rounds WHERE demand_id=%s",
            (demand_id,),
        )
        if not row or row["n"] is None:
            raise AnalysisError("该需求还没有分析轮次：%s" % demand_id)
        round_no = int(row["n"])
    r = db.query_one(
        "SELECT * FROM analysis_rounds WHERE demand_id=%s AND round_no=%s",
        (demand_id, round_no),
    )
    if not r:
        raise AnalysisError("轮次不存在：%s round=%s" % (demand_id, round_no))
    return {
        "demand_id": r["demand_id"],
        "round_no": r["round_no"],
        "dataset": r["dataset"],
        "engine": r["engine"],
        "actor": r["actor"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "round_meta": r["summary"],
        "slots": r["slots"],
        "evidence_chain": r["evidence"],
        "rule_check": r["risks"],
        "questions": r["questions"],
    }


def evidence_chain(demand_id, round_no=None, level=None):
    """取某一轮的证据链，可按优先级过滤。"""
    g = get_round(demand_id, round_no)
    items = g["evidence_chain"]
    if level:
        items = [i for i in items if i["level"] == level]
    return {
        "demand_id": demand_id,
        "round_no": g["round_no"],
        "level_filter": level,
        "count": len(items),
        "by_level": _count_by_level(g["evidence_chain"]),
        "items": items,
    }


def _count_by_level(items):
    out = {}
    for i in items:
        out[i["level"]] = out.get(i["level"], 0) + 1
    return out


# ---------------------------------------------------------------------------
# 落库：轮次
# ---------------------------------------------------------------------------
def _save_round(demand_id, dataset, bundle, result, actor="analyst"):
    round_no = _next_round(demand_id)
    # summary 列承载「轮次级元信息」。证据源降级必须随轮次一起留存，否则事后
    # 回看这一轮时会以为当时证据是齐的——"当时到底缺什么"和结论同等重要。
    meta = {
        "confidence_overall": result.get("confidence_overall"),
        "confidence_cap": result.get("confidence_cap"),
        "degraded_sources": result.get("degraded_sources") or [],
        "evidence_sources": bundle.get("sources"),
    }
    db.execute(
        """
        INSERT INTO analysis_rounds
            (demand_id, round_no, dataset, summary, slots, evidence, risks, questions, engine, actor)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            demand_id,
            round_no,
            dataset,
            db.dumps(meta),
            db.dumps(result["slots"]),
            db.dumps(slim_evidence(bundle["items"], 400)),
            db.dumps(result["rule_check"]),
            db.dumps(result["questions"]),
            "deterministic+evidence",
            actor,
        ),
    )
    return round_no


# ---------------------------------------------------------------------------
# 落库：确认问题（版本化、不可覆盖）
# ---------------------------------------------------------------------------
def _current_confirmation(demand_id, question_id):
    return db.query_one(
        "SELECT * FROM confirmations WHERE demand_id=%s AND question_id=%s "
        "ORDER BY version DESC LIMIT 1",
        (demand_id, question_id),
    )


def _new_cid():
    import random
    import string

    return "CF-%s-%s" % (
        dt.datetime.now().strftime("%Y%m%d%H%M%S"),
        "".join(random.choices(string.ascii_uppercase + string.digits, k=4)),
    )


def _upsert_question(demand_id, round_no, q, actor="system"):
    """把一个问题落成"当前版本"。返回 (动作, 记录)。

    动作 ∈ {created, unchanged, revised, invalidated}
    """
    cur = _current_confirmation(demand_id, q["question_id"])
    if cur is None:
        cid = _new_cid()
        db.execute(
            """
            INSERT INTO confirmations
                (confirmation_id, demand_id, round_no, question_id, slot, question,
                 reason, options, impact_scope, version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1)
            """,
            (
                cid, demand_id, round_no, q["question_id"], q["slot"], q["question"],
                q.get("reason"), db.dumps(q.get("options") or []), q.get("impact_scope"),
            ),
        )
        return "created", cur

    if cur["question"] == q["question"] and cur["slot"] == q["slot"]:
        return "unchanged", cur

    # 问法变了：旧答复不再当数，插一版未答复的新行，旧行保留在历史里
    cid = _new_cid()
    had_answer = bool(cur["answer"])
    db.execute(
        """
        INSERT INTO confirmations
            (confirmation_id, demand_id, round_no, question_id, slot, question,
             reason, options, impact_scope, version, supersedes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            cid, demand_id, round_no, q["question_id"], q["slot"], q["question"],
            q.get("reason"), db.dumps(q.get("options") or []), q.get("impact_scope"),
            int(cur["version"]) + 1, cur["confirmation_id"],
        ),
    )
    if had_answer:
        db.execute(
            "INSERT INTO demand_events (demand_id, event_type, actor, detail) VALUES (%s,%s,%s,%s)",
            (
                demand_id,
                "confirmation_invalidated",
                actor,
                db.dumps(
                    {
                        "question_id": q["question_id"],
                        "superseded_version": int(cur["version"]),
                        "reason": "问题文本变更，原答复不再作为当前结论",
                        "previous_answer": cur["answer"],
                    }
                ),
            ),
        )
    return ("revised" if not had_answer else "invalidated"), cur


def generate_confirmations(demand_id, dataset="B", actor="analyst"):
    """仅重新生成并落库确认问题（不新增分析轮次）。

    与 `first_round` 的分工：`first_round` 是"跑一轮分析"，本函数是"只把这轮的
    待确认问题刷新一遍"——当知识库或元数据更新后想重新提问，用它就不必再堆轮次。
    """
    d, bundle, result = build(demand_id, dataset)
    # 确认问题必须挂在某一轮分析上。还没跑过分析就刷新问题，会得到 round_no=0 的
    # 孤儿记录——追问「这是哪一轮提的问题」时无从回答，回退确认也无从比对。
    rno = _latest_round_no(demand_id)
    if rno <= 0:
        raise AnalysisError(
            "该需求还没有分析轮次，请先跑 analysis_first_round：%s" % demand_id
        )
    actions = {}
    for q in result["questions"]:
        act, _ = _upsert_question(demand_id, rno, q, actor)
        actions[act] = actions.get(act, 0) + 1
    return {
        "demand_id": demand_id,
        "generated": len(result["questions"]),
        "actions": actions,
        "dropped_by_guard": result["dropped_questions"],
        "open": list_confirmations(demand_id),
    }


def _latest_round_no(demand_id):
    row = db.query_one(
        "SELECT coalesce(max(round_no), 0) AS n FROM analysis_rounds WHERE demand_id=%s",
        (demand_id,),
    )
    return int(row["n"])


def list_confirmations(demand_id, include_history=False):
    """列确认问题。默认只给**当前版本**；`include_history=True` 连历史版本一起给。"""
    rows = db.query(
        """
        SELECT * FROM confirmations
        WHERE demand_id = %s
        ORDER BY question_id, version
        """,
        (demand_id,),
    )
    latest = {}
    for r in rows:
        latest[r["question_id"]] = r

    def fmt(r):
        return {
            "confirmation_id": r["confirmation_id"],
            "question_id": r["question_id"],
            "slot": r["slot"],
            "question": r["question"],
            "reason": r["reason"],
            "options": r["options"],
            "impact_scope": r["impact_scope"],
            "version": r["version"],
            "supersedes": r["supersedes"],
            "answer": r["answer"],
            "answered_by": r["answered_by"],
            "answered_at": r["answered_at"].isoformat() if r["answered_at"] else None,
            "answered": bool(r["answer"]),
            "round_no": r["round_no"],
        }

    cur = [fmt(latest[k]) for k in sorted(latest)]
    out = {
        "demand_id": demand_id,
        "open_count": len([c for c in cur if not c["answered"]]),
        "answered_count": len([c for c in cur if c["answered"]]),
        "items": cur,
    }
    if include_history:
        out["history"] = [fmt(r) for r in rows]
        out["history_count"] = len(rows)
    return out


def answer(demand_id, question_id, answer_text, actor="business", choice=None):
    """答复一条确认问题。**不覆盖历史**：插入新版本并指向被取代的那版。"""
    cur = _current_confirmation(demand_id, question_id)
    if cur is None:
        raise AnalysisError("该需求下没有这个问题：%s" % question_id)
    text = (answer_text or "").strip()
    choice = (choice or "").strip()
    if choice:
        # 选项与自由文本可以并存，但**不能自我重复**：若选项本身已出现在文本里
        # （业务点了选项又照抄了一遍），再拼一次会得到"按业务单据逐条（选项：按业务
        # 单据逐条）"这种字符串。危害不只是难看——下面的幂等判断按文本比对，
        # 文本被改了就会把"同一答复再答一次"误判成新版本，平白多出一版历史。
        if text and choice in text:
            pass
        elif text:
            text = "%s（选项：%s）" % (text, choice)
        else:
            text = choice
    if not text:
        raise AnalysisError("答复不能为空")

    if cur["answer"] and cur["answer"] == text:
        # 同一答复再答一次：不动版本（这是"不堆版本"的本意），但**必须留痕**。
        # 一次用户动作在审计链上完全不留痕迹，是"全程留痕"的破口——事后问
        # "业务到底确认过几次、什么时候确认的"就答不上来。所以记一条
        # `confirmation_reaffirmed` 与真正的变更区分开，既不污染版本号，也不丢动作。
        db.execute(
            "INSERT INTO demand_events (demand_id, event_type, actor, detail) VALUES (%s,%s,%s,%s)",
            (
                demand_id,
                "confirmation_reaffirmed",
                actor,
                db.dumps(
                    {
                        "question_id": question_id,
                        "version": int(cur["version"]),
                        "answer": text,
                        "note": "答复与当前版本一致，未新增版本",
                    }
                ),
            ),
        )
        return {
            "ok": True,
            "noop": True,
            "note": "答复与当前版本一致，未新增版本（已留痕）",
            "confirmation": list_confirmations(demand_id),
        }

    cid = _new_cid()
    db.execute(
        """
        INSERT INTO confirmations
            (confirmation_id, demand_id, round_no, question_id, slot, question, reason,
             options, impact_scope, answer, answered_by, answered_at, version, supersedes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)
        """,
        (
            cid, demand_id, cur["round_no"], question_id, cur["slot"], cur["question"],
            cur["reason"], db.dumps(cur["options"] or []), cur["impact_scope"],
            text, actor, int(cur["version"]) + 1, cur["confirmation_id"],
        ),
    )
    db.execute(
        "INSERT INTO demand_events (demand_id, event_type, actor, detail) VALUES (%s,%s,%s,%s)",
        (
            demand_id,
            "confirmation_answered",
            actor,
            db.dumps(
                {
                    "question_id": question_id,
                    "version": int(cur["version"]) + 1,
                    "supersedes": cur["confirmation_id"],
                    "answer": text,
                }
            ),
        ),
    )
    # 业务已答复 → 进入「待补充修改」，等分析人员据答复重新整理
    _advance_status(demand_id, "待补充修改", actor, "业务已答复确认问题")

    return {
        "ok": True,
        "noop": False,
        "confirmation_id": cid,
        "version": int(cur["version"]) + 1,
        "supersedes": cur["confirmation_id"],
        "answer": text,
        "demand_status": demand_mod.get(demand_id)["status"],
    }


# ---------------------------------------------------------------------------
# 状态流转
# ---------------------------------------------------------------------------
def _advance_status(demand_id, status, actor, note):
    try:
        cur = demand_mod.get(demand_id)["status"]
    except Exception:  # noqa: BLE001
        return None
    # 已通过的单子不被分析动作反向拉回（PRD §9.3 业务规则 3：新增事实应"重新打开"
    # 而不是静默改状态）。要改由人显式调用 demand_set_status。
    if cur == "已通过" and status != "已通过":
        return cur
    if cur == status:
        return cur
    demand_mod.set_status(demand_id, status, actor=actor, note=note)
    return status


# ---------------------------------------------------------------------------
# 对外主入口
# ---------------------------------------------------------------------------
def first_round(demand_id, dataset="B", actor="analyst", persist=True):
    """跑第一轮语义分析：取证 → 分析 → 落库轮次 → 生成确认问题 → 状态流转。"""
    d, bundle, result = build(demand_id, dataset)

    slots_backfilled, slots_affected = _backfill_slots_from_confirmations(
        demand_id, result["slots"]
    )

    round_no = _save_round(demand_id, dataset, bundle, result, actor) if persist else None

    actions = {}
    if persist:
        for q in result["questions"]:
            act, _ = _upsert_question(demand_id, round_no, q, actor)
            actions[act] = actions.get(act, 0) + 1
        # 有问题待答 → 待业务确认；问题都被答过 → 待审核通过
        openq = list_confirmations(demand_id)["open_count"]
        target = "待业务确认" if openq else "待审核通过"
        _advance_status(demand_id, "分析中", actor, "开始第一轮语义分析")
        _advance_status(demand_id, target, actor,
                        "生成 %d 个待确认问题" % openq if openq else "无需业务确认，待审核")

    return {
        "ok": True,
        "demand_id": demand_id,
        "round_no": round_no,
        "dataset": dataset,
        "summary": semantics.summary(d),
        "slots": result["slots"],
        "slot_labels": result["slot_labels"],
        "rule_check": result["rule_check"],
        "questions": result["questions"],
        "dropped_questions": result["dropped_questions"],
        "confidence_overall": result["confidence_overall"],
        "confidence_cap": result.get("confidence_cap"),
        "degraded_sources": result.get("degraded_sources") or [],
        "evidence_chain": slim_evidence(bundle["items"]),
        "evidence_status": bundle["status"],
        "evidence_ladder": bundle["ladder"],
        "evidence_sources": bundle["sources"],
        "conflicts": evidence.detect_conflicts(_claims_from_slots(result["slots"])),
        "confirmation_actions": actions,
        "demand_status": demand_mod.get(demand_id)["status"],
        "slots_backfilled": slots_backfilled,
        "slots_backfilled_count": len(slots_backfilled),
        "slots_affected": slots_affected,
    }


def _backfill_slots_from_confirmations(demand_id, slots):
    """把 confirmations 里**当前版本已答复**的内容合并进对应 slot。

    设计原则
    --------
    1. confirmations 表**只增不改**：这里只读 latest，不写版本链。
    2. 回填 candidate 的 level/source 显式标记为「P2 业务确认」，和首跑证据区分开。
    3. 只对已 answered=True 的最新版生效；answer=NULL 的失效答复不回填。
    4. 幂等：对同一 question_id 重复调用不重复堆 candidate。
    """
    import json as _json
    if not slots:
        return [], []
    conf = list_confirmations(demand_id, include_history=False)
    answered_items = [it for it in conf.get("items") or [] if it.get("answered")]
    backfilled_ids = []
    affected_slots = set()
    for it in answered_items:
        qid = it.get("question_id")
        slot_name = it.get("slot")
        answer_val = it.get("answer")
        version = it.get("version")
        if not qid or not slot_name or not answer_val:
            continue
        slot = slots.get(slot_name)
        if slot is None:
            continue
        cands = slot.setdefault("candidates", [])
        # qid 本身已是 Q- 开头（如 Q-TIME-1），不要再拼一个 Q
        dedup_key = "P2_CONFIRM_%s" % qid
        already = any(
            (c.get("source") or "").startswith("P2 业务确认")
            and (c.get("confirmation_dedup") or "") == dedup_key
            for c in cands
        )
        if already:
            continue
        cite_ev = [
            {
                "level": "P2",
                "level_name": "业务确认答复",
                "source": "confirmations",
                "locator": "question_id=%s v%s" % (qid, version),
            }
        ]
        cands.insert(
            0,
            {
                "slot": slot_name,
                "value": answer_val,
                "confidence": 1.0,
                "needs_confirmation": False,
                "level": "confirmed",
                "source": "P2 业务确认 · %s v%s" % (qid, version),
                "confirmation_dedup": dedup_key,
                "question_id": qid,
                "version": version,
                "evidence": cite_ev,
                "matched_terms": [answer_val[:32]],
                "hit_by": ["business_confirmation"],
            },
        )
        slot["needs_confirmation"] = False
        existing_ids = slot.get("filled_by_confirmation_ids") or []
        if qid not in existing_ids:
            existing_ids.append(qid)
        slot["filled_by_confirmation_ids"] = existing_ids
        backfilled_ids.append(qid)
        affected_slots.add(slot_name)
    return backfilled_ids, sorted(affected_slots)


def _claims_from_slots(slots):
    """把槽位结论摊成 (slot, value, level) 断言，供冲突检测使用。"""
    claims = []
    for name, s in (slots or {}).items():
        for c in (s.get("candidates") or []):
            for e in (c.get("evidence") or []):
                claims.append(
                    {
                        "slot": name,
                        "value": c.get("value"),
                        "level": e.get("level"),
                        "source": e.get("source"),
                        "locator": e.get("locator"),
                        # 同级消解需要的次级信号：没有它们就只能任意取首个（见 detect_conflicts）
                        "confidence": c.get("confidence"),
                        "strong_hits": c.get("strong_hits"),
                        "hit_count": c.get("hit_count"),
                        "discriminative_score": c.get("discriminative_score"),
                        "matched_terms": c.get("matched_terms"),
                        "ambiguous": c.get("ambiguous"),
                    }
                )
    return claims
