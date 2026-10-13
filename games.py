"""Мини-игры между подходами (только iOS-приложение): правила сервера.

Сами игры живут в приложении — «Не урони» (`catch`), «Собери вес» (`weigh`),
«Разложи по стойкам» (`sort`), «Собака бежит» (`runner`). Сервер хранит
результаты партий (db.minigame_results), считает по ним лучший счёт, значки и
таблицу лидеров. HTTP — в api_v1_games.py, здесь — то, что от транспорта не
зависит: список игр, проверка правдоподобия, значки, подпись атлета в таблице.

Тексты (названия значков, «Атлет #NNNN») — только через i18n.t в момент
рендера: значения, разрешённые в строку на импорте, запомнили бы язык того,
кто первым дёрнул модуль (CLAUDE.md, «Ловушка, встретившаяся шесть раз»).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

import config
import i18n

# Порядок — тот, в котором игры показывает приложение (GET /v1/games).
GAMES: tuple[str, ...] = ("catch", "weigh", "sort", "runner")

# Проверка правдоподобия: потолок счёта по длительности партии. Партия выше
# потолка не пишется (202, accepted=false) — это не ошибка человека, а либо
# подделка запроса, либо баг клиента, и спорить с ним незачем.
#   score <= duration_ms / per_ms + bonus
#   catch  — блин падает не чаще раза в 150 мс, +2 на ловлю в первые мгновения;
#   weigh  — одна собранная штанга не быстрее 2.5 с, +1 на неполную секунду;
#   sort   — счёт = пройденные уровни, уровень не быстрее 3 с, +1;
#   runner — счёт = метры, собака бежит не быстрее 80 мс на метр, +5 на старт.
SCORE_CAPS: dict[str, tuple[int, int]] = {
    "catch": (150, 2),
    "weigh": (2500, 1),
    "sort": (3000, 1),
    "runner": (80, 5),
}

# Короче секунды партии не бывает: это двойной тап или повтор запроса.
MIN_DURATION_MS = 1000


def is_plausible(game: str, score: int, duration_ms: int) -> bool:
    """Может ли честная партия `game` длиной `duration_ms` дать `score`."""
    if score < 0 or duration_ms < MIN_DURATION_MS:
        return False
    per_ms, bonus = SCORE_CAPS[game]
    # Целочисленно, без деления: score <= duration_ms / per_ms + bonus.
    return score * per_ms <= duration_ms + bonus * per_ms


@dataclass(frozen=True)
class Badge:
    """Значок мини-игр. `threshold` — порог счёта: за одну партию или, при
    `cumulative`, суммой всех партий этой игры. Подписи — свойствами, а не
    полями: язык известен только в момент рендера."""

    code: str
    game: str
    threshold: int
    cumulative: bool = False

    @property
    def title(self) -> str:
        return i18n.t(f"games.badge.{self.code}.title")

    @property
    def description(self) -> str:
        """Взятый значок — констатацией («Поймал 50 блинов…»)."""
        return i18n.t(f"games.badge.{self.code}.description")

    @property
    def goal(self) -> str:
        """Ещё не взятый — целью в повелительном («Поймай 50 блинов…»):
        констатация под невзятым значком была бы неправдой о данных
        (TONE_OF_VOICE.md, «Пуш обязан быть правдой»)."""
        return i18n.t(f"games.badge.{self.code}.goal")


BADGES: tuple[Badge, ...] = (
    Badge("catch_100", "catch", 100, cumulative=True),
    Badge("catch_50_run", "catch", 50),
    Badge("weigh_10", "weigh", 10),
    Badge("sort_5", "sort", 5),
    Badge("runner_1km", "runner", 1000),
)


def cumulative_reached_at(scores: list[tuple[int, str]], threshold: int) -> Optional[str]:
    """Время партии, на которой сумма счёта впервые дошла до `threshold`.
    `scores` — (score, created_at) по порядку партий."""
    total = 0
    for score, at in scores:
        total += score
        if total >= threshold:
            return at
    return None


def anon_number(user_id: int) -> int:
    """Стабильный четырёхзначный номер атлета для «Атлет #NNNN»: HMAC-SHA256 от
    id с серверной солью, по модулю 10000. Обратно в id не разворачивается."""
    digest = hmac.new(
        config.GAMES_ANON_SALT.encode(), str(user_id).encode(), hashlib.sha256
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10000


NAME_MAX_LEN = 20


def leaderboard_name(user_id: int, username: Optional[str], show_name: bool) -> str:
    """Подпись атлета в таблице лидеров. Имя — только если атлет сам включил
    показ (PATCH /v1/games/settings) и имя у него есть: у аккаунта из
    приложения без Telegram его нет, и он остаётся «Атлетом #NNNN»."""
    name = (username or "").strip()
    if show_name and name:
        return name[:NAME_MAX_LEN]
    return i18n.t("games.anon_name", n=f"{anon_number(user_id):04d}")
