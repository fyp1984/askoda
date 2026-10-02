#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据需求智能分析助手 · 实测版本地服务

职责：
  1. 复用 Wren MCP（streamable-http）客户端协议，会话复用 + 失败重握手。
  2. 加载本地真实 MDL（语义层基准）。
  3. 基于 MDL 的确定性 NL→SQL 规划器（封闭世界：只引用 MDL 可见对象）。
  4. 只读 SQL 执行：所有 SQL 经 Wren query 真实跑本地测试库。
  5. 提供 /api/* REST 并托管实测版 HTML demo。

启动：python3 demo-server.py   （默认 127.0.0.1:8080）
依赖：仅 Python 标准库 + 本地已运行的 Wren MCP（127.0.0.1:9000）。
"""
import json, urllib.request, urllib.error, threading, os, re, html
from http.server import HTTPServer, BaseHTTPRequestHandler

WREN_URL = "http://127.0.0.1:9000/mcp"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # 仓库根目录（demo/ 的上一级）
MDL_PATH = os.path.join(ROOT, "wren-docker", "workspace", "mdl.json")
HTML_PATH = os.path.join(HERE, "数据需求智能分析助手-语义层实测.html")

# 演示用的示例问题（与规划器意图一一对应，前端按钮据此渲染）
EXAMPLES = [
    {"tag": "正向", "label": "各品类销售额", "nl": "各品类的销售额分别是多少，按销售额降序"},
    {"tag": "正向", "label": "已完成订单金额", "nl": "已完成订单的总金额是多少"},
    {"tag": "正向", "label": "退款明细", "nl": "列出所有退款订单的明细（订单号、退款金额、原因）"},
    {"tag": "正向", "label": "客户分群GMV", "nl": "按客户类型（消费者/公司）统计GMV和下单量"},
    {"tag": "正向", "label": "各大类库存", "nl": "各大类的总库存数量和SKU数量"},
    {"tag": "正向", "label": "退款率", "nl": "整体退款率是多少"},
    {"tag": "正向", "label": "订单状态分布", "nl": "订单按状态分布的数量（含状态含义）"},
    {"tag": "正向", "label": "客单价", "nl": "客单价是多少"},
    {"tag": "正向", "label": "Top商品", "nl": "卖得最好的商品TOP5"},
    {"tag": "正向", "label": "连带率", "nl": "整体连带率是多少（件/单）"},
    {"tag": "正向", "label": "客户消费排行", "nl": "消费金额最高的客户TOP5"},
    {"tag": "正向", "label": "退款原因分布", "nl": "退款原因分布"},
    {"tag": "正向", "label": "订单趋势", "nl": "按日期看订单数和销售金额趋势"},
    {"tag": "拒绝", "label": "客户手机号(未建模)", "nl": "查一下所有客户的手机号"},
    {"tag": "拒绝", "label": "情绪指数(未建模)", "nl": "分析客户情绪指数TOP10"},
]

# ---------------------------------------------------------------------------
# Wren MCP 客户端（streamable-http）
# ---------------------------------------------------------------------------
class WrenClient:
    def __init__(self):
        self._op = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理，直连回环
        self._sid = None
        self._lock = threading.Lock()

    def _raw(self, payload, sid=None):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        if sid:
            h["Mcp-Session-Id"] = sid
        req = urllib.request.Request(WREN_URL, data=json.dumps(payload).encode(), headers=h)
        r = self._op.open(req, timeout=60)
        sid2 = r.headers.get("Mcp-Session-Id")
        body = r.read().decode()
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), sid2
        return None, sid2

    def _handshake(self):
        res, sid = self._raw({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "demo-server", "version": "1.0"}}})
        self._sid = sid
        if sid:
            try:
                self._raw({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except Exception:
                pass

    def call(self, tool, args=None, _retry=True):
        with self._lock:
            if self._sid is None:
                self._handshake()
            try:
                res, sid = self._raw({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                      "params": {"name": tool, "arguments": args or {}}}, self._sid)
                if sid:
                    self._sid = sid
                return res
            except urllib.error.HTTPError as e:
                if _retry and ("session" in e.read().decode().lower()):
                    self._sid = None
                    return self.call(tool, args, _retry=False)
                raise


_wren = WrenClient()


def wren_text(tool, args=None):
    """调用 Wren 工具，把 content 文本拼起来返回。"""
    res = _wren.call(tool, args)
    if not res or "error" in res:
        raise RuntimeError("Wren 调用失败：" + json.dumps(res.get("error", {}), ensure_ascii=False)[:200])
    return "".join(c.get("text", "") for c in res.get("result", {}).get("content", []))


def wren_query(sql):
    """真实执行：返回结构化结果 {columns, data, dtypes}。"""
    res = _wren.call("query", {"sql": sql})
    if not res or "error" in res:
        raise RuntimeError("Wren query 失败：" + json.dumps(res.get("error", {}), ensure_ascii=False)[:200])
    txt = "".join(c.get("text", "") for c in res.get("result", {}).get("content", []))
    try:
        return json.loads(txt)
    except Exception:
        # 容错：非 JSON 时包成单列文本
        return {"columns": ["result"], "data": [[txt]], "dtypes": {"result": "string"}}


def wren_dryrun(sql):
    """语义层门禁：返回 {ok, message}。"""
    res = _wren.call("dry_run", {"sql": sql})
    if not res or "error" in res:
        return {"ok": False, "message": json.dumps(res.get("error", {}), ensure_ascii=False)[:200]}
    result = res.get("result", {})
    is_error = result.get("isError", False)
    txt = "".join(c.get("text", "") for c in result.get("content", []))
    if is_error or "error" in txt.lower():
        return {"ok": False, "message": txt or "语义层校验未通过"}
    return {"ok": True, "message": "语义层预演通过（引用对象均 ∈ MDL 可见闭集）"}


def wren_manifest():
    return json.loads(wren_text("get_manifest") or "{}")


def wren_relationships():
    return json.loads(wren_text("get_relationships") or "[]")


def wren_health():
    return wren_text("health_check")


# ---------------------------------------------------------------------------
# 语义层（MDL）加载 —— 真实字典
# ---------------------------------------------------------------------------
with open(MDL_PATH, encoding="utf-8") as f:
    MDL = json.load(f)

# 把 MDL 摊平成便于规划器检索的结构
MODELS = {m["name"]: m for m in MDL.get("models", [])}
MODEL_COLUMNS = {m["name"]: {c["name"] for c in m.get("columns", [])} for m in MDL.get("models", [])}
RELATIONSHIPS = MDL.get("relationships", [])


def enum_note(model, column):
    """返回字段的口径/枚举注释（来自 MDL column description）。"""
    m = MODELS.get(model)
    if not m:
        return ""
    for c in m.get("columns", []):
        if c["name"] == column:
            return c.get("description", "")
    return ""


# ---------------------------------------------------------------------------
# 基于 MDL 的确定性 NL→SQL 规划器（封闭世界：只引用已建模对象）
# ---------------------------------------------------------------------------
def _objects_for(models, columns=None, enums=None, rules=None):
    objs = []
    for mn in models:
        m = MODELS.get(mn)
        objs.append({"type": "model", "name": mn,
                     "detail": (m or {}).get("tableReference", {}).get("table", "") if m else ""})
    for col in (columns or []):
        objs.append({"type": "column", "name": col, "detail": ""})
    for en in (enums or []):
        objs.append({"type": "enum", "name": en["name"], "detail": en["value"]})
    for ru in (rules or []):
        objs.append({"type": "rule", "name": ru, "detail": ""})
    return objs


def plan(nl):
    """输入自然语言，输出 {intent, sql, objects, blocked, reason, steps}。"""
    t = nl.strip()
    # 用户直接写 SQL 的情况：透传给执行（语义层仍会校验）
    if re.match(r"^\s*(select|with)\b", t, re.I):
        return {"intent": "用户直接编写 SQL（经语义层校验执行）", "sql": t,
                "objects": [], "blocked": False, "reason": "",
                "steps": ["识别为原始 SQL，跳过意图规划", "经 MDL dry-run 门禁校验", "Wren query 真实执行"]}

    tl = t.lower()
    # —— 拒绝类：未建模对象 ——
    if any(k in t for k in ["手机号", "电话", "手机", "邮箱", "邮件", "身份证", "证件号"]):
        return refuse("客户联系方式", "手机号/邮箱/身份证等敏感字段未纳入语义层建模，对 AI 不可见，禁止生成与明文输出")
    if any(k in t for k in ["情绪指数", "满意度", "评分", "好评", "NPS", "情绪"]):
        return refuse("情绪指数", "「情绪指数」在语义层（MDL）中无任何已建模口径，禁止猜测生成")
    if any(k in t for k in ["利润", "毛利率", "成本"]):
        return refuse("利润/成本", "利润、毛利率、成本等口径尚未在语义层建模，禁止猜测生成")

    # —— 正向意图 ——
    # 1. 各品类销售额
    if any(k in t for k in ["品类", "类目", "大类", "类"]) and any(k in t for k in ["销售", "销售额", "营收", "卖"]):
        sql = ("SELECT p.category_name AS 品类, SUM(oi.subtotal) AS 销售额 "
               "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
               "GROUP BY p.category_name ORDER BY 销售额 DESC")
        return ok("各品类销售额（事实表 order_items × 维度退化列 products.category_name）", sql,
                  _objects_for(["order_items", "products"], columns=["order_items.subtotal", "products.category_name"],
                               rules=["维度退化：products.category_name 免 JOIN categories"]))
    # 2. 已完成订单金额
    if ("已完成" in t and any(k in t for k in ["订单", "金额", "总额", "成交"])) or ("完成" in t and "金额" in t):
        sql = ("SELECT COUNT(*) AS 已完成订单数, SUM(amount) AS 总金额 "
               "FROM orders WHERE status = 3")
        return ok("已完成订单金额（orders.status 枚举：3=已完成）", sql,
                  _objects_for(["orders"], columns=["orders.status", "orders.amount"],
                               enums=[{"name": "orders.status=3", "value": "已完成"}],
                               rules=["枚举口径：status=3 即已完成（MDL 字典定义）"]))
    # 3. 退款率（须先于「退款明细」判定，避免「退款率」被「退款」误命中）
    if "退款率" in t or ("退款" in t and "率" in t):
        sql = ("SELECT ROUND(100.0 * COUNT(r.refund_id) / COUNT(o.order_id), 1) AS 退款率百分比 "
               "FROM orders o LEFT JOIN refunds r ON r.order_id = o.order_id")
        return ok("退款率（退款订单数 / 总订单数）", sql,
                  _objects_for(["orders", "refunds"], columns=["orders.order_id", "refunds.refund_id"]))
    # 退款原因分布（须先于「退款明细」判定，避免被「退款」误命中）
    if "退款原因" in t:
        sql = ("SELECT reason AS 退款原因, COUNT(*) AS 笔数, SUM(refund_amount) AS 退款金额 "
               "FROM refunds GROUP BY reason ORDER BY 笔数 DESC")
        return ok("退款原因分布（refunds 按原因聚合）", sql,
                  _objects_for(["refunds"], columns=["refunds.reason", "refunds.refund_amount"]))
    # 4. 退款明细
    if any(k in t for k in ["退款", "退单"]):
        sql = ("SELECT r.refund_id AS 退款ID, o.order_id AS 订单号, o.status AS 订单状态码, "
               "o.order_date AS 下单日期, r.refund_amount AS 退款金额, r.reason AS 原因 "
               "FROM refunds r JOIN orders o ON r.order_id = o.order_id")
        return ok("退款明细（refunds 事实表 × orders，含订单状态码：1已下单/2已发货/3已完成/4已退款）", sql,
                  _objects_for(["orders", "refunds"], columns=["refunds.refund_amount", "refunds.reason", "orders.status"],
                               enums=[{"name": "orders.status", "value": "1已下单/2已发货/3已完成/4已退款"}],
                               rules=["枚举口径：status 值由 MDL 字典定义；refunds 为退款事实表，可顺带暴露订单状态与退款不匹配的数据质量问题"]))
    # 客户消费排行（须先于「客户分群」判定，避免「按客户」类词被误命中）
    if "客户" in t and any(k in tl for k in ["排行", "top", "消费金额"]):
        sql = ("SELECT c.first_name || ' ' || c.last_name AS 客户, c.segment AS 客户类型, "
               "SUM(oi.subtotal) AS 消费额 "
               "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
               "JOIN order_items oi ON oi.order_id = o.order_id "
               "GROUP BY c.first_name, c.last_name, c.segment ORDER BY 消费额 DESC")
        return ok("客户消费排行（customers × orders × order_items，Top 5）", sql,
                  _objects_for(["customers", "orders", "order_items"],
                               columns=["customers.first_name", "customers.last_name",
                                        "customers.segment", "order_items.subtotal"]))
    # 5. 客户分群 GMV
    if any(k in t for k in ["客户类型", "分群", "消费者", "公司", "客户群", "按客户"]) and any(k in t for k in ["gmv", "销售额", "成交", "下单量", "销量"]):
        sql = ("SELECT c.segment AS 客户类型, COUNT(DISTINCT o.order_id) AS 下单量, SUM(oi.subtotal) AS GMV "
               "FROM customers c JOIN orders o ON o.customer_id = c.customer_id "
               "JOIN order_items oi ON oi.order_id = o.order_id GROUP BY c.segment")
        return ok("客户分群 GMV（customers × orders × order_items）", sql,
                  _objects_for(["customers", "orders", "order_items"],
                               columns=["customers.segment", "orders.order_id", "order_items.subtotal"]))
    # 5. 各大类库存
    if any(k in t for k in ["库存", "存货", "备货"]) and any(k in t for k in ["品类", "大类", "类目", "类", "sku", "商品"]):
        sql = ("SELECT p.category_name AS 大类, SUM(p.stock) AS 总库存, COUNT(*) AS SKU数量 "
               "FROM products p GROUP BY p.category_name ORDER BY 总库存 DESC")
        return ok("各大类库存（products 维度退化列 category_name）", sql,
                  _objects_for(["products"], columns=["products.stock", "products.category_name"]))
    # 6. 客单价 / 平均订单金额
    if any(k in t for k in ["客单价", "平均订单", "平均金额", "均价"]):
        sql = ("SELECT COUNT(*) AS 订单数, ROUND(AVG(amount), 2) AS 客单价 "
               "FROM orders WHERE status = 3")
        return ok("客单价（已完成订单平均金额）", sql,
                  _objects_for(["orders"], columns=["orders.amount", "orders.status"],
                               enums=[{"name": "orders.status=3", "value": "已完成"}]))
    # 8. 订单状态分布
    if any(k in t for k in ["状态分布", "状态", "各状态", "订单状态"]):
        sql = ("SELECT status AS 状态码, COUNT(*) AS 订单数 FROM orders GROUP BY status ORDER BY status")
        return ok("订单状态分布（枚举：1已下单/2已发货/3已完成/4已退款）", sql,
                  _objects_for(["orders"], columns=["orders.status"],
                               enums=[{"name": "orders.status", "value": "1已下单/2已发货/3已完成/4已退款"}]))
    # 连带率（件 / 单）
    if "连带" in t:
        sql = ("SELECT ROUND(1.0 * SUM(quantity) / COUNT(DISTINCT order_id), 2) AS 连带率件每单 "
               "FROM order_items")
        return ok("连带率（销售件数 / 下单订单数）", sql,
                  _objects_for(["order_items"], columns=["order_items.quantity", "order_items.order_id"]))
    # 订单趋势（按日）
    if any(k in t for k in ["趋势", "按日", "每天", "每日"]):
        sql = ("SELECT order_date AS 日期, COUNT(*) AS 订单数, SUM(amount) AS 销售金额 "
               "FROM orders GROUP BY order_date ORDER BY order_date")
        return ok("订单趋势（按下单日期聚合）", sql,
                  _objects_for(["orders"], columns=["orders.order_date", "orders.amount"]))
    # 9. Top 商品 / 最畅销
    if any(k in t for k in ["畅销", "热销", "最卖", "top", "销量", "卖得", "商品排行"]):
        sql = ("SELECT p.product_name AS 商品, SUM(oi.quantity) AS 销量, SUM(oi.subtotal) AS 销售额 "
               "FROM order_items oi JOIN products p ON oi.product_id = p.product_id "
               "GROUP BY p.product_name ORDER BY 销售额 DESC")
        return ok("商品销量与销售额排行（order_items × products）", sql,
                  _objects_for(["order_items", "products"], columns=["order_items.quantity", "order_items.subtotal", "products.product_name"]))

    # —— 未命中任何已建模意图 ——
    return refuse("未命中已建模式", "未能在语义层（MDL）匹配到对应已建模对象，禁止猜测生成；请先在语义层补充建模，或参考下方已支持的示例")


def ok(intent, sql, objects):
    return {"intent": intent, "sql": sql, "objects": objects, "blocked": False, "reason": "",
            "steps": ["业务语义分析：命中意图「%s」" % intent,
                      "MDL 语义对齐：引用 %d 个语义层对象" % len(objects),
                      "用户确认语义理解无误",
                      "经 MDL dry-run 门禁校验",
                      "Wren query 真实执行（落本地测试库）"]}


def refuse(concept, reason):
    return {"intent": "硬门禁拒绝：「%s」" % concept, "sql": "", "objects": [],
            "blocked": True, "reason": reason,
            "steps": ["业务语义分析：识别到未建模概念「%s」" % concept,
                      "硬门禁：未在语义层（MDL）命中 → 禁止生成 SQL",
                      "治理指引：先于「语义层 · MDL 字典」补充建模 / 准入口径后再运行"]}


# ---------------------------------------------------------------------------
# 只读执行保护
# ---------------------------------------------------------------------------
def is_readonly(sql):
    s = re.sub(r"--.*", "", sql, flags=re.I)          # 去行注释
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)        # 去块注释
    s = s.strip().lower()
    if not (s.startswith("select") or s.startswith("with")):
        return False, "只允许 SELECT / WITH（只读）语句"
    banned = ["insert", "update", "delete", "drop", "alter", "create", "truncate",
             "grant", "revoke", "merge", "replace", "call", "exec", ";"]
    for b in banned:
        if re.search(r"\b%s\b" % b, s):
            return False, "检测到非只读关键字「%s」，已拦截" % b
    return True, ""


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            try:
                with open(HTML_PATH, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "HTML 未找到，请先生成 数据需求智能分析助手-语义层实测.html")
            return
        if path.endswith("CheersAI-Logo.png"):
            try:
                with open(os.path.join(HERE, "CheersAI-Logo.png"), "rb") as f:
                    self._send(200, f.read(), "image/png")
            except FileNotFoundError:
                self._send(404, "logo 未找到")
            return
        try:
            if path == "/api/health":
                return self._json({"ok": True, "wren": wren_health()})
            if path == "/api/manifest":
                return self._json({"models": MDL.get("models", []),
                                   "relationships": RELATIONSHIPS,
                                   "engine": "Wren Engine v1.27.0 · Apache DataFusion 底座 · Apache-2.0"})
            if path == "/api/relationships":
                return self._json({"relationships": RELATIONSHIPS})
            if path == "/api/examples":
                return self._json({"examples": EXAMPLES})
            if path == "/api/schema":
                # 用 Wren 真实查询取各表行数 + 样本，展示真实数据规模
                schema = []
                for name, m in MODELS.items():
                    tbl = m.get("tableReference", {}).get("table", name)
                    row = {"model": name, "table": tbl,
                           "columns": [{"name": c["name"], "type": c.get("type", ""),
                                        "desc": c.get("description", "")}
                                       for c in m.get("columns", [])]}
                    try:
                        cnt = wren_query("SELECT COUNT(*) AS c FROM %s" % name)
                        row["rows"] = cnt["data"][0][0] if cnt.get("data") else None
                    except Exception:
                        row["rows"] = None
                    schema.append(row)
                return self._json({"schema": schema})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:300]})
        self._send(404, "not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}
        path = self.path.split("?")[0]
        try:
            if path == "/api/ask":
                nl = payload.get("nl", "")
                planned = plan(nl)
                if planned["blocked"]:
                    return self._json({"blocked": True, **planned})
                # 真实跑 dry-run + query，把真结果带回来
                dry = wren_dryrun(planned["sql"])
                result = None
                exec_error = None
                if dry["ok"]:
                    try:
                        result = wren_query(planned["sql"])
                    except Exception as e:
                        exec_error = str(e)[:300]
                return self._json({"blocked": False, **planned,
                                   "dry_run": dry, "result": result, "exec_error": exec_error})
            if path == "/api/dryrun":
                sql = payload.get("sql", "")
                return self._json(wren_dryrun(sql))
            if path == "/api/query":
                sql = payload.get("sql", "")
                ok_read, msg = is_readonly(sql)
                if not ok_read:
                    return self._json({"ok": False, "error": msg})
                try:
                    return self._json({"ok": True, **wren_query(sql)})
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:300]})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)[:300]})
        self._send(404, "not found")


def main():
    future_port = 8080
    server = HTTPServer(("127.0.0.1", future_port), Handler)
    print("数据需求智能分析助手 · 实测版服务已启动： http://127.0.0.1:%d" % future_port)
    print("依赖：本地 Wren MCP (127.0.0.1:9000) + wren-postgres (test 库 6 表)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
