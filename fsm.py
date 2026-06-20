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
