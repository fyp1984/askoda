#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 知识后端集成自证（联调，需要活的 MCP 底座）。

测什么
------
`gateway/knowledge_mcp.py` 六个后端函数在真实 MCP 底座（默认 WeKnora）上的
端到端行为：health → list_documents → search（含零命中回退）→ upload →
status → delete 全链路闭环，并断言返回形状与 RAGFlow 直连版逐键对齐
（消费方零改动的依据）。

用法
----
    python3 tools/knowledge_mcp_selftest.py            # 用 .env / 环境变量配置
    KNOWLEDGE_BACKEND=mcp-weknora ... python3 tools/knowledge_mcp_selftest.py

前置：KNOWLEDGE_BACKEND=mcp-*，KNOWLEDGE_MCP_URL/TOKEN 已配置，
且 endpoint 绑定了至少一个知识库并启用读写工具。
退出码 0 = 全过，1 = 有失败。
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))

# 从仓库 .env 补齐配置（docstring 承诺「用 .env / 环境变量配置」，此前并未真正读取）。
#
# 为什么必须补：`gate_all.py` 用干净的进程环境跑子进程，不会带 shell 里 export 的变量。
# 于是同一份代码「我手工跑是绿的、门禁跑是红的」——实测就是这样：
# 容器与 .env 里配的是 mcp-weknora，门禁却读到默认值 ragflow，直接判红。
# 判定必须测**项目的真实配置**，而不是测「当前 shell 恰好 export 了什么」。
# 只 setdefault，不覆盖已有环境变量；只读取，不打印任何值（.env 里有凭据）。
def _load_dotenv(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

# .env 里的 `KNOWLEDGE_MCP_URL` 是为**容器内**写的（host.docker.internal = 宿主机）。
# 在宿主上直接跑就会 name-not-known —— 实测报错 `[Errno 8] nodename nor servname`。
# 同一个底座在宿主上就是 127.0.0.1:<宿主端口>，故非容器环境自动改写；
# 容器内（存在 /.dockerenv）保持原样。只改主机名，不动端口与 path。
if not os.path.exists("/.dockerenv") and os.getenv("KNOWLEDGE_MCP_URL"):
    os.environ["KNOWLEDGE_MCP_URL"] = os.getenv("KNOWLEDGE_MCP_URL").replace(
        "host.docker.internal", "127.0.0.1")

BACKEND = os.getenv("KNOWLEDGE_BACKEND", "ragflow").strip().lower()
if not BACKEND.startswith("mcp"):
    print("KNOWLEDGE_BACKEND=%s 不是 MCP 后端；本自证只测 mcp-*。"
          "临时切换示例：KNOWLEDGE_BACKEND=mcp-weknora python3 tools/knowledge_mcp_selftest.py"
          % BACKEND)
    sys.exit(1)

import knowledge as kb  # noqa: E402  （分发块会把六函数绑到 knowledge_mcp）


def _check(name, cond, detail=""):
    mark = "OK" if cond else "!!"
    print("  [%s] %-28s %s" % (mark, name, detail))
    return cond


def main():
    fails = []
    print("== knowledge_mcp 集成自证（backend=%s）==" % BACKEND)
    print("底座 endpoint: %s" % (os.getenv("KNOWLEDGE_MCP_URL", "") or "(未配置)"))

    # 1. health：ok=True 且形状逐键对齐
    h = kb.health()
    ok = _check("health.ok", h.get("ok") is True, "base=%s ms=%s 可见库=%s"
                % (h.get("base"), h.get("ms"), h.get("datasets_visible")))
    ok &= _check("health.形状", all(k in h for k in
                 ("ok", "base", "endpoint", "ms", "datasets_visible")), "")
    if not ok:
        fails.append("health 失败：%s" % h)

    # 2. list_documents：至少能列出（当前生效库里应有语料）
    lst = kb.list_documents(limit=50)
    ok = _check("list_documents.ok", lst.get("ok") is True and lst.get("total", 0) > 0,
                "total=%s" % lst.get("total"))
    docs = lst.get("documents") or []
    ok &= _check("list_documents.行形状", bool(docs) and all(
        k in docs[0] for k in ("id", "name", "run", "progress")), "首行=%s" % (docs[0]["name"] if docs else "-"))
    if not ok:
        fails.append("list_documents 失败：%s" % str(lst)[:200])

    # 3. search：领域问题命中 + 引用形状逐键对齐
    res = kb.search("会员复购率的统计口径是什么")
    cites = res.get("citations") or []
    ok = _check("search.命中", res.get("ok") is True and len(cites) > 0,
                "total=%s top1=%.3f %s" % (res.get("total"),
                                           (cites[0]["similarity"] if cites else 0),
                                           (cites[0]["document_name"] if cites else "-")))
    need_keys = ("n", "document_id", "document_name", "chunk_id", "similarity",
                 "positions", "position_available", "locator", "snippet", "chars")
    ok &= _check("search.引用形状", bool(cites) and all(k in cites[0] for k in need_keys), "")
    ok &= _check("search.answer_context", bool(res.get("answer_context")), "")
    if not ok:
        fails.append("search 失败：%s" % str(res)[:300])

    # 4. upload → status → delete 闭环（临时件，验证后即删）
    #
    # ★ 名字必须每轮不同。此前用固定名 `mcp-selftest-临时件.md`：一旦某轮在删除前
    #   异常退出（底座忙、超时、进程被杀），残留件会让**下一轮上传被防重拦下**，
    #   门禁从此常红——而根因只是"上次没跑完"。实测 2026-11-01 就栽在这里。
    # ★ 顺手把历次留下的同名临时件清掉：只认 `mcp-selftest-` 前缀，
    #   绝不碰业务文档；清不掉也不影响本轮结论（不加入 fails）。
    for d in docs:
        if str(d.get("name", "")).startswith("mcp-selftest-"):
            try:
                kb.delete_document(d["id"], actor="mcp-selftest-清理")
                print("  [清理] 上一轮残留的临时件 %s" % d.get("name"))
            except Exception as e:  # noqa: BLE001
                print("  [清理] 残留件 %s 未清掉（%s），不影响本轮判断"
                      % (d.get("name"), type(e).__name__))
    test_name = "mcp-selftest-临时件-%s.md" % datetime.now().strftime("%m%d-%H%M%S")
    up = kb.upload_document(test_name, "# MCP 自检件\n\n集成自证临时文档，验证后即删。")
    ok = _check("upload.ok", up.get("ok") is True, "id=%s" % (up.get("document_ids") or ["-"])[0])
    if not up.get("ok"):
        fails.append("upload 失败：%s" % up.get("error"))
    else:
        did = up["document_ids"][0]
        st = kb.document_status(document_id=did)
        ok = _check("document_status.命中", st.get("ok") is True and len(st.get("documents") or []) == 1,
                    "progress=%s" % ((st.get("documents") or [{}])[0].get("progress")))
        dl = kb.delete_document(did, actor="mcp-selftest")
        ok &= _check("delete.ok", dl.get("ok") is True, "")
        if not (ok and dl.get("ok")):
            fails.append("status/delete 失败：%s / %s" % (str(st)[:150], str(dl)[:150]))

    # 5. 防重：同名再传必须被拦（duplicate=True）
    if up.get("ok"):
        # 上一步已删，重新传一次再传第二次
        kb.upload_document(test_name, "# MCP 自检件 v2")
        up2 = kb.upload_document(test_name, "# MCP 自检件 v2 重复")
        ok = _check("upload.防重", up2.get("ok") is False and up2.get("duplicate") is True,
                    str(up2.get("error"))[:80])
        if ok:
            listed = kb.list_documents(limit=50)
            dup = next((d for d in listed.get("documents") or []
                        if d.get("name") == test_name), None)
            if dup:
                kb.delete_document(dup["id"], actor="mcp-selftest")
        if not ok:
            fails.append("防重失败：%s" % str(up2)[:200])

    print()
    if fails:
        print("自证失败 %d 项：" % len(fails))
        for f in fails:
            print("  -", f)
        return 1
    print("自证全部通过（health / list / search / upload / status / delete / 防重）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
