# Как приводить людей в бота

Рабочий документ по привлечению: чем размечаем трафик, куда идём первыми
шагами и какие тексты для этого готовы. Бюджеты маленькие, поэтому порядок
такой: сначала бесплатное и вечное (каталоги, статья), потом закупка.

Тексты для внешних площадок пишем тем же голосом, что и бот — см.
`TONE_OF_VOICE.md`. Тренер из подвальной качалки не меняет характер, когда
рассказывает о себе на vc.ru.

## 1. Разметка трафика — до первого потраченного рубля

Каждому каналу — своя ссылка. Без неё непонятно, какой канал приводит людей,
которые действительно тренируются, а какой просто нагоняет открытий.

| Что | Ссылка | Как считается |
|---|---|---|
| Рекламный канал, посев, статья | `t.me/lightweight_log_bot?start=src_<слаг>` | источник `src_<слаг>` |
| Приглашение от атлета | кнопка «🏋️ Тоже вести дневник» на картинке тренировки; экран «Пригласить» в приложении — `/i/ref_<id>` | `referral` + автор |
| Визитка программы/упражнения | ссылка из «Поделиться» | `shared_card` + автор |
| Каталог ботов, поиск, сарафан | обычная `t.me/lightweight_log_bot` | `organic` |

Слаг — латиница, цифры и «_»: `src_kachalka_daily`, `src_vc_article`,
`src_tgads_1`. Регистр и мусор бот срежет сам, кириллица не доедет вовсе.

Источник пишется **один раз**, на первом `/start`. Старожил, перешедший по
рекламной ссылке, каналу не засчитывается — иначе за одного человека платишь
дважды. Все, кто был в боте до появления атрибуции, помечены `legacy` и в
отчёты не попадают.

Смотреть — `/growth` в боте (только админ): воронка по источникам за 30 дней,
`/growth 7` — за неделю. Ключевая цифра не «пришли», а **«записали первую
тренировку»**: канал с дешёвыми переходами, которые не доходят до первой
записи, — выброшенные деньги.

Код: `acquisition.py` (разбор метки и тексты отчёта), `db.set_user_source`,
`db.acquisition_funnel`, `db.top_referrers`.

### Установки из App Store — не в `/growth`

`/growth` видит только первый `/start` в Telegram. Человек, который перешёл
по ссылке на App Store и поставил приложение, до бота не доходит вовсе, и
источника у его аккаунта (`/auth/apple`) нет. Такие установки считает сама
Apple: **App Store Connect → Analytics → Campaigns**.

Работает это через кампанийную ссылку: `ct=<кампания>` у адреса в App Store.
Apple засчитывает `ct` только вместе с `pt=<provider token>` — номер
провайдера аккаунта разработчика, его даёт генератор ссылок там же, в
Campaigns. Поэтому `APP_STORE_URL` на сервере задаётся сразу с ним:

    APP_STORE_URL=https://apps.apple.com/app/apple-store/id<номер>?pt=<provider token>&mt=8

Без `pt` метки безвредны, но в отчёте Apple не появятся.

| Где ссылка | Кампания |
|---|---|
| Сторис «ТЫ» в приложении (карточка «ТЫ» в подвале, итог тренировки) | `ct=you_story` — дописывает приложение (`AppStoreLink.tagged`) |
| Страница приглашения `/i/ref_<id>` | `ct=ref_<id>` — дописывает сервер (`acquisition.with_campaign`) |
| Визитка тренировки, бот | без `ct` — как задано в `APP_STORE_URL` |

Сама отправка сторис видна в продуктовых событиях (`analytics_events`,
platform `ios`): `you_story_open` (source `menu`|`finish`) → `you_story_share`
→ `you_story_shared` (activity — куда ушло: тип действия iOS вроде
`com.apple.UIKit.activity.SaveToCameraRoll`, id расширения мессенджера, или
`cancelled`). Воронка «открыл → отправил» — из событий, «поставил приложение по
сторис» — только из Campaigns: Apple отдаёт установки суммой, без связи с
нашим аккаунтом, так что склеить их по человеку нельзя.

### Приглашения — воронка в `/metrics`

Экран «Пригласить» в приложении («Меню → Ещё → Пригласить» и «Достижения»)
раздаёт личную ссылку `/i/ref_<id>`. Главная цифра — реальные приходы и
сколько из них начали тренироваться; открытия и «Поделиться» — вспомогательные.
Всё — в суточной сводке (`/metrics`: строка за неделю под таблицей, по дням — в CSV):

| Шаг | Строка сводки | Откуда |
|---|---|---|
| Открыли экран | `invite_opens`, `invite_open_users` | событие `invite_open` (source `menu`/`achievements`) |
| «Поделиться» / «Скопировать» | `invite_shares`, `invite_copies`, `*_users`, `invite_senders` | `invite_share`, `invite_copy`; итога системного листа нет |
| Открыли страницу | `invite_page_views` | счётчик сайта, страница `/i/ref` без id, роботы и превью мессенджеров не в счёт |
| **Пришли** | `referral_signups` (`_tg`/`_ios`) | `users.source = referral` с автором: и `?start=ref_<id>` в боте, и код в приложении (`POST /v1/me/referral` — после входа, поэтому не из свойств `signup`) |
| **Начали тренироваться** | `referral_trained` | из пришедших — закончили тренировку у нас (не импорт) в `REFERRAL_TRAINED_DAYS` (7) суток; дозревает неделю, сводка пересчитывает |

С приложением на iPhone ссылка открывает его сразу (Universal Link) — такой
переход страницу не видит, поэтому «пришли» бывает больше «открыли страницу».
Кто сколько привёл за окно и сколько из них тренируются — блок «Кто приводит»
в `/growth`; вчерашние приходы одной строкой — в утреннем отчёте.

## 2. Каталоги ботов — час работы, трафик навсегда

Куда подавать (все — бесплатно, заявка идёт с твоего аккаунта):

- **@BotoStore** — крупнейший каталог, заявка через бота, модерация до недели.
  Лимит длины ссылки — 48 символов, поэтому метка короткая: `?start=src_boto`.
- **tgstat.ru/botsuggest** — регистрация бота в базе TGStat.
- **@tgbotlist** — каталог-агрегатор, добавление через бота.
- **@botsarchive** — просит €10 за размещение: не платим. Правило общее: за
  каталоги денег не отдаём — их трафик копеечный, а те же деньги в посеве
  дают измеримый результат в `/growth`.
- **botostore.com/catalog** и **telegram-store.com** — дубли карточки для SEO:
  сами страницы каталогов индексируются в Яндексе и Google.
- **Product Hunt / vc.ru «Сервисы»** — если захочется англоязычной аудитории.

Что нужно подготовить один раз (лежит ниже, копируй как есть):

1. Название и короткое описание.
2. Развёрнутое описание.
3. 3–5 скриншотов: главное меню, ввод подхода, карточка тренировки картинкой,
   экран прогресса с графиком, ответ AI-тренера.
4. Ссылка `t.me/lightweight_log_bot` — в каталогах свою метку поставить негде, они
   упадут в `organic`.

### Короткое описание (до 80 символов)

> Дневник тренировок с AI-тренером. Пишешь «100 8» — остальное считаю сам.

### Описание в одну строку для списков

> Записываю твои подходы, считаю тоннаж и рекорды, собираю программу под твой
> зал и напоминаю, когда пропускаешь.

### Развёрнутое описание

> ПРИВЕТ АТЛЕТ. Я твой дневник тренировок.
>
> Пишешь `100 8` — записал 100 кг на 8 повторов. Можно голосом: «сто на
> восемь». Никаких форм и выпадающих списков посреди подхода.
>
> Что я умею:
> • считаю тоннаж, рекорды и расчётный максимум в каждом упражнении;
> • показываю прогресс графиком — видно, растёшь ты или стоишь на месте;
> • собираю программу под твой зал, опыт и цель, и веду по ней тренировку;
> • отвечаю на вопросы про технику и подбор веса — как тренер, а не как поиск;
> • разбираю технику по видео: пришли ролик подхода — скажу, что править первым;
> • напоминаю о тренировке, когда начинаешь пропускать;
> • отдаю карточку тренировки картинкой — забрать в сторис или в чат друзьям.
>
> Вес тела, история за всё время, экспорт — на месте.
> Бесплатно.

### Английские версии — для англоязычных каталогов

Бот двуязычный (см. `CLAUDE.md`, «Два языка»), и в англоязычных каталогах
описание нужно своё, а не переведённое машиной. Голос тот же — тренер от
первого лица, по главе `## English voice` в `TONE_OF_VOICE.md`.

**Чего тут НЕ делаем: брендовой формулы.** «HEY ATHLETE!» — приветствие внутри
продукта, где человек уже нажал «Старт». В каталоге он листает фид из двадцати
ботов подряд, и единственная работа строки описания — сказать, что бот делает.
Формула на этом месте занимает первую строку и не говорит ничего.

#### Короткое описание (до 80 символов)

> Workout diary with an AI coach. Type "100 8" — I count the rest myself.

#### Описание в одну строку для списков

> I log your sets, count tonnage and records, build a program for your gym, and
> nudge you when you start skipping.

#### Развёрнутое описание

> I'm your workout diary.
>
> Type `100 8` — logged, 100 kg for 8 reps. Or say it out loud: "a hundred for
> eight". No forms, no dropdowns in the middle of a set.
>
> What I do:
> • count tonnage, records and estimated 1RM for every exercise;
> • show progress on a chart — so you can see whether you're growing or stuck;
> • build a program around your gym, experience and goal, then run the workout by it;
> • answer questions on form and load like a coach, not like a search engine;
> • check your form on video — send a clip of a set and I'll name what to fix first;
> • remind you about training when you start skipping;
> • hand you the workout card as an image — for stories or for the group chat.
>
> Bodyweight, full history, export — all there. Free.

#### Пост для каталогов-архивов (@bots_archive и похожие)

Их формат — один абзац после `ℹ️Description:`, плотный и без переносов, плюс
языки и теги. Абзац отдельный от «развёрнутого» выше: тот сверстан списком под
страницу каталога, а этот обязан читаться одной строкой в ленте.

> ℹ️Description: Athlete Diary logs your training, with an AI coach built in.
> Type "100 8" and the set is down — no forms, no dropdowns. It counts your tonnage,
> records and estimated 1RM, charts your progress, builds a program around your
> gym and goal, reviews your form from a video clip, and nudges you when you
> start skipping. Bodyweight and full history included. Free.
>
> 🌐 Languages: English Russian
>
> #️⃣ Tags: #gym #workout #fitness #workouttracker #workoutlog #strength
> #powerlifting #bodybuilding #aicoach #ai #progress #diary #health #free

Теги подобраны под то, как ищут в таких каналах: сначала общие (#gym, #workout,
#fitness), потом узкие (#powerlifting, #bodybuilding) — общие приводят объём,
узкие приводят тех, кто останется.

## 3. Статья на vc.ru — главный источник первой тысячи

Формат, который работает для инди-продуктов: не реклама, а разбор с цифрами и
кодом. Раздел — «Разработка» или «Здоровье». Ссылку в текст ставим **с
меткой**: `t.me/lightweight_log_bot?start=src_vc_article`.

Что обязательно должно быть в статье:
- конкретная боль в начале, без разгона;
- скриншоты бота (те же, что для каталогов) — по одному на каждый тезис;
- честные цифры: сколько людей, сколько тренировок, сколько стоит хостинг;
- одно техническое решение, разобранное по-настоящему (парсер «100 8» или
  голосовой ввод) — за этим на vc.ru и приходят в комментарии;
- ссылка на бота один раз в конце и один раз по ходу текста, не больше.

Черновик — `docs/vc_article_draft.md`. Перед публикацией подставить реальные
числа: их видно в `/check_users` и `/growth`.

## 3½. Публичные страницы для поиска

Вечный бесплатный трафик — поиск: «как перенести тренировки из Hevy»,
«калькулятор 1ПМ», «нормативы жима лёжа», «программа 5×5» — и поиск внутри
ChatGPT и Perplexity. Страницы отдаёт сам сервер бота (`seo_pages.py`;
математика калькуляторов — `seo_tools.py`, оформление — `seo_design.py`,
статика и картинки превью — `seo_static.py`, библиотека — мышцы, пары,
словарь, гайды, переезд, шаблон, «о проекте» — `seo_library.py`, новые
калькуляторы и карточки «поделиться» — `seo_calcs.py`, файлы шаблона —
`seo_template.py`, 301/gzip/ETag — `seo_http.py`), каждая на двух языках:
`/ru/...` и `/en/...`, пары связаны `hreflang`, голый `/` уводит на язык
браузера.

| Раздел | Адреса | Метка в бота |
|---|---|---|
| Главная, AI-тренер, разбор видео, бот в Telegram | `/`, `/ai-coach`, `/video-review`, `/telegram` | `src_seo_home`, `_ai`, `_video`, `_tg` |
| Переезд | `/import/hevy`, `/import/strong`, `/import/csv` | `src_seo_imp_hevy`, `_imp_strng`, `_imp_csv` |
| Калькуляторы | `/tools`, `/tools/1rm`, `/tools/warmup`, `/tools/rpe`, `/tools/wilks` (Wilks, DOTS, IPF GL), `/tools/tonnage`, `/tools/rest-timer`, `/tools/goal` («когда я пожму 100»), `/tools/strength-percentile`; `/tools/plates` снят — 301 на `/tools` | `src_seo_tools`, `_rm1`, `_warmup`, `_rpe`, `_wilks`, `_tonnage`, `_timer`, `_goal`, `_pct` |
| «Запиши этот подход» под результатом | те же калькуляторы и нормативы; ведёт через `/go/diary` — туда же, куда главная кнопка на этом устройстве | `src_seo_rm1_log`, `_wu_log`, `_rpe_log`, `_wlk_log`, `_str_log`, `_ton_log`, `_tmr_log`, `_goal_log`, `_pct_log` |
| Нормативы силы | `/strength`, `/strength/<bench-press\|squat\|deadlift>/<men\|women>` | `src_seo_str` |
| Программы | `/programs`, `/programs/<key>` (весь `seed_data.WORKOUT_PROGRAMS`) | `src_seo_progs`, `_prog` |
| Техника | `/exercises`, `/exercises/<адрес>` (`seo_pages.EXERCISES`, 72 упражнения) | `src_seo_exs`, `_ex` |
| Сравнения с приложениями | `/vs/hevy`, `/vs/strong`, `/vs/fitbod`, `/vs/jefit` | `src_seo_vs_hevy`, `_vs_strng`, `_vs_fitbod`, `_vs_jefit` |
| Альтернативы | `/alternatives/hevy`, `/alternatives/strong` | `src_seo_alt_hevy`, `_alt_strng` |
| Дневник тренировок | `/workout-log-app` | `src_seo_app` |
| Шаблон дневника | `/templates/workout-log` (+ `.xlsx`, `.csv`, `-example.csv` — импортируются без вопросов, тест туда-обратно) | `src_seo_tpl` |
| Мышцы | `/muscles`, `/muscles/<12 групп>` | `src_seo_mus` |
| Упражнение против упражнения | `/compare`, `/compare/<a>-vs-<b>` (20 пар, `seo_library.PAIRS`) | `src_seo_cmp` |
| Словарь зала | `/glossary`, `/glossary/<12 терминов>` | `src_seo_gls` |
| Гайды | `/guides`, `/guides/<10 статей>` с источниками | `src_seo_guide` |
| О проекте | `/about` (разработчик, Organization с sameAs) | `src_seo_about` |
| Справка для AI-поиска | `/llms.txt`, `/llms-full.txt` (полная версия) | `src_seo_llms` |

Кнопки ведут через `/go/bot?f=<блок>` и `/go/appstore?f=<блок>`: сервер
считает клик и отдаёт редирект на `t.me/<бот>?start=src_seo_<блок>` (ссылка
целиком влезает в 48 символов) или в App Store. Метка — по разделу, а не по
странице. Какая кнопка главная, решает устройство: iPhone — App Store,
Android — бот, компьютер — QR-код в App Store; на английских страницах App
Store главный везде, кроме Android. Обе ссылки есть в любом варианте.

Ещё на сайте: встраиваемый калькулятор `/embed/<язык>/1rm` (единственный
адрес, который можно показывать в чужом iframe; на странице калькулятора —
готовый код; старый `/embed/<язык>/plates` — 301 на него), картинки превью `/og/<язык>/<ключ>.png`,
JSON-LD (SoftwareApplication, BreadcrumbList, ExercisePlan, Article,
FAQPage — только где FAQ виден на странице; Organization с sameAs и
dateModified на каждой странице, видимая строка «Обновлено …»), своя 404
(отвечает 404), карточки «поделиться» `/og/card/<язык>/<вид>.png`
(цель, перцентиль, 1ПМ, DOTS, тоннаж, уровень — параметры строго проверяются),
`sitemap.xml` с `<lastmod>` и картинками упражнений.

Один домен: когда задан `SITE_BASE_URL`, страницы сайта с `www.<домен>` и с
адреса сервера (fly.dev) отвечают 301 на тот же путь своего домена. Только
белый список (`/`, `/ru`, `/en`, `/ru/*`, `/en/*`, sitemap, robots,
llms-файлы) и только GET/HEAD; `/v1`, OAuth, `/mcp`, `/.well-known`,
`/coach/*`, `/privacy`, `/terms`, `/support`, `/embed`, `/go`, `/og`,
`/site`, `/media` не перенаправляются никогда (`seo_http.py`, тест на
длинном списке). Там же gzip и ETag/304.

Главная показывает «неделю в подвале» — настоящие цифры за 7 дней
(`site_numbers.py` → `db.site_week_numbers`: закрытые тренировки, подходы,
тонны; без импорта, своих аккаунтов и демо). Нет данных — нет и доски.

Картинки сайта готовятся офлайн и лежат в `media/site/`: кадры упражнений
AVIF/WebP 360/720 — `scripts/gen_site_exercise_images.py`, экраны
приложения — `scripts/gen_site_screens.py`. Таблица перцентилей силы —
`seo_data/strength_percentiles.json`, пересобирается из выгрузки
OpenPowerlifting `scripts/build_strength_percentiles.py` (раз в полгода).

Нормативы силы — своя шкала по DOTS, а не чужие таблицы: пороги уровней и
доли упражнений в сумме (медианы по классическому троеборью из открытых
данных OpenPowerlifting) — в `seo_tools.LEVELS` и `LIFT_SHARE`, методика
написана на странице.

Тексты — ключи `seo.*` в `locales/*.json`, данные (программы, упражнения,
описания техники) — через те же локализаторы, что у бота. Сравнения с Hevy и
Strong, Fitbod, JEFIT и страницы альтернатив — факты «по данным на октябрь
2026» со ссылками на источники; цены у
конкурентов меняются — перепроверяй их раз в квартал и правь дату.

### Кампании App Store: ct=site_<блок>

Каждая кнопка App Store на сайте (и редирект `/go/appstore`) дописывает к
`APP_STORE_URL` кампанию `ct=site_<блок>` — те же блоки, что у меток бота
(`seo_pages.app_store_link`); `pt` и `mt` из настройки остаются. Смотреть —
App Store Connect → Аналитика → Кампании: там видно, какой раздел сайта
приводит установки. Без `pt` в `APP_STORE_URL` Apple кампании не считает.

### Посещения сайта — в `/growth` и `/activity`

Сервер сам считает просмотры (`site_stats.py`, таблица `site_events`, 13
месяцев): страница, язык, домен источника, вид устройства, имя робота,
`utm_source`, клики по кнопкам. Без cookies, без скриптов, без IP — так и
написано в политике конфиденциальности. `/growth` показывает блок
«🌐 САЙТ»: просмотры по страницам, источникам и устройствам, обходы
роботов (Googlebot, bingbot, GPTBot, OAI-SearchBot, PerplexityBot,
ClaudeBot…), «Из AI-ассистентов» — переходы людей из ChatGPT, Perplexity,
Claude, Gemini, Copilot, Grok, DeepSeek (по источнику или по `utm_source`,
который ассистент дописал сам), «AI открыл страницу по вопросу человека» —
ChatGPT-User, Perplexity-User, Claude-User и похожие (нас цитируют прямо
сейчас; главная цифра GEO), клики и их долю от просмотров и сколько людей пришло в бота по
меткам `src_seo_*`. В `/activity` — кнопка «🌐 Посещения сайта»: просмотры
людей, сложенные по 10 минут.

### IndexNow — чтобы Bing (и поиск ChatGPT) узнал о страницах сразу

1. Придумать ключ (8–128 символов, буквы, цифры, дефис) и положить его
   секретом: `fly secrets set INDEXNOW_KEY=<ключ>`. Сервер начнёт отдавать
   `https://athletediary.app/<ключ>.txt`.
2. После деплоя, который добавил или заметно поменял страницы:
   `INDEXNOW_KEY=<ключ> python scripts/indexnow_ping.py` (`--dry-run` —
   посмотреть, что уйдёт). Скрипт берёт адреса из живого `sitemap.xml`.

### Настройки окружения

- `SITE_BASE_URL` — свой домен сайта (`https://athletediary.app`). Из него
  собираются canonical, hreflang, `sitemap.xml` и строка Sitemap в
  `robots.txt`. Пусто — адрес сервера (`PUBLIC_BASE_URL`/`MCP_PUBLIC_URL`).
  Страницы отвечают на любом хосте; `PUBLIC_BASE_URL` эта настройка не
  трогает — на нём Universal Links приглашения тренера и OAuth MCP.
- `GOOGLE_SITE_VERIFICATION` — значение метатега из Search Console, ставится
  на все страницы. Или `GOOGLE_SITE_VERIFICATION_FILE=google<токен>.html` —
  сервер отдаст файл по адресу `/<имя>`.
- `BOT_USERNAME` — имя бота для ссылок, пока бот не спросил его у Telegram
  сам (по умолчанию `lightweight_log_bot`).
- `APP_STORE_URL` — с ним появляются кнопки App Store, QR на компьютере и
  смарт-баннер Safari (`apple-itunes-app`, номер — из `/id<число>`).
- `INDEXNOW_KEY` — см. выше.

После запуска домена: добавить сайт в Search Console, Bing Webmaster Tools
и Яндекс Вебмастер, отправить `<домен>/sitemap.xml`. `robots.txt` открыт
всем, роботам AI-поиска — отдельной группой.

### Цикл SEO — раз в две недели

1. Search Console → Эффективность → Страницы и Запросы за 28 дней.
2. Выписать запросы и страницы на позициях **8–20** с показами — это те, что
   почти на первой странице.
3. Для каждой: дописать на странице то, что ищут (вопрос — в FAQ,
   недостающий шаг — в текст, точнее заголовок и description под запрос),
   добавить ссылки на неё с соседних страниц. Факты — только те, что держит
   код.
4. Задеплоить, отправить адреса через IndexNow, отметить дату правки — и
   через две недели сравнить позиции.

## 3¾. Каталоги приложений — ссылки на сайт

Позицию в Google у нового домена двигают ссылки с чужих сайтов, а не правка
заголовков. В первые сутки после запуска (17–18 октября 2026) Search
Console показал 250 показов по 160 запросам при средней позиции 54: Google
страницы уже понимает, но ставит далеко. Каталоги — самый дешёвый способ
получить первые ссылки и при этом людей, которые ищут именно замену
Hevy/Strong.

Правила те же, что у каталогов ботов (раздел 2): денег за размещение не
платим; у каждой ссылки на сайт своя метка `?utm_source=<каталог>`, иначе
заходы смешаются в `/growth`; голос — тренер от первого лица, без брендовой
формулы.

### Где мы есть и что дальше

| Площадка | Статус | Метка |
|---|---|---|
| App Store Connect → Marketing URL | ✅ стоит | `utm_source=appstore` |
| AlternativeTo | ✅ заявка 18.10.2026, ждёт модерации (1–7 дней) | `utm_source=alternativeto` |
| There's An AI For That | — | `utm_source=taaft` |
| Futurepedia | — | `utm_source=futurepedia` |
| Toolify.ai | — | `utm_source=toolify` |
| SaaSHub, Uneed, BetaList | — | `utm_source=saashub` / `uneed` / `betalist` |
| Product Hunt | — тексты ниже, нужна галерея 1270×760 | `utm_source=producthunt` |
| Каталоги MCP (Glama, Smithery, mcp.so) | — мы в официальном реестре MCP | `utm_source=<каталог>` |

На AlternativeTo связь «альтернатива» двусторонняя: отмечаешь Hevy
альтернативой нам — и мы появляемся на странице «Hevy alternatives». Там
отмечены Hevy Workout Tracker, Strong, Fitbod, JEFIT, FitNotes, Boostcamp,
Alpha Progression. Порядок на тех страницах — по лайкам: пара лайков от
знакомых поднимает нас в списке. Теги там вводятся по одному (не строкой
через запятую), лучше выбирать из подсказок: `workout-tracker`, `fitness`,
`gym`, `strength-training`, `personal-trainer`, `weightlifting`,
`ai-powered`, `exercise-log`. Платформы — iPhone и Telegram (Android и
Windows не ставим: по правилам площадки — только если есть своё приложение
под эту ОС). Пакетные менеджеры — пусто.

### Тексты (английский, копировать как есть)

Ссылка на App Store — без хвоста `?uo=4`:
`https://apps.apple.com/us/app/athlete-diary-gym-workout-log/id6815211995`.
Бот — `https://t.me/lightweight_log_bot`.

**Короткое описание**

> Workout diary with an AI coach. Type "100 8" — I count the rest myself.

**Развёрнутое описание** (AlternativeTo, AI-каталоги, SaaSHub)

> Athlete Diary is a strength training log with an AI coach built in. Type
> "100 8" and the set is down — no forms, no dropdowns.
>
> I count your tonnage, records and estimated 1RM, and chart your progress
> across your whole history — no 3-month limit, no paywall on graphs.
> Bodyweight moves get their own Max reps view.
>
> The AI coach reads your actual sets: it tells you where you've stalled,
> builds a program around your gym and goal, puts together today's workout
> from the muscle groups that are ready, and reviews your form from a video
> clip.
>
> Coming from Hevy or Strong? Import your CSV export in a minute — and the
> import can be undone.
>
> Works as an iPhone app and as a Telegram bot (so Android and desktop too),
> in English and Russian. You can also connect it to Claude or ChatGPT and
> ask about your training there.
>
> Free.

**«Альтернатива для»**: Hevy, Strong, Fitbod, JEFIT, FitNotes, Boostcamp,
Alpha Progression. Беговые и шагомеры (Strava, Google Fit) — не наши.

**Скриншоты** — те же, что в App Store, 3–5 штук: ввод «100 8», ответ
AI-тренера, график прогресса, итог тренировки, импорт.

### Product Hunt

Запуск — вторник–четверг в 00:01 по тихоокеанскому времени; весь день
отвечать на комментарии.

- **Tagline** (до 60): A workout log where an AI coach reads your real sets
- **Description** (до 260): Log a set by typing "100 8". Full history and
  charts are free — no 3-month limit. The AI coach reads your actual sets,
  tells you where you've stalled, builds programs and reviews form from
  video. Imports Hevy/Strong CSV. iPhone + Telegram.
- **Topics**: Health & Fitness, Artificial Intelligence, iOS, Telegram,
  Productivity. **Pricing**: Free.
- **Links**: сайт с `utm_source=producthunt`, App Store, бот
  `?start=src_ph`.

**Первый комментарий автора**

> Hey Product Hunt 👋
>
> I built Athlete Diary because every gym log I tried either hid my own
> history behind a paywall or gave "AI" advice that had nothing to do with
> what I actually lifted.
>
> So here's what it does:
> • Logging is one line — "100 8" is 100 kg × 8 reps. Voice works too.
> • Full history and charts are free. No 3-month window.
> • The AI coach reads your real sets: where you've stalled, what's
>   recovered, what to train today. It can build a full program or a one-off
>   session, and review your form from a short video.
> • Moving from Hevy or Strong? Drop in the CSV export — and you can undo
>   the import if something looks off.
> • It also connects to Claude and ChatGPT, so you can ask "did my bench
>   move this month?" right in your chat.
>
> It's an iPhone app and a Telegram bot (so it works on Android and desktop
> through Telegram), in English and Russian.
>
> I'd love honest feedback — what would make you switch from your current
> log?

Каждое утверждение здесь держит код (бесплатная полная история, импорт с
отменой, разбор видео, коннектор). Убираешь фичу — правь и эти тексты.

## 4. Дальше — закупка

Порядок и суммы обсуждались отдельно; коротко:

1. **Посевы в тематических ТГ-каналах** — 5–8 небольших каналов по 1–3 тыс. ₽,
   каждому своя метка `src_<канал>`. Масштабируем только те, где цена
   активированного пользователя сошлась.
2. **Telegram Ads через реселлера** (eLama даёт постоплату, порог входа
   ~500 €) — таргет на подписчиков фитнес-каналов, объявление ведёт в бота.
3. **VK и Яндекс Директ** — позже и только с лендингом; увод из VK в Telegram
   теряет большую часть кликов, а в Директе мы конкурируем с мобильными
   приложениями за дорогой клик.

Лендинг на старте не нужен: первое сообщение бота и есть посадочная страница.

## 5. Чат сообщества

Обычная телеграм-группа, где атлеты разговаривают между собой. Бот в неё не
пишет и ничего оттуда не читает — он только даёт дорогу: кнопка «💬 Чат
атлетов» внизу главного меню и команда `/community`.

Как включить:

1. Завести супергруппу в Telegram, взять ссылку-приглашение
   (`https://t.me/имя` или `https://t.me/+код`).
2. Положить её в переменную окружения `COMMUNITY_CHAT_URL` (на Amvera —
   в переменных приложения) и перезапустить бота.

Пока переменная пустая или в ней не ссылка на Telegram, раздела в боте нет
вовсе: ни кнопки в меню, ни команды в «/»-меню (`config.community_available`).
Это же и способ временно закрыть чат — убрать переменную, ссылка перестаёт
показываться новым людям.

Модерация — ручная, средствами Telegram: бот не админ группы и правил в ней не
знает. Первым делом стоит закрыть группу от спама штатными настройками
(одобрение заявок, запрет ссылок новичкам) — с ботом это никак не связано.
