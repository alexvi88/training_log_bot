# Переезд с Amvera на Fly.io

Бот — один процесс: поллинг Telegram + HTTP `/v1` (приложение) и MCP на
порту 80, SQLite и медиа в `/data`. Поэтому на Fly — одна машина с volume,
без автостопа (`fly.toml`). **Два бота одновременно запускать нельзя**: оба
будут забирать апдейты Telegram (409 Conflict) и писать в разные базы.

## 1. Разово: приложение и volume

```sh
brew install flyctl          # или: curl -L https://fly.io/install.sh | sh
fly auth login
fly apps create training-log-bot          # имя должно совпасть с app в fly.toml
fly volumes create data --region fra --size 3 -a training-log-bot
```

## 2. Секреты

Те же переменные, что в Amvera (панель → Переменные). Задаются одной
командой, в репозиторий не попадают:

```sh
fly secrets set -a training-log-bot \
  TG_TOKEN=... ADMIN_ID=... \
  XAI_API_KEY=... OPENAI_API_KEY=... NOVITA_API_KEY=... \
  APNS_KEY_ID=... APNS_TEAM_ID=... APNS_ENV=production \
  MCP_PUBLIC_URL=https://training-log-bot.fly.dev
fly secrets set -a training-log-bot APNS_KEY_P8="$(cat AuthKey_XXXX.p8)"
```

Всё, что в Amvera задано сверх этого (лимиты `AI_*`, `ENGAGEMENT_*` и т.п.),
переносится так же. `MCP_PUBLIC_URL` — без него HTTP-сервер не поднимается
(`config.mcp_available`), а приложению он нужен. Когда будет свой домен —
поменять на него.

## 3. База

1. **Остановить бота на Amvera** (кнопка паузы вверху справа) — иначе после
   копирования он допишет в старую базу, и два бота подерутся за Telegram.
2. Amvera → Репозиторий → Data → «Скачать данные». Распаковать, из папки с
   `training_log.db` собрать архив (без `backups/` — он не нужен):
   `tar czf ~/data.tgz training_log.db training_log.db-wal training_log.db-shm fsm_storage.json media`
   (каких-то `-wal`/`-shm` может не быть — тогда просто без них).
3. Первый деплой **с придержанным ботом** — контейнер поднимется, но бот
   не стартует (start.sh):
   ```sh
   fly secrets set BOT_HOLD=1 -a training-log-bot --stage
   fly deploy
   ```
4. Залить и распаковать:
   ```sh
   fly ssh sftp shell -a training-log-bot     # put /Users/<ты>/data.tgz /data/data.tgz, затем exit
   fly ssh console -a training-log-bot -C "sh -c 'cd /data && tar xzf data.tgz && rm data.tgz && ls -la'"
   ```
5. Отпустить бота: `fly secrets unset BOT_HOLD -a training-log-bot` —
   машина перезапустится уже с ботом. `fly logs -a training-log-bot` —
   ждём `SQLite journal_mode=wal, synchronous=normal`.

## 4. Проверка

- `curl https://training-log-bot.fly.dev/v1/health` → 200.
- Бот отвечает в Telegram.
- В приложении адрес сервера зашит (`TelegramLink.defaultServerURLString`
  в training_log_bot_ios) — его меняем на новый домен и выпускаем сборку.

## 5. После переезда

- Amvera выключить совсем (не просто остановить деплой).
- Автодеплой включён (`.github/workflows/fly-deploy.yml`, push в main);
  и добавить секрет репозитория `FLY_API_TOKEN` (`fly tokens create deploy`).
- Свой домен: `fly certs add api.<домен>` + CNAME на `training-log-bot.fly.dev`,
  затем `MCP_PUBLIC_URL` и адрес в приложении — на домен.

## Грабли переезда

Заморозка проекта на Amvera **не выключает его автодеплой из GitHub**:
первый же мерж в main после переезда собрал и запустил бота на Amvera
снова, и два процесса одновременно забирали апдейты Telegram. Перед
переездом — отвязать репозиторий в Amvera (или удалить приложение), а не
только заморозить.
