"""Своё фото упражнения — файлом на нашем диске, а не только ссылкой в Telegram.

Зачем вообще: раньше единственным хранилищем был `exercises.custom_photo_file_id`
— идентификатор файла ВНУТРИ Telegram, привязанный к токену бота. Смена токена
разом превращает все такие ссылки в ничто, а восстановить их неоткуда: самого
файла у нас никогда не было. Плюс приложение по такой ссылке фото не покажет —
у него нет ни токена бота, ни доступа к Bot API.

Поэтому колонки теперь две и обе живые:

- `custom_photo_file_id` — кэш отправки в Telegram. Переотправка по file_id
  не стоит ничего, а заливка файла заново — стоит, и бот в чате шлёт фото
  именно так, пока ссылка жива. Пустая колонка при живом файле — нормальное
  состояние (фото приехало из приложения либо токен сменился): бот тогда
  отправляет файл с диска и кладёт полученный file_id обратно в колонку.
- `custom_photo_path` — имя файла в этом каталоге. Источник правды: только
  он переживает смену токена и только его умеет отдать REST `/v1`.

Каталог — config.EXERCISE_PHOTO_DIR (постоянный том рядом с базой), раздача —
api_v1_media.py, как и у каталожных демонстраций. Хранится ИМЯ файла, а не
полный путь: путь каталога может смениться переменной окружения, и записанный
в базу абсолютный путь после такого переезда указывал бы в никуда.
"""

from __future__ import annotations

import logging
import os
import uuid

import config

logger = logging.getLogger(__name__)

# Те же расширения, что принимает загрузка фото в `/v1` (api_v1_ai.
# IMAGE_EXTENSION_BY_MIME) — свой список тут был бы вторым набором правил о
# том же самом.
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

# Расширение для фото, приехавшего из Telegram: бот всегда отдаёт сжатое
# фото в JPEG, отдельного mime в ответе Bot API нет.
TELEGRAM_EXTENSION = "jpg"


def photo_dir() -> str:
    """Каталог читается функцией, а не константой модуля: тесты подменяют
    config.EXERCISE_PHOTO_DIR на временный каталог, и константа, снятая при
    импорте, осталась бы указывать на боевой /data."""
    return config.EXERCISE_PHOTO_DIR


def stored_name(exercise) -> str | None:
    """Имя файла из строки упражнения — или None, если фото нет.

    try/except, а не `exercise["custom_photo_path"]` в лоб: сюда приходят и
    подставные строки-словари из тестов соседних модулей, заведённые до
    появления колонки, и падать на них незачем — «нет колонки» это «нет
    фото» (тот же приём, что у exercise_media.catalog_key)."""
    try:
        return exercise["custom_photo_path"] or None
    except (IndexError, KeyError, TypeError):
        return None


def path_for(name: str | None) -> str | None:
    """Имя файла → полный путь, если файл на месте.

    Имя приходит из базы, но проверка выхода за каталог здесь всё равно есть:
    это последний рубеж перед открытием файла, и он не должен зависеть от
    того, как значение попало в колонку."""
    if not name:
        return None
    root = os.path.realpath(photo_dir())
    candidate = os.path.realpath(os.path.join(photo_dir(), name))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def path_for_exercise(exercise) -> str | None:
    """path_for для строки упражнения — то, чем проверяют «файл правда есть»."""
    return path_for(stored_name(exercise))


def has_photo(exercise) -> bool:
    """Есть ли у упражнения своё фото — хоть файлом, хоть ещё только ссылкой
    в Telegram. Кнопка «🗑 Удалить фото» и карточка ориентируются на это, а не
    на одну конкретную колонку: у не перенесённого пока упражнения заполнена
    только ссылка, у приехавшего из приложения — только файл."""
    try:
        file_id = exercise["custom_photo_file_id"]
    except (IndexError, KeyError, TypeError):
        file_id = None
    return bool(file_id) or stored_name(exercise) is not None


def save(exercise_id: int, raw: bytes, ext: str) -> str:
    """Положить байты фото в каталог и вернуть имя файла для базы.

    Имя с новым uuid на каждую загрузку, а не `{exercise_id}.jpg`: замена фото
    иначе переписала бы файл под тем же адресом, и клиент (и его http-кэш)
    продолжал бы показывать старую картинку. Запись через временный файл с
    переименованием — чтобы обрыв на середине не оставил в каталоге
    полуфайл, который потом отдастся как «фото».
    """
    ext = ext.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported photo extension: {ext!r}")
    # Проверка типа до создания каталога и файла: иначе «не байты» (пустой
    # ответ Telegram, подставной объект) оставил бы после себя каталог и
    # недописанный .part.
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise ValueError("photo payload must be non-empty bytes")
    os.makedirs(photo_dir(), exist_ok=True)
    name = f"ex{exercise_id}_{uuid.uuid4().hex[:12]}.{ext}"
    final_path = os.path.join(photo_dir(), name)
    tmp_path = final_path + ".part"
    with open(tmp_path, "wb") as fh:
        fh.write(raw)
    os.replace(tmp_path, final_path)
    return name


def delete(name: str | None) -> None:
    """Снести файл по имени. Отсутствие файла — не ошибка: сюда приходят и
    имена из базы, файл под которыми уже мог пропасть (ручная чистка тома,
    восстановление базы из бэкапа), и звать это приходится на каждом пути
    удаления — падать там нечему."""
    path = path_for(name)
    if path is None:
        return
    try:
        os.remove(path)
    except OSError:
        logger.warning("exercise_photos: не удалось снести %s", path, exc_info=True)


async def backfill_from_telegram(bot) -> int:
    """Разовый перенос уже сохранённых фото из Telegram к себе на диск.

    Почему фоновой задачей на старте, а не отдельным скриптом и не «по
    первому обращению»:

    - скрипт пришлось бы не забыть запустить руками на каждом развороте, а
      бот выкатывается автоматически — шаг, который можно забыть, рано или
      поздно забудут, и до этого момента у части упражнений фото в приложении
      просто нет;
    - «по первому обращению» означало бы поход в Bot API прямо внутри
      GET-запроса приложения: первый показ карточки упирается в чужую сеть, а
      при отвалившемся Telegram превращается в ошибку на ровном месте.

    Безопасность: каждое упражнение — в своём try/except, сбой одного файла
    (истёкший file_id, сеть, место на диске) только пишется в лог и не мешает
    остальным. `custom_photo_file_id` не трогаем НИ В ОДНОМ случае: пока файл
    не лёг на диск, ссылка — единственное, что вообще есть у этого фото.
    Повторный запуск берёт только те строки, у которых файла ещё нет, так что
    перенос идемпотентен и на втором старте не делает ничего.

    `import db` внутри функции, а не наверху модуля: db.py импортирует
    exercise_photos ради удаления файлов, и встречный импорт на уровне модуля
    замкнул бы круг.
    """
    import db

    rows = await db.list_exercises_with_unsaved_photo()
    if not rows:
        return 0
    migrated = 0
    for row in rows:
        try:
            buf = await bot.download(row["custom_photo_file_id"])
            # Пустой/непонятный ответ Telegram отсеет сам save() — заводить
            # здесь вторую проверку того же самого незачем.
            name = save(row["id"], buf.read(), TELEGRAM_EXTENSION)
            await db.set_exercise_photo_path(row["id"], name)
            migrated += 1
        except Exception:
            logger.warning(
                "exercise_photos: не удалось перенести фото упражнения %s из Telegram",
                row["id"],
                exc_info=True,
            )
    logger.info("exercise_photos: перенесено фото из Telegram: %s из %s", migrated, len(rows))
    return migrated


def telegram_input(exercise):
    """Чем отправлять своё фото в чат: готовым file_id или файлом с диска.

    Порядок именно такой — ссылка дешевле заливки, и пока она жива, смысла
    поднимать файл с диска нет. Файл нужен ровно в двух случаях: фото
    приехало из приложения (file_id взяться неоткуда) или сменился токен бота
    и все старые ссылки умерли. None — фото нет вовсе.

    Импорт aiogram локальный: хранение фото само по себе про файлы и базу, и
    api_v1/db, которые зовут соседние функции этого модуля, не должны тянуть
    за собой клиент Telegram.
    """
    try:
        file_id = exercise["custom_photo_file_id"]
    except (IndexError, KeyError, TypeError):
        file_id = None
    if file_id:
        return file_id
    path = path_for_exercise(exercise)
    if path is None:
        return None
    from aiogram.types import FSInputFile

    return FSInputFile(path)


async def remember_sent_file_id(exercise, sent) -> None:
    """Запомнить file_id только что отправленного файла — чтобы следующая
    отправка снова была бесплатной ссылкой, а не повторной заливкой того же
    файла. Зовётся после каждой отправки своего фото и сама решает, есть ли
    что запоминать: при живой ссылке файл никто и не поднимал."""
    try:
        if exercise["custom_photo_file_id"]:
            return
    except (IndexError, KeyError, TypeError):
        pass
    photo = getattr(sent, "photo", None)
    if not photo:
        return
    import db

    await db.set_exercise_photo_file_id(exercise["id"], photo[-1].file_id)
