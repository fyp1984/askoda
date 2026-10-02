# -*- coding: utf-8 -*-
"""只读 SQL 执行 + 留痕（M5-2 · SQL生成链路设计 §4.9 / §7）

执行与审查的两阶段顺序（硬约束②）
---------------------------------
  阶段一：生成后的「预审查」
      gates.review(sql) —— 没有 exec_result。L5 恒 skipped；
      只要 status 为「不通过」= 阻断，不执行、不写 sql_runs。
  阶段二：真实结果返回后的「后置审查」
      gates.review(sql, exec_result=结果) —— L5 此时跑 _result_assertions。
      写 sql_runs 时 review_status / review_detail 用的是这一版（后置），
      validation_result 是 L5 的 signals。
  为什么不能"执行前 review 一次就够了"：L5 要的是**真实结果**（行数、空列、负值金额），
  没有执行就无从断言。顺序错了，验收项「结果校验给出行数/样本/异常信号」直接落空。

为什么 execute_readonly 参数里 requirement_version / pack_version / generator 都可以 None
----------------------------------------------------------------------
接线层（app.py.sql_execute_readonly）**只传 sql / dataset / demand_id** 三个参数；
其余版本号是选传的。既然接线层不传，就自己从 sqlpack.build(demand_id, dataset) 里
把三版本号（schema / requirement / pack）全取出来——sqlpack.build 的返回值里
已经带齐了这三个（M4-3 已输出溯源五维）。

9 项留痕字段与 sql_runs 列名的对应表
-----------------------------------
  §7-1 demand_id                       → demand_id（工具层直接取参数 demand_id）
  §7-2 structured_requirement_version  → requirement_version（列名 = requirement_version）
  §7-3 schema_version                  → schema_version
  §7-4 sql_context_pack_version        → pack_version（列名 = pack_version）
  §7-5 generator_model                 → generator_model
  §7-6 generated_sql                   → generated_sql
  §7-7 review_status                   → review_status
  §7-8 validation_result               → validation_result（JSONB）
  §7-9 final_delivery_sql              → final_delivery_sql
  另外：sql_run_id (PK)、actor (system)、created_at 用列默认。
  **没有 structured_requirement_version / sql_context_pack_version 两列**，千万不要按
  设计文档 §7 的名字去 INSERT（会列不存在报错）。
"""
import secrets
import copy

import db
import registry
import gates as gates_mod
import sqlpack as sqlpack_mod
import sqlgen as sqlgen_mod
import fallback as fallback_mod
from wren import WrenClient, WrenError, WrenTimeout  # noqa: F401

_WREN_CLIENTS = {}


def _wren_for(dataset_key):
    """按数据集 key 拿 Wren 客户端（复用 app.py 的 client_for 思路）。"""
    if dataset_key not in _WREN_CLIENTS:
        ds = registry.get(dataset_key)
        _WREN_CLIENTS[dataset_key] = WrenClient(ds.wren_url, name=dataset_key)
    return _WREN_CLIENTS[dataset_key]


def _resolve_versions(demand_id, dataset, requirement_version, pack_version, schema_version):
    """参数三版本缺失时，从 sqlpack.build 结果补齐。

    返回 (requirement_version, schema_version, pack_version) 三枚；任何一枚取不到
    就尽力而为（None 也行，写库时允许列 NULL，不因为一个版本号丢整条留痕记录）。
    """
    try:
        pack = sqlpack_mod.build(demand_id, dataset=dataset)
    except Exception:
        pack = None
    if isinstance(pack, dict) and pack.get("ok") is False:
        pack = None
    if not isinstance(pack, dict):
        return (requirement_version, schema_version, pack_version)
    if requirement_version is None:
        requirement_version = pack.get("requirement_version")
    if schema_version is None:
        schema_version = pack.get("schema_version")
    if pack_version is None:
        pack_version = pack.get("pack_version")
    return (requirement_version, schema_version, pack_version)


def _last_generated_sql(demand_id):
    """sql 参数为空时：取该需求最近一次的 generated_sql。

    用 sql_runs.created_at DESC LIMIT 1；查不到返回 None。
    """
    try:
        row = db.query_one(
            "SELECT generated_sql FROM sql_runs WHERE demand_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (demand_id,),
        )
    except Exception:
        return None
    if not row:
        return None
    return row.get("generated_sql")


def _new_run_id():
    """sql_run_id = SR- + 16 位十六进制随机串；8 位十六进制是 4 字节会撞，用 8 字节。"""
    return "SR-" + secrets.token_hex(8)


# ---------------------------------------------------------------------------
# execute_readonly
# ---------------------------------------------------------------------------
def execute_readonly(sql, dataset="B", demand_id=None, requirement_version=None,
                     pack_version=None, generator=None, actor="system"):
    """只读执行 SQL（先门禁 → 执行 → 再门禁跑 L5 → 写 sql_runs 留痕）。

    返回：{execution_summary, row_count, sample_rows, validation_result,
           suspicious_signals, review, sql_run_id}
    失败：{"ok": False, "error": "..."}
    """
    # a. sql 为空兜底：最近一次 generated_sql
    sql_effective = (sql if sql is not None else None)
    if sql_effective is None or str(sql_effective).strip() == "":
        if demand_id:
            last = _last_generated_sql(demand_id)
            if last:
                sql_effective = last
        if sql_effective is None or str(sql_effective).strip() == "":
            return {
                "ok": False,
                "error": "待执行 SQL 为空，且 sql_runs 中找不到 demand_id=%s 的历史生成 SQL" % demand_id,
            }

    sql_text = str(sql_effective).strip()

    # b. 预审查（没有 exec_result）：不通过 → 阻断，不执行，不写 sql_runs
    try:
        review_before = gates_mod.review(sql_text, dataset=dataset)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "gates.review(预审查)抛异常 %s: %s" % (type(e).__name__, str(e)[:240]),
        }
    if review_before.get("status") == "不通过":
        # M6-2 F4：status=不通过时透出「退回人工 + 治理指引」
        f4_ids = []
        for r in (review_before.get("rule_violations") or []):
            rid = r.get("rule_id")
            if rid and rid not in f4_ids:
                f4_ids.append(rid)
        # 兼容：semantic_issues/syntax_issues 里的 rule 字段也当命中
        for bucket in (review_before.get("semantic_issues") or []), (review_before.get("syntax_issues") or []):
            for it in bucket or []:
                if isinstance(it, dict):
                    rid = it.get("rule")
                    if rid and rid not in f4_ids:
                        f4_ids.append(rid)
        fallback_block = {
            "kind": "F4",
            "strategy": "退回人工分析 + 治理指引（不自动改写）",
            "guidance": fallback_mod.f4_guidance(f4_ids),
        }
        return {
            "ok": False,
            "blocked": True,
            "blocked_by": "pre_review",
            "error": "SQL 五层门禁预审查未通过：%s" % _blocked_summary(review_before),
            "review": review_before,
            "fallback": fallback_block,
        }

    # c. 执行：WrenClient.query（只读通道，与 wren_query 同源）
    ds_obj = registry.get(dataset)
    wren_err_kind = None
    wren_err_text = ""
    wren_err_attempts = 0
    try:
        client = _wren_for(dataset)
        exec_result = client.query(sql_text)
    except WrenTimeout as wto:
        wren_err_kind = "WrenTimeout"
        wren_err_text = str(wto)
        wren_err_attempts = getattr(client, "last_attempts", 0)
    except WrenError as we:
        wren_err_kind = "WrenError"
        wren_err_text = str(we)
        wren_err_attempts = getattr(client, "last_attempts", 0)
    except Exception as e:  # noqa: BLE001
        wren_err_kind = type(e).__name__
        wren_err_text = str(e)
    if wren_err_kind:
        fallback_block = {
            "kind": "WREN_ERROR",
            "strategy": "Wren 执行失败：已按读幂等重试（WrenTimeout） / 未重试（其他写/非幂等），请人工确认语义层状态"
            if wren_err_kind == "WrenTimeout"
            else "Wren 执行失败：非超时类异常，未自动重试，请人工确认",
            "wren_error": {
                "type": wren_err_kind,
                "message": wren_err_text[:500],
                "attempts": wren_err_attempts,
            },
        }
        return {
            "ok": False,
            "error": "Wren 执行失败（%s，attempts=%d）：%s"
            % (wren_err_kind, wren_err_attempts or 0, wren_err_text[:300]),
            "review": review_before,
            "fallback": fallback_block,
        }

    data = exec_result.get("data") or []
    cols = exec_result.get("columns") or []
    row_count = len(data)
    sample_rows = copy.deepcopy(data[:5])

    # d. 后置审查：传 exec_result 让 L5 真跑
    try:
        review_after = gates_mod.review(sql_text, dataset=dataset, exec_result=exec_result)
    except Exception as e:  # noqa: BLE001
        review_after = {
            "status": "警告",
            "layers": [{
                "layer": "L-EXEC", "name": "后置审查异常",
                "ok": True, "skipped": True,
                "detail": "%s: %s" % (type(e).__name__, str(e)[:200]),
            }],
            "syntax_issues": [],
            "semantic_issues": [],
            "rule_violations": [],
            "revised_sql": None,
            "review_notes": [],
        }

    # 从 review_after 提取 L5 signals（suspicious_signals） 与 validation_result
    l5_signals = []
    l5_layer_ok = True
    for l in (review_after.get("layers") or []):
        if l.get("layer") == "L5" and l.get("name") == "结果断言":
            # 把 detail 文本拆进 signals；_result_assertions 返回的 (ok, signals) 记在 review_notes
            if not l.get("ok"):
                l5_layer_ok = False
            break
    # 更直接：从 _result_assertions 调用的结果——gates.review 里 signals 会写进
    # review_notes；若未写，退化用 detail 文本
    suspicious_signals = list(l5_signals)
    for note in (review_after.get("review_notes") or []):
        if isinstance(note, dict) and "signals" in note:
            suspicious_signals.extend(note["signals"] or [])
        elif isinstance(note, str) and "信号" in note:
            suspicious_signals.append(note)
    # 兜底：直接重新算一次（gates._result_assertions 是模块级私有函数这里不会 import；
    # 改为从 review_after.layers[L5] 的 detail 里提取现成描述）
    if not suspicious_signals:
        for l in (review_after.get("layers") or []):
            if l.get("layer") == "L5":
                # 只在 L5.ok=False 时 detail 是"信号：..."；若 ok=True detail 是"结果断言通过"
                d = l.get("detail") or ""
                if d and not l.get("skipped") and not l.get("ok"):
                    suspicious_signals.append(d[:200])
                break
    if row_count == 0 and not suspicious_signals:
        suspicious_signals.append("结果集为空（0 行），请检查过滤条件或时间范围")
    suspicious_signals = list(dict.fromkeys(suspicious_signals))  # 保序去重

    # M6-2 F5：行数/列数/信号 -> deliverable + 原因
    f5_out = fallback_mod.f5_deliverable(row_count, len(cols), suspicious_signals)
    deliverable = bool(f5_out.get("deliverable"))
    deliverable_reason = str(f5_out.get("reason") or "")

    # M6-2 F4：如果 review_after（后置）status=不通过，也要透出 fallback 治理指引（无论是否阻断，这里是返回信息加字段）
    fallback_extra = None
    f4_ids = []  # 提升到外层：末尾的 classify 汇总也要用
    review_status_final = review_after.get("status") or review_before.get("status")
    if review_status_final == "不通过":
        for r in (review_after.get("rule_violations") or []):
            rid = r.get("rule_id")
            if rid and rid not in f4_ids:
                f4_ids.append(rid)
        for bucket in (review_after.get("semantic_issues") or []), (review_after.get("syntax_issues") or []):
            for it in bucket or []:
                if isinstance(it, dict):
                    rid = it.get("rule")
                    if rid and rid not in f4_ids:
                        f4_ids.append(rid)
        fallback_extra = {
            "kind": "F4",
            "strategy": "退回人工分析 + 治理指引（不自动改写）",
            "guidance": fallback_mod.f4_guidance(f4_ids),
        }

    validation_result = {
        "row_count": row_count,
        "columns": cols,
        "sample_first3": copy.deepcopy(data[:3]),
        "signals": suspicious_signals,
        "l5_ok": l5_layer_ok,
    }

    # M6-2 失败回退矩阵：把本次执行的 F4（审查命中规则）/ F5（结果可交付性）
    # 汇总进唯一入口。此前 fallback.classify() 只有 tools/ 里的自证脚本调用、
    # 生产链路零调用（P2 遗留），这里把它接上，产出 fallback_matrix 字段。
    # 纯增量：不改既有 fallback / deliverable / adopted 的值。
    fallback_matrix = fallback_mod.classify(
        f4_rule_ids=(f4_ids or None),
        f5={"deliverable": deliverable, "reason": deliverable_reason},
    )

    # e. 写 sql_runs（9 字段齐全可回放）
    (rv, sv, pv) = _resolve_versions(
        demand_id, dataset, requirement_version, pack_version, None
    )
    # schema_version 若 resolve 不到但有 sqlpack.build，再最后尝试一次 latest_version
    if sv is None:
        try:
            import schema_scan as ss  # noqa: E402
            lv = ss.latest_version(dataset)
            if lv:
                sv = lv.get("schema_version")
        except Exception:
            pass

    generator_model = generator or "deterministic-planner"
    review_status = review_status_final
    review_detail_payload = {
        "review_before_status": review_before.get("status"),
        "review_after_status": review_after.get("status"),
        "layers": [
            {k: l.get(k) for k in ("layer", "name", "ok", "skipped", "detail") if l.get(k) is not None}
            for l in (review_after.get("layers") or [])
        ],
        "syntax_issues": review_after.get("syntax_issues", [])[:10],
        "semantic_issues": review_after.get("semantic_issues", [])[:10],
        "rule_violations": [
            {k: v for k, v in rv_i.items() if k in ("layer", "rule_id", "type", "detail")}
            for rv_i in (review_after.get("rule_violations") or [])[:10]
        ],
    }
    final_delivery_sql = None
    if isinstance(review_after.get("revised_sql"), str) and review_after["revised_sql"].strip():
        final_delivery_sql = review_after["revised_sql"].strip()
    else:
        final_delivery_sql = sql_text

    run_id = _new_run_id()
    try:
        db.execute(
            """
            INSERT INTO sql_runs
                (sql_run_id, demand_id, requirement_version, schema_version, pack_version,
                 generator_model, generated_sql, review_status, review_detail,
                 validation_result, final_delivery_sql, actor)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                run_id,
                demand_id,
                (int(rv) if isinstance(rv, int) else (int(rv) if str(rv or "").isdigit() else None)),
                (sv if isinstance(sv, str) else None),
                (pv if isinstance(pv, str) else None),
                generator_model,
                sql_text,
                review_status,
                db.dumps(review_detail_payload),
                db.dumps(validation_result),
                final_delivery_sql,
                actor,
            ),
        )
    except Exception as e:  # noqa: BLE001
        result = {
            "ok": False,
            "error": "sql_runs 写入失败 %s: %s" % (type(e).__name__, str(e)[:240]),
            "execution_summary": "Wren 执行成功（%d 行，%d 列）但留痕失败" % (row_count, len(cols)),
            "row_count": row_count,
            "sample_rows": sample_rows,
            "validation_result": validation_result,
            "suspicious_signals": suspicious_signals,
            "review": review_after,
            "sql_run_id": run_id,
        }
        result["deliverable"] = deliverable
        result["deliverable_reason"] = deliverable_reason
        result["adopted"] = bool(deliverable and review_status_final != "不通过")
        result["fallback_matrix"] = fallback_matrix
        if fallback_extra:
            result["fallback"] = fallback_extra
        return result

    adopted = bool(deliverable and review_status_final != "不通过")
    result = {
        "execution_summary": "Wren 执行成功：%d 行 × %d 列（dataset=%s, generator=%s）" % (
            row_count, len(cols), dataset, generator_model
        ),
        "row_count": row_count,
        "sample_rows": sample_rows,
        "validation_result": validation_result,
        "suspicious_signals": suspicious_signals,
        "review": review_after,
        "sql_run_id": run_id,
        "requirement_version": rv,
        "schema_version": sv,
        "pack_version": pv,
        "deliverable": deliverable,
        "deliverable_reason": deliverable_reason,
        "adopted": adopted,
    }
    result["fallback_matrix"] = fallback_matrix
    if fallback_extra:
        result["fallback"] = fallback_extra
    return result


def _blocked_summary(review):
    """从 review 包里提一句话"哪层拦的 + 什么理由"，用于 error 字段。"""
    parts = []
    for l in (review.get("layers") or []):
        if not l.get("ok") and not l.get("skipped"):
            d = (l.get("detail") or "")[:120]
            parts.append("%s(%s): %s" % (l.get("layer"), l.get("name"), d))
    if not parts:
        for r in (review.get("rule_violations") or [])[:3]:
            parts.append(
                "[%s/%s] %s" % (r.get("layer"), r.get("rule_id"), (r.get("detail") or "")[:80])
            )
    return " ｜ ".join(parts[:3]) or "未通过（详情见 review 字段）"


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------
def get_run(demand_id):
    """回放该需求全部 sql_runs（created_at DESC）。

    查不到：{"ok": False, "error": "..."}
    """
    try:
        rows = db.query(
            """
            SELECT
                sql_run_id,
                demand_id,
                requirement_version,
                schema_version,
                pack_version,
                generator_model,
                generated_sql,
                review_status,
                review_detail,
                validation_result,
                final_delivery_sql,
                actor,
                created_at::text AS created_at
            FROM sql_runs WHERE demand_id=%s ORDER BY created_at DESC, sql_run_id DESC
            """,
            (demand_id,),
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "sql_runs 查询异常 %s: %s" % (type(e).__name__, str(e)[:200]),
        }
    if not rows:
        return {
            "ok": False,
            "error": "demand_id=%s 未找到任何 sql_runs 记录" % demand_id,
        }
    # payload 字段在 jsonb 里，psycopg3 返回 dict；若非 dict，做一次 json.loads 兜底
    normalized = []
    import json as _json
    for r in rows:
        item = dict(r)
        for k in ("review_detail", "validation_result"):
            v = item.get(k)
            if isinstance(v, str):
                try:
                    item[k] = _json.loads(v)
                except Exception:
                    pass
        normalized.append(item)
    return {
        "ok": True,
        "demand_id": demand_id,
        "total": len(normalized),
        "runs": normalized,
    }


def _normalize_json(val):
    """DB JSONB/TEXT 统一转成 dict/list；str 尝试 json.loads 失败则原样。"""
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str) and val:
        try:
            import json as _json_inner
            return _json_inner.loads(val)
        except Exception:
            return val
    return val


def _structured_requirement_for(demand_id, requirement_version):
    """按 (demand_id, requirement_version) 查 structured_requirements。

    表结构：demand_id / version / dataset / schema_version / source_round /
            payload(JSONB，版本体) / status / created_at
    版本体在 payload 列，不是 body。
    """
    try:
        if requirement_version is None:
            row = db.query_one(
                "SELECT payload, version FROM structured_requirements "
                "WHERE demand_id=%s ORDER BY version DESC LIMIT 1",
                (demand_id,),
            )
        else:
            row = db.query_one(
                "SELECT payload, version FROM structured_requirements "
                "WHERE demand_id=%s AND version=%s LIMIT 1",
                (demand_id, requirement_version),
            )
    except Exception:
        return None, None
    if not row:
        return None, None
    return _normalize_json(row.get("payload")), row.get("version")


def sql_run_list(demand_id=None, dataset=None, limit=20):
    """sql_runs 跨需求列表（筛选 demand_id / dataset，created_at DESC）。

    dataset 反查：从 structured_requirements 表（确有 dataset 列）反查 demand_id 集合。
    查不到 / 反查异常 → 空结果 + 说明；绝不因异常静默降级成全量。
    """
    note = (
        "sql_runs 无 dataset 列；dataset 筛选走 structured_requirements.dataset "
        "→ 反查出 demand_id 集合 → 再筛 sql_runs.demand_id IN (...)。"
        "查不到或反查异常时返回空（not full）。"
    )
    demand_ids_filter = None
    dataset_filter_applied = False
    if dataset:
        try:
            rows_did = db.query(
                "SELECT DISTINCT demand_id FROM structured_requirements WHERE dataset = %s",
                (dataset,),
            )
            demand_ids_filter = sorted({r["demand_id"] for r in rows_did if r.get("demand_id")})
        except Exception as e:  # noqa: BLE001
            # 反查异常：返回空，不得退回成全量
            return {
                "ok": False,
                "total": 0,
                "items": [],
                "dataset_filter_note": note
                + "；structured_requirements 反查异常 %s: %s（已返回空，不降级成全量）"
                % (type(e).__name__, str(e)[:160]),
                "dataset_filter_applied": False,
                "error": "dataset=%s 反查异常：%s" % (dataset, str(e)[:160]),
            }
        dataset_filter_applied = True
        if not demand_ids_filter:
            return {
                "ok": True,
                "total": 0,
                "items": [],
                "dataset_filter_note": note + "；反查返回 0 个 demand_id（dataset=%s 确无数据）" % dataset,
                "dataset_filter_applied": True,
            }

    base_where = []
    args = []
    if demand_id:
        base_where.append("demand_id=%s")
        args.append(demand_id)
    if demand_ids_filter is not None:
        ph = ",".join(["%s"] * len(demand_ids_filter))
        base_where.append("demand_id IN (%s)" % ph)
        args.extend(demand_ids_filter)
    where_sql = ("WHERE " + " AND ".join(base_where)) if base_where else ""
    total_sql = "SELECT count(*) AS n FROM sql_runs %s" % where_sql
    list_sql = (
        "SELECT sql_run_id, demand_id, created_at::text AS created_at, generator_model, "
        "review_status, pack_version, requirement_version, schema_version, actor, "
        "final_delivery_sql, generated_sql "
        "FROM sql_runs %s ORDER BY created_at DESC, sql_run_id DESC LIMIT %s"
        % (where_sql, "%s")
    )
    args_with_limit = list(args)
    args_with_limit.append(int(limit))
    try:
        total_row = db.query_one(total_sql, tuple(args))
        item_rows = db.query(list_sql, tuple(args_with_limit))
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": "sql_run_list 查询失败 %s: %s" % (type(e).__name__, str(e)[:200]),
            "dataset_filter_note": note,
            "dataset_filter_applied": dataset_filter_applied,
        }
    items = []
    for r in item_rows:
        final_sql = r.get("final_delivery_sql") or ""
        adopted = bool(final_sql and (r.get("review_status") or "") != "不通过")
        items.append({
            "sql_run_id": r.get("sql_run_id"),
            "demand_id": r.get("demand_id"),
            "created_at": r.get("created_at"),
            "generator_model": r.get("generator_model"),
            "review_status": r.get("review_status"),
            "adopted": adopted,
            "pack_version": r.get("pack_version"),
            "requirement_version": r.get("requirement_version"),
            "schema_version": r.get("schema_version"),
            "actor": r.get("actor"),
            "generated_sql_preview": (r.get("generated_sql") or "")[:200],
        })
    out = {
        "ok": True,
        "total": int((total_row or {}).get("n", 0)),
        "items": items,
        "dataset_filter_note": note,
    }
    if dataset:
        out["dataset_filter_applied"] = dataset_filter_applied
    return out


def sql_run_replay(demand_id, version=None):
    """按「第 N 次运行」回放完整上下文；version=None 取最近一次。"""
    import json as _json_local  # noqa: F811 内层不与顶部（此处无）冲突
    # 先取该 demand 所有 sql_runs
    try:
        rows = db.query(
            "SELECT sql_run_id, created_at::text AS created_at, actor, requirement_version, "
            "schema_version, pack_version, generated_sql, final_delivery_sql, review_status, "
            "review_detail, validation_result "
            "FROM sql_runs WHERE demand_id=%s ORDER BY created_at ASC, sql_run_id ASC",
            (demand_id,),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "sql_run_replay 查询失败 %s: %s" % (type(e).__name__, str(e)[:200])}
    if not rows:
        return {"ok": False, "error": "demand_id=%s 未找到任何 sql_runs，无法回放" % demand_id}
    # version 语义：第 N 次执行（从 1 起），None=最后一次
    if version is None:
        idx = len(rows) - 1
        used_version = len(rows)
    else:
        try:
            iv = int(version)
        except Exception:
            return {"ok": False, "error": "version=%s 不是合法整数" % str(version)[:30]}
        if iv < 1 or iv > len(rows):
            return {"ok": False, "error": "version=%d 越界，该 demand 共 %d 次运行" % (iv, len(rows))}
        idx = iv - 1
        used_version = iv
    r = rows[idx]
    run_id = r.get("sql_run_id")
    requirement_version = r.get("requirement_version")
    struct_body, struct_ver = _structured_requirement_for(demand_id, requirement_version)

    gaps = []
    if struct_body is None:
        gaps.append(
            "structured_requirements 未找到 (demand_id=%s, requirement_version=%s)"
            % (demand_id, requirement_version)
        )
    review_detail = _normalize_json(r.get("review_detail"))
    validation = _normalize_json(r.get("validation_result")) or {}
    row_count = validation.get("row_count") if isinstance(validation, dict) else None
    if row_count is None and isinstance(r.get("validation_result"), dict):
        row_count = r["validation_result"].get("row_count")
    signals = validation.get("signals") if isinstance(validation, dict) else []
    final_sql = r.get("final_delivery_sql") or ""
    adopted = bool(final_sql and (r.get("review_status") or "") != "不通过")

    # 知识引用：按 sql_run_id 或 demand_id 查 knowledge_citations（表可能未建/无行）
    citations_rows = []
    try:
        citations_rows = db.query(
            "SELECT citation_id, demand_id, question, document_id, document_name, chunk_id, "
            "positions::text AS positions, round_no, sql_run_id, dataset_id, created_at::text AS created_at, "
            "retired_at::text AS retired_at, retired_reason "
            "FROM knowledge_citations WHERE sql_run_id=%s OR demand_id=%s "
            "ORDER BY created_at ASC, citation_id ASC",
            (run_id, demand_id),
        )
    except Exception:
        gaps.append("knowledge_citations 表不可达，未取到引用留痕（demand_id=%s, sql_run_id=%s）" % (demand_id, run_id))
    citations_items = []
    for cr in citations_rows:
        c = dict(cr)
        if c.get("positions"):
            try:
                c["positions"] = _json_local.loads(c["positions"])
            except Exception:
                pass
        citations_items.append(c)

    return {
        "demand_id": demand_id,
        "sql_run_id": run_id,
        "version_idx": used_version,
        "created_at": r.get("created_at"),
        "actor": r.get("actor"),
        "input": {
            "structured_requirement": struct_body,
            "structured_requirement_version_resolved": struct_ver,
            "pack_version": r.get("pack_version"),
            "schema_version": r.get("schema_version"),
            "requirement_version": r.get("requirement_version"),
        },
        "sql": {
            "generated_sql": r.get("generated_sql"),
            "final_delivery_sql": final_sql,
            "adopted": adopted,
        },
        "review": {
            "status": r.get("review_status"),
            "detail": review_detail if isinstance(review_detail, (dict, list)) else None,
        },
        "result": {
            "row_count": row_count,
            "validation_result": validation,
            "suspicious_signals": list(signals) if isinstance(signals, list) else [],
        },
        "knowledge_citations": citations_items,
        "gaps": gaps,
    }
