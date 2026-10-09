# -*- coding: utf-8 -*-
"""知识检索（M2 · RAGFlow 引用式召回）

对接
----
RAGFlow v0.26.4 原生检索接口 `POST /api/v1/retrieval`（自带 OIDC/API-Key 鉴权，
网关侧零代码侵入 RAGFlow，只做客户端）。

底座唯一性
----------
网关只连**一个** RAGFlow API（`KNOWLEDGE_API_URL` 指向其 `/v1`），代码里不存在第二个
底座地址、也不存在 FileBay 兜底分支：RAGFlow 不可用时 `search()` 如实抛
`KnowledgeError`、`health()` 返回 `ok=false`，由上层把该证据源标为「未就绪」，
而不是悄悄换一个底座给出看似正常的结果。

注意「唯一底座」≠「独立部署」。B4 实测：19380 入口是 nginx 代理，其 backend 仍是
旧绑定栈 compose 内的 `ragflow-cpu`——**「RAGFlow 独立栈」在物理上尚未成立**，
文档口径亦已按此写明。

实测得到的三个关键契约（决定了下面的默认参数）
----------------------------------------------
1. **`similarity_threshold` 必须 > 0**：传 `0.0` 会被服务端按 falsy 处理、
   回退到默认 `0.2`，看起来"阈值放宽了"其实没有。
2. **默认 `vector_similarity_weight = 0.3` 偏重关键词**：融合分 = 0.3×向量 + 0.7×关键词。
   中文业务问句（如"坪效怎么算"）关键词重叠率低，融合分容易被压到 0.2 以下而漏召回。
   实测同一篇文档：`"坪效"` 融合分 0.638，`"坪效怎么算"` 掉到阈值以下。
3. **`total` 是命中的 chunk 数**，`doc_aggs` 给出按文档聚合的命中分布。

因此本题默认取 `threshold=0.1` + `vector_weight=0.7`：以召回优先，交给上层做证据筛选。

引用可追溯
----------
返回的每条 `citation` 都带 `document_id / document_name / chunk_id / positions`，
下游（M3 证据编排、M5 交付说明）据此回溯到"哪篇文档、哪一段"。
"""
import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

API_BASE = os.getenv("KNOWLEDGE_API_URL", "http://127.0.0.1:19380/api/v1")
API_KEY = os.getenv("KNOWLEDGE_API_KEY", "").strip()
API_KEY_FILE = os.getenv("KNOWLEDGE_API_KEY_FILE", "").strip()
DATASET_ID = os.getenv("KNOWLEDGE_DATASET_ID", "").strip()
# 底座标识。**这里只报"网关连的是哪个 RAGFlow API"，不报"部署形态已独立"**——
# B4 实测：19380 入口是 nginx 代理，backend 仍是旧绑定栈 compose 内的 ragflow-cpu，
# 「RAGFlow 独立栈」在物理上尚未成立。改名这个字段等于把未完成的事说成已完成。
BASE_NAME = os.getenv("KNOWLEDGE_BASE_NAME", "RAGFlow API（19380 入口）").strip()
TIMEOUT = int(os.getenv("KNOWLEDGE_TIMEOUT", "30"))
# 上传单独给更长的超时：文档可能几十 MB，且解析排队发生在上传之后，
# 但上传本身在 OrbStack 上实测也会跑到秒级，不能沿用检索的 30s 之外再叠加。
UPLOAD_TIMEOUT = int(os.getenv("KNOWLEDGE_UPLOAD_TIMEOUT", "120"))
# 允许入库的扩展名。RAGFlow 本身能解析更多类型，但准入是业务决定：
# 只放行文档类，避免把二进制/可执行内容塞进知识库。
ALLOWED_SUFFIXES = (
    ".md", ".markdown", ".txt", ".pdf", ".docx", ".doc", ".pptx", ".ppt",
    ".xlsx", ".xls", ".csv", ".html", ".htm",
)
# 单个文档大小上限（RAGFlow 侧也有限制，这里先挡住明显超大的）。
MAX_UPLOAD_BYTES = int(os.getenv("KNOWLEDGE_MAX_UPLOAD_MB", "32")) * 1024 * 1024

# 单个单元格的字符上限。超过这个值的单元格**不能入库**——见 scan_oversized_cells
# 的注释：它会让 embedding 服务静默卡死，整份文档解析失败且看不到原因。
# 2048 与 TEI 的 --max-batch-tokens 对齐（实测 2500 字仍安全、6000 字必卡）。
# 正常业务单元格远达不到：实测该业务 xlsx 单元格长度中位 4 字符、p95 64 字符。
MAX_CELL_CHARS = int(os.getenv("KNOWLEDGE_MAX_CELL_CHARS", "2048"))
# 预检最多报多少处。超大表格可能有成百上千处超标，全报会把错误信息撑爆；
# 前 20 处已足够定位到「是哪张表的哪一类字段」。
MAX_CELL_REPORT = 20

# 实测校准后的默认检索参数
DEFAULT_THRESHOLD = 0.1
DEFAULT_VECTOR_WEIGHT = 0.7
DEFAULT_TOP_K = 5
SNIPPET_CHARS = 400


class KnowledgeError(RuntimeError):
    pass


def _key():
    global API_KEY
    if API_KEY:
        return API_KEY
    if API_KEY_FILE and os.path.exists(API_KEY_FILE):
        with open(API_KEY_FILE, encoding="utf-8") as f:
            API_KEY = f.read().strip()
        return API_KEY
    return ""


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(path, payload):
    key = _key()
    if not key:
        raise KnowledgeError(
            "未配置知识库 API Key：请设置 KNOWLEDGE_API_KEY 或 KNOWLEDGE_API_KEY_FILE"
        )
    url = API_BASE.rstrip("/") + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise KnowledgeError("知识库 HTTP %s：%s" % (e.code, body[:200]))
    except Exception as e:
        raise KnowledgeError(
            "知识库不可达（%s）：%s: %s" % (API_BASE, type(e).__name__, str(e)[:200])
        )
    # RAGFlow 的 chunk 正文可能含未转义控制字符，用宽松模式解析
    return json.loads(raw, strict=False)


def _upload(path, filename, content, content_type="application/octet-stream"):
    """multipart/form-data 上传（RAGFlow 的 `documents/upload` 不是 JSON 接口）。

    手写 multipart 而不引入 requests/httpx：`requirements.txt` 只放行 fastmcp /
    starlette / psycopg / minio / sqlglot，新增运行时依赖要过红线五。
    字段名必须是 `file`（技能实测 v0.26.4 的契约）。
    """
    key = _key()
    if not key:
        raise KnowledgeError(
            "未配置知识库 API Key：请设置 KNOWLEDGE_API_KEY 或 KNOWLEDGE_API_KEY_FILE"
        )
    boundary = "----askoda%s" % uuid.uuid4().hex
    sep = ("--" + boundary + "\r\n").encode("ascii")
    # 中文文件名走 RFC 5987 的 filename*，同时给一个 ASCII 兜底名。
    # 只塞 ASCII 会被服务端按 latin-1 解码——中文知识文档名很常见，必须处理。
    disp = (
        'Content-Disposition: form-data; name="file"; filename="%s"; '
        "filename*=UTF-8''%s\r\n"
        % (_ascii_filename(filename), urllib.parse.quote(str(filename or ""), safe=""))
    )
    body = b"".join([
        sep,
        disp.encode("utf-8"),
        b"Content-Type: " + (content_type or "application/octet-stream").encode("ascii") + b"\r\n\r\n",
        content,
        b"\r\n",
        ("--" + boundary + "--\r\n").encode("ascii"),
    ])
    req = urllib.request.Request(
        API_BASE.rstrip("/") + path,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with _opener().open(req, timeout=UPLOAD_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise KnowledgeError("知识库 HTTP %s：%s" % (e.code, detail[:200]))
    except Exception as e:
        raise KnowledgeError(
            "知识库不可达（%s）：%s: %s" % (API_BASE, type(e).__name__, str(e)[:200])
        )
    return json.loads(raw, strict=False)


def _ascii_filename(name):
    """multipart 头里的 filename 只允许 ASCII，中文名走 RFC 5987 的 filename*。

    直接塞中文会让部分服务端按 latin-1 解码而报 400 或乱码。
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(name or ""))
    return safe or "upload.bin"


def _delete(path, payload):
    key = _key()
    if not key:
        raise KnowledgeError(
            "未配置知识库 API Key：请设置 KNOWLEDGE_API_KEY 或 KNOWLEDGE_API_KEY_FILE"
        )
    req = urllib.request.Request(
        API_BASE.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="DELETE",
    )
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise KnowledgeError("知识库 HTTP %s：%s" % (e.code, detail[:200]))
    except Exception as e:
        raise KnowledgeError("知识库不可达：%s: %s" % (type(e).__name__, str(e)[:200]))
    return json.loads(raw, strict=False)


def _get(path):
    key = _key()
    if not key:
        raise KnowledgeError(
            "未配置知识库 API Key：请设置 KNOWLEDGE_API_KEY 或 KNOWLEDGE_API_KEY_FILE"
        )
    req = urllib.request.Request(
        API_BASE.rstrip("/") + path, headers={"Authorization": "Bearer " + key}
    )
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"), strict=False)
    except Exception as e:
        raise KnowledgeError("知识库不可达：%s: %s" % (type(e).__name__, str(e)[:200]))


# ---------------------------------------------------------------------------
# 连通性与数据集
# ---------------------------------------------------------------------------
def health():
    """知识库连通性 + 数据集可见性 + 当前数据集已解析文档数。"""
    t0 = time.time()
    try:
        ds = _get("/datasets?page=1&page_size=100")
        items = ds.get("data") or []
        target = None
        for it in items:
            if not DATASET_ID or it.get("id") == DATASET_ID:
                target = it
                break
        if target is None and items:
            target = items[0]
        docs = 0
        if target:
            try:
                d = _get("/datasets/%s/documents?page=1&page_size=1" % target["id"])
                docs = (d.get("data") or {}).get("total", 0)
            except Exception:
                docs = None
        return {
            "ok": True,
            "base": BASE_NAME,
            "endpoint": API_BASE,
            "ms": int((time.time() - t0) * 1000),
            "datasets_visible": len(items),
            "dataset": (
                {
                    "id": target.get("id"),
                    "name": target.get("name"),
                    "documents": docs,
                    "chunk_method": target.get("chunk_method"),
                    "embedding_model": target.get("embedding_model"),
                }
                if target
                else None
            ),
        }
    except Exception as e:
        return {
            "ok": False,
            "base": BASE_NAME,
            "endpoint": API_BASE,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
            "ms": int((time.time() - t0) * 1000),
        }


def _parse_error_text(row):
    """取文档解析失败的原因。

    实测（2026-10-08，1296 块的 xlsx 上传）：RAGFlow **把失败原因放在 `progress_msg`**，
    形如「…[ERROR]Generate embedding error:Embedding request failed…」；
    而 `error_msg` 恒为空。只取 `error_msg` 会让前端拿到空错误，界面上只剩
    「解析失败」四个字，运维无从下手——本次正是靠直连底座翻 `progress_msg` 才定位到
    「embedding 批大小 16 > 上限 8」。因此失败时从 `progress_msg` 里挑信息量最大的
    那条 `[ERROR]` 行；抽不到就退化为进度日志末尾片段（仍然比空串有用）。
    """
    msg = (row.get("error_msg") or row.get("parse_error") or "").strip()
    if msg:
        return msg
    # 只在**失败**时翻进度日志：正常/解析中的文档 progress_msg 是正常日志，
    # 原样返回会让前端把「解析完成」当成错误原因显示出来。
    if str(row.get("run") or "").upper() != "FAIL":
        return ""
    pm = (row.get("progress_msg") or "").strip()
    if not pm:
        return ""
    # 取**最长**的一条 [ERROR]，不是最后一条：实测日志顺序是
    # 「[ERROR]Generate embedding error: <含 413 详情的根因>」→「[ERROR][Exception]: <一句话>」，
    # 取最后一条只能拿到那句短的，根因反而丢了。
    errs = [l.strip() for l in pm.splitlines() if "[ERROR]" in l]
    if errs:
        return max(errs, key=len)[:500]
    return pm[-300:]


def list_documents(limit=50):
    ds_id = DATASET_ID
    if not ds_id:
        h = health()
        ds_id = ((h.get("dataset") or {}).get("id")) or ""
    if not ds_id:
        return {"ok": False, "error": "无法确定数据集 ID"}
    d = _get("/datasets/%s/documents?page=1&page_size=%d" % (ds_id, limit))
    rows = (d.get("data") or {}).get("docs") or []
    return {
        "ok": True,
        "dataset_id": ds_id,
        "total": (d.get("data") or {}).get("total", len(rows)),
        "documents": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "run": r.get("run"),
                # progress 与 run 一起给出：前端要显示"解析中/已完成/失败"，
                # 只给 run 的话前端无法区分「WAITING」和「CANCELLED」。
                "progress": r.get("progress"),
                "chunk_count": r.get("chunk_count"),
                "size": r.get("size"),
                "created_at": r.get("create_time"),
                "parse_error": _parse_error_text(r),
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# 知识准入（上传 → 解析 → 撤库）
# ---------------------------------------------------------------------------
# 2026-10-07 补齐。此前网关只有检索侧工具（search/health/documents/retire/citation_list），
# **没有任何写入工具**，前端「知识收集」区的上传功能因此从未通过；
# 页面提示写的是「依赖 RAGFlow 独立站，B4 未复位完成」，与实测不符——
# 实测 19380 入口的检索/列表/解析接口全部 code=0 可用，缺的是网关侧实现，不是底座能力。
def _resolve_dataset_id():
    ds_id = DATASET_ID
    if not ds_id:
        ds_id = ((health().get("dataset") or {}).get("id")) or ""
    return ds_id


def _all_document_names():
    """取数据集内**全部**文档名（按 total 翻页），返回 (names, reachable)。

    `page_size` 服务端上限 100，一次请求拿不全；防重判断必须看全量，
    否则文档多了会漏判、把重复件放进去。

    ⚠️ `reachable=False` 表示**没能确认库里到底有哪些文档**（底座不可达、
    或查询中途被拒/失败），此时 `names` 可能不完整，**防重不能当真**。

    实测踩到的坑（2026-10-09）：原先这里 `except KnowledgeError: return names`
    会把失败吞成空集合，于是防重**静默失效**——重复文档照常入库，调用方毫无察觉。
    起因是宿主直跑时 `KNOWLEDGE_API_URL` 配的 `host.docker.internal`（容器侧域名，
    宿主解析不了）。**配置错误被"宁可放过"的容错一起掩盖了**。
    所以取不全必须如实上报，由上层提示"本次未做重名检查"；
    仍然不阻断上传——同一口径重复入库是脏，但阻断正常入库是瘫，前者可接受。
    """
    ds_id = _resolve_dataset_id()
    if not ds_id:
        return set(), False
    names, page = set(), 1
    try:
        while page <= 50:  # 上限 50 页 = 5000 份，够用且不会打爆上游
            d = _get("/datasets/%s/documents?page=%d&page_size=100" % (ds_id, page))
            if d.get("code") != 0:
                return names, False  # 服务端拒了，等于没查到，不能当"确认无重名"
            data = d.get("data") or {}
            rows = data.get("docs") or []
            names.update((r.get("name") or "") for r in rows if isinstance(r, dict))
            total = data.get("total", len(rows))
            if not rows or len(rows) * page >= total:
                break
            page += 1
    except KnowledgeError:
        return names, False
    return names, True


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _xlsx_cell_text(cell, shared):
    """取单元格文本。xlsx 有三种存法，少一种就会漏检：
    t="s"（共享字符串，v 里存下标）/ t="inlineStr"（内联）/ 默认（数字等原文）。
    """
    t = cell.get("t")
    if t == "inlineStr":
        is_el = cell.find(_XLSX_NS + "is")
        if is_el is None:
            return ""
        return "".join(x.text or "" for x in is_el.iter(_XLSX_NS + "t"))
    v = cell.find(_XLSX_NS + "v")
    if v is None or v.text is None:
        return ""
    if t == "s":
        try:
            idx = int(v.text)
        except ValueError:
            return ""
        return shared[idx] if 0 <= idx < len(shared) else ""
    return v.text


def _xlsx_sheets(zf, names):
    """sheet 名 → 表 XML 路径。拿不到 rels 就退化为按路径排序，宁可少查也不报错。"""
    if "xl/workbook.xml" not in names:
        return [
            ("Sheet%d" % i, p)
            for i, p in enumerate(
                sorted(n for n in names if n.startswith("xl/worksheets/")), 1
            )
        ]
    out = []
    try:
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            rr = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            for rel in rr:
                rels[rel.get("Id")] = rel.get("Target") or ""
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        for sh in wb.iter(_XLSX_NS + "sheet"):
            target = rels.get(sh.get(_XLSX_REL_NS + "id"), "")
            path = target.lstrip("/")
            if path and not path.startswith("xl/"):
                path = "xl/" + path
            if path in names:
                out.append((sh.get("name") or "Sheet", path))
    except Exception:
        pass
    return out


def _scan_xlsx(content, limit):
    """扫 xlsx 里超过 limit 字符的单元格。只用标准库：xlsx 本质是 zip + XML，
    红线五不放行 openpyxl 这类新增运行时依赖，自己解析完全够用。
    """
    hits = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception:
        return hits  # 不是合法 xlsx：交给 RAGFlow 报错，预检不抢着判
    try:
        names = set(zf.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(x.text or "" for x in si.iter(_XLSX_NS + "t"))
                    for si in root.findall(_XLSX_NS + "si")
                ]
            except Exception:
                shared = []
        for sname, path in _xlsx_sheets(zf, names):
            try:
                root = ET.fromstring(zf.read(path))
            except Exception:
                continue
            for cell in root.iter(_XLSX_NS + "c"):
                text = _xlsx_cell_text(cell, shared)
                if text and len(text) > limit:
                    hits.append({
                        "location": "%s!%s" % (sname, cell.get("r") or "?"),
                        "sheet": sname,
                        "cell": cell.get("r") or "?",
                        "chars": len(text),
                        "preview": text[:40],
                    })
                    if len(hits) >= MAX_CELL_REPORT:
                        return hits
    finally:
        zf.close()
    return hits


def _scan_csv(content, limit):
    hits = []
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return hits
    try:
        for row_no, row in enumerate(csv.reader(io.StringIO(text)), 1):
            for col_no, val in enumerate(row, 1):
                if len(val) > limit:
                    hits.append({
                        "location": "第%d行第%d列" % (row_no, col_no),
                        "sheet": "(CSV)",
                        "cell": "R%dC%d" % (row_no, col_no),
                        "chars": len(val),
                        "preview": val[:40],
                    })
                    if len(hits) >= MAX_CELL_REPORT:
                        return hits
    except Exception:
        return hits
    return hits


def scan_oversized_cells(filename, content, limit=None):
    """上传前扫描表格里的超长单元格，判断能不能安全入库。

    为什么要拦这一道
    ----------------
    RAGFlow 的 naive 分块**对超长 excel 行不切分**：即便 dataset 配了
    `chunk_token_num=512`，一个 32767 字符的单元格（Excel 单元格上限）也会被
    整段作为一块送进 embedding。而 TEI 处理单条超过 `--max-batch-tokens`(2048)
    的文本会**静默卡死**——进程不退出、健康检查仍是 healthy，但 CPU 掉到 1%、
    探活 60s 无响应，只能重启。整份文档最终 `run=FAIL / chunk_count=0`，
    前端只看到「解析失败」四个字，看不到任何原因。

    实测依据（2026-10-09，非推断）：
    - 单条 1500 / 2000 / 2500 字正常返回（6~10s），**6000 字必卡**；
    - 连发 12 批正常长度（16 条 × 100 字）全部通过 —— 不是累积性故障，
      只跟**单条长度**有关；
    - 含 36 个超长单元格的真实 xlsx 每次都精确卡死在同一批（progress=0.71003），
      100% 可复现。
    - 调大 TEI 参数救不了：16384、8192 都在 warm up 阶段被 OOM 静默杀掉
      （无 panic 日志、RestartCount 连续 +1），12g 上限撑不住。

    为什么只查单元格、不查"整行拼接长度"
    --------------------------------------
    正常行拼接几十列也可能过千字符，按行判会误伤正常文件；而实测元凶就是单个
    超长单元格（32767 字符）。判单元格最准，也能直接指出「哪张表哪个格子」
    让用户去改。实测该业务 xlsx 单元格长度中位仅 4 字符、p95 64 字符，
    2048 的阈值离正常数据很远，不存在误杀。

    返回
    ----
    {"ok": True,  "checked": True}                    —— 扫过，没问题
    {"ok": True,  "checked": False}                   —— 类型不支持预检，放行
    {"ok": False, "checked": True, "cells": [...]}    —— 有会打断入库的超长内容
    """
    limit = int(limit or MAX_CELL_CHARS)
    suffix = os.path.splitext(str(filename or ""))[1].lower()
    if suffix == ".xlsx":
        hits = _scan_xlsx(content, limit)
    elif suffix == ".csv":
        hits = _scan_csv(content, limit)
    else:
        # 一律放行——预检是保护，不能变成新的阻断源。
        # 但 .xls 是**真盲区**：它是旧二进制格式，标准库解不开
        # （红线五不放行 openpyxl 这类新增依赖），里面照样可能有超长单元格，
        # 照样会把 embedding 打死。所以放行同时如实带出风险提示，
        # 让调用方能告诉用户"建议另存为 .xlsx"。
        # 其余类型（md/pdf/docx…）由 RAGFlow 按 chunk_token_num 切分，
        # 不会产出超长块，无需提示。
        note = ""
        if suffix == ".xls":
            note = (".xls 是旧二进制格式，上传前无法扫描；若含超长单元格仍会让"
                    "解析卡死，建议另存为 .xlsx 再传")
        return {"ok": True, "checked": False, "limit": limit, "note": note}
    if hits:
        return {"ok": False, "checked": True, "limit": limit, "cells": hits}
    return {"ok": True, "checked": True, "limit": limit}


def upload_document(filename, content, content_type="", dataset_id=""):
    """上传一份文档并触发解析，返回新文档的 id 与解析状态。

    分两步（实测 v0.26.4 契约）：先 `POST /datasets/{id}/documents` 直传，
    再 `POST /datasets/{id}/documents/parse` 触发切分入库。直传即可，无需再走
    `files/link-to-datasets`（技能第四节实测结论）。
    """
    name = str(filename or "").strip()
    if not name:
        return {"ok": False, "error": "缺少文件名"}
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in ALLOWED_SUFFIXES:
        return {
            "ok": False,
            "error": "不支持的文件类型 %s；允许：%s"
            % (suffix or "(无扩展名)", "、".join(ALLOWED_SUFFIXES)),
        }
    if not content:
        return {"ok": False, "error": "文件内容为空"}
    if len(content) > MAX_UPLOAD_BYTES:
        return {
            "ok": False,
            "error": "文件超过上限 %d MB" % (MAX_UPLOAD_BYTES // 1024 // 1024),
        }
    ds_id = dataset_id or _resolve_dataset_id()
    if not ds_id:
        return {"ok": False, "error": "无法确定数据集 ID"}

    # 防重：RAGFlow 对同名文件会静默改名入库（`x.md` → `x(1).md`），
    # 同一份口径重复入库会产生互相矛盾的证据（检索时两版都可能被召回）。
    # 这里在入库前拦下并如实报出已有文档的 id，让前端提示"要换就先撤库"。
    # 注意分页：`page_size` 服务端上限 100（技能第四节），一次拉 1000 会被截断，
    # 文档多了就会漏判，所以这里按 total 翻页取全量名字。
    # `dedup_ok=False` 表示没能确认库里有什么，此时**不阻断**（见 _all_document_names
    # 注释）但必须如实带出 `dedup_checked=false`，别让调用方以为查过了。
    existing, dedup_ok = _all_document_names()
    if name in existing:
        listed = list_documents(limit=100)
        dup = next(
            (d for d in (listed.get("documents") or []) if (d.get("name") or "") == name),
            None,
        )
        return {
            "ok": False,
            "error": "库中已有同名文档《%s》；知识库同一口径只留一份，"
            "请先撤库再上传，或改用带版本号的文件名" % name,
            "duplicate": True,
            "document_id": (dup or {}).get("id", ""),
            "run": (dup or {}).get("run", ""),
        }

    # 上传前预检：表格里的超长单元格会让 embedding 静默卡死，整份文档解析失败
    # 且前端只显示「解析失败」看不到原因（实测依据见 scan_oversized_cells 注释）。
    # 放在防重**之后**：不改动既有任何分支的判定顺序，只在真要上传前加一道闸。
    dedup_note = "" if dedup_ok else "知识库不可达，本次未做重名检查（可能重复入库）"

    scan = scan_oversized_cells(name, content)
    precheck_note = scan.get("note") or ""  # 如 .xls 无法扫描的风险提示
    if not scan.get("ok"):
        cells = scan.get("cells") or []
        detail = "；".join(
            "%s（%d 字符）" % (c.get("location"), c.get("chars")) for c in cells[:5]
        )
        tail = "（仅列出前 %d 处）" % len(cells) if len(cells) >= MAX_CELL_REPORT else ""
        return {
            "ok": False,
            "error": "文件里有超长单元格（单格超过 %d 字符），会导致入库时 embedding "
                     "卡死、整份文档解析失败且看不到原因。%s%s。"
                     "请先把这些单元格拆成多行或多列再上传。"
                     % (scan.get("limit"), detail or "（位置未能定位）", tail),
            "oversized": True,
            "cells": cells,
            "limit": scan.get("limit"),
        }

    resp = _upload(
        "/datasets/%s/documents" % ds_id, name, content,
        content_type or "application/octet-stream",
    )
    # **code 必须先判，再取 data**（技能第四节）：错误响应不带 data，
    # 若直接 resp["data"]["docs"] 并 or [] 兜底，会把「参数被拒」误读成「库里没文档」。
    if resp.get("code") != 0:
        return {
            "ok": False,
            "error": "上传被拒：%s" % (resp.get("message") or "code=%s" % resp.get("code")),
        }
    # 实测 v0.26.4：`data` 直接是**文档数组**（不是 {"docs": [...]}），
    # 且中文文件名在 filename* 下原样保留（如「结构探测件.md」）。
    data = resp.get("data")
    docs = data if isinstance(data, list) else ((data or {}).get("docs") or [])
    if not docs:
        return {"ok": False, "error": "上传成功但未返回文档 id，无法继续解析"}
    ids = [d.get("id") for d in docs if isinstance(d, dict) and d.get("id")]
    if not ids:
        return {"ok": False, "error": "上传响应缺少文档 id"}
    first_name = next(
        (d.get("name") for d in docs if isinstance(d, dict) and d.get("name")), name
    )

    # 触发解析（异步）。失败不判为上传失败——文档已在库里，只是没开始切分。
    parse_note = ""
    try:
        pr = _post("/datasets/%s/documents/parse" % ds_id, {"document_ids": ids})
        if pr.get("code") != 0:
            parse_note = "已入库但触发解析失败：%s" % (
                pr.get("message") or "code=%s" % pr.get("code")
            )
    except KnowledgeError as e:
        parse_note = "已入库但触发解析失败：%s" % str(e)[:160]

    # 三类提示合并进同一个 note：解析触发失败 / 防重没做成 / 预检没覆盖。
    # 分开成多个字段会让调用方逐个去判，合并后"有话要说"一眼能看到。
    notes = [x for x in (parse_note, dedup_note, precheck_note) if x]
    return {
        "ok": True,
        "dataset_id": ds_id,
        "name": first_name,
        "document_ids": ids,
        "size": len(content),
        "parse_started": not parse_note,
        "dedup_checked": dedup_ok,
        "note": "；".join(notes),
        # 前端据此轮询 document_status，不阻塞在本次调用上
        "status_hint": "解析为异步，请用 document_status 查询进度",
    }


def document_status(document_id="", limit=50):
    """查文档解析状态（前端轮询用）。"""
    ds_id = _resolve_dataset_id()
    if not ds_id:
        return {"ok": False, "error": "无法确定数据集 ID"}
    listed = list_documents(limit=limit)
    if not listed.get("ok"):
        return listed
    docs = listed["documents"]
    if document_id:
        docs = [d for d in docs if d.get("id") == document_id]
        if not docs:
            return {"ok": False, "error": "未找到文档 %s" % document_id}
        return {"ok": True, "dataset_id": ds_id, "documents": docs}
    return {"ok": True, "dataset_id": ds_id, "total": listed["total"], "documents": docs}


def delete_document(document_id, actor=""):
    """撤库：删掉一份文档（知识准入的反向操作）。

    只删 RAGFlow 里的文档本身，不碰网关的需求单与附件存储。
    """
    did = str(document_id or "").strip()
    if not did:
        return {"ok": False, "error": "缺少 document_id"}
    ds_id = _resolve_dataset_id()
    if not ds_id:
        return {"ok": False, "error": "无法确定数据集 ID"}
    resp = _delete("/datasets/%s/documents" % ds_id, {"ids": [did]})
    if resp.get("code") != 0:
        return {
            "ok": False,
            "error": "撤库被拒：%s" % (resp.get("message") or "code=%s" % resp.get("code")),
        }
    return {"ok": True, "dataset_id": ds_id, "document_id": did, "actor": actor}


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
# 实测记录（2026-09-30，同一数据集同一文档）：
#   「坪效」→ 命中 3 条；「什么是坪效」→ 命中 3 条；
#   「坪效怎么算」→ **0 条**；「怎么算」→ **0 条**；「坪效 计算方式」→ 命中 7 条。
# 即：含口语化疑问成分（"怎么算"）的问句会整体召回失败，而名词性问句正常。
# 因此本工具在零命中时自动去掉疑问词再检一次——业务人员恰恰最常问"XX 怎么算"。
# 分两档处理疑问成分，原因是单字虚词的**全局替换会静默损坏领域词**：
# 实测把「系统性能指标」削成「系统性 指标」、把「是否」削成「否」。
# 这种损坏比不召回更危险——它悄悄换掉了问题本身，检索还"正常返回结果"。
#   档一（多字短语）：不会落在词内部，可安全全局替换
PHRASE_STOPWORDS = [
    "口径是什么", "有什么区别", "有什么不同", "计算方式", "能不能查", "有哪些",
    "是什么", "什么是", "怎么算", "如何算", "能不能", "可不可以", "查一下",
    "为什么", "有什么", "能否", "是否", "怎么", "如何", "哪些", "哪个", "多少",
    "可以", "请问", "帮我", "一下", "查询", "计算", "方式", "方法", "区别", "能查",
]
#   档二（单字虚词）：只在句首/句尾剥离，绝不碰词内部
CHAR_STOPWORDS = ["是", "的", "了", "吗", "呢", "算", "和", "与", "啊", "吧"]


def extract_keywords(question):
    """把口语化问句压成关键词串（去疑问词与虚词），用于零命中时的重试。"""
    s = (question or "").strip()
    for w in sorted(PHRASE_STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    s = s.strip()
    # 首尾单字虚词反复剥离（「坪效的」→「坪效」），中途不做替换
    changed = True
    while changed and len(s) > 1:
        changed = False
        for w in CHAR_STOPWORDS:
            if len(s) > len(w) and s.startswith(w):
                s, changed = s[len(w):].strip(), True
            if len(s) > len(w) and s.endswith(w):
                s, changed = s[: -len(w)].strip(), True
    parts = [p.strip() for p in s.split() if len(p.strip()) >= 2]
    return " ".join(parts)


def _locator(chunk_id, positions):
    """片段定位串。positions 为 `[[page, left, right, top, bottom]]` 形态。

    RAGFlow **对表格型 chunk 不返回坐标**：实测 2026-09-30，文档
    「03-表结构与颗粒度说明-零售会员域.md」里从 markdown 表格切出的 chunk
    返回 `positions: []`，而同文档的正文 chunk 正常返回坐标。
    这里如实标注该退化，而不是吐一个 `position=[]`——空括号会被下游误读成
    "位置在第 0 页"，也可能原样出现在交付说明里。`chunk_id` 在任何情况下都是
    有效定位符（可用它回查该 chunk 全文）。
    """
    if positions:
        return "chunk=%s position=%s" % (chunk_id, positions)
    return "chunk=%s position=n/a(表格型chunk未返回坐标)" % chunk_id


def _parse_chunks(resp, max_snippet):
    """把 RAGFlow 检索响应解析成引用列表。"""
    data = resp.get("data") or {}
    chunks = data.get("chunks") or []
    doc_aggs = data.get("doc_aggs") or []

    citations = []
    for i, c in enumerate(chunks, 1):
        content = (c.get("content") or "").strip()
        pos = c.get("positions") or []
        citations.append(
            {
                "n": i,
                "document_id": c.get("document_id"),
                "document_name": _doc_name(c.get("document_id"), doc_aggs),
                "chunk_id": c.get("id"),
                "similarity": round(float(c.get("similarity") or 0), 4),
                "vector_similarity": round(float(c.get("vector_similarity") or 0), 4),
                "term_similarity": round(float(c.get("term_similarity") or 0), 4),
                "positions": pos,
                # 坐标是否可用：表格型 chunk 会退化为按 chunk_id 定位
                "position_available": bool(pos),
                "locator": _locator(c.get("id"), pos),
                "snippet": content[:max_snippet] + ("…" if len(content) > max_snippet else ""),
                "chars": len(content),
            }
        )
    return citations, doc_aggs, data.get("total")


def search(
    question,
    dataset_ids=None,
    top_k=DEFAULT_TOP_K,
    threshold=DEFAULT_THRESHOLD,
    vector_weight=DEFAULT_VECTOR_WEIGHT,
    max_snippet=SNIPPET_CHARS,
):
    """引用式检索：返回带来源文档名与片段定位的引用列表。

    零命中时自动去掉疑问词重试一次（依据见上方实测记录）：业务人员最常问的
    "XX 怎么算"恰好是关键词检索最不擅长的一类问句，不能让它空手而归。
    """
    question = (question or "").strip()
    if not question:
        raise KnowledgeError("question 不能为空")

    ids = dataset_ids or ([DATASET_ID] if DATASET_ID else [])
    if not ids:
        raise KnowledgeError("未指定 dataset_ids，且未配置 KNOWLEDGE_DATASET_ID")

    # 阈值必须 > 0：0 会被服务端按 falsy 回退默认值
    thr = max(float(threshold), 0.01)

    def _query(q):
        payload = {
            "question": q,
            "dataset_ids": ids,
            "top_k": max(int(top_k), 1),
            "page": 1,
            "page_size": max(int(top_k), 1),
            "similarity_threshold": thr,
            "vector_similarity_weight": float(vector_weight),
            "highlight": False,
        }
        t0 = time.time()
        resp = _post("/retrieval", payload)
        ms = int((time.time() - t0) * 1000)
        if resp.get("code") not in (0, None):
            raise KnowledgeError(
                "检索失败 code=%s：%s" % (resp.get("code"), resp.get("message"))
            )
        cites, aggs, total = _parse_chunks(resp, max_snippet)
        return payload, cites, aggs, total, ms

    payload, citations, doc_aggs, total, elapsed = _query(question)

    # 零命中回退：口语化问句会整体召回失败，去掉疑问词与虚词后再检一次
    fallback_query = None
    if not citations:
        kw = extract_keywords(question)
        if kw and kw != question:
            payload, citations, doc_aggs, total, elapsed = _query(kw)
            fallback_query = kw

    return {
        "ok": True,
        "query": question,
        "fallback_query": fallback_query,
        "dataset_ids": ids,
        "params": {
            "top_k": payload["top_k"],
            "similarity_threshold": thr,
            "vector_similarity_weight": payload["vector_similarity_weight"],
        },
        "total": total if total is not None else len(citations),
        "elapsed_ms": elapsed,
        "citations": citations,
        "doc_aggs": doc_aggs,
        "source_names": sorted({c["document_name"] for c in citations if c["document_name"]}),
        "answer_context": _context(citations),
    }


def _doc_name(doc_id, doc_aggs):
    for a in doc_aggs:
        if a.get("doc_id") == doc_id:
            return a.get("doc_name")
    return None


def _context(citations):
    """拼成可直接喂给 LLM 的带标号证据块（标号与 citations.n 对齐）。"""
    if not citations:
        return ""
    lines = []
    for c in citations:
        lines.append(
            "[%d] 来源：《%s》 %s\n%s"
            % (c["n"], c["document_name"] or c["document_id"], c["locator"], c["snippet"])
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 知识引用审计留痕（PRD §13 第③类）
# ---------------------------------------------------------------------------
def record_citations(demand_id, question, citations, round_no=None,
                     sql_run_id=None, dataset_id=None):
    """把一次检索命中的引用逐条写入 knowledge_citations。

    同问题重复检索会产生新行（审计事实，不做幂等）。
    单条 citation 缺字段存 NULL；不因单条坏数据丢整批。
    """
    import uuid as _uuid
    import db as _db
    import json as _json

    inserted = 0
    citation_ids = []
    if not citations:
        return {"ok": True, "inserted": 0, "citation_ids": []}
    for cite in (citations or []):
        if not isinstance(cite, dict):
            continue
        cid = "KC-" + _uuid.uuid4().hex[:12]
        did = cite.get("document_id")
        dname = cite.get("document_name")
        cid_chunk = cite.get("chunk_id")
        positions_raw = cite.get("positions")
        if positions_raw is None:
            positions_text = None
        elif isinstance(positions_raw, str):
            positions_text = positions_raw
        else:
            try:
                positions_text = _json.dumps(positions_raw, ensure_ascii=False)
            except Exception:
                positions_text = None
        try:
            _db.execute(
                """
                INSERT INTO knowledge_citations
                    (citation_id, demand_id, question, document_id, document_name,
                     chunk_id, positions, round_no, sql_run_id, dataset_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    cid,
                    demand_id,
                    str(question or "")[:2000],
                    (str(did)[:128] if did is not None else None),
                    (str(dname)[:256] if dname is not None else None),
                    (str(cid_chunk)[:128] if cid_chunk is not None else None),
                    positions_text,
                    (int(round_no) if round_no is not None else None),
                    (str(sql_run_id)[:64] if sql_run_id is not None else None),
                    (str(dataset_id)[:32] if dataset_id is not None else None),
                ),
            )
        except Exception:
            # 审计表宁可丢行也不中断上层检索流程
            continue
        inserted += 1
        citation_ids.append(cid)
    return {"ok": True, "inserted": inserted, "citation_ids": citation_ids}


def record_citations_once(demand_id, question, citations, stage, actor,
                          fingerprint, dataset_id=None, round_no=None, sql_run_id=None):
    """幂等版自动取证落表（P1-15）——**新增函数，不动 record_citations 契约**。

    为什么需要幂等
    --------------
    `sql_plan` / `evidence.collect` 是会被反复调用的动作（客户端 Agent 常把同一需求
    重跑好几轮，每次都会重新出一次 plan）。直接在它们里面调 record_citations，
    实测打桩后连跑 3 次 sqlgen.plan → 审计表 +12 行，线性膨胀且内容完全重复。

    幂等键 =（demand_id, stage, 内容指纹）。指纹由**调用侧**给出（本次 plan 的
    选表/时间字段/颗粒度 + 命中引用的 id 集合），内容变了才会再写一行；
    内容没变就直接跳过并返回 skipped="duplicate"。
    """
    import hashlib as _hashlib

    if not demand_id or not citations:
        return {"ok": True, "inserted": 0, "citation_ids": [], "skipped": "empty"}
    raw = str(fingerprint or "")
    fp = _hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12] if raw else "nofp"
    # marker 只含 [A-Za-z0-9=·. _-]，LIKE 通配安全
    marker = "AUTO_INTERNAL · stage=%s · fp=%s" % (stage, fp)
    try:
        import db as _db_once
        dup = _db_once.query_one(
            "SELECT citation_id FROM knowledge_citations "
            "WHERE demand_id=%s AND question LIKE %s LIMIT 1",
            (str(demand_id), "%" + marker + "%"),
        )
        if dup:
            return {"ok": True, "inserted": 0, "citation_ids": [], "skipped": "duplicate"}
    except Exception:
        # 查不到不代表能写；继续往下，由 record_citations 自己的异常兜底
        pass
    full_q = "%s · actor=%s · query=%s" % (marker, actor, str(question or "")[:120])
    res = record_citations(
        demand_id, full_q, citations,
        round_no=round_no, sql_run_id=sql_run_id,
        dataset_id=dataset_id or str(stage or "")[:32],
    )
    if isinstance(res, dict):
        res.setdefault("skipped", None)
    return res


def retire_citation(citation_id, reason):
    """把某条引用标记为已下架/已回退（PRD §13「何时被下架或回退」）。"""
    import db as _db
    try:
        rc = _db.execute(
            """
            UPDATE knowledge_citations
               SET retired_at = now(),
                   retired_reason = %s
             WHERE citation_id = %s
            """,
            (str(reason or "")[:1000], str(citation_id)),
        )
    except Exception:
        return {"ok": False, "updated": 0}
    updated = 0
    if isinstance(rc, int):
        updated = rc
    elif isinstance(rc, dict) and "rowcount" in rc:
        updated = int(rc["rowcount"])
    return {"ok": True, "updated": updated}
