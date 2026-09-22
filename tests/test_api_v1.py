"""REST `/v1` для iOS-клиента: связка аккаунта, подход, история, вес тела.

Гоняется через httpx поверх ASGI-приложения без сокета — тот же путь запроса,
что увидит настоящий клиент, включая проверку Bearer-токена.
"""

import httpx
import pytest

import api_v1


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username="tester")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.mark.asyncio
async def test_auth_link_rejects_unknown_code(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": "nope"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_code"


@pytest.mark.asyncio
async def test_auth_link_issues_token_and_consumes_code(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)
    client = client_factory()
    resp = await client.post("/auth/link", json={"code": code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == 111
    assert body["unit"] == "kg"

    # a used code doesn't work twice
    resp2 = await client.post("/auth/link", json={"code": code})
    assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_auth_link_rate_limits_repeated_bad_codes(fresh_db, client_factory):
    """6-8 цифр — перебираемо без лимита попыток; после mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP
    неудач подряд дальнейшие попытки должны запираться, а не пробовать код."""
    import mcp_oauth

    client = client_factory()
    for _ in range(mcp_oauth.CONSENT_FAILURE_LIMIT_PER_IP):
        resp = await client.post("/auth/link", json={"code": "00000000"})
        assert resp.status_code == 400

    resp = await client.post("/auth/link", json={"code": "00000000"})
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"


@pytest.mark.asyncio
async def test_auth_apple_without_code_creates_app_only_account(fresh_db, client_factory, monkeypatch):
    """Apple ID, о котором сервер ещё не знает, и без link_code — не 404, а
    новый аккаунт без Telegram: App Review не пропускает приложения, которые
    нельзя завести без стороннего мессенджера. Второй вход тем же Apple ID
    должен попасть в тот же аккаунт, а не завести второй."""
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id="apple-1", email="a@example.com"),
    )
    client = client_factory()
    resp = await client.post("/auth/apple", json={"identity_token": "whatever"})
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["user_id"]
    assert user_id < 0, "app-only аккаунт обязан получить синтетический отрицательный id"

    user_row = await fresh_db.get_user(user_id)
    assert user_row is not None
    assert user_row["telegram_linked"] == 0

    second = await client.post("/auth/apple", json={"identity_token": "t2"})
    assert second.status_code == 200
    assert second.json()["user_id"] == user_id


@pytest.mark.asyncio
async def test_auth_apple_without_code_distinct_apple_ids_get_distinct_accounts(
    fresh_db, client_factory, monkeypatch
):
    """Два разных Apple ID без кода — два разных app-only аккаунта, не один на двоих."""
    import apple_signin

    ids = iter(["apple-a", "apple-b"])
    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id=next(ids), email=None),
    )
    client = client_factory()
    first = await client.post("/auth/apple", json={"identity_token": "t1"})
    second = await client.post("/auth/apple", json={"identity_token": "t2"})
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["user_id"] != second.json()["user_id"]


@pytest.mark.asyncio
async def test_auth_apple_links_with_code_then_reauths_without_it(fresh_db, client_factory, monkeypatch):
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id="apple-2", email="b@example.com"),
    )
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    code = await fresh_db.issue_oauth_link_code(111, ttl_seconds=600, digits=8)

    client = client_factory()
    first = await client.post("/auth/apple", json={"identity_token": "t1", "link_code": code})
    assert first.status_code == 200, first.text
    assert first.json()["user_id"] == 111

    # a used link_code doesn't matter the second time — the Apple identity is now known
    second = await client.post("/auth/apple", json={"identity_token": "t2"})
    assert second.status_code == 200
    assert second.json()["user_id"] == 111


@pytest.mark.asyncio
async def test_auth_apple_rejects_invalid_token(fresh_db, client_factory, monkeypatch):
    import apple_signin

    def _raise(token):
        raise apple_signin.AppleTokenError("bad signature")

    monkeypatch.setattr(apple_signin, "verify_identity_token", _raise)
    client = client_factory()
    resp = await client.post("/auth/apple", json={"identity_token": "garbage"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_apple_token"


@pytest.mark.asyncio
async def test_me_requires_bearer_token(fresh_db, client_factory):
    client = client_factory()
    resp = await client.get("/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_reports_telegram_linked(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert resp.json()["telegram_linked"] is True


@pytest.mark.asyncio
async def test_request_telegram_link_code_for_app_only_account(fresh_db, client_factory, monkeypatch):
    """Аккаунт без Telegram может попросить код и передать его боту — обратное
    направление к /auth/link (см. handlers/ios_link.cmd_link_app)."""
    import apple_signin

    monkeypatch.setattr(
        apple_signin, "verify_identity_token",
        lambda token: apple_signin.AppleIdentity(apple_user_id="apple-x", email=None),
    )
    client = client_factory()
    signup = await client.post("/auth/apple", json={"identity_token": "t"})
    token = signup.json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.post("/account/telegram-link-code")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["code"], str) and body["code"]

    # this code is the same currency as /auth/link — consume_link_code
    # resolves it back to the app-only account, the bot side just merges
    # afterwards (see tests/test_account_linking.py)
    user_id = signup.json()["user_id"]
    status, code_user_id = await fresh_db.consume_link_code(body["code"])
    assert status == "ok"
    assert code_user_id == user_id


@pytest.mark.asyncio
async def test_request_telegram_link_code_rejects_already_linked_account(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/account/telegram-link-code")
    assert resp.status_code == 409
    assert resp.json()["error"] == "already_linked"


@pytest.mark.asyncio
async def test_me_returns_linked_user(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == 111


@pytest.mark.asyncio
async def test_register_push_token_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/push/register", json={"device_token": "abc"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_register_and_unregister_push_token(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    empty = await client.post("/push/register", json={"device_token": "   "})
    assert empty.status_code == 400

    resp = await client.post("/push/register", json={"device_token": "abc123"})
    assert resp.status_code == 201
    assert resp.json()["registered"] is True

    # re-registering (new device token after reinstall) overwrites, not duplicates
    resp2 = await client.post("/push/register", json={"device_token": "def456"})
    assert resp2.status_code == 201

    unregistered = await client.delete("/push/register")
    assert unregistered.status_code == 200
    assert unregistered.json()["unregistered"] is True


@pytest.mark.asyncio
async def test_linking_ios_does_not_revoke_mcp_token(fresh_db, client_factory):
    await fresh_db.get_or_create_user(telegram_id=111, username="tester")
    mcp_token = await fresh_db.issue_mcp_token(111)
    await _linked_client(fresh_db, client_factory)
    assert await fresh_db.resolve_mcp_token(mcp_token) == 111


@pytest.mark.asyncio
async def test_full_workout_flow(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    exercise_resp = await client.post("/exercises", json={"name": "Жим лёжа"})
    assert exercise_resp.status_code == 201
    exercise_id = exercise_resp.json()["id"]

    assert (await client.get("/workouts/active")).json() is None

    start_resp = await client.post("/workouts/active")
    assert start_resp.status_code == 201
    workout_id = start_resp.json()["id"]

    # idempotent: asking again returns the same active workout
    again = await client.post("/workouts/active")
    assert again.status_code == 200
    assert again.json()["id"] == workout_id

    set_resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5, "rpe": 8},
    )
    assert set_resp.status_code == 201
    assert set_resp.json()["weight"] == 80
    assert set_resp.json()["reps"] == 5

    active = await client.get("/workouts/active")
    assert active.status_code == 200
    blocks = active.json()["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["exercises"][0]["display_name"] == "Жим лёжа"
    assert len(blocks[0]["exercises"][0]["sets"]) == 1

    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={"note": "норм"})
    assert finish_resp.status_code == 200
    assert finish_resp.json()["status"] == "finished"

    # a finished workout no longer accepts new sets
    late_set = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 80, "reps": 5},
    )
    assert late_set.status_code == 409

    history = await client.get("/workouts")
    assert history.status_code == 200
    items = history.json()
    assert len(items) == 1
    assert items[0]["id"] == workout_id
    assert items[0]["exercise_names"] == ["Жим лёжа"]
    assert items[0]["set_count"] == 1

    detail = await client.get(f"/workouts/{workout_id}")
    assert detail.status_code == 200
    assert detail.json()["note"] == "норм"

    edited = await client.patch(f"/workouts/{workout_id}/note", json={"note": "переписал"})
    assert edited.status_code == 200
    assert edited.json()["note"] == "переписал"

    cleared = await client.patch(f"/workouts/{workout_id}/note", json={"note": None})
    assert cleared.status_code == 200
    assert cleared.json()["note"] is None


@pytest.mark.asyncio
async def test_finish_without_note_preserves_note_set_earlier(fresh_db, client_factory):
    """finish без "note" в теле — не "очисти её": iOS зовёт finish после того,
    как заметку уже поставили через PATCH /note, и раньше finish молча стирал
    её в NULL."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    # Подход обязателен: пустую тренировку финиш теперь удаляет, а не
    # сохраняет (как и бот), и заметку на ней проверять не на чем.
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )

    await client.patch(f"/workouts/{workout_id}/note", json={"note": "заметка до финиша"})

    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert finish_resp.status_code == 200
    assert finish_resp.json()["note"] == "заметка до финиша"


@pytest.mark.asyncio
async def test_finish_with_explicit_note_overrides_earlier_one(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )

    await client.patch(f"/workouts/{workout_id}/note", json={"note": "старая"})
    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={"note": "новая"})
    assert finish_resp.status_code == 200
    assert finish_resp.json()["note"] == "новая"


@pytest.mark.asyncio
async def test_finish_and_update_note_responses_include_blocks(fresh_db, client_factory):
    """iOS перезаписывает весь Workout ответом finish/PATCH note — если в нём
    нет "blocks", список упражнений на экране молча пропадает."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )

    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert finish_resp.status_code == 200
    assert len(finish_resp.json()["blocks"]) == 1

    note_resp = await client.patch(f"/workouts/{workout_id}/note", json={"note": "готово"})
    assert note_resp.status_code == 200
    assert len(note_resp.json()["blocks"]) == 1


@pytest.mark.asyncio
async def test_update_note_rejects_another_users_workout(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    workout_id = (await client_a.post("/workouts/active")).json()["id"]

    resp = await client_b.patch(f"/workouts/{workout_id}/note", json={"note": "чужое"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_exercise_note_is_separate_from_workout_note(fresh_db, client_factory):
    """PATCH .../exercises/{id}/note — заметка к упражнению В ЭТОЙ тренировке
    (live:note бота), а не к тренировке целиком (PATCH .../note): у них разные
    ключи хранения, и запись в один не должна трогать другой."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )

    resp = await client.patch(
        f"/workouts/{workout_id}/exercises/{exercise_id}/note",
        json={"note": "болит колено"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"exercise_id": exercise_id, "note": "болит колено"}

    detail = await client.get(f"/workouts/{workout_id}")
    assert detail.status_code == 200
    exercise_json = detail.json()["blocks"][0]["exercises"][0]
    assert exercise_json["note"] == "болит колено"
    # Заметка ко всей тренировке остаётся отдельным полем и осталась пустой.
    assert detail.json()["note"] is None


@pytest.mark.asyncio
async def test_exercise_note_empty_string_clears_it(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Тяга"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.patch(
        f"/workouts/{workout_id}/exercises/{exercise_id}/note", json={"note": "тест"}
    )

    cleared = await client.patch(
        f"/workouts/{workout_id}/exercises/{exercise_id}/note", json={"note": ""}
    )
    assert cleared.status_code == 200
    assert cleared.json()["note"] is None


@pytest.mark.asyncio
async def test_exercise_note_rejects_another_users_workout_or_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    exercise_id = (await client_a.post("/exercises", json={"name": "Жим"})).json()["id"]
    workout_id = (await client_a.post("/workouts/active")).json()["id"]

    # Чужая тренировка.
    resp = await client_b.patch(
        f"/workouts/{workout_id}/exercises/{exercise_id}/note", json={"note": "чужое"}
    )
    assert resp.status_code == 404

    # Своя тренировка, но чужое упражнение.
    other_workout_id = (await client_b.post("/workouts/active")).json()["id"]
    resp = await client_b.patch(
        f"/workouts/{other_workout_id}/exercises/{exercise_id}/note", json={"note": "чужое"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_last_set(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    missing = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_id}/last-set")
    assert missing.status_code == 404

    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 100, "reps": 5}
    )
    second = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 110, "reps": 3}
    )
    assert second.status_code == 201

    deleted = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_id}/last-set")
    assert deleted.status_code == 200
    assert deleted.json()["weight"] == 110

    active = await client.get("/workouts/active")
    sets = active.json()["blocks"][0]["exercises"][0]["sets"]
    assert len(sets) == 1
    assert sets[0]["weight"] == 100


@pytest.mark.asyncio
async def test_delete_last_set_in_superset_targets_right_exercise(fresh_db, client_factory):
    """Суперсет заводит только бот (api_v1 создаёт лишь одиночные блоки), но
    workout может смешивать оба клиента — API обязана уметь его читать.
    delete_last_set не должна сносить последний подход ЧУЖОГО упражнения в
    том же блоке, даже если его залогировали позже."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_a = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    exercise_b = (await client.post("/exercises", json={"name": "Тяга штанги"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]

    block_id = await fresh_db.create_block(workout_id, "superset")
    await fresh_db.add_block_exercise(block_id, exercise_a, 0)
    await fresh_db.add_block_exercise(block_id, exercise_b, 1)

    await client.post(f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_a, "weight": 80, "reps": 5})
    # b logged after a — a naive "last set in block" delete would remove this one
    await client.post(f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_b, "weight": 60, "reps": 8})

    deleted = await client.delete(f"/workouts/{workout_id}/exercises/{exercise_a}/last-set")
    assert deleted.status_code == 200
    assert deleted.json()["exercise_id"] == exercise_a
    assert deleted.json()["weight"] == 80

    active = await client.get("/workouts/active")
    block = active.json()["blocks"][0]
    exercises_by_id = {e["exercise_id"]: e for e in block["exercises"]}
    assert exercises_by_id[exercise_a]["sets"] == []
    assert len(exercises_by_id[exercise_b]["sets"]) == 1


@pytest.mark.asyncio
async def test_discard_active_workout(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    missing = await client.delete("/workouts/active")
    assert missing.status_code == 404

    workout_id = (await client.post("/workouts/active")).json()["id"]
    discarded = await client.delete("/workouts/active")
    assert discarded.status_code == 200
    assert discarded.json()["discarded"] is True

    assert (await client.get("/workouts/active")).json() is None
    # gone for good, not just unlinked from "active"
    history = await client.get("/workouts")
    assert all(item["id"] != workout_id for item in history.json())


@pytest.mark.asyncio
async def test_set_logging_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)

    exercise_resp = await client_a.post("/exercises", json={"name": "Присед"})
    exercise_id = exercise_resp.json()["id"]

    start_resp = await client_b.post("/workouts/active")
    workout_id = start_resp.json()["id"]

    resp = await client_b.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 60, "reps": 10},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_exercises_filters_by_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    legs_id = await fresh_db.create_muscle_group(111, "Ноги", "🦵")
    chest_id = await fresh_db.create_muscle_group(111, "Грудь", "💪")
    await client.post("/exercises", json={"name": "Присед", "group_id": legs_id})
    await client.post("/exercises", json={"name": "Жим лёжа", "group_id": chest_id})
    await client.post("/exercises", json={"name": "Без группы"})

    resp = await client.get(f"/exercises?group_id={legs_id}")
    assert resp.status_code == 200
    names = [e["display_name"] for e in resp.json()]
    assert names == ["Присед"]

    bad = await client.get("/exercises?group_id=not-a-number")
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_create_exercise_rejects_another_users_group(fresh_db, client_factory):
    """group_id угадываемый — без проверки чужая личная группа мышц молча
    подставилась бы в упражнение другого пользователя."""
    await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    own_group_id = await fresh_db.create_muscle_group(111, "Своя группа A")

    resp = await client_b.post("/exercises", json={"name": "Присед", "group_id": own_group_id})
    assert resp.status_code == 404

    unknown = await client_b.post("/exercises", json={"name": "Присед 2", "group_id": 999999})
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_create_exercise_accepts_global_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    global_groups = await fresh_db.list_muscle_groups(None, global_only=True)
    assert global_groups, "ожидается непустой глобальный каталог групп мышц"
    group_id = global_groups[0]["id"]

    resp = await client.post("/exercises", json={"name": "Присед", "group_id": group_id})
    assert resp.status_code == 201
    assert resp.json()["primary_group_id"] == group_id


@pytest.mark.asyncio
async def test_create_muscle_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    resp = await client.post("/muscle-groups", json={"name": "Кор", "emoji": "🔥"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Кор"
    assert body["emoji"] == "🔥"

    groups = await client.get("/muscle-groups")
    assert any(g["id"] == body["id"] for g in groups.json())

    empty_name = await client.post("/muscle-groups", json={"name": "  "})
    assert empty_name.status_code == 400


@pytest.mark.asyncio
async def test_muscle_groups_report_days_since_last_finished_workout(fresh_db, client_factory):
    """Плитки групп на экране старта: сколько дней с прошлой тренировки
    группы. Незаконченная тренировка не считается, нетронутая группа — null."""
    client = await _linked_client(fresh_db, client_factory)
    groups = (await client.get("/muscle-groups")).json()
    assert all(g["days_ago"] is None for g in groups)

    group_id = groups[0]["id"]
    exercise_id = (await client.post("/exercises", json={"name": "Жим", "group_id": group_id})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5})
    in_progress = {g["id"]: g for g in (await client.get("/muscle-groups")).json()}
    assert in_progress[group_id]["days_ago"] is None

    await client.post(f"/workouts/{workout_id}/finish", json={})
    finished = {g["id"]: g for g in (await client.get("/muscle-groups")).json()}
    assert finished[group_id]["days_ago"] == 0
    other = next(g for g in finished.values() if g["id"] != group_id)
    assert other["days_ago"] is None


@pytest.mark.asyncio
async def test_exercise_progress_lists_sets_from_finished_workouts_only(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    # a set logged in the still-active workout shouldn't show up in progress yet
    empty = await client.get(f"/exercises/{exercise_id}/progress")
    assert empty.status_code == 200
    assert empty.json() == []

    await client.post(f"/workouts/{workout_id}/finish", json={})
    progress = await client.get(f"/exercises/{exercise_id}/progress")
    assert progress.status_code == 200
    entries = progress.json()
    assert len(entries) == 1
    assert entries[0]["workout_id"] == workout_id
    assert entries[0]["weight"] == 80
    assert entries[0]["reps"] == 5


@pytest.mark.asyncio
async def test_exercise_progress_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    exercise_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client_b.get(f"/exercises/{exercise_id}/progress")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_bodyweight_crud(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)

    add_resp = await client.post("/bodyweight", json={"weight": 82.5})
    assert add_resp.status_code == 201
    # iOS decodes this response straight into BodyweightEntry, whose loggedAt
    # is non-optional — a response missing it throws on every single log.
    assert add_resp.json()["logged_at"]
    log_id = add_resp.json()["id"]

    list_resp = await client.get("/bodyweight")
    assert list_resp.status_code == 200
    assert list_resp.json()[0]["weight"] == 82.5

    edit_resp = await client.patch(f"/bodyweight/{log_id}", json={"weight": 83.0})
    assert edit_resp.status_code == 200
    assert edit_resp.json()["weight"] == 83.0
    assert (await client.get("/bodyweight")).json()[0]["weight"] == 83.0

    delete_resp = await client.delete(f"/bodyweight/{log_id}")
    assert delete_resp.status_code == 200
    assert (await client.get("/bodyweight")).json() == []

    missing = await client.delete(f"/bodyweight/{log_id}")
    assert missing.status_code == 404

    missing_edit = await client.patch(f"/bodyweight/{log_id}", json={"weight": 90})
    assert missing_edit.status_code == 404


@pytest.mark.asyncio
async def test_update_bodyweight_rejects_another_users_entry(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=111)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=222)
    log_id = (await client_a.post("/bodyweight", json={"weight": 80})).json()["id"]

    resp = await client_b.patch(f"/bodyweight/{log_id}", json={"weight": 999})
    assert resp.status_code == 404


# --- подход строкой (POST /workouts/{id}/sets/parse) ------------------------
#
# Главный способ записи в боте. Тесты проверяют не парсер (он свой набор имеет),
# а что REST даёт ровно то же поведение: несколько подходов одной строкой, вес
# с прошлого подхода на голых повторах, и человеческий текст ошибки разбора.


async def _active_workout_with_exercise(fresh_db, client, name="Жим лёжа"):
    resp = await client.post("/workouts/active")
    workout_id = resp.json()["id"]
    resp = await client.post("/exercises", json={"name": name})
    return workout_id, resp.json()["id"]


@pytest.mark.asyncio
async def test_parse_line_logs_several_sets_at_once(fresh_db, client_factory):
    """«100 8, 100 7, 95 8» — три подхода одним вводом. Ради этого строка и
    нужна: тремя числовыми полями это три захода с клавиатурой."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(fresh_db, client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "100 8, 100 7, 95 8"},
    )
    assert resp.status_code == 201, resp.text
    sets = resp.json()["sets"]
    assert [(s["weight"], s["reps"]) for s in sets] == [(100.0, 8), (100.0, 7), (95.0, 8)]


@pytest.mark.asyncio
async def test_parse_line_repeats_weight_for_bare_reps(fresh_db, client_factory):
    """«8» после «100 8» — это 100×8, а не 0×8. Вес подставляет сервер: клиент
    не должен знать, какой подход считается предыдущим."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(fresh_db, client)

    await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "100 8"},
    )
    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "7"},
    )
    assert resp.status_code == 201, resp.text
    (logged,) = resp.json()["sets"]
    assert (logged["weight"], logged["reps"]) == (100.0, 7)


@pytest.mark.asyncio
async def test_parse_line_understands_counts_and_rpe(fresh_db, client_factory):
    """«100x8x3» — три одинаковых подхода, «@9» — RPE суффиксом, без отдельного поля."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(fresh_db, client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "100x8x3 @9"},
    )
    assert resp.status_code == 201, resp.text
    sets = resp.json()["sets"]
    assert len(sets) == 3
    assert all(s["rpe"] == 9 and s["weight"] == 100.0 and s["reps"] == 8 for s in sets)


@pytest.mark.asyncio
async def test_parse_line_returns_human_message_on_bad_input(fresh_db, client_factory):
    """Единственное место в /v1, где текст ошибки человеческий: ParseError.message
    уже написан голосом тренера, и клиент показывает его дословно."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id, exercise_id = await _active_workout_with_exercise(fresh_db, client)

    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "как-то так"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "unparsed_input"
    assert body["message"].strip()


@pytest.mark.asyncio
async def test_parse_line_rejects_someone_elses_exercise(fresh_db, client_factory):
    """exercise_id угадывается — чужое упражнение писать в свою тренировку нельзя."""
    owner = await _linked_client(fresh_db, client_factory, telegram_id=555)
    stranger = await _linked_client(fresh_db, client_factory, telegram_id=666)
    resp = await stranger.post("/exercises", json={"name": "Чужое упражнение"})
    stranger_exercise_id = resp.json()["id"]

    resp = await owner.post("/workouts/active")
    workout_id = resp.json()["id"]
    resp = await owner.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": stranger_exercise_id, "text": "100 8"},
    )
    assert resp.status_code == 404


# ---------- тренировка задним числом ----------


@pytest.mark.asyncio
async def test_backfill_is_not_the_active_workout(fresh_db, client_factory):
    """Занесение за прошлый день не должно всплывать как «идёт тренировка»:
    у него нет ни таймера, ни сегодняшней даты, и показать его под кнопкой
    «Продолжить» значило бы соврать про то, что происходит прямо сейчас."""
    client = await _linked_client(fresh_db, client_factory)
    created = await client.post("/workouts/backfill", json={"date": "2026-09-10"})
    assert created.status_code == 201, created.text
    assert created.json()["started_at"].startswith("2026-09-10")

    assert (await client.get("/workouts/active")).json() is None
    assert (await client.get("/workouts/backfill")).json()["id"] == created.json()["id"]


@pytest.mark.asyncio
async def test_backfill_twice_returns_the_same_one(fresh_db, client_factory):
    """Второй POST не заводит вторую форму: две открытые за разные дни человек
    не различит, а брошенная осталась бы в базе навсегда — напомнить о ней
    нечему."""
    client = await _linked_client(fresh_db, client_factory)
    first = await client.post("/workouts/backfill", json={"date": "2026-09-10"})
    second = await client.post("/workouts/backfill", json={"date": "2026-09-11"})
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["started_at"].startswith("2026-09-10")


@pytest.mark.asyncio
async def test_backfill_rejects_a_future_date(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/workouts/backfill", json={"date": "2099-01-01"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_backfill_accepts_sets_and_finishes_on_its_own_date(fresh_db, client_factory):
    """Главное здесь — finished_at. Если бы он брался с часов сервера,
    тренировка за прошлую неделю «закончилась» бы сегодня и растянулась
    в истории на неделю."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/backfill", json={"date": "2026-09-10"})).json()["id"]

    logged = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert logged.status_code == 201, logged.text

    finished = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert finished.status_code == 200, finished.text
    body = finished.json()
    assert body["status"] == "finished"
    assert body["started_at"].startswith("2026-09-10")
    assert body["finished_at"].startswith("2026-09-10")

    # Форма закрыта — следующее занесение начинается с чистого листа.
    assert (await client.get("/workouts/backfill")).json() is None


@pytest.mark.asyncio
async def test_backfill_accepts_a_set_written_as_text(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]
    workout_id = (await client.post("/workouts/backfill", json={"date": "2026-09-10"})).json()["id"]
    resp = await client.post(
        f"/workouts/{workout_id}/sets/parse",
        json={"exercise_id": exercise_id, "text": "100 8, 95 8"},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["sets"]) == 2


@pytest.mark.asyncio
async def test_backfill_can_be_discarded(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/workouts/backfill", json={"date": "2026-09-10"})
    assert (await client.delete("/workouts/backfill")).status_code == 200
    assert (await client.get("/workouts/backfill")).json() is None
    assert (await client.delete("/workouts/backfill")).status_code == 404


@pytest.mark.asyncio
async def test_finished_workout_still_refuses_new_sets(fresh_db, client_factory):
    """Ослабление проверки под занесение задним числом не должно было открыть
    запись в уже законченную тренировку."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    await client.post(f"/workouts/{workout_id}/finish", json={})
    late = await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert late.status_code == 409
    assert late.json()["error"] == "workout_finished"


@pytest.mark.asyncio
async def test_finishing_through_the_api_awards_achievements(fresh_db, client_factory):
    """Значки присваивались только в боте: у человека, который пользуется
    одним приложением, сетка достижений не заполнялась бы никогда."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    assert await fresh_db.list_achievement_codes(111) == set()

    await client.post(f"/workouts/{workout_id}/finish", json={})
    assert await fresh_db.list_achievement_codes(111), "первая тренировка не дала ни одного значка"


@pytest.mark.asyncio
async def test_finishing_an_empty_workout_deletes_it(fresh_db, client_factory):
    """Пустая тренировка не должна сохраняться: строка «Без упражнений · 0
    подходов» в истории не сообщает ничего, кроме того, что человек открыл
    экран и передумал, а портит она и список, и все счётчики тренировок.
    Бот в этом случае тренировку удаляет (workout.empty_deleted)."""
    client = await _linked_client(fresh_db, client_factory)
    workout_id = (await client.post("/workouts/active")).json()["id"]

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"discarded": True, "reason": "empty"}

    assert await fresh_db.get_workout(workout_id) is None
    assert (await client.get("/workouts/active")).json() is None
    assert (await client.get("/workouts")).json() == []


@pytest.mark.asyncio
async def test_finishing_an_empty_backfill_deletes_it_too(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    workout_id = (await client.post("/workouts/backfill", json={"date": "2026-09-10"})).json()["id"]

    resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert resp.status_code == 200
    assert resp.json()["discarded"] is True
    assert (await client.get("/workouts/backfill")).json() is None


@pytest.mark.asyncio
async def test_a_workout_with_sets_still_finishes_normally(fresh_db, client_factory):
    """Проверка, что защита от пустой тренировки не съела обычный путь."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )

    body = (await client.post(f"/workouts/{workout_id}/finish", json={})).json()
    assert body["status"] == "finished"
    assert len(await fresh_db.list_workouts(111)) == 1


# ---------- управление упражнением (PATCH/архив/объединение) ----------

@pytest.mark.asyncio
async def test_update_exercise_renames(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    resp = await client.patch(f"/exercises/{exercise_id}", json={"name": "Жим штанги лёжа"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Жим штанги лёжа"

    ex = await fresh_db.get_exercise(exercise_id)
    assert ex["display_name"] == "Жим штанги лёжа"


@pytest.mark.asyncio
async def test_update_exercise_rename_clash_is_conflict(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    await client.post("/exercises", json={"name": "Присед"})
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]

    resp = await client.patch(f"/exercises/{exercise_id}", json={"name": "Присед"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "name_taken"


@pytest.mark.asyncio
async def test_update_exercise_changes_group(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    group_id = (await fresh_db.create_muscle_group(111, "Ноги"))
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client.patch(f"/exercises/{exercise_id}", json={"group_id": group_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["primary_group_id"] == group_id


@pytest.mark.asyncio
async def test_update_exercise_rejects_another_users_group(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=201)
    await _linked_client(fresh_db, client_factory, telegram_id=202)
    own_group_id = await fresh_db.create_muscle_group(202, "Своя группа")
    exercise_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client_a.patch(f"/exercises/{exercise_id}", json={"group_id": own_group_id})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_exercise_sets_and_clears_description(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client.patch(f"/exercises/{exercise_id}", json={"description": "Спина прямая"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Спина прямая"

    resp2 = await client.patch(f"/exercises/{exercise_id}", json={"description": None})
    assert resp2.status_code == 200
    assert resp2.json()["description"] is None


@pytest.mark.asyncio
async def test_update_exercise_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=301)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=302)
    exercise_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client_b.patch(f"/exercises/{exercise_id}", json={"name": "Чужое"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_exercise_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.patch("/exercises/1", json={"name": "Присед"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_archive_and_unarchive_exercise(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client.post(f"/exercises/{exercise_id}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_archived"] is True

    # архив не должен подмешиваться в обычный список
    listing = await client.get("/exercises")
    assert exercise_id not in [e["id"] for e in listing.json()]

    archived_listing = await client.get("/exercises?archived=true")
    assert [e["id"] for e in archived_listing.json()] == [exercise_id]

    resp2 = await client.post(f"/exercises/{exercise_id}/unarchive")
    assert resp2.status_code == 200
    assert resp2.json()["is_archived"] is False

    listing2 = await client.get("/exercises")
    assert exercise_id in [e["id"] for e in listing2.json()]


@pytest.mark.asyncio
async def test_archive_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=401)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=402)
    exercise_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]

    resp = await client_b.post(f"/exercises/{exercise_id}/archive")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_archive_exercise_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/exercises/1/archive")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_merge_exercises_combines_history_under_target(fresh_db, client_factory):
    """«Жим лёжа» и «жим штанги лёжа» — тот самый случай из задачи: после
    объединения обе истории должны читаться под одним id."""
    client = await _linked_client(fresh_db, client_factory)
    target_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    source_id = (await client.post("/exercises", json={"name": "жим штанги лёжа"})).json()["id"]

    workout1_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout1_id}/sets", json={"exercise_id": target_id, "weight": 80, "reps": 5}
    )
    await client.post(f"/workouts/{workout1_id}/finish", json={})

    workout2_id = (await client.post("/workouts/active")).json()["id"]
    await client.post(
        f"/workouts/{workout2_id}/sets", json={"exercise_id": source_id, "weight": 82.5, "reps": 5}
    )
    await client.post(f"/workouts/{workout2_id}/finish", json={})

    resp = await client.post("/exercises/merge", json={"target_id": target_id, "source_id": source_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == target_id

    progress = await client.get(f"/exercises/{target_id}/progress")
    assert progress.status_code == 200
    weights = sorted(e["weight"] for e in progress.json())
    assert weights == [80, 82.5]

    # упражнение-дубликат больше не существует отдельной строкой
    assert await fresh_db.get_exercise(source_id) is None
    listing = await client.get("/exercises")
    assert [e["id"] for e in listing.json()] == [target_id]


@pytest.mark.asyncio
async def test_merge_exercises_rejects_archived_target(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    target_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    source_id = (await client.post("/exercises", json={"name": "жим штанги лёжа"})).json()["id"]
    await client.post(f"/exercises/{target_id}/archive")

    resp = await client.post("/exercises/merge", json={"target_id": target_id, "source_id": source_id})
    assert resp.status_code == 409
    assert resp.json()["error"] == "target_archived"


@pytest.mark.asyncio
async def test_merge_exercises_rejects_another_users_exercise(fresh_db, client_factory):
    client_a = await _linked_client(fresh_db, client_factory, telegram_id=501)
    client_b = await _linked_client(fresh_db, client_factory, telegram_id=502)
    own_id = (await client_a.post("/exercises", json={"name": "Присед"})).json()["id"]
    stranger_id = (await client_b.post("/exercises", json={"name": "Тяга"})).json()["id"]

    resp = await client_a.post("/exercises/merge", json={"target_id": own_id, "source_id": stranger_id})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_merge_exercises_requires_auth(fresh_db, client_factory):
    client = client_factory()
    resp = await client.post("/exercises/merge", json={"target_id": 1, "source_id": 2})
    assert resp.status_code == 401


# ---------- POST /workouts/active с routine_id ----------
#
# Приложение раньше заводило тренировку без привязки к дню программы — из-за
# этого db.next_program_day навсегда залипал на первом дне (у него нет своего
# курсора, только история workouts.routine_id, см. db.next_program_day).
# Тесты ниже — про починку именно этого; test_next_day_advances_after_a_workout_started_via_routine_id
# главный, ради него всё и делалось.

@pytest.mark.asyncio
async def test_start_workout_with_routine_id_links_it(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    day_id = (await client.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()["id"]

    resp = await client.post("/workouts/active", json={"routine_id": day_id})
    assert resp.status_code == 201, resp.text
    assert resp.json()["routine_id"] == day_id

    stored = await fresh_db.get_workout(resp.json()["id"])
    assert stored["routine_id"] == day_id
    assert stored["program_id"] == program_id


@pytest.mark.asyncio
async def test_start_workout_with_foreign_routine_id_is_404(fresh_db, client_factory):
    owner = await _linked_client(fresh_db, client_factory, telegram_id=111)
    intruder = await _linked_client(fresh_db, client_factory, telegram_id=222)
    program_id = (await owner.post("/programs", json={"name": "PPL"})).json()["id"]
    day_id = (await owner.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()["id"]

    resp = await intruder.post("/workouts/active", json={"routine_id": day_id})
    assert resp.status_code == 404

    # чужой routine_id не должен и завести тренировку "с нуля" по ошибке
    assert (await intruder.get("/workouts/active")).json() is None


@pytest.mark.asyncio
async def test_start_workout_without_routine_id_behaves_as_before(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    resp = await client.post("/workouts/active")
    assert resp.status_code == 201
    assert resp.json()["routine_id"] is None

    stored = await fresh_db.get_workout(resp.json()["id"])
    assert stored["routine_id"] is None
    assert stored["program_id"] is None


@pytest.mark.asyncio
async def test_next_day_advances_after_a_workout_started_via_routine_id(fresh_db, client_factory):
    """Главный тест: тренировка, начатая приложением с routine_id, должна
    продвигать next-day ровно как тренировка, начатая ботом."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    program_id = (await client.post("/programs", json={"name": "PPL"})).json()["id"]
    push_id = (await client.post(f"/programs/{program_id}/days", json={"name": "Push"})).json()["id"]
    pull_id = (await client.post(f"/programs/{program_id}/days", json={"name": "Pull"})).json()["id"]

    # программа ни разу не пройдена — следующий день первый по порядку
    assert (await client.get(f"/programs/{program_id}/next-day")).json()["id"] == push_id

    workout_id = (await client.post("/workouts/active", json={"routine_id": push_id})).json()["id"]
    await client.post(
        f"/workouts/{workout_id}/sets", json={"exercise_id": exercise_id, "weight": 80, "reps": 5}
    )
    finish_resp = await client.post(f"/workouts/{workout_id}/finish", json={})
    assert finish_resp.status_code == 200
    assert finish_resp.json()["status"] == "finished"

    next_day = await client.get(f"/programs/{program_id}/next-day")
    assert next_day.status_code == 200
    assert next_day.json()["id"] == pull_id


@pytest.mark.asyncio
async def test_set_with_non_numeric_weight_is_a_400_not_a_500(fresh_db, client_factory):
    """Вес строкой («100» вместо 100) — обычная ошибка клиента, и ответом на
    неё должно быть внятное 400, а не «internal error»: проверка типа в
    api_v1_common.require срабатывала верно, но падала сама, собирая текст
    ошибки (у кортежа (int, float) нет __name__)."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active", json={})).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": "100", "reps": 8},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"
    assert "weight" in resp.json()["message"]

    # тот же кортеж используется весом тела — и там тоже 400, а не 500
    resp = await client.post("/bodyweight", json={"weight": "80"})
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"


@pytest.mark.asyncio
async def test_set_with_non_numeric_rpe_is_a_400_not_a_500(fresh_db, client_factory):
    """RPE строкой — 400, как и в api_v1_account.add_workout_set: голый
    float("нет") на пути записи подхода ронял запрос пятисоткой."""
    client = await _linked_client(fresh_db, client_factory)
    exercise_id = (await client.post("/exercises", json={"name": "Жим лёжа"})).json()["id"]
    workout_id = (await client.post("/workouts/active", json={})).json()["id"]

    resp = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 8, "rpe": "нет"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "bad_request"

    # число по-прежнему принимается
    ok = await client.post(
        f"/workouts/{workout_id}/sets",
        json={"exercise_id": exercise_id, "weight": 100, "reps": 8, "rpe": 9.5},
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["rpe"] == 9.5


@pytest.mark.asyncio
async def test_list_query_params_reject_garbage_with_400(fresh_db, client_factory):
    """«?limit=abc» — кривая ссылка, а не поломка сервера: списки тренировок и
    веса тела разбирали параметр голым int() и отвечали 500."""
    client = await _linked_client(fresh_db, client_factory)

    for url in ("/workouts?limit=abc", "/workouts?offset=abc", "/bodyweight?limit=abc"):
        resp = await client.get(url)
        assert resp.status_code == 400, (url, resp.text)
        assert resp.json()["error"] == "bad_request"

    # рабочие значения по-прежнему работают, а без параметра — как раньше
    assert (await client.get("/workouts?limit=5&offset=0")).status_code == 200
    assert (await client.get("/bodyweight")).status_code == 200
    assert (await client.get("/bodyweight?limit=5")).status_code == 200
