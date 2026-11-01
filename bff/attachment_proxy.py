# -*- coding: utf-8 -*-
"""附件下载代理 · 纯函数层（无框架依赖，可离线自证）。

为什么需要「代理下载」而不是把预签名链接直接给浏览器
----------------------------------------------------
预签名链接里的 host 是**对象存储的内网地址**（compose 里的 `askoda-minio:9000`、
或本机 `127.0.0.1:19000`）。这类地址：

  - 浏览器所在机器解析不了容器服务名 ⇒ 直接点开是 `ERR_NAME_NOT_RESOLVED`；
  - 就算解析得了，签名 URL 的 Host 参与签名，替换 host 会 403 SignatureDoesNotMatch；
  - 端口也可能没对外暴露。

⇒ 正确做法：浏览器只跟 BFF 说话，BFF 在服务端取回字节流再转发。
   浏览器这一侧的 URL 永远是同源的 `/api/v1/attachments/...`，不存在跨域、
   不存在内网地址、不存在签名过期后点开报错（BFF 每次现取）。

三条安全边界（都在本文件里）
--------------------------
1. **对象键白名单式校验**：只接受 `a/b` 形式的相对键，拒绝协议、拒绝 `..`、
   拒绝反斜线、拒绝空段 ⇒ 这个接口永远不会变成"任意 URL 抓取器"。
2. **只按网关给的预签名地址取数**：BFF 不自己拼对象存储地址，避免出现第二套
   鉴权逻辑与第二处密钥。
3. **下载文件名走 RFC 5987**：中文名不乱码；同时给 ASCII 兜底名，
   老浏览器也不会拿到空文件名。
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import quote

#: 对象键最长（MinIO 限制 1024；这里收紧到 512，正常附件键远小于此）
KEY_MAX = 512
#: 单次代理下载上限（默认 200MB）。超限直接拒绝，避免一次点下载把 BFF 内存打满。
SIZE_MAX = 200 * 1024 * 1024

_SAFE_SEG = re.compile(r"^[^/\\]{1,255}$")


def safe_object_key(raw):
    """校验并归一化对象键；不合法返回空串（**不抛异常**，由调用方给 400）。

    合法形态：`DR-20261021-XXXX/ab12cd34ef56_file.xlsx`（相对键、单斜杠分段）。
    拒绝：带协议（`http://`）、带 `..`、带反斜线、绝对路径、超长、空段。
    """
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s or len(s) > KEY_MAX:
        return ""
    if "://" in s or s.startswith("/") or "\\" in s:
        return ""
    if any(ch in s for ch in ("\x00", "\n", "\r", "\t")):
        return ""
    # 逐段校验：任何一段为空 / 为 . / 为 .. ⇒ 拒绝
    segs = s.split("/")
    for seg in segs:
        if seg in ("", ".", ".."):
            return ""
        if not _SAFE_SEG.match(seg):
            return ""
    norm = posixpath.normpath(s)
    if norm != s or norm.startswith(".."):
        return ""
    return s


def filename_of(object_key):
    """从对象键里还原「人类可读文件名」。

    对象键形如 `{demand_id}/{12位hex}_{原文件名}`（见 gateway/attachments.py 的 `put`），
    下载时要把那 12 位 hex 前缀去掉，否则用户下到的文件名带着一串随机码。
    """
    base = posixpath.basename(object_key or "")
    if not base:
        return "attachment"
    head, sep, tail = base.partition("_")
    if sep and len(head) == 12 and all(c in "0123456789abcdef" for c in head.lower()):
        base = tail or base
    return base or "attachment"


def content_disposition(filename):
    """生成 `Content-Disposition`：ASCII 兜底 + RFC 5987 中文名。

    ⚠️ 两个都要给：只给 `filename*=UTF-8''...` 时，个别老客户端会拿到空文件名；
       只给 ASCII 兜底时中文名会变成乱码。
    """
    name = (filename or "attachment").strip() or "attachment"
    # ASCII 兜底：非 ASCII 与引号/路径分隔符一律替换，避免破坏头语法
    ascii_name = "".join(
        ch if (32 <= ord(ch) < 127 and ch not in '"\\/') else "_" for ch in name
    ).strip() or "attachment"
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name,
        quote(name, safe=""),
    )


def guess_media_type(filename):
    """按扩展名给 Content-Type；未知类型统一二进制流（浏览器会存盘，不会乱渲染）。"""
    import mimetypes

    ext = posixpath.splitext(filename or "")[1].lower()
    table = {
        ".csv": "text/csv; charset=utf-8",
        ".tsv": "text/tab-separated-values; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    if ext in table:
        return table[ext]
    guessed, _ = mimetypes.guess_type("x" + ext)
    return guessed or "application/octet-stream"


def download_url(api_prefix, object_key):
    """给前端的同源下载地址（浏览器只跟 BFF 说话）。"""
    key = safe_object_key(object_key)
    if not key:
        return ""
    return "%s/attachments/%s" % (api_prefix.rstrip("/"), quote(key, safe="/"))
