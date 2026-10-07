#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MVP 四动作闭环端到端验证（G3 真实链路）。

补的到底是什么洞
----------------
`gate_all.py` 的 G3 层自称「真实链路（探活 + `*_verify.py`）」，但仓库里长期
**一份 `*_verify.py` 都没有**（`ls tools/*_verify.py` → no matches found）。
于是 G3 实际只跑了 1 次 `MCP::gateway_health` 探活，MVP 主链路完全在门禁之外：
提交→分析→确认→SQL→只读执行 这四动作、BFF 的 23 个接口、七层门禁，
下次谁改坏一条，G3 照样全绿。本文件把这条链路钉成断言。

跑的是什么
----------
照真实链路跑，**不伪造、不桩、不绕过**：

  ① POST /api/v1/demand                 提交需求        → demand_id
  ② POST /api/v1/demand/{id}/analyze    语义分析        → round_no / 槽位候选 / 待确认问题
  ③ POST /api/v1/demand/{id}/confirm    口径确认        → ★ 答复后自动重跑分析（round_no +1）
  ④ POST /api/v1/demand/{id}/sql        生成 SQL        → review.layers（7 层）
  ⑤ POST /api/v1/demand/{id}/sql/execute 只读执行       → deliverable / row_count

③ 的「答复后自动重跑分析」是这套设计的关键——答复本身不回填槽位，
必须重跑才生效（见 bff/app.py:383 confirm 的 docstring）。本脚本**断言**
round_no 确实 +1、槽位确实被回填、P2 证据确实进链，不允许跳过。

为什么用「各门店销售额与已退款金额统计」这条需求
------------------------------------------------
③ 必须答复一个**真实存在的**待确认问题，所以样本需求得能稳定触发问题生成。
`gateway/semantics.py:1030` 的 Q-SCOPE-1 触发条件是：正文含「已退款」
且不含「排除/不含/剔除…」等排除词。这条需求命中该条件，实测稳定产出
Q-GRAIN-1 + Q-SCOPE-1 两个问题，答复第一个（Q-GRAIN-1）后回填可见。

阈值不放松
----------
本脚本的价值在于「红了能报警」。因此：
  · `row_count` 只断言 `> 0`（真实数据行数为正），**不断言具体行数**
    ——行数会随数据变化，写死就是给自己埋假红；
  · 但 `deliverable` / `五层门禁 L1-L4 全 ok` / `round_no 递增` /
    `P2 证据入链` 这些**行为契约**一律断言死，不容退化；
  · 实跑发现真问题就**如实报红**，不在脚本里绕。

单条失败不中断
--------------
单条断言失败**不立刻退出**，后续断言继续跑，把问题一次报全。
（阶段未到时的`healthy=False`、partial_errors 属正常演进中状态，
中途退出反而会把「还没走到那一步」误报成红。）

用法
----
    export PATH="$HOME/.orbstack/bin:$PATH"
    export MDL_A_PATH=$PWD/wren-docker/workspace/mdl.json
    export MDL_B_PATH=$PWD/wren-docker-b/workspace/mdl.json
    python3 tools/e2e_verify.py            # 人读格式
    python3 tools/e2e_verify.py --json     # 结构化（供 CI / 看板消费）

退出码：0 = 全过，1 = 有红项。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "gateway"))

BFF_BASE = os.getenv("E2E_BFF_BASE", "http://127.0.0.1:18081").rstrip("/")
API = BFF_BASE + "/api/v1"
DATASET = "B"

# 触发 Q-SCOPE-1（正文含「已退款」且无排除词）→ 保证 ③ 有真实问题可答。
SAMPLE_DEMAND = {
    "title": "各门店销售额与已退款金额统计",
    "business_context": "门店月度业绩复盘，需要看含退款的销售口径",
    "description": "统计B库各门店按月的销售额与订单数，"
                   "其中已退款的业务也要一并统计进来，用于对账",
    "expected_output": "门店-月份维度的销售额、订单数、已退款金额结果集",
    "contact": "e2e-gate",
    "time_range": "近6个月",
    "actor": "e2e_verify",
}

# 必须实际跑通（非 skipped）的门禁层；L5 结果断言依赖 exec_result，
# 在 ④ 生成阶段天然 skipped，故只在 ⑤ 断言。
REQUIRED_LAYERS_SQL = ("L1", "L2", "L3", "L4")
EXPECTED_LAYER_COUNT = 7          # L1-L5 + L-SQLFluff + L-GX
HTTP_TIMEOUT = 300


# ---------------------------------------------------------------------------
# 断言累加器：一条红不中断，全部跑完再汇总
# ---------------------------------------------------------------------------
class Report(object):
    def __init__(self):
        self.items = []

    def add(self, stage, name, ok, detail="", warn=False):
        """记一条断言。warn=True 记黄项（不计入失败，但会显示）。"""
        self.items.append({
            "stage": stage, "name": name,
            "ok": bool(ok), "warn": bool(warn), "detail": detail,
        })
        if warn:
            line = "[WARN] %-8s %s%s" % (stage, name, ("  — " + detail) if detail else "")
        elif ok:
            line = "[OK]   %-8s %s%s" % (stage, name, ("  — " + detail) if detail else "")
        else:
            line = "[FAIL] %-8s %s%s" % (stage, name, ("  — " + detail) if detail else "")
        # 进度走 stderr：stdout 只留 --json 的结构化结果（gate_all 两者都收）
        print(line, file=sys.stderr, flush=True)
        return bool(ok)

    def failed(self):
        return [i for i in self.items if not i["ok"] and not i["warn"]]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def http(method, url, payload=None, timeout=HTTP_TIMEOUT):
    """返回 (http_status, parsed_json_or_None, raw_text)。HTTP 错误不抛异常。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status, raw = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 —— 连不上也要报全，不能中途退出
        return 0, None, "%s: %s" % (type(e).__name__, e)
    try:
        return status, json.loads(raw), raw
    except Exception:  # noqa: BLE001
        return status, None, raw


def brief(raw, n=300):
    return " ".join(str(raw).split())[:n]


# ---------------------------------------------------------------------------
# 各阶段断言
# ---------------------------------------------------------------------------
def stage_health(rep):
    """BFF 探活：确认网关链路本身通，且 A/B 双库 ok。"""
    st, data, raw = http("GET", API + "/health", timeout=60)
    if not rep.add("health", "BFF /health 可达", st == 200, "HTTP=%s" % st):
        return None
    if not isinstance(data, dict):
        return rep.add("health", "/health 返回 JSON 对象", False, brief(raw)) and None

    rep.add("health", "status == ok", data.get("status") == "ok",
            "status=%r" % data.get("status"))
    for ds in ("A", "B"):
        item = next((d for d in (data.get("datasets") or []) if d.get("dataset") == ds), None)
        rep.add("health", "数据集 %s ok" % ds, bool(item and item.get("ok")),
                (brief(item.get("response")) if item else "响应里没有 dataset=%s" % ds))
    return data


def stage_submit(rep):
    """① 提交需求。"""
    st, data, raw = http("POST", API + "/demand", SAMPLE_DEMAND, timeout=120)
    if not rep.add("submit", "POST /demand HTTP 200", st == 200, "HTTP=%s %s" % (st, brief(raw, 200))):
        return None
    did = (data or {}).get("demand_id")
    if not rep.add("submit", "返回 demand_id", bool(did), "demand_id=%r" % did):
        return None
    rep.add("submit", "demand_id 形如 DR-YYYYMMDD-XXXX",
            bool(str(did).startswith("DR-")), "demand_id=%s" % did)
    rep.add("submit", "标题回显一致", (data or {}).get("title") == SAMPLE_DEMAND["title"],
            "title=%r" % (data or {}).get("title"))

    # 负向：必填缺失必须被 422 拦下（证明硬门禁没被拆）
    bad = dict(SAMPLE_DEMAND, title="")
    st2, _, raw2 = http("POST", API + "/demand", bad, timeout=60)
    rep.add("submit", "负向：空 title 被 422 拒绝", st2 == 422, "HTTP=%s %s" % (st2, brief(raw2, 160)))
    return did


def stage_analyze(rep, demand_id):
    """② 语义分析。"""
    if not demand_id:
        rep.add("analyze", "前置 demand_id", False, "① 未产出 demand_id，跳过本阶段")
        return None
    st, data, raw = http("POST", API + "/demand/%s/analyze" % demand_id,
                         {"dataset": DATASET, "actor": "e2e_verify"})
    if not rep.add("analyze", "POST /analyze HTTP 200", st == 200, "HTTP=%s %s" % (st, brief(raw, 200))):
        return None
    if not isinstance(data, dict):
        return rep.add("analyze", "返回 JSON 对象", False, brief(raw)) and None

    rno = data.get("round_no")
    rep.add("analyze", "round_no >= 1（首轮）", isinstance(rno, int) and rno >= 1, "round_no=%r" % rno)
    rep.add("analyze", "ok == true", data.get("ok") is True, "ok=%r" % data.get("ok"))
    rep.add("analyze", "返回 demand_id 一致", data.get("demand_id") == demand_id,
            "%r" % data.get("demand_id"))
    rep.add("analyze", "demand_status 非空", bool(data.get("demand_status")),
            "demand_status=%r" % data.get("demand_status"))

    slots = data.get("slots")
    rep.add("analyze", "slots 为非空对象", isinstance(slots, dict) and len(slots) > 0,
            "槽位=%s" % (list(slots.keys()) if isinstance(slots, dict) else type(slots).__name__))
    if isinstance(slots, dict):
        # 槽位候选结构：{"candidates": [{"slot","value","confidence",...}], ...}
        with_cand = [k for k, v in slots.items()
                     if isinstance(v, dict) and (v.get("candidates") or [])]
        rep.add("analyze", "至少一个槽位带候选", len(with_cand) > 0, "带候选槽位=%s" % with_cand)
        subject = slots.get("subject")
        subj_vals = [(c or {}).get("value") for c in ((subject or {}).get("candidates") or [])]
        rep.add("analyze", "subject 槽位有可识别取值", any(bool(v) for v in subj_vals),
                "subject 候选=%s" % subj_vals)

    ev = data.get("evidence_chain")
    rep.add("analyze", "证据链非空", isinstance(ev, list) and len(ev) > 0,
            "evidence_chain 条数=%s" % (len(ev) if isinstance(ev, list) else type(ev).__name__))

    qs = data.get("questions")
    rep.add("analyze", "产出待确认问题（③ 的输入）", isinstance(qs, list) and len(qs) > 0,
            "questions 条数=%s" % (len(qs) if isinstance(qs, list) else type(qs).__name__))
    if isinstance(qs, list) and qs:
        q0 = qs[0]
        # 问题结构：question_id / slot / question / options / impact_scope
        ok_shape = all(q0.get(k) for k in ("question_id", "question", "options"))
        rep.add("analyze", "首个问题结构完整(question_id/question/options)", ok_shape,
                "question_id=%r options=%s" % (q0.get("question_id"), len(q0.get("options") or [])))
    return data


def stage_confirm(rep, demand_id, analysis):
    """③ 口径确认：答复第一个待确认问题，★ 断言自动重跑分析。"""
    if not demand_id or not isinstance(analysis, dict):
        rep.add("confirm", "前置 demand_id + 分析结果", False, "上游未完成，跳过本阶段")
        return None
    qs = analysis.get("questions") or []
    if not qs:
        rep.add("confirm", "存在可答复的待确认问题", False,
                "questions 为空——③ 无真实输入，必须报红（样本需求应触发 Q-SCOPE-1）")
        return None

    q0 = qs[0]
    qid = q0.get("question_id")
    options = q0.get("options") or []
    # 答一个真实选项（不用「其他（请补充说明）」，那条走人工补字路）
    answer = next((o for o in options if "其他" not in o), options[0])
    before_rno = analysis.get("round_no")

    st, data, raw = http("POST", API + "/demand/%s/confirm" % demand_id, {
        "question_id": qid, "answer": answer, "choice": answer, "actor": "e2e_verify",
    })
    if not rep.add("confirm", "POST /confirm HTTP 200", st == 200, "HTTP=%s %s" % (st, brief(raw, 200))):
        return None
    if not isinstance(data, dict):
        return rep.add("confirm", "返回 JSON 对象", False, brief(raw)) and None

    rep.add("confirm", "ok == true", data.get("ok") is True, "ok=%r" % data.get("ok"))
    rep.add("confirm", "confirmation 段非空", bool(data.get("confirmation")),
            "confirmation 键=%s" % list((data.get("confirmation") or {}).keys())[:6]
            if isinstance(data.get("confirmation"), dict) else "")

    # ★ 核心：答复后自动重跑分析
    re_ = data.get("reanalysis")
    if not rep.add("confirm", "★ 答复后自动重跑分析（reanalysis 非空）",
                   isinstance(re_, dict) and bool(re_), "reanalysis 类型=%s"
                   % type(re_).__name__):
        return data
    after_rno = re_.get("round_no")
    rep.add("confirm", "★ reanalysis.round_no == 首轮 +1",
            isinstance(after_rno, int) and isinstance(before_rno, int)
            and after_rno == before_rno + 1,
            "round_no %r -> %r" % (before_rno, after_rno))

    bf = data.get("backfill") or {}
    rep.add("confirm", "★ 槽位被回填（backfill.backfilled）", bf.get("backfilled") is True,
            "backfilled=%r" % bf.get("backfilled"))
    changed = bf.get("slots_changed") or []
    rep.add("confirm", "★ 回填有具体槽位变更记录", len(changed) > 0,
            "变更槽位=%s" % [c.get("slot") for c in changed if isinstance(c, dict)])
    p2 = bf.get("p2_evidence_count")
    rep.add("confirm", "★ P2「业务确认」证据入链", isinstance(p2, int) and p2 >= 1,
            "p2_evidence_count=%r" % p2)

    backfilled = re_.get("slots_backfilled") or []
    rep.add("confirm", "reanalysis.slots_backfilled 含所答问题",
            qid in backfilled, "slots_backfilled=%s（所答=%s）" % (backfilled, qid))
    return data


def stage_sql(rep, demand_id):
    """④ 生成 SQL：断言七层门禁结构与 L1-L4 实跑通过。"""
    if not demand_id:
        rep.add("sql", "前置 demand_id", False, "① 未产出 demand_id，跳过本阶段")
        return None
    st, data, raw = http("POST", API + "/demand/%s/sql" % demand_id, {"dataset": DATASET})
    if not rep.add("sql", "POST /sql HTTP 200", st == 200, "HTTP=%s %s" % (st, brief(raw, 200))):
        return None
    if not isinstance(data, dict):
        return rep.add("sql", "返回 JSON 对象", False, brief(raw)) and None

    draft = (data.get("sql_draft") or "").strip()
    rep.add("sql", "sql_draft 非空", bool(draft), "长度=%d" % len(draft))
    if draft:
        head = draft.lstrip().upper()
        rep.add("sql", "sql_draft 是只读语句(Select/With)",
                head.startswith("SELECT") or head.startswith("WITH"),
                "开头=%r" % draft.lstrip()[:40])
    rep.add("sql", "generator 非空", bool(data.get("generator")), "generator=%r" % data.get("generator"))

    review = data.get("review")
    if not rep.add("sql", "review 段非空", isinstance(review, dict) and bool(review),
                   "review 类型=%s" % type(review).__name__):
        return data
    # review.layers 是 **list**（不是 dict），每层 {"layer","name","ok","skipped","detail"}
    layers = review.get("layers")
    if not rep.add("sql", "review.layers 是 list", isinstance(layers, list),
                   "layers 类型=%s" % type(layers).__name__):
        return data
    rep.add("sql", "layers 共 %d 层" % EXPECTED_LAYER_COUNT, len(layers) == EXPECTED_LAYER_COUNT,
            "实际 %d 层：%s" % (len(layers), [l.get("layer") for l in layers
                                             if isinstance(l, dict)]))
    shape_ok = all(isinstance(l, dict)
                   and all(k in l for k in ("layer", "name", "ok", "skipped", "detail"))
                   for l in layers)
    rep.add("sql", "每层字段齐全(layer/name/ok/skipped/detail)", shape_ok,
            "字段不全的层=%s" % [l.get("layer") for l in layers
                                if isinstance(l, dict)
                                and not all(k in l for k in ("layer", "name", "ok", "skipped", "detail"))])
    for lid in REQUIRED_LAYERS_SQL:
        layer = next((l for l in layers if isinstance(l, dict) and l.get("layer") == lid), None)
        rep.add("sql", "门禁 %s 通过且未跳过" % lid,
                bool(layer and layer.get("ok") is True and layer.get("skipped") is False),
                (("%s / %s" % (layer.get("name"), brief(layer.get("detail"), 120)))
                 if layer else "layers 里没有 %s" % lid))
    rep.add("sql", "review.status == 通过", review.get("status") == "通过",
            "status=%r" % review.get("status"))
    return data


def stage_execute(rep, demand_id):
    """⑤ 只读执行：断言真实出数 + L5 结果断言实跑 + 写操作被拦。"""
    if not demand_id:
        rep.add("execute", "前置 demand_id", False, "① 未产出 demand_id，跳过本阶段")
        return None
    st, data, raw = http("POST", API + "/demand/%s/sql/execute" % demand_id,
                         {"dataset": DATASET, "actor": "e2e_verify"})
    if not rep.add("execute", "POST /sql/execute HTTP 200", st == 200,
                   "HTTP=%s %s" % (st, brief(raw, 200))):
        return None
    if not isinstance(data, dict):
        return rep.add("execute", "返回 JSON 对象", False, brief(raw)) and None

    rc = data.get("row_count")
    # 阈值不写死具体行数（随数据变化），只断言「真的出行数了」
    rep.add("execute", "row_count > 0（真实出数）",
            isinstance(rc, int) and rc > 0, "row_count=%r" % rc)
    rep.add("execute", "deliverable == true", data.get("deliverable") is True,
            "deliverable=%r reason=%r" % (data.get("deliverable"), data.get("deliverable_reason")))
    rep.add("execute", "adopted == true（结果被采纳）", data.get("adopted") is True,
            "adopted=%r" % data.get("adopted"))
    rep.add("execute", "sql_run_id 非空（已落库）", bool(data.get("sql_run_id")),
            "sql_run_id=%r" % data.get("sql_run_id"))
    rep.add("execute", "execution_summary 非空", bool(data.get("execution_summary")),
            "summary=%r" % data.get("execution_summary"))

    vr = data.get("validation_result") or {}
    if isinstance(vr, dict):
        cols = vr.get("columns") or []
        rep.add("execute", "validation_result.row_count 与 row_count 一致",
                vr.get("row_count") == rc, "validation=%r vs %r" % (vr.get("row_count"), rc))
        rep.add("execute", "validation_result.columns 非空", len(cols) > 0, "columns=%s" % cols)
        rows = data.get("sample_rows") or []
        rep.add("execute", "sample_rows 非空", len(rows) > 0, "样本行数=%d" % len(rows))
        if rows and cols:
            bad = [i for i, r in enumerate(rows)
                   if isinstance(r, (list, tuple)) and len(r) != len(cols)]
            rep.add("execute", "sample_rows 每行列数 == columns 数", not bad,
                    "列数不符的行号=%s（期望 %d 列）" % (bad, len(cols)))
        rep.add("execute", "validation_result.l5_ok == true", vr.get("l5_ok") is True,
                "l5_ok=%r" % vr.get("l5_ok"))

    # L5 在执行阶段必须**实跑**（生成阶段是skipped）
    layers = ((data.get("review") or {}).get("layers")) or []
    l5 = next((l for l in layers if isinstance(l, dict) and l.get("layer") == "L5"), None)
    rep.add("execute", "门禁 L5 结果断言实跑通过",
            bool(l5 and l5.get("ok") is True and l5.get("skipped") is False),
            ("%s / %s" % (l5.get("name"), brief(l5.get("detail"), 120))) if l5 else "review 里没有 L5")

    # 负向：写操作必须被 L3 只读校验拦下（证明门禁真的会拦）
    st2, _, raw2 = http("POST", API + "/demand/%s/sql/execute" % demand_id,
                        {"dataset": DATASET, "sql": "DELETE FROM ads_member_repurchase_di",
                         "actor": "e2e_verify"}, timeout=90)
    rep.add("execute", "负向：DELETE 被只读门禁拦截", st2 == 400 and "只读" in (raw2 or ""),
            "HTTP=%s %s" % (st2, brief(raw2, 200)))
    return data


def stage_persist(rep, demand_id):
    """元数据库落库核对：四动作的结果真的写进去了（复用 gateway/db.py，不自拼 DSN）。"""
    if not demand_id:
        rep.add("persist", "前置 demand_id", False, "① 未产出 demand_id，跳过本阶段")
        return
    try:
        import db  # noqa: PLC0415 —— 复用网关同一条连接，失败只报本阶段不中断
    except Exception as e:  # noqa: BLE001
        rep.add("persist", "导入 gateway/db.py", False, "%s: %s" % (type(e).__name__, e))
        return

    try:
        rounds = db.query(
            "SELECT max(round_no) AS rno, count(*) AS n FROM analysis_rounds WHERE demand_id=%s",
            (demand_id,))
        rno = (rounds[0] or {}).get("rno") if rounds else None
        rep.add("persist", "analysis_rounds 落库", bool(rno and rno >= 2),
                "max(round_no)=%r（首轮+重跑应>=2）" % rno)
    except Exception as e:  # noqa: BLE001
        rep.add("persist", "查询 analysis_rounds", False, "%s: %s" % (type(e).__name__, e))

    try:
        cs = db.query(
            "SELECT count(*) AS n FROM confirmations WHERE demand_id=%s AND answer IS NOT NULL",
            (demand_id,))
        n = (cs[0] or {}).get("n") if cs else 0
        rep.add("persist", "confirmations 有答复记录", bool(n and n >= 1), "答复条数=%r" % n)
    except Exception as e:  # noqa: BLE001
        rep.add("persist", "查询 confirmations", False, "%s: %s" % (type(e).__name__, e))

    try:
        rs = db.query("SELECT count(*) AS n FROM sql_runs WHERE demand_id=%s", (demand_id,))
        n = (rs[0] or {}).get("n") if rs else 0
        rep.add("persist", "sql_runs 落库", bool(n and n >= 1), "sql_runs 条数=%r" % n)
    except Exception as e:  # noqa: BLE001
        rep.add("persist", "查询 sql_runs", False, "%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Askoda MVP 四动作闭环 E2E 验证（G3）")
    ap.add_argument("--json", action="store_true", help="输出结构化结果（供 CI / 看板消费）")
    args = ap.parse_args()

    rep = Report()
    # 逐阶段跑，任一阶段整体崩了也继续跑下一阶段 —— 把问题一次报全
    def run(stage, fn, *args):
        try:
            return fn(rep, *args)
        except Exception as e:  # noqa: BLE001
            rep.add(stage, "阶段执行未抛异常", False, "%s: %s" % (type(e).__name__, e))
            return None

    run("health", stage_health)
    demand_id = run("submit", stage_submit)
    analysis = run("analyze", stage_analyze, demand_id)
    run("confirm", stage_confirm, demand_id, analysis)
    run("sql", stage_sql, demand_id)
    run("execute", stage_execute, demand_id)
    run("persist", stage_persist, demand_id)

    failed = rep.failed()
    total = len(rep.items)
    verdict = "全过" if not failed else "有红项（%d 项失败）" % len(failed)
    summary = "E2E 断言 %d 项，失败 %d 项" % (total, len(failed))

    if args.json:
        print(json.dumps({
            "ok": not failed,
            "total": total,
            "failed": len(failed),
            "verdict": verdict,
            "demand_id": demand_id,
            "bff_base": BFF_BASE,
            "dataset": DATASET,
            "assertions": rep.items,
        }, ensure_ascii=False, indent=2))
    else:
        print("")
        print("─" * 72)
        for it in failed:
            print("  红项：[%s] %s — %s" % (it["stage"], it["name"], it["detail"]))
        print("%s —— 总判定：%s" % (summary, verdict))

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
