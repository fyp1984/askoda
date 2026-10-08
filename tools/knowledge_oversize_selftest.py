#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传前「超长单元格」预检自证（离线，不需要网络与 RAGFlow）。

为什么单独做这个自证
--------------------
`scan_oversized_cells()` 拦的是一种**静默**故障：超长单元格送进 embedding 后，
服务不报错、不退出、健康检查还是绿的，只是不再干活，整份文档最后解析失败且
前端只显示「解析失败」。这类故障**外部几乎无法归因**，只能靠入库前拦下。

而这个函数一旦写错，有两种同样糟糕的失效方式：
  - **漏检**：超长内容放进去 → 服务被打死、文档解析失败（回到原点）；
  - **误杀**：正常文件被拦下 → 用户什么都传不了，比故障本身更糟。

所以本文件把两个方向都钉成断言：该拦的必须拦到并**报准位置**，
不该拦的（md / 旧版 xls / 正常长度）必须放行。

xlsx 用例全部由本文件**现场构造**（xlsx = zip + XML，标准库可写），
不依赖任何外部样例文件——自证必须能在干净环境里跑。

用法：python3 tools/knowledge_oversize_selftest.py
退出码 0 = 全过，1 = 有失败。
"""
import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateway"))
import knowledge as kb  # noqa: E402

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
%s
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="%s">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""" % PKG_REL_NS


def _sheet_override(idx):
    return ('<Override PartName="/xl/worksheets/sheet%d.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % idx)


def _workbook(sheet_names):
    sheets = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (n, i + 1, i + 1)
        for i, n in enumerate(sheet_names)
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
            % (MAIN_NS, REL_NS, sheets))


def _workbook_rels(count):
    rels = "".join(
        '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"'
        ' Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
        for i in range(count)
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="%s">%s</Relationships>' % (PKG_REL_NS, rels))


def _shared_strings(texts):
    items = "".join("<si><t>%s</t></si>" % t for t in texts)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="%s">%s</sst>' % (MAIN_NS, items))


def _sheet(cells):
    """cells: [(ref, kind, value)]；kind 为 s(共享字符串)/inline(内联)/num(数字)。"""
    rows = {}
    for ref, kind, value in cells:
        r_no = "".join(ch for ch in ref if ch.isdigit()) or "1"
        rows.setdefault(r_no, []).append((ref, kind, value))
    body = ""
    for r_no in sorted(rows, key=lambda x: int(x)):
        cs = ""
        for ref, kind, value in rows[r_no]:
            if kind == "s":
                cs += '<c r="%s" t="s"><v>%s</v></c>' % (ref, value)
            elif kind == "inline":
                cs += '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, value)
            else:
                cs += '<c r="%s"><v>%s</v></c>' % (ref, value)
        body += '<row r="%s">%s</row>' % (r_no, cs)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (MAIN_NS, body))


def make_xlsx(sheets):
    """sheets: [(sheet名, [(ref, kind, value), ...]), ...] → xlsx 字节串。

    文本统一走**共享字符串**（真实 Excel 的默认存法），value 填下标。
    """
    pool, expanded = [], []
    for name, cells in sheets:
        new_cells = []
        for ref, kind, value in cells:
            if kind == "s":
                pool.append(value)
                new_cells.append((ref, "s", str(len(pool) - 1)))
            else:
                new_cells.append((ref, kind, value))
        expanded.append((name, new_cells))
    parts = {
        "[Content_Types].xml": CONTENT_TYPES % "".join(_sheet_override(i + 1) for i in range(len(expanded))),
        "_rels/.rels": ROOT_RELS,
        "xl/workbook.xml": _workbook([n for n, _ in expanded]),
        "xl/_rels/workbook.xml.rels": _workbook_rels(len(expanded)),
    }
    if pool:
        parts["xl/sharedStrings.xml"] = _shared_strings(pool)
    for i, (_, cells) in enumerate(expanded):
        parts["xl/worksheets/sheet%d.xml" % (i + 1)] = _sheet(cells)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in parts.items():
            zf.writestr(path, data)
    return buf.getvalue()


FAILURES = []


def check(label, cond, hint=""):
    if cond:
        print("  ✅ %s" % label)
    else:
        print("  ❌ %s  %s" % (label, hint))
        FAILURES.append(label)


def main():
    limit = kb.MAX_CELL_CHARS
    long_text = "超" * (limit + 1)
    ok_text = "正" * limit          # 正好等于上限：不算超标
    normal = "正常单元格"

    print("阈值 MAX_CELL_CHARS = %d" % limit)

    # ---- 1. 正常文件必须放行 ----
    print("\n[1] 不该拦的必须放行")
    good = make_xlsx([("表1", [("A1", "s", normal), ("B1", "s", ok_text)])])
    r = kb.scan_oversized_cells("正常.xlsx", good)
    check("全短文本 xlsx 放行", r.get("ok") is True and r.get("checked") is True, str(r))

    r = kb.scan_oversized_cells("说明.md", "# 标题\n正常的 markdown 内容".encode("utf-8"))
    check("md 不做预检（放行）", r.get("ok") is True and r.get("checked") is False, str(r))

    r = kb.scan_oversized_cells("旧表.xls", b"\xd0\xcf\x11\xe0old binary")
    check("旧版 xls 解不开→放行（预检不阻断）", r.get("ok") is True and r.get("checked") is False, str(r))

    r = kb.scan_oversized_cells("数据.csv", "a,b,c\n1,2,3\n".encode("utf-8"))
    check("短字段 CSV 放行", r.get("ok") is True, str(r))

    # 边界：正好等于 limit 不报，limit+1 才报
    edge = make_xlsx([("表1", [("A1", "s", ok_text)])])
    check("长度正好等于阈值 → 不报（边界不误杀）",
          kb.scan_oversized_cells("边界.xlsx", edge).get("ok") is True)

    # ---- 2. 该拦的必须拦到，且位置要报准 ----
    print("\n[2] 该拦的必须拦到并报准位置")
    bad = make_xlsx([("字段分析", [("A1", "s", normal), ("D1234", "s", long_text)])])
    r = kb.scan_oversized_cells("超长.xlsx", bad)
    check("超长单元格被拦下", r.get("ok") is False, str(r))
    cells = r.get("cells") or []
    check("报出 1 处", len(cells) == 1, str(cells))
    check("位置含表名与坐标", cells and cells[0].get("location") == "字段分析!D1234", str(cells))
    check("字符数正确", cells and cells[0].get("chars") == limit + 1, str(cells))

    # 内联字符串分支（t="inlineStr"）不能漏
    inline = ('<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="%s"><sheetData>'
              '<row r="1"><c r="B2" t="inlineStr"><is><t>%s</t></is></c></row>'
              '</sheetData></worksheet>' % (MAIN_NS, long_text))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES % _sheet_override(1))
        zf.writestr("_rels/.rels", ROOT_RELS)
        zf.writestr("xl/workbook.xml", _workbook(["内联表"]))
        zf.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(1))
        zf.writestr("xl/worksheets/sheet1.xml", inline)
    r = kb.scan_oversized_cells("内联.xlsx", buf.getvalue())
    check("内联字符串(inlineStr)也能检出", r.get("ok") is False, str(r))

    # 数字单元格不该被当成超长文本
    num = make_xlsx([("数值表", [("C1", "num", "123456789")])])
    check("纯数字单元格不误报", kb.scan_oversized_cells("数值.xlsx", num).get("ok") is True)

    # 多 sheet 都要扫到
    multi = make_xlsx([
        ("表A", [("A1", "s", normal)]),
        ("表B", [("E5", "s", long_text)]),
        ("表C", [("F9", "s", long_text)]),
    ])
    r = kb.scan_oversized_cells("多表.xlsx", multi)
    locs = sorted(c.get("location") for c in (r.get("cells") or []))
    check("多个 sheet 都能扫到", locs == ["表B!E5", "表C!F9"], str(locs))

    # CSV 超长字段
    r = kb.scan_oversized_cells("数据.csv", ("a,%s,c\n" % long_text).encode("utf-8"))
    check("CSV 超长字段被拦下", r.get("ok") is False, str(r))
    check("CSV 位置报出行列", (r.get("cells") or [{}])[0].get("location") == "第1行第2列", str(r))

    # ---- 3. 报告条数要封顶，避免错误信息被撑爆 ----
    print("\n[3] 报告条数封顶")
    many = make_xlsx([("大表", [("A%d" % i, "s", long_text) for i in range(1, 60)])])
    r = kb.scan_oversized_cells("大表.xlsx", many)
    n = len(r.get("cells") or [])
    check("最多报 %d 处（实测 %d）" % (kb.MAX_CELL_REPORT, n),
          r.get("ok") is False and n == kb.MAX_CELL_REPORT, str(n))

    # ---- 4. 损坏文件不能让预检自己炸 ----
    print("\n[4] 异常输入不能让预检崩")
    r = kb.scan_oversized_cells("坏.xlsx", b"not a zip at all")
    check("非 zip 内容按「解析不了」放行", r.get("ok") is True, str(r))
    r = kb.scan_oversized_cells("空.csv", b"")
    check("空 CSV 放行", r.get("ok") is True, str(r))

    print("\n" + ("全部通过" if not FAILURES else "失败 %d 项：%s" % (len(FAILURES), FAILURES)))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
