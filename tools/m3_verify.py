# -*- coding: utf-8 -*-
"""M3 端到端验收：经 MCP 真实调用，逐条核验三条验收标准（可复跑）

核验什么
--------
M3 的验收标准有三条，本工具对 5 条业务样例逐条核验：

  C1 每条结论带来源与置信度
     六槽位的每个候选都必须有非空 `evidence`，且每条证据含 level / source / locator；
     `confidence` 落在 [0,1]。**无源断言直接判失败**——这是"可解释"的底线。

  C2 确认问题只含影响结果的问题，不含表名 / 字段名等技术问题
     这里**不信任服务端的闸门**，而是从 `wren_manifest` 现取物理表名与字段名，
     在客户端独立重扫一遍题干 + reason + impact + options。自己验自己等于没验。

  C3 确认记录版本化、不可覆盖
     实测三次答复：首次 → version+1；同样答复再答一次 → 应 noop；
     改答复 → version 再 +1 且 supersedes 指向上一版。最后核对历史行数与
     版本号是否连续递增——若哪一版被 UPDATE 覆盖，行数就对不上。

另附两项观察（不判失败，供人工复核）：
  - 各样例的规则触发是否符合其设计意图（must-contain 断言）
  - 证据源降级（`degraded_sources`）——非空即代表相应判定未执行

用法
----
    python tools/m3_verify.py                       # 默认 http://127.0.0.1:18080/mcp
    python tools/m3_verify.py --url http://... --dataset B --keep

    --keep   跑完不清理产生的需求单（默认清理，保持演示环境干净）

退出码：0 全部通过；1 有失败项
"""
import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))
sys.path.insert(0, HERE)

import example_demands  # noqa: E402

PROTOCOL_VERSION = "2025-03-26"

# 各样例设计意图对应的"必须触发"规则（must-contain，不要求精确相等——
# 多触发了别的规则是信息，不是错误）
EXPECT_RULES = {
    "S1": {"R1"},          # 多表 + 口语别名 + 排除条件
    "S2": {"R4", "R6"},    # 比率分母悬空
    "S3": {"R5"},          # 未治理口径
    "S4": {"R7"},          # 个人身份信息
    "S5": {"R1"},          # 跨表金额核对（一对多）
}

# SQL / 结构性技术词。物理表名字段名不写死在这里，一律从 wren_manifest 现取。
SQL_TECH_TERMS = [
    "SELECT", "FROM", "WHERE", "GROUP BY", "ORDER BY", "JOIN", "LEFT JOIN",
    "INNER JOIN", "UNION", "SUM(", "COUNT(", "AVG(", "MAX(", "MIN(",
    "MDL", "SQL", "主键", "外键", "字段名", "表名", "建表", "索引", "分区",
    "数据类型", "varchar", "bigint", "integer", "numeric", "timestamp", "jsonb",
]

FAILS = []
WARNINGS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("    \033[32mPASS\033[0m %s" % name)
    else:
        print("    \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)
    return cond


def warn(msg):
    WARNINGS.append(msg)
    print("    \033[33mWARN\033[0m %s" % msg)


# ---------------------------------------------------------------------------
# MCP over streamable-http（与服务端 wren.py 同一套握手约定）
# ---------------------------------------------------------------------------
class McpClient:
    def __init__(self, url, timeout=120):
        self.endpoint = url
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._sid = None
        self._lock = threading.Lock()
        self._id = 0

    def _rpc(self, payload, sid=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if sid:
            headers["Mcp-Session-Id"] = sid
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), headers=headers
        )
        resp = self._opener.open(req, timeout=self.timeout)
        new_sid = resp.headers.get("Mcp-Session-Id")
        body = resp.read().decode()
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), new_sid
        return None, new_sid

    def handshake(self):
        res, sid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "m3-verify", "version": "1.0.0"},
                },
            }
        )
        self._sid = sid
        if sid:
            try:
                self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except Exception:
                pass
        return res

    def call(self, tool, args=None, _retry=True):
        """调用工具并返回解析后的 dict（服务端约定：内容为 JSON 文本）。"""
        with self._lock:
            if self._sid is None:
                self.handshake()
            self._id += 1
            try:
                res, sid = self._rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": self._id,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": args or {}},
                    },
                    self._sid,
                )
                if sid:
                    self._sid = sid
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode()
                except Exception:
                    pass
                if _retry and ("session" in detail.lower() or e.code in (400, 404)):
                    self._sid = None
                    return self.call(tool, args, _retry=False)
                raise RuntimeError("MCP HTTP %s: %s" % (e.code, detail[:200]))
        if res is None:
            return None
        if "error" in res:
            raise RuntimeError(json.dumps(res["error"], ensure_ascii=False)[:300])
        result = res.get("result", {})
        txt = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(txt[:300] or "工具返回错误")
        try:
            return json.loads(txt)
        except Exception:
            return {"_raw": txt}

    def list_tools(self):
        with self._lock:
            if self._sid is None:
                self.handshake()
            self._id += 1
            res, sid = self._rpc(
                {"jsonrpc": "2.0", "id": self._id, "method": "tools/list", "params": {}},
                self._sid,
            )
            if sid:
                self._sid = sid
        return [t["name"] for t in (res or {}).get("result", {}).get("tools", [])]


# ---------------------------------------------------------------------------
# 技术词独立清单（用于 C2 的客户端复扫）
# ---------------------------------------------------------------------------
def build_tech_terms(cli, dataset):
    """从 wren_manifest 现取表名与字段名——与服务端内部清单相互独立。"""
    terms = set(SQL_TECH_TERMS)
    try:
        man = cli.call("wren_manifest", {"dataset": dataset})
        for m in man.get("models", []):
            if m.get("name"):
                terms.add(m["name"])
            for c in m.get("columns", []):
                if c.get("name"):
                    terms.add(c["name"])
    except Exception as e:  # noqa: BLE001
        warn("取 wren_manifest 失败，技术词清单不完整：%s" % str(e)[:120])
    return sorted(t for t in terms if t and len(t) >= 3)


def tech_hits(text, terms):
    """大小写不敏感 + 词边界，避免 `sum` 误伤 `summary` 这类子串。"""
    import re

    low = (text or "").lower()
    out = []
    for t in terms:
        pat = r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(t.lower())
        if re.search(pat, low):
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 三条验收标准
# ---------------------------------------------------------------------------
def verify_c1_sourced(sample_key, res):
    """C1：每条结论带来源与置信度。"""
    slots = res.get("slots") or {}
    missing_ev, bad_conf, n_cand = [], [], 0
    for sname, s in slots.items():
        for c in (s.get("candidates") or []):
            n_cand += 1
            ev = c.get("evidence") or []
            if not ev:
                missing_ev.append("%s=%s" % (sname, c.get("value")))
                continue
            for e in ev:
                if not (e.get("level") and e.get("source")):
                    missing_ev.append("%s=%s(证据缺 level/source)" % (sname, c.get("value")))
            conf = c.get("confidence")
            if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
                bad_conf.append("%s=%s -> %r" % (sname, c.get("value"), conf))
    check("%s C1a 槽位候选非空" % sample_key, n_cand > 0, "候选数=0")
    check(
        "%s C1b 每条结论都有来源（%d 条候选）" % (sample_key, n_cand),
        not missing_ev,
        "无源：%s" % missing_ev[:4],
    )
    check(
        "%s C1c 置信度均在 [0,1]" % sample_key,
        not bad_conf,
        "越界：%s" % bad_conf[:4],
    )
    # 证据链整体可溯源
    chain = res.get("evidence_chain") or []
    noloc = [i for i in chain if not i.get("locator")]
    check(
        "%s C1d 证据链每条可定位（%d 条）" % (sample_key, len(chain)),
        bool(chain) and not noloc,
        "无 locator 的证据 %d 条" % len(noloc),
    )
    return n_cand


def verify_c2_questions(sample_key, res, tech):
    """C2：确认问题不含技术问题（客户端独立复扫）。"""
    qs = res.get("questions") or []
    check("%s C2a 生成了待确认问题" % sample_key, len(qs) > 0, "问题数=0")

    leaks = []
    for q in qs:
        scanned = " ".join(
            [q.get("question") or "", q.get("reason") or "", q.get("impact_scope") or ""]
            + [str(o) for o in (q.get("options") or [])]
        )
        hit = tech_hits(scanned, tech)
        if hit:
            leaks.append("%s -> %s" % (q.get("question_id"), hit))
    check(
        "%s C2b 问题与选项均不含表名/字段名/SQL 词" % sample_key,
        not leaks,
        "泄漏：%s" % leaks[:3],
    )

    # 每条问题都得说清影响面、给出可选答案，否则业务无法作答
    no_impact = [q.get("question_id") for q in qs if not str(q.get("impact_scope") or "").strip()]
    no_opts = [q.get("question_id") for q in qs if not (q.get("options") or [])]
    check("%s C2c 每条问题都说明了影响面" % sample_key, not no_impact, "缺：%s" % no_impact)
    check("%s C2d 每条问题都给了可选答案" % sample_key, not no_opts, "缺：%s" % no_opts)

    dropped = res.get("dropped_questions") or []
    if dropped:
        warn("%s 有 %d 条问题被闸门拦下（已透明披露）：%s"
             % (sample_key, len(dropped),
                [d.get("question_id") for d in dropped][:4]))
    return qs


def verify_c3_versioning(cli, sample_key, demand_id, qs):
    """C3：确认记录版本化、不可覆盖（实测三次答复）。"""
    if not qs:
        check("%s C3 无问题可答，跳过" % sample_key, False, "问题数为 0，无法验证版本化")
        return
    qid = qs[0]["question_id"]

    before = cli.call("confirmation_list", {"demand_id": demand_id, "include_history": True})
    n_before = before.get("history_count", 0)
    cur = [i for i in before["items"] if i["question_id"] == qid][0]
    v0 = cur["version"]

    # 第 1 次：首次答复
    r1 = cli.call("confirmation_answer",
                  {"demand_id": demand_id, "question_id": qid,
                   "answer": "按业务单据逐条", "choice": "按业务单据逐条"})
    check("%s C3a 首次答复产生 version+1" % sample_key,
          r1.get("version") == v0 + 1, "v%s -> v%s" % (v0, r1.get("version")))
    check("%s C3b 首次答复 supersedes 指向原版本" % sample_key,
          bool(r1.get("supersedes")), "supersedes 为空")

    # 第 2 次：同样答复 → 应 noop，不新增版本
    r2 = cli.call("confirmation_answer",
                  {"demand_id": demand_id, "question_id": qid, "answer": "按业务单据逐条"})
    check("%s C3c 同样答复按 noop 处理，不堆版本" % sample_key,
          r2.get("noop") is True, "noop=%r（应 True）" % r2.get("noop"))

    # 第 3 次：改答复 → version 继续 +1
    v_before3 = r1.get("version")
    r3 = cli.call("confirmation_answer",
                  {"demand_id": demand_id, "question_id": qid, "answer": "按业务明细逐条"})
    check("%s C3d 改答复继续递增版本" % sample_key,
          r3.get("version") == v_before3 + 1, "v%s -> v%s" % (v_before3, r3.get("version")))
    check("%s C3e 新版本 supersedes 指向上一版" % sample_key,
          r3.get("supersedes") == r1.get("confirmation_id"),
          "%r vs %r" % (r3.get("supersedes"), r1.get("confirmation_id")))

    # 核对历史：行数只增不减，版本号连续，旧答复仍在
    after = cli.call("confirmation_list", {"demand_id": demand_id, "include_history": True})
    hist = [h for h in after.get("history", []) if h["question_id"] == qid]
    versions = sorted(h["version"] for h in hist)
    check("%s C3f 历史行数只增不减（%d -> %d）" % (sample_key, n_before, after.get("history_count", 0)),
          after.get("history_count", 0) > n_before,
          "历史未增长，疑似被覆盖")
    check("%s C3g 版本号连续无断档：%s" % (sample_key, versions),
          versions == list(range(versions[0], versions[0] + len(versions))),
          "断档：%s" % versions)
    kept = [h["answer"] for h in hist if h.get("answered")]
    check("%s C3h 历史答复全部保留（%d 条）" % (sample_key, len(kept)),
          "按业务单据逐条" in kept and "按业务明细逐条" in kept,
          "历史答复：%s" % kept)
    newest = sorted(hist, key=lambda h: h["version"])[-1]
    check("%s C3i 当前版本是最新答复" % sample_key,
          newest.get("answer") == "按业务明细逐条",
          "最新版答复=%r" % newest.get("answer"))

    # 事件留痕：三次用户动作（2 次变更 + 1 次同答复重申）都要有痕迹。
    # 只数 confirmation_answered 是不够的——它漏掉"同答复重申"这种不改变状态的
    # 动作，而那正是一次货真价实的用户操作，不留痕就等于审计链有破口。
    d = cli.call("demand_get", {"demand_id": demand_id})
    types = [e["event_type"] for e in (d.get("events") or [])]
    n_ans = types.count("confirmation_answered")
    n_re = types.count("confirmation_reaffirmed")
    check("%s C3j 三次答复动作全部留痕（变更 %d / 重申 %d）" % (sample_key, n_ans, n_re),
          n_ans >= 2 and n_re >= 1,
          "变更=%d（应≥2）、重申=%d（应≥1）" % (n_ans, n_re))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="M3 端到端验收")
    ap.add_argument("--url", default=os.getenv("GATEWAY_MCP_URL", "http://127.0.0.1:18080/mcp"))
    ap.add_argument("--dataset", default="B")
    ap.add_argument("--keep", action="store_true", help="保留产生的需求单")
    args = ap.parse_args()

    print("=" * 74)
    print("M3 端到端验收 —— 语义分析与确认闭环")
    print("端点：%s   数据集：%s" % (args.url, args.dataset))
    print("=" * 74)

    cli = McpClient(args.url)
    tools = cli.list_tools()
    print("\n[0] 工具面：共 %d 个工具" % len(tools))
    required = [
        "analysis_first_round", "analysis_get", "analysis_rounds", "analysis_evidence",
        "confirmation_generate", "confirmation_list", "confirmation_answer",
    ]
    missing = [t for t in required if t not in tools]
    check("M3 七个工具均已注册", not missing, "缺：%s" % missing)
    print("    已注册：%s" % "、".join(t for t in required if t in tools))

    tech = build_tech_terms(cli, args.dataset)
    print("    技术词独立清单：%d 条（取自 wren_manifest，非服务端内部清单）" % len(tech))

    made = []
    results = {}
    try:
        for s in example_demands.SAMPLES:
            key = s["key"]
            print("\n%s [%s]" % ("-" * 70, key))
            print("  意图：%s" % s["intent"])

            d = cli.call("demand_create", dict(s["payload"]))
            if not d.get("demand_id"):
                check("%s 建单成功" % key, False, json.dumps(d, ensure_ascii=False)[:200])
                continue
            did = d["demand_id"]
            made.append(did)
            print("  需求单：%s" % did)

            res = cli.call("analysis_first_round", {"demand_id": did, "dataset": args.dataset})
            if not res.get("ok"):
                check("%s first_round 成功" % key, False,
                      json.dumps(res, ensure_ascii=False)[:200])
                continue
            results[key] = res
            print("  轮次=%s  状态=%s  整体置信度=%s"
                  % (res.get("round_no"), res.get("demand_status"), res.get("confidence_overall")))

            # 规则触发是否符合设计意图
            risks = ((res.get("slots") or {}).get("risks") or {}).get("candidates") or []
            fired = {c.get("rule_id") for c in risks}
            for r in sorted(fired):
                pass
            want = EXPECT_RULES.get(key, set())
            print("  规则触发：%s（预期至少含 %s）"
                  % (sorted(x for x in fired if x), sorted(want)))
            check("%s 规则触发符合意图" % key, want.issubset(fired),
                  "只触发了 %s" % sorted(x for x in fired if x))

            deg = res.get("degraded_sources") or []
            if deg:
                warn("%s 证据源降级 %d 项：%s"
                     % (key, len(deg),
                        [(x["level"], x["severity"]) for x in deg]))

            verify_c1_sourced(key, res)
            qs = verify_c2_questions(key, res, tech)

            rnds = cli.call("analysis_rounds", {"demand_id": did})
            check("%s 轮次已落库（rounds=%s）" % (key, rnds.get("count")),
                  rnds.get("count", 0) >= 1, json.dumps(rnds, ensure_ascii=False)[:160])

            evd = cli.call("analysis_evidence", {"demand_id": did, "level": "P6"})
            check("%s 证据可按优先级过滤（P6 %d 条）" % (key, evd.get("count", 0)),
                  evd.get("count", 0) > 0, "P6 证据为 0")

            verify_c3_versioning(cli, key, did, qs)

        # 轮次只增不改：重跑一轮应产生 round_no=2
        if results:
            key = sorted(results)[0]
            did = [m for m in made][0]
            again = cli.call("analysis_first_round", {"demand_id": did, "dataset": args.dataset})
            print("\n%s 重跑同一需求（验证轮次只增不改）" % ("-" * 70))
            check("重跑产生 round_no=2（上一轮保留）",
                  again.get("round_no") == 2, "实际 round_no=%s" % again.get("round_no"))
            rnds = cli.call("analysis_rounds", {"demand_id": did})
            check("两轮并存可并排比对",
                  rnds.get("count") == 2, "轮次数=%s" % rnds.get("count"))
            acts = again.get("confirmation_actions") or {}
            check("重跑时已答问题不被重复提问",
                  acts.get("created", 0) == 0, "重复创建了 %s 条问题" % acts.get("created"))
    finally:
        if not args.keep and made:
            print("\n%s 清理演示数据" % ("-" * 70))
            for did in made:
                try:
                    r = cli.call("demand_set_status", {"demand_id": did, "status": "已退回",
                                                       "note": "M3 验收现场清理"})
                    print("    %s -> %s" % (did, r.get("status")))
                except Exception as e:  # noqa: BLE001
                    print("    %s 状态流转失败：%s" % (did, str(e)[:100]))
            print("    提示：彻底清理请用 tools/reset_demo_data.py --apply")

    print("\n" + "=" * 74)
    print("共 %d 项断言，失败 %d 项，提醒 %d 项" % (CHECKS[0], len(FAILS), len(WARNINGS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ %s" % f)
        print("\033[31m验收未通过\033[0m")
        return 1
    print("\033[32m三条验收标准全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
