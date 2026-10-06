#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识入库脚本（M2）

把 `knowledge/*.md` 上传到 RAGFlow 独立栈知识底座并触发解析建索引。

三段式（RAGFlow 原生流程，网关侧零改动）
----------------------------------------
1. `POST /api/v1/datasets/{id}/documents`  上传（multipart）
2. `POST /api/v1/datasets/{id}/chunks`     触发解析
3. `GET  /api/v1/datasets/{id}/documents`  轮询解析状态直到 DONE

用法：
    python tools/ingest_knowledge.py                 # 入库 knowledge/ 下全部 .md
    python tools/ingest_knowledge.py --only 01       # 只入库文件名含 "01" 的
    python tools/ingest_knowledge.py --status        # 只看当前入库与解析状态，不上传
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DIR = os.path.join(ROOT, "knowledge")


def load_env():
    """从项目 .env 读取知识库凭据（不回显）。"""
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    for k in ("KNOWLEDGE_API_URL", "KNOWLEDGE_API_KEY", "KNOWLEDGE_DATASET_ID"):
        if os.getenv(k):
            env[k] = os.getenv(k)
    return env


class KB:
    def __init__(self, env, base=None):
        # .env 里的地址是给**网关容器**用的（host.docker.internal），
        # 本脚本在宿主机跑，需回到回环地址；也可用 --base 显式覆盖。
        url = base or env.get("KNOWLEDGE_API_URL", "http://127.0.0.1:19380/api/v1")
        url = url.replace("host.docker.internal", "127.0.0.1")
        self.base = url.rstrip("/")
        self.key = env.get("KNOWLEDGE_API_KEY", "")
        self.ds = env.get("KNOWLEDGE_DATASET_ID", "")
        self.op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if not self.key:
            raise SystemExit("未配置 KNOWLEDGE_API_KEY（见项目 .env）")

    def _headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.key}
        if extra:
            h.update(extra)
        return h

    def json_get(self, path):
        req = urllib.request.Request(self.base + path, headers=self._headers())
        with self.op.open(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"), strict=False)

    def json_post(self, path, payload):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode(),
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            with self.op.open(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"), strict=False)
        except urllib.error.HTTPError as e:
            return {"code": e.code, "message": e.read().decode("utf-8", errors="replace")[:300]}

    def upload(self, filename, data):
        boundary = "----kbform" + uuid.uuid4().hex
        parts = []
        parts.append(
            ('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
             "Content-Type: text/markdown\r\n\r\n" % (boundary, filename)).encode()
        )
        parts.append(data)
        parts.append(b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode())
        body = b"".join(parts)
        req = urllib.request.Request(
            self.base + "/datasets/%s/documents" % self.ds,
            data=body,
            headers=self._headers({"Content-Type": "multipart/form-data; boundary=" + boundary}),
            method="POST",
        )
        try:
            with self.op.open(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"), strict=False)
        except urllib.error.HTTPError as e:
            return {"code": e.code, "message": e.read().decode("utf-8", errors="replace")[:400]}

    def documents(self, page_size=100):
        d = self.json_get("/datasets/%s/documents?page=1&page_size=%d" % (self.ds, page_size))
        return (d.get("data") or {}).get("docs") or []

    def parse(self, doc_ids):
        return self.json_post("/datasets/%s/chunks" % self.ds, {"document_ids": doc_ids})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只处理文件名含该子串的文件")
    ap.add_argument("--status", action="store_true", help="只查看状态")
    ap.add_argument("--base", help="知识库 API 地址（默认取 .env，并回落为 127.0.0.1）")
    ap.add_argument("--wait", type=int, default=180, help="等待解析完成的秒数上限")
    args = ap.parse_args()

    kb = KB(load_env(), base=args.base)
    print("== 知识入库 ==")
    print("端点 %s | 数据集 %s" % (kb.base, kb.ds))

    if not args.status:
        files = sorted(f for f in os.listdir(KB_DIR) if f.endswith(".md"))
        if args.only:
            files = [f for f in files if args.only in f]
        print("待入库 %d 篇：%s\n" % (len(files), ", ".join(files)))

        new_ids = []
        for fn in files:
            with open(os.path.join(KB_DIR, fn), "rb") as fh:
                data = fh.read()
            res = kb.upload(fn, data)
            ok = res.get("code") == 0
            docs = (res.get("data") or []) if isinstance(res.get("data"), list) else []
            did = docs[0].get("id") if docs else None
            print("  %s %-46s %s" % ("✅" if ok else "❌", fn,
                                     ("doc_id=" + did) if did else str(res.get("message"))[:90]))
            if did:
                new_ids.append(did)

        # 已存在同名文档时 RAGFlow 会返回已存在，这里按名称兜底匹配
        existing = {d["name"]: d["id"] for d in kb.documents()}
        for fn in files:
            if fn in existing and existing[fn] not in new_ids:
                new_ids.append(existing[fn])

        if new_ids:
            r = kb.parse(new_ids)
            print("\n触发解析：code=%s %s" % (r.get("code"), str(r.get("message") or "")[:80]))

    # 轮询状态
    if not args.status:
        print("\n等待解析", end="", flush=True)
        deadline = time.time() + args.wait
        while time.time() < deadline:
            docs = kb.documents()
            running = [d for d in docs if (d.get("run") or "").upper() in ("RUNNING", "UNSTART")]
            if not running:
                break
            print(".", end="", flush=True)
            time.sleep(5)
        print()

    print("\n当前入库清单：")
    print("  %-46s %-10s %8s %10s" % ("文档", "解析状态", "分块数", "大小"))
    docs = kb.documents()
    for d in sorted(docs, key=lambda x: x.get("name") or ""):
        print("  %-46s %-10s %8s %10s"
              % ((d.get("name") or "")[:44], d.get("run"), d.get("chunk_count"), d.get("size")))
    done = [d for d in docs if (d.get("run") or "").upper() == "DONE"]
    print("\n已入库 %d 篇，其中解析完成 %d 篇" % (len(docs), len(done)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
