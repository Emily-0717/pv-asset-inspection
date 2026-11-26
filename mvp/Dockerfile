# 晨曦光伏 · 知识增强分流 MVP —— 单容器部署
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    SOLAR_MVP_CONFIG=/app/config.yaml

WORKDIR /app

# 依赖单独一层，代码改动不会触发重装
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY config.yaml run.py ./

# SQLite 落在容器内 ./data；要持久化就挂卷到 /app/data
RUN mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health')" || exit 1

CMD ["python", "run.py"]
