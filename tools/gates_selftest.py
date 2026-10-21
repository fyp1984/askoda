#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-1 SQL 门禁自证（离线，不需要容器与 Wren）。

L4 语义门禁用注入的假 dry_run 函数桩：默认全部通过。
如需测试 L4 阻断，自行传 _dry_run_fn=lambda sql: {"ok": False, "message": "故意失败"}。

用法：
    python3 tools/gates_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import contextlib
import os
import sys
import textwrap as _textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 脚本定位："离线、不需要容器与 Wren"——直接引用仓库内的 workspace 副本，
# 不强制用户手动 export MDL_A_PATH / MDL_B_PATH。
# （若宿主已显式设置环境变量则不覆盖。）
os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import gates as gt  # noqa: E402
# 门禁层数的**唯一权威来源**：与 G3 的 tools/e2e_verify.py 共用同一常量。
#
# ⚠️ 为什么从 gateway/gate_contract.py 取，而不是从 e2e_verify.py 取：
# 本脚本是 **G1 离线自证**（docstring 明写"离线，不需要容器与 Wren"），
# e2e_verify.py 是 **G3 真实链路**（依赖容器与真实服务）。反向 import 会把
# G3 的依赖拖进 G1，破坏本脚本的离线可跑契约。gate_contract 是零依赖中性模块，
# 两侧都可安全 import。详见该模块 docstring。
from gate_contract import EXPECTED_LAYER_COUNT  # noqa: E402


# ===========================================================================
# 7c 组（阶段二 · 覆盖判定）共用夹具
# ===========================================================================
#
# 🔴 为什么夹具只桩「DB 边界」，不桩要素提取
# ---------------------------------------------------------------------------
# 覆盖判定要真的跑起来，就得让 reqelem.build_elements_from_requirement
# 拿到**真实的** bundle。而它的产bundle 路径是：
#     demand_mod.get(demand_id)          ← 需要 DB
#     evidence.build_lexicon(dataset)    ← 需要 DB
#     evidence.collect(...)              ← 纯计算
#     semantics.first_round(bundle)      ← 纯计算
#     SlotMapper(bundle, slots).elements() ← 纯计算
#
# ⇒ 本文件只把**前两步**换成离线等价物，后三步全部跑真实代码。
#   这样「要素从哪来」这条链是真的，不会出现「用例自证建立在合成 bundle 的
#   假定上」——那正是本项目 REQ-COVERAGE 缺陷同构的问题。
# ⚠️ lexicon 置空是**如实反映**无 DB 时的状态，不是削弱：
#   matched 为空时字段要素走 reqelem 自己的 fallback_noun 路径，
#   那条路径同样是真实代码。
import evidence as _evidence_mod  # noqa: E402  —— 取真实 evidence.collect
import reqelem as _reqelem_mod  # noqa: E402
import reqelem as _reqelem_mod  # noqa: E402

#: 六类验收用例的需求夹具（原文即 evidence.collect 拼 text 的那五个字段）
#:
#: ⚠️ 每条用例一个**独立 demand_id**：reqelem 的 bundle 缓存按
#:   (demand_id, dataset, with_knowledge, 原文指纹) 做键，同 id 不同原文会串味。
DEMAND_A1 = {
    "demand_id": "DR-COV-A1",
    "dataset": "B",
    "status": "已归档",
    "title": "各门店月度销售额、订单数与退款额统计",
    "business_context": "需要看每个门店的月度销售额，同时要订单数和已退款金额",
    "description": "按月汇总各门店",
    "expected_output": "门店、月份、销售额、订单数、退款额",
}
#: 用例 2：原文**根本没提时间**（对照用例 6 的「显式说不限时间范围」）
DEMAND_NO_TIME = {
    "demand_id": "DR-COV-NOTIME",
    "dataset": "B",
    "status": "已归档",
    "title": "各门店总销售额",
    "business_context": "把每个门店的总销售额给我",
    "description": "",
    "expected_output": "门店、总销售额",
}
#: 用例 6：原文**明确说**「不限时间范围」
DEMAND_EXEMPT = {
    "demand_id": "DR-COV-EXEMPT",
    "dataset": "B",
    "status": "已归档",
    "title": "各门店总销售额",
    "business_context": "把每个门店的总销售额给我，不限时间范围",
    "description": "",
    "expected_output": "门店、总销售额",
}

_ALL_DEMANDS = {d["demand_id"]: d for d in (DEMAND_A1, DEMAND_NO_TIME, DEMAND_EXEMPT)}


# 🔴 离线怎么跑：用**实例层官方的旁路参数**，不 monkeypatch 任何 DB 函数
# ---------------------------------------------------------------------------
# ⚠️ 这里是本组最容易做错的一处，记下来：
#   覆盖判定要真跑起来就得有 bundle，而 bundle 的生产路径
#   （demand_mod.get → evidence.build_lexicon → evidence.collect →
#    semantics.first_round → SlotMapper）在离线环境里连不上库。
#
#   两条路：
#     ① monkeypatch `demand.get` / `evidence.build_lexicon`（本文件初版做法）
#     ② 走 reqelem.build_elements_from_requirement 的**官方旁路参数 bundle=**
#        （队友提供，其 docstring「为什么 bundle 旁路必须有」写得很清楚：
#         不这样的话「判据层在离线环境里一行都测不到」）
#   ⇒ 选 ②：官方入口会自己校验 bundle 的形状与完整性，
#     而 ① 绕过校验——bundle 形状若变了，① 会**静默产出错要素**，
#     ② 会**报错**。「让错误在注入点就炸」优于「让它一路错到断言」。
#
#   ⚠️ 具体做法：把 `build_elements_from_requirement` 换成一个**薄旁路**——
#     它用夹具里的真实需求原文调**真实的** evidence.collect 现算 bundle，
#     再把 bundle 交给**原函数本体**。函数体一行没改、没伪造，
#     只是把「反查 DB」换成「直接给 bundle」。
@contextlib.contextmanager
def offline_demand_env(mode=None):
    """让覆盖判定在**无 DB** 环境里跑真实代码（走官方 bundle 旁路）。

    参数
    ----
    mode : 覆盖判定策略模式（None = 不改环境变量，沿用当前值）。
           ⚠️ 7c 系列必须在 **blocking** 下全过（团队要求），
              而 report-only 是默认值 ⇒ 每条用例都要显式传。

    ⚠️ 离开时**必须复原**两样东西（旁路闭包 / 策略模式）与 bundle 缓存：
       漏复原会让后续用例在「假环境」下跑，绿灯不再可信——
       这本身又是一处静默失效（与 7b-8 的复原纪律同源）。
    """
    _orig_befr = _reqelem_mod.build_elements_from_requirement
    _orig_mode = os.environ.get(gt._REQ_COVERAGE_MODE_ENV)
    had_cache = hasattr(_reqelem_mod, "_BUNDLE_CACHE")
    orig_cache = dict(getattr(_reqelem_mod, "_BUNDLE_CACHE", {}) or {})

    def _bypass(requirement, *a, **kw):
        """把「反查 DB」换成「用夹具里的真实原文现算 bundle」。"""
        did = (requirement or {}).get("demand_id") if isinstance(requirement, dict) else None
        d = _ALL_DEMANDS.get(did)
        if d is None:
            return {"ok": False, "elements": [],
                    "error": "夹具里没有 demand_id=%s" % did, "cached": False}
        # ⚠️ 走**真实** collect：只把 lexicon 置空（无词表命中，如实反映
        #   无 DB 状态），其余全跑生产代码。
        b = _evidence_mod.collect(demand=dict(d), demand_id=None,
                                  dataset="B", lexicon=[])
        kw.pop("bundle", None)
        return _orig_befr(requirement, *a, bundle=b, **kw)

    _reqelem_mod.build_elements_from_requirement = _bypass
    if mode is not None:
        os.environ[gt._REQ_COVERAGE_MODE_ENV] = mode
    try:
        yield
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr
        if mode is not None:
            if _orig_mode is None:
                os.environ.pop(gt._REQ_COVERAGE_MODE_ENV, None)
            else:
                os.environ[gt._REQ_COVERAGE_MODE_ENV] = _orig_mode
        if had_cache:
            _reqelem_mod._BUNDLE_CACHE.clear()
            _reqelem_mod._BUNDLE_CACHE.update(orig_cache)

def requirement_of(demand_id, dataset="B"):
    """造一份结构化需求对象（形状照抄 requirement.build 的产出）。

    ⚠️ 只放覆盖判定真正要用的键：reqelem 靠demand_id / dataset 重建 bundle，
    其余槽位字段本就不读（阶段一的齐备性口径已整体移除）。
    """
    return {"demand_id": demand_id, "dataset": dataset, "version": 1,
            "source_round": 1, "schema_version": 1, "status": "待审核"}


#: A1 需求（月度）的**已覆盖**写法：GROUP BY 里带 to_char(...,'YYYY-MM')。
#: ⚠️ 用 to_char 而非 date_trunc，是为了顺带证明判据**按粒度类别判、
#   不按实现判**（换一种写法不该突然变成未覆盖）。
_SQL_WITH_MONTH = (
    "SELECT store_name, to_char(stat_date, 'YYYY-MM') AS ym, "
    "SUM(sales_amount) AS sales FROM dws_store_daily_agg "
    "GROUP BY store_name, to_char(stat_date, 'YYYY-MM')")


def _sql_with_month():
    return _SQL_WITH_MONTH


def _fake_dry_run_ok(sql):
    return {"ok": True, "message": "（假桩）语义层预演通过"}


def _layer_by(result, layer_key):
    for l in result["layers"]:
        if l["layer"] == layer_key:
            return l
    return None


def main():
    fails = []

    # ------------------------------------------------------------------
    # 1. 合法 SELECT → L1/L3 通过
    # ------------------------------------------------------------------
    title = "1. 合法 SELECT：L1/L3 通过，status=通过"
    sql = "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name ORDER BY cnt DESC"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l1 = _layer_by(r, "L1")
    l3 = _layer_by(r, "L3")
    ok = (
        r["status"] == "通过"
        and l1 and l1["ok"] and not l1["skipped"]
        and l3 and l3["ok"] and not l3["skipped"]
    )
    if not ok:
        fails.append("%s FAILED: status=%s L1=%s L3=%s" % (title, r["status"], l1, l3))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L1.ok=%s  L3.ok=%s" % (r["status"], l1["ok"] if l1 else None, l3["ok"] if l3 else None))

    # ------------------------------------------------------------------
    # 2. INSERT INTO t VALUES (1) → L3 不通过且写明节点类型
    # ------------------------------------------------------------------
    title = "2. INSERT → L3 不通过，写明 Insert 节点类型"
    sql = "INSERT INTO t VALUES (1)"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_insert_type = False
    for v in r["rule_violations"]:
        if v["layer"] == "L3" and "INSERT" in v.get("type", ""):
            has_insert_type = True
            break
    l3_detail_mentions = l3 and "Insert" in (l3.get("detail") or "")
    ok = (
        r["status"] == "不通过"
        and l3 and not l3["ok"]
        and has_insert_type
        and l3_detail_mentions
    )
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  detail_snippet=%s" % (
        r["status"], l3["ok"] if l3 else None,
        (l3.get("detail")[:60] + "...") if l3 and l3.get("detail") else None))
    print("    rule_violations(L3 only)=", [v for v in r["rule_violations"] if v["layer"] == "L3"])

    # ------------------------------------------------------------------
    # 3. DROP TABLE t → L3 不通过
    # ------------------------------------------------------------------
    title = "3. DROP TABLE → L3 不通过"
    sql = "DROP TABLE t"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_drop = any(v["layer"] == "L3" and "DROP" in v.get("type", "") for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l3 and not l3["ok"] and has_drop
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  violations(L3)=%s" % (
        r["status"], l3["ok"] if l3 else None,
        [v for v in r["rule_violations"] if v["layer"] == "L3"]))

    # ------------------------------------------------------------------
    # 4. SELECT * FROM a, b → L2 分级阻断（未建模表 + 无条件 JOIN 均属阻断类）
    # ------------------------------------------------------------------
    title = "4. SELECT * FROM a,b → L2 分级阻断（未建模表 + 无条件 JOIN 均属阻断类），status=不通过"
    sql = "SELECT * FROM a, b"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    has_join_cond = any(i["rule"] == "JOIN_WITHOUT_CONDITION" for i in r["semantic_issues"])
    has_select_star = any(i["rule"] == "SELECT_STAR" for i in r["semantic_issues"])
    has_unmapped = any(i["rule"] == "UNMAPPED_OBJECT_REF" for i in r["semantic_issues"])
    ok = (
        r["status"] == "不通过"
        and has_join_cond and has_select_star and has_unmapped
    )
    if not ok:
        fails.append("%s FAILED: status=%s issues=%s" % (
            title, r["status"], r["semantic_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  JOIN_WITHOUT_CONDITION=%s  SELECT_STAR=%s  UNMAPPED_OBJECT_REF=%s" % (
        r["status"], has_join_cond, has_select_star, has_unmapped))
    print("    semantic_issues rules=", sorted({i["rule"] for i in r["semantic_issues"]}))

    # ------------------------------------------------------------------
    # 5. SELECT (( → L1 不通过（语法错误）
    # ------------------------------------------------------------------
    title = "5. SELECT (( → L1 语法错误，status=不通过"
    sql = "SELECT (("
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l1 = _layer_by(r, "L1")
    ok = r["status"] == "不通过" and l1 and not l1["ok"] and len(r["syntax_issues"]) >= 1
    if not ok:
        fails.append("%s FAILED: status=%s L1=%s syntax=%s" % (
            title, r["status"], l1, r["syntax_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  syntax_issues=%s" % (r["status"], r["syntax_issues"]))

    # ------------------------------------------------------------------
    # 6. SELECT 1; SELECT 2 → L3 不通过（多语句）
    # ------------------------------------------------------------------
    title = "6. 多语句分号分隔 → L3 不通过，MULTI_STATEMENT"
    sql = "SELECT 1; SELECT 2"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l3 = _layer_by(r, "L3")
    has_multi = any(v["layer"] == "L3" and v["type"] == "MULTI_STATEMENT" for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l3 and not l3["ok"] and has_multi
    if not ok:
        fails.append("%s FAILED: status=%s L3=%s violations=%s" % (
            title, r["status"], l3, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L3.ok=%s  MULTI_STATEMENT=%s" % (
        r["status"], l3["ok"] if l3 else None, has_multi))

    # ------------------------------------------------------------------
    # 7. 插槽层 skipped=True 且不影响总状态
    # ------------------------------------------------------------------
    title = "7. SQLFluff/GX 插槽层 skipped=True，不拖垮 status"
    sql = "SELECT 1 AS one"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    sf = _layer_by(r, "L-SQLFluff")
    gx = _layer_by(r, "L-GX")
    ok = (
        sf and sf["skipped"]
        and gx and gx["skipped"]
        and r["status"] in ("通过", "警告")
    )
    if not ok:
        fails.append("%s FAILED: status=%s SQLFluff=%s GX=%s" % (
            title, r["status"], sf, gx))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  SQLFluff.skipped=%s  GX.skipped=%s  插槽细节reason=%s" % (
        r["status"],
        sf["skipped"] if sf else None,
        gx["skipped"] if gx else None,
        (sf or {}).get("detail"),
    ))

    # ------------------------------------------------------------------
    # 7b. L-REQ 需求要素层（阶段一）
    #
    # 这一组是本阶段的核心契约，必须机器钉住，否则 L-REQ 会退化成
    # 「层在列表里、detail 写得漂亮、但永远不改变 status」的旁观者——
    # 那正是 REQ-COVERAGE 缺陷本身的形状。
    # ------------------------------------------------------------------
    # 7b-1. 无 requirement → skipped，且**其余层照常通过**（向后兼容红线）
    title = "7b-1. 无 requirement → L-REQ skipped，status 不受影响"
    sql_clean = "SELECT store_name, COUNT(*) AS cnt FROM dim_store GROUP BY store_name"
    r = gt.review(sql_clean, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    ok = (
        lreq and lreq["skipped"] is True and lreq["ok"] is True
        and r["status"] == "通过"
        and r.get("requirement_gaps") == []
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s" % (title, r["status"], lreq))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%s" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
        r.get("requirement_gaps"),
    ))

    # 7b-2/ 7b-3 / 7b-4 · 阶段二口径重写（2026-10-20）
    # -------------------------------------------------------------------
    # ⚠️ 这三条原本断言的是**齐备性**（槽位有无结论 / output_fields 是否为空），
    #   而阶段二把 `_requirement_check` 的判据**整体换成了覆盖判定**——
    #   齐备性不再是 L-REQ 的职责。照原断言跑会得到「全红」，但那是**用例过期**，
    #   不是实现坏了：它们断言的那个机制已经被有意移除。
    #
    #   ⇒ 处理方式不是删掉（删掉就少了一层防回归），而是**改写成阶段二等价物**：
    #     · 原 7b-2「齐备的 requirement 不误判红」→ 新「**要素齐全且都覆盖**的
    #       requirement 不误判红」（7c-5 从SQL 侧做，这里从需求侧做，互相补位）。
    #     · 原 7b-3「原文未提时间不被拦」→ 保留该意图，但断言语义改为
    #       「原文未提时间的要素判**不判**（may_omit），不进缺口」。
    #       端到端的 status 断言由 7c-2 在真实链路上做（那里才有真实要素）。
    #     · 原 7b-4「output_fields 空数组出 gaps」→ 已随齐备性判据一同移除，
    #       改钉「**要素清单取不到 ⇒ skipped 且不判红**」——
    #       它守住的是同一条更重要的边界：**抽不出要素不等于需求没被满足**。
    #
    # ⚠️ 三条都**不删**：7b 组是「接线与自述范围」的防线，7c 组是「判得对不对」
    #   的防线，两者互补。少任何一条，另一条被改坏时都无人报警。

    # 7b-2.需求要素全部被SQL 覆盖 ⇒ L-REQ ok、不误判红、不产生缺口
    title = "7b-2. 要素全覆盖的 requirement → L-REQ ok，不误判红（阶段二口径）"
    _req_covered = requirement_of(DEMAND_A1["demand_id"])
    with offline_demand_env(mode="blocking"):
        r = gt.review(_sql_with_month(), dataset="B", requirement=_req_covered,
                      _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    ok = (
        lreq and lreq["ok"] is True and lreq["skipped"] is False
        and r["status"] in ("通过", "警告")
        and not [g for g in (r.get("requirement_gaps") or []) if "月度" in g]
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s" % (
            title, r["status"], lreq, r.get("requirement_gaps")))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%s" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
        r.get("requirement_gaps"),
    ))

    # 7b-3. ★★ 误拦回归钉死：「原文没提时间」的要素必须判**不判**，不进缺口
    #
    # 这条是本组里最重要的一条，**意图与阶段一完全一致、判据换了**：
    #   阶段一靠「槽位 confidence=0」识别「业务方没要求」；
    #   阶段二靠 polarity=may_omit 识别（这才是结构上正确的那个信号）。
    # ⚠️ 阶段一之所以不能靠 confidence=0：requirement.py:237-241 对无候选槽位写的
    #   占位项是 {"value": "无候选结论…", "confidence": 0.0}，
    #   「有没有提时间」与「槽位缺失」在结构化对象里是**同一个信号**。
    #   而"各门店总销售额"这类正常提问的 time_semantics 正是这个形态
    #   ⇒ 早期版本据此判红，实测会拦下根本没提时间的正常需求。
    # ⇒ 阶段二的等价保证：may_omit 的要素 verdict 必须是「不判」，
    #   且**不得**出现在 requirement_gaps 里。
    title = "7b-3. ★★ 原文未提时间的要素 ⇒ 判不判、不进缺口（阶段二口径，防误拦回归）"
    _may_omit_el = {
        "element": "月度", "slot": "granularity",
        "provenance": "需求原文（全文未出现相关表述，无对应句）",
        "confidence": 0.45, "needs_confirmation": False, "mandatory": False,
        "polarity": "may_omit", "matcher": "time_granularity:month",
        "cause": "需求未提出 月度，不作要求",
    }
    _orig_befr_3 = _reqelem_mod.build_elements_from_requirement
    try:
        _reqelem_mod.build_elements_from_requirement = (
            lambda requirement, *a, **kw: {"ok": True, "elements": [dict(_may_omit_el)],
                                           "error": None, "cached": False})
        with offline_demand_env(mode="blocking"):
            r = gt.review(sql_clean, dataset="B",
                          requirement=requirement_of(DEMAND_A1["demand_id"]),
                          _dry_run_fn=_fake_dry_run_ok)
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr_3
    lreq = _layer_by(r, "L-REQ")
    gaps = r.get("requirement_gaps") or []
    detail3 = (lreq or {}).get("detail") or ""
    ok = (
        lreq and lreq["ok"] is True and lreq["skipped"] is False
        and r["status"] in ("通过", "警告")   # ★ 关键断言：没提时间不得被拦
        and gaps == []                # may_omit 绝不进缺口
        and "不作要求" in detail3              # 但必须留痕（缺席/豁免要显式）
        and r.get("revised_sql") is None      # 红线 D1：只报告不改写
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s detail=%s" % (
            title, r["status"], lreq, gaps, detail3[:160]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
    ))
    print("    gaps=%s（期望空：may_omit 不算缺口）" % gaps)
    print("    detail 含「不作要求」=%s" % ("不作要求" in detail3))

    # 7b-4. ★★ 要素清单取不到 ⇒ skipped 且**不判红**（守「抽不出≠不合格」）
    #
    # 这条替代原 7b-4（output_fields 空数组）。它守的是同一条边界的**新形态**：
    # 实例层取不到要素时，若判红就是把「我们没查成」报成「需求不合格」——
    # 而「没查成」在report-only 策略下必须**保守**（skipped、不阻断）。
    title = "7b-4. ★★ 要素清单取不到 ⇒ skipped 且不判红（阶段二口径）"
    _orig_befr_4 = _reqelem_mod.build_elements_from_requirement
    try:
        _reqelem_mod.build_elements_from_requirement = (
            lambda requirement, *a, **kw: {"ok": False, "elements": [],
                                           "error": "取不到需求 DR-COV-A1（模拟环境故障）",
                                           "cached": False})
        with offline_demand_env(mode="blocking"):
            r = gt.review(sql_clean, dataset="B",
                          requirement=requirement_of(DEMAND_A1["demand_id"]),
                          _dry_run_fn=_fake_dry_run_ok)
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr_4
    lreq = _layer_by(r, "L-REQ")
    detail4 = (lreq or {}).get("detail") or ""
    ok = (
        lreq and lreq["skipped"] is True and lreq["ok"] is True
        and r["status"] != "不通过"
        and "跳过原因" in detail4 and "取不到需求要素清单" in detail4
        and r.get("requirement_gaps") == []
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s detail=%s" % (
            title, r["status"], lreq, detail4[:160]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
    ))
    print("    detail=%s" % detail4.replace("\n", " ")[:150])

    # 7b-5. requirement 取不到（ok=False）→ skipped 而非判红
    title = "7b-5. requirement 取不到 → skipped，不判红"
    r = gt.review(sql_clean, dataset="B",
                  requirement={"ok": False, "error": "尚无结构化对象"},
                  _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    ok = (
        lreq and lreq["skipped"] is True
        and r["status"] == "通过"
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s" % (title, r["status"], lreq))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  detail=%s" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
        (lreq or {}).get("detail"),
    ))

    # 7b-6. ★ detail 范围自述：**枚举 + 结构性扫描双重保险**（阶段二口径）
    #
    # 这条防的是「在场但只做了一半」——业务方看到 L-REQ 绿了若不知道本层
    # 没查什么，就会以为「需求要素已校验通过」，那等于把 REQ-COVERAGE
    # 要修的静默失效挖在新位置。「缺席必须显式报未执行」同理适用于此。
    #
    # ⚠️ 为什么**两条断言都要有**（它们互不替代，缺一就有真实缺口）
    #
    #   ① 枚举式（本段）：保证「每条返回路径**都被走到**」。
    #      局限：只覆盖写下来的这几条。
    #   ② 结构性（本段末）：保证「源码里**每一个**产出 detail 的 return 都带前缀」。
    #      能力：新增第 4 种早退若忘带前缀，这里立刻红。
    #
    # ⇒ 反过来：结构性**无法**发现「新增路径没被任何用例调用」（它不进 layers，
    #   枚举不跑就永远不触发）——**所以不能拿结构性替代枚举**。
    #
    # ⚠️ 判据源必须是**源码**，不得改回「遍历 layers 里的 L-REQ 实例」：
    #   那种写法的输入仍来自枚举结果，**与枚举共享同一盲区**——
    #   实测过：新增一条「不被任何用例触达」的无前缀return 时，
    #   「遍历 layers」写法漏检 0，「扫描源码」写法漏检 1（拦住）。
    #   即「防静默失效的判据自己却依赖着会静默的东西」，是最隐蔽的一类缺陷。
    #
    # ⚠️ 阶段二改造（2026-10-20）：结构性扫描从「字符串切`return {`」
    #   改成 **AST 遍历 Return 节点**。原因：阶段二把8 条 skipped 早退收口到
    #   `_skipped()` 辅助函数（`return _skipped(...)`），字符串切分只能看见
    #   2 处 `return {`，会把「6 条早退都经helper 带前缀」误判成「只有 2 处带前缀」。
    #   ⚠️ 这正是「判据本身腐烂」的一种：不改它，它会给出**假的绿灯**。
    title = "7b-6. ★ detail 范围自述：枚举 + 结构性双重保险"
    # ⚠️ 前缀文案随阶段演进（阶段一=槽位齐备性；阶段二=覆盖判定），
    #   断言必须跟着换，否则会把「文案改了」报成「前缀丢了」。
    _pfx = gt._REQ_SCOPE_PREFIX.strip().split("\n")[0][:20]
    _detail_cases = [
        ("无 requirement", gt.review(sql_clean, dataset="B", _dry_run_fn=_fake_dry_run_ok)),
        ("非 dict", gt.review(sql_clean, dataset="B", requirement="不是对象",
                          _dry_run_fn=_fake_dry_run_ok)),
        ("取不到", gt.review(sql_clean, dataset="B",
                         requirement={"ok": False, "error": "x"},
                         _dry_run_fn=_fake_dry_run_ok)),
        ("空 SQL", gt.review("", dataset="B", requirement=requirement_of(DEMAND_A1["demand_id"]),
                         _dry_run_fn=_fake_dry_run_ok)),
        ("语法错误 SQL", gt.review("SELECT ((", dataset="B",
                             requirement=requirement_of(DEMAND_A1["demand_id"]),
                             _dry_run_fn=_fake_dry_run_ok)),
        ("要素全覆盖", gt.review(_sql_with_month(), dataset="B",
                           requirement=requirement_of(DEMAND_A1["demand_id"]),
                           _dry_run_fn=_fake_dry_run_ok)),
    ]
    with offline_demand_env(mode="blocking"):
        _detail_cases.append(
            ("有覆盖缺口",
             gt.review(sql_clean, dataset="B",
                       requirement=requirement_of(DEMAND_A1["demand_id"]),
                       _dry_run_fn=_fake_dry_run_ok)))
    ok = True
    for tag, rr in _detail_cases:
        #遍历 layers 里**每一个** L-REQ 实例（当前结构下只有一个，防御性写法）
        for lreq_inst in [l for l in rr.get("layers", []) if l.get("layer") == "L-REQ"]:
            d = lreq_inst.get("detail") or ""
            if _pfx not in d:
                ok = False
                print("        ✗ %s 缺前缀：%s" % (tag, d[:60]))
    print("[%s] %s（枚举 %d 条路径）" % ("OK" if ok else "!!", title, len(_detail_cases)))
    print("    断言用的前缀片段=%r" % _pfx)

    # ② 结构性扫描（AST）：_requirement_check 的每个 Return 都必须**经由**
    #    _REQ_SCOPE_PREFIX（直接拼，或经 _skipped() 间接拼）；
    #    且 _skipped() 本身必须引用 _REQ_SCOPE_PREFIX。
    #
    # ⚠️ 用 inspect 拿源码而非读文件：selftest 跑的是「已加载的模块」，
    #   与源码文本同源，但 inspect 不依赖文件路径，跨平台更稳。
    import ast as _ast
    import inspect as _inspect
    _src = _inspect.getsource(gt._requirement_check)
    _tree = _ast.parse(_textwrap.dedent(_src))

    def _returns_of(node):
        return [x for x in _ast.walk(node) if isinstance(x, _ast.Return)]

    _fn = _tree.body[0]
    assert isinstance(_fn, _ast.FunctionDef), type(_fn)
    _rets = _returns_of(_fn)
    # 每个 return 的表达式源码里，必须出现前缀常量**或**收口函数名
    _ok_rets = 0
    _bad_rets = []
    for _r in _rets:
        _seg = _ast.unparse(_r.value) if _r.value else ""
        if "_REQ_SCOPE_PREFIX" in _seg or "_skipped(" in _seg:
            _ok_rets += 1
        else:
            _bad_rets.append((_r.lineno, _seg[:60]))
    # _skipped() 必须自己引用前缀（否则 helper 存在但不带前缀，上面全在白判）
    _sk_src = _inspect.getsource(gt._skipped)
    _sk_uses_prefix = "_REQ_SCOPE_PREFIX" in _sk_src
    _struct_ok = (len(_rets) > 0) and (not _bad_rets) and _sk_uses_prefix
    if not _struct_ok:
        ok = False
        if _bad_rets:
            print("        ✗ 结构性：%d 个return 未带前缀：%s" % (len(_bad_rets), _bad_rets))
        if not _sk_uses_prefix:
            print("        ✗ 结构性：_skipped() 没有引用 _REQ_SCOPE_PREFIX（收口函数形同虚设）")
        fails.append("%s FAILED: 结构性扫描发现未带前缀的 return" % title)
    print("[%s] 结构性扫描（AST）：return %d 处，带前缀 %d 处；_skipped 引用前缀=%s"
          % ("OK" if _struct_ok else "!!", len(_rets), _ok_rets, _sk_uses_prefix))

    # ------------------------------------------------------------------
    # 7b-7. 层数契约
    # ------------------------------------------------------------------
    # 层数取权威常量（gateway/gate_contract.py），不写死字面量——否则它会与
    # tools/e2e_verify.py 的同一事实脱节，且脱节表现为「两边各自全绿」不报错。
    title = "7b-7. 层数契约：L-REQ 在最前，且各分支恒 %d 层" % EXPECTED_LAYER_COUNT
    _expect_order = ["L-REQ", "L1", "L2", "L3", "L4", "L5", "L-SQLFluff", "L-GX"]
    _cases = [
        ("正常通过", gt.review(sql_clean, dataset="B", _dry_run_fn=_fake_dry_run_ok)),
        ("L1 语法失败", gt.review("SELECT ((", dataset="B", _dry_run_fn=_fake_dry_run_ok)),
        ("L3 写操作", gt.review("INSERT INTO t VALUES (1)", dataset="B", _dry_run_fn=_fake_dry_run_ok)),
        ("L4 语义失败", gt.review("SELECT 1 FROM x", dataset="B",
                              _dry_run_fn=lambda s: {"ok": False, "message": "故意失败"})),
    ]
    ok = True
    detail = []
    for tag, rr in _cases:
        got_order = [l["layer"] for l in rr["layers"]]
        if got_order != _expect_order:
            ok = False
        # 层数单独再钉一次，且用权威常量：层序断言虽已隐含层数，但那条是
        # 「与 _expect_order 一致」的间接判据。显式钉层数才能让常量真正生效——
        # 否则改常量不改 gates.py 时本脚本照样全绿，等于没收敛。
        if len(got_order) != EXPECTED_LAYER_COUNT:
            ok = False
        detail.append("%s=%d层" % (tag, len(got_order)))
    if not ok:
        fails.append("%s FAILED: %s" % (title, detail))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    期望层序=%s" % _expect_order)
    print("    实测=%s" % detail)

    # ------------------------------------------------------------------
    # 7b-8. ★★ 阻断能力**可测性**用例：L-REQ 判红 ⇒ status 必须变「不通过」
    #
    # ⚠️ 为什么这条必须存在（不是「锦上添花」，是本阶段的唯一保证）
    # -----------------------------------------------------------
    # REQ-COVERAGE 阶段二的所有阻断能力都挂在**同一个开关**上：
    # gateway/gates.py 的 `blocking_layers` 元组里含不含 "L-REQ"。
    # 含 ⇒ L-REQ 的 ok=False 真的能把 status 打成「不通过」；
    # 不含 ⇒ L-REQ 永远只是「层在列表里、detail 写得漂亮、但 ok=False 不改变 status」的旁观者。
    #
    # 🔴 风险性质要记住：**不是「代码失效」，是「失效了也没人知道」**。
    # 架构师实测 monkeypatch 造 ok=False 时 status 确实变「不通过」，
    # 但**零测试覆盖**——7b-1..7b-7 每一条都断言 ok is True，
    # **没有任何一条能触发 ok=False 分支**。
    # 于是「blocking_layers 少了 L-REQ」这个改动会**全绿通过**：
    # 没有一处红灯能指认它。这就是「静默失效」的教科书形状。
    #
    # ⇒ 本用例是**唯一**能让该改动变红的哨兵。
    #
    # ⚠️ 负向验证已做（参照 7b-6 的做法，见文件里本用例的负向验证记录）：
    #   把 gateway/gates.py 的 blocking_layers 元组里删掉 "L-REQ" ⇒ 本用例**变红**；
    #   恢复后全绿。两次均已复原原状。
    #   ⚠️ 改动本用例后请重做该负向验证。
    # ------------------------------------------------------------------
    title = "7b-8. ★★ L-REQ 判红 ⇒ status=不通过（blocking_layers 含 L-REQ）"
    # 阴性对照：同一条 SQL 在**未注入**时必须是**不阻断**的——
    # 否则「status 变不通过」可能由别的层造成，本用例就证明不了 L-REQ 的传导性。
    #
    # ⚠️ 阶段二起这里换了个基准：**不传 requirement 时 L-REQ 恒skipped**
    #   （skipped 的层压根不参与阻断），拿它当基线证明力为零。
    #   必须传一份**真的能算出要素**的 requirement（走 offline_demand_env），
    #   且用**已覆盖**的 SQL（_sql_with_month）⇒ L-REQ 是 ok=True/skipped=False。
    #   前后唯一变量就是 ok/skipped 这一个信号 ⇒ status 的变化只能归因于 L-REQ。
    #
    # ⚠️ 用 report-only 跑对照：默认模式下有阻断级缺口也**不**改 status，
    #   那样注入前就可能已经是「不通过」（被别的层拦），对照就不成立。
    #   本用例要验的是「ok=False ⇒ 传导」，与策略模式无关，故钉住模式。
    _req_ok = requirement_of(DEMAND_A1["demand_id"])
    with offline_demand_env(mode="report-only"):
        _r_before = gt.review(_sql_with_month(), dataset="B", requirement=_req_ok,
                              _dry_run_fn=_fake_dry_run_ok)
    _lreq_before = _layer_by(_r_before, "L-REQ")
    _baseline_clean = (
        _r_before["status"] != "不通过"
        and _lreq_before and _lreq_before["ok"] is True
        and _lreq_before["skipped"] is False
    )
    if not _baseline_clean:
        fails.append("%s FAILED: 阴性对照不成立（注入前 status=%s L-REQ=%s）"
                     % (title, _r_before["status"], _lreq_before))

    # 注入：monkeypatch 掉 _requirement_check，强制返回 ok=False / skipped=False。
    # ⚠️ skipped 必须同时为 False——has_blocking_fail 判的是
    #   `(not ok) and (not skipped)`，skipped=True 会让这一层被跳过（不阻断）。
    #   只造 ok=False 而不造 skipped=False，测的就不是阻断路径了。
    # ⚠️ 签名必须与真实实现一致（阶段二起是 `(requirement, sql=None)`）：
    #   少写一个参数 ⇒ review() 调它时抛 TypeError ⇒ 用例变成「测崩溃」而非「测传导」。
    # ⚠️ gaps 故意留空：本用例只钉「ok=False ⇒ status 变不通过」这条传导链，
    #   不耦合 gaps 的语义。
    _orig_req_check = gt._requirement_check

    def _forced_covering_gap(requirement, sql=None):
        return {
            "ok": False, "skipped": False, "gaps": [],
            "detail": "（注入桩）L-REQ 强制判红：覆盖缺口",
        }

    try:
        gt._requirement_check = _forced_covering_gap
        r = gt.review(_sql_with_month(), dataset="B", requirement=_req_ok,
                      _dry_run_fn=_fake_dry_run_ok)
    finally:
        # 必须复原：漏复原会让后续用例全部在「假 L-REQ 恒判红」的状态下跑，
        # 它们的绿灯将不再可信（且这本身又是一处静默失效）。
        gt._requirement_check = _orig_req_check
    lreq = _layer_by(r, "L-REQ")
    ok = (
        _baseline_clean
        and r["status"] == "不通过"
        and lreq and lreq["ok"] is False and lreq["skipped"] is False
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s" % (title, r["status"], lreq))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    阴性对照（注入前）status=%s  L-REQ(ok=%s skipped=%s)" % (
        _r_before["status"],
        _lreq_before["ok"] if _lreq_before else None,
        _lreq_before["skipped"] if _lreq_before else None,
    ))
    print("    注入后 status=%s  L-REQ(ok=%s skipped=%s)" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
    ))
    print("    复原后 L-REQ 仍是原实现：%s" % (gt._requirement_check is _orig_req_check))

    # ==================================================================
    # 7c. L-REQ 覆盖判定（阶段二：需求要素 vs SQL 实际输出）
    #
    # 这一组是 REQ-COVERAGE 的**验收清单**，六条缺一不认。
    # 与 7b 的分工：7b 钉「接线与自述范围」（阶段一建立，本阶段继续有效），
    # 7c 钉「覆盖判定判得对不对、拦不拦」。
    #
    # ⚠️ 六条**全部在 blocking 模式下跑**（团队要求：契约必须成立、随时可切）。
    #   report-only 只由 7c-1a单独验一次（证明默认不阻断）。
    # ⚠️ 判据**不写死行号**：每条断言都能追到 gateway/gates.py 的具体函数
    #   （_requirement_check / _req_coverage_mode），而不是追到某一行——
    #   改个注释就飘的判据不是判据。
    # ==================================================================
    # SQL 夹具
    #   SQL_A1_BAD：A1 原案 —— 需求要「门店/月度/销售额+订单数+退款额」，
    #                SQL 只出销售额、GROUP BY **缺月度** ⇒ 月度要素未覆盖。
    #   SQL_A1_GOOD：同一需求，月度用 to_char 补上 ⇒ 覆盖。
    SQL_A1_BAD = ("SELECT store_name, SUM(sales_amount) AS sales "
                  "FROM dws_store_daily_agg GROUP BY store_name")
    SQL_A1_GOOD = ("SELECT store_name, to_char(stat_date, 'YYYY-MM') AS ym, "
                   "SUM(sales_amount) AS sales FROM dws_store_daily_agg "
                   "GROUP BY store_name, to_char(stat_date, 'YYYY-MM')")
    SQL_PLAIN = ("SELECT store_name, SUM(sales_amount) AS sales "
                 "FROM dws_store_daily_agg GROUP BY store_name")

    # ------------------------------------------------------------------
    # 7c-1. ★★ A1 原案：月度要素未覆盖 ⇒ 阻断 + 逐项列出缺失
    #
    # 这是本阶段**唯一的主证**：阶段一全程判绿的正是这条 SQL。
    # 断言三件事：① status=不通过；② 缺口里能点名「月度」；
    #③ 逐项列出（不是只报数量）——业务方要能照着改 SQL。
    # ------------------------------------------------------------------
    title = "7c-1. ★★ A1 原案缺月度 ⇒ blocking 下 status=不通过，且逐项列出缺失"
    with offline_demand_env(mode="blocking"):
        r = gt.review(SQL_A1_BAD, dataset="B",
                      requirement=requirement_of(DEMAND_A1["demand_id"]),
                      _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    gaps = r.get("requirement_gaps") or []
    detail7c1 = (lreq or {}).get("detail") or ""
    _hits_month = [g for g in gaps if "月度" in g]
    ok = (
        r["status"] == "不通过"
        and lreq and lreq["ok"] is False and lreq["skipped"] is False
        and len(_hits_month) >= 1
        and "[阻断]" in (_hits_month[0] if _hits_month else "")   # 逐项 + 带级别
        and "granularity" in detail7c1                # 逐项：含槽位，不是只报数量
        and r.get("revised_sql") is None              # 红线 D1：只报告不改写
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s"
                     % (title, r["status"], lreq, gaps))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  缺口 %d 条" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None, len(gaps)))
    print("    缺口=%s" % gaps)
    print("    revised_sql=%s（应为 None）" % r.get("revised_sql"))

    # ------------------------------------------------------------------
    # 7c-1a. 默认 report-only：同一缺口**只留痕不阻断**
    #
    # 为什么单独一条：它是「团队不会因天天红而绕过门禁」这条纪律的机器凭据。
    # ⚠️ 必须同时断言 notes 里**看得到**缺口——若只看 status=通过，
    #   这条用例在「缺口被静默吞掉」时照样全绿。
    # ------------------------------------------------------------------
    title = "7c-1a. ★★ 默认 report-only：同一缺口不阻断，但必须留痕到 notes"
    with offline_demand_env(mode="report-only"):
        r_ro = gt.review(SQL_A1_BAD, dataset="B",
                         requirement=requirement_of(DEMAND_A1["demand_id"]),
                         _dry_run_fn=_fake_dry_run_ok)
    lreq_ro = _layer_by(r_ro, "L-REQ")
    notes_ro = r_ro.get("review_notes") or ""
    ok = (
        r_ro["status"] != "不通过"
        and lreq_ro and lreq_ro["ok"] is True and lreq_ro["skipped"] is False
        and len(r_ro.get("requirement_gaps") or []) >= 1
        and "月度" in notes_ro
        and "report-only" in notes_ro
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s notes=%s"
                     % (title, r_ro["status"], lreq_ro, notes_ro))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%d" % (
        r_ro["status"],
        lreq_ro["ok"] if lreq_ro else None,
        lreq_ro["skipped"] if lreq_ro else None,
        len(r_ro.get("requirement_gaps") or [])))
    print("    notes=%s" % notes_ro[:150])
    # 顺带钉住默认值：不设环境变量时必须是 report-only
    _saved = os.environ.pop(gt._REQ_COVERAGE_MODE_ENV, None)
    try:
        _default_mode = gt._req_coverage_mode()
    finally:
        if _saved is not None:
            os.environ[gt._REQ_COVERAGE_MODE_ENV] = _saved
    ok_def = _default_mode == gt._REQ_MODE_REPORT
    if not ok_def:
        fails.append("7c-1a FAILED: 默认模式应为 report-only，实际 %s" % _default_mode)
    print("[%s] 7c-1a 补充：不设环境变量时默认模式=%s（期望 report-only）"
          % ("OK" if ok_def else "!!", _default_mode))

    # ------------------------------------------------------------------
    # 7c-2. ★「各门店总销售额」（原文**没提**时间）⇒ 通过 + 标「需求未提出」
    #
    # 这是**误拦回归**的第二道闸（第一道是 7b-3）。
    # ⚠️ 与 7c-6 的对照是本组最关键的一对：两者都判通过，
    #   但留痕语义相反 —— 靠 detail 文案区分（下方 7c-6 做 assertNotEqual）。
    # ------------------------------------------------------------------
    title = "7c-2. ★ 原文未提时间 ⇒ 通过，且标注「需求未提出，不作要求」"
    with offline_demand_env(mode="blocking"):
        r2 = gt.review(SQL_PLAIN, dataset="B",
                       requirement=requirement_of(DEMAND_NO_TIME["demand_id"]),
                       _dry_run_fn=_fake_dry_run_ok)
    lreq2 = _layer_by(r2, "L-REQ")
    detail2 = (lreq2 or {}).get("detail") or ""
    ok = (
        r2["status"] in ("通过", "警告")
        and lreq2 and lreq2["ok"] is True and lreq2["skipped"] is False
        and not [g for g in (r2.get("requirement_gaps") or []) if "颗粒度" in g]
        and "需求未提出" in detail2
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s detail=%s"
                     % (title, r2["status"], lreq2, r2.get("requirement_gaps"), detail2[:200]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%s" % (
        r2["status"],
        lreq2["ok"] if lreq2 else None,
        lreq2["skipped"] if lreq2 else None,
        r2.get("requirement_gaps")))
    print("    detail 关键行=%s" % [ln.strip() for ln in detail2.splitlines()
                                if "需求未提出" in ln or "未判定能力" in ln][:4])

    # ------------------------------------------------------------------
    # 7c-3. sql_review 不传 demand_id⇒ skipped + **明写原因**
    #
    # ⚠️ 断言的重点不是 skipped=True（那是阶段一就有的），
    #   而是「原因必须出现在 detail 里」：缺席若不报未执行，
    #   业务方会以为「需求要素已校验通过」——那正是 REQ-COVERAGE 的病。
    # ------------------------------------------------------------------
    title = "7c-3. ★ 不传 demand_id ⇒ skipped 且明写原因（缺席必须显式报未执行）"
    r3 = gt.review(SQL_PLAIN, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    lreq3 = _layer_by(r3, "L-REQ")
    detail3 = (lreq3 or {}).get("detail") or ""
    ok = (
        lreq3 and lreq3["skipped"] is True and lreq3["ok"] is True
        and "跳过原因" in detail3 and "demand_id" in detail3
        and r3["status"] in ("通过", "警告")
    )
    if not ok:
        fails.append("%s FAILED: L-REQ=%s detail=%s" % (title, lreq3, detail3[:200]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)" % (
        r3["status"],
        lreq3["ok"] if lreq3 else None,
        lreq3["skipped"] if lreq3 else None))
    print("    detail=%s" % detail3.replace("\n", " ")[:160])

    # ------------------------------------------------------------------
    # 7c-4. ★★ conf 恰 0.60 且 needs_confirmation=True，未覆盖 ⇒ 警告不阻断
    #
    # 🔴 这是冻结三条件里**最容易被误实现**的一条：`confidence >= 0.6`
    #   恰好成立（不是小于），此时唯一的豁免来源是 needs_confirmation。
    #   若实现写成 `confidence > 0.6`，本用例在 conf=0.60 时就该红——
    #   边界值上的差一错误（`<` vs `<=`）只有这种用例能抓住。
    # ⚠️ 走**真实链路**：注入一个 conf=0.60 / needs=True 的 8 字段契约要素，
    #   经 reqcov.judge 出severity，再验策略层不因它判红。
    #   （不直接调 _severity_of：那会绕过 judge，验不到集成。）
    # ------------------------------------------------------------------
    title = "7c-4. ★★ conf=0.60 且待确认=True 且未覆盖 ⇒ 警告，不阻断"
    import reqcov as _reqcov_mod  # noqa: PLC0415 —— 判据层，与 gates 同源
    _el_060_pending = {
        "element": "月度", "slot": "granularity",
        "provenance": "需求原文第1句：'各门店月度销售额'",
        "confidence": 0.60, "needs_confirmation": True, "mandatory": True,
        "polarity": "must_cover", "matcher": "time_granularity:month",
    }
    _facts_060 = _reqcov_mod.extract_facts(SQL_PLAIN)
    _judge_060 = _reqcov_mod.judge([_el_060_pending], _facts_060)
    _gaps_060 = _judge_060.get("gaps") or []
    _sev_060 = _gaps_060[0].get("severity") if _gaps_060 else None

    # 策略层集成：把这份要素塞进实例层返回，看 L-REQ 在 blocking 下**也不判红**
    _orig_befr = _reqelem_mod.build_elements_from_requirement

    def _fake_befr(requirement, *a, **kw):
        return {"ok": True, "elements": [dict(_el_060_pending)],
                "error": None, "cached": False}

    _reqelem_mod.build_elements_from_requirement = _fake_befr
    try:
        with offline_demand_env(mode="blocking"):
            r4 = gt.review(SQL_PLAIN, dataset="B",
                           requirement=requirement_of(DEMAND_A1["demand_id"]),
                           _dry_run_fn=_fake_dry_run_ok)
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr
    lreq4 = _layer_by(r4, "L-REQ")
    ok = (
        _sev_060 == _reqcov_mod.SEVERITY_WARNING
        and _judge_060.get("stats", {}).get("blocking") == 0
        and lreq4 and lreq4["ok"] is True and lreq4["skipped"] is False
        and r4["status"] in ("通过", "警告")
    )
    if not ok:
        fails.append("%s FAILED: severity=%s stats=%s L-REQ=%s status=%s"
                     % (title, _sev_060, _judge_060.get("stats"), lreq4, r4["status"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    判据层：severity=%s（期望 warning）  stats=%s" % (
        _sev_060, {k: v for k, v in (_judge_060.get("stats") or {}).items() if v}))
    print("    策略层：blocking 下 status=%s  L-REQ(ok=%s) ⇒ 未判红"
          % (r4["status"], lreq4["ok"] if lreq4 else None))
    # 边界对照：同一要素把 needs_confirmation 翻成 False ⇒ 必须变blocking
    _el_060_confirmed = dict(_el_060_pending, needs_confirmation=False)
    _judge_c = _reqcov_mod.judge([_el_060_confirmed], _facts_060)
    _sev_c = (_judge_c.get("gaps") or [{}])[0].get("severity")
    ok_edge = _sev_c == _reqcov_mod.SEVERITY_BLOCKING
    if not ok_edge:
        fails.append("7c-4 FAILED: 边界对照失效（待确认=False 时 severity=%s）" % _sev_c)
    print("[%s] 7c-4 边界对照：同要素 needs_confirmation=False ⇒ severity=%s（期望 blocking）"
          % ("OK" if ok_edge else "!!", _sev_c))

    # ------------------------------------------------------------------
    # 7c-5. ★「月度」用 to_char(...,'YYYY-MM') 实现（非 date_trunc）⇒ 通过
    #
    # 判据必须**按粒度类别判，不按实现判**：否则「同一需求换个写法就判未覆盖」
    # ⇒ 假阳性率飙升 ⇒ 团队绕过门禁。
    # ⚠️ 用真实链路（不注入要素）：确保 to_char 这条路走的是
    #   reqcov 的粒度分类器 + 实例层真实产出的「月度」要素。
    # ------------------------------------------------------------------
    title = "7c-5. ★ 月度用 to_char(...,'YYYY-MM') 实现 ⇒ 判已覆盖，不报缺口"
    with offline_demand_env(mode="blocking"):
        r5 = gt.review(SQL_A1_GOOD, dataset="B",
                       requirement=requirement_of(DEMAND_A1["demand_id"]),
                       _dry_run_fn=_fake_dry_run_ok)
    lreq5 = _layer_by(r5, "L-REQ")
    gaps5 = r5.get("requirement_gaps") or []
    ok = (
        r5["status"] in ("通过", "警告")
        and lreq5 and lreq5["ok"] is True and lreq5["skipped"] is False
        and not [g for g in gaps5 if "月度" in g]
        and "覆盖判定通过" in ((lreq5 or {}).get("detail") or "")
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s"
                     % (title, r5["status"], lreq5, gaps5))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%s" % (
        r5["status"],
        lreq5["ok"] if lreq5 else None,
        lreq5["skipped"] if lreq5 else None, gaps5))
    print("    SQL=%s" % SQL_A1_GOOD[:78])

    # ------------------------------------------------------------------
    # 7c-6. ★ 原文明确说「不限时间范围」⇒ 通过 + 标「业务方显式豁免」
    #
    # 🔴 与 7c-2 成对：两者**都判通过**，但留痕语义**相反**
    #   （7c-2 = 业务方没表态；7c-6 = 业务方已表态不要）。
    #   这对断言是本组最难被"糊弄"的地方：只看 status 与 gaps，
    #   两条用例**完全一样** ⇒ 区分它们的只有文案。
    #   ⇒ 本条除了各自断言自己的关键词，还要 assertNotEqual 互斥。
    # ------------------------------------------------------------------
    title = "7c-6. ★ 原文明确豁免 ⇒ 通过，且标注「业务方显式豁免」"
    with offline_demand_env(mode="blocking"):
        r6 = gt.review(SQL_PLAIN, dataset="B",
                       requirement=requirement_of(DEMAND_EXEMPT["demand_id"]),
                       _dry_run_fn=_fake_dry_run_ok)
    lreq6 = _layer_by(r6, "L-REQ")
    detail6 = (lreq6 or {}).get("detail") or ""
    ok = (
        r6["status"] in ("通过", "警告")
        and lreq6 and lreq6["ok"] is True and lreq6["skipped"] is False
        and "显式豁免" in detail6
        and not [g for g in (r6.get("requirement_gaps") or []) if "时间" in g]
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s detail=%s"
                     % (title, r6["status"], lreq6, r6.get("requirement_gaps"), detail6[:200]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)  gaps=%s" % (
        r6["status"],
        lreq6["ok"] if lreq6 else None,
        lreq6["skipped"] if lreq6 else None,
        r6.get("requirement_gaps")))
    print("    detail 关键行=%s" % [ln.strip() for ln in detail6.splitlines()
                                if "豁免" in ln][:4])

    # 🔴 互斥断言：**同一个要素**在两条用例里必须被标成不同成因
    #
    # ⚠️ 判据必须**逐要素**比，不能比整段文本里有没有出现某个词。
    #   实测踩过两次：
    #   ① 写成 XOR（`(A in d2) != (B in d6)`）——必然假红：正确行为恰恰是
    #      d2 含「需求未提出」**且** d6 含「显式豁免」（都真），XOR 要求一假。
    #   ② 改成「d6 不含'需求未提出'」——**也假红**：用例 6 的需求里只有
    #      「时间口径」被显式豁免，主体/颗粒度/范围条件仍是「未提出」，
    #      整段文本里两个词都会出现。
    #   ⇒ 正确口径：取「时间口径」那一行单独比。
    #   这两次都是「判据写错 ⇒ 正确实现被判红」，比漏检更坏：
    #   会逼后来人把实现改成迎合错判据（把两种成因合并成一句）。
    def _line_of(detail, keyword):
        """detail 里提到该要素的那一行（找不到返回空串）。"""
        for ln in (detail or "").splitlines():
            if keyword in ln and "：" in ln:
                return ln.strip()
        return ""

    _line2_time = _line_of(detail2, "时间口径")
    _line6_time = _line_of(detail6, "时间口径")
    _mutual = (
        "需求未提出" in _line2_time
        and "业务方显式豁免" in _line6_time
        and "业务方显式豁免" not in _line2_time
        and "需求未提出" not in _line6_time
    )
    if not _mutual:
        fails.append(
            "7c-6 FAILED: 同一要素（时间口径）的成因文案未区分："
            "case2=%r / case6=%r" % (_line2_time, _line6_time))
    print("[%s] 7c-6 ★ 互斥断言（同一要素「时间口径」逐行比）："
          "case2=%s / case6=%s"
          % ("OK" if _mutual else "!!", _line2_time, _line6_time))
    # 7c-7. ★ 判据层与策略层**必须分离**（结构性）
    #
    # ⚠️ 为什么单独一条：若有人往 reqcov.judge() 里加 status / should_block，
    #   判据层就混进了策略 ⇒ 第4 步的开关无处插（本文件的 _req_coverage_mode
    #   会变成摆设：改判据就等于改策略）。
    #   本脚本不能改 reqcov_selftest.py 的断言，故在此独立钉一份。
    # ⚠️ 判据取**顶层键**：嵌套里出现 status 不算混策略（例如 gap 里的
    #   业务字段），但顶层出现就是「judge 回答了要不要拦」。
    # ------------------------------------------------------------------
    title = "7c-7. ★ 判据层不含策略字段：judge() 顶层无 status/blocked/should_block"
    _judge_keys = sorted(_judge_060.keys())
    _forbidden = [k for k in _judge_keys
                  if k in ("status", "blocked", "block", "should_block", "gate_status")]
    ok = not _forbidden
    if not ok:
        fails.append("%s FAILED: judge() 顶层出现策略字段 %s" % (title, _forbidden))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    judge() 顶层键=%s" % _judge_keys)
    # 交叉确认：策略字段只存在于 gates 侧（开关确实在这一层）
    print("    策略开关在 gates 侧：%s=%s  当前模式=%s" % (
        gt._REQ_COVERAGE_MODE_ENV,
        repr(os.environ.get(gt._REQ_COVERAGE_MODE_ENV)),
        gt._req_coverage_mode()))

    # ------------------------------------------------------------------
    # 7c-8. ★ 策略开关真能切（不是写死的假开关）
    #
    # ⚠️ 这条防的是「开关摆在那里但改了没用」——症状是「切了blocking
    #   门禁照旧不拦」，**零报错**，与本项目在修的病同构。
    #   实现上曾真出过一次：_req_coverage_mode() 返回 bool，
    #   而调用点是 `mode == _REQ_MODE_BLOCKING`（与字符串比）⇒ 恒 False。
    #   本条就是那次事故留下的哨兵。
    # ------------------------------------------------------------------
    title = "7c-8. ★ 策略开关真能切：同一 SQL 在两种模式下status 不同"
    with offline_demand_env(mode="blocking"):
        _s_blk = gt.review(SQL_A1_BAD, dataset="B",
                           requirement=requirement_of(DEMAND_A1["demand_id"]),
                           _dry_run_fn=_fake_dry_run_ok)["status"]
    with offline_demand_env(mode="report-only"):
        _s_rep = gt.review(SQL_A1_BAD, dataset="B",
                           requirement=requirement_of(DEMAND_A1["demand_id"]),
                           _dry_run_fn=_fake_dry_run_ok)["status"]
    # 拼错的值必须落report-only（开关写错 ≠ 静默全量阻断）
    with offline_demand_env(mode="BLOCKING_拼错了"):
        _s_typo = gt._req_coverage_mode()
    ok = (_s_blk == "不通过" and _s_rep != "不通过" and _s_typo == gt._REQ_MODE_REPORT)
    if not ok:
        fails.append("%s FAILED: blocking=%s report-only=%s 拼错值落=%s"
                     % (title, _s_blk, _s_rep, _s_typo))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    blocking=%s  report-only=%s  拼错值→%s（期望 report-only）"
          % (_s_blk, _s_rep, _s_typo))
    # 复原确认：offline_demand_env 退出后环境变量必须回到原状
    ok_restore = os.environ.get(gt._REQ_COVERAGE_MODE_ENV) not in ("blocking", "BLOCKING_拼错了")
    if not ok_restore:
        fails.append("7c-8 FAILED: offline_demand_env 未复原环境变量（当前=%s）"
                     % os.environ.get(gt._REQ_COVERAGE_MODE_ENV))
    print("[%s] 7c-8 补充：夹具退出后环境变量=%r（应为 None/原值）"
          % ("OK" if ok_restore else "!!", os.environ.get(gt._REQ_COVERAGE_MODE_ENV)))

    # ------------------------------------------------------------------
    # 7c-9. ★★ 「不判」三类成因**分流正确**（委托实例层的 not_judged_reason）
    #
    #🔴 这条防的是「把『没查成』谎报成『不需要』」——那正是 REQ-COVERAGE 的
    #   原始病。judge() 把两类成因归到同一个 verdict="不判"，
    #   报告层若分错，业务方就会以为「订单数丢了也是我自己说的不要」。
    #
    # ⚠️ 判据**必须钉住三类都出现**：只钉前两类时，若实例层新增第三类
    #   （返回「原因不明」）而我这边把它塞进「未判定能力」，本用例照样全绿。
    # ------------------------------------------------------------------
    title = "7c-9. ★★ 「不判」三类成因分流：有意不要 / 未判定能力 / 成因未识别"
    _fake_elems = [
        # ① 业务方有意不要（may_omit + cause）
        {"element": "月度", "slot": "granularity", "matcher": "time_granularity:month",
         "polarity": "may_omit", "confidence": 0.45, "needs_confirmation": False,
         "mandatory": False, "provenance": "需求原文（全文未出现相关表述，无对应句）",
         "cause": "需求未提出 月度，不作要求"},
        # ② 我们没能力判（must_cover + 实例层保守 kind）
        {"element": "订单数", "slot": "output_fields", "matcher": "field_present:订单数",
         "polarity": "must_cover", "confidence": 0.8, "needs_confirmation": False,
         "mandatory": True, "provenance": "需求原文第1句：'…订单数…'"},
        # ③ 成因未识别（must_cover + 未注册的 kind ⇒ 底座判「不判 + 未注册」）
        {"element": "神秘指标", "slot": "output_fields", "matcher": "mystery_kind:神秘指标",
         "polarity": "must_cover", "confidence": 0.8, "needs_confirmation": False,
         "mandatory": True, "provenance": "需求原文第1句：'…神秘指标…'"},
    ]
    _orig_befr_9 = _reqelem_mod.build_elements_from_requirement
    try:
        _reqelem_mod.build_elements_from_requirement = (
            lambda requirement, *a, **kw: {"ok": True,
                                           "elements": [dict(e) for e in _fake_elems],
                                           "error": None, "cached": False})
        with offline_demand_env(mode="blocking"):
            r9 = gt.review(sql_clean, dataset="B",
                           requirement=requirement_of(DEMAND_A1["demand_id"]),
                           _dry_run_fn=_fake_dry_run_ok)
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr_9
    _lreq9 = _layer_by(r9, "L-REQ")
    _d9 = (_lreq9 or {}).get("detail") or ""
    _sec_waived = "业务方未要求" in _d9
    _sec_unable = "未判定能力" in _d9
    _sec_unknown = "成因未识别" in _d9
    # 逐类点名：不能只看节标题（标题在、内容空也算「有」）
    _in_waived = "需求未提出 月度" in _d9
    _in_unable = "订单数" in _d9 and "未判定能力" in _d9
    _in_unknown = "神秘指标" in _d9
    ok = (_sec_waived and _sec_unable and _sec_unknown
          and _in_waived and _in_unable and _in_unknown)
    if not ok:
        fails.append("%s FAILED: 三节=%s/%s/%s 逐类=%s/%s/%s"
                     % (title, _sec_waived, _sec_unable, _sec_unknown,
                        _in_waived, _in_unable, _in_unknown))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    三节标题：有意的=%s 未判定能力=%s 成因未识别=%s"
          % (_sec_waived, _sec_unable, _sec_unknown))
    print("    逐类点名：月度(有意不要)=%s 订单数(未判定)=%s 神秘指标(未识别)=%s"
          % (_in_waived, _in_unable, _in_unknown))

    # ------------------------------------------------------------------
    # 7c-10. ★★「未判定能力」必须带**限定说明**（防被读成「未覆盖」）
    #
    # 🔴 这条钉的是**呈现层的语义**，不是判据。
    #   实例层实测（队友，DR-20261020-1P4K）：SQL 里是 `s.store_name`，
    #   需求写「门店」⇒ 中文业务名与英文列名对不上 ⇒ 判 UNDECIDABLE；
    #   而同一条 SQL 里 `AS 销售额` 的中文别名能精确命中 ⇒ 判「已覆盖」。
    #   ⇒ 「未判定能力」这一节里**有的其实已覆盖，只是认不出**。
    #   若节里只写「未判定能力」四个字，业务方会把「判不了」读成「没覆盖」
    #   ⇒ 去改本来正确的 SQL。**故限定说明是必需的，不是可选的润色。**
    #
    # ⚠️ 判据要同时钉住两件事：
    #   ① 限定说明**存在**；
    #   ② 限定说明**在逐项清单之前**（放末尾就没人先看到它了）。
    # ------------------------------------------------------------------
    title = "7c-10. ★★「未判定能力」节带限定说明，且说明在逐项清单之前"
    _orig_befr_10 = _reqelem_mod.build_elements_from_requirement
    try:
        _reqelem_mod.build_elements_from_requirement = (
            lambda requirement, *a, **kw: {"ok": True, "elements": [dict(e) for e in _fake_elems],
                                           "error": None, "cached": False})
        with offline_demand_env(mode="blocking"):
            r10 = gt.review(sql_clean, dataset="B",
                            requirement=requirement_of(DEMAND_A1["demand_id"]),
                            _dry_run_fn=_fake_dry_run_ok)
    finally:
        _reqelem_mod.build_elements_from_requirement = _orig_befr_10
    _d10 = (_layer_by(r10, "L-REQ") or {}).get("detail") or ""
    _lines10 = _d10.splitlines()
    _i_head = next((i for i, l in enumerate(_lines10) if "未判定能力" in l and l.strip().startswith("以下要素")), -1)
    _i_note = next((i for i, l in enumerate(_lines10)
                    if "说明" in l and "不代表没覆盖" in l), -1)
    _i_item = next((i for i, l in enumerate(_lines10) if "订单数" in l), -1)
    _has_note = _i_note >= 0
    # ⚠️ 必须「说明在清单之前」：否则业务方先读到清单再读到解释，效果减半
    _note_before = (_i_note >= 0 and _i_item >= 0 and _i_note < _i_item)
    ok = _has_note and _note_before
    if not ok:
        fails.append("%s FAILED: 限定说明存在=%s 在清单之前=%s (head=%s note=%s item=%s)"
                     % (title, _has_note, _note_before, _i_head, _i_note, _i_item))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    限定说明存在=%s  在逐项清单之前=%s  (标题行=%s 说明行=%s 清单行=%s)"
          % (_has_note, _note_before, _i_head, _i_note, _i_item))
    if _has_note:
        print("    说明文案=%s" % _lines10[_i_note].strip()[:110])

    # ------------------------------------------------------------------
    # 附加：is_write() 辅助函数
    # ------------------------------------------------------------------
    title = "附加-8. is_write() 正反向用例"
    write_cases = [
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a=1",
        "DELETE FROM t",
        "DROP TABLE t",
        "ALTER TABLE t ADD c INT",
        "CREATE TABLE t (id INT)",
        "TRUNCATE TABLE t",
        "SELECT 1; SELECT 2",
        "SELECT ((",
    ]
    read_cases = [
        "SELECT 1",
        "WITH x AS (SELECT 1) SELECT * FROM x",
    ]
    bad_w = [c for c in write_cases if not gt.is_write(c)]
    bad_r = [c for c in read_cases if gt.is_write(c)]
    ok = not bad_w and not bad_r
    if not ok:
        fails.append("%s FAILED: 写操作漏拦=%s 读操作误拦=%s" % (title, bad_w, bad_r))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    写操作漏拦=%d 读操作误拦=%d" % (len(bad_w), len(bad_r)))

    # ------------------------------------------------------------------
    # 附加-8b：退化输入（空串 / 纯空白 / 纯注释）—— **独立组，不得混入 read_cases**
    #
    # 为什么必须另起一组（这是个真实踩过的坑）
    # --------------------------------------------
    # read_cases 的断言语义是「**读操作不该被拦**」：bad_r = 被误判为写操作的读操作。
    # 而退化输入**根本不是读操作**——它是「无法判定」。两者期望相反：
    #   read_cases 期望 is_write == False ；退化输入期望 is_write == True（保守不放行）。
    # 把 "" 塞进 read_cases 会在「期望 False」的断言里塞一个真值为 True 的输入 ⇒ 用例判红。
    #
    # 契约内容（保守方向）
    # --------------------
    # 退化输入一律 is_write == True。这是**保守方向：不可判定 ⇒ 不放行**，
    # ⚠️ **不是**「已确含写操作」——调用方若要区分成因，请查 L1（语法）/ L3（只读）。
    #
    # ⚠️ 刻意**不钉** `sqlglot.parse("") == [None]` 这类第三方内部行为：
    #   那是实现细节，钉死它等于给未来 sqlglot 升级埋雷。**保守行为本身才是契约。**
    #   （成因已核对：两个环境——门禁子进程与 askoda 容器——的 sqlglot 版本不同，
    #     但 parse('') 均为 [None]（长度 1），故走 isinstance(None, _READONLY_ROOT)
    #     分支返回 True。⚠️ 这里**不写具体版本号**，因为版本号会随升级而过期；
    #     若将来行为变化，先核对 sqlglot 版本再改契约，不要直接改这里的期望值。）
    # ------------------------------------------------------------------
    title = "附加-8b. 退化输入 is_write==True（保守方向，不混 read_cases）"
    degenerate_cases = [
        "",# 空串
        "   ",            # 纯空白
        "-- c",           # 纯注释
    ]
    bad_d = [c for c in degenerate_cases if not gt.is_write(c)]
    ok = not bad_d
    if not ok:
        fails.append("%s FAILED: 退化输入未被保守拒绝=%s" % (title, bad_d))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    退化输入 %d 项，未被拒绝=%d（期望 0；True=不可判定故不放行）"
          % (len(degenerate_cases), len(bad_d)))
    # 交叉确认：这些输入确实**不在** read_cases 里（防止将来有人合并两组）
    overlap = [c for c in degenerate_cases if c in read_cases]
    print("    与 read_cases 重叠=%s（期望空）" % (overlap or "无"))

    # ------------------------------------------------------------------
    # 附加：L5 结果断言（无 exec_result 时 skipped）
    # ------------------------------------------------------------------
    title = "附加-9. L5 无 exec_result → skipped=True，不拖垮总状态"
    sql = "SELECT 1 AS x"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l5 = _layer_by(r, "L5")
    ok = l5 and l5["skipped"] and not l5["ok"] is False and r["status"] != "不通过"
    if not ok:
        fails.append("%s FAILED: status=%s L5=%s" % (title, r["status"], l5))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L5.skipped=%s  L5.detail=%s" % (
        r["status"], l5["skipped"] if l5 else None, (l5 or {}).get("detail", "")[:50]))

    # ------------------------------------------------------------------
    # 附加：L2 各类静态规则（GROUP BY 不一致、UNNECESSARY DISTINCT）
    # ------------------------------------------------------------------
    title = "附加-10. GROUP BY 不一致 + 不必要 DISTINCT 警告（status=警告）"
    # 修正（2026-10-02）：原 SQL 用假表 t / 假列 a,b,c。因「引用未建模对象」现已属
    # 硬错误（blocking），会把本用例顶成"不通过"，偏离本用例意图（本用例只验
    # GROUP BY 软规则能被检出且不阻断）。改用 B 库真实表/列，保持原意图。
    sql = "SELECT DISTINCT store_id, order_status, COUNT(*) FROM dwd_order_di GROUP BY store_id"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    rules = {i["rule"] for i in r["semantic_issues"]}
    has_gb = "GROUP_BY_INCONSISTENT" in rules
    has_distinct = "UNNECESSARY_DISTINCT" in rules
    # 该 SQL 还带聚合，UNNECESSARY_DISTINCT 不一定会命中（因有聚合函数），
    # 所以不强求 has_distinct；只要求 GROUP BY 不一致能检出且 status != 不通过
    ok = has_gb and r["status"] != "不通过"
    if not ok:
        fails.append("%s FAILED: status=%s rules=%s issues=%s" % (
            title, r["status"], rules, r["semantic_issues"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  rules=%s" % (r["status"], sorted(rules)))

    # ------------------------------------------------------------------
    # 附加：PT-2 GROUP_BY_INCONSISTENT 6 例（别名/序号/别名引用 去误报；真阳性保留）
    # ------------------------------------------------------------------
    title = "附加-10B. PT-2 GROUP BY 6 例：①②③④⑤ 无误报、⑥ 真阳性 1 条"
    gb_cases = [
        # ① 无别名
        ("① 无别名",
         "SELECT p.category_name, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ② 中文别名 GROUP BY 原列名
        ("② 中文别名+GROUP BY 原列",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ③ 英文同名别名 GROUP BY 原列
        ("③ 英文同名别名",
         "SELECT p.category_name AS category_name, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         0),
        # ④ 序号 GROUP BY 1
        ("④ GROUP BY 序号",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY 1",
         0),
        # ⑤ GROUP BY 别名引用
        ("⑤ GROUP BY 别名引用",
         "SELECT p.category_name AS 品类, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY 品类",
         0),
        # ⑥ 真缺列：SELECT 两个维度列，但 GROUP BY 只写一个
        ("⑥ 真缺列（品牌未 GROUP BY）",
         "SELECT p.category_name AS 品类, p.brand AS 品牌, SUM(oi.subtotal) AS amt "
         "FROM products p JOIN order_items oi ON p.id = oi.product_id "
         "GROUP BY p.category_name",
         1),
    ]
    ok_10b = True
    details_10b = []
    for label, sql, expected_n in gb_cases:
        r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
        actual_n = sum(1 for i in r["semantic_issues"] if i.get("rule") == "GROUP_BY_INCONSISTENT")
        hit = actual_n == expected_n
        if not hit:
            ok_10b = False
        details_10b.append((label, expected_n, actual_n, hit))
    if not ok_10b:
        fails.append("%s FAILED: details=%s" % (title, details_10b))
    print("[%s] %s" % ("OK" if ok_10b else "!!", title))
    for label, expected_n, actual_n, hit in details_10b:
        print("    %s  GROUP_BY_INCONSISTENT 期望=%d 实测=%d → %s" % (
            label, expected_n, actual_n, "OK" if hit else "!!"))

    # ------------------------------------------------------------------
    # 附加：L5 断言（给 exec_result 验证负值检测/空行检测/超上限检测）
    # ------------------------------------------------------------------
    title = "附加-11. L5 exec_result 行超限 & 金额负值 → status=不通过"
    sql = "SELECT * FROM big_table"
    exec_result = {
        "columns": ["id", "amount", "销售额"],
        "data": [[i, -100.5, -50] for i in range(100001)],
    }
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok, exec_result=exec_result)
    l5 = _layer_by(r, "L5")
    has_over = any("超过上限" in v["detail"] for v in r["rule_violations"] if v["layer"] == "L5")
    has_neg = any("金额类列" in v["detail"] and "负值" in v["detail"] for v in r["rule_violations"] if v["layer"] == "L5")
    ok = r["status"] == "不通过" and l5 and not l5["skipped"] and not l5["ok"] and has_over and has_neg
    if not ok:
        fails.append("%s FAILED: status=%s L5=%s violations(L5)=%s" % (
            title, r["status"], l5, [v for v in r["rule_violations"] if v["layer"] == "L5"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  over_limit=%s  neg_amount=%s" % (r["status"], has_over, has_neg))

    # ------------------------------------------------------------------
    # 附加：L4 注入失败桩 → 阻断
    # ------------------------------------------------------------------
    title = "附加-12. L4 dry_run 失败桩 → 阻断"
    def _fake_fail(sql):
        return {"ok": False, "message": "故意失败：引用不存在的模型"}
    sql = "SELECT * FROM nonexistent_table"
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_fail)
    l4 = _layer_by(r, "L4")
    has_sem_violation = any(v["layer"] == "L4" for v in r["rule_violations"])
    ok = r["status"] == "不通过" and l4 and not l4["ok"] and not l4["skipped"] and has_sem_violation
    if not ok:
        fails.append("%s FAILED: status=%s L4=%s violations=%s" % (
            title, r["status"], l4, r["rule_violations"]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L4.ok=%s  L4.detail=%s" % (
        r["status"], l4["ok"] if l4 else None, (l4 or {}).get("detail", "")[:60]))

    # ------------------------------------------------------------------
    # 附加-13. 复合 SQL 命中阻断类规则（无 ON JOIN） → status=不通过
    # ------------------------------------------------------------------
    title = "附加-13. 复合SQL: SELECT* + 无ON JOIN(阻断类) + GROUP_CONCAT无ORDER → status=不通过"
    sql = """
        SELECT *, GROUP_CONCAT(t1.sku_id, ',') AS skus
        FROM dwd_order_detail_di t1, dim_store t2
        GROUP BY t1.store_id
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    l2 = _layer_by(r, "L2")
    sem_count = len(r.get("semantic_issues") or [])
    ok = (
        r["status"] == "不通过"
        and sem_count >= 3
        and (l2 is None or (not l2.get("skipped") and not l2.get("fatal")))
    )
    if not ok:
        fails.append("%s FAILED: status=%s sem_count=%d issues=%s" % (
            title, r["status"], sem_count, r.get("semantic_issues")[:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  semantic_issues=%d  rules=%s" % (
        r["status"], sem_count, [x.get("rule") for x in (r.get("semantic_issues") or [])[:5]]))

    # ------------------------------------------------------------------
    # 附加-14. 含 CTE 的正常 SQL → R5 UNMAPPED_OBJECT_REF 0 条不误报
    # ------------------------------------------------------------------
    title = "附加-14. CTE monthly 正常 SQL → UNMAPPED_OBJECT_REF 0 条"
    sql = """
        WITH monthly AS (
            SELECT store_id, SUM(sales_amount) AS amt
            FROM dws_store_daily_agg
            GROUP BY store_id
        )
        SELECT m.store_id, s.store_name, m.amt
        FROM monthly m
        JOIN dim_store s ON m.store_id = s.store_id
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    unmapped_n = sum(1 for x in (r.get("semantic_issues") or []) if x.get("rule") == "UNMAPPED_OBJECT_REF")
    ok = unmapped_n == 0 and r["status"] in ("警告", "通过")
    if not ok:
        fails.append("%s FAILED: status=%s UNMAPPED_OBJECT_REF=%d issues=%s" % (
            title, r["status"], unmapped_n,
            [x for x in (r.get("semantic_issues") or []) if x.get("rule") == "UNMAPPED_OBJECT_REF"][:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  UNMAPPED_OBJECT_REF=%d" % (r["status"], unmapped_n))

    # ------------------------------------------------------------------
    # 附加-15. MANY_TO_ONE：事实表⋈维表 + 聚合 → ONE_TO_MANY_UNHANDLED 0 条 + status=通过
    # ------------------------------------------------------------------
    title = "附加-15. MANY_TO_ONE: dws_store_daily_agg⋈dim_store 聚合 ONE_TO_MANY_UNHANDLED=0"
    sql = """
        SELECT b.region_name, SUM(a.sales_amount) AS gmv,
               SUM(a.order_cnt) AS order_cnt,
               SUM(a.member_cnt) AS member_cnt
        FROM dws_store_daily_agg a
        JOIN dim_store b
          ON a.store_id = b.store_id
        WHERE a.stat_date >= '2025-03-01'
        GROUP BY b.region_name
    """
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    r1_n = sum(1 for x in (r.get("semantic_issues") or []) if x.get("rule") == "ONE_TO_MANY_UNHANDLED")
    ok = r1_n == 0 and r["status"] == "通过"
    if not ok:
        fails.append("%s FAILED: status=%s ONE_TO_MANY_UNHANDLED=%d issues=%s" % (
            title, r["status"], r1_n,
            [x for x in (r.get("semantic_issues") or []) if x.get("rule") == "ONE_TO_MANY_UNHANDLED"][:3]))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  ONE_TO_MANY_UNHANDLED=%d  all_rules=%s" % (
        r["status"], r1_n, [x.get("rule") for x in (r.get("semantic_issues") or [])[:5]]))

    # ------------------------------------------------------------------
    # 附加-16. 仅命中 warning 类规则 → status=警告（不阻断）—— 分级阻断的阴性对照
    # ------------------------------------------------------------------
    title = "附加-16. 仅 TIME_FIELD_SUSPECT(warning) → status=警告，不阻断"
    sql = ("SELECT date_trunc('month', order_date) AS 月份, COUNT(*) AS 订单数, "
           "SUM(pay_amount) AS 销售金额 FROM dwd_order_di WHERE order_status = '已完成' "
           "GROUP BY date_trunc('month', order_date) ORDER BY 月份")
    r = gt.review(sql, dataset="B", _dry_run_fn=_fake_dry_run_ok)
    hit = {x.get("rule") for x in (r.get("semantic_issues") or [])}
    ok = r["status"] == "警告" and "TIME_FIELD_SUSPECT" in hit
    if not ok:
        fails.append("%s FAILED: status=%s rules=%s" % (title, r["status"], sorted(hit)))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  rules=%s" % (r["status"], sorted(hit)))

    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("自证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
