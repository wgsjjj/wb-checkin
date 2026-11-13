# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WB_HOST=0.0.0.0 \
    WB_PORT=8787 \
    WB_DATA_DIR=/data \
    WB_NO_BROWSER=1

WORKDIR /app

# tzdata 必须装，否则容器内是 UTC，定时签到会整体偏移 8 小时
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
 && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY browser_login.py ./
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/api/me',timeout=3)" || exit 1

CMD ["python", "app.py"]
