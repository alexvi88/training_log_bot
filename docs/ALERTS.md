# Тревоги

Что само приходит админу (`ADMIN_ID`) в личку бота и что надо один раз
настроить снаружи. Код — `ops_alerts.py`, там же подробности.

## Приходит само

| Что | Когда | Откуда |
|---|---|---|
| 🚨 Ошибка | любая запись уровня ERROR в логе: упавший хендлер, 500 в `/v1`, умершая фоновая задача, `logger.exception` где угодно | `AdminAlertHandler` на корневом логгере |
| 🔁 Повторялось | одинаковые ошибки после первой — одной сводкой раз в 15 мин | склейка по типу исключения и месту |
| 🧯 Сбой iOS | первое падение/зависание с новой причиной в сборке | `api_v1_diagnostics` → `maybe_alert_new_diagnostic` |
| 📵 Пуши не доходят | за час ≥5 попыток APNs, и больше половины не доставлено | `run_hourly_checks` |
| 💸 AI на потолке | расход за сутки дошёл до мягкого/жёсткого потолка, раз на ступень за сутки | `run_hourly_checks` |

Зашумело — `fly secrets set -a training-log-bot OPS_ALERTS_ENABLED=false`.
Конкретную запись, о которой код и так пишет админу сам, глушит
`extra={"ops_alert": False}`.

## Бот лёг целиком — настроить один раз

Мёртвый процесс сам ничего не пришлёт, поэтому о его смерти пишет сторонний
сервис. Процесс раз в 5 минут дёргает `HEALTHCHECK_PING_URL`; перестал —
сервис пишет в Telegram.

1. Зарегистрироваться на https://healthchecks.io (бесплатно).
2. **Add Check**: Period — 5 minutes, Grace — 10 minutes. Скопировать ping
   URL вида `https://hc-ping.com/<uuid>`.
3. **Integrations → Telegram → Add Integration**: откроется их бот, нажать
   Start — чек привяжется к твоему чату. Лишние интеграции (email) можно
   выключить.
4. `fly secrets set -a training-log-bot HEALTHCHECK_PING_URL=https://hc-ping.com/<uuid>`
   (секрет перезапускает машину).
5. Через пару минут чек в Healthchecks станет зелёным «up».

Проверить: `fly scale count 0 -a training-log-bot` на 15 минут → придёт
«down» → `fly scale count 1` → «up». Или просто дождаться первого деплоя: он
короче грейса и тревоги не даст.

Опционально — доступность HTTP снаружи (приложение ходит в `/v1`, и пульс
изнутри не заметит, если лёг только вход Fly): UptimeRobot (https://uptimerobot.com): HTTP(s)-монитор на
`https://training-log-bot.fly.dev/v1/health`, интервал 5 мин, alert contact —
Telegram.
