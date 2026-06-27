from aiogram.fsm.state import State, StatesGroup


class WorkoutFlow(StatesGroup):
    idle = State()
    picking_group = State()
    picking_exercise = State()
    creating_exercise_name = State()
    logging_set = State()
    finishing_note = State()


class ExerciseManage(StatesGroup):
    picking_group = State()
    picking_exercise = State()
    editing_name = State()
    new_group_name = State()
    creating_exercise_name = State()


class HistoryFlow(StatesGroup):
    browsing = State()


class SettingsFlow(StatesGroup):
    menu = State()


class BackfillFlow(StatesGroup):
    awaiting_date = State()


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
