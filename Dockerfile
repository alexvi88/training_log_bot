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

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# /data — volume Fly (fly.toml [mounts]): база, FSM, медиа, бэкапы. Пути
# по умолчанию в config.py уже смотрят туда, как на Amvera.
CMD ["python", "main.py"]
