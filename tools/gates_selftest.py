#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M5-1 SQL 门禁自证（离线，不需要容器与 Wren）。

L4 语义门禁用注入的假 dry_run 函数桩：默认全部通过。
如需测试 L4 阻断，自行传 _dry_run_fn=lambda sql: {"ok": False, "message": "故意失败"}。

用法：
    python3 tools/gates_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 脚本定位："离线、不需要容器与 Wren"——直接引用仓库内的 workspace 副本，
# 不强制用户手动 export MDL_A_PATH / MDL_B_PATH。
# （若宿主已显式设置环境变量则不覆盖。）
os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))
sys.path.insert(0, os.path.join(HERE, "..", "gateway"))
import gates as gt  # noqa: E402


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

    # 7b-2. 槽位齐备 + output_fields 非空 → L-REQ ok 且不改变 status
    title = "7b-2. 槽位齐备的 requirement → L-REQ ok，不误判红"
    _req_ok = {
        s: {"value": "结论-" + s, "evidence": [{"level": "P8"}], "confidence": 0.9}
        for s in ("subject", "granularity", "time_semantics", "data_scope")
    }
    _req_ok["output_fields"] = [
        {"value": "销售额", "evidence": [{"level": "P8"}], "confidence": 0.9},
    ]
    r = gt.review(sql_clean, dataset="B", requirement=_req_ok, _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    ok = (
        lreq and lreq["ok"] is True and lreq["skipped"] is False
        and r["status"] == "通过"
        and r.get("requirement_gaps") == []
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

    # ------------------------------------------------------------------
    # 7b-3. ★★ 误拦回归钉死：「原文没提时间」的需求必须【通过】
    #
    # 这条是本组里最重要的一条。requirement.py:237-241 对无候选槽位写的是
    # {"value": "无候选结论（…）", "confidence": 0.0}——value 非空但 confidence=0，
    # 于是「有没有提时间」与「槽位缺失」在结构化对象里是同一个信号。
    # 阶段一早期版本据此判红，实测会把「各门店总销售额」这种
    # **根本没提时间的正常提问**拦下来（PM 用例 2 要求通过）。
    #
    # ⇒ 阶段一 L-REQ 恒 ok=True：有缺口只提示，不阻断。
    # ------------------------------------------------------------------
    title = "7b-3. ★★ 原文未提时间 → 只提示不阻断，status=通过（防误拦回归）"
    _req_notime = {
        s: {"value": "结论-" + s, "evidence": [{"level": "P8"}], "confidence": 0.9}
        for s in ("subject", "granularity", "data_scope")
    }
    # 复刻 requirement.build 的缺失占位形态：value 明写「无候选结论」且 confidence=0
    _req_notime["time_semantics"] = {
        "value": "无候选结论（该槽位在本轮分析中为空）",
        "evidence": [{"level": "P8", "source": "analysis_rounds.slots"}],
        "confidence": 0.0,
    }
    _req_notime["output_fields"] = [
        {"value": "总销售额", "evidence": [{"level": "P8"}], "confidence": 0.9},
    ]
    r = gt.review(sql_clean, dataset="B", requirement=_req_notime, _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    gaps = r.get("requirement_gaps") or []
    ok = (
        lreq and lreq["ok"] is True and lreq["skipped"] is False
        and r["status"] == "通过"          # ★ 关键断言：没提时间不得被拦
        and len(gaps) == 1# 缺口仍要留痕
        and "L-REQ 提示" in (r.get("review_notes") or "")
        and r.get("revised_sql") is None   # 红线 D1：只报告不改写
    )
    if not ok:
        fails.append("%s FAILED: status=%s L-REQ=%s gaps=%s notes=%s" % (
            title, r["status"], lreq, gaps, r.get("review_notes")))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  L-REQ(ok=%s skipped=%s)" % (
        r["status"],
        lreq["ok"] if lreq else None,
        lreq["skipped"] if lreq else None,
    ))
    print("    gaps=%s" % gaps)
    print("    review_notes=%s" % (r.get("review_notes") or ""))

    # 7b-4. output_fields 空数组 → 出 gaps，但阶段一不阻断
    title = "7b-4. output_fields 空数组 → 出 gaps，仍不阻断"
    _req_nofields = dict(_req_ok)
    _req_nofields["output_fields"] = []
    r = gt.review(sql_clean, dataset="B", requirement=_req_nofields, _dry_run_fn=_fake_dry_run_ok)
    lreq = _layer_by(r, "L-REQ")
    gaps = r.get("requirement_gaps") or []
    ok = (
        lreq and lreq["ok"] is True
        and r["status"] == "通过"
        and len(gaps) == 1 and "output_fields" in gaps[0]
    )
    if not ok:
        fails.append("%s FAILED: status=%s gaps=%s" % (title, r["status"], gaps))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    status=%s  gaps=%s" % (r["status"], gaps))

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

    # 7b-6. ★ detail 范围自述：**枚举 + 结构性扫描双重保险**
    #
    # 这条防的是「在场但只做了一半」——业务方看到 L-REQ 绿了若不知道本阶段
    # 没查覆盖，就会以为「需求要素已校验通过」，那等于把 REQ-COVERAGE
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
    # ✅ 本断言已做负向验证（注入无前缀 return，确认会红，不是摆设）：
    #   ① 注入到枚举列表**有**对应 case 的分支   ⇒ 红
    #   ② 注入到枚举列表**无**对应 case 的新分支 ⇒ 仍红（这是关键的一验）
    #   两次均已恢复原状。⚠️ 改动本断言后请重做这两次负向验证。
    title = "7b-6. ★ detail 范围自述：枚举 + 结构性双重保险"
    _pfx = "本阶段仅校验槽位齐备性"
    _detail_cases = [
        ("无 requirement", gt.review(sql_clean, dataset="B", _dry_run_fn=_fake_dry_run_ok)),
        ("非 dict", gt.review(sql_clean, dataset="B", requirement="不是对象",
                          _dry_run_fn=_fake_dry_run_ok)),
        ("取不到", gt.review(sql_clean, dataset="B",
                         requirement={"ok": False, "error": "x"},
                         _dry_run_fn=_fake_dry_run_ok)),
        ("齐备", gt.review(sql_clean, dataset="B", requirement=_req_ok,
                       _dry_run_fn=_fake_dry_run_ok)),
        ("有缺口", gt.review(sql_clean, dataset="B", requirement=_req_notime,
                         _dry_run_fn=_fake_dry_run_ok)),
    ]
    ok = True
    for tag, rr in _detail_cases:
        #遍历 layers 里**每一个** L-REQ 实例（当前结构下只有一个，防御性写法）
        for lreq_inst in [l for l in rr.get("layers", []) if l.get("layer") == "L-REQ"]:
            d = lreq_inst.get("detail") or ""
            if _pfx not in d:
                ok = False
                print("        ✗ %s 缺前缀：%s" % (tag, d[:60]))
    print("[%s] %s（枚举 %d 条路径）" % ("OK" if ok else "!!", title, len(_detail_cases)))

    # ② 结构性扫描：源码里每个产出 detail 的 return 都必须引用 _REQ_SCOPE_PREFIX
    #
    # 用 inspect 拿源码而非 grep 文件：selftest 跑的是「已加载的模块」，
    # 与源码文本同源，但 inspect 不依赖文件路径，跨平台更稳。
    #
    # ⚠️ 计数必须**先剥掉 docstring**——docstring 里为了说明用途也提到了
    # _REQ_SCOPE_PREFIX，若不剥离会多计 1 处，把 5 个 return/5 处引用
    # 误判成「5 vs 6 有return 漏带前缀」。这是实现该断言时实测踩到的。
    import inspect as _inspect
    _src = _inspect.getsource(gt._requirement_check)
    _doc = _inspect.getdoc(gt._requirement_check) or ""
    if _doc:
        # getdoc 会做 dedent/规范化，未必与源码逐字一致，故按行剔除首个三引号块
        _lines = _src.splitlines()
        _body = "\n".join(ln for ln in _lines if not ln.strip().startswith("#"))
    else:
        _body = _src
    # 只统计出现在 return 语句块内的引用：逐个 return{ 到下一个 } 之间的片段
    _returns = 0
    _covered = 0
    _chunks = _src.split("return {")[1:]
    for _c in _chunks:
        _seg = _c.split("\n        }")[0]
        _returns += 1
        if "_REQ_SCOPE_PREFIX" in _seg:
            _covered += 1
    # 期望：每个 return 都带前缀（_returns == _covered）。
    # ⚠️ 若将来加了 return 却忘带前缀，这里 _covered < _returns 立刻红。
    _struct_ok = (_returns > 0) and (_returns == _covered)
    if not _struct_ok:
        ok = False
        print("        ✗ 结构性：return{%d 处，仅 %d 处带前缀 ⇒ 有 return 未自述范围"
              % (_returns, _covered))
        fails.append("%s FAILED: 结构性扫描发现未带前缀的 return" % title)
    print("[%s] 结构性扫描：return{%d 处，其中带前缀 %d 处"
          % ("OK" if _struct_ok else "!!", _returns, _covered))
    print("    样例（取不到）=%s"
          % ((_layer_by(_detail_cases[2][1], "L-REQ") or {}).get("detail") or "")[:120])

    # ⚠️ 已知待处理：此处硬编码 8 层，与 tools/e2e_verify.py 的 EXPECTED_LAYER_COUNT = 8
    #    是两个独立来源，门禁层数演进时会再次脱节。待抽共享常量统一（已立项）。
    #    本行是「层数契约断言」而非对外文案，故不在 D-LAYER 的去数字化范围内。
    # 7b-7. 层数契约：无论走哪条级联分支，layers 恒为 8 层
    title = "7b-7. 层数契约：L-REQ 在最前，且各分支恒 8 层"
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
        detail.append("%s=%d层" % (tag, len(got_order)))
    if not ok:
        fails.append("%s FAILED: %s" % (title, detail))
    print("[%s] %s" % ("OK" if ok else "!!", title))
    print("    期望层序=%s" % _expect_order)
    print("    实测=%s" % detail)

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
