# 本地 LLM token 网关 —— 容器化部署
#
# 用法见 README「Docker 部署」章节，或直接：
#   docker build -t llm-token-gateway .
#   docker run -d -p 8317:8317 -e DASHSCOPE_API_KEY=你的key --name gateway llm-token-gateway
#
# 关键点：
#   * 容器是 Linux，网关主进程用 uvicorn 直接启动 server:app，不碰 Windows 注册表读 key 的逻辑
#     （run_gateway.py 的 winreg 分支只在环境变量为空时才执行，容器里务必用 -e 传 key）。
#   * --host 0.0.0.0：容器内监听 127.0.0.1 宿主机连不上，必须绑 0.0.0.0 再映射端口。

FROM python:3.13-slim

WORKDIR /app

# 先装依赖，利用 Docker 层缓存（代码常改、依赖不常改）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 只复制运行时需要的代码与配置（不带 .venv / logs / 评测脚本）
COPY *.py ./
COPY config.json ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

EXPOSE 8317

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8317"]
