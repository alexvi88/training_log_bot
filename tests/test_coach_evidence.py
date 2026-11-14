"""Выжимки гайдов в системном промпте тренера (ai_trainer._evidence_block):
тренер отвечает о белке, подходах, отдыхе цифрами проверенных гайдов, а не
общими знаниями модели."""

import ai_trainer
import i18n
import seo_library


def test_every_training_guide_is_in_the_coach_prompt():
    prompt = ai_trainer._system_prompt()
    for slug in seo_library.GUIDES:
        title = i18n.t_in("ru", f"seo.guide.{slug}.h1", **seo_library.link_params("ru"))
        if slug in ai_trainer._EVIDENCE_SKIP:
            assert f"• {title}:" not in prompt
        else:
            assert f"• {title}:" in prompt, slug


def test_protein_base_and_reserve_reach_the_coach():
    block = ai_trainer._evidence_block()
    assert ai_trainer._EVIDENCE_HEAD in block
    assert "1.6" in block and "2.2" in block


def test_block_is_plain_text_and_stable():
    block = ai_trainer._evidence_block()
    assert "<" not in block and "{" not in block
    assert ai_trainer._system_prompt() == ai_trainer._system_prompt()
    # Язык ответа — хвостом в самом конце, после фактов (кэш общий у всех языков).
    assert ai_trainer._system_prompt().endswith(i18n.t("ai.language_tail"))
