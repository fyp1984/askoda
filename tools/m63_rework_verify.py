# -*- coding: utf-8 -*-
"""M6-3 返工复验（验收方独立资产 · 容器内运行）。

覆盖《M6-3验收单与返工任务》§六 的返工验收口径 ① ② ③：
  ① WREN_B_TIMEOUT=0.002 连跑 10 次（真实端点）→ 10/10 全 WrenTimeout 且 attempts==READ_RETRIES
  ② read 阶段打桩（open 成功 / read 抛 socket.timeout）→ WrenTimeout 且 attempts==READ_RETRIES
     （返工前应 FAIL、返工后应 PASS —— 本条是该缺陷的直接回归）
  ③ 环境变量超时链 A3 / A4

运行：
  docker cp tools/m63_rework_verify.py askoda:/app/tools/m63_rework_verify.py
  docker exec -e PYTHONPATH=/app -e WREN_B_TIMEOUT=0.002 \
      askoda python /app/tools/m63_rework_verify.py

退出码 0 = 全过，1 = 有失败。
"""
import os
import socket
import sys
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GW = os.path.join(ROOT, "gateway")
if GW not in sys.path:
    sys.path.insert(0, GW)

os.environ.setdefault("MDL_A_PATH", os.path.join(ROOT, "wren-docker", "workspace", "mdl.json"))
os.environ.setdefault("MDL_B_PATH", os.path.join(ROOT, "wren-docker-b", "workspace", "mdl.json"))

import registry  # noqa: E402
from wren import WrenClient, WrenError, WrenTimeout, READ_RETRIES  # noqa: E402

FAIL = []
PROBE_URL = "http://127.0.0.1:59999"  # 只作构造用，全部打桩，不发真实请求


def expect(name, cond, detail=""):
    if cond:
        print("  [OK]", name)
    else:
        msg = "  [FAIL] %s %s" % (name, ("| " + detail) if detail else "")
        print(msg)
        FAIL.append(msg)


def classify(client, sql="SELECT 1"):
    """调一次 query，返回 (类型串, last_attempts, message)。

    类型串取值：WrenTimeout / BareRawError:<T> / OTHER:<T> / NO-ERROR
    BareRawError 专指「本应被规范化却裸抛」的 socket.timeout / urllib URLError。
    """
    try:
        client.query(sql)
        return ("NO-ERROR", getattr(client, "last_attempts", -1), "")
    except WrenTimeout as e:
        return ("WrenTimeout", getattr(client, "last_attempts", -1), str(e))
    except (socket.timeout, urllib.error.URLError) as e:
        return ("BareRawError:" + type(e).__name__, getattr(client, "last_attempts", -1), str(e)[:200])
    except Exception as e:  # noqa: BLE001
        return ("OTHER:" + type(e).__name__, getattr(client, "last_attempts", -1), str(e)[:200])


def part1_read_stub():
    print("\n[1] read 阶段打桩：open 成功、read 抛 socket.timeout")

    class FakeResp:
        headers = {"Mcp-Session-Id": "fake-sid"}

        def read(self):
            raise socket.timeout("timed out (simulated on read)")

    class FakeOpener:
        def open(self, req, timeout=None):
            return FakeResp()

    c = WrenClient(PROBE_URL, name="B", timeout=5)
    c._opener = FakeOpener()
    t, attempts, msg = classify(c)
    print("    type=%s  attempts=%d  msg=%s" % (t, attempts, msg[:140]))
    expect("1.1 read 阶段超时被规范化为 WrenTimeout（非裸 TimeoutError）",
           t == "WrenTimeout", "实际=%s" % t)
    expect("1.2 读侧重试真的生效：attempts == READ_RETRIES (%d)" % READ_RETRIES,
           attempts == READ_RETRIES, "attempts=%d" % attempts)
    expect("1.3 message 与 open 阶段同构（含 tool= / timeout=）",
           ("tool=" in msg and "timeout=" in msg), msg[:140])


def part2_open_regression():
    print("\n[2] open 阶段超时（回归项，不得退化）")

    class BoomOpener:
        def open(self, req, timeout=None):
            raise socket.timeout("connect timed out")

    c = WrenClient(PROBE_URL, name="B", timeout=5)
    c._opener = BoomOpener()
    t, attempts, msg = classify(c)
    print("    type=%s  attempts=%d" % (t, attempts))
    expect("2.1 open 阶段超时仍为 WrenTimeout", t == "WrenTimeout", "实际=%s" % t)
    expect("2.2 open 阶段重试次数不变（%d）" % READ_RETRIES, attempts == READ_RETRIES, "attempts=%d" % attempts)


def part3_env_chain():
    print("\n[3] 环境变量超时链 A3 / A4")
    saved = {k: os.environ.get(k) for k in ("WREN_TIMEOUT", "WREN_B_TIMEOUT")}
    try:
        os.environ.pop("WREN_B_TIMEOUT", None)
        os.environ["WREN_TIMEOUT"] = "0.003"
        c = WrenClient(PROBE_URL, name="B")
        expect("A3 只设 WREN_TIMEOUT → name=B 取 0.003",
               abs(c.timeout - 0.003) < 1e-9, "实际=%s" % c.timeout)

        os.environ["WREN_B_TIMEOUT"] = "0.007"
        cb = WrenClient(PROBE_URL, name="B")
        ca = WrenClient(PROBE_URL, name="A")
        expect("A4 专用键优先 → name=B 取 0.007",
               abs(cb.timeout - 0.007) < 1e-9, "实际=%s" % cb.timeout)
        expect("A4 通用键回落 → name=A 取 0.003",
               abs(ca.timeout - 0.003) < 1e-9, "实际=%s" % ca.timeout)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def part4_live_10():
    print("\n[4] 真实端点连跑 10 次")
    env_to = os.environ.get("WREN_B_TIMEOUT")
    print("    WREN_B_TIMEOUT =", env_to)
    if not env_to:
        expect("4.0 WREN_B_TIMEOUT 已设置（否则测不出超时）", False, "未设置")
        return
    ds = registry.get("B")
    print("    B wren_url =", ds.wren_url)
    results = []
    for i in range(1, 11):
        c = WrenClient(ds.wren_url, name="B")
        t, attempts, _ = classify(c)
        results.append((t, attempts))
        ok = (t == "WrenTimeout" and attempts == READ_RETRIES)
        print("    #%02d [%s] type=%-26s attempts=%d"
              % (i, "OK " if ok else "BAD", t, attempts))
    bad = [r for r in results if r[0] != "WrenTimeout"]
    bad_att = [r for r in results if r[1] != READ_RETRIES]
    expect("4.1 10/10 全为 WrenTimeout（不再有裸 TimeoutError）",
           len(bad) == 0, "非 WrenTimeout 次数=%d → %s" % (len(bad), [r[0] for r in bad]))
    expect("4.2 10/10 attempts 均 == %d（读侧重试未被绕过）" % READ_RETRIES,
           len(bad_att) == 0, "异常 attempts=%s" % [r[1] for r in bad_att])


def main():
    print("=" * 68)
    print("M6-3 返工复验（容器内）")
    print("  wren.py =", os.path.join(GW, "wren.py"))
    print("  READ_RETRIES =", READ_RETRIES)
    print("  socket.timeout is TimeoutError =", socket.timeout is TimeoutError)
    print("=" * 68)
    part1_read_stub()
    part2_open_regression()
    part3_env_chain()
    part4_live_10()
    print("\n==================== 总览 ====================")
    if not FAIL:
        print("全部通过")
        return 0
    print("%d 条失败：" % len(FAIL))
    for f in FAIL:
        print(" ", f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
