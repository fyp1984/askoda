#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M2 端到端验收脚本（需求受理 + 知识底座）

全部经 **MCP 工具面**调用，即验"Agent 能用"，而不只是"代码能跑"。

覆盖 M2 五条验收标准
--------------------
| 编号 | 验收标准 | 对应用例 |
|---|---|---|
| A | 提交含敏感字段的需求 → 落库 / 掩码 / 待分析 | V1 V2 V3 |
| B | PRD §9.2 字段与校验规则逐条覆盖（必填/敏感/重复） | V4 V5 V6 V7 V8 |
| C | 元数据字典可按表查中文名与口径 | V9 V10 |
| D | RAGFlow 与网关连通（检索接口 200） | V11 |
| E | 入库文档可召回并返回可追溯引用 | V12 |

用法：
    python tools/m2_verify.py [--base http://127.0.0.1:18080]
退出码：0 = 全部通过；1 = 有失败
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.request

PASS, FAIL = "✅", "❌"

# 手机号 / 身份证 / 地址 / 姓名 / 账号 —— 用来验证入口侧脱敏
SENSITIVE_DESC = (
    "客户张伟（手机 13800001234，身份证 110101199001011234）在北京市朝阳区建国路88号的门店"
    "连续三个月复购，客户号 A8899123456，需要看他的复购情况。"
)


class Mcp:
    def __init__(self, base):
        self.endpoint = base.rstrip("/") + "/mcp"
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.sid = None
        self._seq = 0

    def _rpc(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), headers=headers
        )
        resp = self.op.open(req, timeout=90)
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self.sid = sid
        body = resp.read().decode()
        if not body.strip():
            # notifications/* 属于通知，服务端按协议不回响应体
            return None
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(body)

    def init(self):
        self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "m2-verify", "version": "1.0"},
                },
            }
        )
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, tool, **kwargs):
        self._seq += 1
        res = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 100 + self._seq,
                "method": "tools/call",
                "params": {"name": tool, "arguments": kwargs},
            }
        )
        if "error" in res:
            raise RuntimeError(json.dumps(res["error"], ensure_ascii=False)[:300])
        result = res.get("result", {})
        txt = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(txt[:300])
        try:
            return json.loads(txt, strict=False)
        except Exception:
            return {"_raw": txt}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, vid, name, ok, detail):
        self.rows.append((vid, name, ok, detail))
        print("%s %-5s %-34s %s" % (PASS if ok else FAIL, vid, name, detail))

    @property
    def failed(self):
        return [r for r in self.rows if not r[2]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    args = ap.parse_args()

    rep = Report()
    mcp = Mcp(args.base).init()

    print("=" * 100)
    print("M2 端到端验收  目标：%s" % args.base)
    print("=" * 100)

    # ---------------------------------------------------------------- A 组
    print("\n【A】需求受理：落库 / 掩码 / 待分析")
    d1 = mcp.call(
        "demand_create",
        title="门店会员复购情况分析",
        business_context="区域运营需要评估各门店会员粘性，用于季度经营复盘。",
        description=SENSITIVE_DESC,
        expected_output="按门店维度的复购率明细表，含活跃会员数与复购会员数。",
        contact="张伟 13800001234",
        time_range="2026-07-01 至 2026-09-30",
    )
    ok1 = bool(d1.get("demand_id")) and d1.get("status") == "待分析"
    rep.add("V1", "需求落库且进入待分析", ok1,
            "demand_id=%s status=%s" % (d1.get("demand_id"), d1.get("status")))
    did = d1.get("demand_id")

    fields = ["title", "business_context", "description", "expected_output", "contact"]
    masked_text = " ".join(str(d1.get(f, "")) for f in fields)
    leaked = [s for s in ("13800001234", "110101199001011234", "A8899123456") if s in masked_text]
    rep.add("V2", "敏感信息掩码（手机/身份证/账号）", not leaked,
            "掩码字段=%s，明文泄漏=%s" % (d1.get("masked_fields"), leaked or "无"))

    rep.add("V3", "掩码覆盖率（命中类型统计）",
            bool((d1.get("sensitivity") or {}).get("types")),
            "命中=%s" % json.dumps(d1.get("sensitivity"), ensure_ascii=False)[:120])

    # 原文受控访问
    got_reveal = mcp.call("demand_get", demand_id=did, reveal=True)
    ev_types = [e["event_type"] for e in (got_reveal.get("events") or [])]
    rep.add("V3b", "原文访问受控且留痕",
            "reveal_original" in ev_types and "13800001234" in str(got_reveal.get("contact")),
            "事件链=%s" % ",".join(ev_types))

    # 默认读取应为掩码版
    got = mcp.call("demand_get", demand_id=did)
    rep.add("V3c", "默认读取返回掩码版",
            "13800001234" not in str(got.get("description")) and got.get("redacted") is True,
            "redacted=%s" % got.get("redacted"))

    # ---------------------------------------------------------------- B 组
    print("\n【B】PRD §9.2 提交字段与校验规则逐条覆盖")

    r1 = mcp.call(
        "demand_create",
        title="缺核心三项的测试单",
        business_context="",
        description="",
        expected_output="",
        contact="张三",
    )
    rep.add("V4", "校验①：核心三项缺失→拒绝", r1.get("rejected") is True,
            str(r1.get("reason"))[:90])

    r2 = mcp.call(
        "demand_create",
        title="缺联系人字段的测试单",
        business_context="验证非核心必填字段缺失时的处理。",
        description="这是一条用于校验必填字段提示的需求描述，内容完整。",
        expected_output="返回校验明细。",
        contact="",
    )
    checks2 = {c["name"]: c for c in ((r2.get("validation") or {}).get("checks") or [])}
    rep.add("V5", "校验①：非核心必填缺失→提示不拒绝",
            bool(r2.get("demand_id")) and not checks2.get("core_fields", {}).get("passed", True),
            "core_fields.passed=%s detail=%s"
            % (checks2.get("core_fields", {}).get("passed"),
               (checks2.get("core_fields", {}).get("detail") or "")[:60]))

    r3 = mcp.call(
        "demand_create",
        title="门店会员复购情况分析",  # 与 V1 高度相似
        business_context="区域运营需要评估各门店会员粘性，用于季度经营复盘。",
        description=SENSITIVE_DESC,
        expected_output="按门店维度的复购率明细表，含活跃会员数与复购会员数。",
        contact="李四",
        time_range="2026-07-01 至 2026-09-30",
    )
    sim = r3.get("similar_requests") or []
    rep.add("V6", "校验③：相似历史需求提示复用",
            bool(sim) and any(s["demand_id"] == did for s in sim),
            "相似单=%s" % [(s["demand_id"], s["similarity"]) for s in sim][:2])

    r4 = mcp.call(
        "demand_create",
        title="近30天门店日均销售额",
        business_context="运营周会要看近期门店表现。",
        description="希望统计近30天各门店日均销售额，并给出环比变化。",
        expected_output="门店×日均销售额表。",
        contact="王五",
    )
    checks4 = {c["name"]: c for c in ((r4.get("validation") or {}).get("checks") or [])}
    ts = checks4.get("time_scope", {})
    rep.add("V7", "校验④：时间范围与强口径矛盾提示",
            bool(r4.get("demand_id")) and ts.get("passed") is False,
            (ts.get("detail") or "")[:80])

    r5 = mcp.call(
        "demand_create",
        title="附件格式识别与高风险提示校验",
        business_context="验证附件格式校验分支。",
        description="附带一份无法识别的附件，检查系统提示。",
        expected_output="返回附件校验明细。",
        contact="赵六",
        attachments=[{"filename": "口径说明.xyz", "object_key": "x/y.xyz", "size": 10}],
    )
    checks5 = {c["name"]: c for c in ((r5.get("validation") or {}).get("checks") or [])}
    att = checks5.get("attachment", {})
    rep.add("V8", "校验②：附件不可识别→提示补文字",
            bool(r5.get("demand_id")) and att.get("passed") is False,
            (att.get("detail") or "")[:80])

    # ---------------------------------------------------------------- 附件
    print("\n【附件】MinIO 接入与高风险提示")
    csv_body = "member_id,member_name,member_phone\n101,张伟,13800001234\n102,王芳,13800005678\n"
    up = mcp.call(
        "attachment_put",
        filename="会员名单样例.csv",
        content_base64=base64.b64encode(csv_body.encode()).decode(),
        demand_id=did,
    )
    meta = up.get("attachment") or {}
    rep.add("V8b", "附件上传 + 高风险提示",
            up.get("ok") and bool((meta.get("risk") or {}).get("types")),
            "risk=%s sha=%s" % (json.dumps((meta.get("risk") or {}).get("types"), ensure_ascii=False),
                                meta.get("sha256")))

    lst = mcp.call("attachment_list", demand_id=did)
    rep.add("V8c", "附件随单可列出", bool(lst.get("objects")),
            "对象键=%s" % [o["object_key"] for o in (lst.get("objects") or [])][:2])

    if lst.get("objects"):
        url = mcp.call("attachment_url", object_key=lst["objects"][0]["object_key"])
        rep.add("V8d", "附件临时下载链接可生成",
                url.get("ok") and "http" in str(url.get("url")),
                str(url.get("url"))[:80])

    # ---------------------------------------------------------------- C 组
    print("\n【C】元数据字典：按表查中文名与口径")
    md = mcp.call("metadata_lookup", table="dwd_order_di", dataset="B")
    t = (md.get("tables") or [{}])[0]
    has_caliber = any("口径" in (c.get("description") or "") for c in (t.get("columns") or []))
    rep.add("V9", "按表查中文名 / 颗粒度 / 口径",
            bool(t.get("table_name")) and bool(t.get("grain")) and has_caliber,
            "%s | %s | 字段 %d" % (t.get("table_name"), (t.get("grain") or "")[:34], t.get("column_count", 0)))

    mdh = mcp.call("metadata_lookup", table="dim_member", dataset="B", include_hidden=True)
    th = (mdh.get("tables") or [{}])[0]
    hidden = [c["column_name"] for c in (th.get("columns") or []) if not c.get("ai_visible")]
    rep.add("V10", "未建模敏感列被标注为 AI 不可见",
            set(hidden) >= {"member_phone", "id_card"},
            "隐藏列=%s" % hidden)

    gl = mcp.call("metadata_glossary")
    rep.add("V10b", "业务口径词表可查", bool(gl.get("terms")),
            "条目数=%d 例：%s" % (len(gl.get("terms") or []),
                                 (gl.get("terms") or [{}])[0].get("term")))

    # ---------------------------------------------------------------- D/E 组
    print("\n【D/E】知识底座：连通性与引用式召回")
    kh = mcp.call("knowledge_health")
    rep.add("V11", "RAGFlow 与网关连通",
            kh.get("ok") is True and bool(kh.get("dataset")),
            "数据集=%s 文档=%s 嵌入=%s"
            % ((kh.get("dataset") or {}).get("name"),
               (kh.get("dataset") or {}).get("documents"),
               (kh.get("dataset") or {}).get("embedding_model")))

    ks = mcp.call("knowledge_search", question="什么是坪效，怎么算？", top_k=3)
    cits = ks.get("citations") or []
    # 「可追溯」= 每条引用都能回到"哪篇文档、哪一段"：
    #   文档名 + chunk_id 是**必须**的（chunk_id 恒为有效定位符，可据此回查全文）；
    #   坐标 positions 只要求"至少一条真有"——RAGFlow 对表格型 chunk 不返回坐标
    #   （实测见 knowledge._locator 的说明），要求条条都有坐标是不成立的断言。
    #   另校验 locator 不得出现 `position=[]` 这类会误导下游的空坐标串。
    traceable = (
        bool(cits)
        and all(c.get("document_name") and c.get("chunk_id") for c in cits)
        and any(c.get("position_available") for c in cits)
        and all("position=[]" not in (c.get("locator") or "") for c in cits)
    )
    rep.add("V12", "可召回且引用可追溯（文档名+片段定位）", traceable,
            "命中 %d 条 | 带坐标 %d 条 | %s" % (
                len(cits),
                sum(1 for c in cits if c.get("position_available")),
                " ; ".join("%s sim=%.3f" % (c["document_name"], c["similarity"])
                           for c in cits[:2])))

    # 引用能回溯到具体片段
    if cits:
        rep.add("V12b", "引用含片段定位与证据块",
                bool(cits[0].get("locator")) and bool(ks.get("answer_context")),
                cits[0]["locator"][:70])

    # 口语化问句的回退召回：实测「坪效怎么算」用原始问句是 0 命中，
    # 必须靠"去疑问词后重试"救回（见 gateway/knowledge.extract_keywords）。
    # 业务人员最常问的就是这种句式，所以单独立一条用例守住它。
    ks2 = mcp.call("knowledge_search", question="坪效怎么算", top_k=3)
    cits2 = ks2.get("citations") or []
    rep.add("V12c", "口语化问句可召回（关键词回退）",
            bool(cits2) and bool(ks2.get("fallback_query")),
            "问句=坪效怎么算 → 回退问句=%s 命中=%d 条"
            % (ks2.get("fallback_query"), len(cits2)))

    # ---------------------------------------------------------------- 汇总
    print("\n【汇总】")
    s = mcp.call("demand_summarize")
    rep.add("V13", "需求看板汇总",
            s.get("total", 0) >= 4 and bool(s.get("by_status")),
            "总数=%s 待分析=%s 状态分布=%s"
            % (s.get("total"), s.get("pending_analysis"),
               json.dumps(s.get("by_status"), ensure_ascii=False)))
    rep.add("V14", "敏感命中汇总可统计", bool(s.get("sensitive_types")),
            json.dumps(s.get("sensitive_types"), ensure_ascii=False))

    print("\n" + "=" * 100)
    if rep.failed:
        print("%s 共 %d 项未通过：" % (FAIL, len(rep.failed)))
        for vid, name, _ok, detail in rep.failed:
            print("   - %s %s：%s" % (vid, name, detail))
        return 1
    print("%s M2 全部 %d 项验收用例通过" % (PASS, len(rep.rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
