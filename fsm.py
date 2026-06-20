from aiogram.fsm.state import State, StatesGroup


class WorkoutFlow(StatesGroup):
    idle = State()
    picking_group = State()
    picking_exercise = State()
    creating_exercise_name = State()
    creating_exercise_attrs = State()
    logging_set = State()
    picking_superset_exercises = State()
    logging_superset = State()
    entering_weight = State()
    entering_reps = State()
    finishing_note = State()


class ExerciseManage(StatesGroup):
    picking_group = State()
    picking_exercise = State()
    editing = State()
    editing_step = State()
    new_group_name = State()


class HistoryFlow(StatesGroup):
    browsing = State()


class SettingsFlow(StatesGroup):
    menu = State()
    awaiting_weight_step = State()
    awaiting_bodyweight = State()


class BackfillFlow(StatesGroup):
    awaiting_date = State()
    awaiting_bulk_text = State()
    confirming = State()


class ResolveFlow(StatesGroup):
    """Shared sub-flow for mapping a free-typed exercise name to an exercise row."""
    picking = State()
    picking_new_group = State()


class EditWorkoutFlow(StatesGroup):
    viewing = State()
    awaiting_date = State()
    editing_set = State()
    adding_set = State()


class ImportFlow(StatesGroup):
    awaiting_file = State()
    mapping_columns = State()
    confirming = State()
