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
* Пользовательский текст (то, что атлет ввёл сам) в сценарии написан на
  языке атлета, поэтому исключений по полям почти нет: `EN_IDENTITY_KEYS` —
  идентичность шаблона каталога, она русская навсегда по замыслу
  (см. модульную докстрингу seed_data), `MACHINE_KEYS` — машинные поля
  (коды, даты, ключи), в которых проверять русский текст бессмысленно.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx
import pytest

import ai_trainer
import api_v1
import api_v1_feedback
import config
import i18n

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
    "vs",       # «(↓3кг vs 23.09)» у e1RM — так бот пишет и в русской карточке
                # тренировки (formatting.e1rm_line); в зале это обычное слово
}

# Идентичность каталога: русское имя шаблона, по которому ключуются картинки и
# описания (exercises.original_name). Не показывается как текст — клиент
# показывает display_name. По-русски навсегда, на любом языке аккаунта.
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
}

# Сочетания, которые в русском тексте продукта законны целиком (жаргон зала,
# так и пишется в каталоге программ бота), — вырезаются перед проверкой слов.
RU_ALLOWED_LATIN_PHRASES = ("full body",)


def _string_leaves(value: Any, key: str | None = None, path: str = ""):
    if isinstance(value, str):
        yield key, path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _string_leaves(v, k, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _string_leaves(v, key, f"{path}[{i}]")


def language_violations(payload: Any, lang: str) -> list[str]:
    found = []
    for key, path, text in _string_leaves(payload):
        if key in MACHINE_KEYS:
            continue
        if lang == "en":
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
NON_JSON = {"GET /export/csv"}


class _Walker:
    def __init__(self, client: httpx.AsyncClient, lang: str):
        self.client = client
        self.lang = lang
        self.walked: set[str] = set()
        self.violations: list[str] = []

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
        for v in language_violations(body, self.lang):
            self.violations.append(f"{method} {path} [{resp.status_code}]: {v}")
        return resp


# Текст, который атлет вводит сам, — на его языке.
USER_TEXT = {
    "en": {
        "group": "Core work", "exercise": "Cable crunch", "exercise2": "Wrist curl",
        "description": "Keep the elbows tucked.", "note": "Felt strong today", "program": "My split",
        "program2": "Spare split", "day": "Heavy day", "routine": "Quick pump", "food": "Rice bowl",
        "question": "How is my progress?", "feedback": "Love the app", "post": "Squats kill knees",
        "answer": "Three days a week", "csv_exercise": "Bench Press", "voice": "100 8",
    },
    "ru": {
        "group": "Кор", "exercise": "Скручивания в кроссовере", "exercise2": "Сгибания запястий",
        "description": "Локти прижаты.", "note": "Сегодня шло легко", "program": "Мой сплит",
        "program2": "Запасной сплит", "day": "Тяжёлый день", "routine": "Быстрая накачка", "food": "Гречка",
        "question": "Как мой прогресс?", "feedback": "Классное приложение", "post": "Присед убивает колени",
        "answer": "Три дня в неделю", "csv_exercise": "Жим лёжа", "voice": "100 8",
    },
}

MODEL_TEXT = {
    "en": {"answer": "Solid work — keep adding weight.", "question": "How many days a week can you train?",
           "program": "Full body", "day": "Day 1", "program_description": "A simple full-body base.",
           "food": "Rice", "verdict": "Mostly myth.", "comment": "Strong session."},
    "ru": {"answer": "Хорошая работа — добавляй вес.", "question": "Сколько дней в неделю можешь тренироваться?",
           "program": "Фуллбоди", "day": "День 1", "program_description": "Простая база на всё тело.",
           "food": "Рис", "verdict": "Скорее миф.", "comment": "Сильная тренировка."},
}

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
        if question.startswith("PROGRAM"):
            await ai_trainer.execute_tool(user_id, "propose_program", {
                "name": model["program"],
                "days": [{"name": model["day"], "exercises": [
                    {"name": "Жим штанги лёжа", "sets": 3, "reps_min": 5, "reps_max": 8},
                    {"name": "Присед со штангой", "sets": 3, "reps_min": 5, "reps_max": 8},
                ]}],
                "description": model["program_description"],
            }, on_program=on_program)
        elif question.startswith("SETUP"):
            await on_questions([{"question": model["question"], "choices": []}])
        else:
            # Настоящий инструмент тренера, пишущий в базу: он же собирает
            # подпись кнопки отката через i18n.t — под языком хода.
            await ai_trainer.execute_tool(
                user_id, "create_exercise", {"name": "Pallof press" if lang == "en" else "Жим Палоффа",
                                             "group": "Chest" if lang == "en" else "Грудь"},
                on_action=on_action,
            )
        if on_wire is not None:
            await on_wire(history + [{"role": "user", "content": question},
                                     {"role": "assistant", "content": model["answer"]}])
        return model["answer"]

    monkeypatch.setattr(ai_trainer, "ask", fake_ask)

    async def fake_transcribe(buf, user_id):
        return USER_TEXT[lang]["voice"]

    monkeypatch.setattr(ai_trainer, "transcribe_voice", fake_transcribe)

    async def fake_analyze_food(user_id, **kwargs):
        return {"is_food": True, "description": model["food"], "items": [], "calories": 250,
                "protein": 5, "fat": 1, "carbs": 55}

    monkeypatch.setattr(ai_trainer, "analyze_food", fake_analyze_food)

    async def fake_fact_check(user_id, post_text, image_data_url=None):
        return model["verdict"]

    monkeypatch.setattr(ai_trainer, "fact_check_post", fake_fact_check)

    async def fake_comment(user_id, workout_id):
        return model["comment"]

    monkeypatch.setattr(ai_trainer, "comment_on_workout", fake_comment)

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
    await fresh_db.update_user(111, goal="strength" if lang == "en" else "сила")

    anon = _client()
    headers = {"Accept-Language": "en-US,en;q=0.9" if lang == "en" else "ru-RU,ru;q=0.9"}
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
    resp = await w.call("POST", "/auth/link", json={"code": code}, expect=200)
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"

    # --- аккаунт ---
    await w.call("GET", "/me", expect=200)
    await w.call("GET", "/settings", expect=200)
    await w.call("PATCH", "/settings", json={"unit": "kg"}, expect=200)
    await w.call("GET", "/profile", expect=200)
    await w.call("POST", "/push/register", json={"device_token": "abc"}, expect=201)
    await w.call("DELETE", "/push/register", expect=200)
    await w.call("POST", "/account/telegram-link-code", expect=(200, 409))

    # --- группы, каталог, упражнения ---
    groups = (await w.call("GET", "/muscle-groups", expect=200)).json()
    chest_id = groups[0]["id"]
    await w.call("POST", "/muscle-groups", json={"name": text["group"]}, expect=201)
    await w.call("POST", "/muscle-groups", json={"name": ""}, expect=400)
    await w.call("GET", "/exercise-templates?query=" + ("bench" if lang == "en" else "жим"), expect=200)
    templates = (await w.call("GET", f"/exercise-templates?group_id={chest_id}", expect=200)).json()
    template_id = templates[0]["id"]
    await w.call("GET", f"/exercise-templates/{template_id}", expect=200)
    forked = (await w.call("POST", f"/exercise-templates/{template_id}/add", expect=(200, 201))).json()
    forked_id = forked["id"]
    squat_template = next(
        t for t in (await w.call("GET", "/exercise-templates?query=" + ("squat" if lang == "en" else "присед"),
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
    await w.call("POST", f"/exercises/{own_id}/photo",
                 json={"image_data_url": f"data:image/png;base64,{PNG_1PX}"}, expect=201)
    await w.call("DELETE", f"/exercises/{own_id}/photo", expect=200)
    await w.call("POST", f"/exercises/{own2_id}/archive", expect=200)
    await w.call("GET", "/exercises?archived=1", expect=200)
    await w.call("POST", f"/exercises/{own2_id}/unarchive", expect=200)
    await w.call("GET", "/exercises/999999/media", expect=404)

    # --- тренировка ---
    wid = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    await w.call("POST", "/workouts/active", json={}, expect=(200, 201, 409))
    set1 = (await w.call("POST", f"/workouts/{wid}/sets",
                         json={"exercise_id": forked_id, "weight": 100, "reps": 5}, expect=201)).json()
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": -5, "reps": 5},
                 expect=400)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": 100, "reps": 0},
                 expect=400)
    await w.call("POST", f"/workouts/{wid}/sets/parse", json={"exercise_id": forked_id, "text": "100 8"},
                 expect=201)
    await w.call("POST", f"/workouts/{wid}/sets/parse", json={"exercise_id": forked_id, "text": "???"},
                 expect=400)
    await w.call("POST", f"/workouts/{wid}/sets/voice",
                 json={"exercise_id": squat_id, "audio_data_url": "data:audio/m4a;base64,AAAA"}, expect=201)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": own_id, "weight": 20, "reps": 12},
                 expect=201)
    await w.call("GET", "/workouts/active", expect=200)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    await w.call("GET", f"/workouts/{wid}/exercises/{forked_id}/next-target", expect=200)
    await w.call("PATCH", f"/workouts/{wid}/exercises/{forked_id}/note", json={"note": text["note"]}, expect=200)
    await w.call("PATCH", f"/workouts/{wid}/note", json={"note": text["note"]}, expect=200)
    await w.call("DELETE", f"/workouts/{wid}/exercises/{own_id}/last-set", expect=200)
    await w.call("GET", f"/exercises/{forked_id}/superset-partners?workout_id={wid}", expect=200)
    await w.call("GET", f"/exercises/next-suggestions?last_finished_id={forked_id}", expect=200)
    await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
    # На день раньше второй тренировки ниже: сравнение «(Δ vs дата)» в её итогах
    # появляется, только если прошлая начата строго раньше. Обе заводятся в одну
    # секунду, и без явной даты тест проходил эту ветку через раз.
    await w.call("PATCH", f"/workouts/{wid}/date", json={"date": "2026-01-05"}, expect=200)
    await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": forked_id, "weight": 100, "reps": 5},
                 expect=409)
    await w.call("GET", "/workouts", expect=200)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    await w.call("GET", "/workouts/calendar?year=2026&month=9", expect=200)
    await w.call("GET", "/workouts/search?exercise=" + text["csv_exercise"][:3], expect=200)
    await w.call("GET", f"/workouts/{wid}/card", expect=200)
    await w.call("GET", f"/workouts/{wid}/ai-comment", expect=200)
    await w.call("POST", f"/workouts/{wid}/ai-comment", expect=200)
    await w.call("PATCH", f"/workouts/{wid}/sets/{set1['id']}", json={"reps": 6}, expect=200)
    await w.call("POST", f"/workouts/{wid}/exercises/{own_id}/sets", json={"weight": 20, "reps": 10},
                 expect=201)
    await w.call("DELETE", f"/workouts/{wid}/exercises/{own_id}", expect=200)
    await w.call("GET", f"/exercises/{forked_id}/progress", expect=200)
    await w.call("GET", f"/exercises/{forked_id}/progress/sessions", expect=200)
    await w.call("GET", "/export/csv", expect=200)
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
    await w.call("GET", "/achievements", expect=200)
    await w.call("GET", "/achievements/nearest", expect=200)
    await w.call("GET", "/achievements/stats", expect=200)
    await w.call("GET", "/hall-of-fame", expect=200)
    await w.call("GET", "/hall-of-fame/rank-ladder", expect=200)

    # --- вес тела и еда ---
    bw = (await w.call("POST", "/bodyweight", json={"weight": 80.5}, expect=201)).json()
    await w.call("GET", "/bodyweight", expect=200)
    await w.call("PATCH", f"/bodyweight/{bw['id']}", json={"weight": 81}, expect=200)
    await w.call("DELETE", f"/bodyweight/{bw['id']}", expect=200)
    await w.call("DELETE", f"/bodyweight/{bw['id']}", expect=404)
    food = (await w.call("POST", "/food", json={"name": text["food"], "kcal": 300}, expect=201)).json()
    await w.call("GET", "/food", expect=200)
    await w.call("GET", "/food/days", expect=200)
    await w.call("GET", f"/food/{food['id']}", expect=200)
    await w.call("POST", "/food/goal", json={"goal": 2200}, expect=200)
    await w.call("POST", "/food/goal", json={"goal": 99999}, expect=400)
    await w.call("POST", "/food/parse", json={"text": text["food"]}, expect=200)
    await w.call("DELETE", f"/food/{food['id']}", expect=200)

    # --- программы ---
    catalog = (await w.call("GET", "/programs/catalog", expect=200)).json()
    catalog_program = (await w.call("POST", f"/programs/catalog/{catalog[0]['key']}", json={},
                                    expect=201)).json()
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
    await w.call("DELETE", f"/routine-exercises/{item_id}", expect=200)
    day_id = day["id"] if "id" in day else day["days"][-1]["id"]
    await w.call("POST", f"/routines/{day_id}/reorder", json={"direction": "up"}, expect=(200, 400))
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
    await w.call("DELETE", f"/share/{share_rt['token']}", expect=200)
    await w.call("GET", f"/share/{share_rt['token']}", expect=404)

    # --- тренер ---
    await w.call("GET", "/ai/limits", expect=200)
    asked = (await w.call("POST", "/ai/ask", json={"question": text["question"]}, expect=200)).json()
    assert asked["actions"], "тренер завёл упражнение — кнопка отката обязана прийти"
    await w.call("POST", "/ai/ask", json={"question": ""}, expect=400)
    await w.call("GET", "/ai/history", expect=200)
    conversations = (await w.call("GET", "/ai/conversations", expect=200)).json()
    conv_items = conversations.get("conversations") or conversations.get("items") or []
    if conv_items:
        await w.call("GET", f"/ai/conversations/{conv_items[0]['id']}", expect=200)
    else:
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
    await w.call("POST", "/factcheck", json={"text": text["post"]}, expect=200)
    await w.call("POST", "/diagnostics", json={
        "kind": "crash", "payload": {"diagnosticMetaData": {"appVersion": "1.0", "signal": 11}},
    }, expect=201)
    await w.call("POST", "/diagnostics", json={"kind": "nope", "payload": {"a": 1}}, expect=400)
    await w.call("DELETE", "/profile", expect=200)

    # --- импорт ---
    csv_text = f"date,exercise,weight,reps\n2025-03-01,{text['csv_exercise']},100,5\n"
    await w.call("POST", "/import/csv/preview", json={"csv": csv_text}, expect=200)
    await w.call("POST", "/import/csv", json={"csv": csv_text, "create_missing_exercises": False},
                 expect=(200, 201))
    bad_csv = f"date,exercise,weight,reps\n2025-03-01,{text['csv_exercise']},-50,5\n"
    await w.call("POST", "/import/csv/preview", json={"csv": bad_csv}, expect=400)

    # --- слияние, удаление ---
    await w.call("POST", "/exercises/merge", json={"source_id": own2_id, "target_id": own_id}, expect=200)
    await w.call("DELETE", f"/routines/{rid}", expect=200)
    await w.call("DELETE", f"/programs/{pid}", expect=200)
    await w.call("DELETE", "/account", expect=400)
    await w.call("DELETE", "/account?confirm=delete", expect=200)
    await w.call("GET", "/settings", expect=401)

    w.captured = captured
    return w


@pytest.mark.parametrize("lang", ["en", "ru"])
async def test_no_foreign_language_in_any_v1_response(fresh_db, monkeypatch, tmp_path, lang):
    walker = await _scenario(fresh_db, monkeypatch, tmp_path, lang)
    assert not walker.violations, (
        f"язык {lang}: в ответах /v1 нашёлся чужой язык —\n" + "\n".join(walker.violations)
    )
    # Ход тренера шёл под языком атлета, и системный промпт кончается хвостом
    # его языка — иначе модель ответила бы на чужом.
    assert walker.captured["langs"] and set(walker.captured["langs"]) == {lang}
    assert walker.captured["system_prompt"].endswith(i18n.t_in(lang, "ai.language_tail"))


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
