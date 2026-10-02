#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M6-1 返工第二轮 · 版本口径独立核对（验收方自建，非交付方脚本）

目的：
  1) 独立复算每条规则 evaluate 的源码指纹 source_sha8，与交付方自述逐个对照；
  2) 打印 rules_version（其值随 REGISTRY 内容变化 —— 本脚本只核一致性与可复算，不写死具体值）；
  3) 用真实需求单复算 A/B 的 pack_version 是否满足
     pack_version == sha8(schema_version + requirement_version + rules_version)。

运行（容器内，需 DB 中有需求单）：
    docker exec -e PYTHONPATH=/app askoda python /tmp/m61r2_version_check.py
"""
import hashlib
import inspect

import rules
import sqlpack


def sha8(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


print("=== 每条规则 evaluate 源码指纹（独立复算 sha256(inspect.getsource)[:8]）===")
for r in rules.REGISTRY:
    fn = r.get("evaluate")
    try:
        h = sha8(inspect.getsource(fn))
    except Exception as e:  # noqa: BLE001
        h = "ERR:%s" % e
    print("  %-26s %s" % (r["id"], h))

print()
print("rules_version =", sqlpack._rules_version())

print()
print("=== pack_version 组合关系复算（真实需求单）===")
import db  # noqa: E402

for ds in ("A", "B"):
    row = db.query_one(
        "SELECT demand_id FROM structured_requirements WHERE dataset = '%s' "
        "ORDER BY created_at DESC LIMIT 1" % ds)
    if not row:
        print("  dataset=%s 无需求单，跳过" % ds)
        continue
    did = row["demand_id"]
    p = sqlpack.build(did, dataset=ds)
    h = hashlib.sha256()
    for x in (p.get("schema_version"), str(p.get("requirement_version")),
              p.get("rules_version")):
        h.update(str(x if x is not None else "").encode("utf-8"))
    ok = p.get("pack_version") == h.hexdigest()[:8]
    print("  dataset=%s demand=%s pack_version=%s 期望=%s 组合关系=%s "
          "rules_version=%s schema_version=%s"
          % (ds, did, p.get("pack_version"), h.hexdigest()[:8],
             "OK" if ok else "MISMATCH", p.get("rules_version"), p.get("schema_version")))
