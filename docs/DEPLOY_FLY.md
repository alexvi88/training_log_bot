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

### Демо-аккаунт для App Review

Apple на ревью (Guideline 2.1(a)) требует логин и пароль от аккаунта с
данными. Для этого есть `POST /v1/auth/password` (`review_demo.py`): пока
обе переменные не заданы, маршрут отвечает 404, как будто его нет. Пароль —
длинный случайный, в репозиторий не кладётся:

```sh
PW="$(openssl rand -base64 18)"; echo "$PW"   # запиши сразу: fly secrets list значений не показывает
fly secrets set -a training-log-bot REVIEW_DEMO_USERNAME=appreview REVIEW_DEMO_PASSWORD="$PW"
```

Эти же логин и пароль вписываются в App Store Connect → App Review
Information → Sign-In Information. На первом входе сервер заводит app-only
аккаунт (английский, если клиент не прислал `lang`) и заполняет его историей:
12 тренировок за последние четыре недели, взвешивания, рекорды и значки.
Повторные входы попадают в тот же аккаунт и ничего не дописывают. Сменить
`REVIEW_DEMO_USERNAME` — значит получить новый аккаунт (старый останется в
базе); удалит аккаунт сам ревьюер — следующий вход заведёт и заполнит новый.
Выключить вход — `fly secrets unset -a training-log-bot REVIEW_DEMO_USERNAME
REVIEW_DEMO_PASSWORD`.

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

## Непрерывный бэкап базы (Litestream + Tigris)

Включается одной командой — она создаёт бакет Tigris и сама ставит
приложению секреты `BUCKET_NAME`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION` (машина
перезапустится):

```sh
fly storage create -a training-log-bot
```

С этого момента `start.sh` запускает бота под `litestream replicate`: изменения
базы уходят в бакет через ~1 с. В логах — `litestream ... replicating to`.

Восстановление: если `/data/training_log.db` нет (новый volume), start.sh сам
делает `litestream restore` перед стартом. Вручную, на любой момент времени:

```sh
fly ssh console -a training-log-bot
litestream restore -o /data/restored.db -timestamp 2026-09-24T10:00:00Z /data/training_log.db
```

Ежедневный бэкап админу в Telegram (`admin_tasks.py`) остаётся как был.
