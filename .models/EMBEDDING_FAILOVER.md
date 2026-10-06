# embedding 主备方案（seven smile 主 / 本机备）

## 为什么要主备

知识库检索的 embedding 是**单点**：RAGFlow 的 `base_url` 是 MySQL 里的静态字符串，它自己不会切。
主服务（seven smile）一挂，检索立即全断。

已实测：2026-10-06 当天seven smile 侧掉线两次（5/20、12/30），当时只能靠本机临时救场。
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

## 遗留风险

1. **主服务仍是单点**：seven smile 容器停 / 被 OOM → 检索断。靠 `restart: unless-stopped` + 8g 内存限兜。
2. **切换是人工/半自动的**：`auto` 需要有人（或定时任务）跑，没有接进健康检查。
   若要全自动，可加 cron 每5 分钟跑一次 `auto`。
3. **本机备与主同在一条 Tailscale 链路上**：若问题出在网络/链路本身，主备会同时不可用。
4. **对外封装（FileBay 品牌）前建议**：把 `auto` 挂成定时任务，消除"等人发现"这一环。
