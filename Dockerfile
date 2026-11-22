FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VOLT_DB_PATH=/data/volt.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
# 单 worker + 多线程：SQLite 文件库配合 WAL 与 busy_timeout 即可满足内网比对场景
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", \
     "--threads", "8", "--timeout", "60", "run:app"]
