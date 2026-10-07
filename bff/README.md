# 数据需求智能分析助手 · 统一前端与 BFF

业务用户的**唯一门面**。前端只调本目录的 BFF，BFF 只经 MCP 网关触达后端能力。

调用链：`浏览器 → BFF(:18081) → MCP 网关 → 各层能力`
BFF **不直连** Wren（9000/9002）、不直连业务 PG。

## 1. 起 BFF

**推荐：走 compose**（连网关一起起，一条命令拿到完整应用，见第 6 节）：

```bash
docker compose up -d bff
```

**本机手工起**（调试用）：

```bash
cd bff
pip install -r requirements.txt
python serve.py                      # 默认 127.0.0.1:18081
```

网关端点默认 `http://127.0.0.1:18080/mcp`，可用环境变量覆盖：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ASKODA_GATEWAY_URL` | `http://127.0.0.1:18080/mcp` | MCP 网关端点。**只在 BFF 侧配置** |
| `ASKODA_BFF_PORT` | `18081` | BFF 监听端口（避开已占用端口） |
| `ASKODA_MCP_TIMEOUT` | `180` | 单次 MCP 调用超时（秒）。SQL 链路是长任务 |
| `ASKODA_STATIC_DIR` | `bff/static` | 前端产物目录 |
| `ASKODA_SERVE_STATIC` | `1` | 是否由 BFF 同源托管前端产物 |

验证：

```bash
curl -s http://127.0.0.1:18081/api/v1/health
open http://127.0.0.1:18081/docs        # OpenAPI 契约
```

## 2. 构建前端

```bash
cd web
npm install
npm run build          # 产物直接输出到 ../bff/static
```

BFF 启动时若检测到 `bff/static/` 存在，会自动同源托管；打开 `http://127.0.0.1:18081/` 即是完整应用。

### 前端联调（可选，带热更新）

```bash
cd web && npm run dev        # 监听 18000，/api 代理到 18081
```

## 3. 接口清单（统一前缀 `/api/v1`，契约版本化）

| 方法 | 路径 | 对应 MCP 工具 |
| --- | --- | --- |
| GET | `/api/v1/demand/{id}/e2e-status` | **聚合**：demand_get + analysis_get + confirmation_list + requirement_get + sql_context_pack + sql_run_list |
| POST | `/api/v1/demand` | demand_create |
| POST | `/api/v1/demand/similar-precheck` | demand_similar_precheck |
| POST | `/api/v1/demand/{id}/analyze` | analysis_first_round |
| POST | `/api/v1/demand/{id}/confirm` | confirmation_answer **+ 自动重跑analysis** |
| POST | `/api/v1/demand/{id}/sql` | requirement_structured + sql_generate |
| POST | `/api/v1/demand/{id}/sql/execute` | sql_execute_readonly |
| GET | `/api/v1/demand/{id}/citations` | knowledge_citation_list |
| GET | `/api/v1/datasets` | datasets |
| GET | `/api/v1/health` | gateway_health |
| GET | `/api/v1/tools` | tools/list（前端自解释排查用） |

统一错误体：`{code, message, hint}`。`hint` 必填，写「下一步该干什么」——
任何硬门禁拒绝都要说明「为什么拒绝 + 去哪里治理」，不允许只报错不指路。

## 4. 契约生成前端类型（禁止手写接口类型）

契约是唯一事实来源：

```bash
# 1) 起BFF 后导出契约
curl -s http://127.0.0.1:18081/openapi.json -o web/schemas/openapi.json
# 2) 生成类型
cd web && npm run gen:api       # 产出 src/api/schema.ts
```

## 6. 容器化（已交付）

BFF 已纳入`docker-compose.yml`，服务名 `bff`，与网关同栈：

```bash
docker compose up -d bff      # 交付形态：正常模式，无reload
curl http://127.0.0.1:18081/api/v1/health   # 200
```

| 项 | 值 |
| --- | --- |
| 镜像 | `askoda-bff:0.1.0`（`bff/Dockerfile`，基础镜像 `python:3.13-slim`） |
| 端口 | 容器 `18081` → 宿主机 `18081`（`.env` 里 `BFF_HOST_PORT` 可改） |
| 容器内网关地址 | `ASKODA_GATEWAY_URL=http://gateway:8080/mcp`（**走服务名**；容器内 `127.0.0.1` 指向自己，不是网关） |
| 前端产物 | 随镜像走（`COPY . ./` 含 `static/`），由 BFF 同源托管 |
| 启动命令 | `uvicorn app:app --host 0.0.0.0 --port 18081` |

**为什么不用 `python serve.py`**：`serve.py` 面向宿主手工起，`host` 写死 `127.0.0.1`
且 `reload` 写死 `False`（见 `bff/serve.py:22-26`）；容器内必须监听 `0.0.0.0`。
镜像里改用等价的 uvicorn 命令行，参数与 `serve.py` 保持一致。
`serve.py` 本身**仍然可用**（本机手工调试不受影响）。

开发期热重载见根目录 `README.md`「开发期热重载」与 `docker-compose.dev.yml`
文件头注释（含 gateway 为何只能 restart 的实测原因）。

> ⚠️ **当前无鉴权，仅限本机访问。** `18081` 上的23 个接口与前端应用都没有
> 访问控制，不要暴露到公网或局域网。

## 7. 红线约束（自检已跑通）

- **R2**：前端只出现相对路径 `/api/v1/...`；网关地址只存在于 `bff/config.py`。
  自检命令（应无输出）：
  ```bash
  grep -rnoE 'localhost:(9000|9001|9002|9003|18080)|127\.0\.0\.1:(9000|9001|9002|9003|18080)|:900[0-3]\b|18080|wren-mcp|wren-postgres|assistant-minio|assistant-postgres|host\.docker\.internal|ragflow' \
    web/src web/index.html web/vite.config.ts bff/static
  ```
- **出参脱敏**：`bff/sanitize.py` 按键删除 + 按值兜底，剥掉网关返回里的
  `wren_endpoint` / `data_source` / `endpoint` 等内部地址，避免透传到浏览器。
- **R3/R4**：不 fork、不改开源源码，无 UI 补丁 / DOM 注入 / 产物替换。
- **R5**：BFF 依赖只写进 `bff/requirements.txt`，不动 `gateway/requirements.txt`。