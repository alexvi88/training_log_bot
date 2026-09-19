"""Поиск по истории тренировок «в какой тренировке был жим» — сбор данных,
отдельно от их показа.

Бот листает найденное текстовым списком с постраничностью
(handlers/history.py._render_search_page, поверх db.search_workouts_by_exercise),
приложение получит то же самое JSON'ом через `/v1`. Запрос и постраничная
математика («сколько показано из скольки найдено») — общий кусок для обоих:
db.search_workouts_by_exercise/count_workouts_by_exercise уже единственный
источник правды о самом поиске, а третьей парой запросов (найти + досчитать
имена упражнений и число подходов на каждую тренировку) второй REST-слой не
заводит — она здесь одна, как и у dashboard_data/progress_data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import db


@dataclass(frozen=True)
class WorkoutSearchItem:
    """Одна найденная тренировка — ровно то, что показывает бот в строке
    списка (дата) и API — в элементе JSON (плюс состав, как у GET /workouts)."""

    id: int
    started_at: str
    exercise_names: list[str] = field(default_factory=list)
    set_count: int = 0


@dataclass(frozen=True)
class WorkoutSearchPage:
    """Одна страница результатов плюс общее число совпадений — без него старые
    тренировки частого упражнения были бы физически недостижимы после первой
    страницы, а «показано N из M» нечем было бы посчитать ни боту, ни клиенту."""

    items: list[WorkoutSearchItem]
    total: int
    offset: int
    limit: int

    @property
    def has_next(self) -> bool:
        return self.offset + len(self.items) < self.total


async def search(user_id: int, query: str, limit: int, offset: int) -> WorkoutSearchPage:
    """Страница законченных тренировок, где встречается `query`, новые сначала."""
    workouts = await db.search_workouts_by_exercise(user_id, query, limit=limit, offset=offset)
    contents = await db.list_workout_contents([w["id"] for w in workouts])
    items = [
        WorkoutSearchItem(
            id=w["id"],
            started_at=w["started_at"],
            exercise_names=contents.get(w["id"], ([], 0))[0],
            set_count=contents.get(w["id"], ([], 0))[1],
        )
        for w in workouts
    ]
    total = await db.count_workouts_by_exercise(user_id, query)
    return WorkoutSearchPage(items=items, total=total, offset=offset, limit=limit)
