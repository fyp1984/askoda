# BFF（Backend for Frontend）镜像
#
# 为什么单独一个镜像，而不是并进 gateway
# --------------------------------------
# 红线 R5：BFF 依赖与 gateway/requirements.txt 相互独立，两边不互相污染。
# 且 BFF 是纯 HTTP 服务、gateway 是 FastMCP 服务，启动方式与热重载能力都不同，
# 合成一个镜像会让「改 BFF 不用重建 gateway」这件事做不到。
#
# 前端产物为什么随镜像走（B 方案）
# ------------------------------
# 前端构建产物固定输出到 bff/static/（见 web/vite.config.ts），
# 由 BFF 同源托管——浏览器只认识相对路径 /api/v1/...（红线 R2），
# 所以镜像即交付物：拿到镜像就有了完整应用，不需要再单独部署前端。
# 代价是改前端要重新 build 镜像，见 README「快速上手」。
FROM python:3.13-slim

# 依赖安装走国内镜像，避免构建期网络抖动（与 gateway/Dockerfile 一致）
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -i ${PIP_INDEX} -r requirements.txt

# 源码 + 前端产物（bff/static/）一起进镜像。
# bff/static/ 是 web/ 的构建输出，已随仓库入库（见 bff/README.md 第 2 节），
# 故无需在镜像内跑 npm build。
COPY . ./

ENV PYTHONUNBUFFERED=1 \
    ASKODA_BFF_PORT=18081 \
    ASKODA_SERVE_STATIC=1

EXPOSE 18081

# 容器自检：/api/v1/health 必须 200。
# 该接口是网关健康透传，故这条检查同时验证了「BFF→网关」链路通。
HEALTHCHECK --interval=15s --timeout=25s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18081/api/v1/health', timeout=20).status==200 else 1)"

# 不用 `python serve.py`：serve.py 面向宿主手工起，host 写死 127.0.0.1
# 且 reload 写死 False（见 bff/serve.py:22-26）。容器里必须监听 0.0.0.0，
# 所以直接用 uvicorn 命令行，参数与 serve.py 保持一致（app:app / 同端口）。
# 开发期热重载见 docker-compose.dev.yml 的 command 覆盖（加 --reload）。
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18081"]