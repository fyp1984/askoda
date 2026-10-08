FROM python:3.13-slim

# 依赖安装走国内镜像，避免构建期网络抖动
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
WORKDIR /app

# ── 构建 context 是**仓库根**（见 docker-compose.yml 的 build.context）──
#   这样才能同时把 gateway/ 与仓库根的 tools/ 带进镜像。容器内需要 tools/
#   才能跑门禁 G2/G3（mcp_acceptance_check / r2_check 等脚本）。
#   若把 context 改回 ./gateway，tools/ 会缺失，每次 build 后门禁都会假红。
COPY gateway/requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

# 业务代码
COPY gateway/ ./
# 门禁脚本（容器内跑 G2/G3 需要）
COPY tools/ ./tools/
#契约文件
COPY gateway/contracts/ ./contracts/

ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8080 \
    GATEWAY_MCP_PATH=/mcp \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# 容器自检：/healthz 必须 200
HEALTHCHECK --interval=15s --timeout=25s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=20).status==200 else 1)"

CMD ["python", "app.py"]
