# ⚠ 构建上下文是 orchestration/（不是本目录），见 docker-compose.yml 的 build.context
# 因为 app.py 要 import 同级的 comfy_guard.py，必须一起打进镜像。
FROM python:3.11-slim
WORKDIR /app
# 容器内自带 ffmpeg（含 libass），不依赖宿主机挂载
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pipeline-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pipeline-service/app.py comfy_guard.py ./
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
