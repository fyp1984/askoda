# Askoda 部署手册

> **这份文档写给第一次接触本系统的人。**
> 不需要懂 Docker、不需要懂数据库，按顺序照着做即可。
>
> 全程大约 20 分钟（不含下载时间）。

---

## 目录

- [第 0 步 · 你需要准备什么](#第-0-步--你需要准备什么)
- [第 1 步 · 装 Docker（三平台分开）](#第-1-步--装docker三平台分开)
- [第 2 步 · 下载本项目](#第-2-步--下载本项目)
- [第 3 步 · 填配置](#第-3-步--填配置)
- [第 4 步 · 启动](#第-4-步--启动)
- [第 5 步 · 首次初始化](#第-5-步--首次初始化)
- [第 6 步 · 验证跑通了](#第-6-步--验证跑通了)
- [常见问题](#常见问题)
- [附录 A · 开源软件清单](#附录-a--开源软件清单)
- [附录 B · 命令速查](#附录-b--命令速查)

---

## 第 0 步 · 你需要准备什么

### 硬件要求

| 项 | 最低 | 推荐 |
|---|---|---|
| 内存 | 8 GB | 16 GB |
| 磁盘可用空间 | 20 GB | 40 GB |
| CPU | 4 核 | 8 核 |
| 处理器架构 | x86_64（Intel/AMD）或 arm64（Apple 芯片） | 都支持 |

> 磁盘主要是 Docker 镜像和数据库文件。第一次 `docker compose up` 会下载约 3–4 GB 镜像。

### 软件要求

**只需要装 Docker。** 其他（数据库、语义引擎、对象存储）都由项目自带镜像自动装好，**不需要你手动下载**。

| 软件 | 是否需要手动装 |
|---|---|
| Docker Desktop / OrbStack / Docker Engine | ✅ **要装**，见[第 1 步](#第-1-步--装docker三平台分开) |
| Python | ❌ **不用装**（用 `tools/init_first_run.sh` 初始化）；只有想手工跑脚本才需要 |
| Git | ⚠️ 可选，想用命令行下载才需要（也可直接下 ZIP 包） |

### 关于知识库功能（可选）

系统自带的业务口径文档需要 RAGFlow（知识底座）才能用上。

- **不部署 RAGFlow**：系统照常可用，只是「引用知识文档」这个功能是空的，其他都不受影响。
- **要部署**：需额外按 [RAGFlow 官方文档](https://github.com/infiniflow/ragflow) 装一套（它自带 MySQL / Elasticsearch / MinIO / Redis，约占 8 GB 内存）。

<p align="center">

💡 <b>建议：</b>第一次部署先把 RAGFlow 放一边，先把主系统跑通，跑顺了再回来装。

</p>

---

## 第 1 步 · 装 Docker（三平台分开）

### 我是 Windows 用户

1. 打开 <https://www.docker.com/products/docker-desktop/>
2. 点 **Download for Windows**，下载 `Docker Desktop Installer.exe`
3. 双击安装，**一路点默认选项即可**
   - 安装中会提示启用 WSL 2，勾选并继续（Windows 10 需 19041 以上，Windows 11 均可）
   - 若提示重启电脑，**重启后再继续**
4. 启动 Docker Desktop，**等任务栏小鲸鱼图标变成绿色**
5. 打开 PowerShell 验证：

```powershell
docker --version
docker compose version
```

两条命令都能打出版本号（如 `Docker version 27.x` / `Docker Compose version v2.x`）即成功。

<details>
<summary><b>常见问题：报错 "'docker' 不是内部或外部命令"</b></summary>

Docker 没装好或没重启。**重启电脑**后再试；仍不行则重新运行安装包。
</details>

<details>
<summary><b>常见问题：WSL2 安装失败</b></summary>

先用 PowerShell（管理员）执行：

```powershell
wsl --install --no-distribution
```

然后重启，再运行 Docker Desktop 安装包。
</details>

### 我是 macOS 用户

两种选择任选其一：

**方案 1 · OrbStack（推荐，更省资源）**

1. 打开 <https://orbstack.dev/>
2. 下载并安装（Apple 芯片选 Apple Silicon 版，Intel 芯片选 Intel 版）
3. 打开 OrbStack，等它启动完成

**方案 2 · Docker Desktop**

1. 打开 <https://www.docker.com/products/docker-desktop/>
2. 下载 Mac 版（同样按芯片选）
3. 安装并启动

在终端验证：

```bash
docker --version
docker compose version
```

<details>
<summary><b>常见问题：提示 "Cannot connect to the Docker daemon"</b></summary>

Docker 没启动完。**等 30 秒再试**；OrbStack 首次启动要初始化虚拟机，慢的话等 1–2 分钟。
</details>

<details>
<summary><b>常见问题：命令找不到（zsh: command not found: docker）</b></summary>

OrbStack 的命令行工具不在 PATH 里。执行：

```bash
echo 'export PATH="$HOME/.orbstack/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

Docker Desktop 用户忽略这条。
</details>

### 我是 Linux 用户

**方式 1 · 官方 Docker Engine（推荐，CentOS / Ubuntu / Debian 通用）**

打开 Docker 官方安装文档，按你的发行版选对应命令：
**<https://docs.docker.com/engine/install/>**

装完验证：

```bash
sudo systemctl enable --now docker
docker --version
docker compose version
```

**方式 2 · Docker Desktop**

下载地址同Windows / macOS：<https://www.docker.com/products/docker-desktop/>

**把当前用户加进 docker 组（避免每次都 sudo）**

```bash
sudo usermod -aG docker $USER
newgrp docker      # 或注销后重新登录
docker run hello-world   # 验证免sudo 可用
```

<details>
<summary><b>常见问题：<code>docker compose</code> 报 "command not found"</b></summary>

Compose 没装。Ubuntu/Debian 可直接装：

```bash
sudo apt-get install -y docker-compose-plugin
```
</details>

---

## 第 2 步 · 下载本项目

进入你想放项目的目录，然后：

```bash
git clone <repo-url> askoda
cd askoda
```

> **不想用 Git？** 在 GitHub 页面点 **Code → Download ZIP**，解压后进入解压出来的文件夹，
> 后续步骤完全一样（少了 `cd askoda` 这步）。
>
> <b>注意：</b>解压后要进入<b>项目根目录</b>——就是能看到 `docker-compose.yml` 的那一层，
> 别进到 `askoda-1.0.0` 里面的子目录。

**确认你进对目录了：**

```bash
ls docker-compose.yml .env.example
```

能看到这两个文件就对了。

---

## 第 3 步 · 填配置

### 3.1 复制配置样例

**Windows（PowerShell）：**

```powershell
Copy-Item .env.example .env
```

**macOS / Linux：**

```bash
cp .env.example .env
```

### 3.2 填两项必填

用**任意文本编辑器**打开 `.env`（记事本、VS Code 都行），找到这两行：

```ini
KNOWLEDGE_API_KEY=
KNOWLEDGE_DATASET_ID=
```

**如果暂时没装 RAGFlow**：这两行**留空**，但网关启动会报错，需要按 3.3 处理。

**如果装了 RAGFlow**：在 RAGFlow 界面建数据集、生成 API Key，把两个值填进去（必须来自同一个数据集）。

### 3.3 只想先看界面、不装 RAGFlow 怎么办

把 `.env` 里这一行：

```ini
KNOWLEDGE_API_URL=http://host.docker.internal:19380/api/v1
```

改成：

```ini
KNOWLEDGE_API_URL=http://127.0.0.1:19380/api/v1
```

这样能通过启动检查。**代价是知识检索不可用**，其余功能（需求分析、SQL 生成、门禁、审计）全部正常。

<p align="center">

⚠️ <b>这一步只是让你快速看到界面。</b>真正要用知识检索，还是得装 RAGFlow。

</p>

---

## 第 4 步 · 启动

在项目根目录执行：

```bash
docker compose up -d --build
```

**这一步会做什么**：下载基础镜像、构建本项目镜像、创建数据库、启动 8 个服务。
首次耗时 3–10 分钟（取决于网速），之后只要 10 秒左右。

**等它跑完**，然后检查状态：

```bash
docker compose ps
```

**看到所有服务都是 `healthy` 或 `running` 就算成功。**

<details>
<summary><b>第一次启动要等 1–2 分钟，Wren 那两个服务可能先显示 starting</b></summary>

正常现象——语义引擎冷启动要 30–60 秒。
</details>

<p align="center">

🎉 <b>打开浏览器访问：<code>http://127.0.0.1:18081/</code></b>

</p>

---

## 第 5 步 · 首次初始化

新机器上数据库是空的，**不做这步，界面能开但分析结果是空的**。

### 5.1 一条命令完成初始化

```bash
bash tools/init_first_run.sh
```

<p align="center">

💡 <b>这条命令不需要装 Python。</b>脚本会自动在网关容器内执行——
依赖都已在镜像里装好了。首次部署强烈建议就用这个方式。

</p>

**看到这段就是成功了：**

```
==> ① 采集元数据（表结构 / 字段说明）
[A] 一致性自检：MDL 可见字段 29 个，物理缺失 0 个 ✅
[B] 一致性自检：MDL 可见字段 48 个，物理缺失 0 个 ✅
  ✅ 元数据采集完成
```

**其他可选参数：**

```bash
# 连知识文档一起导入（需已部署 RAGFlow）
bash tools/init_first_run.sh --with-knowledge

# 全部做（含业务口径词条）
bash tools/init_first_run.sh --all
```

> **Windows 用户**：这条是 bash 脚本，原生 PowerShell 跑不了。两个办法——
>① 用 WSL2（推荐，见 1.1）；② 直接在容器里执行：
> ```powershell
> docker exec -e PYTHONPATH=/app:/app/gateway `
>   -e ASSISTANT_DB_DSN=postgresql://assistant:assistant@host.docker.internal:15434/assistant `
>   askoda sh -c "cd /app && python tools/collect_metadata.py --dataset A --dataset B --seed-glossary"
> ```

### 5.2 手工方式（不想用脚本才需要）

先装 Python：

**Windows：** 官网下载 <https://www.python.org/downloads/>，安装时**务必勾选 `Add Python to PATH`**
**macOS：** `brew install python`
**Linux：** `sudo apt-get install -y python3 python3-pip`

再装依赖并运行：

```bash
pip install "psycopg[binary]" "minio>=7.2" "sqlglot>=25.0"

python3 tools/collect_metadata.py --dataset A --dataset B --seed-glossary
python3 tools/ingest_knowledge.py      # 导入知识文档（需 RAGFlow）
python3 tools/seed_glossary.py --apply # 业务口径（可选）
```

> **Windows 用户把 `python3` 换成 `python`。**
>
> 报「未安装 psycopg」= 上面那步 `pip install` 没做。

---

## 第 6 步 · 验证跑通了

### 6.1 三条命令自检

**① 整体健康**

```bash
curl -s http://127.0.0.1:18081/api/v1/health
```

Windows PowerShell 里 `curl` 有歧义，用：
```powershell
Invoke-RestMethod http://127.0.0.1:18081/api/v1/health | ConvertTo-Json -Depth 5
```

看到 `"status": "ok"` 即正常。

**② 页面能打开**

浏览器访问 `http://127.0.0.1:18081/`，五个菜单（需求受理 / 语义分析 / SQL 生成 / 交付 / 审计）都能点开。

**③ 跑一次完整业务流**

在「需求受理」页提交一条：**各门店本月坪效**

依次看：需求单号 → 语义分析结果 → 口径确认 → 生成的 SQL → 执行结果 → 审计回放。

**这六步都走通 = 部署成功。**

### 6.2 可选：跑工程自检

```bash
python3 tools/gate_all.py --layers G0,G1
```

预期输出末尾：`总判定：✅ 全绿（60 项全部 PASS）`

---

## 常见问题

### 启动阶段

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `docker` 命令找不到 | Docker 没装好或没重启 | 回[第 1 步](#第-1-步--装docker三平台分开) |
| `Cannot connect to the Docker daemon` | Docker 没启动完 | 等 30 秒；OrbStack 首次启动可能要 1–2 分钟 |
| `port is already allocated` | 端口被别的程序占了 | 见下方「端口冲突」 |
| 报「未配置知识库 API Key」 | `.env` 两项必填没填 | 见 3.2，或按 3.3 先绕过 |
| 报 `external volume not found` | 数据卷残留 | 见下方「数据卷问题」 |
| `wren-mcp-a` 一直 starting | 语义引擎冷启动慢 | 等 1–2 分钟；`docker compose logs wren-mcp-a` 看进度 |

### 端口冲突

先查是谁占的：

**macOS / Linux：**
```bash
lsof -i :18081
```

**Windows：**
```powershell
netstat -ano | findstr :18081
```

改 `.env` 里的端口（**只改这里，不要改 `docker-compose.yml`**）：

```ini
BFF_HOST_PORT=18083
GATEWAY_HOST_PORT=18082
ASSISTANT_PG_PORT=15436
ASSISTANT_MINIO_PORT=19003
ASSISTANT_MINIO_CONSOLE_PORT=19004
```

改完重新启动，**访问地址也跟着改**：`http://127.0.0.1:18083/`

### 数据卷问题

新机器上不存在旧数据卷会报这个错。**最简单的处理是让 Docker 自动建卷**——编辑
`docker-compose.yml`，把 `external: true` 改成 `external: false`；或在 `.env` 里把
`WREN_PG_VOLUME` / `WREN_PG_VOLUME_B` 换成新名字。

### 改了代码不生效

**必须重新构建，不能只重启：**

```bash
docker compose up -d --build gateway bff
```

❌ `docker compose restart gateway` ——这只是用旧镜像重起进程，代码还是旧的。

### macOS 上 build 报 "operation not permitted"

```
failed to update builder last activity time ... operation not permitted
```

**这是 OrbStack 的噪音，不影响结果。** 判断成功与否看输出里有没有 `Built` 这行。

### 运行阶段

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 页面空白 | 前端产物没构建 | `cd web && npm install && npm run build && cd ..` 再 `docker compose restart bff` |
| 页面 404 / 白屏 | 端口变了但还用旧地址 | 用新端口访问 |
| 提交需求报「拿不到表结构」 | 初始化没跑 | 回[第 5 步](#第-5-步--首次初始化) |
| 知识检索一直是 0 条 | RAGFlow 或 embedding 不可用 | 看 `docker compose logs` 里有没有 `EmbeddingError` |
| 知识检索报找不到 API Key | `.env` 没填 | 回[3.2](#32-填两项必填) |

### 知识检索全断（重要）

**RAGFlow 每次检索都现场调 embedding，不缓存**，所以 embedding 一挂，检索就全断
而且**不报错**，只表现为「明明有文档却一条搜不到」。

排查顺序（别一上来就重启服务）：

1. 在跑 embedding 的那台机器上直接 `curl` 试一下
2. 看容器日志里有没有真实请求打点
3. 查端口映射、查防火墙
4. 最后才重启

`docker inspect` 显示 `Running=true` **只能证明进程在，不能证明服务能用**。
另外 `ping` 通不代表能用——延迟高到一定程度（实测抖到过 453ms）一样会超时。

### 三平台差异速查

| | Windows | macOS | Linux |
|---|---|---|---|
| 装 Docker | Docker Desktop（含 WSL2） | OrbStack（推荐）或 Docker Desktop | Docker Engine |
| 装 Python | 官网下载，**必勾 Add to PATH** | `brew install python` | `apt-get install python3` |
| 跑 python 脚本 | `python xxx.py` | `python3 xxx.py` | `python3 xxx.py` |
| 路径分隔符 | `\` | `/` | `/` |
| shell 脚本（`tools/*.sh`） | 需 WSL2 或 Git Bash | 直接跑 | 直接跑 |
| 换端口 | 同（改 `.env`） | 同（改 `.env`） | 同（改 `.env`） |

> **Windows 用户注意**：项目里的 `tools/*.sh` 是 bash 脚本，**Windows 上跑不了**。
> 请在 WSL2 里操作，或直接跳过（这些脚本只用于 embedding 主备切换，不影响主流程）。

---

## 附录 A · 开源软件清单

**下面这些都会被 `docker compose up -d --build` 自动下载，不需要你手动装。**

### A.1 Python 库（装在项目镜像里，代码 import 使用）

| 名称 | 用途 | 仓库 | 许可证 | 版本 |
|---|---|---|---|---|
| FastMCP | MCP 服务框架 | <https://github.com/jlowin/fastmcp> | MIT | 4.0.10 |
| Starlette | 异步 Web 框架 | <https://github.com/encode/starlette> | BSD | ≥0.47 |
| psycopg | PostgreSQL 驱动 | <https://github.com/psycopg/psycopg> | LGPL-3.0 | ≥3.2 |
| MinIO Python SDK | 对象存储客户端 | <https://github.com/minio/minio-py> | Apache-2.0 | ≥7.2 |
| sqlglot | SQL 解析（静态检查用） | <https://github.com/tobymao/sqlglot> | MIT | ≥25.0 |
| FastAPI | BFF 框架 | <https://github.com/fastapi/fastapi> | MIT | ≥0.115 |
| Uvicorn | ASGI 服务器 | <https://github.com/encode/uvicorn> | BSD | ≥0.30 |
| HTTPX | HTTP 客户端 | <https://github.com/encode/httpx> | BSD | ≥0.27 |

> 完整清单见 `gateway/requirements.txt` 与 `bff/requirements.txt`。

### A.2 需要拉取镜像的开源软件

`docker compose up` 时自动拉取，**无需手动下载**：

| 名称 | 用途 | 镜像地址 | 仓库 | 许可证 |
|---|---|---|---|---|
| Wren Engine | 语义层（把数据库表变成可查的业务语义） | `ghcr.io/canner/wren-engine-ibis:latest` | <https://github.com/Canner/wren-engine> | Apache-2.0 |
| PostgreSQL | 数据库（3 个实例） | `postgres:15-alpine` | <https://github.com/postgres/postgres> | PostgreSQL License |
| MinIO | 对象存储（存附件） | `minio/minio:RELEASE.2024-11-07T00-52-20Z` | <https://github.com/minio/minio> | AGPL-3.0 |
| TEI | 本机 embedding，**默认不装** | `infiniflow/text-embeddings-inference:cpu-1.8` | <https://github.com/huggingface/text-embeddings-inference> | Apache-2.0 |

### A.3 需要你自己另外装的

| 名称 | 用途 | 是否必须 | 仓库 / 下载 |
|---|---|---|---|
| **Docker Desktop / OrbStack** | 跑容器的平台 | ✅ **必须** | <https://www.docker.com/products/docker-desktop/> · <https://orbstack.dev/> |
| **RAGFlow** | 知识库底座 | ❌ 不装也能跑 | <https://github.com/infiniflow/ragflow> |
| Git | 下载项目 | ❌ 可用 ZIP 代替 | <https://git-scm.com/> |
| Python | 跑初始化脚本 | ⚠️ 首次初始化需要 | <https://www.python.org/> |
| Node.js | 只有改前端才需要 | ❌ | <https://nodejs.org/> |

### A.4 关于 RAGFlow 的重要说明

<p align="center">

⚠️ <b>RAGFlow 不在本项目仓库里，需要你按官方文档单独部署。</b>

</p>

它**自带一整套依赖**（MySQL、Elasticsearch、MinIO、Redis），内存占用约 8 GB。
部署指引：<https://github.com/infiniflow/ragflow>

**RAGFlow 与本项目通过 HTTP API 相连**，不共享数据库、不共享容器，互不影响。

---

## 附录 B · 命令速查

### 日常

```bash
# 看状态
docker compose ps

# 看日志（排错第一件事）
docker compose logs -f gateway
docker compose logs -f bff

# 只重启某个服务
docker compose restart gateway

# 停止（数据保留）
docker compose down

# 停止并删数据（⚠️ 不可逆）
docker compose down -v
```

### 改了代码

```bash
docker compose up -d --build gateway bff
```

### 备份数据

```bash
mkdir -p backup
docker exec wren-postgres      pg_dump -U test     test    > backup/a.sql
docker exec wren-postgres-b    pg_dump -U test     retail  > backup/b.sql
docker exec askoda-postgres    pg_dump -U assistant assistant > backup/meta.sql
```

### 健康检查

```bash
curl -s http://127.0.0.1:18081/api/v1/health | python3 -m json.tool
curl -s http://127.0.0.1:18080/healthz | python3 -m json.tool
```

### 本机 embedding（备用，需要时才开）

```bash
# 启动
docker compose --profile local-embed up -d tei-embedding

# 验证（应返回 200）
curl -s -m 10 -X POST http://127.0.0.1:18002/embed \
  -H 'Content-Type: application/json' -d '{"inputs":["ok"]}' | head -c 120
```

> 之后在 RAGFlow 界面（模型管理 → embedding 模型）把地址改成
> `http://host.docker.internal:18002/v1`。

### 工程自检

```bash
python3 tools/gate_all.py --layers G0,G1   # 日常，60 项
python3 tools/gate_all.py                  # 全量，64 项（会真实连库）
```

### 首次初始化（等价于第 5 步）

```bash
bash tools/init_first_run.sh                 # 元数据采集
bash tools/init_first_run.sh --all# 含知识文档 + 业务口径
```

---

## 相关文档

- [README](../README.md) — 这个系统是干什么的
- [MCP 工具参考](MCP_TOOLS.md) — 48 个工具怎么调
- [验证与自证](VERIFICATION.md) — 门禁体系详解
- [外部依赖与限制](DEPENDENCIES.md) — embedding 等依赖详解
- [演示指引](DEMO_GUIDE.md) — 完整的九步演示流程
- [术语表](GLOSSARY.md) — 名词解释

<p align="center">

文档与实际不符时，<b>以实际运行为准</b>。

</p>