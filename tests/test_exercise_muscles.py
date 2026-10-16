"""Схема мышц упражнения каталога (exercise_muscles): разметка покрывает весь
каталог и говорит слагами схемы приложения, а `/v1` отдаёт её в `media` —
у превью шаблона и у своей копии, в том числе переименованной."""
import httpx
import pytest

import api_v1
import db
import exercise_muscles
import seed_data

_CATALOG = {name for _group, name in seed_data.EXERCISE_TEMPLATES}


def test_every_catalog_template_is_marked_and_nothing_else():
    assert set(exercise_muscles.MUSCLES) == _CATALOG


def test_marks_use_body_map_slugs_without_overlap():
    for name, (primary, secondary) in exercise_muscles.MUSCLES.items():
        assert primary, f"{name}: нет основных мышц"
        assert set(primary) <= exercise_muscles.MUSCLE_SLUGS, name
        assert set(secondary) <= exercise_muscles.MUSCLE_SLUGS, name
        assert not set(primary) & set(secondary), name
        assert len(primary) == len(set(primary)) and len(secondary) == len(set(secondary)), name


def test_staple_lifts_hit_the_right_muscles():
    def primary(name):
        return set(exercise_muscles.MUSCLES[name][0])

    assert "chest" in primary("Жим штанги лёжа")
    assert "quadriceps" in primary("Присед со штангой")
    assert "upper-back" in primary("Тяга штанги в наклоне")
    assert "hamstring" in primary("Румынская тяга")
    assert primary("Шраги со штангой") == {"trapezius"}
    assert primary("Подъём на носки стоя") == {"calves"}


@pytest.fixture
def client_factory():
    def _make():
        transport = httpx.ASGITransport(app=api_v1.build_app())
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def _linked_client(fresh_db, client_factory, telegram_id=111):
    await fresh_db.get_or_create_user(telegram_id=telegram_id, username=f"user{telegram_id}")
    code = await fresh_db.issue_oauth_link_code(telegram_id, ttl_seconds=600, digits=8)
    client = client_factory()
    token = (await client.post("/auth/link", json={"code": code})).json()["token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def _template(fresh_db, name):
    return (await fresh_db.find_global_templates_by_names([name]))[name]


@pytest.mark.asyncio
async def test_template_preview_carries_muscles(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _template(fresh_db, "Тяга штанги в наклоне")
    body = (await client.get(f"/exercise-templates/{template['id']}")).json()
    assert body["media"]["muscles"] == {
        "primary": list(exercise_muscles.MUSCLES["Тяга штанги в наклоне"][0]),
        "secondary": list(exercise_muscles.MUSCLES["Тяга штанги в наклоне"][1]),
    }


@pytest.mark.asyncio
async def test_renamed_fork_keeps_muscles_own_exercise_has_none(fresh_db, client_factory):
    client = await _linked_client(fresh_db, client_factory)
    template = await _template(fresh_db, "Присед со штангой")
    fork_id = (await client.post(f"/exercise-templates/{template['id']}/add")).json()["id"]
    await db.update_exercise_name(fork_id, "Мой присед")
    media = (await client.get(f"/exercises/{fork_id}/media")).json()
    assert "quadriceps" in media["muscles"]["primary"]

    own_id = await db.create_exercise(111, "Моё странное упражнение", group_id=None)
    media = (await client.get(f"/exercises/{own_id}/media")).json()
    assert media["muscles"] is None
