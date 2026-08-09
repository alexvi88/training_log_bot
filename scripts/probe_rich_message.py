"""Ручной пробник трёх кандидатов из Bot API 10.2: thinking-черновик, таблица,
фото-блок со свежим PNG. Не тесты — реальная отправка в чат с собой, чтобы
увидеть, как Telegram-клиент это рисует.

Запуск:
    TG_TOKEN=... TEST_USER_ID=... python scripts/probe_rich_message.py

Токен и id берутся из окружения — ничего не хардкожено и никуда, кроме
Telegram, не уходит. Если TEST_USER_ID не задан, шлём на ADMIN_ID.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InputMediaPhoto,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockTable,
    InputRichBlockThinking,
    InputRichMessage,
    RichBlockCaption,
    RichBlockTableCell,
)

import charts


def _table() -> InputRichBlockTable:
    def cell(text, align="left", is_header=False):
        return RichBlockTableCell(text=text, align=align, valign="middle", is_header=is_header)

    return InputRichBlockTable(
        cells=[
            [cell("Упражнение", is_header=True), cell("Лучший", "right", is_header=True),
             cell("Тоннаж", "right", is_header=True)],
            [cell("Присед"), cell("140×6", "right"), cell("4 200 кг", "right")],
            [cell("Жим"), cell("90×5", "right"), cell("1 800 кг", "right")],
        ],
        is_striped=True,
        is_bordered=True,
    )


def _chart_png() -> bytes:
    return charts.render_workout_card(
        "Присед", ["140×6", "130×8", "120×10"], "6 200 кг · 24 подхода", "Личный рекорд по весу"
    )


async def main() -> None:
    token = os.environ["TG_TOKEN"]
    chat_id = int(os.environ.get("TEST_USER_ID") or os.environ["ADMIN_ID"])

    bot = Bot(token=token)
    try:
        draft_id = 1
        print("1/3 отправляю thinking-черновик…")
        await bot.send_rich_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            rich_message=InputRichMessage(blocks=[InputRichBlockThinking(text="Смотрю на твою тягу…")]),
        )

        await asyncio.sleep(2.5)

        print("2/3 собираю таблицу + график…")
        photo_block = InputRichBlockPhoto(
            photo=InputMediaPhoto(media=BufferedInputFile(_chart_png(), filename="probe.png")),
            caption=RichBlockCaption(text="Присед за последние тренировки"),
        )

        print("3/3 финализирую rich-сообщение…")
        msg = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(
                blocks=[
                    InputRichBlockParagraph(text="Пробник rich-блоков: thinking → table → photo."),
                    _table(),
                    photo_block,
                ]
            ),
        )
        print("Готово, message_id =", msg.message_id)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
