# 外部依赖与已知限制

> 本文是 [Askoda](../README.md) 的**依赖说明与风险披露**。
> 换机器部署前**必读**这一节，否则会遇到「其他功能都正常、唯独某项能力全断
> 且没有任何报错」的情况。
> 完整部署流程看[部署手册](DEPLOYMENT.md)。

## 目录

- [1· 远端 embedding 服务（知识检索的硬依赖）](#1-远端-embedding-服务知识检索的硬依赖)
- [2 · 本机备用 embedding 与主备切换](#2-本机备用-embedding-与主备切换)
- [3 · 知识底座尚未独立部署](#3--知识底座尚未独立部署请勿粉饰)

---

这一节是**换机器部署前必读**。本项目不是全自包含的：知识检索这条链路依赖  
**一台外部主机**，且**知识底座尚未独立部署**。不了解这一节，照 README 做完会得到一个  
「其他功能都正常、唯独知识检索全断」的系统，且没有任何报错提示你缺了什么。

#### 1 远端 embedding 服务（知识检索的硬依赖）

| 项             | 值                                |
| ------------- | -------------------------------- |
| 地址            | `http://100.103.240.78:18001/v1` |
| Tailscale 节点名 | `sevensmile.tailb5e44d.ts.net`   |
| 模型            | `bge-m3`（固定，不可换）                 |
| 谁在用           | RAGFlow 每次检索时现调它算查询向量            |

**断了会怎样**：知识检索**全断**，不是降级。RAGFlow 不缓存查询向量，  
每次 `/api/v1/retrieval` 都要现调 embedding，所以这个地址一挂，  
所有检索请求直接报 `EmbeddingError code=100`（表现为 `knowledge_search` 整条失败）。

**Tailscale 是前置条件**：这个地址是 Tailscale 内网地址，**不登录 Tailscale 就根本连不上**。  
换机器部署时若本机没装/没登录 Tailscale，知识检索一定是不通的 —— 这不是配置错，是网络层不通。

先自查连通性（不依赖本项目任何容器）：

```bash
curl -s -m 8 --noproxy '*' -o /dev/null -w "HTTP=%{http_code}\n" \
  -X POST "http://100.103.240.78:18001/v1/embeddings" \
  -H 'Content-Type: application/json' \
  -d '{"input":"探活","model":"/models/bge-m3"}'
# 期望：HTTP=200
```

#### 2 本机备用 embedding 与主备切换

本机保留了一份 TEI 作为**备用**，但在 compose 里挂了 `profiles: ["local-embed"]`，  
**默认不启动** —— `docker compose config --services` 里 `tei-embedding` 数量为 **0** 是正常的。

需要它时（主挂了、或者想在本机跑）：

```bash
# 启动本机备用（首次需拉模型：./.models/fetch_bge_m3.sh "$PWD/bge-m3"）
docker compose --profile local-embed up -d tei-embedding
```

> 宿主机端口由 `TEI_EMBEDDING_PORT` 控制，默认 `18002`。  
> amd64 镜像在 arm64 上模拟运行，**模型加载 + warm up 实测约 40～180 秒**，  
> 期间 `/health` 不通属正常，不要过早判定启动失败。

切换主备用 `tools/embedding_failover.sh`（改 RAGFlow MySQL 里的 `base_url`，  
**实测改完立即生效，无需重启 RAGFlow**）：

| 命令                                             | 作用                               |
| ---------------------------------------------- | -------------------------------- |
| `bash tools/embedding_failover.sh status`      | 看当前指向哪个 + 主备各自探活结果               |
| `bash tools/embedding_failover.sh check`       | 只探活，主备都探，**不改任何东西**              |
| `bash tools/embedding_failover.sh auto`        | 自动判断：主不通则切备，备不通则切回主（带 300s 冷却防抖） |
| `bash tools/embedding_failover.sh switch-back` | 主 → 备（切本机 TEI，会自动把它拉起来）          |
| `bash tools/embedding_failover.sh switch-main` | 备 → 主（切回远端）                      |

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
