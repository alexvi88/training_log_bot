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
fly volumes create data --region waw --size 3 -a training-log-bot
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

Нужны файлы из `/data` Amvera: `training_log.db`, `fsm_storage.json`,
папка `media/`. Если Amvera недоступна — последний ежедневный бэкап базы
бот присылает админу в Telegram (`admin_tasks.py`).

Сначала **остановить бота на Amvera** (иначе после копирования он допишет
в старую базу). Затем первый деплой и заливка:

```sh
fly deploy                                   # поднимет пустую базу
fly ssh sftp shell -a training-log-bot       # put training_log.db /data/training_log.db
                                             # put fsm_storage.json /data/fsm_storage.json
fly machine restart -a training-log-bot
fly logs -a training-log-bot                 # ждём "SQLite journal_mode=wal, synchronous=normal"
```

Медиа (`/data/media`) — тем же sftp, или архивом:
`tar czf media.tgz media` → `put media.tgz /data/` → `fly ssh console`
→ `cd /data && tar xzf media.tgz && rm media.tgz`.

## 4. Проверка

- `curl https://training-log-bot.fly.dev/v1/health` → 200.
- Бот отвечает в Telegram.
- В приложении адрес сервера зашит (`TelegramLink.defaultServerURLString`
  в training_log_bot_ios) — его меняем на новый домен и выпускаем сборку.

## 5. После переезда

- Amvera выключить совсем (не просто остановить деплой).
- Автодеплой: в `.github/workflows/fly-deploy.yml` раскомментировать `push`
  и добавить секрет репозитория `FLY_API_TOKEN` (`fly tokens create deploy`).
- Свой домен: `fly certs add api.<домен>` + CNAME на `training-log-bot.fly.dev`,
  затем `MCP_PUBLIC_URL` и адрес в приложении — на домен.
