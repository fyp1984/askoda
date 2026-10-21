# 本机开发环境：沙箱禁删与 `tempfile` 不可用

> 这份说明记录**本机（WorkBuddy 沙箱）**的一个环境限制，以及仓库为此提供的对策。
> 换到能正常删除文件的机器上，本文档描述的问题不存在，仓库脚本照常工作。

## 一、现象

在本机跑任何**用到 `tempfile` 的脚本**，起手就红：

```
FileNotFoundError: [Errno 2] No usable temporary directory found in
['/var/folders/wj/…/T/', '/tmp', '/var/tmp', '/usr/tmp', '…/askoda']
```

同时每次 `python` 启动都会打印：

```
Error in sitecustomize; set PYTHONVERBOSE for traceback:
FileNotFoundError: [Errno 2] No usable temporary directory found in […]
```

## 二、根因（实测定位，不是猜测）

### 2.1 沙箱禁止「让文件从某路径消失」

本机安全策略拦截**所有**让文件消失的操作：`os.remove` / `os.unlink` /
`Path.unlink()` / `shutil.rmtree`，**连 `os.rename` 也拦**（策略按**路径**拦，
不看系统调用）。

### 2.2 Python 的 `tempfile` 靠「建+删」验证目录可用

CPython 的 `tempfile.gettempdir()` 判定「这个目录能不能用」的方式是
**建一个探测文件、再把它删掉**（`_get_default_tempdir()` 里 `os.open(...)` +
`os.unlink(...)`）。

⇒ 删不掉 ⇒ 探测抛 `FileNotFoundError` ⇒ `gettempdir()` 失败
⇒ 任何用到 `tempfile` 的脚本**在第一行就炸**。

### 2.3 还有一个更隐蔽的坑：Python 只加载第一个 `sitecustomize`

本机 `PYTHONPATH` 指向 WorkBuddy 自带的 shim：

```
/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/vendor/shim
```

那个 shim 的 `_build_os_tmp_dirs()` 里会调 `tempfile.gettempdir()`
⇒ **它自己就把 `sitecustomize` 的加载搞炸了**（报错但不致命，解释器照常启动）。

而 Python **只加载 `sys.path` 里第一个找到的 `sitecustomize`**
⇒ 光「再加一个 PYTHONPATH 目录」没用，除非排在它**前面**。

### 2.4 ⚠️「逻辑删除」这招在本机**也不生效**（实测）

`~/.workbuddy/pyshim/sitecustomize.py` 的做法是把删除改成**重命名**成 `*.deleted`。
但由于 **2.1 连 `os.rename` 都拦**，重命名同样失败 ⇒ **文件仍在原处**
⇒ **依赖「删文件造场景」的用例会得出假结论**。

⚠️ 典型症状（**失败信息字面误导**）：某个用例报「判据漏判」，真实情况是
**「用例没造出场景」**——判据其实是对的。
本仓库的 C2 用例在改造前就踩过这个：报「清单缺失却判绿」，实际是
**清单根本没被删掉**（`load_frozen_baseline_manifest` 读到的仍是正常清单）。

⇒ **本仓库的自测一律不靠删除造场景**（见第四节）。

## 三、仓库的对策

### 3.1 `tools/_pyshim/sitecustomize.py`（仓库自带垫片）

两件事：

1. **先设 `tempfile.tempdir`** ⇒ `gettempdir()` 见到 `tempdir` 已设就直接返回，
   **跳过整个探测** ⇒ 不需要「建+删」⇒ 探测不再失败。
   ★ **这一条才是关键**，它让 `tempfile.mkdtemp()` 可用。
2. 临时目录内的删除改成**逻辑删除**（重命名为 `*.deleted`）。
   ⚠️ 但按 2.4，本机**连 `os.rename` 都拦** ⇒ 这条在本机**不生效**（静默失败）。
   ⇒ 它的作用只在**允许删除**的机器上（那里本就不需要它，属于无害的冗余）；
   **本机的正确做法是「自测不靠删除造场景」**（见第四节）。

#### 🔴 第 2 件事的**启用条件**（正常机器上必须完全不生效）

⚠️ 本垫片会被 `gate_all.py` **无条件**挂到子进程 `PYTHONPATH` 最前
⇒ 它在**任何机器**上都会被加载。
⇒ 若第 2 件事（替换 `os.remove` / `os.unlink` / `shutil.rmtree`）也**无条件**做，
那么在**正常开发者机器**上，「删除」会被改成「重命名为 `*.deleted`」：

- 临时目录里**堆积 `.deleted` 残骸**（本该消失的文件留在盘上）；
- 任何依赖「删掉后目录为空」的用例会得到**错误结论**。

⇒ **那是污染正常环境，比「本机起手就红」更糟。**

⇒ 垫片因此**先探测再决定**（`_probe_delete_works()`，见 `tools/_pyshim/sitecustomize.py`）：

| 探测结果 | `_LOGICAL_DELETE_ENABLED` | 效果 |
|---|---|---|
| rename 与 remove **都能**做（正常机器） | **False** | **完全不碰删除语义**；只保留第 1 件事（设 `tempfile.tempdir`） |
| rename **被拦**（本机沙箱） | **True** | 启用逻辑删除（本机用它造「缺失」场景） |

⚠️ 探测**不能靠删文件**（沙箱禁删）⇒ 改用「建一个文件 → 试着 `rename` 它」：
rename 被拦 ⇒ 逻辑删除也不可用 ⇒ 说明本机确实禁删 ⇒ 才启用。
⚠️ 探测用的临时文件在真删机器上会被真删掉；在禁删机器上可能留下残骸
（沙箱禁删，留残骸是环境限制的必然结果，不是缺陷）。

⚠️ 本垫片**不修改** `/Applications/WorkBuddy.app/` 下的任何东西
（那是别的产品的产物，改了会被升级覆盖，也不在本仓库授权范围内）。

### 3.2 `tools/run_selftest.sh`（推荐入口）

```bash
bash tools/run_selftest.sh                  # 跑全量门禁（日常 G0+G1，不写库）
bash tools/run_selftest.sh tools/xxx.py     # 跑单个脚本
bash tools/run_selftest.sh --strict         # 其余参数原样传给 gate_all.py
```

它把 `tools/_pyshim` 挂到 `PYTHONPATH` **最前面**（必须排在 WorkBuddy 自带 shim 之前，
理由见 2.3）。

手动跑单个脚本时的等价写法：

```bash
PYTHONPATH="$PWD/tools/_pyshim:$PYTHONPATH" python3 tools/xxx.py
```

### 3.3 `tools/gate_all.py` 的子进程环境

`_child_env()` 已把 `tools/_pyshim` 插到子进程 `PYTHONPATH` 最前
⇒ **直接跑 `python3 tools/gate_all.py` 也能让 G1/G2 的子进程拿到垫片**，
不需要额外设 `PYTHONPATH`。

实测（`gate_all.py --layers G0,G1`）：

| | 垫片前 | 垫片后 |
|---|---|---|
| G1 通过 | 12/16 | **15/15（当时）** |
| 失败原因 | 4 项全是 `No usable temporary directory` | 0 |

## 四、⚠️ 本仓库的自测**不依赖**删除

沙箱连 `os.rename` 都拦 ⇒ 垫片的「逻辑删除」在本机**不生效**
⇒ 依赖「删文件造场景」的用例会得出**假结论**（以为文件没了、其实还在）。

所以本仓库的自测一律**不靠删除造场景**：

| 脚本 | 造场景的方式 |
|---|---|
| `tools/redline_r3_selftest.py` | **纯内存** fixture（判据核心 `_r3_findings_in_source` 是纯函数，只吃「源码文本 + 改动行号表」），完全不碰文件系统 |
| `tools/redline_r4_selftest.py` | 自带 `tempfile.tempdir`（跳过探测）；C1「登记资产不存在」用**把登记路径改成一个不存在的路径**达成；C2「清单缺失」用**造一个根本没有 manifest 的沙箱根**达成 —— 两者都不删任何文件 |

⚠️ 另有两个脚本（`tools/mdl_check_selftest.py` / `tools/gate_count_check_selftest.py`，
**不是本单产出**）仍依赖「删文件造场景」：在**允许删除**的机器上它们正常；
在本机上它们会红，原因是**环境限制**（垫片的逻辑删除被 `os.rename` 拦截失效），
不是脚本缺陷。

### 4.1 附：本批**未**统一「中文文档 / 英文代码」

本仓库现行的口径是「**文档与注释用中文，代码标识符用英文**」。
把它**全面统一**（例如把 `docs/` 下的中文术语表回译成英文）属于**风格统一**，
**本批不做**，登记为后续迭代。
⚠️ 但**在此明确一行口径**，免得下次又当成新议题讨论：

> **约定**：文档 / 注释 / 提交说明 = 中文；代码标识符（变量名、函数名、文件名）= 英文。
> 本批只做**判据与自证**的修正，不做文案风格改造。

## 五、排查清单

| 症状 | 检查 |
|---|---|
| `No usable temporary directory` | 是否用了 `tempfile`？有没有设 `tempfile.tempdir`？ |
| `Error in sitecustomize` 每次启动都打印 | `PYTHONPATH` 里第一个 `sitecustomize` 是哪个？我们的垫片排前面了吗？ |
| 某个用例「文件明明删了却还在」 | 依赖删除的用例在本机不可靠（`os.rename` 也被拦） |
| 门禁 G1 整层红且提示临时目录 | 走 `bash tools/run_selftest.sh`（它会挂垫片） |
| `tools/` 下多出调试探针后 G0 项数 +1 | 探针放**子目录**（`tools/<子目录>/`）不计入 `tools/*.py`；本机**禁删也禁改名**（`os.rename` / `os.unlink` 均被拦），**移不走** ⇒ 用 `.gitignore` 的 `tools/_*` 排除（未跟踪 + 被 ignore ⇒ 不计入项数） |
