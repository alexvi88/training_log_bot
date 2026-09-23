# Образ для Fly.io (см. fly.toml и docs/DEPLOY_FLY.md). Amvera собирает бота
# сама из amvera.yaml — этот файл ей не нужен и ничего у неё не меняет.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Шрифты для matplotlib-графиков (кириллица) — в slim-образе их нет.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Litestream — непрерывная репликация SQLite в Tigris (litestream.yml, start.sh).
ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz && rm /tmp/litestream.tar.gz

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# /data — volume Fly (fly.toml [mounts]): база, FSM, медиа, бэкапы. Пути
# по умолчанию в config.py уже смотрят туда, как на Amvera.
RUN chmod +x start.sh && cp litestream.yml /etc/litestream.yml
CMD ["./start.sh"]
