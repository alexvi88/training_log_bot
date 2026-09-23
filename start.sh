#!/bin/sh
# Точка входа контейнера на Fly (Dockerfile CMD).
#
# BOT_HOLD=1 — бот НЕ стартует, контейнер просто ждёт. Нужно ровно для
# переезда: volume /data пустой, и запущенный бот успел бы завести в нём
# новую базу (и начать отвечать людям с пустым дневником), а заливка
# настоящей базы поверх открытого SQLite-файла её бы испортила. С BOT_HOLD
# заливаем /data спокойно, затем `fly secrets unset BOT_HOLD` перезапускает
# машину уже с ботом. См. docs/DEPLOY_FLY.md.
if [ "${BOT_HOLD:-}" = "1" ]; then
  echo "BOT_HOLD=1: бот не запущен, ждём заливки /data"
  exec sleep infinity
fi
exec python main.py
