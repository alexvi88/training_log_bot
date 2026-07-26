from aiogram.fsm.state import State, StatesGroup


class WorkoutFlow(StatesGroup):
    idle = State()
    picking_group = State()
    picking_exercise = State()
    creating_exercise_name = State()
    logging_set = State()
    logging_exercise_note = State()
    confirming_finish_date = State()
    awaiting_finish_date = State()
    editing_finished_note = State()


class ExerciseManage(StatesGroup):
    picking_group = State()
    picking_exercise = State()
    editing_name = State()
    editing_description = State()
    new_group_name = State()
    creating_exercise_name = State()
    awaiting_photo = State()


class HistoryFlow(StatesGroup):
    browsing = State()


class ProgressFlow(StatesGroup):
    picking_group = State()
    picking_exercise = State()


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
    adding_exercise_group = State()
    adding_exercise_pick = State()


class AdminFlow(StatesGroup):
    browsing_users = State()
    browsing_history = State()
    browsing_pushes = State()
    browsing_ai_users = State()


class AITrainerFlow(StatesGroup):
    chatting = State()


class BodyweightFlow(StatesGroup):
    viewing = State()


class RoutineFlow(StatesGroup):
    naming = State()
    renaming = State()
    adding_exercise_group = State()
    adding_exercise_pick = State()


class ImportFlow(StatesGroup):
    awaiting_file = State()
    mapping_columns = State()
    confirming = State()


class FeedbackFlow(StatesGroup):
    awaiting_message = State()
