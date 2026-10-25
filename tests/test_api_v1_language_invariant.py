"""Инвариант «человек видит только свой язык» для всего REST `/v1`.

Английский атлет не должен получить от сервера ни одной кириллической буквы,
русский — ни одного английского слова в человеческом тексте. Раньше это
держалось аудитами, и каждый аудит находил новую протечку: ответ тренера по-
русски, «Грудь» в списке групп, английское «daily question limit reached»
в сообщении об ошибке. Тут это проверяет CI.

Как устроено:

* `_scenario` проходит продукт целиком через сам API — заводит упражнения,
  форкает каталог, пишет тренировку, берёт программу из каталога, спрашивает
  тренера (модель подменена), шарит, импортирует CSV, — и каждый ответ
  прогоняется через проверку языка (`_Walker.call`). Ошибки (4xx) проверяются
  так же: их поле `message` показывает приложение.
* Маршруты перечисляются из таблицы приложения (`api_v1.routes`), а не
  списком в тесте: новый эндпоинт, который сценарий не вызвал и который не
  внесён в `SKIPPED` с причиной, валит `test_every_route_is_walked`.
* Тренер и атлет на РАЗНЫХ языках (`test_coach_and_client_on_different_languages`):
  данные атлета у тренера — имена его упражнений и групп, программа, которую
  тренер ему назначает или собирает с AI, — на языке АТЛЕТА, а хром экранов
  тренера — на языке тренера. Такие поля перечислены поимённо, путями, в
  `CLIENT_DATA_PATHS`, и проверяются не «пропуском», а языком атлета: русская
  буква в них у английского атлета — такая же протечка. Обратная сторона —
  `COACH_DATA_PATHS`: комментарий и имя тренера атлет видит как набрано.
* Пользовательский текст (то, что атлет ввёл сам) в сценарии написан на
  языке атлета, поэтому исключений по полям почти нет: `EN_IDENTITY_KEYS` —
  идентичность шаблона каталога, она русская навсегда по замыслу
  (см. модульную докстрингу seed_data), `MACHINE_KEYS` — машинные поля
  (коды, даты, ключи), в которых проверять русский текст бессмысленно.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
import time as _time
from typing import Any

import httpx
import pytest

import ai_trainer
import api_v1
import api_v1_feedback
import coach_service
import config
import i18n
import text_import

pytestmark = pytest.mark.asyncio

CYRILLIC = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD = re.compile(r"[A-Za-z]{2,}")

# Английские токены, законные в русском тексте продукта: бренды, единицы,
# общепринятые аббревиатуры зала. Каждое — словом, а не подстрокой.
RU_ALLOWED_LATIN = {
    "AI",       # «AI-тренер» — так продукт называет тренера и по-русски
    "RPE",      # шкала усилия, в зале её по-русски не называют
    "e1RM", "RM",  # расчётный максимум — то же
    "Telegram",  # бренд
    "Apple", "ID",  # «Apple ID» — бренд
    "kg", "lb",  # единицы, как их пишет бот в обоих языках
    "CSV",      # формат файла импорта
    "EZ",       # «EZ-гриф» — название снаряда
    "TRX",      # петли TRX — название снаряда
    "Push", "Pull", "Legs", "PPL",  # названия дней сплита — так их зовут и по-русски
    "Upper", "Lower",
}

# Идентичность каталога: русское имя шаблона, по которому ключуются картинки и
# описания (exercises.original_name, и то же поле `original_name` в сводке
# шаблона GET /exercise-templates — клиент читает его начиная с iOS PR #715,
# `ExerciseTemplateSummary.originalName`, чтобы узнать свою копию шаблона). Не
# показывается как текст — клиент показывает display_name/name. По-русски
# навсегда, на любом языке аккаунта.
EN_IDENTITY_KEYS = {"original_name"}

# Машинные поля: коды, ключи, даты, url, токены, перечисления. В них не бывает
# человеческого текста, и в русском прогоне латиница там законна ("finished",
# "epley", "kg", "2026-09-23T...").
MACHINE_KEYS = {
    "error", "detail", "code", "key", "token", "start_param", "kind", "status", "type",
    "unit", "lang", "e1rm_formula", "equipment_key", "draft_id", "url", "images", "animation",
    "started_at", "finished_at", "created_at", "logged_at", "eaten_on", "date", "day",
    "first_at", "last_at", "from", "to", "last_workout_at", "updated_at", "last_used_at",
    "slug", "icon", "emoji", "tier", "direction", "bodyweight_load", "source", "source_ref",
    "mime", "image_url", "photo_url", "media_url", "attachment_key", "role", "id_key",
    "achievement_key", "rank_key", "program_key", "catalog_key", "unit_label_key",
    "achieved_at", "unlocked_at", "since", "until", "period", "window", "metric",
    "trend", "state", "verdict_code", "category", "family", "goal_key", "level_key",
    "reason", "topic", "app_store_url",
    # Ник в Telegram — идентификатор, который человек выбрал сам, а не текст продукта.
    "username",
    # Идентификаторы пачки импорта и объединения (hex) — для undo-ручек.
    "batch_id", "merge_id",
    # Черновик CSV из /import/text/convert: машинные заголовки колонок и имена
    # упражнений так, как их написал сам атлет, — вход для /import/csv, а не
    # текст на экране.
    "csv",
    # Ссылка приглашения тренера для приложения (trainingdiary://coach/<код>).
    "app_link",
    # Идентификатор мини-игры (catch/weigh/sort/runner) — название рисует приложение.
    "game",
    # Маршрут пуша и строки «Уведомлений» (push_ios.ios_route): экран и неделя
    # — машинные значения, имя упражнения в нём — показываемое и проверяется.
    "screen", "week",
    # Пол атлета в /v1/settings и /v1/profile — перечисление male/female/null,
    # подпись «Мужской»/«Male» рисует приложение.
    "gender",
    # Подключение к Claude/ChatGPT (/v1/mcp/*): id клиента OAuth (uuid из
    # регистрации) и адрес коннектора — машинные значения.
    "client_id", "server_url",
    # Схема мышц упражнения (`media.muscles`, exercise_muscles): слаги зон
    # схемы тела приложения, подпись мышцы рисует приложение своим каталогом.
    "primary", "secondary",
}

# Сочетания, которые в русском тексте продукта законны целиком (жаргон зала,
# так и пишется в каталоге программ бота), — вырезаются перед проверкой слов.
RU_ALLOWED_LATIN_PHRASES = ("full body",)


# Данные атлета в ответах его тренеру — на языке АТЛЕТА (coach_service.client_lang):
# тренер составляет атлету программы, и упражнения, группы и программа должны
# называться так, как их видит сам атлет. Путь — без номеров элементов списка.
# Только для обходчика тренера с атлетом другого языка; всё остальное в этих
# же ответах (подписи, строки рекордов, ошибки) — на языке тренера.
CLIENT_DATA_PATHS: dict[str, frozenset[str]] = {
    # Карточка клиента: свежие рекорды и назначенные ему программы (копии у
    # атлета — на его языке, coach_service.assign_program).
    "GET /coach/clients/{client_id:user_id}": frozenset({
        ".records[].exercise", ".programs[].name", ".programs[].days[].name",
    }),
    "GET /coach/clients/{client_id:user_id}/workouts": frozenset({".workouts[].exercise_names[]"}),
    # Тренировка: имя упражнения и тег группы — атлета; строки рекорда и
    # прошлых подходов — подписи тренеру.
    "GET /coach/clients/{client_id:user_id}/workouts/{workout_id:int}": frozenset({
        ".workout.blocks[].exercises[].display_name", ".workout.blocks[].exercises[].group_tag",
    }),
    "GET /coach/clients/{client_id:user_id}/progress": frozenset({
        ".growth.tiles[].exercise", ".lifts[].exercise", ".records[].exercise", ".recent_records[].exercise",
    }),
    "GET /coach/clients/{client_id:user_id}/exercises": frozenset({
        ".exercises[].exercise", ".exercises[].group", ".groups[].name",
    }),
    # Имя копии у атлета.
    "POST /coach/clients/{client_id:user_id}/programs": frozenset({".name"}),
    # Программа атлету в чате AI-тренера (coach_program_ai): черновик —
    # программа ДЛЯ атлета. Реплика модели и подписи — тренеру.
    "POST /ai/ask": frozenset({
        ".program.name", ".program.description", ".program.days[].name",
        ".program.days[].items[].name", ".program.days[].items[].target",
    }),
    "POST /ai/program/save": frozenset({".name"}),
}

# Обратная сторона: у атлета — то, что набрал тренер (комментарий, имя), как
# набрано, то есть на языке тренера. `my_name` — имя, которым атлет назвался
# тренеру: в этом прогоне оно набрано письмом тренера (см. _coach_scenario).
COACH_DATA_PATHS: dict[str, frozenset[str]] = {
    "GET /workouts/{workout_id:int}/coach-comments": frozenset({".comments[].text", ".comments[].coach_name"}),
    "GET /coach/invites/{code}": frozenset({".coach_name"}),
    "POST /coach/invites/{code}/accept": frozenset({".coach_name", ".my_name"}),
    "GET /coach-links": frozenset({".coaches[].coach_name", ".coaches[].my_name"}),
    "PATCH /coach-links/{coach_id:user_id}": frozenset({".coach_name", ".my_name"}),
    # «Программа от тренера Миши» в списке программ атлета.
    "GET /programs": frozenset({"[].coach_name"}),
}

# Строки, где хром одного языка обёрнут вокруг данных другого, — в прогоне с
# разными языками их не проверить ни тем, ни другим языком; каждую сверяет
# сам тест дословно.
MIXED_PATHS: dict[str, frozenset[str]] = {
    # «Забрать: <имя программы атлету>» — кнопка тренеру вокруг имени на
    # языке атлета (test_coach_and_client_on_different_languages).
    "POST /ai/ask": frozenset({".program.label"}),
}


def _shape(path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", path)


def _string_leaves(value: Any, key: str | None = None, path: str = ""):
    if isinstance(value, str):
        yield key, path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _string_leaves(v, k, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _string_leaves(v, key, f"{path}[{i}]")


def language_violations(
    payload: Any, lang: str, data_paths: frozenset[str] = frozenset(), data_lang: str | None = None,
    mixed_paths: frozenset[str] = frozenset(),
) -> list[str]:
    """`data_paths` — поля, которые принадлежат другому человеку и проверяются
    его языком `data_lang` (см. CLIENT_DATA_PATHS); `mixed_paths` — при
    чужом языке не проверяются вовсе (MIXED_PATHS, их сверяет тест)."""
    found = []
    default_lang = lang
    for key, path, text in _string_leaves(payload):
        if key in MACHINE_KEYS:
            continue
        shape = _shape(path)
        if data_lang is not None and shape in mixed_paths:
            continue
        lang = data_lang if data_lang is not None and shape in data_paths else default_lang
        if lang != "ru":
            if key in EN_IDENTITY_KEYS:
                continue
            if CYRILLIC.search(text):
                found.append(f"{path} = {text!r}")
        else:
            # URL и data:-строки — не текст.
            if text.startswith(("http://", "https://", "data:", "/media/")):
                continue
            for phrase in RU_ALLOWED_LATIN_PHRASES:
                text = text.replace(phrase, "")
            words = [w for w in LATIN_WORD.findall(text) if w not in RU_ALLOWED_LATIN]
            if words:
                found.append(f"{path} = {text!r} (латиница: {words})")
    return found


def _route_for(method: str, path: str) -> str | None:
    from starlette.routing import Match

    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in api_v1.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return f"{method} {route.path}"
    return None


# Маршруты, которые сценарий не зовёт, — каждый с причиной.
SKIPPED: dict[str, str] = {
    "GET /media/exercises/{name:path}": "раздаёт файл картинки (не JSON, текста нет)",
    "GET /exercises/{exercise_id:int}/photo": "раздаёт байты фото (не JSON, текста нет)",
    "GET /ai/history/{turn_id:int}/image": "раздаёт байты картинки чата (не JSON)",
    "GET /support/photos/{message_id:int}": "раздаёт байты фото из поддержки (не JSON)",
}

# Ответы не-JSON, которые сценарий зовёт, но языком не проверяет.
NON_JSON = {"GET /export/csv", "GET /export/archive"}


class _Walker:
    def __init__(
        self, client: httpx.AsyncClient, lang: str, *,
        data_lang: str | None = None, data_paths: dict[str, frozenset[str]] | None = None,
    ):
        self.client = client
        self.lang = lang
        # Чужие данные в ответах (атлет у тренера, тренер у атлета) — на языке
        # их владельца: поля по маршрутам и этот язык.
        self.data_lang = data_lang
        self.data_paths = data_paths or {}
        self.walked: set[str] = set()
        self.violations: list[str] = []
        # Каждый JSON-ответ как есть — для контрактного теста с моделями
        # приложения (test_api_v1_ios_contract), он идёт тем же сценарием.
        self.responses: list[dict[str, Any]] = []

    async def call(self, method: str, path: str, *, expect: int | tuple[int, ...] | None = None, **kwargs):
        route = _route_for(method, path.split("?")[0])
        assert route is not None, f"нет маршрута для {method} {path}"
        self.walked.add(route)
        resp = await self.client.request(method, path, **kwargs)
        allowed = expect if isinstance(expect, tuple) else (expect,)
        if expect is not None:
            assert resp.status_code in allowed, f"{method} {path}: {resp.status_code} {resp.text[:300]}"
        else:
            assert resp.status_code < 500, f"{method} {path}: {resp.status_code} {resp.text[:300]}"
        if route in NON_JSON or not resp.headers.get("content-type", "").startswith("application/json"):
            return resp
        body = resp.json()
        self.responses.append({
            "method": method, "route": route, "path": path, "status": resp.status_code, "body": body,
        })
        mixed = MIXED_PATHS.get(route, frozenset()) if self.data_lang is not None else frozenset()
        for v in language_violations(
            body, self.lang, self.data_paths.get(route, frozenset()), self.data_lang, mixed,
        ):
            self.violations.append(f"{method} {path} [{resp.status_code}]: {v}")
        return resp


def _sse_events(raw: str) -> list[tuple[str, Any]]:
    """События `text/event-stream` (стрим тренера): (имя, data как JSON), без пингов."""
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        name, data = "message", None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        events.append((name, data))
    return events


# Текст, который атлет вводит сам, — на его языке.
USER_TEXT = {
    "en": {
        "group": "Core work", "exercise": "Cable crunch", "exercise2": "Wrist curl",
        "description": "Keep the elbows tucked.", "note": "Felt strong today", "program": "My split",
        "program2": "Spare split", "day": "Heavy day", "routine": "Quick pump", "food": "Rice bowl",
        "question": "How is my progress?", "feedback": "Love the app", "post": "Squats kill knees",
        "answer": "Three days a week", "csv_exercise": "Bench Press", "voice": "100 8",
        "coach_name": "Coach Mike", "client_name": "Masha",
    },
    "ru": {
        "group": "Кор", "exercise": "Скручивания в кроссовере", "exercise2": "Сгибания запястий",
        "description": "Локти прижаты.", "note": "Сегодня шло легко", "program": "Мой сплит",
        "program2": "Запасной сплит", "day": "Тяжёлый день", "routine": "Быстрая накачка", "food": "Гречка",
        "question": "Как мой прогресс?", "feedback": "Классное приложение", "post": "Присед убивает колени",
        "answer": "Три дня в неделю", "csv_exercise": "Жим лёжа", "voice": "100 8",
        "coach_name": "Тренер Миша", "client_name": "Маша",
    },
}

MODEL_TEXT = {
    # Ответ называет упражнения — своё и каталожное: карточки-ссылки под ним
    # (`mentions`) проходят ту же проверку языка, что и сам текст.
    "en": {"answer": "Solid work on the Barbell Bench Press — add Dumbbell Bench Press, keep adding weight.", "question": "How many days a week can you train?",
           "program": "Full body", "day": "Day 1", "program_description": "A simple full-body base.",
           "food": "Rice", "verdict": "Mostly myth.", "comment": "Strong session."},
    "ru": {"answer": "Жим штанги лёжа идёт хорошо — добавь жим гантелей лёжа и прибавляй вес.", "question": "Сколько дней в неделю можешь тренироваться?",
           "program": "Фуллбоди", "day": "День 1", "program_description": "Простая база на всё тело.",
           "food": "Рис", "verdict": "Скорее миф.", "comment": "Сильная тренировка."},
}

# Остальные языки (es, pt, de, fr, tr) проверяются по правилу английского —
# в ответе нет кириллицы; свой текст атлета и ответы модели — английские
# заглушки (латиница, проверке они не мешают).
NEW_LANGS = ("es", "pt", "de", "fr", "tr")
for _lang in NEW_LANGS:
    USER_TEXT[_lang] = USER_TEXT["en"]
    MODEL_TEXT[_lang] = MODEL_TEXT["en"]


def _accept_language(lang: str) -> str:
    return {"ru": "ru-RU,ru;q=0.9", "en": "en-US,en;q=0.9"}.get(lang, f"{lang};q=0.9")


PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
    )
).decode()


def _install_fakes(monkeypatch, tmp_path, lang: str) -> dict[str, Any]:
    """Всё, что ходит наружу (модель, Apple, отправка письма), — подменено.
    Подмены пишут текст на языке атлета, как это сделала бы модель с
    правильным языковым хвостом; язык самого хвоста проверяет
    `captured["tail_ok"]`."""
    model = MODEL_TEXT[lang]
    captured: dict[str, Any] = {"langs": []}

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)
    monkeypatch.setattr(ai_trainer, "is_voice_configured", lambda: True)
    monkeypatch.setattr(config, "EXERCISE_PHOTO_DIR", str(tmp_path / "photos"))
    monkeypatch.setattr(config, "AI_CHAT_MEDIA_DIR", str(tmp_path / "chat"))
    monkeypatch.setattr(config, "ADMIN_ID", 1)

    async def fake_ask(user_id, question, history, on_program=None, on_action=None, on_questions=None,
                       on_wire=None, **kwargs):
        captured["langs"].append(i18n.get_lang())
        captured["system_prompt"] = ai_trainer._system_prompt()
        # Программа атлету (coach_program_ai): инструменты — у субъекта.
        subject = kwargs.get("subject")
        execute = (
            (lambda name, args, **cb: subject.execute(name, args, on_program=cb.get("on_program")))
            if subject is not None else
            (lambda name, args, **cb: ai_trainer.execute_tool(user_id, name, args, **cb))
        )
        if question.startswith("PROGRAM"):
            # Программа атлету пишется на языке АТЛЕТА — так велит заметка
            # режима (coach.ai_chat.mode_lang.*); реплика тренеру — на его.
            program_text = MODEL_TEXT[subject.lang] if subject is not None else model
            await execute("propose_program", {
                # Своё имя у программы атлету: «Забрать» той же программы в
                # своём чате ниже по сценарию иначе упёрлось бы в занятое имя.
                "name": program_text["program"] + (" 2" if subject is not None else ""),
                "days": [{"name": program_text["day"], "exercises": [
                    {"name": "Жим штанги лёжа", "sets": 3, "reps_min": 5, "reps_max": 8},
                    {"name": "Присед со штангой", "sets": 3, "reps_min": 5, "reps_max": 8},
                ]}],
                "description": program_text["program_description"],
            }, on_program=on_program)
        elif question.startswith("SETUP"):
            await on_questions([{"question": model["question"], "choices": []}])
        else:
            # Стрим: строка «что тренер делает» — настоящий каталог
            # ai.tool_status.* под языком хода, как её шлёт цикл инструментов.
            on_status = kwargs.get("on_status")
            if on_status is not None:
                await on_status(ai_trainer._tool_status_variants("get_full_workout_history")[0])
                await on_status(ai_trainer._tool_status_variants("no_such_tool")[0])
            on_chunk = kwargs.get("on_chunk")
            if on_chunk is not None:
                await on_chunk(model["answer"])
            # Настоящий инструмент тренера, пишущий в базу: он же собирает
            # подпись кнопки отката через i18n.t — под языком хода.
            await ai_trainer.execute_tool(
                user_id, "create_exercise", {"name": "Pallof press" if lang != "ru" else "Жим Палоффа",
                                             "group": "Chest" if lang != "ru" else "Грудь"},
                on_action=on_action,
            )
        if on_wire is not None:
            await on_wire(history + [{"role": "user", "content": question},
                                     {"role": "assistant", "content": model["answer"]}])
        return model["answer"]

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    async def fake_transcribe(buf, user_id, duration_seconds=None):
        return USER_TEXT[lang]["voice"]

    monkeypatch.setattr(ai_trainer, "transcribe_voice", fake_transcribe)

    async def fake_fact_check(user_id, post_text, image_data_url=None):
        return model["verdict"]

    monkeypatch.setattr(ai_trainer, "fact_check_post", fake_fact_check)

    async def fake_comment(user_id, workout_id):
        return model["comment"]

    monkeypatch.setattr(ai_trainer, "comment_on_workout", fake_comment)

    async def fake_coach_summary(coach_id, messages):
        captured["langs"].append(i18n.get_lang())
        captured["coach_prompt"] = messages[0]["content"]
        return model["answer"]

    monkeypatch.setattr(coach_service, "_model_answer", fake_coach_summary)

    async def fake_extract_sets(user_id, text, today, lang=None):
        return text_import.ExtractResult(rows=[{
            "date": "2025-03-02", "exercise": USER_TEXT[lang]["csv_exercise"],
            "weight": 100.0, "unit": None, "reps": 5,
        }])

    monkeypatch.setattr(text_import, "extract_sets", fake_extract_sets)

    async def fake_send(user_id, text, photo):
        return None

    monkeypatch.setattr(api_v1_feedback, "_send_feedback_to_admin", fake_send)
    # Суточная квота писем разработчику — словарь в памяти процесса, и без
    # сброса третий прогон сценария упирался бы в неё.
    monkeypatch.setattr(api_v1_feedback, "_daily_counts", {})

    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=f"apple-{token}", email=None),
    )
    return captured


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api_v1.build_app()), base_url="http://test")


async def _scenario(fresh_db, monkeypatch, tmp_path, lang: str) -> _Walker:
    captured = _install_fakes(monkeypatch, tmp_path, lang)
    text = USER_TEXT[lang]
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    await fresh_db.update_user(111, goal="strength" if lang != "ru" else "сила")

    anon = _client()
    headers = {"Accept-Language": _accept_language(lang)}
    anon_walker = _Walker(anon, lang)
    await anon_walker.call("GET", "/health", expect=200)
    await anon_walker.call("POST", "/auth/link", json={"code": "nope"}, headers=headers, expect=400)
    # Воронка до входа (api_v1_funnel): без токена, ответ — машинный флаг,
    # а отказ по шагу вне белого списка — текст на языке тела.
    await anon_walker.call(
        "POST", "/funnel",
        json={"install_id": "8e7b0c9e-5b2a-4f4e-9d0a-2f1b8b2c3d4e", "step": "onboarding_slide_1", "lang": lang},
        expect=200,
    )
    await anon_walker.call(
        "POST", "/funnel",
        json={"install_id": "8e7b0c9e-5b2a-4f4e-9d0a-2f1b8b2c3d4e", "step": "nope", "lang": lang},
        headers=headers, expect=400,
    )
    apple = await anon_walker.call("POST", "/auth/apple", json={"identity_token": "new", "lang": lang}, expect=200)
    # Выход: без токена — 401 (язык ошибки из Accept-Language), с токеном —
    # гасит только его.
    await anon_walker.call("POST", "/auth/logout", headers=headers, expect=401)
    await anon_walker.call(
        "POST", "/auth/logout", headers={**headers, "Authorization": f"Bearer {apple.json()['token']}"},
        expect=200,
    )
    # Демо-вход App Review: без обоих секретов ручка — 404, поэтому включаем
    # и проверяем язык ошибки неверного пароля (успешный вход — тот же
    # _issue_token_response, что у /auth/apple выше).
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", "appreview")
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", "correct-horse")
    await anon_walker.call(
        "POST", "/auth/password",
        json={"username": "appreview", "password": "wrong", "lang": lang}, headers=headers, expect=401,
    )
    await anon_walker.call("GET", "/me", expect=401, headers=headers)

    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = _client()
    # Как у настоящего клиента: URLSession ставит Accept-Language по языкам
    # устройства сам. Нужен только тем ответам, у которых нет пользователя
    # (401 после удаления аккаунта), — остальное язык берёт из users.lang.
    client.headers.update(headers)
    w = _Walker(client, lang)
    w.walked |= anon_walker.walked
    w.violations += anon_walker.violations
    w.responses += anon_walker.responses
    resp = await w.call("POST", "/auth/link", json={"code": code}, expect=200)
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    # --- аккаунт ---
    await w.call("GET", "/me", expect=200)
    # Код реферальной ссылки (/i/ref_<id>): кривой — ошибка на языке атлета,
    # чужой — 200 (у атлета из бота источник уже есть или автора нет).
    await w.call("POST", "/me/referral", json={"code": "nope"}, expect=400)
    await w.call("POST", "/me/referral", json={"code": "ref_999"}, expect=200)
    # Выбора модели тренера больше нет: /me без ai_model/ai_model_options (iOS
    # строку «Модель тренера» не показывает), PATCH /ai/model — 410 текстом на
    # языке атлета, и своему аккаунту тоже.
    await w.call("PATCH", "/ai/model", json={"ai_model": "luna"}, expect=410)
    monkeypatch.setattr(config, "TEST_USER_ID", 111)
    me_own = await w.call("GET", "/me", expect=200)
    assert "ai_model_options" not in me_own.json() and "ai_model" not in me_own.json()
    await w.call("PATCH", "/ai/model", json={"ai_model": "luna"}, expect=410)
    monkeypatch.setattr(config, "TEST_USER_ID", None)
    await w.call("GET", "/settings", expect=200)
    await w.call("PATCH", "/settings", json={"unit": "kg"}, expect=200)
    await w.call("PATCH", "/settings", json={"rep_range_min": 8, "rep_range_max": 15}, expect=200)
    await w.call("PATCH", "/settings", json={"rep_range_min": 7, "rep_range_max": 9}, expect=400)
    await w.call("PATCH", "/settings", json={"lock_screen_used": True}, expect=200)
    await w.call("PATCH", "/settings", json={"lock_screen_used": "yes"}, expect=400)
    await w.call("PATCH", "/settings", json={"gender": "female"}, expect=200)
    await w.call("PATCH", "/settings", json={"gender": "other"}, expect=400)
    await w.call("PATCH", "/settings", json={"gender": None}, expect=200)
    await w.call("GET", "/profile", expect=200)
    await w.call("POST", "/push/register", json={"device_token": "abc"}, expect=201)
    await w.call("DELETE", "/push/register", expect=200)
    await w.call("POST", "/account/telegram-link-code", expect=(200, 409))
    await w.call("POST", "/mcp/link-code", expect=503)  # адреса MCP в тесте нет
    await w.call("GET", "/mcp/apps", expect=503)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "https://training-log.example.com")
    await w.call("POST", "/mcp/link-code", expect=200)
    # Подключённое приложение без имени в регистрации — запасное имя из
    # каталога обязано прийти на языке атлета.
    await fresh_db.save_oauth_client("00000000-0000-4000-8000-000000000001", None, json.dumps({"redirect_uris": []}))
    await fresh_db.create_oauth_token(
        access_token=f"walk-at-{lang}", refresh_token=f"walk-rt-{lang}", client_id="00000000-0000-4000-8000-000000000001",
        user_id=111, scopes="[]", resource=None,
        expires_at=_time.time() + 3600, refresh_expires_at=_time.time() + 86400,
    )
    await w.call("GET", "/mcp/apps", expect=200)
    await w.call("DELETE", "/mcp/apps/00000000-0000-4000-8000-000000000001", expect=200)
    await w.call("DELETE", "/mcp/apps/00000000-0000-4000-8000-000000000001", expect=200)
    monkeypatch.setattr(config, "MCP_PUBLIC_URL", "")

    # --- группы, каталог, упражнения ---
    groups = (await w.call("GET", "/muscle-groups", expect=200)).json()
    chest_id = groups[0]["id"]
    await w.call("POST", "/muscle-groups", json={"name": text["group"]}, expect=201)
    await w.call("POST", "/muscle-groups", json={"name": ""}, expect=400)
    await w.call("GET", "/exercise-templates?query=" + ("bench" if lang != "ru" else "жим"), expect=200)
    templates = (await w.call("GET", f"/exercise-templates?group_id={chest_id}", expect=200)).json()
    template_id = templates[0]["id"]
    await w.call("GET", f"/exercise-templates/{template_id}", expect=200)
    forked = (await w.call("POST", f"/exercise-templates/{template_id}/add", expect=(200, 201))).json()
    forked_id = forked["id"]
    squat_template = next(
        t for t in (await w.call("GET", "/exercise-templates?query=" + ("squat" if lang != "ru" else "присед"),
                                 expect=200)).json()
    )
    squat_id = (await w.call("POST", f"/exercise-templates/{squat_template['id']}/add",
                             expect=(200, 201))).json()["id"]
    own_id = (await w.call("POST", "/exercises", json={"name": text["exercise"], "group_id": chest_id},
                           expect=201)).json()["id"]
    own2_id = (await w.call("POST", "/exercises", json={"name": text["exercise2"]}, expect=201)).json()["id"]
    await w.call("POST", "/exercises", json={"name": text["exercise"]}, expect=(200, 201, 409))
    await w.call("GET", "/exercises", expect=200)
    await w.call("GET", f"/exercises?group_id={chest_id}", expect=200)
    await w.call("GET", "/exercises?query=" + text["exercise"][:4], expect=200)
    await w.call("PATCH", f"/exercises/{own_id}", json={"description": text["description"]}, expect=200)
    await w.call("GET", f"/exercises/{forked_id}/description", expect=200)
    await w.call("GET", f"/exercises/{own2_id}/description", expect=404)
    await w.call("GET", f"/exercises/{forked_id}/media", expect=200)
    await w.call("GET", f"/exercises/{squat_id}/alternatives", expect=200)
    await w.call("GET", f"/exercises/{own_id}/alternatives", expect=200)
    await w.call("POST", f"/exercises/{own_id}/photo",
                 json={"image_data_url": f"data:image/png;base64,{PNG_1PX}"}, expect=201)
    await w.call("DELETE", f"/exercises/{own_id}/photo", expect=200)
    await w.call("POST", f"/exercises/{own2_id}/archive", expect=200)
    await w.call("GET", "/exercises?archived=1", expect=200)
    await w.call("POST", f"/exercises/{own2_id}/unarchive", expect=200)
    # «Сделать копию»: копия каталожного упражнения и все три отказа — текст
    # ошибки (то же имя / занято / пусто / нет такого) на языке атлета.
    await w.call("POST", f"/exercises/{forked_id}/copy", json={"name": text["exercise"] + " 2"}, expect=201)
    await w.call("POST", f"/exercises/{own_id}/copy", json={"name": text["exercise"]}, expect=409)
    await w.call("POST", f"/exercises/{own_id}/copy", json={"name": text["exercise2"]}, expect=409)
    await w.call("POST", f"/exercises/{own_id}/copy", json={"name": " "}, expect=400)
    await w.call("POST", "/exercises/999999/copy", json={"name": text["exercise"] + " 3"}, expect=404)
    # «Сделать копию» шаблона каталога: копия и отказы — то же имя, что у
    # шаблона на языке атлета, занятое своим, пустое, нет такого шаблона.
    await w.call("POST", f"/exercise-templates/{template_id}/copy",
                 json={"name": text["exercise"] + " 4"}, expect=201)
    await w.call("POST", f"/exercise-templates/{template_id}/copy",
                 json={"name": templates[0]["name"]}, expect=409)
    await w.call("POST", f"/exercise-templates/{template_id}/copy", json={"name": text["exercise"]}, expect=409)
    await w.call("POST", f"/exercise-templates/{template_id}/copy", json={"name": " "}, expect=400)
    await w.call("POST", "/exercise-templates/999999/copy", json={"name": text["exercise"] + " 5"}, expect=404)
    await w.call("GET", "/exercises/999999/media", expect=404)

    # --- тренировка ---
    wid = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    await w.call("POST", "/workouts/active", json={}, expect=(200, 201, 409))
    # Офлайн-синхронизация: тексты новых ошибок — на языке атлета.
    for bad in (
        {"client_id": "not-a-uuid"},
        {"started_at": "2020-01-01T10:00:00Z"},
        {"client_id": "0b9f6c1e-6f57-4a3c-9d0e-6a1f2b3c4d5e", "started_at": "вчера"},
        {"client_id": "0b9f6c1e-6f57-4a3c-9d0e-6a1f2b3c4d5e", "started_at": "2020-01-01T10:00:00Z"},
        {"client_id": "0b9f6c1e-6f57-4a3c-9d0e-6a1f2b3c4d5e", "started_at": "2999-01-01T10:00:00Z"},
    ):
        await w.call("POST", "/workouts/active", json=bad, expect=400)
    set1 = (await w.call("POST", f"/workouts/{wid}/sets",
                         json={"exercise_id": forked_id, "weight": 100, "reps": 5}, expect=201)).json()
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": -5, "reps": 5},
                 expect=400)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": 100, "reps": 0},
                 expect=400)
    # Время подхода с телефона: мусор в `created_at` не отказ (очередь приложения
    # сочла бы 400 окончательным и потеряла подход) — подход пишется «сейчас».
    await w.call("POST", f"/workouts/{wid}/sets",
                 json={"exercise_id": forked_id, "weight": 100, "reps": 5, "created_at": "вчера"}, expect=201)
    await w.call("POST", f"/workouts/{wid}/sets/parse",
                 json={"exercise_id": forked_id, "text": "100 8", "created_at": "вчера"}, expect=201)
    await w.call("POST", f"/workouts/{wid}/sets/parse", json={"exercise_id": forked_id, "text": "100 8"},
                 expect=201)
    await w.call("POST", f"/workouts/{wid}/sets/parse", json={"exercise_id": forked_id, "text": "???"},
                 expect=400)
    # Подозрительная строка у клиента, который просит спросить: вопрос тренера
    # (и предложение «60 8») — на языке атлета, ничего не записано.
    await w.call("POST", f"/workouts/{wid}/sets/parse",
                 json={"exercise_id": forked_id, "text": "8 60", "confirm_suspicious": True}, expect=200)
    await w.call("POST", f"/workouts/{wid}/sets/voice",
                 json={"exercise_id": squat_id, "audio_data_url": "data:audio/m4a;base64,AAAA"}, expect=201)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": own_id, "weight": 20, "reps": 12},
                 expect=201)
    # Подтягивания — «только повторы»: вес — отказ с подсказкой про вариант
    # «с весом» на языке атлета, повторы — подход и вопрос о весе тела.
    pull_tpl = next(
        t for t in (await w.call("GET", "/exercise-templates?query=pull", expect=200)).json()
        if t["original_name"] == "Подтягивания"
    )
    pull_id = (await w.call("POST", f"/exercise-templates/{pull_tpl['id']}/add",
                            expect=(200, 201))).json()["id"]
    refused = (await w.call("POST", f"/workouts/{wid}/sets",
                            json={"exercise_id": pull_id, "weight": 10, "reps": 8}, expect=400)).json()
    assert refused["error"] == "weight_on_bodyweight_exercise"
    await w.call("POST", f"/workouts/{wid}/sets/parse", json={"exercise_id": pull_id, "text": "10 8"},
                 expect=400)
    logged = (await w.call("POST", f"/workouts/{wid}/sets",
                           json={"exercise_id": pull_id, "weight": 0, "reps": 8}, expect=201)).json()
    assert logged["bodyweight_prompt"] == {"kind": "missing"}
    await w.call("POST", "/bodyweight/prompt/later", expect=200)
    await w.call("GET", "/workouts/active", expect=200)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    await w.call("GET", f"/workouts/{wid}/exercises/{forked_id}/next-target", expect=200)
    await w.call("PATCH", f"/workouts/{wid}/exercises/{forked_id}/note", json={"note": text["note"]}, expect=200)
    await w.call("PATCH", f"/workouts/{wid}/note", json={"note": text["note"]}, expect=200)
    await w.call("DELETE", f"/workouts/{wid}/exercises/{own_id}/last-set", expect=200)
    # Упражнение без единого подхода: «🎯 Цель» с диапазоном повторов из настроек.
    no_history = (await w.call("GET", f"/workouts/{wid}/exercises/{own_id}/next-target", expect=200)).json()
    assert no_history["hint"]["no_history"] is True
    await w.call("GET", f"/exercises/{forked_id}/superset-partners?workout_id={wid}", expect=200)
    await w.call("GET", f"/exercises/next-suggestions?last_finished_id={forked_id}", expect=200)
    await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
    # На день раньше второй тренировки ниже: сравнение «(+Δ к дате)» в её итогах
    # появляется, только если прошлая начата строго раньше. Обе заводятся в одну
    # секунду, и без явной даты тест проходил эту ветку через раз.
    await w.call("PATCH", f"/workouts/{wid}/date", json={"date": "2026-01-05"}, expect=200)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": 100, "reps": 5},
                 expect=409)
    await w.call("GET", "/workouts", expect=200)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    await w.call("GET", "/workouts/calendar?year=2026&month=9", expect=200)
    await w.call("GET", "/workouts/visits", expect=200)
    await w.call("GET", "/workouts/search?exercise=" + text["csv_exercise"][:3], expect=200)
    await w.call("GET", f"/workouts/{wid}/card", expect=200)
    await w.call("GET", f"/workouts/{wid}/ai-comment", expect=200)
    await w.call("POST", f"/workouts/{wid}/ai-comment", expect=200)
    await w.call("POST", "/ai/conversations/workout", json={"workout_id": wid}, expect=200)
    await w.call("PATCH", f"/workouts/{wid}/sets/{set1['id']}", json={"reps": 6}, expect=200)
    await w.call("POST", f"/workouts/{wid}/exercises/{own_id}/sets", json={"weight": 20, "reps": 10},
                 expect=201)
    await w.call("DELETE", f"/workouts/{wid}/exercises/{own_id}", expect=200)
    await w.call("GET", f"/exercises/{forked_id}/progress", expect=200)
    await w.call("GET", f"/exercises/{forked_id}/progress/sessions", expect=200)
    await w.call("GET", "/export/csv", expect=200)
    # ZIP: README.txt внутри на языке атлета держит tests/test_full_export.py.
    await w.call("GET", "/export/archive", expect=200)
    rid_from_workout = (await w.call("POST", f"/workouts/{wid}/routines", json={"name": text["routine"]},
                                     expect=201)).json()["id"]
    await w.call("POST", f"/workouts/{wid}/repeat", expect=(200, 201))
    await w.call("POST", f"/workouts/{wid}/repeat", expect=409)
    await w.call("DELETE", "/workouts/active", expect=200)
    await w.call("GET", "/workouts/active", expect=(200, 404))

    # Вторая тренировка — задним числом, её потом удаляем.
    bf = (await w.call("POST", "/workouts/backfill", json={"date": "2026-01-10"}, expect=(200, 201))).json()
    await w.call("GET", "/workouts/backfill", expect=200)
    await w.call("POST", f"/workouts/{bf['id']}/sets", json={"exercise_id": squat_id, "weight": 60, "reps": 5},
                 expect=201)
    await w.call("DELETE", "/workouts/backfill", expect=200)
    await w.call("POST", "/workouts/backfill", json={"date": "2999-01-01"}, expect=400)
    wid2 = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    set2 = (await w.call("POST", f"/workouts/{wid2}/sets", json={"exercise_id": squat_id, "weight": 80, "reps": 5},
                         expect=201)).json()
    await w.call("POST", f"/workouts/{wid2}/finish", json={}, expect=200)
    await w.call("PATCH", f"/workouts/{wid2}/date", json={"date": "2026-02-01"}, expect=200)
    await w.call("DELETE", f"/workouts/{wid2}/sets/{set2['id']}", expect=200)
    await w.call("DELETE", f"/workouts/{wid2}", expect=200)
    await w.call("GET", "/workouts/999999", expect=404)

    # --- сводки ---
    await w.call("GET", "/dashboard", expect=200)
    # «ТЫ» по дням для видео «Как ты рос»: имя звания каждой точки — на языке атлета.
    await w.call("GET", "/dashboard/timeline", expect=200)
    # Итог недели: воскресный разбор тренера лежит на языке атлета — он
    # проходит ту же проверку, что и тексты вердикта. Без недели — прошлая
    # (у сценария она пустая), с неделей — текущая (первая неделя атлета).
    import timeutil
    import weekly_summary

    this_monday = weekly_summary.week_monday(timeutil.user_today(await fresh_db.get_user(111)))
    await fresh_db.save_weekly_digest(111, this_monday.isoformat(), lang, MODEL_TEXT[lang]["answer"])
    await w.call("GET", "/weekly-summary", expect=200)
    await w.call("GET", f"/weekly-summary?week={this_monday.isoformat()}", expect=200)
    await w.call("GET", "/weekly-summary?week=2999-01-05", expect=400)
    await w.call("GET", "/weekly-summary?week=soon", expect=400)
    await w.call("GET", "/achievements", expect=200)
    await w.call("GET", "/achievements/nearest", expect=200)
    await w.call("GET", "/achievements/stats", expect=200)
    await w.call("GET", "/hall-of-fame", expect=200)
    await w.call("GET", "/hall-of-fame/rank-ladder", expect=200)

    # --- мини-игры между подходами ---
    # Пустой экран (все значки — цели), партия, отвергнутая партия, неизвестная
    # игра, таблица лидеров. Имя в таблице — «Атлет #NNNN» на языке атлета;
    # показ ника включается и выключается обратно ДО чтения таблицы: ник —
    # латиница, выбранная самим атлетом, а не текст продукта.
    import api_v1_games

    api_v1_games.reset_rate_limit()
    await w.call("GET", "/games", expect=200)
    await w.call("POST", "/games/results", json={"game": "catch", "score": 60, "duration_ms": 60_000}, expect=201)
    await w.call("POST", "/games/results", json={"game": "runner", "score": 50, "duration_ms": 10_000}, expect=201)
    await w.call("POST", "/games/results", json={"game": "weigh", "score": 99, "duration_ms": 1_000}, expect=202)
    await w.call("POST", "/games/results", json={"game": "chess", "score": 1, "duration_ms": 5_000}, expect=400)
    await w.call("GET", "/games", expect=200)
    await w.call("PATCH", "/games/settings", json={"show_name": True}, expect=200)
    await w.call("PATCH", "/games/settings", json={"show_name": "yes"}, expect=400)
    await w.call("PATCH", "/games/settings", json={"show_name": False}, expect=200)
    await w.call("GET", "/games/catch/leaderboard", expect=200)
    await w.call("GET", "/games/catch/leaderboard?period=all", expect=200)
    await w.call("GET", "/games/catch/leaderboard?period=year", expect=400)
    await w.call("GET", "/games/chess/leaderboard", expect=400)

    # --- вес тела и еда ---
    bw = (await w.call("POST", "/bodyweight", json={"weight": 80.5}, expect=201)).json()
    await w.call("GET", "/bodyweight", expect=200)
    await w.call("PATCH", f"/bodyweight/{bw['id']}", json={"weight": 81}, expect=200)
    await w.call("DELETE", f"/bodyweight/{bw['id']}", expect=200)
    await w.call("DELETE", f"/bodyweight/{bw['id']}", expect=404)
    # Дневника еды больше нет нигде: каждая ручка — 410 с текстом
    # на языке атлета (api.error.food_gone), его и проверяет обходчик.
    await w.call("POST", "/food", json={"name": text["food"], "kcal": 300}, expect=410)
    await w.call("GET", "/food", expect=410)
    await w.call("GET", "/food/days", expect=410)
    await w.call("GET", "/food/1", expect=410)
    await w.call("POST", "/food/goal", json={"goal": 2200}, expect=410)
    await w.call("POST", "/food/parse", json={"text": text["food"]}, expect=410)
    await w.call("DELETE", "/food/1", expect=410)

    # --- программы ---
    catalog = (await w.call("GET", "/programs/catalog", expect=200)).json()
    # Упражнение в архиве — программа достаёт его обратно и называет в
    # `unarchived`: это имя тоже обязано прийти на языке атлета.
    await w.call("POST", f"/exercises/{squat_id}/archive", expect=200)
    catalog_program = (await w.call("POST", f"/programs/catalog/{catalog[0]['key']}", json={},
                                    expect=201)).json()
    assert isinstance(catalog_program["unarchived"], list)
    await w.call("POST", f"/exercises/{squat_id}/unarchive", expect=200)
    await w.call("POST", f"/programs/catalog/{catalog[0]['key']}", json={}, expect=409)
    await w.call("GET", "/programs", expect=200)
    await w.call("GET", f"/programs/{catalog_program['id']}", expect=200)
    await w.call("GET", f"/programs/{catalog_program['id']}/next-day", expect=200)
    pid = (await w.call("POST", "/programs", json={"name": text["program"]}, expect=201)).json()["id"]
    pid2 = (await w.call("POST", "/programs", json={"name": text["program2"]}, expect=201)).json()["id"]
    await w.call("PATCH", f"/programs/{pid}", json={"description": text["description"]}, expect=200)
    day = (await w.call("POST", f"/programs/{pid}/days", json={"name": text["day"]}, expect=201)).json()
    await w.call("POST", f"/programs/{pid2}/days", json={"name": text["day"]}, expect=201)
    await w.call("POST", f"/programs/{pid2}/merge", json={"into_id": pid}, expect=200)
    await w.call("GET", "/routines", expect=200)
    rid = (await w.call("POST", "/routines", json={"name": text["routine"] + " 2"}, expect=201)).json()["id"]
    await w.call("GET", f"/routines/{rid}", expect=200)
    await w.call("PATCH", f"/routines/{rid}", json={"name": text["routine"] + " 3"}, expect=200)
    item = (await w.call("POST", f"/routines/{rid}/exercises", json={"exercise_id": forked_id, "target": "3×8"},
                         expect=201)).json()
    item_id = item["id"] if "id" in item else item["exercises"][0]["id"]
    await w.call("POST", f"/routines/{rid}/exercises", json={"exercise_id": squat_id}, expect=201)
    await w.call("PATCH", f"/routine-exercises/{item_id}", json={"target": "4×6"}, expect=200)
    await w.call("POST", f"/routine-exercises/{item_id}/reorder", json={"direction": "down"}, expect=200)
    await w.call("POST", f"/routines/{rid}/exercises/order", json={"item_ids": [item_id]}, expect=200)
    await w.call("POST", f"/routines/{rid}/exercises/order", json={"item_ids": "x"}, expect=400)
    await w.call("DELETE", f"/routine-exercises/{item_id}", expect=200)
    day_id = day["id"] if "id" in day else day["days"][-1]["id"]
    await w.call("POST", f"/routines/{day_id}/reorder", json={"direction": "up"}, expect=(200, 400))
    await w.call("POST", f"/programs/{pid}/days/order", json={"routine_ids": [day_id]}, expect=200)
    await w.call("POST", f"/programs/{pid}/days/order", json={}, expect=400)
    detached = (await w.call("PATCH", f"/routines/{day_id}", json={"program_id": None}, expect=200)).json()
    await w.call("POST", f"/routines/{day_id}/attach", json={"program_id": detached["former_program_id"],
                                                             "position": detached["former_position"]}, expect=200)
    await w.call("POST", f"/routines/{day_id}/attach", json={"program_id": pid}, expect=400)
    await w.call("POST", f"/routines/{rid}/reorder", json={"direction": "up"}, expect=(200, 400))
    await w.call("POST", "/programs", json={"name": ""}, expect=400)

    # --- шаринг ---
    share_ex = (await w.call("POST", f"/share/exercises/{forked_id}", expect=201)).json()
    await w.call("GET", f"/share/{share_ex['token']}", expect=200)
    await w.call("POST", f"/share/{share_ex['token']}/import", expect=400)
    share_prog = (await w.call("POST", f"/share/programs/{catalog_program['id']}", expect=201)).json()
    await w.call("GET", f"/share/{share_prog['token']}", expect=200)
    share_rt = (await w.call("POST", f"/share/routines/{rid_from_workout}", expect=201)).json()
    await w.call("GET", f"/share/{share_rt['token']}", expect=200)
    await w.call("GET", "/share/mine", expect=200)
    await w.call("DELETE", f"/share/{share_rt['token']}", expect=200)
    await w.call("GET", f"/share/{share_rt['token']}", expect=404)

    # --- сервис для тренеров: 111 — тренер, 222 — его атлет того же языка ---
    await _coach_scenario(fresh_db, w, headers, lang, text, catalog_program["id"])

    # --- тренер ---
    await w.call("GET", "/ai/limits", expect=200)
    asked = (await w.call("POST", "/ai/ask", json={"question": text["question"]}, expect=200)).json()
    assert asked["actions"], "тренер завёл упражнение — кнопка отката обязана прийти"
    assert len(asked["mentions"]["exercises"]) >= 2, "ответ назвал два упражнения — чипы обязаны прийти"
    await w.call("POST", "/ai/ask", json={"question": ""}, expect=400)
    # Тот же ход стримом: события `chunk`/`status`/`done` — не JSON-ответ, и
    # обходчик их не читает, поэтому разбираем их здесь и меряем тем же
    # `language_violations`.
    streamed = await w.call("POST", "/ai/ask", json={"question": text["question"], "stream": True}, expect=200)
    assert streamed.headers["content-type"].startswith("text/event-stream")
    stream_events = _sse_events(streamed.text)
    assert "status" in [name for name, _ in stream_events], "стрим обязан сказать, что тренер делает"
    assert stream_events[-1][0] == "done"
    for name, data in stream_events:
        for v in language_violations(data, lang):
            w.violations.append(f"POST /ai/ask stream [{name}]: {v}")
    await w.call("GET", "/ai/history", expect=200)
    conversations = (await w.call("GET", "/ai/conversations", expect=200)).json()
    conv_items = conversations.get("conversations") or conversations.get("items") or []
    if conv_items:
        await w.call("GET", f"/ai/conversations/{conv_items[0]['id']}", expect=200)
        await w.call("POST", f"/ai/conversations/{conv_items[0]['id']}/activate", expect=200)
    await w.call("POST", "/ai/conversations/999999/activate", expect=404)
    if not conv_items:
        await w.call("GET", "/ai/conversations/999999", expect=404)
    await w.call("GET", "/ai/pending", expect=200)
    await w.call("GET", "/ai/thinking", expect=200)
    await w.call("POST", "/ai/undo", json={"key": asked["actions"][0]["key"]}, expect=200)
    await w.call("POST", "/ai/undo", json={"key": asked["actions"][0]["key"]}, expect=(404, 409))
    await w.call("POST", "/ai/ask", json={"question": "SETUP " + text["question"]}, expect=200)
    await w.call("POST", "/ai/questions/answer", json={"question_index": 0, "answer": text["answer"]},
                 expect=200)
    await w.call("POST", "/ai/questions/answer", json={"question_index": 5, "answer": text["answer"]},
                 expect=409)
    program_turn = (await w.call("POST", "/ai/ask", json={"question": "PROGRAM " + text["question"]},
                                 expect=200)).json()
    draft_id = program_turn["program"]["draft_id"]
    # Имена групп в weekly_sets — показываемые данные: без непустого списка
    # инвариант их бы не прошёл.
    assert program_turn["program"]["weekly_sets"]
    await w.call("GET", "/ai/pending", expect=200)
    await w.call("POST", "/ai/program/train", json={"draft_id": draft_id}, expect=201)
    await w.call("DELETE", "/workouts/active", expect=200)
    program_turn = (await w.call("POST", "/ai/ask", json={"question": "PROGRAM " + text["question"]},
                                 expect=200)).json()
    await w.call("POST", "/ai/program/save", json={"draft_id": program_turn["program"]["draft_id"]},
                 expect=200)
    await w.call("POST", "/ai/program/save", json={"draft_id": "gone"}, expect=404)
    await w.call("POST", "/ai/voice", json={"audio_data_url": "data:audio/m4a;base64,AAAA"}, expect=200)
    await w.call("POST", "/ai/video", json={"video_data_url": "data:video/mp4;base64,AAAA"},
                 expect=(400, 503))
    await w.call("DELETE", "/ai/history", expect=200)
    await w.call("POST", "/feedback", json={"text": text["feedback"]}, expect=201)
    await w.call("POST", "/feedback", json={"text": ""}, expect=400)
    # Поддержка: своя ветка — как атлет; ветки всех — только админу, атлету
    # 403 с человеческим текстом на его языке.
    await w.call("POST", "/support/messages", json={"text": text["feedback"]}, expect=201)
    await w.call("POST", "/support/messages", json={"text": ""}, expect=400)
    await w.call("GET", "/support/messages", expect=200)
    await w.call("POST", "/support/read", expect=200)
    await w.call("GET", "/support/threads", expect=403)
    await w.call("GET", "/support/threads/111/messages", expect=403)
    await w.call("POST", "/support/threads/111/messages", json={"text": text["feedback"]}, expect=403)
    await w.call("POST", "/support/threads/111/read", expect=403)
    # «Уведомления» (api_v1_inbox): строки кладут настоящие отправители —
    # ответ поддержки, ежедневный пуш, анонс — на языке атлета; список,
    # страница по курсору, отметка одной и всех, и ошибка тела на его языке.
    import api_v1_support
    import inbox as inbox_module
    import push_ios
    import push_texts

    await api_v1_support.send_admin_reply(111, text["feedback"])
    title, body = await push_ios.ios_alert(111, push_texts.SKIP_CATEGORY_BY_DAY[7], lang)
    await inbox_module.deliver(
        111, push_texts.SKIP_CATEGORY_BY_DAY[7], title, body, lang=lang,
        route=push_ios.ios_route(push_texts.SKIP_CATEGORY_BY_DAY[7]),
    )
    title, body = await push_ios.ios_alert(111, push_ios.ANNOUNCEMENT, lang)
    await inbox_module.deliver(111, push_ios.ANNOUNCEMENT, title, body, lang=lang)
    listed = (await w.call("GET", "/inbox", expect=200)).json()
    # Тренер и клиент выше тоже уже положили сюда свои пуши.
    assert len(listed["items"]) >= 3 and listed["unread"] >= 3
    page = (await w.call("GET", "/inbox?limit=1", expect=200)).json()
    await w.call("GET", f"/inbox?limit=1&before={page['next_before']}", expect=200)
    await w.call("GET", "/inbox?before=nope", expect=400)
    await w.call("POST", "/inbox/read", json={"id": listed["items"][0]["id"]}, expect=200)
    await w.call("POST", "/inbox/read", json={"id": "nope"}, expect=400)
    await w.call("POST", "/inbox/read", json={"all": True}, expect=200)
    await w.call("POST", "/factcheck", json={"text": text["post"]}, expect=200)
    await w.call("POST", "/diagnostics", json={
        "kind": "crash", "payload": {"diagnosticMetaData": {"appVersion": "1.0", "signal": 11}},
    }, expect=201)
    await w.call("POST", "/diagnostics", json={"kind": "nope", "payload": {"a": 1}}, expect=400)
    await w.call("POST", "/events", json={"events": [
        {"name": "app_open"}, {"name": "screen_view", "props": {"screen": "progress"}},
    ]}, expect=200)
    await w.call("POST", "/events", json={"events": "nope"}, expect=400)
    await w.call("DELETE", "/profile", expect=200)

    # --- импорт ---
    csv_text = f"date,exercise,weight,reps\n2025-03-01,{text['csv_exercise']},100,5\n"
    await w.call("POST", "/import/csv/preview", json={"csv": csv_text}, expect=200)
    await w.call("POST", "/import/csv/preview", json={"csv": csv_text, "file_unit": "lb"}, expect=200)
    await w.call("POST", "/import/csv/preview", json={"csv": csv_text, "file_unit": "x"}, expect=400)
    await w.call("POST", "/import/csv", json={"csv": csv_text, "create_missing_exercises": False},
                 expect=(200, 201))
    bad_csv = f"date,exercise,weight,reps\n2025-03-01,{text['csv_exercise']},-50,5\n"
    await w.call("POST", "/import/csv/preview", json={"csv": bad_csv}, expect=400)
    await w.call("POST", "/import/text/convert", json={"text": f"{text['csv_exercise']} 100 5"}, expect=200)
    await w.call("POST", "/import/text/convert", json={"text": " "}, expect=400)
    await w.call("POST", "/import/csv", json={"csv": csv_text, "exercise_mapping": {"x": 999999}}, expect=400)
    # Похожее своё есть, а выбора нет — 409 с человеческим текстом; с выбором
    # — импорт, его можно отменить целиком (пачка импорта).
    choice_csv = f"date,exercise,weight,reps\n2025-03-02,{text['csv_exercise']},100,5\n"
    await w.call("POST", "/import/csv", json={"csv": choice_csv}, expect=(200, 409))
    imported = (await w.call("POST", "/import/csv", json={
        "csv": choice_csv, "exercise_mapping": [{"name": text["csv_exercise"], "exercise_id": own_id}],
    }, expect=200)).json()
    await w.call("GET", "/import/batches", expect=200)
    await w.call("POST", f"/import/batches/{imported['batch_id']}/undo", expect=200)
    await w.call("POST", f"/import/batches/{imported['batch_id']}/undo", expect=404)

    # --- слияние, удаление ---
    merged = (await w.call("POST", "/exercises/merge", json={"source_id": own2_id, "target_id": own_id}, expect=200)).json()
    await w.call("POST", f"/exercises/merges/{merged['merge_id']}/undo", expect=200)
    await w.call("POST", f"/exercises/merges/{merged['merge_id']}/undo", expect=404)
    await w.call("POST", "/exercises/merge", json={"source_id": own2_id, "target_id": own_id}, expect=200)
    await w.call("DELETE", f"/routines/{rid}", expect=200)
    await w.call("DELETE", f"/programs/{pid}", expect=200)
    await w.call("DELETE", "/account", expect=400)
    await w.call("DELETE", "/account?confirm=delete", expect=200)
    await w.call("GET", "/settings", expect=401)

    w.captured = captured
    return w


async def _coach_scenario(
    fresh_db, w: _Walker, headers: dict, lang: str, text: dict, program_id: int, client_lang: str | None = None,
) -> None:
    """Обе стороны сервиса для тренеров: тренер (111, walker сценария) и его
    атлет (222, свой walker) — каждая ручка /coach* и /coach-links*.

    `client_lang` — атлет на другом языке, чем тренер: его данные у тренера
    проверяются его языком (CLIENT_DATA_PATHS), комментарии тренера у него —
    языком тренера (COACH_DATA_PATHS). Имя, которым атлет назвался тренеру,
    в этом прогоне набрано письмом тренера: оно стоит внутри фраз тренеру
    («Программа для Маши»), и смешанный язык там проверить нечем."""
    alang = client_lang or lang
    atext = USER_TEXT[alang]
    if client_lang is not None:
        w.data_lang, w.data_paths = client_lang, CLIENT_DATA_PATHS
    await w.call("GET", "/coach", expect=200)
    await w.call("GET", "/coach/clients", expect=403)
    await w.call("POST", "/coach", json={"display_name": " "}, expect=400)
    await w.call("POST", "/coach", json={"display_name": text["coach_name"]}, expect=200)
    invite = (await w.call("POST", "/coach/invite", expect=201)).json()
    await w.call("GET", "/coach/clients", expect=200)

    await fresh_db.get_or_create_user(telegram_id=222, username="athlete")
    await fresh_db.set_user_lang(222, alang)
    athlete = _client()
    athlete.headers.update(headers)
    athlete.headers["Accept-Language"] = _accept_language(alang)
    athlete.headers["Authorization"] = f"Bearer {await fresh_db.issue_api_token(222)}"
    a = _Walker(athlete, alang)
    if client_lang is not None:
        a.data_lang, a.data_paths = lang, COACH_DATA_PATHS
    code = invite["code"]
    await a.call("GET", f"/coach/invites/{code}", expect=200)
    await a.call("GET", "/coach/invites/NOPE2345", expect=404)
    await w.call("POST", f"/coach/invites/{code}/accept", json={"name": text["client_name"]}, expect=400)
    await a.call("POST", f"/coach/invites/{code}/accept", json={"name": text["client_name"]}, expect=201)
    await a.call("POST", f"/coach/invites/{code}/accept", json={"name": text["client_name"]}, expect=409)
    await a.call("GET", "/coach-links", expect=200)
    await w.call("GET", "/coach/clients/222/bodyweight", expect=403)
    await a.call("PATCH", "/coach-links/111", json={"show_bodyweight": True}, expect=200)
    await a.call("PATCH", "/settings", json={"ai_consent": True}, expect=200)

    ex = (await a.call("POST", "/exercises", json={"name": atext["exercise"]}, expect=201)).json()["id"]
    # Каталожное упражнение атлета: у тренера оно обязано прийти на языке
    # атлета, а не тренера (идентичность каталога — одна, показ — его).
    bench = next(
        t for t in (await a.call("GET", "/exercise-templates?query=" + ("bench" if alang != "ru" else "жим"),
                                 expect=200)).json()
        if t["original_name"] == "Жим штанги лёжа"
    )
    bench_id = (await a.call("POST", f"/exercise-templates/{bench['id']}/add", expect=(200, 201))).json()["id"]
    # Две тренировки: старая легче — у свежей рекорд недели (records в карточке атлета).
    old = (await a.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    await a.call("POST", f"/workouts/{old}/sets", json={"exercise_id": ex, "weight": 50, "reps": 5}, expect=201)
    await a.call("POST", f"/workouts/{old}/sets", json={"exercise_id": bench_id, "weight": 70, "reps": 5},
                 expect=201)
    await a.call("POST", f"/workouts/{old}/finish", json={}, expect=200)
    old_day = (dt.date.today() - dt.timedelta(days=20)).isoformat()
    await a.call("PATCH", f"/workouts/{old}/date", json={"date": old_day}, expect=200)
    wid = (await a.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    await a.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": ex, "weight": 60, "reps": 5}, expect=201)
    await a.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": bench_id, "weight": 80, "reps": 5},
                 expect=201)
    await a.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
    await a.call("POST", "/bodyweight", json={"weight": 70.5}, expect=201)

    await w.call("GET", "/coach/clients", expect=200)
    await w.call("GET", "/coach/clients/222", expect=200)
    await w.call("GET", "/coach/clients/222/workouts", expect=200)
    await w.call("GET", f"/coach/clients/222/workouts/{wid}", expect=200)
    await w.call("GET", "/coach/clients/222/bodyweight", expect=200)
    await w.call("GET", "/coach/clients/999/workouts", expect=404)
    await w.call("GET", "/coach/clients/222/visits", expect=200)
    await w.call("GET", "/coach/clients/999/visits", expect=404)
    await w.call("GET", "/coach/clients/222/progress", expect=200)
    await w.call("GET", "/coach/clients/999/progress", expect=404)
    await w.call("GET", "/coach/clients/222/exercises", expect=200)
    await w.call("GET", "/coach/clients/999/exercises", expect=404)
    await w.call("GET", f"/coach/clients/222/exercises/{ex}/progress", expect=200)
    await w.call("GET", f"/coach/clients/222/exercises/{ex}/progress?limit=10", expect=200)
    await w.call("GET", f"/coach/clients/999/exercises/{ex}/progress", expect=404)
    # Своё упражнение тренера через id клиента — не упражнение этого атлета.
    own = (await w.call("POST", "/exercises", json={"name": text["exercise"] + " 3"}, expect=201)).json()["id"]
    await w.call("GET", f"/coach/clients/222/exercises/{own}/progress", expect=404)
    await w.call("POST", f"/coach/clients/222/workouts/{wid}/comments", json={"text": text["note"]}, expect=201)
    await w.call("POST", f"/coach/clients/222/workouts/{wid}/comments", json={"text": ""}, expect=400)
    await w.call("POST", "/coach/clients/222/programs", json={"program_id": program_id}, expect=201)
    empty = (await w.call("POST", "/programs", json={"name": text["program2"]}, expect=201)).json()["id"]
    await w.call("POST", "/coach/clients/222/programs", json={"program_id": empty}, expect=400)
    await w.call("DELETE", f"/programs/{empty}", expect=200)
    await w.call("GET", "/coach/clients/222", expect=200)
    await w.call("POST", "/coach/ai-summary", json={}, expect=200)
    await w.call("POST", "/coach/ai-summary", json={"client_id": 222}, expect=200)
    await w.call("GET", "/coach/ai-summary", expect=200)
    await w.call("GET", "/coach/ai-summary?client_id=222", expect=200)
    await w.call("GET", "/coach/ai-summary?client_id=999", expect=404)
    # Программа атлету в чате AI-тренера: отдельный разговор, ход по дневнику
    # атлета, «Забрать» тренеру, назначить, выйти в свой чат.
    await w.call("POST", "/ai/conversations/coach-client", json={"client_id": 999}, expect=404)
    await w.call("POST", "/ai/conversations/coach-client", json={"client_id": 222}, expect=201)
    await w.call("GET", "/ai/history", expect=200)
    client_turn = (await w.call("POST", "/ai/ask", json={"question": "PROGRAM " + text["question"]},
                                expect=200)).json()
    # Кнопка — тренеру на его языке, имя программы — атлету на его.
    assert client_turn["program"]["name"] == MODEL_TEXT[alang]["program"] + " 2"
    assert client_turn["program"]["label"] == i18n.t_in(
        lang, "btn.claim_program", program_name=client_turn["program"]["name"]
    )
    await w.call("POST", "/ai/program/train", json={"draft_id": client_turn["program"]["draft_id"]}, expect=409)
    client_program = (await w.call("POST", "/ai/program/save",
                                   json={"draft_id": client_turn["program"]["draft_id"]}, expect=200)).json()
    await w.call("POST", "/coach/clients/222/programs", json={"program_id": client_program["program_id"]},
                 expect=201)
    await w.call("POST", "/ai/conversations/coach-client/exit", expect=200)
    await a.call("GET", f"/workouts/{wid}/coach-comments", expect=200)
    await a.call("GET", "/programs", expect=200)
    await a.call("PATCH", "/coach-links/111", json={"show_bodyweight": False}, expect=200)
    await w.call("GET", "/coach/clients/222/bodyweight", expect=403)
    await w.call("DELETE", "/coach/clients/222", expect=200)
    await a.call("POST", f"/coach/invites/{code}/accept", json={"name": text["client_name"]}, expect=201)
    await a.call("DELETE", "/coach-links/111", expect=200)
    await a.call("DELETE", "/coach-links/111", expect=404)
    # Атлет сам закрыл доступ — у тренера серая строка `recently_unlinked`.
    gone = (await w.call("GET", "/coach/clients", expect=200)).json()["recently_unlinked"]
    assert [g["client_id"] for g in gone] == [222]

    w.walked |= a.walked
    w.violations += a.violations
    w.responses += a.responses
    w.data_lang, w.data_paths = None, {}


@pytest.mark.parametrize("lang", ["en", "ru", *NEW_LANGS])
async def test_no_foreign_language_in_any_v1_response(fresh_db, monkeypatch, tmp_path, lang):
    walker = await _scenario(fresh_db, monkeypatch, tmp_path, lang)
    assert not walker.violations, (
        f"язык {lang}: в ответах /v1 нашёлся чужой язык —\n" + "\n".join(walker.violations)
    )
    # Ход тренера шёл под языком атлета, и системный промпт кончается хвостом
    # его языка — иначе модель ответила бы на чужом.
    assert walker.captured["langs"] and set(walker.captured["langs"]) == {lang}
    assert walker.captured["system_prompt"].endswith(i18n.t_in(lang, "ai.language_tail"))
    # AI-сводка тренеру — тот же языковой хвост языка тренера.
    assert walker.captured["coach_prompt"].endswith(i18n.t_in(lang, "ai.language_tail"))


@pytest.mark.parametrize("coach_lang, client_lang", [("en", "ru"), ("ru", "en"), ("ru", "es"), ("de", "ru")])
async def test_coach_and_client_on_different_languages(fresh_db, monkeypatch, tmp_path, coach_lang, client_lang):
    """Тренер и атлет на разных языках: каждая ручка /coach* тем же
    сценарием, хром тренеру — на языке тренера, данные атлета — на языке
    атлета (CLIENT_DATA_PATHS), у атлета — всё на его языке, кроме набранного
    тренером (COACH_DATA_PATHS). И то, что ложится атлету, — его копии
    программ, — на его языке: каталожная и собранная с AI."""
    captured = _install_fakes(monkeypatch, tmp_path, coach_lang)
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, coach_lang)
    await fresh_db.update_user(111, ai_consent_at=fresh_db.now_iso())
    headers = {"Accept-Language": _accept_language(coach_lang)}
    client = _client()
    client.headers.update(headers)
    client.headers["Authorization"] = f"Bearer {await fresh_db.issue_api_token(111)}"
    w = _Walker(client, coach_lang)
    catalog = (await w.call("GET", "/programs/catalog", expect=200)).json()
    key = catalog[0]["key"]
    catalog_program = (await w.call("POST", f"/programs/catalog/{key}", json={}, expect=201)).json()

    await _coach_scenario(fresh_db, w, headers, coach_lang, USER_TEXT[coach_lang], catalog_program["id"],
                          client_lang=client_lang)

    assert not w.violations, (
        f"тренер {coach_lang}, атлет {client_lang}: чужой язык —\n" + "\n".join(w.violations)
    )
    # Реплика модели и сводка — тренеру, на его языке.
    assert set(captured["langs"]) == {coach_lang}
    assert captured["coach_prompt"].endswith(i18n.t_in(coach_lang, "ai.language_tail"))

    import seed_data

    copies = {p["name"]: p for p in await fresh_db.list_programs(222)}
    # Каталожная программа тренера — у атлета на языке атлета, с ключом
    # каталога: смена языка атлета переведёт её как его собственную.
    catalog_name = seed_data.localized_program_name(key, client_lang)
    assert catalog_name in copies, copies.keys()
    days = await fresh_db.list_program_days_by_id(copies[catalog_name]["id"])
    assert [d["name"] for d in days] == [
        seed_data.localized_program_day_name(key, i, client_lang) for i in range(len(days))
    ]
    # Программа, собранная тренером с AI для атлета, — на языке атлета: имя и
    # день пишет модель (по заметке режима), упражнения — по идентичности.
    ai_name = MODEL_TEXT[client_lang]["program"] + " 2"
    assert ai_name in copies, copies.keys()
    [day] = await fresh_db.list_program_days_by_id(copies[ai_name]["id"])
    assert day["name"] == MODEL_TEXT[client_lang]["day"]
    shown = [row["display_name"] for row in await fresh_db.list_routine_exercises(day["id"])]
    assert shown == [
        seed_data.localized_exercise_name("Жим штанги лёжа", client_lang),
        seed_data.localized_exercise_name("Присед со штангой", client_lang),
    ]


async def test_every_route_is_walked(fresh_db, monkeypatch, tmp_path):
    walker = await _scenario(fresh_db, monkeypatch, tmp_path, "en")
    all_routes = {
        f"{m} {r.path}" for r in api_v1.routes for m in (r.methods - {"HEAD"})
    }
    missing = sorted(all_routes - walker.walked - set(SKIPPED))
    assert not missing, (
        "эти маршруты /v1 не прошёл сценарий языкового инварианта и не внесены в SKIPPED "
        "с причиной — допиши их вызов в _scenario:\n" + "\n".join(missing)
    )
    stale = sorted(set(SKIPPED) - all_routes)
    assert not stale, f"в SKIPPED лежат маршруты, которых больше нет: {stale}"


@pytest.mark.parametrize("lang", ["en", "ru", *NEW_LANGS])
async def test_fresh_start_setup_and_history_have_no_foreign_language(fresh_db, monkeypatch, lang):
    """Короткий путь «Составь мне программу» у чистого листа (без модели) и
    видимая лента после опросника: ответ, вопросы, варианты и история — на
    языке атлета, без служебной рамки модели."""
    import ai_trainer

    monkeypatch.setattr(ai_trainer, "is_configured", lambda: True)

    async def fake_ask(user_id, question, history, **kwargs):
        return "Built." if lang != "ru" else "Собрал."

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    await fresh_db.set_user_lang(111, lang)
    client = _client()
    w = _Walker(client, lang)
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    resp = await w.call("POST", "/auth/link", json={"code": code}, expect=200)
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    seed = i18n.t_in(lang, "ai.screen.build_program_seed")
    asked = (await w.call("POST", "/ai/ask", json={
        "question": seed, "shown_question": i18n.t_in(lang, "ai.screen.build_program_intro"),
    }, expect=200)).json()
    assert asked["questions"]["total"] == 6
    for idx in range(6):
        await w.call("POST", "/ai/questions/answer", json={"question_index": idx, "answer": None}, expect=200)
    history = (await w.call("GET", "/ai/history", expect=200)).json()["messages"]
    assert all("propose_program" not in m["text"] for m in history)
    await w.call("DELETE", "/ai/history", expect=200)
    await w.call("GET", "/ai/conversations", expect=200)
    assert not w.violations, "\n".join(w.violations)
