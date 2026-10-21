# -*- coding: utf-8 -*-
"""门禁层数契约（G1/G3 两侧共用的**唯一权威来源**）。

这个常量为什么不住在 tools/e2e_verify.py
----------------------------------------
层数曾有两份**互相独立**的来源：`tools/e2e_verify.py` 的 `EXPECTED_LAYER_COUNT = 8`
（G3 层，走真实链路），与 `tools/gates_selftest.py` 里写死的 8（G1 层，离线自证）。
两份各自演进 ⇒ 门禁层数一旦变更就会再次脱节，而**脱节的表现是"两边各自全绿"**——
G1 验不到 G3 的假设、G3 也不看 G1 的假设，故障要等真出问题才暴露。

为什么不直接把常量留在 e2e_verify 再让 gates_selftest import 它
--------------------------------------------------------------
**import 方向不能反**。两个脚本分属门禁不同层，依赖能力是单向的：

  · `tools/e2e_verify.py`  —— G3 真实链路，本来就依赖容器与真实服务；
  · `tools/gates_selftest.py` —— G1 离线自证，docstring 明写
    "离线，不需要容器与 Wren"。它只 import `gateway.gates`（纯解析，无连接）。

若让 G1 反向 import G3 的脚本，就把「要容器、要真实链路」的依赖拖进 G1，
**破坏 gates_selftest 离线可跑这条已有契约**。故常量必须落在**两侧都敢 import**
的中性位置——即本文件。

本文件的硬约束（改动前先读）
--------------------------
⚠️ **必须保持零 import**（只用标准库，且现在连标准库都不需要）。
它是 G1 离线链路上的一环；一旦在这里 `import psycopg` / `db` / `wren` 之类，
就会把容器与数据库依赖拖进 G1，`gates_selftest.py` 的离线可跑性即刻失效。
新增内容一律用字面量，不要引入依赖。

为什么放在 gateway/ 而不是 tools/
---------------------------------
层数是 `gateway/gates.py`（`review()` 的产出）的契约，权威数字应与**产出它的那份代码**
同目录——将来在 gates.py 里加/删一层的人，看的就是这个目录。
且 `gateway/` 已被两侧脚本放进 `sys.path`
（`gates_selftest.py` 与 `e2e_verify.py` 各自 `sys.path.insert(.../gateway)`，
`gate_all.py` 另经 PYTHONPATH 兜底），两侧都能直接 import，无需改动任何路径逻辑。
"""

# `gateway.gates.review()` 产出的 layers 恒定层数：L-REQ + L1-L5 + L-SQLFluff + L-GX。
# 改这个数字 ⇒ 必须同步改 `gateway/gates.py` 里 review() 的各条级联分支，
# 否则门禁自证会立刻报红（这是本常量存在的意义：把散落两处的数字收敛成一处）。
EXPECTED_LAYER_COUNT = 8
