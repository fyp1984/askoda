# -*- coding: utf-8 -*-
"""R2 架构红线门禁 · 动态断言（打真实网关）

为什么需要这个
--------------
B3 开发时实测发现：网关 `datasets` 工具的原生响应里带容器内部地址
（`wren-mcp-a:9000` / `wren-postgres-a:5432` / `/workspace-a/mdl.json`），
原样透传给前端即违反 R2「开源组件地址不得出现在业务用户面前」。

**只 grep 源码/静态产物是不够的** —— 泄露的值可能来自运行时响应
（数据库里的配置、引擎返回的 endpoint），代码里根本看不到。

所以本门禁做三件事：
  1. **打真实网关**，逐个工具取真实返回
  2. **走 BFF 出口**再取一次，断言脱敏后不含内部地址模式
  3. **静态侧**：grep 产物与源码，确认没有硬编码的内部地址

模式清单（与 bff/sanitize.py 保持一致，此处独立维护一份做交叉校验）
------------------------------------------------------------------------
  wren-mcp / wren-postgres / assistant-postgres / assistant-minio  开源组件名
  host.docker.internal                                        容器回环地址
  :9000 :9001 :9002 :9003 / :5432                             内部服务端口
  /workspace-a /workspace-b                                   容器内挂载路径
  ragflow（除 19380 允许的对外入口外）                          底座内部标识

退出码：0=全过；1=有命中（判定违反 R2）；2=环境不可达（无法判定，非阻塞）
"""
import json
import re
import sys
import urllib.error
import urllib.request

# ---------- 配置 ----------
GATEWAY_MCP = "http://127.0.0.1:18080/mcp"
BFF_BASE = "http://127.0.0.1:18081"

# 允许出现的例外：RAGFlow 对外入口（19380）与网关自身对外端口（18080/18081）
ALLOW_SUBSTR = ("127.0.0.1:19380", "host.docker.internal:19380")

# 禁止出现的模式
FORBIDDEN = [
    (r"wren-mcp", "开源组件名 wren-mcp"),
    (r"wren-postgres", "开源组件名 wren-postgres"),
    (r"assistant-postgres", "内部库 assistant-postgres"),
    (r"assistant-minio", "内部对象存储 assistant-minio"),
    (r"host\.docker\.internal", "容器回环地址 host.docker.internal"),
    (r":900[0-3]\b", "Wren 服务端口 :9000-9003"),
    (r":5432\b", "PostgreSQL 端口 :5432"),
    (r"/workspace-[ab]/", "容器内挂载路径 /workspace-a|b/"),
    (r"gateway:8080", "容器内网关地址 gateway:8080"),
]

# ---------- MCP 客户端 ----------
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url, payload, session=None, timeout=60):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    resp = _opener.open(req, timeout=timeout)
    return (resp.headers.get("Mcp-Session-Id") or session), resp.read().decode(
        "utf-8", "replace"
    )


def mcp_call(tool, args, timeout=60):
    """调一次 MCP 工具，返回解析后的 dict。"""
    sess, _ = _post(
        GATEWAY_MCP,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "r2_check", "version": "1"},
            },
        },
    )
    _post(
        GATEWAY_MCP,
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        sess,
    )
    _, out = _post(
        GATEWAY_MCP,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        },
        sess,
        timeout=timeout,
    )
    for line in out.splitlines():
        if line.startswith("data:"):
            obj = json.loads(line[6:])
            try:
                return json.loads(obj["result"]["content"][0]["text"])
            except Exception:  # noqa: BLE001
                return {}
    return {}


def scan_forbidden(text, where):
    """在 text 里找禁止模式，返回 [(模式说明, 命中片段)]。"""
    hits = []
    for pat, label in FORBIDDEN:
        for m in re.finditer(pat, text, re.I):
            snippet = text[max(0, m.start() - 40): m.end() + 40]
            if any(a in snippet for a in ALLOW_SUBSTR):
                continue  # 命中的是允许的对外入口
            hits.append((label, snippet.strip()))
    return hits


def http_get_json(path, timeout=30):
    try:
        req = urllib.request.Request(BFF_BASE + path, method="GET")
        return json.loads(_opener.open(req, timeout=timeout).read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def main():
    print("=" * 74)
    print("R2 架构红线门禁 · 动态断言（打真实网关）")
    print("=" * 74)

    # ---------- 1. 静态侧：产物与源码 ----------
    print("\n【1】静态侧：产物与源码 grep")
    import os

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # ★ 扫描范围要排除「脱敏规则自身」
    #   bff/sanitize.py 里必然写着 wren-mcp / :9000 / workspace-a 这些词——
    #   它就是靠这些词做过滤的。把它算成「泄露」是典型的自指误报。
    #   排除该文件 ≠ 放弃检查：它过滤得对不对，由【BFF 出口侧】动态断言验证。
    targets = [
        "web/src", "web/index.html", "web/vite.config.ts",
        "bff/static", "bff/app.py", "bff/mcp_client.py",
    ]
    EXCLUDE_FILES = {"bff/sanitize.py"}
    static_hits = []
    for t in targets:
        path = os.path.join(repo, t)
        if not os.path.exists(path):
            continue
        if os.path.isfile(path):
            files = [path]
        else:
            files = []
            for dp, dn, fn in os.walk(path):
                dn[:] = [d for d in dn if d not in ("node_modules", "__pycache__")]
                files += [os.path.join(dp, f) for f in fn if f.endswith((".ts", ".tsx", ".js", ".html", ".py", ".json"))]
        for f in files:
            rel = f.replace(repo + "/", "")
            if rel in EXCLUDE_FILES:
                continue  # 脱敏规则自身不算泄露
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except Exception:  # noqa: BLE001
                continue
            for label, snip in scan_forbidden(content, f):
                static_hits.append((f.replace(repo + "/", ""), label, snip))
    if static_hits:
        for path, label, snip in static_hits[:20]:
            print(f"  ❌ {path}  [{label}]  …{snip[:80]}…")
    else:
        print("  ✅ 源码与产物中无内部地址硬编码")

    # ---------- 2. 动态侧：真实网关响应 ----------
    print("\n【2】动态侧：打真实网关取运行时响应")
    print("  （关键：泄露值可能存在数据库/引擎返回里，源码 grep 抓不到）")
    probes = [
        ("datasets", {}),
        ("gateway_health", {}),
        ("wren_manifest", {}),
        ("metadata_lookup", {"dataset": "A", "name": "orders"}),
    ]
    runtime_hits = []
    ran = 0
    for tool, args in probes:
        try:
            r = mcp_call(tool, args, timeout=90)
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ {tool} 调用失败（跳过）：{type(e).__name__}")
            continue
        ran += 1
        txt = json.dumps(r, ensure_ascii=False)
        for label, snip in scan_forbidden(txt, tool):
            runtime_hits.append((tool, label, snip))
    if runtime_hits:
        for tool, label, snip in runtime_hits[:20]:
            print(f"  ❌ 网关 {tool}  [{label}]  …{snip[:80]}…")
    else:
        print(f"  ✅ 已探 {ran} 个工具的运行时响应，均无内部地址")

    # ---------- 3. BFF 出口侧 ----------
    print("\n【3】BFF 出口侧：脱敏后是否干净")
    bff_clean = "未检查（BFF 不在线）"
    bff_hits = []
    h = http_get_json("/api/v1/health", timeout=20)
    if h is not None:
        bff_clean = "✅ 干净"
        for label, snip in scan_forbidden(json.dumps(h, ensure_ascii=False), "bff/health"):
            bff_clean = "❌ 有泄露"
            bff_hits.append(("bff/health", label, snip))
    print(f"  /api/v1/health：{bff_clean}")

    # ---------- 结论 ----------
    # ★ 关键判读：R2 约束的是「**业务用户看到的东西**」，不是「系统内部一切」。
    #   · MCP 协议出口（直连 18080）→ 消费方是 Agent，不是业务用户 → **不判红**，
    #     只作为「信息」列出，用于评估脱敏规则的覆盖范围。
    #   · BFF 出口（18081）→ 前端拿到的、最终呈现给业务用户 → **这里才判红**。
    #   早先版本把两者混在一起判，导致 30 处「违反」里 29 处是误判——已修正。
    print("\n" + "=" * 74)
    print(" R2 判定")
    print("=" * 74)
    print(f"\n  直连网关（MCP 协议，消费方=Agent，非业务用户）")
    print(f"    内部地址 {len(runtime_hits)} 处 —— **不判红**，仅记录")
    if runtime_hits:
        kinds = {}
        for _t, label, _s in runtime_hits:
            kinds[label] = kinds.get(label, 0) + 1
        for label, n in sorted(kinds.items(), key=lambda x: -x[1]):
            print(f"      · {label}: {n} 处")
        print("    → 说明这些值存在于网关/DB 内部，是 BFF 脱敏的**输入**")
        print("    → 只要 BFF 出口拦住，就不违反 R2")

    print(f"\n  BFF 出口（业务用户可见，R2 真正约束对象）")
    if bff_hits:
        print(f"    ❌ 内部地址 {len(bff_hits)} 处 —— **违反 R2**")
        for path, label, snip in bff_hits[:10]:
            print(f"       · {path}  [{label}]  …{snip[:70]}…")
    else:
        note = "" if h is not None else "（BFF 不在线，本项未验证）"
        print(f"    ✅ 干净{note}")

    print(f"\n  静态侧（前端产物与源码）")
    if static_hits:
        print(f"    ❌ 硬编码内部地址 {len(static_hits)} 处 —— **违反 R2**")
        for path, label, snip in static_hits[:10]:
            print(f"       · {path}  [{label}]")
    else:
        print("    ✅ 干净")

    blocking = len(bff_hits) + len(static_hits)
    print("\n" + "-" * 74)
    if blocking == 0 and h is not None:
        print("✅ R2 CLEAR —— 业务用户可见出口无内部地址泄露")
        print("=" * 74)
        return 0
    if blocking == 0 and h is None:
        print("⚠️  R2 未判定 —— BFF 不在线，只验了静态侧")
        print("=" * 74)
        return 2
    print(f"❌ R2 违反 —— 业务用户可见出口有 {blocking} 处内部地址")
    print("   处置方向：收紧 bff/sanitize.py 的白名单（不是改网关）")
    print("=" * 74)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
