"""Контракт «сервер ↔ iOS-приложение»: ответы /v1 против Swift-моделей.

Класс бага, ради которого тест заведён: сервер на лёгкой тренировке прислал
`rewards.tonnage_equivalent: null`, а `WorkoutRewards` в приложении объявляла
поле обязательной `String`. `JSONDecoder` падает на ВЕСЬ ответ, и `/finish`
не показывает экран итогов, хотя сервер ответил 200.

Как устроено:

* `tests/ios_contract.json` — снимок Swift-моделей и вызовов клиента, его строит
  `scripts/ios_contract.py` из чекаута iOS-репозитория (CI бота его не видит,
  поэтому снимок обновляют руками: `python scripts/ios_contract.py --ios ../training_log_bot_ios`).
  Для каждого типа в нём поля, их ключи после `convertFromSnakeCase` и
  `CodingKeys`, тип и обязательность; для каждого метода клиента — метод HTTP,
  путь и корневой тип, которым декодируется ответ.
* Тест берёт сценарий-обходчик языкового инварианта (`_scenario`: все ручки на
  данных атлета) плюс «неудобные» состояния (`EDGE_STATES`: подход 0×5,
  упражнение с весом тела, аккаунт в lb, пустая история) и сверяет КАЖДЫЙ
  2xx-ответ, у которого в снимке есть корневой тип, рекурсивно: обязательное
  поле есть и не null; тип совпадает (String ↔ str, Int ↔ int не bool,
  Double ↔ int|float, Bool ↔ bool, массив ↔ list, объект ↔ dict).
* Расхождения, о которых владелец знает и которые пока не чинятся, — в
  `KNOWN_MISMATCHES` с причиной. Запись, которая перестала воспроизводиться,
  валит тест: починили — вычеркни.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from test_api_v1_language_invariant import _client, _route_for, _scenario, _Walker

import config

MANIFEST_PATH = Path(__file__).with_name("ios_contract.json")
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

# Расхождения, о которых известно и которые не чинятся в этом PR.
# Ключ — `<МЕТОД /маршрут> :: <путь по Swift-ключам> :: <проблема>`, значение — причина.
KNOWN_MISMATCHES: dict[str, str] = {}

# Вызовы клиента, чей путь не находится среди маршрутов сервера. Каждый — с причиной.
KNOWN_UNROUTED: dict[str, str] = {}

# Структуры приложения, которых ни разу не было в проверенных ответах (массив пуст или
# поля нет), — каждая с причиной. Новая структура в манифесте без записи сюда обязана
# хоть раз прийти в ответе сценария — иначе проверка по ней была бы пустой.
NEVER_SEEN: dict[str, str] = {
    "AIMentionProgram": "нужен ответ тренера, в тексте которого названа программа атлета; "
                        "форму сервер собирает в api_v1_ai (kind/id/name) — сверено чтением кода",
    "SupersetPartner": "нужен суперсет-блок в прошлых тренировках; форму собирает "
                       "api_v1.superset_partners (id/name) — сверено чтением кода",
}

# Вызовы клиента, которые ни сценарий, ни «неудобные» состояния не довели до 2xx
# (поэтому их ответ не сверен). Каждый — с причиной; новый вызов клиента без
# записи сюда обязан быть пройден сценарием.
NOT_EXERCISED: dict[str, str] = {}


# --- сопоставление ключей и типов -----------------------------------------


def _foundation_capitalized(word: str) -> str:
    """`NSString.capitalized`: буква после не-буквы — заглавная, остальные —
    строчные. Цифра — граница слова: `e1rm` → `E1Rm`, а не `E1rm`."""
    out, prev_letter = [], False
    for ch in word:
        out.append(ch.lower() if prev_letter else ch.upper())
        prev_letter = ch.isalpha()
    return "".join(out)


def swift_key(key: str) -> str:
    """`JSONDecoder.KeyDecodingStrategy.convertFromSnakeCase`: ключ провода → ключ декодера."""
    if "_" not in key:
        return key
    stripped = key.strip("_")
    if not stripped:
        return key
    lead = key[: len(key) - len(key.lstrip("_"))]
    trail = key[len(key.rstrip("_")):]
    parts = [p for p in stripped.split("_") if p]
    if len(parts) == 1:
        return lead + stripped + trail
    return lead + parts[0].lower() + "".join(_foundation_capitalized(p) for p in parts[1:]) + trail


def _split_dict(inner: str) -> tuple[str, str] | None:
    depth = 0
    for i, ch in enumerate(inner):
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch == ":" and depth == 0:
            return inner[:i].strip(), inner[i + 1:].strip()
    return None


PRIMITIVE_KIND = {
    **{n: "string" for n in ("String", "Substring", "URL", "UUID", "Data")},
    **{n: "int" for n in ("Int", "Int8", "Int16", "Int32", "Int64", "UInt", "UInt8", "UInt16", "UInt32", "UInt64")},
    **{n: "double" for n in ("Double", "Float", "CGFloat", "Decimal", "TimeInterval")},
    "Bool": "bool",
    "Date": "date",
}


def _py_kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return "dict"


def _snippet(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


class Checker:
    def __init__(self, manifest: dict):
        self.types = manifest["types"]
        self.dates = manifest["decoder"]["dates"]
        # Что реально встретилось в ответах: тип целиком и пары (тип, ключ) с непустым значением.
        self.seen_types: set[str] = set()
        self.seen_fields: set[tuple[str, str]] = set()

    # Возвращает [(путь, проблема, пример)] — путь по Swift-ключам, без индексов массивов.
    def check(self, value: Any, swift_type: str, path: str = "$") -> list[tuple[str, str, str]]:
        problems: list[tuple[str, str, str]] = []
        self._value(value, swift_type, path, problems)
        return problems

    def _value(self, value, t: str, path: str, out: list, *, lenient: bool = False) -> None:
        if t.endswith("?"):
            if value is None:
                return
            t = t[:-1]
        if value is None:
            out.append((path, "null", "null"))
            return
        if t in ("Any", "AnyCodable", "JSONValue"):
            return
        if t.startswith("["):
            kv = _split_dict(t[1:-1])
            if kv:
                if not isinstance(value, dict):
                    out.append((path, f"тип: ждали объект-словарь ({t}), пришёл {_py_kind(value)}", _snippet(value)))
                    return
                for v in value.values():
                    self._value(v, kv[1], f"{path}{{}}", out)
            else:
                if not isinstance(value, list):
                    out.append((path, f"тип: ждали массив ({t}), пришёл {_py_kind(value)}", _snippet(value)))
                    return
                for item in value:
                    self._value(item, t[1:-1], f"{path}[]", out)
            return
        if t in PRIMITIVE_KIND:
            self._primitive(value, PRIMITIVE_KIND[t], t, path, out)
            return
        tdef = self.types[t]
        kind = tdef.get("kind")
        if kind == "lenient":
            return
        if kind == "scalar":
            if _py_kind(value) not in tdef["accepts"]:
                out.append((path, f"тип: {t} принимает {tdef['accepts']}, пришёл {_py_kind(value)}", _snippet(value)))
            return
        if not isinstance(value, dict):
            out.append((path, f"тип: ждали объект ({t}), пришёл {_py_kind(value)}", _snippet(value)))
            return
        self._object(value, t, path, out)

    def _object(self, obj: dict, t: str, path: str, out: list) -> None:
        tdef = self.types[t]
        self.seen_types.add(t)
        for flat in tdef.get("flatten", []):
            self._object(obj, flat, path, out)
        by_swift = {}
        for k in obj:
            by_swift.setdefault(swift_key(k), k)
        for key, field in tdef["fields"].items():
            fpath = f"{path}.{key}"
            if key not in by_swift:
                if not field["optional"]:
                    out.append((fpath, "missing", f"нет ключа (сервер прислал {sorted(obj)[:8]})"))
                continue
            value = obj[by_swift[key]]
            if value is not None:
                self.seen_fields.add((t, key))
            if value is None:
                if not field["optional"]:
                    out.append((fpath, "null", f"{by_swift[key]}: null"))
                continue
            sub: list = []
            self._value(value, field["type"], fpath, sub)
            if field.get("lenient"):
                continue  # `try?` в init(from:) глотает любую ошибку этого поля
            out.extend(sub)

    def _primitive(self, value, kind: str, t: str, path: str, out: list) -> None:
        py = _py_kind(value)
        if kind == "string":
            ok = py == "str"
        elif kind == "int":
            ok = py == "int"
        elif kind == "double":
            ok = py in ("int", "double")
        elif kind == "bool":
            ok = py == "bool"
        else:  # date: без dateDecodingStrategy декодер ждёт число, иначе — строку
            ok = py in ("int", "double") if self.dates == "deferredToDate" else py == "str"
        if not ok:
            out.append((path, f"тип: ждали {t}, пришёл {py}", _snippet(value)))


# --- таблица «ручка → корневой тип» ----------------------------------------


def _resolve_endpoints() -> tuple[dict[str, list[dict]], list[str]]:
    """route сервера → эндпоинты клиента, которые его декодируют; и список
    клиентских путей, которым маршрута на сервере нет."""
    by_route: dict[str, list[dict]] = {}
    unrouted: list[str] = []
    for ep in MANIFEST["endpoints"]:
        probe = ep["path"].replace("{}", "1")
        route = _route_for(ep["method"], probe)
        if route is None:
            unrouted.append(f"{ep['method']} {ep['path']} ({ep['swift']})")
            continue
        by_route.setdefault(route, []).append(ep)
    return by_route, unrouted


# --- неудобные состояния ---------------------------------------------------


async def _login(fresh_db, lang: str, telegram_id: int) -> _Walker:
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"u{telegram_id}")
    await fresh_db.set_user_lang(telegram_id, lang)
    client = _client()
    w = _Walker(client, lang)
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    resp = await w.call("POST", "/auth/link", json={"code": code}, expect=200)
    client.headers["Authorization"] = f"Bearer {resp.json()['token']}"
    return w


async def _read_everything(w: _Walker, *, exercise_id: int | None, workout_id: int | None, today: str) -> None:
    """Все чтения, которые клиент делает на главных экранах, — на текущих данных.
    `today` не используется для запросов, но оставлен в подписи: состояния
    называют день явно, а не угадывают его по часам."""
    for path in (
        "/me", "/settings", "/profile", "/muscle-groups", "/exercises", "/exercises?archived=true",
        "/workouts", "/workouts/active", "/workouts/backfill", "/workouts/visits",
        "/workouts/calendar?year=2026&month=1", "/workouts/calendar?year=2026&month=9",
        "/workouts/search?exercise=a", "/dashboard", "/hall-of-fame", "/hall-of-fame/rank-ladder",
        "/weekly-summary", "/achievements", "/achievements/nearest", "/achievements/stats",
        "/bodyweight", "/bodyweight?limit=1", "/programs", "/programs/catalog", "/routines",
        "/share/mine", "/ai/limits", "/ai/history", "/ai/conversations", "/ai/pending",
        "/ai/thinking?q=hello", "/import/batches", "/support/messages", "/exercise-templates?query=a",
        "/exercises/next-suggestions",
    ):
        await w.call("GET", path, expect=(200, 404))
    # Текущая неделя (без `week` сервер отдаёт прошлую): итоги с рекордами и «следующим днём».
    import db as _db
    import timeutil
    import weekly_summary

    me = (await w.call("GET", "/me", expect=200)).json()
    user = await _db.get_user(me["user_id"])
    monday = weekly_summary.week_monday(timeutil.user_today(user))
    await w.call("GET", f"/weekly-summary?week={monday.isoformat()}", expect=200)
    if exercise_id is not None:
        for path in (
            f"/exercises/{exercise_id}/progress", f"/exercises/{exercise_id}/progress/sessions",
            f"/exercises/{exercise_id}/media", f"/exercises/{exercise_id}/alternatives",
            f"/exercises/{exercise_id}/description",
        ):
            await w.call("GET", path, expect=(200, 404))
    if workout_id is not None:
        await w.call("GET", f"/workouts/{workout_id}", expect=200)
        if exercise_id is not None:
            await w.call("GET", f"/exercises/{exercise_id}/superset-partners?workout_id={workout_id}", expect=200)


async def _fork(w: _Walker, query: str) -> int:
    """Форкает шаблон каталога: с точным именем, если он есть в выдаче, иначе первый."""
    found = (await w.call("GET", f"/exercise-templates?query={query}", expect=200)).json()
    assert found, f"каталог без шаблона по запросу {query!r}"
    pick = next((t for t in found if t["name"].lower() == query.lower()), found[0])
    return (await w.call("POST", f"/exercise-templates/{pick['id']}/add", expect=(200, 201))).json()["id"]


async def _train(w: _Walker, exercise_id: int, sets: list[tuple[float, int]], *, date: str | None) -> int:
    """Одна тренировка с подходами; `date` — на какой день её переставить."""
    workout = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()
    wid = workout["id"]
    await w.call("GET", f"/workouts/{wid}/exercises/{exercise_id}/next-target", expect=200)
    for weight, reps in sets:
        await w.call("POST", f"/workouts/{wid}/sets", json={"exercise_id": exercise_id, "weight": weight, "reps": reps},
                     expect=201)
    await w.call("GET", "/workouts/active", expect=200)
    await w.call("GET", f"/workouts/{wid}/exercises/{exercise_id}/next-target", expect=200)
    await w.call("GET", f"/exercises/{exercise_id}/progress", expect=200)
    await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
    if date:
        await w.call("PATCH", f"/workouts/{wid}/date", json={"date": date}, expect=200)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    return wid


async def _state_empty(fresh_db, lang: str) -> list[_Walker]:
    """Новичок: ни тренировок, ни веса тела. Всё, что приложение открывает с порога."""
    w = await _login(fresh_db, lang, 201)
    own = (await w.call("POST", "/exercises", json={"name": "Cable crunch"}, expect=201)).json()["id"]
    await _read_everything(w, exercise_id=own, workout_id=None, today="2026-10-05")
    # Активная тренировка без единого подхода — и её итоги.
    wid = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
    await w.call("GET", f"/workouts/{wid}/exercises/{own}/next-target", expect=200)
    # Пустую тренировку сервер при завершении выбрасывает (`discarded`), читать её уже нечем.
    finished = (await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)).json()
    assert finished.get("discarded") is True
    await _read_everything(w, exercise_id=own, workout_id=None, today="2026-10-05")
    return [w]


async def _state_zero_weight(fresh_db, lang: str) -> list[_Walker]:
    """Подход 0×5 на своём упражнении: лёгкая тренировка без тоннажа (сюда же
    приходил `tonnage_equivalent: null`)."""
    w = await _login(fresh_db, lang, 202)
    own = (await w.call("POST", "/exercises", json={"name": "Cable crunch"}, expect=201)).json()["id"]
    await _train(w, own, [(0, 5)], date="2026-01-05")
    await _train(w, own, [(0, 5), (0, 5)], date=None)
    await _read_everything(w, exercise_id=own, workout_id=None, today="2026-10-05")
    return [w]


async def _state_bodyweight(fresh_db, lang: str) -> list[_Walker]:
    """Упражнение с весом тела (подтягивания): без веса тела атлета и с ним."""
    w = await _login(fresh_db, lang, 203)
    pull = await _fork(w, "pull up")
    await _train(w, pull, [(0, 8), (0, 6)], date="2026-01-05")
    await _read_everything(w, exercise_id=pull, workout_id=None, today="2026-10-05")
    await w.call("POST", "/bodyweight", json={"weight": 80}, expect=201)
    await _train(w, pull, [(0, 9), (5, 5)], date=None)
    await _read_everything(w, exercise_id=pull, workout_id=None, today="2026-10-05")
    return [w]


async def _state_lb(fresh_db, lang: str) -> list[_Walker]:
    """Аккаунт в фунтах: числа и подписи с сервера идут в lb."""
    w = await _login(fresh_db, lang, 204)
    await w.call("PATCH", "/settings", json={"unit": "lb"}, expect=200)
    bench = await _fork(w, "bench")
    await w.call("POST", "/bodyweight", json={"weight": 176.5}, expect=201)
    await _train(w, bench, [(135, 5), (225, 3)], date="2026-01-05")
    await _train(w, bench, [(225, 5)], date=None)
    await _read_everything(w, exercise_id=bench, workout_id=None, today="2026-10-05")
    return [w]


async def _state_program_week(fresh_db, lang: str) -> list[_Walker]:
    """Программа из каталога, тренировка по её дню, прогрессия у упражнения дня,
    архивное упражнение, которое программа достаёт обратно, и итоги текущей недели."""
    w = await _login(fresh_db, lang, 205)
    catalog = (await w.call("GET", "/programs/catalog", expect=200)).json()
    squat = await _fork(w, "Barbell Squat")
    await w.call("POST", f"/exercises/{squat}/archive", expect=200)
    program = (await w.call("POST", f"/programs/catalog/{catalog[0]['key']}", json={}, expect=201)).json()
    await w.call("POST", f"/exercises/{squat}/unarchive", expect=200)
    pid = program["id"]
    await w.call("PATCH", f"/programs/{pid}", json={"deload_every_weeks": 4}, expect=200)
    day = (await w.call("GET", f"/programs/{pid}/next-day", expect=200)).json()
    routine = (await w.call("GET", f"/routines/{day['id']}", expect=200)).json()
    item = routine["exercises"][0]
    await w.call("PATCH", f"/routine-exercises/{item['id']}",
                 json={"progression": {"rule": "double", "step": 2.5, "reps_top": 12}}, expect=200)
    await w.call("PATCH", f"/routine-exercises/{item['id']}", json={"target": "3×8"}, expect=200)
    await w.call("GET", f"/routines/{day['id']}", expect=200)
    await w.call("GET", f"/programs/{pid}", expect=200)
    exercise_id = item["exercise_id"]
    # Две тренировки по дню: на день раньше и сегодня, вторая тяжелее — рекорд недели.
    for weight, date in ((60, "2026-01-05"), (70, None)):
        wid = (await w.call("POST", "/workouts/active", json={"routine_id": day["id"]}, expect=(200, 201))).json()["id"]
        await w.call("GET", f"/workouts/{wid}/exercises/{exercise_id}/next-target", expect=200)
        for _ in range(3):
            await w.call("POST", f"/workouts/{wid}/sets",
                         json={"exercise_id": exercise_id, "weight": weight, "reps": 8, "rpe": 8.5}, expect=201)
        await w.call("GET", f"/exercises/{exercise_id}/superset-partners?workout_id={wid}", expect=200)
        await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
        if date:
            await w.call("PATCH", f"/workouts/{wid}/date", json={"date": date}, expect=200)
        await w.call("GET", f"/workouts/{wid}", expect=200)
        await w.call("POST", f"/workouts/{wid}/routines", json={"name": f"Copy {weight}"}, expect=201)
    await w.call("GET", "/exercises/next-suggestions?last_finished_id=%d" % exercise_id, expect=200)
    await _read_everything(w, exercise_id=exercise_id, workout_id=None, today="2026-10-05")
    await w.call("GET", f"/exercises/{exercise_id}/progress/sessions", expect=200)
    return [w]


async def _state_share(fresh_db, lang: str) -> list[_Walker]:
    """Один атлет делится программой, второй её забирает; владелец читает свои ссылки."""
    a = await _login(fresh_db, lang, 206)
    b = await _login(fresh_db, lang, 207)
    catalog = (await a.call("GET", "/programs/catalog", expect=200)).json()
    program = (await a.call("POST", f"/programs/catalog/{catalog[0]['key']}", json={}, expect=201)).json()
    card = (await a.call("POST", f"/share/programs/{program['id']}", expect=201)).json()
    await b.call("GET", f"/share/{card['token']}", expect=200)
    await b.call("POST", f"/share/{card['token']}/import", expect=(200, 201))
    own = (await a.call("POST", "/exercises", json={"name": "Cable crunch"}, expect=201)).json()["id"]
    ex_card = (await a.call("POST", f"/share/exercises/{own}", expect=201)).json()
    await b.call("GET", f"/share/{ex_card['token']}", expect=200)
    await b.call("POST", f"/share/{ex_card['token']}/import", expect=(200, 201))
    await a.call("GET", "/share/mine", expect=200)
    await a.call("DELETE", f"/share/{card['token']}", expect=200)
    return [a, b]


async def _state_app_only(fresh_db, lang: str, monkeypatch) -> list[_Walker]:
    """Аккаунт, заведённый Sign in with Apple прямо в приложении: без Telegram,
    без истории, с демо-входом App Review."""
    anon = _Walker(_client(), lang)
    apple = (await anon.call("POST", "/auth/apple", json={"identity_token": "contract", "lang": lang},
                             expect=200)).json()
    w = _Walker(_client(), lang)
    w.client.headers["Authorization"] = f"Bearer {apple['token']}"
    await w.call("GET", "/me", expect=200)
    await w.call("POST", "/account/telegram-link-code", expect=200)
    await _read_everything(w, exercise_id=None, workout_id=None, today="2026-10-05")
    monkeypatch.setattr(config, "REVIEW_DEMO_USERNAME", "appreview")
    monkeypatch.setattr(config, "REVIEW_DEMO_PASSWORD", "correct-horse")
    await anon.call("POST", "/auth/password",
                    json={"username": "appreview", "password": "correct-horse", "lang": lang}, expect=200)
    return [w, anon]


async def _state_support_admin(fresh_db, lang: str, monkeypatch) -> list[_Walker]:
    """Владелец поддержки: ветки всех атлетов и ответ в одну из них."""
    athlete = await _login(fresh_db, lang, 208)
    await athlete.call("POST", "/support/messages", json={"text": "Hello"}, expect=201)
    monkeypatch.setattr(config, "ADMIN_ID", 209)
    admin = await _login(fresh_db, lang, 209)
    await admin.call("GET", "/me", expect=200)
    await admin.call("GET", "/support/threads", expect=200)
    await admin.call("GET", "/support/threads/208/messages", expect=200)
    await admin.call("POST", "/support/threads/208/messages", json={"text": "Hi"}, expect=201)
    await admin.call("POST", "/support/threads/208/read", expect=200)
    await athlete.call("GET", "/support/messages", expect=200)
    return [athlete, admin]


async def _state_video(fresh_db, lang: str, monkeypatch) -> list[_Walker]:
    """Разбор техники по видео: ответ — тот же `AIAnswer`, что у `/ai/ask`. Платный разбор
    и ffmpeg подменены: контракту важна форма ответа, а не качество разбора."""
    import chat_attachments
    import video_analysis

    async def fake_analyze(raw, user_id, **kwargs):
        return object()

    monkeypatch.setattr(config, "video_analysis_available", lambda: True)
    monkeypatch.setattr(video_analysis, "analyze", fake_analyze)
    monkeypatch.setattr(video_analysis, "to_context_block", lambda analysis: "Technique looks fine.")
    monkeypatch.setattr(chat_attachments, "save_video_frame", lambda user_id, raw: None)
    w = await _login(fresh_db, lang, 211)
    await w.call("POST", "/ai/video", json={"video_data_url": "data:video/mp4;base64,AAAA"}, expect=200)
    return [w]


async def _state_offline_replay(fresh_db, lang: str) -> list[_Walker]:
    """Офлайн-очередь приложения: тренировка с `client_id`/`started_at` с телефона и повтор
    подхода с тем же `idempotency_key` — ответы на повтор декодируются теми же моделями."""
    import datetime as dt

    w = await _login(fresh_db, lang, 214)
    own = (await w.call("POST", "/exercises", json={"name": "Cable crunch"}, expect=201)).json()["id"]
    started = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"client_id": "0b9f6c1e-6f57-4a3c-9d0e-6a1f2b3c4d5f", "started_at": started}
    wid = (await w.call("POST", "/workouts/active", json=body, expect=(200, 201))).json()["id"]
    await w.call("POST", "/workouts/active", json=body, expect=(200, 201))  # повтор той же заявки
    sent = {"exercise_id": own, "weight": 0, "reps": 5, "idempotency_key": "0b9f6c1e-6f57-4a3c-9d0e-6a1f2b3c4d60"}
    await w.call("POST", f"/workouts/{wid}/sets", json=sent, expect=(200, 201))
    await w.call("POST", f"/workouts/{wid}/sets", json=sent, expect=(200, 201))  # повтор из очереди
    await w.call("POST", f"/workouts/{wid}/sets/parse",
                 json={"exercise_id": own, "text": "20x10, 22.5x8", "idempotency_key": "abc-1"}, expect=201)
    await w.call("GET", f"/workouts/{wid}", expect=200)
    await w.call("POST", f"/workouts/{wid}/finish", json={"started_at": started}, expect=200)
    await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)  # повтор finish
    return [w]


async def _state_long_history(fresh_db, lang: str) -> list[_Walker]:
    """Два месяца истории с чередованием упражнений (суперсет) и ростом весов: подсказки
    «что дальше», напарники по суперсету, плитки роста на главной и «лучший подъём» недели."""
    w = await _login(fresh_db, lang, 213)
    a = (await w.call("POST", "/exercises", json={"name": "Cable row"}, expect=201)).json()["id"]
    b = (await w.call("POST", "/exercises", json={"name": "Face pull"}, expect=201)).json()["id"]
    for date, base in (("2026-08-03", 40), ("2026-08-24", 45), ("2026-09-14", 50), (None, 60)):
        wid = (await w.call("POST", "/workouts/active", json={}, expect=(200, 201))).json()["id"]
        for k in range(3):
            for ex, weight in ((a, base), (b, base / 2)):
                await w.call("POST", f"/workouts/{wid}/sets",
                             json={"exercise_id": ex, "weight": weight, "reps": 10 - k}, expect=201)
        await w.call("GET", f"/exercises/{a}/superset-partners?workout_id={wid}", expect=200)
        await w.call("POST", f"/workouts/{wid}/finish", json={}, expect=200)
        if date:
            await w.call("PATCH", f"/workouts/{wid}/date", json={"date": date}, expect=200)
    await w.call("GET", f"/exercises/next-suggestions?last_finished_id={a}", expect=200)
    await w.call("GET", f"/exercises/next-suggestions?last_finished_id={b}&done_ids={a}", expect=200)
    await _read_everything(w, exercise_id=a, workout_id=None, today="2026-10-05")
    await w.call("GET", f"/exercises/{a}/progress/sessions", expect=200)
    return [w]


async def _state_import(fresh_db, lang: str, monkeypatch) -> list[_Walker]:
    """Импорт Hevy-подобного файла с разминкой, неизвестным упражнением, подозрительным
    весом и двусмысленной датой: пропуски, предупреждения, значки и откат пачки."""
    import ai_trainer

    # Без модели имена сопоставляются без сетевого вызова (иначе импорт ходит к настоящему xAI).
    with monkeypatch.context() as m:
        m.setattr(ai_trainer, "is_configured", lambda: False)
        return await _import_flow(fresh_db, lang)


async def _import_flow(fresh_db, lang: str) -> list[_Walker]:
    w = await _login(fresh_db, lang, 212)
    await _fork(w, "Barbell Bench Press")
    hevy = "title,start_time,end_time,exercise_title,set_index,set_type,weight_kg,reps\n" + "".join(
        f'Push,"{day} Mar 2026, 08:00","{day} Mar 2026, 09:00",{name},{i},{kind},{weight},{reps}\n'
        for day in (3, 5, 9)
        for i, (name, kind, weight, reps) in enumerate((
            ("Barbell Bench Press", "warmup", 20, 10), ("Barbell Bench Press", "normal", 100, 5),
            ("Barbell Bench Press", "normal", 320, 3), ("Zottman Curl Machine X", "normal", 15, 12),
        ))
    )
    preview = (await w.call("POST", "/import/csv/preview", json={"csv": hevy, "file_unit": "kg"}, expect=200)).json()
    assert preview["skipped"], "разминка обязана попасть в пропуски"
    ambiguous = "date,exercise,weight,reps\n03/09/2026,Barbell Bench Press,100,5\n04/09/2026,Barbell Bench Press,100,5\n"
    await w.call("POST", "/import/csv/preview", json={"csv": ambiguous}, expect=200)
    await w.call("POST", "/import/csv/preview", json={"csv": hevy, "file_unit": "lb"}, expect=200)
    done = (await w.call("POST", "/import/csv", json={"csv": hevy, "file_unit": "kg"}, expect=(200, 409))).json()
    if "batch_id" not in done:  # неуверенное имя — выбор за человеком; соглашаемся создать
        done = (await w.call("POST", "/import/csv", json={"csv": hevy, "file_unit": "kg",
                                                          "create_missing_exercises": True}, expect=200)).json()
    await w.call("GET", "/import/batches", expect=200)
    await w.call("POST", f"/import/batches/{done['batch_id']}/undo", expect=200)
    return [w]


async def _state_athlete_profile(fresh_db, lang: str) -> list[_Walker]:
    """Тренер уже знает атлета: заполненный профиль."""
    w = await _login(fresh_db, lang, 210)
    await fresh_db.update_user(
        210, experience="intermediate", goal="strength", equipment=json.dumps(["barbell", "dumbbells"]),
        limitations="left shoulder",
    )
    await w.call("GET", "/profile", expect=200)
    await w.call("GET", "/me", expect=200)
    await w.call("DELETE", "/profile", expect=200)
    return [w]


EDGE_STATES = {
    "empty_history": _state_empty,
    "zero_weight_set": _state_zero_weight,
    "bodyweight_exercise": _state_bodyweight,
    "lb_account": _state_lb,
    "program_week": _state_program_week,
    "share": _state_share,
    "app_only_account": _state_app_only,
    "support_admin": _state_support_admin,
    "athlete_profile": _state_athlete_profile,
    "coach_video": _state_video,
    "import_file": _state_import,
    "long_history": _state_long_history,
    "offline_replay": _state_offline_replay,
}


# --- сверка ----------------------------------------------------------------


def _verify(
    responses: list[dict], by_route: dict[str, list[dict]], checker: Checker | None = None
) -> tuple[dict[str, dict], set[str]]:
    """({id расхождения: детали}, маршруты, у которых был 2xx-ответ)."""
    checker = checker or Checker(MANIFEST)
    found: dict[str, dict] = {}
    ok_routes: set[str] = set()
    for r in responses:
        route, status, body = r["route"], r["status"], r["body"]
        if status >= 400:
            # Тело ошибки приложение читает как `APIErrorBody {error: String, message: String}`.
            problems = []
            if not isinstance(body, dict) or not isinstance(body.get("error"), str):
                problems.append(("$.error", "missing", _snippet(body)))
            if not isinstance(body, dict) or not isinstance(body.get("message"), str):
                problems.append(("$.message", "missing", _snippet(body)))
            for path, problem, example in problems:
                mid = f"{route} [{status}] :: {path} :: {problem}"
                found.setdefault(mid, {"route": route, "path": path, "problem": problem, "example": example,
                                       "request": r["path"], "swift": "APIErrorBody"})
            continue
        ok_routes.add(route)
        for ep in by_route.get(route, ()):
            if ep["nullable"] and body is None:
                continue
            for path, problem, example in checker.check(body, ep["root"] + ("?" if ep["nullable"] else "")):
                mid = f"{route} :: {path} :: {problem}"
                found.setdefault(mid, {"route": route, "path": path, "problem": problem, "example": example,
                                       "request": r["path"], "swift": f"{ep['swift']} -> {ep['root']}"})
    return found, ok_routes


async def _collect(fresh_db, monkeypatch, tmp_path) -> list[dict]:
    """Ответы главного сценария и всех «неудобных» состояний (оба языка не нужны:
    форма ответа от языка не зависит — язык держит соседний инвариант)."""
    responses: list[dict] = []
    walker = await _scenario(fresh_db, monkeypatch, tmp_path, "en")
    responses += walker.responses
    # `_scenario` стёр аккаунт; состояния заводят своих атлетов с новыми id.
    for state in EDGE_STATES.values():
        needs_patch = state in (_state_app_only, _state_support_admin, _state_video, _state_import)
        walkers = await (state(fresh_db, "en", monkeypatch) if needs_patch else state(fresh_db, "en"))
        for w in walkers:
            responses += w.responses
    return responses




# --- тесты -----------------------------------------------------------------


def test_manifest_is_complete_and_current_format():
    assert MANIFEST["decoder"]["keys"] == "convertFromSnakeCase"
    assert MANIFEST["endpoints"] and MANIFEST["types"]
    for ep in MANIFEST["endpoints"]:
        root = ep["root"].strip("[]?").split(":")[-1]
        assert root in MANIFEST["types"] or root in PRIMITIVE_KIND, f"корневой тип {ep['root']} не описан"


def test_swift_key_matches_foundation():
    # Образцы из приложения: цифра внутри слова — граница для `capitalized`.
    assert swift_key("best_e1rm") == "bestE1Rm"
    assert swift_key("e1rm_formula") == "e1rmFormula"
    assert swift_key("tonnage_equivalent") == "tonnageEquivalent"
    assert swift_key("id") == "id"
    assert swift_key("exerciseId") == "exerciseId"
    assert swift_key("_private_key_") == "_privateKey_"
    assert swift_key("max_bw_reps") == "maxBwReps"


def test_checker_catches_the_original_bug():
    """Тот самый класс бага: null у обязательной String во вложенном объекте.

    Исходный случай был с `tonnageEquivalent`; приложение сделало его
    необязательным (iOS #703), и пример переехал на `tonnage` — всё ещё
    обязательную строку в том же объекте. Проверяется сам `WorkoutRewards`:
    в `FinishResponse` блок итогов читается терпимо (`rewards` — `lenient`)."""
    checker = Checker(MANIFEST)
    rewards = {"sets": 1, "exercises": 1, "tonnage": None, "tonnage_equivalent": None, "new_achievements": []}
    problems = checker.check(rewards, "WorkoutRewards")
    assert [(p[0], p[1]) for p in problems] == [("$.tonnage", "null")]
    # Оба других класса: нет ключа и неверный тип (bool — не Int).
    del rewards["tonnage"]
    rewards["sets"] = True
    kinds = {(p[0], p[1].split(":")[0]) for p in checker.check(rewards, "WorkoutRewards")}
    assert kinds == {("$.tonnage", "missing"), ("$.sets", "тип")}
    # Необязательное поле null и лишний ключ сервера — не расхождение.
    rewards.update(sets=1, tonnage="0 kg", tonnage_equivalent=None, milestone=None, extra=1)
    assert checker.check(rewards, "WorkoutRewards") == []


def test_every_client_path_has_a_server_route():
    _, unrouted = _resolve_endpoints()
    new = [u for u in unrouted if u not in KNOWN_UNROUTED]
    assert not new, "приложение зовёт путь, которого нет среди маршрутов /v1:\n" + "\n".join(new)
    stale = sorted(set(KNOWN_UNROUTED) - set(unrouted))
    assert not stale, f"в KNOWN_UNROUTED лежат пути, которые уже находятся: {stale}"


async def test_v1_responses_decode_with_ios_models(fresh_db, monkeypatch, tmp_path):
    by_route, _ = _resolve_endpoints()
    responses = await _collect(fresh_db, monkeypatch, tmp_path)
    checker = Checker(MANIFEST)
    found, ok_routes = _verify(responses, by_route, checker)

    unknown = sorted(set(found) - set(KNOWN_MISMATCHES))
    details = "\n".join(
        f"  {mid}\n      тип приложения: {found[mid]['swift']}; запрос: {found[mid]['request']}; пример: {found[mid]['example']}"
        for mid in unknown
    )
    assert not unknown, (
        "ответ сервера не декодируется Swift-моделью приложения — либо чини сервер (null→\"\"/0, "
        "ключ, тип), либо обсуди с владельцем и занеси в KNOWN_MISMATCHES с причиной:\n" + details
    )
    stale = sorted(set(KNOWN_MISMATCHES) - set(found))
    assert not stale, f"в KNOWN_MISMATCHES лежат расхождения, которые больше не воспроизводятся: {stale}"

    not_run = sorted(
        f"{route} ({', '.join(e['swift'] for e in eps)})"
        for route, eps in by_route.items()
        if route not in ok_routes and route not in NOT_EXERCISED
    )
    assert not not_run, (
        "клиент зовёт эти ручки, а сценарий не получил от них 2xx — допиши вызов в _scenario или "
        "EDGE_STATES (или занеси в NOT_EXERCISED с причиной):\n" + "\n".join(not_run)
    )
    stale = sorted(k for k in NOT_EXERCISED if k in ok_routes)
    assert not stale, f"в NOT_EXERCISED лежат ручки, которые уже проходят: {stale}"

    structs = {name for name, tdef in MANIFEST["types"].items() if "kind" not in tdef}
    unseen = sorted(structs - checker.seen_types - set(NEVER_SEEN))
    assert not unseen, (
        "эти структуры приложения ни разу не пришли в ответах сценария — их поля не проверены; "
        "дай сценарию данных, чтобы они появились (или занеси в NEVER_SEEN с причиной):\n" + "\n".join(unseen)
    )
    stale = sorted(set(NEVER_SEEN) & checker.seen_types)
    assert not stale, f"в NEVER_SEEN лежат структуры, которые уже приходят: {stale}"
