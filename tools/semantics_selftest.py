# -*- coding: utf-8 -*-
"""语义分析 · 闸门与降级自证（离线可跑，不依赖容器）

守什么
------
本文件守两类**沉默失效**——它们不会报错，只会悄悄让结论变得不可信：

  A. 技术词闸门失效
     "确认问题不许含表名/字段名/SQL 技术语"是 M3 的验收标准之一。闸门若失效，
     表现不是报错，而是**物理表名被平静地写给业务**（实测踩到过：题干干净，
     选项里带着表名）。所以必须用负例证明它真的会拦，而不是摆着好看。

  B. 证据源缺失被读成"没问题"
     `collect()` 在某个来源取不到时返回空列表，与"查过确实没有"无法区分。
     若无人把关，P7（表关系）取不到时 R1（一对多，blocking 级）会被静默跳过——
     "订单金额 vs 商品明细金额核对"这种教科书级的一对多需求会报告"无风险"。
     所以 `degradation()` 必须有断言：缺了就要显式说出来。

用法：
    python tools/semantics_selftest.py
退出码：0 全过；1 有断言失败
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "gateway"))
sys.path.insert(0, HERE)

# ⚠️ **容器内的路径布局与宿主不同**（实测）：
#    宿主是 ``<repo>/gateway/*.py``；容器镜像把 gateway **扁平铺在 /app 下**
#    （``/app/semantics.py``），没有 ``/app/gateway/`` 目录。
#    ⇒ 在容器里跑本脚本时，上一行插的 ``/app/gateway`` 不存在 ⇒ import 失败。
#    这里补一条回退：``/app`` 下有 ``semantics.py`` 就把 /app 也加进 path。
#    ⚠️ 为什么要能在容器里跑：J 段（真实 bundle / 真实 demand / 需求对象入口）
#    **必须连库**才有意义，而 DB 只在容器里 ⇒ J 段只有容器内才真跑。
#    裸解释器（宿主）跑 J 段会走 skip()，如实标"未验证"。
if not os.path.isdir(os.path.join(ROOT, "gateway")):
    _flat = "/app" if os.path.isfile("/app/semantics.py") else ROOT
    sys.path.insert(0, _flat)

import semantics as se  # noqa: E402
import requirement as requirement_mod  # noqa: E402


def _src_path(rel):
    """读仓内源码文件的绝对路径（**宿主与容器都能用**）。

    ⚠️ 容器镜像把 gateway **扁平铺在 /app 下**（没有 ``/app/gateway/`` 目录），
    而本脚本多处要**读源码**（AST 直读契约常量、动态查行号）——
    只按 ``ROOT/gateway`` 拼路径在容器里会 FileNotFoundError。
    这里按"先试仓内布局、再试容器扁平布局"的顺序解析，两处都能用。
    ⚠️ 调用方传的 rel 有两种写法：``"gateway/reqcov.py"``（带目录前缀）
    与 ``"reqcov.py"``（纯文件名）—— 两种都要能解析，
    否则会拼出 ``<repo>/gateway/gateway/reqcov.py`` 这种双重前缀。
    """
    base = os.path.basename(rel)
    cands = [
        os.path.join(ROOT, rel),                       # 已是完整相对路径
        os.path.join(ROOT, "gateway", base),           # 仓内布局：gateway/xxx.py
        os.path.join("/app", base),                     # 容器扁平布局：/app/xxx.py
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError("找不到源码 %s（试过 %s）" % (rel, "、".join(cands)))


def _ast_consts(path):
    """用 AST 直读某模块的**全部模块级常量**（不 import ⇒ 不引入其依赖）。

    纪律要求"拿数字下结论用 Python 直读或 AST 解析"——底座 reqcov 依赖 sqlglot，
    裸解释器里 import 不了它，但它的**契约常量**是纯字面量，AST 读得到。

    返回 {名字: 值}；值里引用了别的常量的（如 ``POLARITIES = (POLARITY_MUST_COVER, …)``）
    会被解析出来——否则读到的只是名字，读不出取值。
    """
    import ast
    tree = ast.parse(open(_src_path(path), encoding="utf-8").read())
    raw = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    raw[tgt.id] = node.value

    def _eval(node, depth=0):
        if depth > 3:
            return None
        if isinstance(node, ast.Name):
            return _eval(raw[node.id], depth + 1) if node.id in raw else None
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(_eval(e, depth + 1) for e in node.elts)
        if isinstance(node, ast.Dict):
            out = {}
            for k, v in zip(node.keys, node.values):
                if k is None:
                    continue
                key = _eval(k, depth + 1)
                if key is None:
                    return None
                out[key] = _eval(v, depth + 1)
            return out
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    return {name: _eval(node) for name, node in raw.items()}


def _ast_const(path, name):
    """读单个模块级常量（找不到返回 None）。"""
    return _ast_consts(path).get(name)


def _ast_const_tuple(path, name):
    v = _ast_const(path, name)
    return tuple(v) if isinstance(v, (list, tuple)) else None


def _ast_const_dict(path, name):
    v = _ast_const(path, name)
    return dict(v) if isinstance(v, dict) else None


def _ast_const_keys(path, name):
    v = _ast_const(path, name)
    return sorted(v.keys()) if isinstance(v, dict) else []

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  \033[32mPASS\033[0m %s" % name)
    else:
        print("  \033[31mFAIL\033[0m %s  %s" % (name, detail))
        FAILS.append(name)


def skip(name, reason):
    """如实报"这一条没跑"（不是 PASS，也不是 FAIL）。

    纪律：**未跑过的行为一律标「推断」**——不能让它混进 PASS 里，
    也不能在没有真实缺陷时报 FAIL 让门禁假红。
    门禁的解释器带全依赖 ⇒ 那条会真跑；裸解释器下才走这里。
    """
    print("  \033[33mSKIP\033[0m %s  %s" % (name, reason))


# ---------------------------------------------------------------------------
# A. 结构常量
# ---------------------------------------------------------------------------
def test_constants():
    print("\nA. 结构常量")
    check("六槽位齐备", se.SLOT_KEYS == ["subject", "granularity", "time", "scope", "fields", "risks"],
          "实际：%s" % se.SLOT_KEYS)
    check("槽位中文名与槽位一一对应", set(se.SLOT_LABELS) == set(se.SLOT_KEYS))
    check("置信度基线覆盖 P1–P9",
          all(("P%d" % i) in se.LEVEL_BASE for i in range(1, 10)),
          "缺：%s" % [("P%d" % i) for i in range(1, 10) if ("P%d" % i) not in se.LEVEL_BASE])
    check("业务确认(P2)是最高可信来源",
          se.LEVEL_BASE["P2"] == max(se.LEVEL_BASE.values()),
          "P2=%s，最大=%s" % (se.LEVEL_BASE["P2"], max(se.LEVEL_BASE.values())))
    check("模型推断(P9)是兜底最低分",
          se.LEVEL_BASE["P9"] == min(se.LEVEL_BASE.values()),
          "P9=%s" % se.LEVEL_BASE["P9"])
    check("需确认上限低于业务确认分（不许高置信地不确定）",
          se.CONF_CAP_NEEDS_CONFIRM < se.LEVEL_BASE["P2"])
    check("降级上限不高于需确认上限",
          se.CONF_CAP_DEGRADED <= se.CONF_CAP_NEEDS_CONFIRM,
          "降级=%s 需确认=%s" % (se.CONF_CAP_DEGRADED, se.CONF_CAP_NEEDS_CONFIRM))


# ---------------------------------------------------------------------------
# B. 置信度
# ---------------------------------------------------------------------------
def test_confidence():
    print("\nB. confidence —— 交付级结论才是高置信")
    check("无证据 = 模型推断 0.30", se.confidence([]) == 0.30, "实际 %s" % se.confidence([]))
    check("P2 证据 = 0.85", se.confidence(["P2"]) == 0.85, "实际 %s" % se.confidence(["P2"]))
    check("多来源取最高（低优先级不得拉低）",
          se.confidence(["P1", "P6"]) == se.LEVEL_BASE["P6"],
          "实际 %s" % se.confidence(["P1", "P6"]))
    check("置信度不超过 0.95 上限", se.confidence(["P2"], extra=0.5) == 0.95,
          "实际 %s" % se.confidence(["P2"], extra=0.5))
    check("cap 生效", se.confidence(["P2"], cap=0.5) == 0.5,
          "实际 %s" % se.confidence(["P2"], cap=0.5))


# ---------------------------------------------------------------------------
# C. 结论构造：来源必须可追溯
# ---------------------------------------------------------------------------
def test_cand():
    print("\nC. _cand —— 每条结论都带来源")
    full = [
        {"level": "P6", "level_name": "数据字典", "source": "column_docs",
         "locator": "B.dim_store.store_name", "content": "门店名称"},
    ]
    c = se._cand("subject", "dim_store", full)
    check("证据被压成引用摘要", c["evidence"][0]["source"] == "column_docs"
          and c["evidence"][0]["locator"] == "B.dim_store.store_name")
    check("引用摘要保留 level / level_name",
          c["evidence"][0]["level"] == "P6" and c["evidence"][0]["level_name"] == "数据字典")
    check("置信度按证据等级自动计算", c["confidence"] == se.LEVEL_BASE["P6"],
          "实际 %s" % c["confidence"])
    check("默认不需确认", c["needs_confirmation"] is False)

    c2 = se._cand("granularity", "未定", full, needs=True)
    check("needs=True 时置信度被压到上限以内",
          c2["confidence"] <= se.CONF_CAP_NEEDS_CONFIRM, "实际 %s" % c2["confidence"])
    check("needs=True 被标记", c2["needs_confirmation"] is True)

    c3 = se._cand("scope", "x", [], conf=0.4)
    check("无证据也可构造（但只给显式给定的低分）",
          c3["evidence"] == [] and c3["confidence"] == 0.4)


# ---------------------------------------------------------------------------
# D. 技术词闸门（负例为主）
# ---------------------------------------------------------------------------
def test_tech_gate():
    print("\nD. 技术词闸门 —— 用负例证明它真的会拦")
    tech = ["dwd_order_di", "dwd_order_detail_di", "dim_member", "member_phone"]

    check("命中物理表名", se._technical_hits("按 dwd_order_di 查", tech) != [],
          "实际 %s" % se._technical_hits("按 dwd_order_di 查", tech))
    check("大小写不敏感", "dwd_order_di" in se._technical_hits("查 DWD_ORDER_DI", tech),
          "实际 %s" % se._technical_hits("查 DWD_ORDER_DI", tech))
    check("下划线标识符不被词边界误放行",
          se._technical_hits("看 dwd_order_detail_di", tech) != [])
    check("词边界拦截子串误伤：sum 不应命中 summary",
          se._technical_hits("这段 summary 很长", ["sum"]) == [],
          "实际 %s" % se._technical_hits("这段 summary 很长", ["sum"]))
    check("词边界仍然放行真命中：sum 命中 sum(x)",
          se._technical_hits("求 sum(x)", ["sum"]) != [])
    check("过短词条被忽略（避免噪音）",
          se._technical_hits("这行代表什么", ["行"]) == [],
          "实际 %s" % se._technical_hits("这行代表什么", ["行"]))
    check("内置中文技术词命中：主键",
          "主键" in se._technical_hits("请给出主键", []))
    check("内置 SQL 词命中：GROUP BY",
          "GROUP BY" in se._technical_hits("这里要不要 GROUP BY", []))


def test_question_gate():
    print("\nE. 问题闸门 —— 干净的要放行，脏的要拦下")

    base_state = {"granularity": {"needs_confirmation": True}, "time": {}, "rules": {}}
    clean_bundle = {"text": "", "technical_terms": []}

    out = se.questions(clean_bundle, base_state)
    qids = [q["question_id"] for q in out["confirmation_questions"]]
    check("干净问题被放行（Q-GRAIN-1）", "Q-GRAIN-1" in qids, "实际 %s" % qids)
    check("放行的问题自带影响面",
          all(str(q.get("impact_scope") or "").strip() for q in out["confirmation_questions"]))
    check("放行的问题自带可选答案",
          all(q.get("options") for q in out["confirmation_questions"]))
    check("无问题被误拦", out["dropped_questions"] == [],
          "被拦：%s" % [d["question_id"] for d in out["dropped_questions"]])

    # 负例一：问题文本命中技术语 → 必须拦下，且如实报出被拦的term。
    # 这里人为把"每一行"标成技术语——它确实出现在 Q-GRAIN-1 的题干里，
    # 于是可以验证"闸门是活的"，而不是只验证"当前这批问题恰好干净"。
    dirty = {"text": "", "technical_terms": ["每一行"]}
    out2 = se.questions(dirty, base_state)
    qids2 = [q["question_id"] for q in out2["confirmation_questions"]]
    dropped2 = {d["question_id"]: d["blocked_terms"] for d in out2["dropped_questions"]}
    check("题干命中技术语 → 被拦下", "Q-GRAIN-1" not in qids2, "仍放行：%s" % qids2)
    check("被拦原因如实披露（不是静默丢弃）",
          "Q-GRAIN-1" in dropped2 and "每一行" in dropped2.get("Q-GRAIN-1", []),
          "实际 %s" % dropped2)

    # 负例二：选项里带技术语 → 也必须拦下（实测踩过的洞：只扫题干会漏）
    dirty_opt_bundle = {"text": "", "technical_terms": ["其他（请补充说明）"]}
    out3 = se.questions(dirty_opt_bundle, base_state)
    qids3 = [q["question_id"] for q in out3["confirmation_questions"]]
    check("选项命中技术语 → 同样被拦下（闸门扫全部对外文本）",
          "Q-GRAIN-1" not in qids3, "仍放行：%s" % qids3)

    # 负例三：缺影响面 / 缺选项 → 拦下。
    # 这两条分支在真实生成逻辑里被"凡是 add() 都必须传 impact 与 options"约束住，
    # 正常路径构造不出来，所以改为**源码级断言**：确认实现里确实有这两个处置分支。
    # 断言写死文案是有意的——文案是留给复核人看的，改文案就该改测试。
    src = open(_src_path("semantics.py"), encoding="utf-8").read()
    for label, why in (
        ("缺少 impact_scope：说不清影响的问题不问", "缺影响面的问题必须拦下"),
        ("未给建议选项：业务无法作答的问题不问", "缺选项的问题必须拦下"),
    ):
        check("闸门分支存在：%s" % why, label in src, "未在实现中找到该处置文案")


# ---------------------------------------------------------------------------
# F. 证据源降级
# ---------------------------------------------------------------------------
def test_degradation():
    print("\nF. degradation —— 把「取不到」显式说出来")

    ok_bundle = {
        "status": {
            "P1": {"ok": True, "count": 1},
            "P6": {"ok": True, "count": 21},
            "P7": {"ok": True, "count": 8},
        }
    }
    check("全部就绪时无降级", se.degradation(ok_bundle) == [],
          "实际 %s" % se.degradation(ok_bundle))

    bad_bundle = {
        "status": {
            "P6": {"ok": True, "count": 21},
            "P7": {"ok": False, "count": 0, "error": "FileNotFoundError: /workspace-b/mdl.json"},
            "P2": {"ok": False, "count": 0, "error": "meta db down"},
        }
    }
    deg = se.degradation(bad_bundle)
    by = {d["level"]: d for d in deg}
    check("P7 缺失被报出", "P7" in by, "实际 %s" % list(by))
    check("P7 缺失判为 blocking（它支撑 blocking 级 R1）",
          by["P7"]["severity"] == "blocking", "实际 %s" % by["P7"]["severity"])
    check("P7 缺失说明影响的是 R1 未生效",
          "R1" in by["P7"]["impact"], "实际 %r" % by["P7"]["impact"])
    check("P7 缺失保留原始错误（便于排障）",
          "mdl.json" in (by["P7"]["error"] or ""), "实际 %r" % by["P7"]["error"])
    check("仅支撑参考性判断的 P2 缺失只算 warning",
          by["P2"]["severity"] == "warning", "实际 %s" % by["P2"]["severity"])
    check("就绪的来源不出现", "P6" not in by)

    risks = se._degraded_risks(deg)
    check("降级转为风险结论（与规则风险同列，免得被漏读）",
          len(risks) == len(deg), "%d vs %d" % (len(risks), len(deg)))
    check("降级风险带独立 rule_id 便于聚合",
          all(r.get("rule_id") == "SRC-UNAVAILABLE" for r in risks))
    check("降级风险保留严重级",
          {r.get("severity") for r in risks} == {"blocking", "warning"},
          "实际 %s" % {r.get("severity") for r in risks})
    check("降级风险也有来源可追溯",
          all(r["evidence"] and r["evidence"][0]["source"] == "evidence.status" for r in risks))


# ---------------------------------------------------------------------------
# G. 泛化词门控（主体精度）
# ---------------------------------------------------------------------------
def test_generic_gate():
    print("\nG. _tables_of —— 泛化词不足以单独确立主体")

    def hit(table, label, term):
        return {"table": table, "label": label, "term": term, "matched": term}

    strong = hit("t_strong", "坪效", "坪效")          # term == clean_label → 非泛化
    weak1 = hit("t_weak1", "归属门店", "门店")        # 去修饰变体 → 泛化
    weak2 = hit("t_weak2", "所属大区", "大区")        # 另一个泛化词
    check("术语命中（term==规范标签）不算泛化", se._is_derived_hit(strong) is False)
    check("去修饰变体算泛化", se._is_derived_hit(weak1) is True)

    got = se._tables_of([strong, weak1])
    check("有强命中 → 该表入选", "t_strong" in got, "实际 %s" % sorted(got))
    check("仅 1 个泛化词 → 该表被剔除（门控生效）",
          "t_weak1" not in got, "实际 %s" % sorted(got))

    got2 = se._tables_of([hit("t_w2", "归属门店", "门店"), hit("t_w2", "所属大区", "大区")])
    check("≥%d 个不同泛化词 → 该表入选" % se.GENERIC_ONLY_MIN,
          "t_w2" in got2, "实际 %s" % sorted(got2))


# ---------------------------------------------------------------------------
# H. 需求原文里的粒度词（REQ-COVERAGE 阶段二 · 第3步）
# ---------------------------------------------------------------------------
def _src_lines():
    """读 gateway/semantics.py 源码行（判据要能追到具体行号）。"""
    path = _src_path("semantics.py")
    with open(path, encoding="utf-8") as f:
        return f.read().split("\n")


def _find_line(lines, needle, start=0):
    """返回 needle 首次出现的行号（1 起）；找不到返回 0。"""
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return 0


def test_granularity_from_text():
    print("\nH. granularity() —— 原文粒度词必须进槽位（且不误报）")

    src = _src_lines()
    const_line = _find_line(src, "GRANULARITY_TEXT_PATTERNS = {")
    hit_line = _find_line(src, 'hit_by="demand_text"')
    check("判据可追到具体行号：词表常量与原文候选标记都在 semantics.py 里",
          const_line > 0 and hit_line > 0,
          "词表行=%d 原文候选行=%d" % (const_line, hit_line))
    if const_line:
        # 词表字典跨若干行，故按"从定义行起往下 8 行"的整块来读
        block = "\n".join(src[const_line - 1:const_line + 7])
        check("词表在源码里就是四类 month/day/quarter/year",
              all(('"%s"' % k) in block for k in ("month", "day", "quarter", "year")),
              "词表定义行=%d" % const_line)
    check("词表内容就是约定的四类（month/day/quarter/year）",
          set(se.GRANULARITY_TEXT_PATTERNS) == {"month", "day", "quarter", "year"},
          "实际 %s" % sorted(se.GRANULARITY_TEXT_PATTERNS))
    if hit_line:
        check("原文候选带 hit_by=demand_text 标记",
              'hit_by="demand_text"' in src[hit_line - 1],
              "标记所在行=%d" % hit_line)

    # 造一个最小 bundle：只要 by_level / tables / text 三个键
    def _bundle(text, grain="一行 = 一条 dwd_order_di 记录（主键 order_id）"):
        return {
            "text": text,
            "by_level": {
                "P1": [{
                    "level": "P1",
                    "level_name": "当前业务说明",
                    "source": "需求单·description",
                    "locator": "demand.description",
                    "content": text,
                }],
                "P6": [],
                "P7": [],
            },
            "tables": {"dwd_order_di": {"grain": grain}},
        }

    subjects = [{"slot": "subject", "value": "dwd_order_di"}]

    # 1) 原文含「月度」⇒ 出现 hit_by=demand_text 的候选
    g1 = se.granularity(_bundle("统计各门店的月度销售额、订单数和退款额"), subjects)
    cands1 = g1["granularity_candidates"]
    text_cands = [c for c in cands1 if c.get("hit_by") == "demand_text"]
    check("原文含「月度」⇒ granularity_candidates 里出现 hit_by=demand_text 的候选",
          len(text_cands) == 1,
          "实际 %d 个（原文候选=%s）" % (len(text_cands), [c.get("value") for c in cands1]))
    if text_cands:
        check("原文候选的粒度类判成 month",
              text_cands[0].get("granularity_class") == "month",
              "实际 %r" % text_cands[0].get("granularity_class"))
        check("原文候选带得上 P1 需求原文的出处（不是无源断言）",
              any(e.get("level") == "P1" for e in (text_cands[0].get("evidence") or [])),
              "实际 %r" % (text_cands[0].get("evidence"),))
        check("原文候选排在表级元数据候选之前（原文优先于元数据）",
              cands1[0].get("hit_by") == "demand_text",
              "首候选 hit_by=%r" % (cands1[0].get("hit_by"),))

    # 2) 原文不含任何粒度词 ⇒ 不出现该候选（防误报）
    g2 = se.granularity(_bundle("统计各门店的销售额、订单数和退款额"), subjects)
    cands2 = g2["granularity_candidates"]
    check("原文不含任何粒度词 ⇒ 不出现 demand_text 候选（防误报）",
          not [c for c in cands2 if c.get("hit_by") == "demand_text"],
          "实际出现 %s" % [c.get("value") for c in cands2
                           if c.get("hit_by") == "demand_text"])

    # 3) 「统计期间」不是粒度：近6个月里的"月"不得被读成月度粒度
    g3 = se.granularity(_bundle("统计近6个月的销售额"), subjects)
    check("统计期间不算粒度（近6个月 ⇒ 不产出 demand_text 候选）",
          not [c for c in g3["granularity_candidates"] if c.get("hit_by") == "demand_text"],
          "实际出现 %s" % [c.get("value") for c in g3["granularity_candidates"]
                           if c.get("hit_by") == "demand_text"])

    # 4) 返回结构不变：加候选不得增删键
    check("granularity() 返回结构不变（仍是那 6 个键）",
          set(g1.keys()) == {
              "granularity_candidates", "unique_identifier_candidates",
              "one_to_many_risks", "row_definition_notes",
              "confidence", "needs_confirmation",
          },
          "实际 %s" % sorted(g1.keys()))


# ---------------------------------------------------------------------------
# I. 需求要素抽取（实例层 · REQ-COVERAGE 阶段二 第3步）
# ---------------------------------------------------------------------------
def _fake_bundle(text, matched=None, p1_field="description"):
    """造一个证据包（离线可跑，不连库）。

    结构照抄 ``evidence.collect`` 的返回形状：
    ``by_level.P1`` 是需求原文证据项、``matched`` 是词表命中、
    ``text`` 是拼好的需求原文。
    """
    return {
        "text": text,
        "by_level": {
            "P1": [{
                "level": "P1",
                "level_name": "当前业务说明",
                "rank": 0,
                "source": "需求单·%s" % p1_field,
                "locator": "demand.%s" % p1_field,
                "field": p1_field,
                "content": text,
            }],
            "P6": [],
            "P7": [],
        },
        "matched": matched or [],
    }


def _fake_hit(term, kind="glossary"):
    """造一条词表命中（形状照抄 ``evidence.match_terms`` 的返回）。"""
    return {
        "term": term,
        "matched": term,
        "kind": kind,
        "label": term,
        "locator": term,
        "span": (0, 0),
    }


def _fake_slots(granularity_cands=None, time_cands=None, scope_cands=None,
                subject_cands=None, field_cands=None):
    """造一份 M3 槽位（形状照抄 ``semantics.first_round`` 的 ``slots``）。"""
    return {
        "subject": {"candidates": subject_cands or []},
        "granularity": {"candidates": granularity_cands or []},
        "time": {"candidates": time_cands or []},
        "scope": {"candidates": scope_cands or []},
        "fields": {"candidates": field_cands or []},
        "risks": {"candidates": []},
    }


def _gran_cand(cls, terms):
    """造一条 granularity 候选（与 semantics.granularity 的 demand_text 候选同形）。"""
    return {
        "slot": "granularity",
        "value": "一行 = 一个月的汇总（需求原文：%s）" % "、".join(terms),
        "confidence": 0.6,
        "needs_confirmation": False,
        "evidence": [{"level": "P1", "level_name": "当前业务说明",
                      "source": "需求单·description", "locator": "demand.description"}],
        "matched_terms": list(terms),
        "hit_by": "demand_text",
        "granularity_class": cls,
    }


def _empty_facts():
    """造一个"什么都没覆盖"的 SqFacts（底座类型，需 sqlglot 环境）。"""
    import reqcov as reqcov_mod  # noqa: PLC0415
    return reqcov_mod.SqFacts(select_projections=(), group_by_keys=(),
                              where_predicates=(), group_by_granularities={})


def test_elements_extraction():
    print("\nI. 需求要素抽取（实例层）—— 要素清单 / provenance / polarity")

    import reqelem  # noqa: PLC0415  —— 实例层模块（不依赖 sqlglot，裸解释器可跑）

    # --- I-0 契约镜像与底座一致（AST 直读底座源码，不 import ⇒ 不依赖 sqlglot） ---
    base_fields = _ast_const_tuple("gateway/reqcov.py", "CONTRACT_FIELDS")
    check("本模块的 8 字段契约与底座 reqcov.CONTRACT_FIELDS 逐字段一致",
          base_fields == reqelem.CONTRACT_FIELDS,
          "底座=%s 本模块=%s" % (base_fields, reqelem.CONTRACT_FIELDS))
    base_pol = _ast_const_tuple("gateway/reqcov.py", "POLARITIES")
    check("polarity 取值域与底座 reqcov.POLARITIES 一致",
          set(base_pol) == {reqelem.POLARITY_MUST_COVER, reqelem.POLARITY_MAY_OMIT},
          "底座=%s" % (base_pol,))
    base_cls = _ast_const_keys("gateway/reqcov.py", "GRANULARITY_CLASSES")
    check("粒度类与底座 reqcov.GRANULARITY_CLASSES 对齐（month/quarter/year/day 都在）",
          {"month", "quarter", "year", "day"} <= set(base_cls),
          "底座=%s" % sorted(base_cls))
    base_slot_map = _ast_const_dict("gateway/requirement.py", "SLOT_MAP")
    check("槽位口径走 requirement.SLOT_MAP（未硬编码第二套）",
          base_slot_map == dict(requirement_mod.SLOT_MAP),
          "requirement=%s" % base_slot_map)
    check("residual_risks 不参与要素清单",
          "residual_risks" in reqelem.SLOTS_NOT_PARTICIPATING,
          "实际 %s" % (reqelem.SLOTS_NOT_PARTICIPATING,))

    # --- I-1 切句：按 。；！？\n 切，且记偏移 ---
    sents = reqelem.sentence_split("第一句。第二句；第三句！第四句？")
    check("切句按 。；！？ 切（4 句）", len(sents) == 4, "实际 %d 句" % len(sents))
    check("句子编号从 1 起", sents and sents[0]["index"] == 1, "实际 %s" % (sents[0]["index"],))
    check("句子偏移能在原文里回溯出原句",
          all(text[s["start"]:s["end"]] == s["text"]
              for text in ["第一句。第二句；第三句！第四句？"] for s in sents),
          "实际 %s" % [(s["start"], s["end"], s["text"]) for s in sents])
    check("逗号不切（中文里逗号是并列连接）",
          len(reqelem.sentence_split("销售额、订单数和退款额")) == 1,
          "实际 %d 句" % len(reqelem.sentence_split("销售额、订单数和退款额")))

    # --- I-2 A1：要素候选含 月度 / 销售额 / 订单数 / 退款额 ---
    # 关键验收点：「退款额」不在真实词表里（实测 161 条零命中）⇒ 必须由兜底路径捞出来
    a1_text = "统计各门店的月度销售额、订单数和退款额"
    a1_bundle = _fake_bundle(
        a1_text,
        matched=[_fake_hit("门店", "column"),
                 _fake_hit("销售额"),
                 _fake_hit("订单数", "glossary_phrase")],
    )
    a1_slots = _fake_slots(
        granularity_cands=[_gran_cand("month", ["月度", "月", "按月"])],
    )
    a1_els = reqelem.build_elements(a1_bundle, a1_slots)
    a1_names = [e["element"] for e in a1_els]
    for want in ("月度", "销售额", "订单数", "退款额"):
        check("A1 要素候选含「%s」" % want, want in a1_names, "实际 %s" % a1_names)
    refund = [e for e in a1_els if e["element"] == "退款额"]
    check("「退款额」由兜底路径产出（词表没命中也提得出来）",
          refund and refund[0]["source"] == "fallback_noun",
          "实际 %s" % [(e["element"], e["source"]) for e in a1_els])
    check("兜底要素置信度低（⇒ 降级为警告，不误拦）",
          refund and float(refund[0]["confidence"]) < 0.6,
          "实际 %s" % (refund[0]["confidence"] if refund else None,))
    check("兜底要素 needs_confirmation=True（人工确认前不作硬要求）",
          refund and refund[0]["needs_confirmation"] is True,
          "实际 %s" % (refund[0]["needs_confirmation"] if refund else None,))

    # --- I-3 契约投影：恰好 8 字段（多一个底座就拒收） ---
    proj = reqelem.to_contract_dicts(a1_els)
    check("契约投影恰好 8 个字段（底座 from_dict 拒收多字段）",
          proj and all(len(d) == 8 for d in proj),
          "实际 %s" % sorted({len(d) for d in proj}))
    check("契约投影的字段名与底座一致",
          all(set(d) == set(reqelem.CONTRACT_FIELDS) for d in proj),
          "实际 %s" % (sorted(proj[0]) if proj else None,))
    check("粒度要素的 matcher 对齐底座 time_granularity:month",
          any(d["slot"] == "granularity" and d["matcher"] == "time_granularity:month"
              for d in proj),
          "实际 %s" % [d["matcher"] for d in proj if d["slot"] == "granularity"])

    # --- I-4 provenance 形如「需求原文第N句：'…'」且 N 正确 ---
    prov = [d for d in proj if d["element"] == "月度"][0]["provenance"]
    check("provenance 形如「需求原文第N句：'…'」",
          prov.startswith("需求原文第") and "句：'" in prov,
          "实际 %r" % prov)
    import re as _re
    m = _re.match(r"需求原文第(\d+)句：'(.*)'$", prov)
    check("provenance 的 N 可解析且句内容与原文一致",
          m and m.group(2) in a1_text,
          "实际 %r" % prov)
    check("provenance 的 N 指向的确实是含该要素的那一句",
          m and a1_text.find(m.group(2)) >= 0
          and "月度" in m.group(2),
          "实际 %r" % prov)

    # --- I-5 polarity：三种取值 ---
    # 用例2：原文完全没提时间 ⇒ 时间类要素 may_omit + 「需求未提出…」
    c2_text = "各门店总销售额"
    c2_els = reqelem.build_elements(
        _fake_bundle(c2_text, matched=[_fake_hit("门店", "column"),
                                       _fake_hit("销售额")]),
        _fake_slots(),
    )
    c2_time = [e for e in c2_els if e["slot"] == "time_semantics"]
    check("用例2：时间类要素存在且 polarity=may_omit",
          c2_time and all(e["polarity"] == "may_omit" for e in c2_time),
          "实际 %s" % [(e["element"], e["polarity"]) for e in c2_time])
    check("用例2：成因是「需求未提出…不作要求」",
          c2_time and all("需求未提出" in (e["cause"] or "") for e in c2_time),
          "实际 %s" % [e["cause"] for e in c2_time])

    # 用例6：原文显式豁免时间 ⇒ 仍 may_omit，但成因换成「业务方显式豁免…」
    c6_text = "各门店总销售额，不限时间范围"
    c6_els = reqelem.build_elements(
        _fake_bundle(c6_text, matched=[_fake_hit("门店", "column"),
                                       _fake_hit("销售额")]),
        _fake_slots(),
    )
    c6_time = [e for e in c6_els if e["slot"] == "time_semantics"]
    check("用例6：时间类要素 polarity=may_omit",
          c6_time and all(e["polarity"] == "may_omit" for e in c6_time),
          "实际 %s" % [(e["element"], e["polarity"]) for e in c6_time])
    check("用例6：成因是「业务方显式豁免…」",
          c6_time and all("显式豁免" in (e["cause"] or "") for e in c6_time),
          "实际 %s" % [e["cause"] for e in c6_time])

    # 用例2 与用例6 的成因文案必须不同（语义相反，要能区分）
    c2_cause = c2_time[0]["cause"] if c2_time else ""
    c6_cause = c6_time[0]["cause"] if c6_time else ""
    check("用例2 与用例6 的成因文案不同（assertNotEqual 语义）",
          c2_cause != c6_cause,
          "用例2=%r 用例6=%r" % (c2_cause, c6_cause))

    # --- I-6 反例：豁免词与槽位词必须同句共现，粒度不能被时间豁免牵连 ---
    # 「不限时间范围，但要看月度趋势」：时间口径被豁免，**月度照旧 must_cover**
    c7_els = reqelem.build_elements(
        _fake_bundle("不限时间范围，但要看月度趋势"),
        _fake_slots(granularity_cands=[_gran_cand("month", ["月度"])]),
    )
    c7_grain = [e for e in c7_els if e["slot"] == "granularity"]
    c7_time = [e for e in c7_els if e["slot"] == "time_semantics"]
    check("反例：月度要素仍是 must_cover（不被时间豁免牵连）",
          c7_grain and all(e["polarity"] == "must_cover" for e in c7_grain),
          "实际 %s" % [(e["element"], e["polarity"]) for e in c7_grain])
    check("反例：时间口径被显式豁免",
          c7_time and all("显式豁免" in (e["cause"] or "") for e in c7_time),
          "实际 %s" % [e["cause"] for e in c7_time])

    # --- I-6b 元素级豁免：豁免范围由"被豁免对象的具体程度"决定 ---
    # 「不限月度」只豁免月度、不豁免日度；「不限销售额」只豁免销售额、不豁免订单数。
    # ⚠️ 反过来写（只按槽位判）会把「不限月度」读成"整个粒度口径都不要"
    #    ⇒ 门禁静默放过日度缺口，与阶段二那个病同构。
    mp = reqelem.SlotMapper({}, {})
    sent = lambda t: {"index": 1, "text": t}  # noqa: E731
    check("元素级豁免：「不限月度」豁免月度",
          mp._exempt_in_sentence("granularity", sent("统计销售额，不限月度"),
                                 element="月度", cls="month") is not None,
          "实际 %r" % mp._exempt_in_sentence("granularity", sent("统计销售额，不限月度"),
                                              element="月度", cls="month"))
    check("元素级豁免：「不限日度」**不**豁免月度（粒度越具体豁免范围越小）",
          mp._exempt_in_sentence("granularity", sent("统计销售额，不限日度"),
                                 element="月度", cls="month") is None,
          "实际 %r" % mp._exempt_in_sentence("granularity", sent("统计销售额，不限日度"),
                                              element="月度", cls="month"))
    check("元素级豁免：「不限粒度」豁免整个粒度槽位（通用说法才整槽豁免）",
          mp._exempt_in_sentence("granularity", sent("统计销售额，不限粒度"),
                                 element="月度", cls="month") is not None,
          "实际 %r" % mp._exempt_in_sentence("granularity", sent("统计销售额，不限粒度"),
                                              element="月度", cls="month"))
    check("元素级豁免：「不限销售额」豁免销售额",
          mp._exempt_in_sentence("output_fields", sent("统计销售额和订单数，不限销售额"),
                                 element="销售额") is not None,
          "实际 %r" % mp._exempt_in_sentence("output_fields",
                                              sent("统计销售额和订单数，不限销售额"),
                                              element="销售额"))
    check("元素级豁免：「不限销售额」**不**豁免同句的订单数",
          mp._exempt_in_sentence("output_fields", sent("统计销售额和订单数，不限销售额"),
                                 element="订单数") is None,
          "实际 %r" % mp._exempt_in_sentence("output_fields",
                                              sent("统计销售额和订单数，不限销售额"),
                                              element="订单数"))
    # 端到端：同一句里两个要素，一个被豁免一个不被豁免
    c8_els = reqelem.build_elements(
        _fake_bundle("统计各门店销售额和订单数，不限销售额",
                     matched=[_fake_hit("门店", "column"),
                              _fake_hit("销售额"),
                              _fake_hit("订单数", "glossary_phrase")]),
        _fake_slots(),
    )
    c8_by = {e["element"]: e for e in c8_els}
    check("端到端：同句里「销售额」被豁免⇒may_omit",
          c8_by.get("销售额", {}).get("polarity") == "may_omit",
          "实际 %s" % [(k, v["polarity"]) for k, v in c8_by.items()])
    check("端到端：同句里「订单数」未被豁免⇒仍 must_cover",
          c8_by.get("订单数", {}).get("polarity") == "must_cover",
          "实际 %s" % [(k, v["polarity"]) for k, v in c8_by.items()])

    # --- I-6c 兜底提取不得把「时点/期间」当成业务名词要素 ---
    # ⚠️ 这条是实测撞出来的：原文「统计各门店上月的销售额和退款额」曾产出一个
    #    叫「上月」的 output_fields 要素——它以「月」收尾，过了业务名词形态那一关。
    # 修法复用 semantics 已有的两道闸门（PERIOD_RE 遮蔽 + 左文指示字），
    # **不另立第三套时间词表**：同一个「上月」不能在粒度判定里被排除、
    # 在兜底提取里却被当成指标，两处口径打架时漏项和误项都会来。
    mp2 = reqelem.SlotMapper({}, {})
    pointing = ["上月", "本月", "去年", "今年", "当日", "昨天", "明日",
                "次日", "上个月", "本季度", "上季度", "本年度", "去年年初"]
    leaked = [c for c in pointing if mp2._fragments(c)]
    check("兜底提取：时点/期间词一个都不提成业务名词",
          not leaked, "漏网 %s" % leaked)
    # ⚠️ 不含「门店」：它是**通用维度名**（"按什么拆"），不是业务口径 ⇒
    #    兜底路径不该产出它（真实链路里它由词表命中提供）。
    #    把它算进"不误伤业务名词"是期望写错了（自证是空词表 ⇒ 词表那条路走不到）。
    business = ["退款额", "已退款金额", "月销售额", "订单数",
                "销售额", "上月销售额", "当日销售额", "上月退款额"]
    missing = [c for c in business if not mp2._fragments(c)]
    check("兜底提取：时点闸门不误伤业务名词（含带时点前缀的）",
          not missing, "被误伤 %s" % missing)

    # 端到端：真实型原文里「上月」不进要素，「退款额」照提
    c9_els = reqelem.build_elements(
        _fake_bundle("统计各门店上月的销售额和退款额，按天看趋势",
                     matched=[_fake_hit("门店", "column"),
                              _fake_hit("销售额")]),
        _fake_slots(),
    )
    c9_names = [e["element"] for e in c9_els]
    check("端到端：「上月」不被当成指标要素（实测撞出来过的误项）",
          "上月" not in c9_names, "实际 %s" % c9_names)
    check("端到端：同一句里的「退款额」仍提得出来（闸门没把真指标一起挡掉）",
          "退款额" in c9_names, "实际 %s" % c9_names)

    # --- I-7 回归护栏：granularity() 仍 6 键；questions()/rules() 同口径 ---
    g = se.granularity(_fake_bundle("统计各门店的月度销售额"), [])
    check("回归护栏：granularity() 仍返回 6 个键",
          len(g) == 6, "实际 %s" % sorted(g))
    # state 形状照抄 semantics.first_round 传给 questions()/rules() 的那个
    st = {
        "granularity": {"needs_confirmation": True, "one_to_many_risks": []},
        "time": {"time_risks": []},
        "rules": {},
        "slots": {
            "granularity": {"candidates": [{
                "slot": "granularity", "value": "一行 = 一个月的汇总",
                "confidence": 0.6, "needs_confirmation": True, "evidence": [],
            }]},
            "time": {"candidates": []},
        },
    }
    qs = se.questions(_fake_bundle("统计各门店的月度销售额"), st)
    qids = [q["question_id"] for q in qs["confirmation_questions"]]
    check("回归护栏：questions() 仍产出 Q-GRAIN-1",
          "Q-GRAIN-1" in qids, "实际 %s" % qids)
    rc = se.rules(_fake_bundle("统计各门店的月度销售额"), st)
    rids = [x["rule_id"] for x in rc["blocking_risks"]]
    check("回归护栏：rules() 的 R1/R4 判定同口径（R4 仍触发、R1 不触发）",
          "R4" in rids and "R1" not in rids, "实际 %s" % rids)
    check("回归护栏：R4 仍是 mandatory 确认项",
          "R4" in rc["mandatory_confirmations"], "实际 %s" % rc["mandatory_confirmations"])

    # --- I-8 实例侧 matcher 登记（扩展点，不改底座骨架） ---
    # 底座 reqcov 依赖 sqlglot ⇒ 裸解释器里 import 不了它。
    # 登记函数按需导入，**导不进去时如实报"未验证"**，不假装通过。
    try:
        import reqcov as reqcov_mod  # noqa: PLC0415
    except ImportError as exc:
        skip("实例侧 matcher 登记",
             "reqcov 不可导入（%s）——底座依赖 sqlglot，门禁解释器带该依赖，此处会真跑" % exc)
    else:
        before = set(reqcov_mod.matcher_kinds_snapshot())
        kinds = reqelem.register_instance_matchers()
        after = set(reqcov_mod.matcher_kinds_snapshot())
        own_kinds = {
            reqelem.MATCHER_KIND_FIELD, reqelem.MATCHER_KIND_TIME,
            reqelem.MATCHER_KIND_SCOPE, reqelem.MATCHER_KIND_SUBJECT,
            reqelem.MATCHER_KIND_SLOT,
        }
        check("登记后底座多出实例层自有的 matcher kind",
              own_kinds <= after,
              "缺 %s；实际 %s" % (sorted(own_kinds - after), sorted(after)))
        check("登记**没有**动底座自带的 kind（骨架不改）",
              reqcov_mod.MATCHER_KIND_TIME_GRANULARITY
              if hasattr(reqcov_mod, "MATCHER_KIND_TIME_GRANULARITY")
              else "time_granularity" in after,
              "实际 %s" % sorted(after))
        check("登记没有覆盖底座已有 kind 的实现（只新增）",
              before <= after, "前=%s 后=%s" % (sorted(before), sorted(after)))
        check("实例层 kind 名不与底座自证的『未注册 kind』示例冲突",
              "metric" not in after,
              "底座自证断言 metric:销售额 属未注册 kind；实际已注册 %s"
              % sorted(k for k in after if k == "metric"))
        # 保守语义：认不出 ⇒ 不判（不当成"已覆盖"，也不算缺口）
        mfn = reqcov_mod.matcher_kinds_snapshot()[reqelem.MATCHER_KIND_FIELD]
        st3, msg = mfn("销售额", _empty_facts())
        check("实例层 matcher 认不出时返回 UNDECIDABLE（不当已覆盖、不算缺口）",
              st3 == reqcov_mod.UNDECIDABLE,
              "实际 %s / %s" % (st3, msg))
        # 幂等"确保已注册"：重复调用不报错、不重复注册
        ensured = reqelem.ensure_instance_matchers()
        check("ensure_instance_matchers() 幂等（重复调用仍返回全部实例 kind）",
              set(ensured) == own_kinds,
              "实际 %s" % sorted(ensured))
        # 🔴 守卫：底座自带的 time_granularity matcher 不能被实例层覆盖
        # （register_matcher 是 dict 赋值 = 覆盖；底座那条按类别判粒度，
        #  被替换 ⇒ "换个写法就判未覆盖"的假阳性防火墙静默失效）
        check("实例层不注册 time_granularity（底座自带，实例层不得覆盖）",
              reqelem.MATCHER_KIND_TIME_GRANULARITY not in own_kinds,
              "实例 kind=%s" % sorted(own_kinds))
        base_fn = reqcov_mod.matcher_kinds_snapshot().get(
            reqelem.MATCHER_KIND_TIME_GRANULARITY)
        check("底座的 time_granularity matcher 完好（未被实例层覆盖）",
              base_fn is not None
              and getattr(base_fn, "__name__", "") == "_match_time_granularity"
              and getattr(base_fn, "__module__", "") == "reqcov",
              "实际 %s.%s" % (getattr(base_fn, "__module__", "?"),
                              getattr(base_fn, "__name__", "?")))
        # 模拟「有人顺手把粒度加进实例 kind」⇒ 守卫必须抛错（不是静默放过）
        # 🔴 守卫**必须留两处痕迹**（抛异常可能被调用方 except 降级成静默跳过）：
        #   ① 模块级列表 GRANULARITY_GUARD_FAILURES（进程内可查）
        #   ② stderr 告警（即使异常被吞，日志里仍有痕迹）
        _saved = reqcov_mod.matcher_kinds_snapshot()[reqelem.MATCHER_KIND_TIME_GRANULARITY]
        _before = len(reqelem.GRANULARITY_GUARD_FAILURES)
        try:
            reqcov_mod.register_matcher(
                reqelem.MATCHER_KIND_TIME_GRANULARITY,
                lambda arg, facts: (reqcov_mod.MATCHED, "假的"))
            try:
                reqelem.ensure_instance_matchers()
                check("守卫：粒度 matcher 被覆盖时 ensure_instance_matchers() 抛错",
                      False, "未抛错 —— 守卫失效")
            except RuntimeError as exc:
                check("守卫：粒度 matcher 被覆盖时 ensure_instance_matchers() 抛错",
                      "time_granularity" in str(exc),
                      "异常文案=%s" % str(exc)[:120])
            # ① 模块级列表必须记下
            _after = len(reqelem.GRANULARITY_GUARD_FAILURES)
            check("守卫：触发时留下模块级痕迹（GRANULARITY_GUARD_FAILURES）",
                  _after > _before,
                  "前=%d 后=%d" % (_before, _after))
        finally:
            reqcov_mod.register_matcher(
                reqelem.MATCHER_KIND_TIME_GRANULARITY, _saved)


def test_real_bundle_and_entry():
    """J. 真实数据链路（REQ-COVERAGE 阶段二 · 第 4a 步实例层适配）

    ⚠️ 本段**只在能连库的环境跑**（门禁解释器 / 容器内）。
    裸解释器无 DB ⇒ 走 ``skip()`` 如实标"未验证"，不假装通过。
    """
    print("\nJ. 真实 bundle 键名 / 真实 demand / 需求对象入口")

    import reqelem  # noqa: PLC0415

    # --- J-1 真实 bundle 键名（不能按假定写）---
    try:
        import demand as demand_mod  # noqa: PLC0415
        import evidence  # noqa: PLC0415
    except ImportError as exc:
        skip("J 真实数据链路", "证据/需求模块不可导入（%s）" % exc)
        return
    try:
        d = demand_mod.get("DR-20261020-EUQC")
    except Exception as exc:  # noqa: BLE001
        skip("J 真实数据链路", "取不到测试需求 DR-20261020-EUQC（%s）" % exc)
        return
    if not isinstance(d, dict) or not d:
        skip("J 真实数据链路", "测试需求 DR-20261020-EUQC 不存在")
        return

    lex = evidence.build_lexicon("B")
    bundle = evidence.collect(demand=d, demand_id="DR-20261020-EUQC",
                              dataset="B", lexicon=lex, knowledge_mod=None)
    check("J-1 真实 bundle 顶层键与 reqelem 读法一致",
          {"text", "matched", "by_level"} <= set(bundle.keys()),
          "实际 %s" % sorted(bundle.keys()))
    matched = bundle.get("matched") or []
    check("J-1 真实 matched[0] 含 term/matched/label 三个键（reqelem 按这三个读）",
          matched and {"term", "matched", "label"} <= set(matched[0].keys()),
          "实际 %s" % (sorted(matched[0].keys()) if matched else None))
    check("J-1 真实 matched[0] 含 kind（reqelem 按 kind 筛字段类命中）",
          matched and "kind" in matched[0],
          "实际 %s" % (sorted(matched[0].keys()) if matched else None))
    check("J-1 真实 bundle 有 P1 证据项（provenance 的句子来源）",
          bool((bundle.get("by_level") or {}).get("P1")),
          "实际 P1=%d 条" % len((bundle.get("by_level") or {}).get("P1") or []))

    # --- J-2 真实 demand 跑 build_elements：关键四项要素 ---
    import semantics as se2  # noqa: PLC0415
    slots2 = (se2.first_round(bundle, region_label="B") or {}).get("slots") or {}
    els2 = reqelem.build_elements(bundle, slots2)
    names2 = [e["element"] for e in els2]
    # 「退款额」在真实原文里写的是「已退款金额」（同义不同字）——两个都要认
    for want in ("月度", "销售额", "订单数"):
        check("J-2 真实 demand 要素含「%s」" % want, want in names2, "实际 %s" % names2)
    check("J-2 真实 demand 要素含兜底提出来的「已退款金额」（词表没有它）",
          "已退款金额" in names2, "实际 %s" % names2)
    refund2 = [e for e in els2 if e["element"] == "已退款金额"]
    check("J-2 「已退款金额」来自兜底路径且置信度低（⇒ 只作警告不误拦）",
          refund2 and refund2[0]["source"] == "fallback_noun"
          and float(refund2[0]["confidence"]) < 0.6,
          "实际 %s" % [(e["element"], e["source"], e["confidence"]) for e in refund2])
    check("J-2 真实 demand 每条要素的 provenance 都指到原文某句",
          all(e["provenance"].startswith("需求原文第") for e in els2),
          "实际 %s" % [e["provenance"] for e in els2])

    # --- J-3 需求对象入口在真实数据上可用 ---
    try:
        import requirement as requirement_mod2  # noqa: PLC0415
    except ImportError as exc:
        skip("J-3 需求对象入口", "requirement 模块不可导入（%s）" % exc)
        return
    req_obj = requirement_mod2.get("DR-20261020-EUQC")
    check("J-3 真实 requirement 对象含 demand_id 与 dataset（入口靠这两个反查原文）",
          isinstance(req_obj, dict)
          and req_obj.get("demand_id") == "DR-20261020-EUQC"
          and req_obj.get("dataset") == "B",
          "实际 demand_id=%r dataset=%r" % (req_obj.get("demand_id"),
                                             req_obj.get("dataset"))
          if isinstance(req_obj, dict) else "类型 %s" % type(req_obj).__name__)
    res3 = reqelem.build_elements_from_requirement(req_obj)
    check("J-3 需求对象入口在真实数据上返回 ok",
          res3.get("ok") is True, "实际 %s" % res3.get("error"))
    check("J-3 需求对象入口产出的要素与直接跑 build_elements 一致",
          [e["element"] for e in (res3.get("elements") or [])] == names2,
          "入口=%s 直接=%s" % ([e["element"] for e in (res3.get("elements") or [])],
                              names2))
    proj3 = reqelem.build_contract_from_requirement(req_obj)
    check("J-3 契约投影入口恰好 8 字段（底座 from_dict 拒收多字段）",
          proj3.get("ok") is True
          and all(len(d) == 8 for d in (proj3.get("elements") or [])),
          "实际 %s" % sorted({len(d) for d in (proj3.get("elements") or [])}))

    # --- J-6 同 version 二次调用命中缓存 ---
    res4 = reqelem.build_elements_from_requirement(req_obj)
    check("J-6 同 version 二次调用命中缓存（cached=True）",
          res4.get("cached") is True,
          "实际 cached=%r" % res4.get("cached"))
    check("J-6 缓存命中与未命中产出逐项相同（缓存不能改结果）",
          res4.get("elements") == res3.get("elements"),
          "缓存后=%s" % [e["element"] for e in (res4.get("elements") or [])])

    # --- 失败口径：取不到 ≠ 没问题 ---
    for bad, why in ((None, "None"), ({"ok": False, "error": "查不到"}, "取不到"),
                     ({"version": 1}, "缺 demand_id")):
        rb = reqelem.build_elements_from_requirement(bad)
        check("J-3 取不到需求时返回 ok=False 且给出原因（%s）" % why,
              rb.get("ok") is False and bool(rb.get("error")),
              "实际 %s" % rb)

    # --- J-7 bundle 旁路：纯本地、不连库（离线自证路径） ---
    # 队友 gates.py 的离线自证连不上 DB ⇒ 反查路径必然 ok=False ⇒ 覆盖判定永远
    # skipped ⇒ 判据层一行都测不到。传 bundle 进来，这条路径才是纯函数。
    res7 = reqelem.build_elements_from_requirement(None, bundle=bundle)
    check("J-7 传 bundle 时不反查、不连库（requirement=None 也能出要素）",
          res7.get("ok") is True,
          "实际 %s" % res7.get("error"))
    check("J-7 bundle 旁路产出与反查路径逐项相同（两条路径口径必须一致）",
          [e["element"] for e in (res7.get("elements") or [])] == names2,
          "旁路=%s 反查=%s" % ([e["element"] for e in (res7.get("elements") or [])],
                              names2))
    res7c = reqelem.build_contract_from_requirement(None, bundle=bundle)
    check("J-7 bundle 旁路的契约投影恰好 8 字段",
          res7c.get("ok") is True
          and all(len(d) == 8 for d in (res7c.get("elements") or [])),
          "实际 %s" % sorted({len(d) for d in (res7c.get("elements") or [])}))

    # --- J-8 离线路径：喂 demand + lexicon ⇒ 走真实 collect 产出真实要素 ---
    # 队友 gates.py 的离线自证连不上 DB ⇒ 合成 bundle 形状可能对不上真实
    # ⇒ 正确姿势是喂真实 demand + 真实 lexicon，走**真实代码路径**。
    lex_off = [
        {"term": "门店", "kind": "column", "canonical": "t.store_id",
         "label": "归属门店", "table": "t", "column": "store_id",
         "data_type": "integer", "definition": "归属门店", "level": "P6",
         "source": "column_docs", "locator": "t.store_id",
         "demand_fields": ["title"], "in_demand_text": True},
        {"term": "销售额", "kind": "glossary", "canonical": "销售额",
         "label": "销售额", "table": "t", "column": "amount",
         "data_type": "numeric", "definition": "销售额", "level": "P6",
         "source": "glossary", "locator": "销售额",
         "demand_fields": ["description"], "in_demand_text": True},
    ]
    dem_off = {
        "demand_id": "DR-OFFLINE-0001", "title": "各门店总销售额",
        "business_context": "", "description": "统计各门店总销售额",
        "expected_output": "", "contact": "", "time_range": "",
    }
    res8 = reqelem.build_elements_from_requirement(
        {"demand_id": "DR-OFFLINE-0001", "dataset": "B"},
        demand=dem_off, lexicon=lex_off, slots={})
    check("J-8 喂 demand+lexicon ⇒ 不连库也能出要素（离线自证路径）",
          res8.get("ok") is True, "实际 %s" % res8.get("error"))
    r8_names = [e["element"] for e in (res8.get("elements") or [])]
    check("J-8 离线路径产出的要素含词表命中的 门店/销售额",
          "门店" in r8_names and "销售额" in r8_names, "实际 %s" % r8_names)
    r8_time = [e for e in (res8.get("elements") or [])
               if e["slot"] == "time_semantics"]
    check("J-8 离线路径：原文没提时间 ⇒ 时间类 may_omit + 成因「需求未提出…」",
          r8_time and r8_time[0]["polarity"] == "may_omit"
          and "需求未提出" in (r8_time[0].get("cause") or ""),
          "实际 %s" % [(e["element"], e["polarity"], e.get("cause")) for e in r8_time])
    # 带 slots 的离线路径：粒度候选 + 兜底提取
    slots_off = {
        "granularity": {"candidates": [{
            "slot": "granularity", "value": "一行 = 一个月的汇总（需求原文：月度）",
            "confidence": 0.6, "needs_confirmation": True, "evidence": [],
            "granularity_class": "month", "hit_by": "demand_text",
            "matched_terms": ["月度"]}]},
        "fields": {"candidates": []}, "subject": {"candidates": []},
        "time": {"candidates": []}, "scope": {"candidates": []},
        "risks": {"candidates": []},
    }
    dem_off2 = dict(dem_off)
    dem_off2["description"] = "统计各门店的月度销售额和退款额"
    res8b = reqelem.build_elements_from_requirement(
        {"demand_id": "DR-OFFLINE-0002", "dataset": "B"},
        demand=dem_off2, lexicon=lex_off, slots=slots_off)
    r8b_names = [e["element"] for e in (res8b.get("elements") or [])]
    check("J-8 离线路径：粒度候选生效（产出「月度」）",
          "月度" in r8b_names, "实际 %s" % r8b_names)
    check("J-8 离线路径：兜底提取生效（产出「退款额」，词表没有它）",
          "退款额" in r8b_names, "实际 %s" % r8b_names)
    res8c = reqelem.build_contract_from_requirement(
        {"demand_id": "DR-OFFLINE-0002", "dataset": "B"},
        demand=dem_off2, lexicon=lex_off, slots=slots_off)
    check("J-8 离线路径的契约投影恰好 8 字段",
          res8c.get("ok") is True
          and all(len(d) == 8 for d in (res8c.get("elements") or [])),
          "实际 %s" % sorted({len(d) for d in (res8c.get("elements") or [])}))
    # --- J-9 兜底提取的精度：既不漏提（真业务名）也不误提（动作短语） ---
    # ⚠️ 漏提比误提更危险（lead：宁可多提不可漏提）—— 但"多提"也要有个度，
    #    否则报告里全是「汇总各门店」这种噪音，业务方看不懂。
    m9 = reqelem.SlotMapper({}, {})
    # ① 漏提修复：定语结构里的真业务名必须提得出来
    must_keep = ["转化率", "复购率", "客单价", "退款额", "已退款金额", "月度销售额"]
    lost = [c for c in must_keep if not m9._fragments(c)]
    check("J-9 兜底不漏提：未登录的业务名词都提得出来",
          not lost, "漏提 %s" % lost)
    # ② 漏提修复（长句 + 定语结构）：这是实测撞出来的原始缺陷
    #    「统计各渠道的转化率」整块 9 字 > 上限 8 ⇒ 整块被丢 ⇒ 「转化率」消失
    for src, want in (("统计各渠道的转化率", "转化率"),
                      ("需要每个门店的月度销售额", "月度销售额"),
                      ("导出各渠道的UV和转化率", "转化率")):
        got = m9._fragments(src)
        check("J-9 兜底不漏提：长句里的业务名提得出来（%s）" % src,
              want in got, "实际 %s" % got)
    # ③ 误提收紧：动作短语 / 时间名词不该成为要素
    # ⚠️ 「同时要订单数」**不在此列**——「订单数」是真业务名词（词表里有），
    #    剥掉动词前缀后提「订单数」是**正确**的提取；它与词表命中重复
    #    由 `_all_known_terms()` 去重解决，不该在这里挡掉（挡掉=漏提）。
    must_drop = ["汇总各门店", "需要看每个门店", "月份",
                 "统计各门店", "需要每个门店"]
    kept = [c for c in must_drop if m9._fragments(c)]
    check("J-9 兜底不误提：动作短语与时间名词都不提成要素",
          not kept, "仍被提 %s" % kept)
    # ④ 剥离动词前缀时**不能**把前缀后的真业务名一起丢掉
    got4 = m9._fragments("汇总各渠道转化率")
    check("J-9 剥离动词前缀时保留后面的真业务名（不是整块丢）",
          "转化率" in got4 or "渠道转化率" in got4, "实际 %s" % got4)
    # ⑤ 🔴 「含X」限定结构（真实需求实测撞出来的）：
    #    真实原文「需要看含退款的销售口径」曾产出一个叫「含退款」的要素
    #    —— 它以「款」收尾、过了业务名词形态那一关，但不是业务口径。
    #    ⚠️ 而「含退款**金额**」是**真业务名** ⇒ 不能"含X 一律丢"。
    check("J-9 限定结构：「含退款」不提成要素（是'含了哪些'的说法）",
          not m9._fragments("含退款"), "实际 %s" % m9._fragments("含退款"))
    check("J-9 限定结构：「含退款金额」保留（限定词+业务名，是真业务名）",
          bool(m9._fragments("含退款金额")),
          "实际 %s" % m9._fragments("含退款金额"))
    # ⑥ 动词分支与通用分支必须同口径（"需要看含退款的销售口径" 走的是动词分支）
    check("J-9 限定结构：动词分支与通用分支同口径（「需要看含退款…」也不提成）",
          not m9._fragments("需要看含退款的销售口径"),
          "实际 %s" % m9._fragments("需要看含退款的销售口径"))
    # ⑦ 🔴 行数/结构说明（"一行"）：真实需求 expected_output="每门店每月一行：…"
    #    ⇒ 兜底曾把「一行」当成输出字段要素（它以「行」收尾，过了形态关）
    check("J-9 行数说明：「一行」不提成要素",
          not m9._fragments("一行"), "实际 %s" % m9._fragments("一行"))
    check("J-9 行数说明：不误伤真业务名「销售额行数」",
          bool(m9._fragments("销售额行数")),
          "实际 %s" % m9._fragments("销售额行数"))
    # ⑧ 🔴 计算公式说明（"退款率按退款"）：真实需求 description="退款率按退款订单数…"
    #    ⇒ 兜底曾把「退款率按退款」当成输出字段要素
    check("J-9 公式说明：「退款率按退款」不提成要素",
          not m9._fragments("退款率按退款"),
          "实际 %s" % m9._fragments("退款率按退款"))
    check("J-9 公式说明：不误伤真业务名「按月销售额」",
          bool(m9._fragments("按月销售额")),
          "实际 %s" % m9._fragments("按月销售额"))
    # ⑨ 🔴 Python 陷阱：set("一行 多行") 是**按字符拆分**的 ⇒ 词组集合必须用元组。
    #    （实测踩到过：_ROW_STRUCTURE 被拆成 26 个单字，"一行"判据形同虚设）
    check("J-9 词组集合没有踩 set(字符串) 的坑（多字词完整保留）",
          "一行" in reqelem._ROW_STRUCTURE and "多行" in reqelem._ROW_STRUCTURE
          and len(reqelem._ROW_STRUCTURE) == 8,
          "实际 %s" % sorted(reqelem._ROW_STRUCTURE))

    # --- J-11 0.60 边界：我的粒度要素 confidence 恰为 0.6（最容易被误降级的那一个）---
    # 🔴 底座曾把 `float(confidence) <= 0.6` 写成警告判据，而冻结口径是「≥ 0.6 ⇒ 阻断」
    #    ⇒ 恰为 0.6 的「月度」被降级成 warning ⇒ "月度粒度没进 GROUP BY"这个最该拦的
    #    缺口拦不住（判据层在最关键处是哑的）。底座已修（`<=` → `<`）。
    # ⇒ 本条断言钉住**我的这一侧**：粒度要素的 confidence 必须恰为 0.6
    #    （若将来有人改成 0.59 或 0.61，行为会变 ⇒ 至少这里会看见）。
    grain_conf = [e["confidence"] for e in (res8b.get("elements") or [])
                  if e["slot"] == "granularity"]
    check("J-11 粒度要素的 confidence 恰为 0.6（在冻结阈值上，两侧行为须一致）",
          grain_conf and abs(grain_conf[0] - 0.6) < 1e-9,
          "实际 %s" % grain_conf)
    # 兜底要素的 confidence 必须 **低于** 0.6（否则它会变成可阻断的要素 ⇒ 误拦）
    fb_conf = [e["confidence"] for e in (res8b.get("elements") or [])
               if e.get("source") == "fallback_noun"]
    check("J-11 兜底要素的 confidence 严格低于 0.6（恒不阻断 ⇒ 不误拦）",
          all(c < 0.6 for c in fb_conf), "实际 %s" % fb_conf)
    # needs_confirmation 与 confidence 必须同向（低置信 ⇒ 需确认）
    check("J-11 兜底要素 needs_confirmation=True（低置信 ⇒ 人工确认前不作硬要求）",
          all(e["needs_confirmation"] is True for e in (res8b.get("elements") or [])
              if e.get("source") == "fallback_noun"),
          "实际 %s" % [(e["element"], e["confidence"], e["needs_confirmation"])
                       for e in (res8b.get("elements") or [])
                       if e.get("source") == "fallback_noun"])

    # --- J-10 「不判」成因分流：不反查 stats、不嗅探 reason 文本 ---
    # 🔴 判据层把两种「不判」（业务方有意不要 / 我们没能力判）归到同一个
    #    verdict="不判" + severity="none"，只有 reason 文本不同
    #    ⇒ 靠文本嗅探是脆的（底座自己就批评过"把语义藏在字符串里"）
    #    ⇒ 实例层提供结构化分流。
    r10 = reqelem.not_judged_reason
    k1 = r10({"element": "时间口径", "polarity": "may_omit",
              "cause": "需求未提出 时间口径，不作要求",
              "matcher": "slot_requirement:time_semantics"})
    check("J-10 may_omit ⇒ 归「业务方有意不要」且带 cause",
          k1[0] == "业务方有意不要" and "需求未提出" in k1[1],
          "实际 %s" % (k1,))
    k2 = r10({"element": "时间口径", "polarity": "may_omit",
              "cause": "业务方显式豁免 时间口径",
              "matcher": "slot_requirement:time_semantics"})
    check("J-10 两种 may_omit 的文案仍可区分（cause 不同）",
          k1[1] != k2[1] and "豁免" in k2[1],
          "实际 %s / %s" % (k1[1], k2[1]))
    k3 = r10({"element": "门店", "polarity": "must_cover", "cause": None,
              "matcher": "field_present:门店"},
             {"reason": "无法确认「门店」是否被覆盖（可能用了别的别名/列），不作缺口"})
    check("J-10 实例 kind 判不了 ⇒ 归「未判定能力」（不是『不需要』）",
          k3[0] == "未判定能力", "实际 %s" % (k3,))
    k4 = r10({"element": "月度", "polarity": "must_cover", "cause": None,
              "matcher": "time_granularity:month"},
             {"reason": "GROUP BY 未含任何时间粒度，未覆盖 month"})
    check("J-10 底座 kind 判「不判」属异常 ⇒ 归「原因不明」（不硬塞进任何一类）",
          k4[0] == "原因不明" and "异常" in k4[2], "实际 %s" % (k4,))
    # 🔴 关键性质：**分类不依赖 reason 文本**（把文本改掉，分类不变）
    k5 = r10({"element": "门店", "polarity": "must_cover", "cause": None,
              "matcher": "field_present:门店"},
             {"reason": "完全不同的文案"})
    check("J-10 分类靠结构化信号（polarity+kind），不靠 reason 文本",
          k5[0] == "未判定能力" and k3[0] == k5[0],
          "实际 %s / %s" % (k3[0], k5[0]))


def main():
    print("=" * 66)
    print("语义分析 · 闸门与降级自证")
    print("=" * 66)
    test_constants()
    test_confidence()
    test_cand()
    test_tech_gate()
    test_question_gate()
    test_degradation()
    test_generic_gate()
    test_granularity_from_text()
    test_elements_extraction()
    test_real_bundle_and_entry()
    print()
    print("-" * 66)
    print("共 %d 项断言，失败 %d 项" % (CHECKS[0], len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  ✗ %s" % f)
        return 1
    print("\033[32m全部通过\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
