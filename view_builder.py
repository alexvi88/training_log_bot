"""Turns raw workout/block/set rows from db.py into formatting.py view objects."""

import db
from formatting import BlockView, ExerciseBlockView, SupersetBlockView


async def build_block_views(workout_id: int, formula: str = "epley") -> list[BlockView]:
    blocks = await db.list_blocks_for_workout(workout_id)
    views: list[BlockView] = []
    group_cache: dict[int | None, str] = {}

    async def group_info(group_id: int | None) -> str:
        if group_id is None:
            return "без группы"
        if group_id not in group_cache:
            g = await db.get_muscle_group(group_id)
            group_cache[group_id] = g["name"] if g else "?"
        return group_cache[group_id]

    for block in blocks:
        block_exs = await db.get_block_exercises(block["id"])
        sets = await db.list_sets_for_block(block["id"])
        if not block_exs:
            continue

        if block["type"] == "single":
            ex_id = block_exs[0]["exercise_id"]
            ex = await db.get_exercise(ex_id)
            gname = await group_info(ex["primary_group_id"])
            sets_tuples = [(s["weight"], s["reps"], bool(s["is_warmup"])) for s in sets]
            views.append(
                ExerciseBlockView(
                    group_name=gname,
                    exercise_name=ex["display_name"],
                    sets=sets_tuples,
                    formula=formula,
                )
            )
        else:
            ex_ids = [be["exercise_id"] for be in block_exs]
            names = [be["display_name"] for be in block_exs]
            rounds_map: dict[int, dict[int, tuple[float, int, bool]]] = {}
            for s in sets:
                rounds_map.setdefault(s["round_index"], {})[s["exercise_id"]] = (
                    s["weight"],
                    s["reps"],
                    bool(s["is_warmup"]),
                )
            rounds = [
                [rounds_map[r_idx].get(eid) for eid in ex_ids] for r_idx in sorted(rounds_map)
            ]
            views.append(SupersetBlockView(exercise_names=names, rounds=rounds))

    return views
