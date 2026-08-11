"""video_analysis: чистка ответа модели и текст наблюдений для тренера.

Оба сценария в _sanitize взяты с живых прогонов Qwen3-VL, а не придуманы:
модель писала в not_visible «напряжение мышц кора и ягодиц», сама же поясняя,
что это не ограничение ракурса, и одновременно держала usable=true с пустым
problem при списке «судить нельзя» из четырёх пунктов.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import config
import video_analysis

pytestmark = pytest.mark.asyncio


def _checklist(**verdicts):
    """Семь точек, по умолчанию все пройдены с описанием увиденного."""
    return [
        {
            "point": point,
            "verdict": verdicts.get(point, "норма"),
            "what_i_see": f"по {point} в кадре видно вот это",
            "when": "0:04",
        }
        for point in video_analysis.CHECKPOINTS
    ]


def _analysis(**over):
    base = {
        "exercise": "становая тяга",
        "reps_seen": 3,
        "view": {"angle": "сбоку", "usable": True, "problem": ""},
        "checklist": _checklist(),
        "observations": [],
        "not_visible": [],
        "camera_advice": "",
    }
    base.update(over)
    return base


async def test_unobservable_items_dropped_from_not_visible():
    """«Напряжение мышц» — не ограничение ракурса, и промптом это не лечится."""
    out = video_analysis._sanitize(
        _analysis(
            view={"angle": "сзади", "usable": False, "problem": "спина к камере"},
            not_visible=[
                {"what": "напряжение мышц кора и ягодиц", "why": "не видно по телу"},
                {"what": "дыхание и ритм", "why": "не видно по грудной клетке"},
                {"what": "положение стоп", "why": "обрезаны по щиколотки"},
            ],
        )
    )
    what = [item["what"] for item in out["not_visible"]]
    assert what == ["положение стоп"]


async def test_long_not_visible_trimmed_when_view_declared_fine():
    """usable=true с пустым problem и «судить нельзя» на четыре пункта — отписка."""
    out = video_analysis._sanitize(
        _analysis(
            not_visible=[
                {"what": "изгиб позвоночника", "why": "виден только силуэт"},
                {"what": "положение лопаток", "why": "камера сбоку"},
                {"what": "положение головы", "why": "не разглядеть"},
                {"what": "угол таза", "why": "не определить"},
            ]
        )
    )
    assert len(out["not_visible"]) == 2


async def test_not_visible_kept_when_view_really_bad():
    """А когда ракурс признан плохим — список ограничений законный, не режем."""
    out = video_analysis._sanitize(
        _analysis(
            view={"angle": "сзади", "usable": False, "problem": "обрезано по колено"},
            not_visible=[
                {"what": "изгиб позвоночника", "why": "спина к камере"},
                {"what": "траектория грифа", "why": "обрезано по колено"},
                {"what": "угол в колене", "why": "колени вне кадра"},
            ],
        )
    )
    assert len(out["not_visible"]) == 3


async def test_observation_without_evidence_dropped():
    """Поле evidence — весь смысл затеи: без него наблюдение дописано, а не увидено."""
    out = video_analysis._sanitize(
        _analysis(
            observations=[
                {"what": "круглая спина", "evidence": "на 0:05 поясница выгнута наружу",
                 "severity": "мешает", "confidence": "высокая"},
                {"what": "колени внутрь", "evidence": "   ", "severity": "мешает"},
                {"what": "рывок с пола", "severity": "мелочь"},
            ]
        )
    )
    assert [o["what"] for o in out["observations"]] == ["круглая спина"]


async def test_observations_sorted_by_severity():
    """Тренеру важное подаётся первым, иначе он озвучит мелочь как главное."""
    out = video_analysis._sanitize(
        _analysis(
            observations=[
                {"what": "мелочь", "evidence": "видно", "severity": "мелочь"},
                {"what": "главное", "evidence": "видно", "severity": "мешает"},
                {"what": "среднее", "evidence": "видно", "severity": "стоит поправить"},
            ]
        )
    )
    assert [o["what"] for o in out["observations"]] == ["главное", "среднее", "мелочь"]


async def test_context_block_states_empty_findings_honestly():
    """Пустой разбор не должен превращаться в «идеально» — это разные утверждения."""
    block = video_analysis.to_context_block(_analysis())
    assert "Отклонений не нашли" in block
    assert "Не выдумывай ошибку" in block
    assert "становая тяга" in block
    # Живой провал: на пустом списке тренер сказал «чинить нечего, оставляй
    # такой рисунок и наращивай нагрузку» — похвалил технику и велел грузить её,
    # не имея ни одного наблюдения. Запрет на это должен доезжать до него текстом.
    assert "не хвали" in block
    assert "добавлять вес" in block


async def test_checkpoint_marked_normal_without_evidence_becomes_unseen():
    """«Норма» без описания — проставленная галочка, а не осмотр.

    Дороже любой другой ошибки: тренер верит «норме» и советует грузить вес.
    """
    checklist = _checklist()
    checklist[0]["what_i_see"] = "   "
    out = video_analysis._sanitize(_analysis(checklist=checklist))
    spine = next(item for item in out["checklist"] if item["point"] == "спина")
    assert spine["verdict"] == "не видно"


async def test_skipped_checkpoint_is_not_invented_as_normal():
    """Точку, которую модель не прошла, не достраиваем — тренер узнает правду.

    Ровно этот провал и видели живьём: модель описала стопы, гриф, разгибание,
    фиксацию и повторы, молча пропустив спину, — и вернула ноль отклонений.
    """
    checklist = [item for item in _checklist() if item["point"] != "спина"]
    out = video_analysis._sanitize(_analysis(checklist=checklist))

    assert [item["point"] for item in out["checklist"]] == [
        p for p in video_analysis.CHECKPOINTS if p != "спина"
    ]
    block = video_analysis.to_context_block(out)
    assert "не смотрели вовсе: спина" in block


async def test_checklist_reordered_to_our_order_and_junk_points_dropped():
    checklist = list(reversed(_checklist())) + [
        {"point": "настрой", "verdict": "норма", "what_i_see": "собран"}
    ]
    out = video_analysis._sanitize(_analysis(checklist=checklist))
    assert [item["point"] for item in out["checklist"]] == list(video_analysis.CHECKPOINTS)


async def test_checkpoint_deviation_reaches_coach_even_without_observation():
    """Модель теряет своё наблюдение между шагом 1 и шагом 2 — косяк не должен пропасть."""
    out = video_analysis._sanitize(
        _analysis(checklist=_checklist(**{"спина": "отклонение"}), observations=[])
    )
    block = video_analysis.to_context_block(out)
    assert "спина [ОТКЛОНЕНИЕ]" in block


async def test_context_block_carries_severity_and_confidence():
    block = video_analysis.to_context_block(
        _analysis(
            observations=[{
                "what": "круглая спина", "phase": "съём", "when": "0:05", "reps": [2, 3],
                "evidence": "поясница выгнута наружу", "severity": "мешает",
                "confidence": "низкая",
            }]
        )
    )
    assert "мешает" in block and "низкая" in block
    assert "поясница выгнута наружу" in block
    # Тренеру прямо сказано, как обращаться с неуверенным наблюдением.
    assert "предположение" in block


async def test_analyze_logs_real_usage_for_pricing(monkeypatch):
    """Цену считает дневной отчёт по usage — значит логировать надо настоящий."""
    logged = {}

    async def fake_log(
        user_id, event_type, *, model=None, prompt_tokens=0, completion_tokens=0,
        cached_tokens=0, reasoning_tokens=0,
    ):
        logged.update(
            user_id=user_id, event_type=event_type, model=model,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        )

    monkeypatch.setattr(video_analysis.db, "log_cost_event", fake_log)
    monkeypatch.setattr(config, "NOVITA_API_KEY", "test-key")

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=24000, completion_tokens=900),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    out = await video_analysis.analyze(b"fake-mp4-bytes", 42)

    assert out is not None
    assert logged["event_type"] == "llm_call"
    assert logged["model"] == config.NOVITA_VIDEO_MODEL
    assert logged["prompt_tokens"] == 24000
    assert logged["completion_tokens"] == 900
    # Модель в этом прайсе должна быть, иначе отчёт посчитает её по дефолтной
    # ставке Grok и цифра уедет.
    assert config.NOVITA_VIDEO_MODEL in config.LLM_PRICES_USD_PER_1K


async def test_analyze_sends_video_as_data_url_not_telegram_link(monkeypatch):
    """В URL файла Telegram лежит токен бота — наружу он уехать не должен."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    await video_analysis.analyze(b"bytes", 1)

    content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    video_part = next(p for p in content if p["type"] == "video_url")
    assert video_part["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert "api.telegram.org" not in video_part["video_url"]["url"]


async def test_analyze_returns_none_on_non_json(monkeypatch):
    """Мусор вместо JSON — тренер отвечает без видео, а не падает."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Оценка техники: 8.5/10 💪"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    assert await video_analysis.analyze(b"bytes", 1) is None


async def test_analyze_returns_none_when_provider_fails(monkeypatch):
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("502"))
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    assert await video_analysis.analyze(b"bytes", 1) is None


# ---------- ответ thinking-модели ----------


async def test_reasoning_block_is_stripped_before_parsing():
    """Thinking печатает цепочку в тот же content, что и ответ. Без чистки
    json.loads падает, и разбор целиком уходит в None — то есть переключение на
    thinking просто выключило бы фичу."""
    raw = '<think>Смотрю на поясницу, к середине подъёма она круглится.</think>\n{"exercise": "становая"}'

    assert json.loads(video_analysis._strip_reasoning(raw)) == {"exercise": "становая"}


async def test_json_fence_and_chatter_around_the_object_are_stripped():
    """Модель то обрамляет ответ ```json, то дописывает текст до и после."""
    fenced = '```json\n{"exercise": "присед"}\n```'
    chatty = 'Вот разбор:\n{"exercise": "присед"}\nГотово.'

    assert json.loads(video_analysis._strip_reasoning(fenced)) == {"exercise": "присед"}
    assert json.loads(video_analysis._strip_reasoning(chatty)) == {"exercise": "присед"}


async def test_analyze_parses_a_thinking_response(monkeypatch):
    """Полный путь: ответ с рассуждением доезжает до разобранной структуры."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    content = "<think>долго думаю</think>\n" + json.dumps(_analysis())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    out = await video_analysis.analyze(b"bytes", 1)

    assert out is not None
    assert out["exercise"] == "становая тяга"


async def test_the_prompt_biases_doubt_towards_flagging_not_towards_praise():
    """Калибровка, ради которой промпт и правился: сомнение округляется в
    «отклонение с низкой уверенностью», а не в «норму». Молчание тренер читает
    как «техника в порядке» и советует добавить вес."""
    assert "confidence" in video_analysis.SYSTEM_PROMPT
    assert "Сомневаешься" in video_analysis.SYSTEM_PROMPT


async def test_the_prompt_no_longer_hands_out_per_exercise_failure_lists():
    """Список типовых провалов по упражнениям убран, и это не упрощение.

    Живой провал: тягу штанги к поясу модель приняла за становую — и выдала
    ровно три пункта, которые мы сами же перечислили в промпте для становой
    (круглая поясница, таз раньше плеч, гриф от голени). Не потому что видела
    их, а потому что для становой это самый частый набор. Список превратился в
    шпаргалку с «правильными ответами» под неверную догадку.

    Та же ловушка, из-за которой мы отказывались подавать сюда техничку из
    exercise_descriptions, просто с другой стороны: не «как надо», а «как
    обычно ломается». Пересказывается и то и другое одинаково охотно.
    """
    prompt = video_analysis.SYSTEM_PROMPT
    assert "таз стреляет" not in prompt
    assert "колени сваливаются внутрь на подъёме" not in prompt
    assert "НЕ подгоняй наблюдения под типовые ошибки" in prompt


async def test_named_exercise_from_the_caption_beats_the_models_eyes(monkeypatch):
    """Подпись атлета надёжнее глаз модели, и она обрывает подгонку."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    await video_analysis.analyze(b"bytes", 1, exercise_hint="тяга штанги к поясу")

    content = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    text = next(part["text"] for part in content if part["type"] == "text")
    assert "тяга штанги к поясу" in text
    assert "верь подписи" in text


async def test_low_exercise_confidence_makes_the_coach_ask_first():
    """Не уверен в упражнении — спроси, а не разбирай наугад."""
    block = video_analysis.to_context_block(_analysis(exercise_confidence="низкая"))
    assert "уверенность низкая" in block
    assert "сначала спроси" in block


async def test_coach_is_told_not_to_relabel_findings_onto_a_corrected_exercise():
    """Живой провал: атлет поправил упражнение, и тренер переклеил на него те же
    три наблюдения — собранные под другое движение и, возможно, подогнанные под
    его типовые ошибки."""
    block = video_analysis.to_context_block(_analysis())
    assert "НЕ переклеивай" in block


async def test_json_mode_is_off_by_default(monkeypatch):
    """Прод лёг ровно на этом: Novita отдаёт 400 INVALID_REQUEST_BODY —
    «qwen3-vl-235b-a22b-thinking does not support feature: structured-outputs».
    Instruct фичу умел, thinking нет, и переключение модели положило КАЖДЫЙ
    разбор. Неподдерживаемый параметр провайдер не игнорирует, а роняет запрос,
    поэтому по умолчанию его не шлём вовсе."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_JSON_MODE", False)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    await video_analysis.analyze(b"bytes", 1)

    assert "response_format" not in client.chat.completions.create.call_args.kwargs


async def test_json_mode_can_be_switched_back_on(monkeypatch):
    """Флаг — чтобы вернуть строгий JSON одной переменной, когда модель его умеет."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_JSON_MODE", True)
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_analysis())))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    await video_analysis.analyze(b"bytes", 1)

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}


async def test_bare_json_without_strict_mode_still_parses(monkeypatch):
    """Раз строгий режим выключен, разбор ответа держится на нашем парсере —
    он и есть страховка, а не response_format."""
    monkeypatch.setattr(video_analysis.db, "log_cost_event", AsyncMock())
    monkeypatch.setattr(config, "VIDEO_ANALYSIS_JSON_MODE", False)
    content = "Разбор готов:\n```json\n" + json.dumps(_analysis()) + "\n```\nВсё."
    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
    )
    monkeypatch.setattr(video_analysis, "_get_client", lambda: client)

    out = await video_analysis.analyze(b"bytes", 1)

    assert out is not None
    assert out["exercise"] == "становая тяга"


async def test_no_false_alarm_when_observation_wording_differs_from_point_name(caplog):
    """Живой прогон: точка «порядок движения» → наблюдение «таз поднимается
    раньше плеч». Названия точек наши, формулировки модели свободные, совпадать
    они не обязаны — и прежняя проверка по подстроке кричала о потере на
    каждом ролике. Лог, срабатывающий всегда, приучает не смотреть."""
    import logging

    with caplog.at_level(logging.WARNING, logger="video_analysis"):
        video_analysis._sanitize(
            _analysis(
                checklist=_checklist(**{"порядок движения": "отклонение"}),
                observations=[{
                    "what": "таз поднимается раньше плеч",
                    "evidence": "на 0:03 таз вверху, гриф ниже колен",
                    "severity": "мешает",
                }],
            )
        )

    assert not caplog.records


async def test_warns_when_deviations_exist_but_observations_empty(caplog):
    """А вот это настоящая потеря — про неё предупредить обязаны."""
    import logging

    with caplog.at_level(logging.WARNING, logger="video_analysis"):
        video_analysis._sanitize(
            _analysis(checklist=_checklist(**{"спина": "отклонение"}), observations=[])
        )

    assert any("observations пуст" in r.message for r in caplog.records)


async def test_context_block_tells_the_coach_to_stay_out_of_the_log():
    """Живой прогон: на «разбери технику» Grok сходил в базу дважды.

    Из данных он взял ровно одну фразу — «веса рабочие серьёзные, значит косяк
    не мелочь». Стоила она двух лишних раундов (~6.4¢) и девяти секунд ожидания
    человека в зале, притом что блины видно прямо на видео. Не запрет: спросят
    про прогресс — сходит.
    """
    block = video_analysis.to_context_block(_analysis())
    assert "в дневник без надобности не лезь" in block
    assert "прогресс" in block
