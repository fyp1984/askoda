# 本地 embedding 模型（B4b）

## 这里放什么

`bge-m3/` —— BAAI/bge-m3 的 ONNX 权重（2.27GB），供 `infiniflow/text-embeddings-inference:cpu-1.8`
以 `--model-id /models/bge-m3` 加载。**不入 git**（体积过大），由脚本拉取。

## 怎么来的

```bash
./fetch_bge_m3.sh "$PWD/bge-m3"
```

源是 `https://hf-mirror.com/BAAI/bge-m3/resolve/main`（HF 国内镜像）。
本机直连 `huggingface.co` 不通（探测 `http=000`），hf-mirror 可通（`http=200`）。

拉完后校验过每个文件字节数与远端 `Content-Length` 精确一致（9/9 `ALL_MATCH`），
其中 `onnx/model.onnx_data` = 2266820608 字节（2.27GB）。

## 为什么必须是 bge-m3，不能换一个更小的模型

ES 里已入库 chunk 的**向量是当初用 bge-m3 生成的**（1024 维）。RAGFlow 每次检索会
现调 embedding 算查询向量，再与库内向量算余弦。换模型 → 查询向量与库内向量的
空间不一致 → 相似度失去意义、召回**静默变错**。这种错比报错更危险，所以固定 bge-m3。

代价：`onnx/model.onnx_data` 是 2.27GB 的 fp32 权重，加载时内存占用高，
在 OrbStack（amd64 镜像跑 arm64 模拟）里默认参数会被 OOM kill
（实测 `ExitCode=137 OOMKilled=true`）。compose 里把 tokenization workers 降到 2、
max-batch-tokens 降到 2048 后可稳定常驻。**首次拉起约需 2~4 分钟**（模型加载 + warm up）。

## 目录结构

TEI 要求 `tokenizer.json` 在模型**根**目录，而 HF 仓库把它放在 `onnx/` 下，
因此根目录放了一份副本（`fetch_bge_m3.sh` 之后需 `cp` 一次，或直接看 `bge-m3/` 当前状态）：

```
bge-m3/
├── config.json                # HF 根配置
├── tokenizer.json             # ← 从 onnx/ 复制上来的副本（TEI 在根目录找）
├── tokenizer_config.json      # ← 同上
├── special_tokens_map.json    # ← 同上
├── 1_Pooling/config.json
└── onnx/
    ├── model.onnx             # 724 KB（计算图）
    ├── model.onnx_data        # 2.27GB（权重本体）
    ├── tokenizer.json
    ├── sentencepiece.bpe.model
    └── ...
```

## RAGFlow 侧的连接配置

`rag_flow.tenant_model_instance.extra` 里两个 bge-m3 实例的 `base_url` 已从
`http://100.103.240.78:18001/v1`（远端 TEI，已宕）改为
`http://host.docker.internal:18002/v1`（本机 compose 里的 `tei-embedding`）。

RAGFlow 的 `ragflow-cpu` 容器能解析 `host.docker.internal` → `0.250.250.254`
（实测容器内 `/v1/embeddings` 返回 1024 维向量）。

改前备份：`backup/ragflow_embedding_models_before_b4b.sql`（`tenant_model_instance`
+ `tenant_model` 两张表的 mysqldump）。**未触碰 dataset / document / chunk 数据。**

改端口的话要同步改两处：compose 的 `TEI_EMBEDDING_PORT`，以及 MySQL 里
`tenant_model_instance.extra` 的 `base_url`。

## 排障

```bash
# 1. TEI 是否活着
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18002/health   # 期望 200

# 2. 真实 embedding + 维度
curl -s -X POST http://127.0.0.1:18002/v1/embeddings \
  -H 'Content-Type: application/json' \
  -d '{"model":"bge-m3","input":["复购率怎么算"]}' | python3 -c \
  "import json,sys;print('dims =', len(json.load(sys.stdin)['data'][0]['embedding']))"   # 期望 1024

# 3. RAGFlow 容器侧能否访问
docker exec filebay-knowledge-trial-ragflow-cpu-1 python3 -c "
import urllib.request, json
r = urllib.request.Request('http://host.docker.internal:18002/v1/embeddings',
    data=json.dumps({'model':'bge-m3','input':['t']}).encode(),
    headers={'Content-Type':'application/json'})
print('dims =', len(json.loads(urllib.request.urlopen(r, timeout=90).read())['data'][0]['embedding']))"

# 4. OOM 排查
docker inspect askoda-tei-embedding --format '{{.State.OOMKilled}} {{.HostConfig.Memory}}'
docker logs --tail 20 askoda-tei-embedding
```

若 OOM：先把 compose 里 `--tokenization-workers` 继续降到 1、`--max-batch-tokens`
降到 1024；仍不行则 `docker compose stop` 停掉同机不用的容器腾内存（VM 总内存
只有 15.66GiB，当前同时跑着 10+ 个容器）。

`docker logs` 里出现 `Backend does not support a batch size > 8` 是**正常提示**
（onnx 后端上限），TEI 会自动把 `max_batch_requests` 收敛到 8。
