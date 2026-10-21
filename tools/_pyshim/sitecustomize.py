# -*- coding: utf-8 -*-
"""仓库自带的 `sitecustomize` 垫片 —— 修「沙箱禁删 ⇒ tempfile 不可用」。

为什么需要这个文件
------------------
本机（WorkBuddy 沙箱）**禁止任何"让文件从某路径消失"的操作**：
`os.remove` / `os.unlink` / `Path.unlink()` / `shutil.rmtree` 全被拒。

而 Python 的 `tempfile.gettempdir()` 判定「这个目录可不可用」的方式是
**建一个探测文件、再把它删掉**（CPython 的 `_get_default_tempdir()` 里
`os.open(...)` + `os.unlink(...)`）⇒ 删不掉 ⇒ 探测抛 `FileNotFoundError`
⇒ 任何用到 `tempfile` 的自测脚本一律以

    FileNotFoundError: [Errno 2] No usable temporary directory found in [...]

起手就红。**这不是脚本缺陷，是环境限制**（同一批脚本在正常机器上全绿）。

⚠️ 还有一个更隐蔽的坑：**Python 只加载 sys.path 里第一个找到的 `sitecustomize`**。
本机 `PYTHONPATH` 指向 WorkBuddy 自带的 shim
（`/Applications/WorkBuddy.app/.../cli/vendor/shim`），而那个 shim 的
`_build_os_tmp_dirs()` 里第 807 行会调 `tempfile.gettempdir()` ⇒ 它自己
就把 sitecustomize 的加载搞炸了（**报错但不致命**，解释器照常启动）。
所以**光改 PYTHONPATH 顺序没用**——必须是「我们的 shim 排在前面」，
让 WorkBuddy 那个**根本没被加载**。

这个垫片做两件事
----------------
1. **先设 `tempfile.tempdir`**：`gettempdir()` 见到 `tempdir` 已设就直接返回，
   **跳过整个探测** ⇒ 不需要「建+删」⇒ 探测不再失败。
   ⚠️ 这一条就足够让 `tempfile.mkdtemp()` 可用。
2. **让临时目录内的删除变成"逻辑删除"**（重命名为 `*.deleted`）：
   依赖"删文件造场景"的自测用例才能跑出正确结论（文件真的从原路径消失了）。

⚠️ **实测**：本机沙箱连 `os.rename` 也会被拦（安全策略按**路径**拦，不看系统调用），
   所以第 2 条的重命名**在本机不生效**（会静默失败，文件仍在原处）。
   ⇒ 因此**本仓库的自测脚本一律不依赖"删文件造场景"**：
     · `tools/redline_r3_selftest.py` —— 纯内存 fixture，完全不碰文件系统；
     · `tools/redline_r4_selftest.py` —— 自带 `tempfile.tempdir`，
       且 C1/C2 改用「不依赖删除」的造法（见该文件注释）。
   垫片保留第 2 条是为了**别的**脚本（mdl_check_selftest / gate_count_check_selftest）
   在**允许删除的机器**上行为不变；在本机它们仍会因删不掉而红（见文档说明）。

⚠️ 本文件**不修改** `/Applications/WorkBuddy.app/` 下的任何东西
（那是别的产品的产物，改了会被升级覆盖，也不在本仓库授权范围内）。

怎么用
------
本仓库的 `tools/run_selftest.sh` 会自动把本目录挂到 PYTHONPATH **最前面**：

    bash tools/run_selftest.sh                 # 跑全量门禁 G0+G1
    bash tools/run_selftest.sh tools/xxx.py    # 跑单个脚本

手动跑单个脚本时，等价写法是：

    PYTHONPATH="$PWD/tools/_pyshim:$PYTHONPATH" python3 tools/xxx.py
"""
import os
import shutil
import tempfile

# ---------------------------------------------------------------------------
# 1. 跳过 tempfile 的可用性探测
# ---------------------------------------------------------------------------
# ⚠️ 必须**在**任何代码调用 gettempdir() **之前**设好 tempdir。
#   优先用 WorkBuddy 自己的 tmp 目录（与其它工具的落盘位置一致）；
#   建不出来就退回仓库内的 .tmp_gate/。
_TD_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "tmp"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  ".tmp_gate"),
]
for _c in _TD_CANDIDATES:
    try:
        os.makedirs(_c, exist_ok=True)
        tempfile.tempdir = _c
        break
    except Exception:  # noqa: BLE001
        continue

_TD = tempfile.tempdir


def _under_td(p):
    """路径是否位于临时目录内（临时目录的删除是安全的）。"""
    try:
        return os.path.abspath(str(p)).startswith(os.path.abspath(_TD) + os.sep)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 2. 临时目录内的「删除」改成逻辑删除（重命名）
# ---------------------------------------------------------------------------
# ⚠️ 在**允许删除**的机器上，这保证依赖"删文件造场景"的用例能跑出正确结论
#   （文件真的从原路径消失了 ⇒ 判据看到的是「缺失」而不是「还在」）。
#   ⚠️ 在**禁删**的机器上（沙箱按路径拦，连 rename 也拦），
#   重命名会失败 ⇒ 文件仍在原处 ⇒ 这类用例会得出**假结论**。
#   ⇒ 本仓库自己的自测不依赖删除（见上），不受此影响。
class DeleteBlockedError(OSError):
    """本机沙箱禁止删除 ⇒ 逻辑删除（重命名）也失败。

    ⚠️ 为什么要**抛**而不是静默 pass：
    静默 pass 时，"建文件 → 删 → 断言文件不存在"这类用例会**假绿**：
    `os.remove()` 返回成功、文件却还在原处 ⇒ 断言"缺失"其实没验到缺失
    ⇒ **判据看起来在跑，实际没测到它要测的东西**。
    一个"永远成功"的删除比没有删除更坏 —— 它让人以为场景搭好了。
    ⇒ 所以这里抛错：**让"造场景失败"当场暴露**，而不是变成一条假绿。
    """


def _logical_delete(path):
    """逻辑删除：重命名为 ``<path>.deleted``。**成功返回 True，失败抛 DeleteBlockedError**。

    ⚠️ 本机沙箱按**路径**拦，连 ``os.rename`` 也拦 ⇒ 重命名会失败
    ⇒ 在**允许删除**的机器上正常生效（文件真的从原路径消失）；
    在本机则抛错（由调用方决定是"造场景失败"还是"清理收尾，可忽略"）。
    """
    try:
        os.rename(path, str(path) + ".deleted")
        return True
    except Exception as e:  # noqa: BLE001
        raise DeleteBlockedError(
            "沙箱禁止删除（连 rename 也被拦），逻辑删除失败：%s（%s）"
            % (path, e))


_remove = os.remove
_unlink = os.unlink
_rmtree = shutil.rmtree


def _safe_remove(path, *a, **kw):
    """显式删除单个文件。

    ⚠️ 这里**抛错**（不静默）：显式 `os.remove` 几乎都是"造场景"
    （造一个缺失/损坏的文件来验判据）⇒ 静默失败会让判据**假绿**。

    ⚠️ 失败时**先记账再抛**：显式删除是最需要被看见的那条路，
    若它只抛异常而不进 :data:`_DELETE_FAILURES`，
    "删不掉"这件事在计数里就表现为 0 ⇒ 计数给出的"没有删除失败"是**不完整**的。
    """
    if _under_td(path):
        try:
            _logical_delete(path)
        except Exception as e:  # noqa: BLE001
            _DELETE_FAILURES.append((str(path), 1))
            raise
        return None
    return _remove(path, *a, **kw)


def _safe_unlink(path, *a, **kw):
    """显式删除单个文件（同 :func:`_safe_remove`：假绿比报错更坏）。"""
    if _under_td(path):
        try:
            _logical_delete(path)
        except Exception as e:  # noqa: BLE001
            _DELETE_FAILURES.append((str(path), 1))
            raise
        return None
    return _unlink(path, *a, **kw)


def _safe_rmtree(path, *a, **kw):
    """删除整棵目录树。

    ⚠️ **这里刻意保持宽容（不抛）**，与 :func:`_safe_remove` 相反：
    ``rmtree`` 的绝大多数调用是**收尾清理**（``finally: shutil.rmtree(tmp,
    ignore_errors=True)``）⇒ 抛错会让"清理失败"变成"用例失败"，
    那是**假红**（把环境问题算成判据问题）。

    ⇒ 但"没删掉"不能完全静默：用一个**模块级计数器**记下失败数，
    供自证/审计查询（``get_delete_failures()``）。
    ⚠️ 注意 ``shutil.rmtree`` 自身传 ``ignore_errors=True`` 时
    也会吞掉本函数的异常 ⇒ 这正是"宽容"这一侧要的效果。
    """
    if _under_td(path):
        # 目录树：逐层改名，留下 .deleted 残骸（无害）
        failures = 0
        for root, dirs, files in os.walk(path, topdown=False):
            for name in files:
                try:
                    _logical_delete(os.path.join(root, name))
                except Exception:  # noqa: BLE001
                    failures += 1
            for name in dirs:
                try:
                    _logical_delete(os.path.join(root, name))
                except Exception:  # noqa: BLE001
                    failures += 1
        try:
            _logical_delete(path)
        except Exception:  # noqa: BLE001
            failures += 1
        if failures:
            _DELETE_FAILURES.append((str(path), failures))
        return None
    return _rmtree(path, *a, **kw)


#: 累计的"逻辑删除失败"记录（(路径, 失败项数)）。
#: ⚠️ **这是「进程内」计数，不是跨进程审计**：
#:   门禁把每个自测跑在**子进程**里 ⇒ **父进程读不到子进程的计数**
#:   ⇒ 本计数**不能**用于判断"全仓自证是否依赖删除"。
#:   跨进程审计需另设机制（**未实现**）。
#:   它的正当用途：同一个进程内排查"这次删除为什么没生效"。
_DELETE_FAILURES = []


def get_delete_failures():
    """返回**本进程**累计的"逻辑删除失败"记录。

    ⚠️ **这是「进程内」计数，不是跨进程审计**：
      门禁把每个自测跑在**子进程**里 ⇒ **父进程读不到子进程的计数**
      ⇒ 本计数**不能**用于判断"全仓自证是否依赖删除"。
      跨进程审计需另设机制（**未实现**）。
      它的正当用途：同一个进程内排查"这次删除为什么没生效"。
    """
    return list(_DELETE_FAILURES)


# ---------------------------------------------------------------------------
# 3. 启用条件：只在「本机沙箱真的禁删」时才改删除语义
# ---------------------------------------------------------------------------
# 🔴 为什么必须有这个条件（**不能无条件改**）：
#   本垫片会被 gate_all.py 无条件挂到子进程 PYTHONPATH 最前 ⇒ 它在**任何机器**上
#   都会被加载。若无条件替换 os.remove / os.unlink / shutil.rmtree，
#   则在**正常开发者机器**上也会把「删除」改成「重命名为 *.deleted」：
#   · 临时目录里会**堆积 .deleted 残骸**（本该消失的文件留在盘上）；
#   · 任何依赖「删掉后目录为空」的用例会得到错误结论。
#   ⇒ 那是**污染正常环境**，比「本机起手就红」更糟。
#
# ⇒ 判据：**先探测**当前机器能不能真删；**能真删 ⇒ 完全不动删除语义**，
#   只保留第 1 件事（设 tempfile.tempdir，跳过探测）。
#   探测本身不能删文件（沙箱禁删）⇒ 改用「建一个文件 → 试着 rename 它」：
#   rename 被拦 ⇒ 逻辑删除也不可用 ⇒ 说明本机确实禁删 ⇒ 才启用逻辑删除。
#   ⚠️ 探测用的临时文件在真删机器上会被真删掉；在禁删机器上它可能留下残骸
#   （沙箱禁删，留残骸是环境限制的必然结果，不是缺陷）。
def _probe_delete_works():
    """探测本机能否真删/真改名。返回 (can_rename, can_remove)。"""
    import tempfile as _tf
    probe_dir = _tf.mkdtemp(prefix="shim_probe_")
    probe = os.path.join(probe_dir, "probe.txt")
    can_rename = can_remove = False
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        try:
            os.rename(probe, probe + ".renamed")
            can_rename = True
            os.rename(probe + ".renamed", probe)
        except Exception:  # noqa: BLE001
            can_rename = False
        try:
            os.remove(probe)
            can_remove = True
        except Exception:  # noqa: BLE001
            can_remove = False
    except Exception:  # noqa: BLE001
        pass
    finally:
        # 收尾：沙箱禁删时只能改名；能真删的机器上这里走真删
        for p in (probe, probe + ".renamed"):
            try:
                os.remove(p)
            except Exception:  # noqa: BLE001
                try:
                    os.rename(p, p + ".gone")
                except Exception:  # noqa: BLE001
                    pass
        try:
            os.rmdir(probe_dir)
        except Exception:  # noqa: BLE001
            pass
    return can_rename, can_remove


_CAN_RENAME, _CAN_REMOVE = _probe_delete_works()

#: 本机是否启用「逻辑删除」语义。**只在连 rename 都被拦时才启用。**
#: ⚠️ 正常机器（能真删）⇒ 恒为 False ⇒ 垫片**完全不碰删除语义** ⇒ 不污染正常环境。
_LOGICAL_DELETE_ENABLED = not _CAN_RENAME

if _LOGICAL_DELETE_ENABLED:
    os.remove = _safe_remove
    os.unlink = _safe_unlink
    shutil.rmtree = _safe_rmtree

try:
    import pathlib as _pl

    _punlink = _pl.Path.unlink

    def _safe_punlink(self, *a, **kw):
        """显式删除单个文件（同 :func:`_safe_remove`：假绿比报错更坏）。"""
        if _LOGICAL_DELETE_ENABLED and _under_td(str(self)):
            _logical_delete(str(self))
            return None
        return _punlink(self, *a, **kw)

    _pl.Path.unlink = _safe_punlink
except Exception:  # noqa: BLE001
    pass
