# 验证与自证

> 本文是 [Askoda](../README.md) 的**验收方法手册**：跑哪些脚本、每层门禁查什么、
> 演示前怎么重置环境。
> 想跑起来看[快速开始](../README.md#快速开始)；要部署看[部署手册](DEPLOYMENT.md)。

所有验证脚本遵循统一约定：

- **退出码 0 = 全绿；非 0 = 有断言失败；退出码 2 = 数据不一致**
- **不依赖外部 LLM**，可在离线环境独立复现
- **自己验自己 ≠ 验收**：关键链路（如 M3 技术词闸门）由脚本在客户端**独立复扫**，不信赖服务端内部清单

### 1 核心验证脚本（宿主机能跑的）

```bash
# 双探活（HTTP + MCP 全链路），加 --deep 额外真执行一次 gateway_health
python3 gateway/healthcheck.py --base http://127.0.0.1:18080 --deep

# MCP 工具面验收（48 个工具 + 真实调用比对，对照《实施推进与验收方案》）
python3 tools/mcp_acceptance_check.py

# 五层门禁自证（纯离线，不依赖数据库）
python3 tools/gates_selftest.py
python3 tools/rules_selftest.py

# 脱敏 / 检索 / 证据 / 语义 四门自证（基本都是纯离线）
python3 tools/masking_selftest.py
python3 tools/knowledge_selftest.py
python3 tools/evidence_selftest.py
python3 tools/semantics_selftest.py
```

### 2 统一门禁（G0–G3 四层）

```bash
# 一条命令跑完四层门禁，产出 gate-report.json / gate-report.md（均为运行产物，不入库）
python3 tools/gate_all.py

# 日常快跑：只跑静态 + 单元两层（不写库、不碰真链路）
python3 tools/gate_all.py --layers G0,G1

# 总验收：所有红项一律阻塞（忽略「已知基线红」豁免表）
python3 tools/gate_all.py --strict
```

| 层      | 内容                    | 是否写库     |
| ------ | --------------------- | -------- |
| **G0** | 静态自检（语法编译 + 依赖基线）     | 否        |
| **G1** | 单元自证（`*_selftest.py`） | 否        |
| **G2** | 独立复核（`*_check.py`）    | 否        |
| **G3** | 真实链路（探活 + 端到端）        | **是**，慎跑 |

> 门禁按命名约定**自动发现**同目录脚本：`*_selftest.py` → G1、`*_check.py` → G2、`*_verify.py` → G3。退出码即准入结论：0 全绿，1 有红项。

### 3 端到端演练

```bash
python3 poc-eval/poc_e2e_gateway.py
# 产出 poc-eval/e2e-gateway-report.json（机器可读，运行产物，不入库）
```

覆盖：主线故事 12 条 + POC-1 题库 30 条（20 正向 / 10 拒绝）+ POC-3 陷阱 10 条 + MDL 事实核对 50 条。

### 4 演示前环境重置

```bash
# 默认 dry-run，不加 --apply 一个字都不改
python3 tools/reset_demo_data.py

# 清需求单 + 分析轮次 + 确认问答 + 知识库测试夹具
python3 tools/reset_demo_data.py --apply

# 连同附件对象一并清理
python3 tools/reset_demo_data.py --apply --purge-attachments
```

---
