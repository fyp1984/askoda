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
| G0 | 静态自检（语法 + 依赖基线） | 65 | 否 |
| G1 | 单元自证（`*_selftest.py`） | 15 | 否 |
| G2 | 独立复核（`*_check.py`） | 5 | 否 |
| G3 | 真实链路端到端 | 2 | **是** |

> ⚠️ **项数以 `gate_all.py` 实跑为准**（`gate-report.md` 里每层都自动输出实际项数）。
> 本表数字会随新增 `*_selftest.py` / `*_check.py` 脚本而变——
> **新增脚本时请一并更新本表**，否则下一个人会按过期数字判断门禁覆盖面。
> ⚠️ 历史上本表已与实际脱节过（曾长期停在 52/8/2/2，实际已是 58/11/3/2），
> 根因是**门禁项数没有单一事实源**——`gate_all.py` 里也没有任何期望项数常量。

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
| 3 | 改动 D1 决策（SQL 审查不通过时**不自动改写**，`review.revised_sql` 恒为 `None`）<br>（"D1"指分级阻断决策 D1，非缺陷编号） |
| 4 | 删除数据，或修改**仓库内登记的冻结基线资产**（清单：`tools/baseline_manifest.json`）<br>（文档库基线的漂移巡检由 `tools/docsync.py` 承担，不在本红线职责内） |
| 5 | 新增运行时依赖（见 HC-03） |
| 6 | 需要人工点「信任」或改`mcp.json` 才能生效的改动 |

### 判据已知假阳性与豁免记录

红线由 `tools/redline_guard.py` 自动判定。它是**辅助判定，不是最终裁决**——
判据本身也可能与红线意图脱节。下表登记已确认的假阳性。

| 日期 | 场景 | 判据实际行为 | 红线意图 | 处置 |
|---|---|---|---|---|
| 2026-10-20 | `sql_review` 末尾追加可选参数 `demand_id` | 判红。`check_r2_tool_contract` 只匹配 diff 删除侧的 `-def <已注册工具名>`，**不比较参数名与顺序** | **未违反**：末尾追加可选参数不破坏任何现有调用方（`review(sql, dataset)` 与 `review(sql, dataset=dataset)` 行为不变） | 主理人裁决豁免本次；判据精确化另立任务单（见下） |

⚠️ **本条豁免仅对「仅在末尾追加、且新参数带默认值」成立。**
改名 / 改顺序 / 在已有参数中间插参**仍然判红**，不得援引本条。

#### 待办：R2 判据精确化

`check_r2_tool_contract` 的 def 行判据应改为比对参数签名：

- **判红**：函数名丢失/改名、已有参数名被改或被删、已有参数相对顺序被改、新参数插在已有参数中间
- **豁免**：仅在末尾追加、且新参数带默认值

⚠️ 改判据必须**自带 fixture 测试**（末尾追加⇒不判红；重排⇒判红；改名⇒判红），
否则就是又一次「判据说它做了它没做的事」。

⚠️ **判据当前测的是「def 行是否出现在 diff 删除侧」，与它声称的「参数名/顺序」不符。**
这与 `docs/GLOSSARY.md` 的门禁层定义写反、导出脚本声明层数与实列不符，
是同一类病灶：**文档/判据声称的与实际做的不一致**。

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