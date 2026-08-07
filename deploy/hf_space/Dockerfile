# ============================================================
# Hugging Face Space · Docker 部署镜像（海外免费托管）
# 由 qiang26/cangyue 一键部署脚本生成，也可手动使用。
#
# 运行时需在 Space Settings → Variables and secrets 配置：
#   TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
# 可选：
#   TELEGRAM_API_BASE / HF_POLL_INTERVAL / HF_TOP
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# 1) 先装依赖（利用构建缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) 复制项目源码与数据
COPY src ./src
COPY scripts ./scripts
COPY data ./data
COPY config ./config
COPY .env.example .

# 3) HF Space 入口（web 健康检查 + v1.1 信号守护线程）
COPY deploy/hf_space/app.py ./app.py

# HF Spaces 默认监听端口（由平台注入 $PORT，默认 7860）
ENV PORT=7860
EXPOSE 7860

# 启动：app.py 内同时运行 Web 服务与信号轮询守护线程
CMD ["python", "app.py"]
