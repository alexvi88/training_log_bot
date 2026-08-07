"""Разбор видео подхода: глаза тренера, но не его голос.

Видео уходит в Qwen3-VL на Novita и возвращается структурой наблюдений — не
текстом для человека. Говорить с атлетом будет Grok (см. ai_trainer.ask,
параметр video_context): у него характер, история подходов и инструменты по
базе, а у этой модели только кадры. Ровно так же устроен живой веб-поиск —
отдельная модель добывает факты, основная их озвучивает.

Почему структура, а не готовый текст. В живых прогонах Qwen на «оцени технику»
выдавал «Общая оценка: 8.5/10» и «Отличная работа, продолжайте в том же
духе! 💪» — вежливый фитнес-ассистент из интернета, то есть ровно не тот
персонаж, который описан в TONE_OF_VOICE.md. Плюс полторы тысячи токенов
простыни ехали сорок секунд. Короткий JSON и быстрее, и дешевле, и голос
остаётся один.
"""

import base64
import json
import logging
from typing import Any, Optional

from openai import AsyncOpenAI

import config
import db

logger = logging.getLogger(__name__)

_client: Optional[AsyncOpenAI] = None


def is_configured() -> bool:
    return bool(config.NOVITA_API_KEY)


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.NOVITA_API_KEY,
            base_url=config.NOVITA_BASE_URL,
            timeout=config.VIDEO_ANALYSIS_TIMEOUT_SECONDS,
        )
    return _client


# Промпт выстрадан двумя живыми прогонами, и оба провала стоит помнить, потому
# что чинятся они разными частями текста.
#
# Провал 1 — «8.5/10, отличная работа 💪»: модель хвалит и ставит оценки. Лечится
# блоком ЗАПРЕЩЕНО.
#
# Провал 2 — важнее. На запрет выдумывать ошибки модель нашла бесплатный выход:
# объявила невидимым всё (включая изгиб спины на съёмке СБОКУ) и вернула ноль
# наблюдений при usable=true. Причина была в асимметрии: за пустой список
# наблюдений стояла похвала, а за «не видно» — никакой цены. Отсюда три вещи:
# обязательный шаг описания (от него не отвертеться), обязательное «почему» на
# каждый пункт «не видно», и явный перечень того, что со съёмки сбоку видно.
SYSTEM_PROMPT = """\
Ты — система разбора видео силовых упражнений. Ты НЕ разговариваешь с атлетом:
твой вывод читает другая модель, которая сама с ним поговорит.

Работа делится на два шага, и первый обязателен.

ШАГ 1 — ОПИСАНИЕ. Опиши, что происходит на видео, нейтрально и по фактам, без
разделения на хорошее и плохое. По каждому пункту — где ты это видишь по
времени. Описывать нужно то, что физически видно на съёмке такого типа:
траекторию снаряда относительно стоп, как меняется угол корпуса, что
разгибается раньше, как выглядит фиксация в верхней точке, темп фаз,
отличаются ли повторы между собой. «В норме» — допустимое описание: это
суждение с содержанием.

ШАГ 2 — ОТКЛОНЕНИЯ. Только после описания перечисли то, что расходится с
разумным исполнением. Каждое отклонение опирается на твоё же описание из
шага 1 и несёт доказательство в поле evidence.

ЗАПРЕЩЕНО:
- Оценки в баллах, проценты, «8.5/10», сводные рейтинги.
- Похвала, подбадривание, обращения к человеку, эмодзи.
- Советы и рекомендации. Что делать — решает не ты.
- Перечислять общие правила упражнения. «Штанга должна скользить по ногам»,
  «спина должна быть прямой» — это учебник. Такие формулировки допустимы
  ТОЛЬКО как описание конкретного отклонения с указанием момента.

ПРО «НЕ ВИДНО» — читай внимательно, здесь чаще всего ошибаются.

Список not_visible — это ограничения ИМЕННО ЭТОЙ СЪЁМКИ, и каждый пункт обязан
объяснить в поле why, что конкретно в кадре мешает: обрезано по колено, атлет
за стойкой, темно, снято сверху, спина к камере.

Не пиши в not_visible то, чего не видно ни на одном видео в принципе:
напряжение мышц, работу кора, намерения, усилие, боль, дыхание. Это не
ограничение ракурса, и в списке им не место.

Со съёмки СБОКУ видны: изгиб позвоночника и его изменение под нагрузкой,
траектория снаряда относительно середины стопы, углы в колене и
тазобедренном, наклон корпуса, положение головы, момент фиксации. Не заявляй,
что этого не видно, если ракурс боковой и кадр не обрезан.

НЕПРОТИВОРЕЧИВОСТЬ: если view.usable = true и view.problem пустой, то
not_visible должен быть пустым или содержать один-два пункта. Нельзя
одновременно утверждать, что ракурс хороший, и что судить по нему нельзя. Если
not_visible получается длинным — значит ракурс плохой, и тогда usable = false
с заполненным problem.

Пустой список observations допустим, но только если шаг 1 заполнен подробно:
«описал всё и отклонений не нашёл» — честно, а «ничего не описал и ничего не
нашёл» — нет.

Отвечай строго одним JSON-объектом:

{
  "exercise": "какое упражнение, или \\"не определил\\"",
  "reps_seen": <сколько повторов видно целиком>,
  "view": {
    "angle": "сбоку | спереди | сзади | три четверти | не определить",
    "usable": <true/false>,
    "problem": "что мешает, или \\"\\""
  },
  "description": [
    {"aspect": "...", "what_i_see": "нейтрально, по факту", "when": "0:04"}
  ],
  "observations": [
    {
      "what": "что расходится с разумным исполнением",
      "phase": "съём | подъём | верхняя точка | опускание | между повторами",
      "when": "0:04",
      "reps": [<номера повторов, которых это касается>],
      "evidence": "что конкретно видно в кадре, из чего это следует",
      "severity": "мешает | стоит поправить | мелочь",
      "confidence": "высокая | средняя | низкая"
    }
  ],
  "not_visible": [{"what": "...", "why": "что именно в этой съёмке мешает"}],
  "camera_advice": "как переснять, если ракурс плохой, иначе \\"\\""
}"""

USER_PROMPT = (
    "Разбери это видео. Сначала заполни description по фактам, потом "
    "observations — только то, что следует из твоего же описания."
)


# То, что не видно ни на одном видео в принципе. Промпт это запрещает прямым
# текстом, и модель всё равно вписывала сюда «напряжение мышц кора и ягодиц»,
# сама же поясняя в why: «это не ограничение ракурса, а принципиальная
# невозможность оценить по видео». Процитировала запрет и нарушила его — значит
# добивать промптом бесполезно, дешевле фильтр в коде.
_UNOBSERVABLE_MARKERS = (
    "напряжен", "кор", "ягодич", "мышц", "дыхан", "намерен",
    "усили", "боль", "пульс", "самочувств",
)

_SEVERITY_ORDER = {"мешает": 0, "стоит поправить": 1, "мелочь": 2}


def _is_unobservable(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _UNOBSERVABLE_MARKERS)


def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
    """Убрать из ответа модели то, что она обещала не писать.

    Чинит ровно те отклонения, которые видели живьём, и ничего не достраивает:
    пустой разбор так и остаётся пустым — это честный исход.
    """
    view = data.get("view") or {}
    usable = bool(view.get("usable", True))
    problem = (view.get("problem") or "").strip()

    # Наблюдение без доказательства — то самое «дописал, а не увидел», ради
    # чего поле evidence и заведено. Нет его — нет наблюдения.
    observations = [
        obs for obs in (data.get("observations") or [])
        if isinstance(obs, dict) and (obs.get("evidence") or "").strip()
    ]
    observations.sort(key=lambda o: _SEVERITY_ORDER.get(o.get("severity"), 3))

    not_visible = [
        item for item in (data.get("not_visible") or [])
        if isinstance(item, dict) and not _is_unobservable(str(item.get("what", "")))
    ]
    # Непротиворечивость, которую промпт просит, а модель соблюдает не всегда:
    # «ракурс хороший, помех нет» и длинный список «судить нельзя» вместе не
    # живут. Верим первому — оно проверяемо, а список был отпиской.
    if usable and not problem:
        not_visible = not_visible[:2]

    return {
        "exercise": (data.get("exercise") or "не определил"),
        "reps_seen": data.get("reps_seen"),
        "view": {"angle": view.get("angle"), "usable": usable, "problem": problem},
        "description": [d for d in (data.get("description") or []) if isinstance(d, dict)],
        "observations": observations,
        "not_visible": not_visible,
        "camera_advice": (data.get("camera_advice") or "").strip(),
    }


async def analyze(
    video_bytes: bytes,
    user_id: Optional[int],
    mime_type: str = "video/mp4",
) -> Optional[dict[str, Any]]:
    """Видео → структура наблюдений, или None если разобрать не удалось.

    None означает ровно «наблюдений нет»: тренер в этом случае отвечает без
    видео, а не извиняется за чужую поломку.

    mime_type берётся из самого апдейта, а не подставляется наугад: ролик без
    аудиодорожки Telegram отдаёт как animation, и там встречается и video/mp4, и
    image/gif. Соврать про тип в data: URL — значит отправить модели файл под
    чужой вывеской.
    """
    data_url = f"data:{mime_type};base64," + base64.b64encode(video_bytes).decode()
    try:
        response = await _get_client().chat.completions.create(
            model=config.NOVITA_VIDEO_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {"type": "video_url", "video_url": {"url": data_url}},
                    ],
                },
            ],
            max_tokens=config.VIDEO_ANALYSIS_MAX_TOKENS,
            temperature=config.VIDEO_ANALYSIS_TEMPERATURE,
            response_format={"type": "json_object"},
        )
    except Exception:
        logger.exception("video analysis call failed for user %s", user_id)
        return None

    # Цену считает дневной отчёт по имени модели (config.LLM_PRICES_USD_PER_1K),
    # поэтому логируем настоящий usage тем же событием, что и вызовы Grok.
    # Своей оценке модели тут веры нет: на вопрос «сколько токенов ушло» она
    # выдумывает число, у неё нет доступа к счётчику.
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
    completion_tokens = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    try:
        await db.log_cost_event(
            user_id,
            "llm_call",
            model=config.NOVITA_VIDEO_MODEL,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=getattr(details, "cached_tokens", 0) or 0,
        )
    except Exception:
        logger.exception("failed to log video analysis cost event")

    raw = (response.choices[0].message.content or "").strip()
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("video analysis returned non-JSON for user %s: %r", user_id, raw[:300])
        return None
    if not isinstance(parsed, dict):
        return None
    return _sanitize(parsed)


def to_context_block(analysis: dict[str, Any]) -> str:
    """Наблюдения → текст, который получает Grok.

    Пишется как показания свидетеля, а не как готовый ответ: явно сказано, что
    смотрела другая модель и что уверенность у наблюдений разная. Иначе тренер
    озвучит догадку как факт — а по CLAUDE.md утверждать про данные атлета
    можно только то, что данные подтверждают.
    """
    view = analysis.get("view") or {}
    lines = [
        "Разбор присланного видео. Смотрела отдельная модель по кадрам — она не "
        "знает ни истории атлета, ни его весов, и может ошибаться. Это "
        "показания, а не приговор: наблюдение с низкой уверенностью подавай как "
        "предположение («похоже, что...»), а не как факт. Своих наблюдений не "
        "добавляй — чего нет в списке, того на видео не видели.",
        f"Упражнение на видео: {analysis.get('exercise')}.",
    ]
    if analysis.get("reps_seen"):
        lines.append(f"Повторов видно: {analysis['reps_seen']}.")
    lines.append(f"Ракурс: {view.get('angle')}, годен для разбора: {view.get('usable')}.")
    if view.get("problem"):
        lines.append(f"Помеха на съёмке: {view['problem']}.")

    if analysis.get("description"):
        lines.append("\nЧто видно на видео:")
        for item in analysis["description"]:
            when = item.get("when")
            lines.append(
                f"- {item.get('aspect')}: {item.get('what_i_see')}"
                + (f" ({when})" if when else "")
            )

    if analysis.get("observations"):
        lines.append("\nОтклонения:")
        for obs in analysis["observations"]:
            reps = obs.get("reps")
            lines.append(
                f"- [{obs.get('severity')}, уверенность {obs.get('confidence')}] "
                f"{obs.get('what')} — фаза: {obs.get('phase')}, {obs.get('when')}"
                + (f", повторы {reps}" if reps else "")
                + f". Из чего следует: {obs.get('evidence')}"
            )
    else:
        lines.append(
            "\nОтклонений не нашли. Это не значит «идеально» — значит, заметного "
            "по этому видео не увидели. Не выдумывай ошибку, чтобы было что сказать."
        )

    if analysis.get("not_visible"):
        lines.append("\nПо этому видео оценить нельзя:")
        for item in analysis["not_visible"]:
            lines.append(f"- {item.get('what')} ({item.get('why')})")

    if analysis.get("camera_advice"):
        lines.append(f"\nКак переснять: {analysis['camera_advice']}")
    return "\n".join(lines)
