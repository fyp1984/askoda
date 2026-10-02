# -*- coding: utf-8 -*-
"""结构化技术需求对象（M4-2 · PRD §9.4 / SQL生成链路设计 §4.4）

设计动机
--------
M3 语义分析输出的六槽位 + 已确认答复，散落在 analysis_rounds / confirmations 两张表里，
下游（sql_context_pack / sqlgen）如果各自去查表、拼字段，会各自拼出不一样的口径——
口径漂移的根因不是"模型算错了"，而是"不同环节对同一需求各自理解了一遍"。

本模块的作用是**把上游（M3）的结论收成一份、且只收一份**：经契约校验（validate）
通过后的对象是 SQL 生成链路的唯一上游输入。任何关于"需求的结构化长什么样"的争议，
都以 payload 列里的这份 JSON 为准——这就是"单一事实源（SSOT）"的落地。

为什么不用现成的 JSON Schema 校验库
-----------------------------------
硬约束只允许引入 sqlglot 一个新依赖。jsonschema 库不在白名单里，所以手写校验器
（validator）。一期的契约字段只有 15 个，手写的成本远低于"再加一个依赖并维护其
版本与 import 链"。而且手写校验器能把"哪个字段、为什么错"写得更细——例如
"无源结论（evidence 为空数组）"和"缺 evidence 键"是两种不同错误，JSON Schema
只会报"不符合 minItems=1"。

槽位键名的两套口径对齐（必须在此映射，不可绕开）
--------------------------------------------------
设计文档 §4.2 的六字段是：subject / granularity / time_semantics / data_scope /
output_fields / aggregation_rules。

M3 实际落库的槽位键（semantics.SLOT_KEYS）是：
    ["subject", "granularity", "time", "scope", "fields", "risks"]

两套名字**不一致**——直接写 slots['time_semantics'] 会 KeyError。SLOT_MAP 是
本模块内的唯一映射点：所有从 M3 slots 取值的代码，必须经过 SLOT_MAP[obj_field]
取得真实键名，而不是把设计文档那套键名硬编码到取值处。
"""
import copy

import db
import analysis as analysis_mod
import schema_scan as schema_scan_mod

# ---------------------------------------------------------------------------
# 对象字段 ← M3 slots 真实键名（对齐 SLOT_KEYS 的唯一映射点）
# ---------------------------------------------------------------------------
# 键：结构化对象里的字段名（设计文档口径）
# 值：slots 字典里的真实键（semantics.SLOT_KEYS 口径）
SLOT_MAP = {
    "subject": "subject",
    "granularity": "granularity",
    "time_semantics": "time",        # 设计文档写作 time_semantics，M3 真实键是 time
    "data_scope": "scope",           # 设计文档写作 data_scope，M3 真实键是 scope
    "output_fields": "fields",       # 设计文档写作 output_fields，M3 真实键是 fields
    "residual_risks": "risks",       # 设计文档没给名，risks 槽位 = 残余风险集合
}

# 对象中"取值 = 单条 SlotClaim（取 candidates[0]）"的字段
SINGLE_SLOT_FIELDS = ("subject", "granularity", "time_semantics", "data_scope")

# 对象中"取值 = 全量 candidates（SlotClaim 数组）"的字段
ARRAY_SLOT_FIELDS = ("output_fields", "residual_risks")

# 合法 status 枚举（契约 status.enum）
VALID_STATUS = ("待审核", "已通过", "已退回")

# ---------------------------------------------------------------------------
# 聚合关键词派生（aggregation_rules · M3 无此槽位，从输出字段派生）
# ---------------------------------------------------------------------------
# 与 semantics.AGG_KEYWORDS 保持同源语义：output_fields[i].value 里命中这些关键词，
# 则认为存在相应聚合意图。派不出给 []，绝不编造。
#
# 为什么不直接 import semantics.AGG_KEYWORDS
# --------------------------------------------
# 一是降低耦合：语义层调关键词的用途是"识别聚合意图并提确认问题"，本层的用途是
# "把已明确出现在输出字段名里的聚合词沉淀为规则"——两者字面相同但职责不同；
# 二是避免循环 import 风险（semantics 里 import evidence，evidence 里 import db）。
AGG_KEYWORDS_RULES = [
    ("汇总求和", ("合计", "总计", "总和", "求和", "汇总", "累计")),
    ("计数", ("数量", "个数", "笔数", "单量", "人数", "次数", "多少家")),
    ("平均", ("平均", "均值", "人均", "店均", "日均")),
    ("比率", ("率", "占比", "比例", "百分比")),
    ("排名", ("排名", "排行", "TOP", "top", "前十", "前10", "最高", "最低")),
    ("趋势对比", ("趋势", "走势", "变化", "对比", "相比", "比较", "增减")),
    ("分组", ("按", "分", "各", "每个", "各个", "分别", "逐")),
]

# 契约顶层必填字段清单（对齐 JSON Schema 的 required 数组）
REQUIRED_TOP = (
    "demand_id", "dataset", "version", "source_round", "schema_version",
    "subject", "granularity", "time_semantics", "data_scope",
    "output_fields", "aggregation_rules", "confirmed_facts",
    "evidence_chain", "residual_risks", "status",
)


# ---------------------------------------------------------------------------
# 辅助：取 M3 槽位里的 candidates，走 SLOT_MAP（绝不直取）
# ---------------------------------------------------------------------------
def _slot_candidates(slots, obj_field):
    """按对象字段名，经 SLOT_MAP 转真实键，拿到 slots[real_key]['candidates']。

    不存在的键 / candidates 非数组 → 统一退成 []，不抛异常。
    统一退空是为了让上层不用一堆 try/except 保护："槽位不存在"和"槽位存在但空"
    对下游效果一样——都判成"没取到"，然后走 residual_risks 记录。
    """
    real_key = SLOT_MAP.get(obj_field)
    if real_key is None:
        return []
    s = slots.get(real_key) or {}
    c = s.get("candidates")
    if not isinstance(c, list):
        return []
    return c


def _claim_from_candidate(cand):
    """把 M3 的 candidate（带 slot/needs_confirmation 等多余键）压成契约 SlotClaim。

    契约只要求 value/evidence/confidence 三键；其他字段作为额外属性保留
    （SlotClaim 的 additionalProperties=true，多塞字段不违规）。
    **绝不重新构造 evidence/confidence**：原样透出，保证口径可回溯到 M3 分析时的值。
    """
    return {
        "value": cand.get("value"),
        "evidence": cand.get("evidence") or [],
        "confidence": cand.get("confidence", 0.0),
    }


def _derive_aggregation(output_fields):
    """从输出字段 value 里命中聚合关键词派生 aggregation_rules。

    M3 没有"聚合规则"槽位——但"合计销售额/平均客单价/各门店复购率"这类话里，
    聚合词是直接写在输出字段里的。这里只是把字面出现过的词沉淀下来，供下游参考。
    若完全命中不到任何关键词 → 返回 []，绝不编造"默认求和"这种兜底。
    """
    rules = []
    seen = set()
    for f in output_fields or []:
        val = "" if f is None else str(f.get("value") or "")
        for rule, words in AGG_KEYWORDS_RULES:
            for w in words:
                if w and w in val:
                    key = (rule, f.get("value"), w)
                    if key in seen:
                        continue
                    seen.add(key)
                    rules.append({
                        "rule": rule,
                        "field": f.get("value"),
                        "matched_keyword": w,
                    })
    return rules


def _get_schema_version(dataset):
    """取 schema_version：先查最近快照；没有则扫一次再查；仍无 → 返回 None。

    为什么 schema_scan.scan() 失败不兜底
    ------------------------------------
    schema_version 是 SQL 生成链路的溯源锚点——"没有就给个默认值"会让下游 SQL 在
    结构变更后无法精确定位"是哪版 schema 写的这条 SQL"，复现口径就崩了。
    所以拿不到必须抛错返回给调用方，不能装成有值。
    """
    lv = schema_scan_mod.latest_version(dataset)
    if lv is not None and lv.get("schema_version"):
        return lv["schema_version"]
    try:
        schema_scan_mod.scan(dataset, persist=True)
    except Exception:
        # scan 内部会写库，失败了不掩盖——latest_version 会仍为 None，下一段统一报
        pass
    lv2 = schema_scan_mod.latest_version(dataset)
    if lv2 is not None and lv2.get("schema_version"):
        return lv2["schema_version"]
    return None


def _next_version(demand_id):
    """同 demand_id 已存最大 version + 1。首次 = 1。**只增不改**：旧版永不覆盖。"""
    row = db.query_one(
        "SELECT coalesce(max(version), 0) AS n FROM structured_requirements WHERE demand_id=%s",
        (demand_id,),
    )
    return int(row["n"]) + 1


# ---------------------------------------------------------------------------
# build · 合成结构化技术需求对象并落库
# ---------------------------------------------------------------------------
def build(demand_id, dataset="B", round_no=None):
    """合成「结构化技术需求对象」。

    成功：返回对象本身（**顶层不得有 "ok" 键**——否则契约校验会把 ok 当成
    额外字段判 additionalProperties=false 违规）。
    失败：返回 {"ok": False, "error": "..."}（接线层按 ok is False 判断并短路）。
    """
    try:
        round_result = analysis_mod.get_round(demand_id, round_no)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "取不到分析轮次：%s（请先执行 analysis_first_round）" % str(e)[:200],
        }

    real_round_no = round_result["round_no"]
    slots = round_result.get("slots") or {}
    evidence_chain = round_result.get("evidence_chain") or []

    # 1. 四枚单值槽位 + 记录"取不到则写入 residual_risks"
    built_slots = {}
    extra_risks = []
    for obj_field in SINGLE_SLOT_FIELDS:
        cands = _slot_candidates(slots, obj_field)
        if cands:
            built_slots[obj_field] = _claim_from_candidate(cands[0])
        else:
            # 槽位真取不到：放一个「缺失标记」占位 SlotClaim，并进 residual_risks。
            #
            # 为什么占位也必须带非空 evidence（不能留空数组）
            # -------------------------------------------------
            # 契约里 SlotClaim.evidence 有 minItems=1（空数组 = 无源断言违规，对应硬判据 H3）。
            # 若占位写 evidence=[]，build 产出的对象会被本模块自己的 validate() 判为不通过——
            # 即"自己产的对象过不了自己的契约"。所以占位必须带**非空且真实**的来源。
            #
            # 来源是什么：不是把缺失伪装成结论，而是"本轮分析对该槽位确实没有产出候选"这一
            # **可回溯的事实**——locator 指向 analysis_rounds 的轮次号与槽位键，可复核。
            # 占位 value 明写"无候选"、confidence=0，且另立一条 blocking 风险，三处互相印证。
            absent_ev = [{
                "level": "P8",
                "level_name": "通用规则",
                "rank": 7,
                "source": "analysis_rounds.slots",
                "locator": "demand_id=%s round_no=%s slot=%s candidates=[]"
                           % (demand_id, real_round_no, SLOT_MAP[obj_field]),
                "content": "缺失标记（非业务结论）：本轮语义分析对槽位「%s」未产出候选，"
                           "依契约以占位项明示缺失，不作为结论参与下游取数。"
                           % SLOT_MAP[obj_field],
            }]
            built_slots[obj_field] = {
                "value": "无候选结论（该槽位在本轮分析中为空）",
                "evidence": absent_ev,
                "confidence": 0.0,
            }
            extra_risks.append({
                "value": "结构化字段「%s」在本轮分析中无候选结论（M3 slots['%s'].candidates 为空）"
                         % (obj_field, SLOT_MAP[obj_field]),
                "evidence": absent_ev,
                "confidence": 0.0,
                "severity": "blocking",
                "missing_field": obj_field,
            })

    # 2. output_fields：数组，全量 candidates 保留
    field_cands = _slot_candidates(slots, "output_fields")
    output_fields = [_claim_from_candidate(c) for c in field_cands]

    # 2'. output_fields 槽位缺失同样适用"该槽位留空并进 residual_risks"的规则。
    #     与四个单值槽位不同，数组型字段不会触发无源断言（空数组是合法的），
    #     所以很容易被漏记——但"没有输出字段"意味着下游 SELECT 无可选列，属阻断级
    #     缺失，必须留痕（否则 A 库这类裸库跑出来只剩空数组、无从判断空的原因）。
    if not output_fields:
        extra_risks.append({
            "value": "结构化字段「output_fields」在本轮分析中无候选结论"
                     "（M3 slots['fields'].candidates 为空）——下游无可选输出列",
            "evidence": [{
                "level": "P8",
                "level_name": "通用规则",
                "rank": 7,
                "source": "analysis_rounds.slots",
                "locator": "demand_id=%s round_no=%s slot=fields candidates=[]"
                           % (demand_id, real_round_no),
                "content": "缺失标记（非业务结论）：本轮语义分析未产出输出字段候选，"
                           "依契约以空数组呈现并在此留痕，不作为结论参与下游取数。",
            }],
            "confidence": 0.0,
            "severity": "blocking",
            "missing_field": "output_fields",
        })

    # 3. residual_risks：原 risks 槽位全量 + 上面"槽位取不到"追加项
    risk_cands = _slot_candidates(slots, "residual_risks")
    residual_risks = [_claim_from_candidate(c) for c in risk_cands] + extra_risks

    # 4. aggregation_rules：从 output_fields 的 value 关键词派生
    aggregation_rules = _derive_aggregation(output_fields)

    # 5. 已确认事实：只取 answered=True 的当前版本
    try:
        conf_res = analysis_mod.list_confirmations(demand_id, include_history=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "取不到已确认答复：%s" % str(e)[:200]}
    confirmed_facts = []
    for item in (conf_res.get("items") or []):
        if not item.get("answered"):
            continue
        confirmed_facts.append({
            "question_id": item.get("question_id"),
            "slot": item.get("slot"),
            "question": item.get("question"),
            "answer": item.get("answer"),
            "version": item.get("version"),
            "answered_by": item.get("answered_by"),
            "answered_at": item.get("answered_at"),
        })

    # 6. schema_version：查 / 再扫 / 仍无 → 报错
    sv = _get_schema_version(dataset)
    if not sv:
        return {
            "ok": False,
            "error": "数据集 %s 尚无 schema 快照，请先执行 schema_scan" % dataset,
        }

    # 7. version：同 demand_id 最大 version + 1
    version = _next_version(demand_id)

    # 8. 组装对象（顶层不得出现 ok 键）
    obj = {
        "demand_id": demand_id,
        "dataset": dataset,
        "version": version,
        "source_round": int(real_round_no),
        "schema_version": sv,
        "subject": built_slots["subject"],
        "granularity": built_slots["granularity"],
        "time_semantics": built_slots["time_semantics"],
        "data_scope": built_slots["data_scope"],
        "output_fields": output_fields,
        "aggregation_rules": aggregation_rules,
        "confirmed_facts": confirmed_facts,
        "evidence_chain": copy.deepcopy(evidence_chain),
        "residual_risks": residual_risks,
        "status": "待审核",
    }

    # 9. 落库 structured_requirements（payload 存整对象，方便回放/对比）
    try:
        db.execute(
            """
            INSERT INTO structured_requirements
                (demand_id, version, dataset, schema_version, source_round, payload, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                demand_id, version, dataset, sv, int(real_round_no),
                db.dumps(obj), "待审核",
            ),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "落库 structured_requirements 失败：%s" % str(e)[:240]}

    return obj


# ---------------------------------------------------------------------------
# validate · 手写契约校验（不引入 jsonschema 依赖）
# ---------------------------------------------------------------------------
def _err(path, message):
    return {"path": path, "message": message}


def _check_slot_claim(obj, path, out):
    """按 SlotClaim 契约校验一个值。发现错误直接 append 到 out。"""
    if not isinstance(obj, dict):
        out.append(_err(path, "必须是对象（dict），实际类型 %s" % type(obj).__name__))
        return
    for sub in ("value", "evidence", "confidence"):
        if sub not in obj:
            out.append(_err(path + "." + sub, "缺失必填子字段 '%s'" % sub))
    ev = obj.get("evidence")
    if "evidence" in obj and not isinstance(ev, list):
        out.append(_err(path + ".evidence", "evidence 必须是数组，实际类型 %s" % type(ev).__name__))
    elif isinstance(ev, list) and len(ev) == 0:
        # 无源结论：evidence 为空数组——这是比"类型不对"更严重的契约违规，单独报
        out.append(_err(path + ".evidence", "无源结论：evidence 是空数组，SlotClaim 必须带来源"))
    if "confidence" in obj:
        c = obj["confidence"]
        if not isinstance(c, (int, float)) or isinstance(c, bool):
            out.append(_err(path + ".confidence", "confidence 必须是数字，实际 %s" % type(c).__name__))
        elif c < 0.0 or c > 1.0:
            out.append(_err(path + ".confidence", "confidence 越界，必须在 [0.0, 1.0]，实际 %s" % repr(c)))


def validate(obj):
    """对照 structured_requirement.schema.json 手写校验。

    返回：{"ok": bool, "errors": [{"path": "...", "message": "..."}]}

    能抓出的契约违规种类：
    - 顶层必填字段缺失
    - 顶层出现契约外字段（additionalProperties=false）
    - version/source_round 非整数 或 <1
    - demand_id/dataset/schema_version 非串 / 空串
    - status 不在 待审核/已通过/已退回 三枚举
    - 四枚单值槽位 + output_fields 每项：缺 value/evidence/confidence 三键之一
    - 任何 SlotClaim：evidence 不是数组 / 空数组（无源结论）
    - 任何 SlotClaim：confidence 非数字 / <0 / >1
    - 数组型字段（output_fields/aggregation_rules/confirmed_facts/evidence_chain/residual_risks）非数组
    """
    errors = []

    if not isinstance(obj, dict):
        errors.append(_err("$", "顶层必须是对象（dict），实际类型 %s" % type(obj).__name__))
        return {"ok": False, "errors": errors}

    # A. 必填字段存在性
    for k in REQUIRED_TOP:
        if k not in obj:
            errors.append(_err("$", "缺少必填顶层字段 '%s'" % k))

    # B. additionalProperties=false：顶层不得出现 REQUIRED_TOP 以外的键
    for k in obj.keys():
        if k not in REQUIRED_TOP:
            errors.append(_err(
                "$." + k,
                "额外字段 '%s' 未在契约中声明（additionalProperties=false）" % k,
            ))

    if errors:
        # 基础形状错了就不继续——下面的下标访问会炸
        return {"ok": False, "errors": errors}

    # C. 字符串型 必填 + 非空
    for sk in ("demand_id", "dataset", "schema_version"):
        v = obj[sk]
        if not isinstance(v, str):
            errors.append(_err("$." + sk, "必须是字符串，实际类型 %s" % type(v).__name__))
        elif len(v.strip()) == 0:
            errors.append(_err("$." + sk, "不能为空字符串"))

    # D. 整数型 version / source_round：必须 int 且 >=1
    for ik in ("version", "source_round"):
        v = obj[ik]
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(_err(
                "$." + ik,
                "必须是整数（int），实际类型 %s" % type(v).__name__,
            ))
        elif v < 1:
            errors.append(_err("$." + ik, "必须 >= 1，实际 %d" % v))

    # E. status 三枚举
    if obj["status"] not in VALID_STATUS:
        errors.append(_err(
            "$.status",
            "status 必须是 待审核/已通过/已退回 之一，实际 %s" % repr(obj["status"]),
        ))

    # F. 四枚单值 SlotClaim
    for sf in SINGLE_SLOT_FIELDS:
        _check_slot_claim(obj[sf], "$." + sf, errors)

    # G. output_fields：必须是数组，且每一项都是 SlotClaim
    of = obj["output_fields"]
    if not isinstance(of, list):
        errors.append(_err("$.output_fields", "必须是数组，实际类型 %s" % type(of).__name__))
    else:
        for i, item in enumerate(of):
            _check_slot_claim(item, "$.output_fields[%d]" % i, errors)

    # H. 其余数组字段：类型只做数组校验，元素结构不做强约束
    #    （confirmed_facts 结构在 build() 里由我们自己固定产出，evidence_chain/residual_risks
    #     允许额外属性；一期只要"形状对"即可）
    for arr_key in ("aggregation_rules", "confirmed_facts", "evidence_chain", "residual_risks"):
        if not isinstance(obj[arr_key], list):
            errors.append(_err(
                "$." + arr_key,
                "必须是数组，实际类型 %s" % type(obj[arr_key]).__name__,
            ))

    # I. residual_risks 若非空也顺便过一遍 SlotClaim 形状（不强求 evidence>=1，
    #    但至少结构对——否则下游展示时会缺字段）
    rr = obj["residual_risks"]
    if isinstance(rr, list):
        for i, item in enumerate(rr):
            p = "$.residual_risks[%d]" % i
            if isinstance(item, dict):
                for sub in ("value", "evidence", "confidence"):
                    if sub not in item:
                        errors.append(_err(p, "残余风险项缺少 '%s' 子字段" % sub))
            else:
                errors.append(_err(p, "残余风险项必须是对象（dict），实际 %s" % type(item).__name__))

    return {"ok": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# get · 读结构化对象（默认最新版）
# ---------------------------------------------------------------------------
def get(demand_id, version=None):
    """取结构化技术需求对象。

    默认返回最新版（同 demand_id 下 version 最大者）；version 指定则取对应版。
    成功：返回对象本身（**顶层无 ok**）。
    查不到：返回 {"ok": False, "error": "..."}。
    """
    if version is None:
        row = db.query_one(
            """
            SELECT payload FROM structured_requirements
            WHERE demand_id=%s ORDER BY version DESC LIMIT 1
            """,
            (demand_id,),
        )
    else:
        if isinstance(version, bool) or not isinstance(version, int):
            return {"ok": False, "error": "version 必须是整数，实际 %s" % type(version).__name__}
        row = db.query_one(
            """
            SELECT payload FROM structured_requirements
            WHERE demand_id=%s AND version=%s
            """,
            (demand_id, version),
        )
    if not row:
        if version is None:
            return {"ok": False, "error": "需求 %s 尚无结构化技术需求对象，请先执行 requirement_structured" % demand_id}
        return {"ok": False, "error": "需求 %s 不存在 version=%d 的结构化对象" % (demand_id, version)}
    payload = row.get("payload")
    if isinstance(payload, (dict, list)):
        return payload
    # psycopg3 的 JSONB 会返回 str 或 dict 视配置而定；统一走 json.loads 兜底
    if isinstance(payload, str):
        import json as _json
        try:
            return _json.loads(payload)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "payload JSON 解析失败：%s" % str(e)[:160]}
    return {"ok": False, "error": "payload 类型异常：%s" % type(payload).__name__}
