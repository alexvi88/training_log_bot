# MCP-коннектор в публичных каталогах

Как «Athlete Diary» попадает в каталоги AI-коннекторов: официальный MCP Registry,
каталог коннекторов Claude, каталог приложений ChatGPT, Glama, Smithery, PulseMCP,
mcp.so. Здесь — требования (сверены 16.10.2026, источники в конце), что уже сделано в
коде, тексты для листингов и пошаговые действия владельца по каждому каталогу.

Код: `mcp_server.py` (инструменты, аннотации, имя и иконка), `mcp_oauth.py` (OAuth,
страница согласия, `/mcp/demo-code` для ревьюеров), `mcp_wellknown.py` (файлы
подтверждения домена, карточка сервера), `legal/connect.*.html` (публичная инструкция
`/connect`), `server.json` в корне (запись для MCP Registry). Тесты —
`tests/test_mcp_listing.py`.

## Главное одной таблицей

| | Адрес |
|---|---|
| Коннектор (MCP, streamable HTTP) | `https://athletediary.app/mcp` |
| OAuth (issuer) | `https://athletediary.app` — `/authorize`, `/token`, `/register` (DCR), `/revoke` |
| Метаданные | `/.well-known/oauth-authorization-server`, `/.well-known/oauth-protected-resource/mcp` (и без `/mcp`) |
| Инструкция (documentation URL) | `https://athletediary.app/connect` (`?lang=ru` — по-русски) |
| Политика конфиденциальности | `https://athletediary.app/privacy` |
| Условия | `https://athletediary.app/terms` |
| Поддержка | `https://athletediary.app/support`, `athletediary.app@gmail.com` |
| Иконка | `https://athletediary.app/site/icons/icon-512.png` (512×512, PNG, иконка приложения) |
| Код для ревьюеров | `https://athletediary.app/mcp/demo-code` (логин/пароль демо-аккаунта каталогов, `CATALOG_DEMO_*`) |
| Карточка сервера | `https://athletediary.app/.well-known/mcp/server-card.json` |

Старый адрес `https://training-log-bot.fly.dev/mcp` тем же процессом отвечает дальше
(выданные там токены работают), но метаданные OAuth везде называют athletediary.app —
новые подключения и все листинги идут только на свой домен.

**Не перенаправлять** (координация с SEO-редиректами fly.dev → athletediary.app):
`/mcp`, `/mcp/*`, `/.well-known/*`, `/authorize`, `/token`, `/register`, `/revoke`,
`/v1/*`, `/coach/*`. Обработчики OAuth в SDK висят на корне, не под `/oauth*`: 301 на
`/token` или `/register` ломает подключение клиентам, которые не повторяют POST после
редиректа.

## Чек-лист требований

| # | Требование | Кто требует | Статус |
|---|---|---|---|
| 1 | Удалённый сервер по HTTPS, streamable HTTP | Claude, ChatGPT, Registry, Smithery | ✅ `https://athletediary.app/mcp` |
| 2 | OAuth 2.0/2.1 с PKCE S256, `code_challenge_methods_supported` в метаданных | Claude, ChatGPT | ✅ SDK |
| 3 | 401 с `WWW-Authenticate: Bearer resource_metadata=…` без токена | Claude | ✅ тест `test_unauthenticated_call_gets_401_…` |
| 4 | `resource` в protected-resource metadata = URL коннектора, первый `authorization_servers` — наш issuer | Claude | ✅ тест `test_oauth_metadata_points_at_the_canonical_host` |
| 5 | DCR (`registration_endpoint`) или CIMD | Claude, ChatGPT | ✅ DCR. CIMD серверная часть SDK не умеет — см. «Риски» |
| 6 | Redirect `https://claude.ai/api/mcp/auth_callback` и loopback Claude Code | Claude | ✅ любой https и loopback принимаются при регистрации |
| 7 | Ротация refresh-токенов, `invalid_grant` на мёртвый refresh | Claude | ✅ `exchange_refresh_token` |
| 8 | `/token` принимает `application/x-www-form-urlencoded` | Claude | ✅ SDK |
| 9 | Ответ OAuth-эндпоинтов быстрее 10 с | Claude | ✅ SQLite, без внешних вызовов |
| 10 | У каждого инструмента `title` + `readOnlyHint`/`destructiveHint` | Claude | ✅ `mcp_server.TOOL_ANNOTATIONS`, тест |
| 11 | Явные `readOnlyHint`, `destructiveHint`, `openWorldHint` у всех инструментов | ChatGPT | ✅ все четыре подсказки явно |
| 12 | Чтение и запись — разные инструменты, без «универсального» | Claude | ✅ 12 читающих, 4 пишущих |
| 13 | Имя инструмента ≤ 64 символа, описания точные, без инструкций модели «как себя вести» | Claude, ChatGPT | ✅ тест; описания переписаны под атлета, без «бот-only» |
| 14 | Человеческие ошибки, не «Internal Server Error» | Claude | ✅ ошибки инструментов текстом на языке атлета; 401 — двуязычный с путём за кодом |
| 15 | Размер ответа по задаче | Claude | ✅ история ≤ 200 тренировок, последние ≤ 10 |
| 16 | Не запрашивать переписку/память клиента, минимум данных на входе | Claude, ChatGPT | ✅ входы — имя упражнения, число, id; чат тренера наружу не отдаётся |
| 17 | Нет PHI/PCI/паролей во входах инструментов | ChatGPT | ✅ входы без чувствительного; см. «Риски» про профиль |
| 18 | Ограничение частоты | общее | ✅ 60 вызовов/мин на атлета, 60 регистраций/10 мин на адрес |
| 19 | Имя, версия, иконка, сайт сервера | Registry, каталоги | ✅ `serverInfo`: `athlete-diary`, «Athlete Diary», 1.1.0, icon, websiteUrl |
| 20 | Политика конфиденциальности: что собирается, зачем, кому, сроки, контакты; про данные в AI-приложениях | Claude, ChatGPT | ✅ `/privacy` обновлена (раздел «Кому передаётся» → AI-приложения) |
| 21 | Публичная документация (как подключить, что умеет) | Claude (к дате публикации), ChatGPT | ✅ `/connect` ru/en |
| 22 | Контакт поддержки | Claude, ChatGPT | ✅ `/support`, e-mail |
| 23 | Тестовый аккаунт с данными для ревьюера | Claude, ChatGPT | ✅ `/mcp/demo-code` + свой демо-аккаунт каталогов (`CATALOG_DEMO_*`, `catalog_demo.py`), не App Review |
| 24 | Подтверждение домена: Registry — `/.well-known/mcp-registry-auth` | Registry (HTTP-вход) | ✅ из `MCP_REGISTRY_AUTH`, иначе 404 |
| 25 | Подтверждение домена: ChatGPT — `/.well-known/openai-apps-challenge` (ровно токен) | ChatGPT | ✅ из `OPENAI_APPS_CHALLENGE`, иначе 404 |
| 26 | Заявка на листинг Glama — `/.well-known/glama.json` | Glama | ✅ из `GLAMA_MAINTAINER_EMAIL`, иначе 404 |
| 27 | Карточка сервера для сканеров без входа | Smithery | ✅ `/.well-known/mcp/server-card.json` из живого списка инструментов |
| 28 | `server.json` по схеме `2025-12-11`, `description` ≤ 100 символов | Registry | ✅ проверено `jsonschema` против схемы, тест |
| 29 | Аккаунт с платным планом Claude; верифицированная организация OpenAI и роль Owner | Claude, ChatGPT | ⛔ действие владельца |
| 30 | Деплой на прод | все | ⛔ действие владельца, см. «Что блокирует» |

## server.json

Лежит в корне (`server.json`), его же публикует `mcp-publisher`. Пространство имён —
домен задом наперёд, `app.athletediary`, вход в Registry через HTTP-файл на домене.
`version` = `mcp_server.SERVER_VERSION` (тест сверяет); поднимаешь версию сервера —
поднимай и здесь, и публикуй заново.

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "app.athletediary/athlete-diary",
  "title": "Athlete Diary",
  "description": "Your workout log in chat: history, progress, records, muscle volume, body weight, programs.",
  "version": "1.1.0",
  "websiteUrl": "https://athletediary.app/connect",
  "icons": [
    {
      "src": "https://athletediary.app/site/icons/icon-512.png",
      "mimeType": "image/png",
      "sizes": ["512x512"]
    }
  ],
  "remotes": [
    {
      "type": "streamable-http",
      "url": "https://athletediary.app/mcp"
    }
  ]
}
```

Вариант без своего домена — пространство `io.github.alexvi88/athlete-diary` и вход
`mcp-publisher login github` (нужен только GitHub). Хуже: имя не совпадает с доменом
коннектора, и каталоги, которые тянут Registry, покажут «github-пользователя», а не
«athletediary.app». Рекомендация — домен.

## Тексты листингов

**Name:** Athlete Diary (ru: «Дневник атлета»)

**One-liner / subtitle (≤ 30 символов для ChatGPT):** Your workout log, in chat

**Short description (Claude ≤ 200, Registry ≤ 100):**
Ask about your own training: progress, records, weekly volume, recovery, body weight and programs from your Athlete Diary log.

ru: Спрашивай про свои тренировки: прогресс, рекорды, объём за неделю, восстановление, вес тела и программы из «Дневника атлета».

**Long description (Claude ≤ 2000):**

> Athlete Diary is a workout log for the gym — an iPhone app and a Telegram bot where
> you record every set in a second. This connector lets Claude read your own diary, so
> you can ask about your training in plain words and get answers grounded in what you
> actually lifted.
>
> Ask whether your bench press grew over the last three months, which muscle groups you
> are undertraining this week, what changed since the last block, how recovered your legs
> are today, or how closely you follow your program. Claude sees your workouts and sets,
> exercise progress with estimated 1RM and records, weekly volume by muscle group, muscle
> recovery, body weight and your saved programs, and can build a new program around the
> exercises you already do.
>
> Almost everything is read-only. On your request the connector can also log today's
> body weight, delete a wrong weigh-in, add a custom exercise or duplicate a saved
> program — nothing else is ever changed. It never sees your AI coach chat, e-mail,
> Apple ID or Telegram account.
>
> Connecting takes a minute: add the connector, then confirm on the Athlete Diary page
> with a one-time 6-digit code from the app (Profile → Connect to Claude and ChatGPT) or
> the bot (/mcp). You can disconnect at any time in the app or the bot. Free.

ru (для русскоязычных каталогов и описания в боте):

> «Дневник атлета» — дневник тренировок для зала: приложение для iPhone и Telegram-бот,
> где подход записывается за секунду. Коннектор даёт Claude и ChatGPT читать твой
> собственный дневник: спрашиваешь про тренировки своими словами — получаешь ответ по
> тому, что ты реально поднял. Вырос ли жим за три месяца, какие группы недогружены на
> этой неделе, восстановились ли ноги, насколько ты держишься программы; можно попросить
> собрать новую программу вокруг твоих упражнений. Почти всё — только чтение; по твоей
> просьбе коннектор ещё заносит вес тела, удаляет неверное взвешивание, заводит своё
> упражнение и копирует программу. Переписку с AI-тренером, почту, Apple ID и Telegram он
> не видит. Подключение — минута: добавь коннектор и подтверди шестизначным кодом из
> приложения или бота. Бесплатно.

**Categories:** Health & Fitness (основная), Lifestyle, Productivity. Список категорий у
каждого каталога свой — бери ближайшее к «фитнес / здоровье / личные данные».

**Example prompts (10):**

1. Did my bench press progress in the last 3 months?
2. Build me a 4-day program around the exercises I already do.
3. What muscles am I undertraining this week?
4. Compare my last 90 days with the 90 before — what improved and what stalled?
5. What should I train today based on my recovery?
6. What are my best squat, bench and deadlift, and when did I set them?
7. How well have I stuck to my program this month?
8. How has my body weight changed since summer?
9. Show my last three workouts and tell me where I could add weight.
10. Log my body weight: 81.4kg.

ru: «Вырос ли мой жим за 3 месяца?», «Собери программу на 4 дня из моих упражнений»,
«Какие мышцы я недогружаю на этой неделе?», «Что тренировать сегодня с учётом
восстановления?», «Занеси вес 81.4кг».

**Tools (для форм, где просят описание каждого):**

| Tool | Title | Kind | What it does |
|---|---|---|---|
| `get_training_overview` | Training overview | read | Units, e1RM formula, stats, training profile and the athlete's exercise list — call first |
| `get_active_workout` | Current workout | read | The workout in progress, if any |
| `list_recent_workouts` | Recent workouts | read | Last 1–10 completed workouts with every set |
| `get_full_workout_history` | Full workout history | read | Up to the last 200 workouts, for long-span questions |
| `get_exercise_progress` | Exercise progress | read | One exercise: sets by date, working weights, e1RM, records |
| `compare_periods` | Compare two periods | read | Last N days vs the previous N, across all exercises |
| `get_weekly_volume_by_group` | Weekly volume by muscle group | read | Working sets per muscle group, this and last week |
| `get_muscle_recovery` | Muscle recovery | read | Recovery % per muscle group, last trained |
| `get_bodyweight_history` | Body weight history | read | Weigh-ins |
| `get_saved_programs` | Saved programs | read | Program list, or one program in full |
| `get_program_adherence` | Program adherence | read | Planned vs done vs skipped |
| `list_exercise_catalog` | Exercise catalog | read | Built-in catalog by muscle group |
| `log_bodyweight` | Log body weight | write | Adds today's weigh-in |
| `create_exercise` | Create custom exercise | write | Adds a custom exercise (idempotent by name) |
| `copy_program` | Copy a saved program | write | Duplicates a program; original untouched |
| `delete_bodyweight_log` | Delete a body weight entry | write, destructive | Removes one weigh-in by id |

Обоснования подсказок (ChatGPT просит по фразе): **readOnly** — 12 инструментов только
читают дневник атлета через тот же ридер, что у AI-тренера бота; 4 пишущих помечены
`readOnlyHint: false`. **openWorld: false у всех** — инструменты ходят только в дневник
вошедшего атлета, ничего не публикуют и не трогают чужие данные или интернет.
**destructive** — только `delete_bodyweight_log` (удаляет запись); остальные пишущие
только добавляют строку или копию.

**Auth instructions (для формы и для ревьюера):**

> OAuth 2.1 with dynamic client registration and PKCE. There is no password: on the
> Athlete Diary consent page the user enters a one-time 6-digit code that only they can
> get — in the iPhone app (Profile → Connect to Claude and ChatGPT → Get code) or in the
> Telegram bot (/mcp). The code lasts 10 minutes and works once.
>
> **For reviewers:** open https://athletediary.app/mcp/demo-code, sign in with the test
> credentials below, and you get a code for the demo account (pre-filled with 12 workouts
> over the last four weeks, body weight entries, a two-day program and a training
> profile). Then connect `https://athletediary.app/mcp` in Claude/ChatGPT and enter
> that code on the consent page. Username: `<CATALOG_DEMO_USERNAME>`, password:
> `<CATALOG_DEMO_PASSWORD>`.

В форму заявки (и Claude, и OpenAI) идут значения `CATALOG_DEMO_USERNAME` и
`CATALOG_DEMO_PASSWORD` — **не** пара App Review: страница `/mcp/demo-code` пару
`REVIEW_DEMO_*` не принимает (401), а вход по паролю в приложении не принимает пару
каталогов. Аккаунт ревьюеров каталогов свой, отдельный: демо App Review остаётся
витриной для Apple, и пишущие инструменты коннектора его не касаются.

**Logo:** `media/site/icons/icon-512.png` (иконка приложения; если каталог просит
1024×1024 — `training_log_bot_ios/TrainingLog/Resources/Assets.xcassets/AppIcon.appiconset`).

**Screenshots (3, PNG ≥ 1000px по ширине, ответ без вопроса в кадре):**

1. Claude на вопрос «How did my bench progress this month?» — таблица по датам с e1RM и
   рекордом (в демо-аккаунте 12 тренировок за четыре недели — длинный срок на нём не
   показать).
2. ChatGPT собирает «4-day program» по истории — дни и упражнения атлета.
3. Страница подтверждения «Athlete Diary — confirm access» с полем кода и списком того,
   что получает приложение (честность доступа — то, что смотрят ревьюеры).

Для обычного коннектора без UI Claude скриншоты не требует (только MCP Apps), ChatGPT —
просит; снимай с демо-аккаунта на английском.

**Тест-кейсы для ChatGPT (5 положительных, 3 отрицательных):**

| # | Prompt | Ожидается | Tools |
|---|---|---|---|
| + | Did my bench press progress in the last 3 months? | Числа по датам, e1RM, рекорд | `get_training_overview`, `get_exercise_progress` |
| + | What muscles am I undertraining this week? | Группы с малым числом подходов | `get_weekly_volume_by_group` |
| + | Show my last 3 workouts | Даты и подходы | `list_recent_workouts` |
| + | What should I train today? | Группы ≥ 85% восстановления | `get_muscle_recovery` |
| + | Log my body weight: 81.4kg | Вес записан | `log_bodyweight` |
| − | What's the weather in Berlin? | Коннектор не вызывается | — |
| − | Show my friend's workouts | Объяснение: доступ только к своему дневнику | — |
| − | Rename my program PPL to Push-Pull | Объяснение: переименование — в приложении или боте | — |

## Что делает владелец — по каталогам

### 0. Один раз перед всеми

1. Смержить ветку бота и iOS (PR), задеплоить бота (`fly deploy` или автодеплой).
2. `fly secrets list -a training-log-bot`: если там есть `MCP_PUBLIC_URL` — `fly secrets
   unset MCP_PUBLIC_URL -a training-log-bot` (секрет перекрывает `[env]` в `fly.toml`, и
   коннектор остался бы на fly.dev).
3. Проверить снаружи:
   `curl -s https://athletediary.app/.well-known/oauth-protected-resource/mcp` → `resource`
   = `https://athletediary.app/mcp`;
   `curl -si https://athletediary.app/mcp -X POST` → `401` и `WWW-Authenticate`;
   `curl -s https://athletediary.app/connect` → страница.
4. Завести демо-аккаунт ревьюеров каталогов — свою пару секретов (логин не совпадает с
   `REVIEW_DEMO_USERNAME`/`WALK_DEMO_USERNAME`, иначе страницы нет):
   ```sh
   PW=$(openssl rand -base64 24)
   fly secrets set -a training-log-bot CATALOG_DEMO_USERNAME=connector-review CATALOG_DEMO_PASSWORD="$PW"
   ```
   Язык аккаунта — `CATALOG_DEMO_LANG` (по умолчанию `en`, задавать не нужно). Эти же
   логин и пароль — в формы заявок Claude и OpenAI. Проверить: `https://athletediary.app/mcp/demo-code`
   открывается (без пары — 404), первый вход заводит и заполняет аккаунт (12 тренировок,
   вес тела, программа «Strength A/B», профиль), повторный — тот же аккаунт без дублей.
   Квот на вопросы у аккаунта нет (`ai_limits.quota_exempt`), суточный потолок по
   деньгам и 60 вызовов/мин коннектора — держат. Аккаунт исключён из цифр сайта, пуша
   «Как тебе дневник?» и таблиц игр (провайдер `review_demo`). Выключить —
   `fly secrets unset -a training-log-bot CATALOG_DEMO_USERNAME CATALOG_DEMO_PASSWORD`.
5. Самому пройти подключение в claude.ai (Settings → Connectors → Add custom connector →
   `https://athletediary.app/mcp`) и в ChatGPT (Developer mode), вызвать каждый инструмент
   (Claude портал просит это подтвердить).

### 1. Официальный MCP Registry (registry.modelcontextprotocol.io)

Аккаунт: не нужен; вход — ключом домена athletediary.app.

1. Поставить `mcp-publisher`: `brew install mcp-publisher` или бинарник из
   https://github.com/modelcontextprotocol/registry/releases.
2. Ключ (локально, закрытую половину — никуда):
   ```sh
   openssl genpkey -algorithm Ed25519 -out mcp-registry-key.pem
   PUB=$(openssl pkey -in mcp-registry-key.pem -pubout -outform DER | tail -c 32 | base64)
   echo "v=MCPv1; k=ed25519; p=$PUB"
   ```
   (на macOS — `openssl@3` из Homebrew: системный LibreSSL Ed25519 не умеет.)
3. Строку `v=MCPv1; k=ed25519; p=…` положить в прод:
   `fly secrets set -a training-log-bot MCP_REGISTRY_AUTH='v=MCPv1; k=ed25519; p=…'`.
   Проверка: `curl https://athletediary.app/.well-known/mcp-registry-auth`.
4. Закрытый ключ в hex:
   `openssl pkey -in mcp-registry-key.pem -noout -text | grep -A3 priv: | tail -n +2 | tr -d ' :\n'`
5. Из корня репозитория бота:
   `mcp-publisher login http --domain athletediary.app --private-key <hex>` →
   `mcp-publisher publish` (берёт `server.json`).
6. Проверка: `curl "https://registry.modelcontextprotocol.io/v0/servers?search=athlete"`.

Альтернатива без ключа — DNS TXT на apex `athletediary.app` с той же строкой и
`mcp-publisher login dns --domain athletediary.app --private-key <hex>`.

### 2. Каталог коннекторов Claude

Аккаунт: любой платный план Claude (Pro/Max/Team/Enterprise).

1. https://claude.ai/directory/manage → **Submit new** → **MCP connector**.
2. **Connection:** `https://athletediary.app/mcp`, Universal URL.
3. **Tools:** подтянутся сами, все 16 с заголовками; предупреждений об аннотациях быть не
   должно.
4. **Listing:** Name «Athlete Diary»; one-liner и description — из раздела выше;
   категории; Documentation URL `https://athletediary.app/connect`; Privacy policy
   `https://athletediary.app/privacy`; Support `athletediary.app@gmail.com`; иконка
   `icon-512.png`; slug `athlete-diary` (навсегда).
5. **Use cases:** вопросы про свои тренировки и прогресс, программа по истории; нужен
   бесплатный аккаунт Athlete Diary (iPhone или Telegram); reads and writes.
6. **Company:** имя владельца/проекта, сайт `https://athletediary.app`, контакт.
7. **Authentication:** OAuth with dynamic client registration.
8. **Data handling:** own API; personal health data — **да** (вес тела, ограничения/травмы
   в профиле, если человек их записал) — честный ответ, иначе отказ при проверке.
9. **Test & launch:** текст «For reviewers» из раздела «Auth instructions» с логином и
   паролем демо-аккаунта; подтвердить, что все инструменты вызваны.
10. **Compliance:** семь галочек. Отправить. Статусы — там же; эскалация —
    `mcp-review@anthropic.com`.

### 3. Каталог приложений ChatGPT

Аккаунт: OpenAI Platform, организация с пройденной верификацией (индивидуальной —
листинг под именем человека, бизнес — под именем компании), роль Owner или Apps
Management Write.

1. platform.openai.com → Settings → Organization → General: верификация.
2. Портал приложений/плагинов (platform.openai.com → Apps/Plugins → **Upload new**) →
   **With MCP**, Production MCP URL `https://athletediary.app/mcp`.
3. **Domain verification:** портал выдаст токен →
   `fly secrets set -a training-log-bot OPENAI_APPS_CHALLENGE=<токен>` → проверить
   `curl https://athletediary.app/.well-known/openai-apps-challenge` (ровно токен) → Verify.
4. **Scan tools:** проверить, что у всех трёх подсказок явные значения; вписать
   обоснования из раздела выше.
5. CSP — не нужен (у коннектора нет UI-виджетов).
6. App info: display name «Athlete Diary», subtitle «Your workout log, in chat»,
   описание, категория, логотип, скриншоты, privacy/terms URL.
7. Reviewer credentials — как для Claude; тест-кейсы 5+3 из таблицы выше.
8. Отправить. Имена и описания инструментов после публикации замораживаются: правка —
   новая отправка.

### 4. Glama

1. Войти на glama.ai (лучше GitHub-аккаунтом владельца).
2. `fly secrets set -a training-log-bot GLAMA_MAINTAINER_EMAIL=<почта аккаунта Glama>` →
   `curl https://athletediary.app/.well-known/glama.json`.
3. Glama подтягивает серверы из официального Registry: после шага 1 выше найти «Athlete
   Diary» и нажать **Claim**; файл подтверждается за несколько минут. Если листинга нет —
   «Add server» с адресом `https://athletediary.app/mcp`.

### 5. Smithery

1. smithery.ai/new → URL `https://athletediary.app/mcp` → шаги публикации (или `smithery
   mcp publish https://athletediary.app/mcp -n <org>/athlete-diary`).
2. Сканер без входа получит 401 — тогда он читает
   `/.well-known/mcp/server-card.json` (уже отдаём).

### 6. PulseMCP и mcp.so

PulseMCP и Glama берут серверы из официального Registry — после шага 1 обычно ничего не
нужно; проверить через неделю. mcp.so — форма «Submit» на сайте (ссылка на
`https://athletediary.app/connect` и описание выше).

## Что блокирует

1. **Деплой** этих веток и снятие `MCP_PUBLIC_URL` из `fly secrets`, если он там есть.
   До деплоя `/mcp` на athletediary.app отвечает, но OAuth называет fly.dev.
2. **SEO-редиректы** fly.dev → athletediary.app не должны задевать пути из списка «Не
   перенаправлять» выше.
3. **Действия владельца в аккаунтах:** платный план Claude; верификация организации
   OpenAI; ключ для Registry и секреты `MCP_REGISTRY_AUTH`, `OPENAI_APPS_CHALLENGE`,
   `GLAMA_MAINTAINER_EMAIL` — значения выдают каталоги, в коде их нет.
4. **iOS-экран** (адрес с сервера, «Подключённые приложения») приедет со следующей сборкой;
   до неё приложение показывает адрес fly.dev — по нему работают статические токены, но
   новое OAuth-подключение надо делать по `https://athletediary.app/mcp` (его показывает
   бот и `/connect`).

## Риски и что не проверено

- **CIMD.** Claude советует CIMD или свои учётные данные вместо DCR при большом трафике
  из каталога: DCR заводит клиента на каждое подключение. Серверная часть MCP SDK 2.0
  CIMD не поддерживает; регистрации чистятся раз в сутки (`db.purge_expired_oauth`), предел
  — 60 за 10 минут на адрес (`mcp_oauth.REGISTER_RATE_LIMIT_PER_IP`). При росте —
  `oauth_anthropic_creds` через `mcp-review@anthropic.com`.
- **Медданные.** Профиль атлета (ограничения, травмы) и вес тела могут считаться
  данными о здоровье. В форме Claude отвечаем «да»; у OpenAI в правилах — запрет на
  PHI. Инструменты PHI на вход не просят, но `get_training_overview` отдаёт профиль; если
  ревью OpenAI на это укажет — убрать `limitations` из сводки для MCP.
- Схема `glama.json` (`https://glama.ai/mcp/schemas/connector.json`) и пути отправки
  PulseMCP/mcp.so сверены только по вторичным источникам — прямые страницы из
  окружения разработки были недоступны. Проверить при подаче.
- Порядок шагов портала OpenAI взят из вторичных обзоров (официальные страницы
  developers.openai.com из окружения не открывались) — названия кнопок могут отличаться.

## Источники (открыты 16.10.2026)

- MCP Registry, вход по домену (HTTP/DNS) и GitHub —
  https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/authentication.mdx
- MCP Registry, удалённые серверы в `server.json` —
  https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/remote-servers.mdx
- Схема `server.json` 2025-12-11 —
  https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json
- Claude: отправка коннектора — https://claude.com/docs/connectors/building/submission
- Claude: критерии ревью — https://claude.com/docs/connectors/building/review-criteria
- Claude: авторизация коннекторов — https://claude.com/docs/connectors/building/authentication
- OpenAI: правила отправки приложений — https://developers.openai.com/apps-sdk/app-submission-guidelines
- OpenAI: правила для разработчиков (зеркало) —
  https://github.com/janwilmake/openai-app-sdk-context/blob/main/app-developer-guidelines.md
- OpenAI: файл отправки и аннотации —
  https://github.com/openai/plugins/blob/main/plugins/openai-developers/skills/chatgpt-app-submission/SKILL.md
- OpenAI: загрузка и отправка плагина — https://developers.openai.com/plugins/deploy/submission
- OpenAI: подтверждение домена (форум) —
  https://community.openai.com/t/chatgpt-app-submissions-domain-verification-step-does-not-support-subpath-hosted-mcp-servers/1379021
- Smithery: публикация — https://smithery.ai/docs/build/publish
- Glama: заявка через `/.well-known/glama.json` — https://glama.ai/mcp/connectors/io.github.irmasemma/jsonlify
