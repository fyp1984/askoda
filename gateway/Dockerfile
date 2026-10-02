FROM python:3.13-slim

# 依赖安装走国内镜像，避免构建期网络抖动
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

COPY . .

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
