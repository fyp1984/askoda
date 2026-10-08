# 贡献指南

感谢你愿意参与 Askoda。

**动手前请先读这三条，它们会直接决定你的 PR 能不能合：**

1. **不改工具入参** —— 48 个 MCP 工具的**入参名与顺序是按位契约**，改名或改顺序会让所有已发布的调用方失效。新增可以，改名不行。
2. **不改开源上游** —— Wren Engine / RAGFlow 等一律以独立进程 + 标准协议接入，不 fork、不打补丁、不改 core。
3. **网关不内置 LLM** —— 规划必须是确定性的，同一需求两次执行必须产出逐字节相同的 SQL。

---

## 目录

- [提交规范](#提交规范)
- [代码约束](#代码约束)
- [验收新功能的三步法](#验收新功能的三步法)
- [提交前必跑的门禁](#提交前必跑的门禁)
- [红线：什么不能做](#红线什么不能做)

---

## 提交规范

Conventional Commits + 署名：

```
<type>(<scope>): <subject>

<body>

Signed-off-by: 西北人 <fyp1984@yeah.net>
```

- `type` ∈ `feat / fix / docs / style / refactor / perf / test / chore / revert`
- `scope` = `gateway / bff / tools / web / docs / knowledge / poc-eval`
- **每条提交必须带 `Signed-off-by`**（`git commit -s` 自动加）
- **一条提交只做一件事**；代码与文档改动分离提交

**走 PR，不直推 main。** 从最新 `main` 切出 `feat/xxx` 或 `fix/xxx`，push 后提 PR，由维护者合并。

---

## 代码约束

以下 9 条是硬约束，违反过不了门禁：

| 编号 | 约束 | 验证方式 |
|---|---|---|
| HC-01 | 网关侧不内置任何 LLM 调用 | `grep -r "openai\|anthropic\|gemini\|llm\|chat_" gateway/` 必须空 |
| HC-02 | 禁止修改第三方开源源码 | 代码审查 |
| HC-03 | 第三方依赖仅限 `fastmcp / psycopg / minio / sqlglot` | `gateway/requirements.txt` 行数不得增加（bugfix 版本 pin 除外） |
| HC-04 | 数据库访问必须走 `gateway/db.py` 的 `query/query_one/execute` | 自查调用点 |
| HC-05 | `gateway/rules.py` 必须纯函数，零 IO / 零 DB / 零网络 | 自查 import |
| HC-06 | 所有脚本必须通过语法检查 | 对每个 `.py` 跑 `ast.parse` |
| HC-07 | 九字段留痕一行都不能缺 | `tools/gate_all.py` G3 真实链路断言 |
| HC-08 | `actor` 必须透传写入 `sql_runs.actor`，不许用默认值 | `tools/gate_all.py` G3 留痕断言 |
| HC-09 | 筛选异常时严禁静默降级成全量 | `tools/gate_all.py` G3 dataset 三态对照 |

<p align="center">

<b>HC-03 是硬红线：不要新增运行时依赖。</b>需要新能力时优先用标准库或已有依赖实现。

</p>

---

## 验收新功能的三步法

**顺序不能颠倒——先写自证，再改代码。**

1. **先写自证脚本**：放 `tools/<feature>_selftest.py`（G1 自动发现）或 `tools/<feature>_check.py`（G2 自动发现）。全是断言，不是打印。
2. **再改代码**：改完先跑一遍全量语法检查。
3. **最后跑门禁**：`python3 tools/gate_all.py --strict`，四层全绿才算过。

<p align="center">

<b>为什么先写测试：</b>本项目多个真实缺陷（缺口分类关键词表太窄、
别名指向错误字段）都是<b>写自证脚本时才发现的</b>，不是代码review发现的。

</p>

---

## 提交前必跑的门禁

```bash
python3 tools/gate_all.py --layers G0,G1# 日常：静态 + 单元，不写库
python3 tools/gate_all.py                # 全量：含 G2/G3，会真实连库
```

| 层 | 内容 | 项数 | 是否写库 |
|---|---|---|---|
| G0 | 静态自检（语法 + 依赖基线） | 52 | 否 |
| G1 | 单元自证（`*_selftest.py`） | 8 | 否 |
| G2 | 独立复核（`*_check.py`） | 2 | 否 |
| G3 | 真实链路端到端 | 2 | **是** |

**退出码即结论**：`0` 全绿；非 `0` 有红项，看 `gate-report.md`。

<p align="center">

<b>日常只跑 G0/G1。</b>G3 会真实连库并调接口，耗时更长且要求全部服务在跑。

</p>

---

## 红线：什么不能做

以下动作一律禁止，PR 会被直接打回：

| # | 红线 |
|---|---|
| 1 | 修改开源上游代码（Wren / RAGFlow 等） |
| 2 | 改动已有工具的**入参名或顺序**（按位契约） |
| 3 | 改动 D1 决策（SQL 审查不通过时**不自动改写**，`review.revised_sql` 恒为 `None`） |
| 4 | 删除数据，或修改 sha256 冻结基线 |
| 5 | 新增运行时依赖（见 HC-03） |
| 6 | 需要人工点「信任」或改`mcp.json` 才能生效的改动 |

<p align="center">

<b>关于演示数据：</b>两套模拟业务库的数据既是演示素材，也是回归验证的基准。
<b>它们是资产，不是垃圾。</b>需要清理时用 `tools/reset_demo_data.py`
（默认 dry-run，加 `--apply` 才动手），不要直接写 SQL 删表。

</p>

---

## 发现安全问题

**不要公开提issue。** 见主 [README 的安全披露](README.md#安全披露)。

---

## 许可证

提交即表示你同意你的贡献按[MIT License](LICENSE) 授权。
文档部分采用 CC BY 4.0。