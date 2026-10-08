# 外部依赖与已知限制

> 本文是 [Askoda](../README.md) 的**依赖说明与风险披露**。
> 换机器部署前**必读**这一节，否则会遇到「其他功能都正常、唯独某项能力全断
> 且没有任何报错」的情况。
> 完整部署流程看[部署手册](DEPLOYMENT.md)。

## 目录

- [1· 本机 embedding 服务（知识检索的硬依赖）](#1-本机-embedding-服务知识检索的硬依赖)
- [2 · 远端 embedding（已停用，仅作回滚选项）](#2-远端-embedding已停用仅作回滚选项)
- [3 · 知识底座尚未独立部署](#3--知识底座尚未独立部署请勿粉饰)

---

这一节是**换机器部署前必读**。知识检索这条链路依赖 **embedding 服务**与  
**知识底座（RAGFlow）**；其中知识底座目前仍跑在「旧绑定栈」里，尚未独立部署。  
不了解这一节，照 README 做完会得到一个「其他功能都正常、唯独知识检索全断」的系统，  
且没有任何报错提示你缺了什么。

#### 1 本机 embedding 服务（知识检索的硬依赖）

| 项    | 值                                     |
| ---- | ------------------------------------- |
| 地址   | `http://host.docker.internal:18002/v1` |
| 宿主机端口 | `18002`（由 `TEI_EMBEDDING_PORT` 控制）      |
| compose 服务 | `tei-embedding`（**默认启动**，无 profile 门槛） |
| 模型   | `bge-m3`（固定，不可换）                       |
| 谁在用  | RAGFlow 每次检索、每次文档入库时现调它算向量            |

**断了会怎样**：知识检索**全断**，不是降级。RAGFlow 不缓存查询向量，  
每次 `/api/v1/retrieval` 都要现调 embedding，所以这个服务一挂，  
所有检索请求直接报 `EmbeddingError code=100`（表现为 `knowledge_search` 整条失败）；  
文档上传也会卡在解析阶段（切分已完成、向量化失败，`chunk_count` 恒为 0）。

**启动与自查**：

```bash
# 随主栈一起起（模型未拉取时先跑 ./.models/fetch_bge_m3.sh "$PWD/bge-m3"）
docker compose up -d tei-embedding

curl -s -m 8 --noproxy '*' -o /dev/null -w "HTTP=%{http_code}\n" \
  -X POST "http://127.0.0.1:18002/v1/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{"input":"探活","model":"bge-m3"}'
# 期望：HTTP=200
```

> amd64 镜像在 arm64 上模拟运行，**模型加载 + warm up 实测约 40～180 秒**，  
> 期间 `/health` 不通属正常，不要过早判定启动失败。
>
> **客户端批量上限必须 ≥16**：RAGFlow 的 OpenAI 兼容 embedding 客户端写死每批 16 条，  
> 而 TEI 的 `--max-client-batch-size` 默认上限是 8，不改则每批都被 413 拒掉，  
> 大文件整份解析失败（2026-10-08 实证）。compose 里已显式设为 32。

#### 2 远端 embedding（已停用，仅作回滚选项）

2026-10-06 曾把 embedding 迁到 seven smile 主机 `http://100.103.240.78:18001/v1`  
（Tailscale 节点 `sevensmile.tailb5e44d.ts.net`，x86_64 原生）。**2026-10-08 起收回本机**，  
原因是该主机**一周内多次中断**：负载被其它任务冲到 19~132，`/info` 秒回但 `/embeddings`  
排队 30~50s、单批 76~120s，最终整份文档解析挂死 10 分钟以上。跨主机依赖让「上传知识文档」  
这类基础功能随时不可用，且排障要跨两台机器。

收回时已验证**权重同源、向量空间未变**（本机 `.models/bge-m3` 与远端 `bge-m3-onnx` 同一份权重）：  
切换后用既有文档检索，「坪效」0.710 /「会员等级」0.774 仍正常召回，**已入库文档无需重建**。

需要回滚时用 `tools/embedding_failover.sh`（改 RAGFlow MySQL 里的 `base_url`）：

切换主备用 `tools/embedding_failover.sh`（改 RAGFlow MySQL 里的 `base_url`，  
**实测改完立即生效，无需重启 RAGFlow**）：

| 命令                                             | 作用                                    |
| ---------------------------------------------- | ------------------------------------- |
| `bash tools/embedding_failover.sh status`      | 看当前指向哪个 + 主备各自探活结果                    |
| `bash tools/embedding_failover.sh check`       | 只探活，主备都探，**不改任何东西**                   |
| `bash tools/embedding_failover.sh auto`        | 自动判断：主不通则切备，备不通则切回主（带 300s 冷却防抖）      |
| `bash tools/embedding_failover.sh switch-main` | 备 → 主（切回**本机** TEI，默认方向）              |
| `bash tools/embedding_failover.sh switch-back` | 主 → 备（切到**远端**，仅回滚用）                  |

> 主 = 本机 TEI（`host.docker.internal:18002`），备 = 远端（`100.103.240.78:18001`）。

> 该脚本依赖 RAGFlow 的 MySQL 容器 `filebay-knowledge-trial-mysql-1`。  
> 这个容器属于下面的「旧绑定栈」—— **如果那个栈没起，脚本读不到当前指向，`status` 会显示「读取失败」**。

> ⚠️ **判障纪律：请求超时 ≠ 服务挂了**（2026-10-07 实测踩坑）
>
> 排查顺序必须是：① 在**服务所在机器上自测** → ② 看容器日志有无**真实请求打点**  
> → ③ 查端口映射 → ④ 查防火墙 → ⑤ 才轮到重启服务。
>
> 当时的实际情况：宿主机侧 3 次 × 30s 全超时，被判为「服务宕机」；  
> 但在远端机器本机自测是 **HTTP 200 / 0.14s**，容器日志持续成功打点，  
> 恢复后连跑 10 次全通（0.10~0.57s）。**真因是两机链路临时抖动，不是服务故障。**
>
> 两个反直觉点：
>
> - `docker inspect` 的 `Running=true` **只能证明进程在，不能证明服务可用**；
> - `ping` **通**不代表链路可用——实测出现过 0% 丢包但延迟从 7ms 抖到 453ms，  
>   延迟高到一定程度就会导致请求超时。**判链路质量必须实测响应时间。**
>
> **不要用「重启」代替「定位」。**

#### 3 ⚠️ 知识底座尚未独立部署（请勿粉饰）

**这一条是本节最重要的内容：RAGFlow 底座目前不是一个独立栈，物理上尚未独立部署。**

实际情况是：

- `127.0.0.1:19380` 这个入口由一个 **nginx 代理**（`ekos-ragflow-proxy`）提供；
- 它的 **backend 仍然是旧绑定栈（FileBay 栈）内的 `ragflow-cpu` 容器**，  
  连同它的 MySQL / Elasticsearch / Redis / MinIO，全都还挂在 `filebay-knowledge-trial-*` 这一套里；
- 也就是说：**RAGFlow 并不在本项目 `docker compose up -d` 的编排范围内**。  
  本仓库起的 8 个服务里**没有** RAGFlow，`docker compose up -d` **不会**把它带起来。

因此当前状态是：

| 能力                                                  | 状态                                  |
| --------------------------------------------------- | ----------------------------------- |
| 双模拟库 Wren 语义层、元数据采集、需求单、附件、语义分析                     | ✅ 本栈内自包含，`docker compose up -d` 即可用 |
| 知识检索 `knowledge_search` / `knowledge_citation_list` | ⚠️ **依赖外部栈**，需先单独把旧绑定栈拉起，否则不可用      |

换机器部署时，知识检索要能用，**必须先在宿主机上把那套旧绑定栈（FileBay 栈）起起来**；  
只跑本仓库的 `docker compose up -d` 是**不够**的。

> 这一项在项目内部记为 P1-1技术债。网关侧已按「无兜底分支」实现：  
> RAGFlow 不可用就如实报错，**不会静默降级成假结果** —— 这是刻意的设计，  
> 宁可报错也不要让业务人员拿到看似正常的错数据。
