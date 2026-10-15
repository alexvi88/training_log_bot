"""Лента активности называет мини-игру и счёт, а не «сыграл в мини-игру»."""
import api_v1_activity


async def test_game_result_detail_names_the_game_and_score():
    assert await api_v1_activity.describe_detail({"game": "weigh", "score": 7, "duration_ms": 60000}, {}) == "«Собери вес» — 7"
    assert await api_v1_activity.describe_detail({"game": "catch", "score": 12}, {}) == "«Не урони» — 12"
    assert await api_v1_activity.describe_detail({"game": "newgame"}, {}) == "«newgame»"
