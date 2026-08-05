# fin-alert 应用镜像
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY config/ ./config/
COPY src/ ./src/
COPY data/ ./data/

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8080), 3)" || exit 1

CMD ["python", "-m", "src.main"]
