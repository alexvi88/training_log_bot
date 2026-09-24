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

# Litestream (litestream.yml): если хранилище подключено (`fly storage create`
# ставит BUCKET_NAME и AWS_*), бот работает под `litestream replicate` —
# каждое изменение базы уходит в Tigris через секунду, а не раз в сутки.
# Базы на диске нет (новый/умерший volume) — сначала восстанавливаем её
# из реплики, если реплика есть. Без BUCKET_NAME — как раньше.
# Одна строка в лог при каждом старте: иначе по `fly logs` не понять, идёт
# ли бэкап вообще, — без хранилища Litestream молчит, а бот работает как ни
# в чём не бывало.
if [ -n "${BUCKET_NAME:-}" ]; then
  echo "litestream: бэкап базы включён, реплика в Tigris"
  if [ ! -f /data/training_log.db ]; then
    echo "Базы нет на диске — восстанавливаю из Tigris, если там есть реплика"
    litestream restore -if-replica-exists /data/training_log.db
  fi
  exec litestream replicate -exec "python main.py"
fi
echo "litestream: BUCKET_NAME не задан — бэкап базы ВЫКЛЮЧЕН"
exec python main.py
