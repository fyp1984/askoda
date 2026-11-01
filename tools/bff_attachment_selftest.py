#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件代理下载（bff/attachment_proxy.py）离线自证。

为什么要有这条判据
------------------
这个接口把「用户可控的字符串」拼进下载地址，又由服务端去取字节流——
一旦对象键校验写松了，它就从「附件下载」变成「服务端任意取 URL」。
而这类漏洞**不会**被门禁的功能测试发现：功能上它确实能把文件下下来。

⇒ 判据必须专门盯**拒绝面**（该拒的都拒了），而不是只测能下下来。

测什么
------
1. 对象键校验：合法键通过；协议 / `..` / 绝对路径 / 反斜线 / 空段 / 控制字符 /
   超长 一律拒绝；
2. 文件名还原：去掉 `{12位hex}_` 前缀；前缀不是 hex 时原样保留；空键给兜底名；
3. 下载头：`Content-Disposition` 必须同时有 ASCII 兜底与 RFC 5987 中文名；
4. 类型推断：已知扩展名给准确 MIME；未知给 `application/octet-stream`；
5. 下载地址：同源、分段百分号编码、非法键返回空串；
6. 往返一致：`download_url` 产出的地址经 URL 解码后能被 `safe_object_key` 认回原键；
7. 上限常量为正（防止有人写成 0 导致所有下载被拒）。

用法：python3 tools/bff_attachment_selftest.py   （退出码 0=全过）
"""
import os
import sys
from urllib.parse import unquote

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO, "bff"))

import attachment_proxy as A  # noqa: E402

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


GOOD = "DR-20261021-A1AL/ab12cd34ef56_会员复购明细.xlsx"


@case("对象键：合法键原样通过")
def _t_key_ok():
    assert A.safe_object_key(GOOD) == GOOD, A.safe_object_key(GOOD)
    assert A.safe_object_key("a/b/c.txt") == "a/b/c.txt"
    assert A.safe_object_key("  a/b.txt  ") == "a/b.txt", "两端空白应被裁掉"


@case("对象键：协议 / 路径回退 / 绝对路径 / 反斜线 一律拒绝")
def _t_key_reject():
    bad = [
        "http://evil/x", "https://evil/x", "file:///etc/passwd",
        "../etc/passwd", "a/../../b", "a/./b", "./a", "a/..",
        "/etc/passwd", "\\\\server\\share\\x", "a\\b",
        "", "   ", "a//b", "a/", "/a",
    ]
    for k in bad:
        got = A.safe_object_key(k)
        assert got == "", "该拒绝却通过了：%r ⇒ %r" % (k, got)


@case("对象键：控制字符与超长拒绝")
def _t_key_limits():
    for k in ("a/b\nX", "a/b\rX", "a/b\x00X", "a/b\tX"):
        assert A.safe_object_key(k) == "", "控制字符应拒绝：%r" % k
    assert A.safe_object_key("a/" + "x" * A.KEY_MAX) == "", "超长应拒绝"
    assert A.safe_object_key("a/" + "x" * 10) != "", "正常长度不该被误杀"
    assert A.safe_object_key(None) == "", "非字符串应拒绝"


@case("文件名：去掉 12 位 hex 前缀；非 hex 前缀保留原名")
def _t_filename():
    assert A.filename_of(GOOD) == "会员复购明细.xlsx", A.filename_of(GOOD)
    assert A.filename_of("d/a1b2c3d4e5f6_x.csv") == "x.csv"
    # 前缀不是 12 位 hex ⇒ 不是我们加的随机码，不能擅自砍掉
    assert A.filename_of("d/2026_年度报表.csv") == "2026_年度报表.csv", \
        A.filename_of("d/2026_年度报表.csv")
    assert A.filename_of("") == "attachment"
    assert A.filename_of("d/") == "attachment"


@case("下载头：ASCII 兜底 + RFC 5987 中文名，两者都要有")
def _t_disposition():
    cd = A.content_disposition("会员复购明细.xlsx")
    assert cd.startswith("attachment;"), cd
    assert 'filename="' in cd, "缺 ASCII 兜底名：%s" % cd
    assert "filename*=UTF-8''" in cd, "缺 RFC 5987 中文名：%s" % cd
    ascii_part = cd.split('filename="', 1)[1].split('"', 1)[0]
    assert all(32 <= ord(c) < 127 for c in ascii_part), "ASCII 兜底名含非 ASCII：%r" % ascii_part
    assert "%" in cd.split("filename*=UTF-8''", 1)[1], "中文名应被百分号编码"
    # 引号不能让头语法断掉
    cd2 = A.content_disposition('a"b\\c.xlsx')
    assert cd2.count('"') == 2, "引号未转义：%s" % cd2


@case("类型推断：已知扩展名准确，未知给二进制流")
def _t_media_type():
    assert A.guess_media_type("a.xlsx").endswith("spreadsheetml.sheet")
    assert A.guess_media_type("a.CSV").startswith("text/csv"), "扩展名应忽略大小写"
    assert A.guess_media_type("a.weird") == "application/octet-stream"
    assert A.guess_media_type("") == "application/octet-stream"


@case("下载地址：同源 + 分段编码 + 非法键返回空串")
def _t_download_url():
    url = A.download_url("/api/v1", GOOD)
    assert url.startswith("/api/v1/attachments/"), url
    assert "://" not in url, "不能拼出绝对地址：%s" % url
    assert "会员" not in url, "中文应被编码：%s" % url
    assert url.count("/") >= 5, url
    assert A.download_url("/api/v1", "../../etc/passwd") == "", "非法键不该产出地址"


@case("往返一致：地址解码后能认回原键（含中文文件名）")
def _t_roundtrip():
    url = A.download_url("/api/v1", GOOD)
    tail = url.split("/api/v1/attachments/", 1)[1]
    back = unquote(tail)
    assert back == GOOD, "往返丢信息：%r != %r" % (back, GOOD)
    assert A.safe_object_key(back) == GOOD


@case("上限常量：必须为正，且不是小到离谱")
def _t_limits():
    assert A.SIZE_MAX > 1024 * 1024, "下载上限过小会误伤正常附件：%d" % A.SIZE_MAX
    assert A.KEY_MAX > 0


def main():
    ok = 0
    fail = []
    for name, fn in CASES:
        try:
            fn()
            ok += 1
            print("  [OK] %s" % name)
        except AssertionError as e:
            fail.append((name, str(e)))
            print("  [FAIL] %s\n         %s" % (name, e))
        except Exception as e:  # noqa: BLE001
            fail.append((name, "%s: %s" % (type(e).__name__, e)))
            print("  [ERROR] %s\n         %s: %s" % (name, type(e).__name__, e))
    print()
    print("bff_attachment_selftest: %d/%d 通过" % (ok, len(CASES)))
    if fail:
        print("失败项：")
        for n, m in fail:
            print("  - %s：%s" % (n, m))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
