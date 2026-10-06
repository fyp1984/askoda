# embedding 主备切换（开发测试期的便利工具，非生产级高可用）

> **定位说明（2026-10-06 用户明确）**：当前处于**开发与测试阶段**，服务挂掉不算大问题。
> 本方案是为了「本地开发时远端embedding 挂了，能一条命令切回本机继续干活」，**不是生产级高可用**。
> 负载均衡、业务连续性、多副本等留到生产环境集中处理。

## 为什么要主备

知识库检索的 embedding 是**单点**：RAGFlow 的 `base_url` 是 MySQL 里的静态字符串，它自己不会切。
主服务（seven smile）一挂，检索立即全断。

已实测：2026-10-06 当天seven smile侧掉线两次（5/20、12/30），当时只能靠本机临时救场。
现在把这条救场路径**固化成脚本 + compose profile**。

## 架构

```
                    ┌─────────────────────────────────────┐
RAGFlow检索  ──►    │ rag_flow.tenant_model_instance.extra │
（无感知能力）      │        base_url（静态字符串）        │
                    └──────────────┬──────────────────────┘
                            外部脚本切换 ↓
              ┌─────────────────────────┴─────────────────────────┐
        主（默认）                                          备（按需）
  100.103.240.78:18001                        host.docker.internal:18002
  seven smile 主机（x86_64 原生）本机 TEI（amd64 模拟）
  模型 ~/models/bge-m3-onnx（ONNX）           模型 ./.models/bge-m3（ONNX）
  restart unless-stopped，8g 限                compose profile: local-embed
```

**关键前提（已实测）**：改完 MySQL 的 `base_url` **立即生效，不需要重启 RAGFlow**。
所以切换是秒级的。

## 为什么本机备默认不启动

本机 TEI 占2.1GiB / 15.66GiB、CPU 1.77%——**不是资源瓶颈**，但常驻也没必要。
用 compose profile 隔离：`docker compose up` 不会拉起它，需要时才显式启动。

**回滚/启用本机备**：

```bash
cd /Users/FYP/Documents/WorkSpace/askoda
export PATH="$HOME/.orbstack/bin:$PATH"
docker compose --profile local-embed up -d tei-embedding
# 首次需 40~180s 加载模型（amd64 模拟下更慢）
```

## 用法

```bash
cd /Users/FYP/Documents/WorkSpace/askoda
export PATH="$HOME/.orbstack/bin:$PATH"

bash tools/embedding_failover.sh status       # 看当前指向 + 双路探活
bash tools/embedding_failover.sh check        # 只探活，不改任何东西
bash tools/embedding_failover.sh auto         # ★ 自动判断该切哪边
bash tools/embedding_failover.sh switch-back   # 手动切到本机备
bash tools/embedding_failover.sh switch-main   # 手动切回 seven smile 主
```

### `auto` 的判断逻辑

```
主通？
├─ 是 →当前在用备？ → 切回主（冷却期内不动）
│└─ 否 → 已在用主，无需动作
└─ 否 → 当前在用主？ → 切到备（会先自动拉起本机 TEI 并等其就绪）
        └─ 否 → 已在用备，等主恢复后再跑 auto 自动切回
```

**冷却保护**：切换后 300 秒内不再重复切（记录在 `/tmp/askoda_embedding_failover_last`）。
防止主服务抖动（今天实测它掉过两次）导致反复横跳。

## 切换后必须验证

脚本会校验 `base_url` 是否真的改对了，**没改对会明确报错并保持现状**。但仍建议手工确认一次：

```bash
/Users/FYP/.workbuddy/binaries/python/envs/default/bin/python tools/mcp_knowledge_search.py --m2
#期望：total=3 / total=18 / total=19，无 ERR
```

## 两侧一致性保证

两侧是**同一份 ONNX 权重**（`model.onnx_data` 均为 2,266,820,608 字节），
向量交叉比对三句话余弦相似度**全部 = 1.000000**（逐位一致）。

所以切换不会改变召回质量。**若哪天换了一侧的模型，必须重做交叉比对**，
否则可能出现「静默的召回质量下降」——相似度失去意义但不报错。

## 遗留风险（开发测试期可接受，生产阶段须处理）

**开发测试期**以下几条都不阻塞，随手用 `auto` 救一下即可：

1. **主服务仍是单点**：seven smile 容器停 / 被 OOM → 检索断。手动跑一次 `switch-back` 即可。
2. **切换需人触发**：不设 cron 定时任务（开发期不值得，挂了跑一下就好）。
3. **主备同在一条 Tailscale 链路上**：链路本身故障时两侧会一起挂。

**进入生产环境前必须补齐**（本期不做，届时集中处理）：

- 真正的负载均衡与故障转移（不依赖人工触发）
- RAGFlow 侧对 `base_url` 的感知能力（当前它是静态字符串，完全无感知）
- 端到端监控与告警（谁在什么时候切过、切成功没有）
- 两侧模型版本一致性校验（换任一侧模型必须重跑向量交叉比对，否则召回会静默变差）
