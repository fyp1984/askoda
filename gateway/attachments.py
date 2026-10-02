# -*- coding: utf-8 -*-
"""附件接入（M2 · MinIO）

职责
----
需求单附件的上传 / 检查 / 下载。对应 PRD §9.2 的「附件与表样」与
「附件中的高风险信息给出提示，要求提交人确认是否允许继续上传」。

设计要点
--------
1. **对象键与需求单绑定**：`{demand_id}/{uuid}_{filename}`，附件随单可溯。
2. **高风险附件识别**：文本类附件（csv / txt / md / json）上传时即做敏感扫描，
   命中则在返回里标 `risk`，并写入需求单事件，由提交人确认后继续流转。
3. **二元制附件**（xlsx / pdf / png 等）不做内容解析，仅登记元数据 +
   提示"请确认是否含敏感信息"，避免为解析引入重型依赖（PRD §9.2 第 2 条）。
4. 预留 `presigned_url` 能力，供后续前端直连下载，不必经网关转发字节流。
"""
import datetime as dt
import hashlib
import os
import uuid

import masking

try:
    from minio import Minio
    from minio.error import S3Error
except ImportError:
    Minio = None
    S3Error = Exception

ENDPOINT = os.getenv("MINIO_ENDPOINT", "127.0.0.1:19000")
ACCESS_KEY = os.getenv("MINIO_ACCESS_KEY", "assistant")
SECRET_KEY = os.getenv("MINIO_SECRET_KEY", "assistant123")
BUCKET = os.getenv("MINIO_BUCKET", "demand-attachments")
SECURE = os.getenv("MINIO_SECURE", "false").lower() == "true"

# 可做内容级敏感扫描的文本类附件
TEXT_EXT = {".csv", ".tsv", ".txt", ".md", ".json"}
MAX_SCAN_BYTES = 512 * 1024

_client = None


class AttachmentError(RuntimeError):
    pass


def client():
    global _client
    if Minio is None:
        raise AttachmentError("未安装 minio（容器内应随 requirements.txt 安装）")
    if _client is None:
        _client = Minio(ENDPOINT, access_key=ACCESS_KEY, secret_key=SECRET_KEY, secure=SECURE)
    return _client


def ensure_bucket():
    c = client()
    if not c.bucket_exists(BUCKET):
        c.make_bucket(BUCKET)
    return BUCKET


def health():
    try:
        c = client()
        exists = c.bucket_exists(BUCKET)
        return {"ok": True, "endpoint": ENDPOINT, "bucket": BUCKET, "bucket_exists": exists}
    except Exception as e:
        return {
            "ok": False,
            "endpoint": ENDPOINT,
            "bucket": BUCKET,
            "error": "%s: %s" % (type(e).__name__, str(e)[:200]),
        }


def _ext(filename):
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def put(demand_id, filename, data, content_type=None):
    """上传附件。data 为 bytes。返回附件元数据（存进需求单的 attachments 数组）。

    文本类附件会附加 `risk` 字段，列出命中的敏感信息类型——按 PRD §9.2，
    这种情况需要提交人确认是否允许继续上传。
    """
    if not filename:
        raise AttachmentError("filename 不能为空")
    if not data:
        raise AttachmentError("附件内容为空")

    ensure_bucket()
    ext = _ext(filename)
    object_key = "%s/%s_%s" % (demand_id, uuid.uuid4().hex[:12], filename)
    ctype = content_type or _guess_type(ext)

    try:
        from io import BytesIO

        client().put_object(
            BUCKET, object_key, BytesIO(data), length=len(data), content_type=ctype
        )
    except Exception as e:
        raise AttachmentError("上传失败：%s: %s" % (type(e).__name__, str(e)[:200]))

    meta = {
        "filename": filename,
        "object_key": object_key,
        "bucket": BUCKET,
        "size": len(data),
        "content_type": ctype,
        "sha256": hashlib.sha256(data).hexdigest()[:32],
        "ext": ext,
        "recognizable": ext in {
            ".xlsx", ".xls", ".csv", ".tsv", ".pdf", ".docx", ".doc",
            ".txt", ".md", ".png", ".jpg", ".jpeg", ".json",
        },
        "uploaded_at": dt.datetime.now().isoformat(timespec="seconds"),
    }

    # 文本类附件做内容级敏感扫描
    if ext in TEXT_EXT:
        try:
            text = data[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
            hits, summary = masking.scan(text)
            if hits:
                meta["risk"] = {
                    "level": "high",
                    "types": summary,
                    "message": "附件文本中识别到敏感信息（%s），请确认是否允许继续上传。"
                    % "、".join("%s×%d" % (k, v) for k, v in summary.items()),
                    "sample": [h["preview"] for h in hits[:5]],
                }
        except Exception:
            pass
    elif not meta["recognizable"]:
        meta["risk"] = {
            "level": "unknown",
            "types": {},
            "message": "该附件格式无法自动解析，无法判断是否含敏感信息；请在需求说明中补充文字描述。",
        }

    return meta


def get(object_key):
    """下载附件字节流。返回 (bytes, content_type)。"""
    try:
        resp = client().get_object(BUCKET, object_key)
        try:
            data = resp.read()
        finally:
            resp.close()
            resp.release_conn()
        return data, (resp.headers.get("Content-Type") if resp.headers else None)
    except Exception as e:
        raise AttachmentError("下载失败：%s: %s" % (type(e).__name__, str(e)[:200]))


def presigned(object_key, expires_seconds=3600):
    """生成临时下载链接，供前端直连（不经网关转发字节流）。"""
    try:
        return client().presigned_get_object(BUCKET, object_key, expires=dt.timedelta(seconds=expires_seconds))
    except Exception as e:
        raise AttachmentError("生成链接失败：%s: %s" % (type(e).__name__, str(e)[:200]))


def list_objects(prefix=""):
    try:
        objs = client().list_objects(BUCKET, prefix=prefix, recursive=True)
        return [
            {
                "object_key": o.object_name,
                "size": o.size,
                "last_modified": o.last_modified.isoformat() if o.last_modified else None,
            }
            for o in objs
        ]
    except Exception as e:
        raise AttachmentError("列举失败：%s: %s" % (type(e).__name__, str(e)[:200]))


def _guess_type(ext):
    return {
        ".csv": "text/csv",
        ".tsv": "text/tab-separated-values",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xls": "application/vnd.ms-excel",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(ext, "application/octet-stream")
