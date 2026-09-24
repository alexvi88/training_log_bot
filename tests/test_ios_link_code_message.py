"""/ios — код для приложения: строчный <code> и кнопка «Скопировать код»."""
from handlers import ios_link


def test_code_is_inline_not_pre_block():
    text = ios_link._ios_link_text("029277")
    assert "<code>029277</code>" in text
    assert "<pre>" not in text


def test_copy_button_copies_exact_code():
    kb = ios_link._copy_code_keyboard("029277")
    button = kb.inline_keyboard[0][0]
    assert button.copy_text.text == "029277"
