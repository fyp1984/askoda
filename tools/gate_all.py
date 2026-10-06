#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Askoda 统一门禁脚本（G0-G3 四层收敛）。

一条命令跑完 35 个自检/复核/验真脚本，输出 JSON + Markdown 双份报告。
退出码：0 = 全绿；1 = 任一红项。

用法：
    python tools/gate_all.py                 # 跑全部四层
    python tools/gate_all.py --layers G0,G1  # 只跑指定层
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ALLOWED_REQ_BASELINES = {
    "fastmcp==4.0.10",
    "starlette",
    "psycopg[binary]",
    "minio",
    "sqlglot",
}

SINGLE_TIMEOUT = 300

LAYER_ORDER = ["G0", "G1", "G2", "G3"]

LAYER_DESC = {
    "G0": "静态自检（语法 + 依赖基线）",
    "G1": "单元自证（*_selftest.py）",
    "G2": "独立复核（*_check.py）",
    "G3": "真实链路（探活 + *_verify.py）",
}

TAIL_LINES = 40

# 个别脚本的**前置环境变量**——脚本 docstring 里写明的运行姿势。
# 例：某脚本需 `WREN_B_TIMEOUT=0.002`，否则"测不出超时链路"
#（那条断言本身会先 FAIL）。按需只为该脚本注入，不污染其它脚本的超时行为。
PER_SCRIPT_ENV = {
}

# 已知基线红：**如实报红，但不阻塞准入**。
# 登记条件（缺一不可）：① 现象已定位到具体根因；② 有明确的修复批次；
# ③ 与本批改动无关。修好后必须从本表移除。
#   · reason = 为什么红 / 谁来修
#   · match  = 失败指纹（必须出现在 fail_hint 或输出尾部才认，防止把
#              该脚本将来**别的**失败也一并赦免）
# 加 --strict 可忽略本表（所有红项一律阻塞，用于总验收）。
KNOWN_RED_BASELINE = {
}
# 历史沿革（本表当前为空，说明所有曾登记的已知红项都已真修好）：
#
# · ("G2", "rules_rework_check.py") —— B2c 已移除。R1 梯次(1) 细化为
#   「方向 + 聚合列归属」两级判据，只聚合多端列不再误报，退出码转 0。
#
# · ("G3", "m2_verify.py") —— **B4b 已移除**（原指纹「可召回且引用可追溯」）。
#   当时红因是 embedding 上游断供：RAGFlow 的 `tenant_model_instance.extra.base_url`
#   写死远端 TEI `100.103.240.78:18001`（已宕），而 RAGFlow 每次检索都现调
#   embedding、不缓存查询向量，于是 knowledge_search 全量
#   `EmbeddingError('Connection error.')`、命中恒为 0。
#   修法见 `.models/README.md`：本地起 bge-m3 TEI（compose 服务 `tei-embedding`），
#   把 base_url 改为 host.docker.internal:18002。
#   规则是「只登记已知待修，修好必须移除」，故此处不再保留。
#
# 注：上述两个里程碑验收脚本已随过程产物清理从仓库删除，此处仅留决策沿革备查。
#
# 留着的危害：known-red 长期挂账，会把真实红项一并赦免。


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _repo_rel(path):
    """把绝对路径收缩成相对仓库根的展示路径。"""
    ap = os.path.abspath(path)
    if ap.startswith(REPO_ROOT):
        return ap[len(REPO_ROOT) + 1:]
    return ap


def _last_lines(stdout_b, stderr_b, n=TAIL_LINES):
    """合并 stdout + stderr，取末尾 n 行，返回字符串。"""
    parts = []
    for tag, b in (("STDOUT", stdout_b), ("STDERR", stderr_b)):
        if not b:
            continue
        txt = b.decode("utf-8", errors="replace").rstrip()
        if not txt:
            continue
        lines = txt.splitlines()
        if len(lines) > n:
            lines = ["...（前略 %d 行）..." % (len(lines) - n)] + lines[-n:]
        parts.append("---- %s ----\n%s" % (tag, "\n".join(lines)))
    return "\n".join(parts) if parts else "(无输出)"


def _locate_fail_line(tail_text):
    """在末尾输出里搜寻断言失败行，返回首个命中行，找不到返回空串。"""
    keywords = (
        "[FAIL]", "FAIL", "AssertionError", "Error:", "Traceback",
        "FAIL]", "失败", "❌", "不通过",
    )
    for line in tail_text.splitlines():
        sl = line.strip()
        if not sl:
            continue
        for kw in keywords:
            if kw in sl:
                # 去掉可能的颜色控制字符
                clean = re.sub(r"\x1b\[[0-9;]*m", "", sl)
                if len(clean) > 120:
                    clean = clean[:120] + "..."
                return clean
    return ""


def _timeout_for(abs_script_path):
    """统一返回单脚本超时上限（秒）。

    这里**刻意不做**「探针类用短超时」的分层——B0 首轮实测证明那样判是错的：
    *_verify.py 是真实端到端脚本，环境正常时单条就要跑几十秒到几分钟，
    25s 的短上限会把「跑得慢」误判成「红」（实测 a_verify / m2_verify /
    m3_verify / m4_verify / m5_verify 五条全部 TIMEOUT@25.03s，全是假红）。

    真实链路是否要跑，交给 G3 前置探活判断（探活不过 → verify 全部 SKIP，
    不会出现「9 个脚本各自等满超时」）。想快速过一遍用 --layers 选层，
    或 --timeout 调小上限。
    """
    return SINGLE_TIMEOUT


GATEWAY_DIR = os.path.join(REPO_ROOT, "gateway")
DEFAULT_MCP_BASE = "http://127.0.0.1:18080"

# --- 子进程解释器选择 -------------------------------------------------------
# 为什么需要这一层（B0 首轮实测教训）：
#   gateway/ 下的模块依赖第三方包（sqlglot / psycopg），而 PATH 上的 python3
#   往往是「干净」的托管解释器，缺这些包 → 4 个 selftest（gates / rules /
#   sqlgen / sqlpack）与 5 个 check 会以 ModuleNotFoundError 报红。
#   看起来像脚本坏了，实际是**用错了解释器**——门禁必须自己把这件事兜住，
#   否则「一条命令 + 一个退出码」的承诺不成立。
REQUIRED_MODULES = ("sqlglot", "psycopg")

CHILD_PYTHON = sys.executable
CHILD_PYTHON_INFO = {
    "path": sys.executable,
    "version": "",
    "missing": list(REQUIRED_MODULES),
    "reason": "尚未选择（默认 sys.executable）",
}


def _python_candidates():
    """按优先级给出候选解释器：VIRTUAL_ENV → 项目 venv → 宿主 venv → 系统 python。"""
    cands = []
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        cands.append(os.path.join(venv, "bin", "python"))
    cands.append(os.path.join(REPO_ROOT, ".venv", "bin", "python"))
    cands.append(os.path.join(REPO_ROOT, "venv", "bin", "python"))
    cands.append(os.path.expanduser("~/.workbuddy/binaries/python/envs/default/bin/python"))
    cands.append("/usr/bin/python3")
    cands.append(sys.executable)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _probe_python(path):
    """探测解释器是否具备必需依赖，返回 (ok, version, missing 列表)。"""
    if not path or not os.path.exists(path):
        return False, "", list(REQUIRED_MODULES)
    code = (
        "import sys, importlib.util\n"
        "miss = [m for m in %r if importlib.util.find_spec(m) is None]\n"
        "print(sys.version.split()[0])\n"
        "print('|'.join(miss))\n" % (REQUIRED_MODULES,)
    )
    try:
        p = subprocess.run([path, "-c", code], capture_output=True, timeout=30)
    except Exception:  # noqa: BLE001
        return False, "", list(REQUIRED_MODULES)
    if p.returncode != 0:
        return False, "", list(REQUIRED_MODULES)
    lines = p.stdout.decode("utf-8", "replace").splitlines()
    ver = lines[0].strip() if lines else ""
    miss = [x for x in (lines[1].split("|") if len(lines) > 1 else []) if x]
    return (not miss), ver, miss


def _pick_python(explicit=None):
    """选定子进程解释器。--python 显式指定时不做替换；否则自动探测首个可用者。"""
    global CHILD_PYTHON, CHILD_PYTHON_INFO
    if explicit:
        ok, ver, miss = _probe_python(explicit)
        info = {
            "path": explicit, "version": ver, "missing": miss,
            "reason": "由 --python 显式指定",
        }
    else:
        info = None
        for cand in _python_candidates():
            ok, ver, miss = _probe_python(cand)
            if ok:
                info = {
                    "path": cand, "version": ver, "missing": [],
                    "reason": "自动探测：首个具备 %s 的解释器"
                              % "/".join(REQUIRED_MODULES),
                }
                break
        if info is None:
            ok, ver, miss = _probe_python(sys.executable)
            info = {
                "path": sys.executable, "version": ver,
                "missing": miss or list(REQUIRED_MODULES),
                "reason": "未找到具备必需依赖的解释器，回退 sys.executable",
            }
    CHILD_PYTHON, CHILD_PYTHON_INFO = info["path"], info
    return info["path"], info


def _child_env(extra=None):
    """构造子进程环境：补 PYTHONPATH + 统一 MCP 地址与 MDL 路径旋钮。

    为什么必须补（B0 首轮实测，三条都是**管道问题而非脚本缺陷**）：
    1. tools/ 下 21 个脚本直接 `import db / rules / registry / sqlpack …`
       ——它们设计为容器内 `docker exec -e PYTHONPATH=/app` 运行。
       宿主直跑时缺 gateway 目录 → ModuleNotFoundError（实测 m45 / m61r2 /
       pt / rules_independent / rules_rework 五条全红于此）。
       本函数统一把 gateway/ 挂到 PYTHONPATH 头部，脚本无需再自带 sys.path 兜底。
    2. 部分脚本（a_verify / m3 / m4 / m5 / m64 / m65_remediation / m62_live /
       m42_live）从环境变量读 MCP 地址，默认值写的是**容器内端口 8080**，
       而宿主侧映射在 18080。统一 setdefault 成宿主可达地址；
       用户显式 export 过的值优先，不覆盖。
    """
    env = os.environ.copy()
    parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if GATEWAY_DIR not in parts:
        parts.insert(0, GATEWAY_DIR)
    env["PYTHONPATH"] = os.pathsep.join(parts)

    base = env.get("GATE_ALL_MCP_BASE", DEFAULT_MCP_BASE).rstrip("/")
    mcp_url = base + "/mcp"
    for key in ("GATEWAY_MCP_URL", "M64_MCP_URL", "M65_MCP_URL"):
        env.setdefault(key, mcp_url)

    # 3. MDL 语义层路径：gateway/registry.py 的默认值是**容器内路径**
    #    /workspace-a/mdl.json 与 /workspace-b/mdl.json；宿主直跑会
    #    FileNotFoundError（实测 pt / rules_independent / rules_rework 三条
    #    check 全红于此）。统一按仓库相对路径兜底，脚本自带 setdefault 的
    #    （audit_verify / fallback_verify / gates_selftest）取值与之相同，不冲突。
    env.setdefault("MDL_A_PATH", os.path.join(REPO_ROOT, "wren-docker", "workspace", "mdl.json"))
    env.setdefault("MDL_B_PATH", os.path.join(REPO_ROOT, "wren-docker-b", "workspace", "mdl.json"))

    # 4. 逐脚本前置变量（PER_SCRIPT_ENV）——优先级最高，覆盖上面的兜底值
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_subprocess(cmd, cwd, timeout_s=SINGLE_TIMEOUT, extra_env=None):
    """统一调用子进程，返回 (exit_code, stdout_b, stderr_b, seconds, timed_out)。

    设计原因：
    1. 所有脚本都是独立 Python 文件，统一用 subprocess 隔离，
       避免在主进程内 import 时模块级副作用（环境变量 monkeypatch、
       全局 FAIL 列表污染）互相干扰。
    2. 每个脚本独立超时，防止某一单测挂死拖累全套门禁。
    3. 环境由 _child_env() 统一构造（PYTHONPATH + MCP 地址 + MDL 路径），
       让「容器内脚本」与「宿主直跑」的差异全部收敛到这一处。
    """
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_s,
            env=_child_env(extra_env),
        )
        return (proc.returncode, proc.stdout, proc.stderr, time.time() - start, False)
    except subprocess.TimeoutExpired as e:
        stdout_b = e.stdout or b""
        stderr_b = e.stderr or b""
        if isinstance(stdout_b, str):
            stdout_b = stdout_b.encode("utf-8", errors="replace")
        if isinstance(stderr_b, str):
            stderr_b = stderr_b.encode("utf-8", errors="replace")
        msg = ("\n[gate_all] 单脚本超时 %ds，已强制终止\n" % timeout_s).encode("utf-8")
        return (-99, stdout_b, stderr_b + msg, time.time() - start, True)


def _script_verdict(exit_code, timed_out):
    if timed_out:
        return "TIMEOUT"
    return "PASS" if exit_code == 0 else "FAIL"


# ---------------------------------------------------------------------------
# G0 · 静态自检
# ---------------------------------------------------------------------------

def _g0_py_compile(target_glob, report_list):
    """对某 glob 下全部 .py 跑 py_compile，逐一写报告。"""
    files = sorted(glob.glob(os.path.join(REPO_ROOT, target_glob)))
    for f in files:
        rel = _repo_rel(f)
        script = rel
        start = time.time()
        ec, so, se, to = 0, b"", b"", False
        try:
            # 使用 compileall 的 py_compile 语义等价：编译但不生成 .pyc
            source = open(f, "rb").read()
            compile(source, f, "exec", dont_inherit=True)
        except SyntaxError as e:
            ec = 1
            msg = "SyntaxError: %s\n  File \"%s\", line %d\n    %s" % (
                e.msg, e.filename, e.lineno or 0, (e.text or "").rstrip()
            )
            se = msg.encode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            ec = 2
            se = ("%s: %s" % (type(e).__name__, e)).encode("utf-8", errors="replace")
        secs = time.time() - start
        report_list.append({
            "layer": "G0",
            "script": "py_compile::" + script,
            "exit_code": ec,
            "seconds": round(secs, 3),
            "stdout_tail": _last_lines(so, se),
            "verdict": _script_verdict(ec, False),
            "fail_hint": _locate_fail_line(_last_lines(so, se)) if ec != 0 else "",
        })


def _g0_requirements_baseline(report_list):
    """读取 gateway/requirements.txt，与基线清单比对。

    设计原因：项目硬约束一期只放 5 项依赖；任何新增都必须先改基线流程，
    不得通过"顺手加一行"绕过架构评审。
    """
    rel = "gateway/requirements.txt"
    f = os.path.join(REPO_ROOT, rel)
    start = time.time()
    ec, so, se = 0, b"", b""
    detected = []
    unknowns = []
    try:
        with open(f, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                # 剥离版本号区间符号：starlette>=0.47 → starlette
                clean = re.split(r"[<>=!~; \[]", line, maxsplit=1)[0].strip()
                if not clean:
                    continue
                detected.append((line, clean))
        # 逐项比对：要求 clean 名必须在基线中
        for orig, clean in detected:
            matched = False
            for base in ALLOWED_REQ_BASELINES:
                base_clean = re.split(r"[<>=!~; \[]", base, maxsplit=1)[0].strip()
                if clean.lower() == base_clean.lower():
                    matched = True
                    break
            if not matched:
                unknowns.append(orig)
        if unknowns:
            ec = 1
            se = ("发现 %d 项未登记依赖：\n  %s\n基线只允许：\n  %s" % (
                len(unknowns),
                "\n  ".join("- " + u for u in unknowns),
                "\n  ".join("- " + b for b in sorted(ALLOWED_REQ_BASELINES)),
            )).encode("utf-8")
        else:
            so = ("解析到 %d 项有效依赖，全部在基线内：\n  %s" % (
                len(detected),
                "\n  ".join("- " + o for o, _ in detected),
            )).encode("utf-8")
    except FileNotFoundError:
        ec = 2
        se = "requirements.txt 不存在".encode("utf-8")
    except Exception as e:  # noqa: BLE001
        ec = 3
        se = ("%s: %s" % (type(e).__name__, e)).encode("utf-8")

    report_list.append({
        "layer": "G0",
        "script": "req_baseline::" + rel,
        "exit_code": ec,
        "seconds": round(time.time() - start, 3),
        "stdout_tail": _last_lines(so, se),
        "verdict": _script_verdict(ec, False),
        "fail_hint": _locate_fail_line(_last_lines(so, se)) if ec != 0 else "",
    })


def _g0_env_selfcheck(report_list):
    """G0 环境自检：确认子进程解释器可用（含必需第三方依赖）。

    判据：解释器必须能 import sqlglot 与 psycopg。
    缺包即判红——否则后面 4 个 selftest、5 个 check 会以
    ModuleNotFoundError 形式散落报红，掩盖真实门禁结论。
    """
    start = time.time()
    info = CHILD_PYTHON_INFO
    miss = list(info.get("missing") or [])
    ec = 1 if miss else 0
    fallback = os.path.expanduser(
        "~/.workbuddy/binaries/python/envs/default/bin/python"
    )
    txt = (
        "解释器：%s\nPython：%s\n必需依赖：%s\n缺失：%s\n选择依据：%s"
        % (
            info.get("path"),
            info.get("version") or "(未知)",
            " / ".join(REQUIRED_MODULES),
            ("、".join(miss) if miss else "无"),
            info.get("reason"),
        )
    )
    if miss:
        txt += (
            "\n\n>>> 当前解释器缺 %s，请换一个能用的重跑，例如：\n"
            "    %s tools/gate_all.py\n"
            "或用 --python <解释器路径> 显式指定。"
            % ("/".join(miss), fallback)
        )
    report_list.append({
        "layer": "G0",
        "script": "env_selfcheck::interpreter",
        "exit_code": ec,
        "seconds": round(time.time() - start, 3),
        "stdout_tail": txt,
        "verdict": _script_verdict(ec, False),
        "fail_hint": ("解释器 %s 缺 %s" % (info.get("path"), "/".join(miss))) if miss else "",
    })


def run_g0(report_list):
    """G0 总入口：环境自检 → py_compile(gateway + tools) → requirements 基线。"""
    _g0_env_selfcheck(report_list)
    _g0_py_compile("gateway/*.py", report_list)
    _g0_py_compile("tools/*.py", report_list)
    _g0_requirements_baseline(report_list)


# ---------------------------------------------------------------------------
# G1 · 单元自证（*_selftest.py）
# ---------------------------------------------------------------------------

def _find_scripts(suffix_pattern):
    return sorted(glob.glob(os.path.join(REPO_ROOT, "tools", suffix_pattern)))


def _run_python_script(layer, abs_path, report_list, extra_flags=None):
    """以 `python <script>` 方式跑一份脚本，把结果追加进 report_list。"""
    rel = _repo_rel(abs_path)
    base = os.path.basename(abs_path)
    timeout_s = _timeout_for(abs_path)
    print(
        "  [... ] %s %-30s (timeout=%ds) ... " % (layer, base, timeout_s),
        end="", file=sys.stderr, flush=True,
    )
    t0 = time.time()
    cmd = [CHILD_PYTHON] + (extra_flags or []) + [abs_path]
    ec, so, se, secs, to = _run_subprocess(
        cmd, cwd=REPO_ROOT, timeout_s=timeout_s,
        extra_env=PER_SCRIPT_ENV.get(base),
    )
    tail = _last_lines(so, se)
    vd = _script_verdict(ec, to)
    # 进度符号
    icon = {"PASS": "[OK]", "FAIL": "[FL]", "TIMEOUT": "[TO]"}.get(vd, "[??]")
    extra = "" if vd == "PASS" else ("  exit=%s" % ec)
    print(
        "\r  %s %s %-30s %6.2fs%s" % (icon, layer, base, secs, extra),
        file=sys.stderr, flush=True,
    )
    item = {
        "layer": layer,
        "script": rel,
        "exit_code": ec,
        "seconds": round(secs, 3),
        "stdout_tail": tail,
        "verdict": vd,
        "fail_hint": _locate_fail_line(tail) if (ec != 0 or to) else "",
    }
    if timeout_s != SINGLE_TIMEOUT:
        item["timeout_policy"] = "probe_short=%ds" % timeout_s
    report_list.append(item)


def run_g1(report_list):
    """G1：tools/*_selftest.py（交付方离线自证）。"""
    scripts = _find_scripts("*_selftest.py")
    for s in scripts:
        _run_python_script("G1", s, report_list)


# ---------------------------------------------------------------------------
# G2 · 独立复核（*_check.py · 含独立性标注）
# ---------------------------------------------------------------------------

INDEPENDENT_HINTS = [
    "独立复", "独立扫", "独立核", "独立实现", "独立编写",
    "验收方独立", "验收方自建", "验收方资产",
    "不采信交付方", "不调用被验方", "不复用交付方",
    "自行实现一遍", "零共享代码", "不同代码路径",
    "自建，非交付方", "独立复算",
]

DEPENDENT_HINTS = [
    "live_check", "probe",
]


def _check_independence(abs_path):
    """读脚本源码做启发式判断：是否具备「独立重算」特征。

    为什么不直接跑 import 关系判断？
    因为许多 check 脚本确实会 import 被测模块拿到"原始输出"再做独立比对，
    import ≠ 不独立；真正判别的是 docstring / 代码注释里是否声明了
    「自行实现公式 / 独立客户端 / 不采信自证结论」等话语。
    """
    try:
        src = open(abs_path, "r", encoding="utf-8", errors="replace").read()
    except Exception:
        return False, "无法读取源码"
    reasons = []
    for h in INDEPENDENT_HINTS:
        if h in src:
            reasons.append("命中关键字「%s」" % h)
    base = os.path.basename(abs_path)
    for dh in DEPENDENT_HINTS:
        if dh in base:
            reasons.append("文件名含「%s」（环境探针类，不计入独立算法复算）" % dh)
            break
    if "independent" in base.lower():
        reasons.append("文件名含 independent")
    # 判定：必须至少有一条正向证据（或文件名含 independent），
    # 且不能被 DEPENDENT_HINTS 覆盖
    neg = any(dh in base for dh in DEPENDENT_HINTS)
    pos_flag = (
        any(h in src for h in INDEPENDENT_HINTS)
        or "independent" in base.lower()
    )
    if neg:
        return False, "；".join(reasons) if reasons else "属于探针/直播类脚本"
    return pos_flag, "；".join(reasons) if reasons else "未检测到明确声明"


def run_g2(report_list):
    """G2：tools/*_check.py（验收方独立复核）。

    判据两栏：
      · 脚本本身 exit 0 / 非 0
      · 独立性判定（仅做报告标注，不影响 PASS/FAIL —— 即使探针类也要真跑）
    """
    scripts = _find_scripts("*_check.py")
    for s in scripts:
        _run_python_script("G2", s, report_list)
        # 把独立判定写进最后一条追加字段
        indep, indep_reason = _check_independence(s)
        report_list[-1]["independent_recalc"] = indep
        report_list[-1]["independence_reason"] = indep_reason


# ---------------------------------------------------------------------------
# G3 · 真实链路（探活 + *_verify.py）
# ---------------------------------------------------------------------------

def _minimal_mcp_rpc(endpoint, payload, sid=None, timeout=30):
    """最小化 MCP HTTP 客户端（复用 gateway/healthcheck.py 思路）。

    不引入 fastmcp 包——因为 G0 允许的 5 项里其实有 fastmcp，
    但为了探活逻辑"与 fastmcp 解耦，未来换 SDK 也能跑"，这里手写一份；
    用标准库 urllib 即可。
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, None, None, "HTTPError %s" % e.code
    except Exception as e:  # noqa: BLE001
        return 0, None, None, "%s: %s" % (type(e).__name__, e)
    new_sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
    body = resp.read().decode("utf-8", errors="replace")
    # SSE: 取第一条 data: 行
    obj = None
    for line in body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                try:
                    obj = json.loads(chunk)
                    break
                except Exception:  # noqa: BLE001
                    continue
    if obj is None:
        # 非 SSE：直接按整段 JSON 解析
        try:
            obj = json.loads(body)
        except Exception:  # noqa: BLE001
            obj = None
    return resp.status, obj, new_sid, None


def _gateway_health_probe(report_list):
    """G3 前置探活：等效调 MCP 的 gateway_health 工具，确认 A/B 双库 ok。

    失败判定：
      · 任何一步异常 → G3 全红（verify 脚本不跑，每条写 SKIP_PROBE_FAIL）
      · gateway_health 返回不含 status=ok / A/B 任一 not ok → 同上
    """
    rel = "MCP::gateway_health"
    start = time.time()
    base = os.environ.get("GATE_ALL_MCP_BASE", DEFAULT_MCP_BASE).rstrip("/")
    endpoint = base + "/mcp"
    so_frags = []
    ec, se = 0, ""
    ab_ok = {"A": False, "B": False}
    try:
        # 1. initialize
        status, res, sid, err = _minimal_mcp_rpc(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "gate_all", "version": "1.0"},
                },
            },
            timeout=20,
        )
        so_frags.append("[initialize] HTTP=%s err=%s" % (status, err))
        if err or status != 200 or not res or "result" not in res:
            ec = 1
            se = "MCP initialize 失败：HTTP=%s err=%s" % (status, err or "(无 result)")
        else:
            # 2. notifications/initialized
            _minimal_mcp_rpc(
                endpoint,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                sid=sid, timeout=10,
            )
            # 3. tools/call gateway_health
            status, res2, _, err2 = _minimal_mcp_rpc(
                endpoint,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "gateway_health", "arguments": {}},
                },
                sid=sid, timeout=30,
            )
            so_frags.append("[gateway_health] HTTP=%s err=%s" % (status, err2))
            txt = ""
            if res2 and isinstance(res2, dict):
                rr = res2.get("result", {})
                sc = rr.get("structuredContent")
                if isinstance(sc, dict):
                    txt = json.dumps(sc, ensure_ascii=False)
                else:
                    for c in rr.get("content", []) or []:
                        if c.get("type") == "text":
                            txt += c.get("text", "")
            so_frags.append("[gateway_health 回文] " + txt[:500])
            if err2 or status != 200:
                ec = 2
                se = "gateway_health 调用失败：HTTP=%s err=%s" % (status, err2 or "")
            else:
                if '"status": "ok"' in txt or '"status":"ok"' in txt:
                    # 解析 dataset A/B 状态
                    for ds in ("A", "B"):
                        # 寻找形如 "dataset":"A","ok":true 的片段
                        m = re.search(
                            r'"dataset"\s*:\s*"%s"[^}]*?"ok"\s*:\s*(true|false)' % ds,
                            txt,
                        )
                        if m and m.group(1) == "true":
                            ab_ok[ds] = True
                    # 宽松判据：要么 A/B 都显式 ok，要么整体 status=ok 且没看到 A/B 的 false
                    any_bad = False
                    for ds in ("A", "B"):
                        m = re.search(
                            r'"dataset"\s*:\s*"%s"[^}]*?"ok"\s*:\s*false' % ds, txt,
                        )
                        if m:
                            any_bad = True
                    if not any_bad and (
                        (ab_ok["A"] and ab_ok["B"])
                        or not re.search(r'"dataset"\s*:\s*"[AB]"', txt)
                    ):
                        ec = 0
                    else:
                        ec = 3
                        se = "gateway_health 返回 A/B 数据集非全部 ok：A=%s B=%s" % (
                            ab_ok["A"], ab_ok["B"],
                        )
                else:
                    ec = 4
                    se = "gateway_health 未返回 status=ok，截取=%s" % txt[:200]
    except Exception as e:  # noqa: BLE001
        ec = 9
        se = "探活异常：%s: %s" % (type(e).__name__, e)

    tail = ("\n".join(so_frags) + "\n" + se).strip()
    report_list.append({
        "layer": "G3",
        "script": rel,
        "exit_code": ec,
        "seconds": round(time.time() - start, 3),
        "stdout_tail": tail[:2000],
        "verdict": _script_verdict(ec, False),
        "fail_hint": se if ec != 0 else "",
        "_probe_ok": ec == 0,
    })
    return ec == 0


def run_g3(report_list):
    """G3：MCP 探活 → 成功则跑 tools/*_verify.py。

    探活失败时，verify 脚本全部记为 SKIP（exit_code=-1，verdict=SKIP_PROBE_FAIL），
    避免 9 个脚本接连抛「连不上 DB/Wren」的长错误淹没报告。
    """
    probe_ok = _gateway_health_probe(report_list)
    scripts = _find_scripts("*_verify.py")
    for s in scripts:
        rel = _repo_rel(s)
        if not probe_ok:
            start = time.time()
            tail = "（G3 探活未通过，已跳过；请先确认 docker compose up 且 %s 可达）" % (
                os.environ.get("GATE_ALL_MCP_BASE", DEFAULT_MCP_BASE),
            )
            report_list.append({
                "layer": "G3",
                "script": rel,
                "exit_code": -1,
                "seconds": round(time.time() - start, 4),
                "stdout_tail": tail,
                "verdict": "SKIP_PROBE_FAIL",
                "fail_hint": "探活未通过，未执行",
            })
            continue
        _run_python_script("G3", s, report_list)


# ---------------------------------------------------------------------------
# 报告输出
# ---------------------------------------------------------------------------

def _known_red_reason(layer, rel_script, text=""):
    """命中「已知基线红」表则返回原因，否则空串。

    匹配规则：
      · 脚本路径尾部相同（防相对/绝对混用）；
      · 若该条登记了 match 指纹，则失败输出里必须出现该指纹才认。
    """
    for (ly, scr), spec in KNOWN_RED_BASELINE.items():
        if ly != layer:
            continue
        if not (rel_script.endswith(scr) or scr.endswith(rel_script)):
            continue
        if isinstance(spec, dict):
            need = spec.get("match")
            if need and need not in text:
                continue
            return spec.get("reason", "")
        return spec
    return ""


def _mark_known_reds(report_list):
    """给命中基线表的非 PASS 项打上 known_red 标记（原 verdict 不变，仍报红）。"""
    n = 0
    for it in report_list:
        if it["verdict"] == "PASS":
            continue
        text = "%s\n%s" % (it.get("fail_hint") or "", it.get("stdout_tail") or "")
        reason = _known_red_reason(it["layer"], it["script"], text)
        if reason:
            it["known_red"] = reason
            n += 1
    return n


def _report_summary(report_list, strict=False):
    """计算每层统计。

    failed  = 真红（阻塞准入）；known_red = 已知基线红（报红但不阻塞）。
    strict=True 时不认基线表，known_red 一并计入 failed。
    """
    by_layer = {k: [] for k in LAYER_ORDER}
    for item in report_list:
        by_layer.setdefault(item["layer"], []).append(item)
    summary = {}
    for layer, items in by_layer.items():
        if not items:
            continue
        total = len(items)
        passed = sum(1 for x in items if x["verdict"] == "PASS")
        skipped = sum(1 for x in items if x["verdict"].startswith("SKIP"))
        known = 0 if strict else sum(1 for x in items if x.get("known_red"))
        failed = total - passed - skipped - known
        summary[layer] = {
            "total": total, "passed": passed,
            "failed": failed, "skipped": skipped, "known_red": known,
            "all_green": failed == 0 and skipped == 0,
        }
    overall = all(v.get("all_green", False) for v in summary.values()) and bool(summary)
    return summary, overall


def _write_json(report_list, summary, overall, strict=False):
    """写 gate-report.json（相对仓库根）。"""
    obj = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "repo_root": REPO_ROOT,
        "child_python": CHILD_PYTHON,
        "child_python_info": CHILD_PYTHON_INFO,
        "strict": strict,
        "summary": summary,
        "overall_pass": overall,
        "known_red_baseline": {
            "%s::%s" % k: v for k, v in KNOWN_RED_BASELINE.items()
        },
        "items": report_list,
    }
    out = os.path.join(REPO_ROOT, "gate-report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    return out


def _write_md(report_list, summary, overall):
    """写 gate-report.md（人类可读，红项置顶）。"""
    lines = []
    lines.append("# Askoda 统一门禁报告")
    lines.append("")
    lines.append("- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    lines.append("- 仓库根：`%s`" % REPO_ROOT)
    hard_reds = [x for x in report_list
                 if x["verdict"] not in ("PASS",) and not x.get("known_red")]
    known_reds = [x for x in report_list
                  if x["verdict"] not in ("PASS",) and x.get("known_red")]
    if overall and known_reds:
        overall_icon = "✅ 准入通过（另有 %d 项已知基线红，不阻塞）" % len(known_reds)
    elif overall:
        overall_icon = "✅ 全绿"
    else:
        overall_icon = "❌ 存在 %d 项阻塞红项" % len(hard_reds)
    lines.append("- 总判定：**%s**" % overall_icon)
    lines.append("")

    # 每层摘要表
    lines.append("## 分层摘要")
    lines.append("")
    lines.append("| 层 | 名称 | 脚本数 | PASS | FAIL | 已知红 | SKIP | 判定 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for layer in LAYER_ORDER:
        s = summary.get(layer)
        if not s:
            continue
        icon = "✅" if s["all_green"] else ("⚠️" if s["failed"] == 0 and s["skipped"] > 0 else "❌")
        lines.append(
            "| %s | %s | %d | %d | %d | %d | %d | %s |"
            % (layer, LAYER_DESC[layer], s["total"],
               s["passed"], s["failed"], s.get("known_red", 0),
               s["skipped"], icon)
        )
    lines.append("")

    # 红项置顶区：阻塞红在前，已知基线红单列一节（如实展示，只是不阻塞准入）
    def _emit_reds(title, items):
        if not items:
            return
        lines.append(title)
        lines.append("")
        for idx, x in enumerate(items, 1):
            vd = x["verdict"]
            tag = {"TIMEOUT": "⏱️", "SKIP_PROBE_FAIL": "⏭️"}.get(vd, "❌")
            lines.append("### %d. %s %s · %s" % (idx, tag, vd, x["script"]))
            lines.append("")
            lines.append("- 所在层：%s（%s）" % (x["layer"], LAYER_DESC[x["layer"]]))
            lines.append("- 退出码：%s" % x["exit_code"])
            lines.append("- 耗时：%.3fs" % x["seconds"])
            if x.get("fail_hint"):
                lines.append("- 失败定位：`%s`" % x["fail_hint"].replace("`", "'"))
            if x.get("known_red"):
                lines.append("- ⚠️ 已知基线红（不阻塞准入）：%s" % x["known_red"])
            if x.get("independent_recalc") is not None:
                ind_str = "✅ 独立重算" if x["independent_recalc"] else "⚠️ 非独立重算（探针类或未声明）"
                lines.append("- 独立性：%s" % ind_str)
                if x.get("independence_reason"):
                    lines.append("  - 判定依据：%s" % x["independence_reason"])
            lines.append("")
            lines.append("```text")
            lines.append(x["stdout_tail"])
            lines.append("```")
            lines.append("")

    _emit_reds("## 🚨 阻塞红项明细（按出现顺序）", hard_reds)
    _emit_reds("## 🧷 已知基线红（如实记录 · 不阻塞准入）", known_reds)

    # 每层完整清单（含 PASS）
    lines.append("## 分层完整清单")
    lines.append("")
    for layer in LAYER_ORDER:
        items = [x for x in report_list if x["layer"] == layer]
        if not items:
            continue
        lines.append("### %s · %s（%d 项）" % (layer, LAYER_DESC[layer], len(items)))
        lines.append("")
        lines.append("| # | 脚本 | 判定 | 退出码 | 耗时 | 失败定位/独立性 |")
        lines.append("| ---: | --- | --- | ---: | ---: | --- |")
        for idx, x in enumerate(items, 1):
            icon = {
                "PASS": "✅",
                "FAIL": "❌",
                "TIMEOUT": "⏱️",
                "SKIP_PROBE_FAIL": "⏭️",
            }.get(x["verdict"], x["verdict"])
            hint_parts = []
            if x.get("known_red"):
                hint_parts.append("已知红(不阻塞)")
            if x.get("fail_hint"):
                hint_parts.append(x["fail_hint"].replace("|", "/").replace("\n", " "))
            if x.get("independent_recalc") is not None:
                hint_parts.append(
                    "独立=%s" % ("✅" if x["independent_recalc"] else "⚠️")
                )
            hint = "；".join(hint_parts)
            if len(hint) > 120:
                hint = hint[:117] + "..."
            lines.append(
                "| %d | `%s` | %s %s | %s | %.2fs | %s |"
                % (idx, x["script"], icon, x["verdict"],
                   x["exit_code"], x["seconds"], hint)
            )
        lines.append("")

    out = os.path.join(REPO_ROOT, "gate-report.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _parse_args():
    ap = argparse.ArgumentParser(description="Askoda G0-G3 统一门禁脚本")
    ap.add_argument(
        "--layers", default=",".join(LAYER_ORDER),
        help="只跑指定层，逗号分隔，如 G0,G1 （默认全部）",
    )
    ap.add_argument(
        "--timeout", type=int, default=SINGLE_TIMEOUT,
        help="单脚本超时上限（秒），默认 %d" % SINGLE_TIMEOUT,
    )
    ap.add_argument(
        "--python", default=None,
        help="指定跑子脚本的解释器（默认自动探测一个具备 sqlglot/psycopg 的）",
    )
    ap.add_argument(
        "--strict", action="store_true",
        help="不认「已知基线红」表：所有红项一律阻塞（用于总验收）",
    )
    return ap.parse_args()


def main():
    global SINGLE_TIMEOUT
    args = _parse_args()
    if args.timeout and args.timeout > 0:
        SINGLE_TIMEOUT = args.timeout
    py_path, py_info = _pick_python(args.python)
    print("=" * 78)
    print("Askoda 统一门禁 · 启动")
    print("  子进程解释器：%s（Python %s）"
          % (py_path, py_info.get("version") or "?"))
    print("  选择依据　　：%s" % py_info.get("reason"))
    if py_info.get("missing"):
        print("  ⚠️ 缺依赖　　：%s" % "、".join(py_info["missing"]))
    print("-" * 78)
    chosen = [x.strip().upper() for x in args.layers.split(",") if x.strip()]
    invalid = [x for x in chosen if x not in LAYER_ORDER]
    if invalid:
        print("未知层：%s；可选：%s" % (invalid, LAYER_ORDER), file=sys.stderr)
        return 2

    report_list = []

    if "G0" in chosen:
        run_g0(report_list)
    if "G1" in chosen:
        run_g1(report_list)
    if "G2" in chosen:
        run_g2(report_list)
    if "G3" in chosen:
        run_g3(report_list)

    known_n = _mark_known_reds(report_list)
    summary, overall = _report_summary(report_list, strict=args.strict)

    json_path = _write_json(report_list, summary, overall, strict=args.strict)
    md_path = _write_md(report_list, summary, overall)

    # 控制台速览
    print("=" * 78)
    print("Askoda 统一门禁 · 执行完毕")
    print("  JSON 报告：%s" % _repo_rel(json_path))
    print("  MD   报告：%s" % _repo_rel(md_path))
    if args.strict:
        print("  模式　　　：--strict（已知基线红也阻塞）")
    print("-" * 78)
    for layer in LAYER_ORDER:
        s = summary.get(layer)
        if not s:
            continue
        icon = "✅" if s["all_green"] else ("⚠️" if s["failed"] == 0 and s["skipped"] > 0 else "❌")
        print(
            "  %s %-2s %-20s total=%d pass=%d fail=%d known=%d skip=%d"
            % (icon, layer, LAYER_DESC[layer],
               s["total"], s["passed"], s["failed"],
               s.get("known_red", 0), s["skipped"])
        )
    print("-" * 78)
    total_red = sum(s.get("failed", 0) for s in summary.values())
    if total_red == 0 and overall:
        if known_n and not args.strict:
            print("总判定：✅ 准入通过（%d 项 PASS，另有 %d 项已知基线红不阻塞）" % (
                len(report_list) - known_n, known_n,
            ))
        else:
            print("总判定：✅ 全绿（%d 项全部 PASS）" % len(report_list))
        return 0
    print("总判定：❌ 存在 %d 项阻塞红项，请查看 %s" % (
        total_red, _repo_rel(md_path),
    ))
    return 1


if __name__ == "__main__":
    sys.exit(main())
