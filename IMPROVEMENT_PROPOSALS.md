# AI Trainer Improvement Proposals

Based on live testing analysis of communication flow, defect patterns, and UX expectations.

---

## Proposal 1: Explicit Setup → Program Context Window

**Problem**: When setup questions are answered, the system sends answers back as plain text. Trainer doesn't have explicit signal that this is a program-building context, might respond with advice instead.

**Current Code** (ai_trainer.py:2373):
```python
async def _finish_setup(target, state, user_id, setup):
    text = _setup_answers_text(setup)  # "Опыт: ...\nЦель: ..."
    await _handle_question(target, state, text, history_question=text, user_id=user_id)
```

**Proposed Change**: Wrap setup answers with explicit signal:

```python
async def _finish_setup(target, state, user_id, setup):
    text = _setup_answers_text(setup)
    
    # CHANGE: Add explicit context about program building
    history_question = f"""Вот ответы на вопросы для программы:
{text}

Собери программу на основе этих ответов."""
    
    # history_question goes to model; original text stays for DB logging
    await _handle_question(
        target, state, text, 
        history_question=history_question,  # ← explicit signal
        user_id=user_id
    )
```

**Why**: The model will see "Собери программу" (Build a program) as explicit instruction, not just context. This increases likelihood it calls `propose_program` tool.

**Risk**: Small - just makes instruction more explicit. System prompt already trains model to build programs from setup answers.

---

## Proposal 2: Program Draft Lifecycle Documentation

**Problem**: Users don't know when `ai_program_draft` is cleared from state, leading to confusion about stale buttons.

**Current State**: Draft is cleared when:
1. Program is saved (line 1622)
2. New program is generated (line 2077)
3. User navigates away and state.clear() is called (FSM-dependent)

**Proposed Changes**:

### 2A: Document in CLAUDE.md
Add to project instructions:
```markdown
## AI Trainer Program Draft Lifecycle

A program draft is stored in FSM state (`ai_program_draft`) while the user
decides whether to save it. Drafts are cleared when:

1. User clicks "✅ Добавить себе" — program is saved to catalog
2. New program proposal arrives from trainer — old draft is replaced
3. User navigates out of AI trainer context — state is cleared by FSM

Buttons in chat remain clickable but show "Это предложение уже неактуально"
if the draft they reference no longer exists in state. This is expected behavior.

To troubleshoot: If a save button fails, it means the draft was cleared since
the message was posted. Ask the trainer to build a new program.
```

### 2B: Enhance error message
Current (line 912-915): Generic message
```python
_PROGRAM_GONE = (
    "Это предложение уже неактуально. Если ты его сохранял — программа уже в "
    "«🗂 Программы»; если нет — попроси тренера собрать заново."
)
```

Proposed: Add context about why it expired:
```python
async def _program_draft(...):
    data = await state.get_data()
    draft = data.get("ai_program_draft")
    
    if not draft or not draft.get("days"):
        # New message: hint about what happened
        why = ""
        if data.get("ai_program_draft_deleted_reason"):
            why = f"\n\n⚠️ {data['ai_program_draft_deleted_reason']}"
        
        await callback.answer(_PROGRAM_GONE + why, show_alert=True)
        return None
```

**Impact**: Users understand why buttons stop working, reducing support questions.

---

## Proposal 3: Voice Transcription Feedback Loop

**Problem**: Voice → Whisper → Setup answer, no chance to correct misheard text.

**Current Code** (handlers/ai_trainer.py:2647-2710):
```python
async def ai_voice_question(message: Message, state: FSMContext):
    # Voice → transcribe → if setup active, record as answer
    question = await _transcribe(message.voice)
    setup = _active_setup(await state.get_data())
    if setup is not None:
        await _record_setup_answer(message, state, user_id, setup, question)
```

**Proposed Change**: Show transcription with edit option for setup questions:

```python
async def ai_voice_question(message: Message, state: FSMContext):
    question = await _transcribe(message.voice)
    setup = _active_setup(await state.get_data())
    
    if setup is not None:
        # FOR SETUP: show transcribed text, let user confirm
        keyboard = InlineKeyboardBuilder()
        keyboard.button(text="✅ Верно", callback_data=f"ai:voiceok:{setup['idx']}")
        keyboard.button(text="✏️ Изменить", callback_data=f"ai:voiceedit:{setup['idx']}")
        
        await message.reply(
            f"Услышал:\n\n<i>{escape(question)}</i>",
            reply_markup=keyboard.as_markup()
        )
        return  # Wait for confirmation
    
    # FOR NON-SETUP: record immediately (existing behavior)
    await _handle_question(message, state, question, history_question=question)
```

Add handlers:
```python
@router.callback_query(F.data.startswith("ai:voiceok:"))
async def ai_voice_confirm(callback: CallbackQuery, state: FSMContext):
    # User said "Верно" — record the transcription
    data = await state.get_data()
    setup = _active_setup(data)
    transcribed_text = ...  # retrieve from somewhere
    await _record_setup_answer(..., setup, transcribed_text)
    await callback.answer()

@router.callback_query(F.data.startswith("ai:voiceedit:"))
async def ai_voice_edit(callback: CallbackQuery, state: FSMContext):
    # User wants to edit manually
    await callback.answer()
    await callback.message.reply("Напиши, что ты имел в виду:")
    # Next text message becomes setup answer
```

**Impact**: Prevents garbage transcriptions from being recorded as setup answers. UX: "Услышал X. Верно? [✅ Верно] [✏️ Изменить]"

**Implementation Cost**: Medium - adds two callback handlers and state tracking for transcription.

---

## Proposal 4: CSV Import Context in AI Trainer

**Problem**: Trainer doesn't know what exercises were just imported; user must manually reference them.

**Current System**: Trainer has tools:
- `get_exercise_progress(exercise_name)` - requires exact name
- `get_full_workout_history()` - all history

No "recent additions" view.

**Proposed Solution**: Add context on AI intro and create tool:

### 4A: Enhance AI intro screen
```python
async def menu_ai(...):
    # Current: Shows intro text + preset questions
    
    # NEW: Also check for recent exercise additions
    recent_exercises = await db.get_recently_added_exercises(user_id, days=7)
    if recent_exercises:
        intro_text += f"""

🆕 **Недавно добавлены**: {', '.join(recent_exercises[:5])}
"""
    
    await callback.message.edit_text(intro_text, reply_markup=keyboard)
```

### 4B: Create trainer tool
Add to `ai_trainer.py` tool registry:
```python
async def get_recently_added_exercises(
    user_id: int, days: int = 7
) -> dict:
    """List of exercises added in the last N days (from CSV import or manual creation).
    
    Returns:
        {"exercises": [{"name": "...", "added": "2026-08-10", "use_count": 0}]}
    """
    return await training_log.get_recently_added_exercises(user_id, days)
```

Update system prompt:
```python
SYSTEM_PROMPT = """
...
Available tools:
- get_training_overview() - current fitness data
- get_exercise_progress(...) - performance on specific exercise
- get_recently_added_exercises() - exercises added via CSV import (new in this version)
...
"""
```

**Impact**: Trainer immediately aware of imports. "О! Я вижу, что ты добавил Тягу Т-грифа. Это отличное упражнение для спины, добавим его в программу?"

**Implementation Cost**: Low-medium. Requires:
1. DB query for recently added exercises
2. One new trainer tool
3. Prompt update

---

## Proposal 5: Partial Response Indicator

**Problem**: If stream is interrupted, user doesn't know response is incomplete.

**Current Code** (handlers/ai_trainer.py:2114-2127):
```python
# Message gets sent as HTML or Rich, but no indicator if it was cut short
await _send_html_answer(message, placeholder, chunks, quota_html, reply_markup)
```

**Proposed Change**: Add completion status:

```python
async def _handle_question(...):
    # ... stream collection ...
    
    # NEW: Track if stream completed
    stream_complete = True
    try:
        # Existing collection loop
    except TelegramRetryAfter as e:
        stream_complete = False
        logger.warning(f"Stream interrupted for user {user_id} after {len(chunks)} chunks")
    
    # ... prepare answer ...
    
    # NEW: Add indicator if incomplete
    if not stream_complete:
        answer += "\n\n⏸ _Ответ был обрезан из-за сетевой задержки. Записал, что есть._"
    
    chunks = formatting.split_for_telegram(answer, TG_CHUNK)
    await _send_html_answer(...)
```

**Impact**: Users know whether they got the full response. Manages expectations about incomplete advice.

**Implementation Cost**: Low - just adding a flag and conditional note.

---

## Proposal 6: Program Draft Expiration

**Problem**: Old program buttons stay clickable indefinitely, creating clutter and confusion.

**Proposed**: Add timestamp-based expiration:

```python
# When program draft is created (line 2086)
program_draft["id"] = secrets.token_hex(4)
program_draft["created_at"] = datetime.now().isoformat()  # NEW

# When draft is loaded (line 1198-1211)
async def _program_draft(callback, state, draft_id):
    draft = data.get("ai_program_draft")
    
    # NEW: Check expiration
    if draft:
        created = datetime.fromisoformat(draft.get("created_at") or "")
        age = datetime.now() - created
        if age > timedelta(hours=1):  # Draft older than 1 hour
            await callback.answer(
                "Это предложение уже час как на столе. Попроси тренера собрать новое.",
                show_alert=True
            )
            await state.update_data(ai_program_draft=None)
            return None
    
    # Existing checks...
```

**Impact**: Old buttons become stale and clearly unusable. Encourages user to ask for fresh program.

**Tradeoff**: User who's been thinking about a program for 2 hours can't save it anymore.

**Mitigation**: Add "Сохранить эту программу даже если она старая?" option.

---

## Summary Table: Priority & Effort

| Proposal | Impact | Effort | Priority |
|----------|--------|--------|----------|
| 1. Setup context window | HIGH - fixes D1 | Low | 🔴 HIGH |
| 2. Draft lifecycle docs | MEDIUM - UX clarity | Low | 🟡 MEDIUM |
| 3. Voice transcription feedback | MEDIUM - prevents D4 | Medium | 🟡 MEDIUM |
| 4. CSV import context | MEDIUM - fixes D5 | Low-Medium | 🟡 MEDIUM |
| 5. Partial response indicator | LOW - UX polish | Low | 🟢 LOW |
| 6. Draft expiration | LOW - reduces clutter | Medium | 🟢 LOW |

**Recommended Sequence**:
1. **First**: Proposal 1 (Setup context) - solves core D1 issue
2. **Then**: Proposal 4 (CSV context) - improves trainer awareness
3. **Then**: Proposal 2 (Documentation) - prevents confusion
4. **Then**: Proposal 3 (Voice feedback) - UX improvement
5. **Optional**: 5 & 6 for polish

---

## Testing These Changes

For each proposal, test via Telegram API with:

```
P1 - Setup context:
  User: "Собери программу"
  [Answer setup questions]
  Expected: Program proposal (not advice text)
  Verify: "Добавить себе" button appears

P4 - CSV context:
  [Import CSV with new exercises]
  User: "Какие упражнения я импортировал?"
  Expected: Trainer lists recent additions
  Verify: Newly imported exercises appear in response

P3 - Voice feedback:
  [Setup active]
  [Send noisy voice]
  Expected: Transcription preview + [✅ Верно] [✏️ Изменить]
  Verify: User can correct transcription

P5 - Partial response:
  [Interrupt stream mid-response]
  Expected: Final message includes "⏸ Ответ был обрезан"
  Verify: User knows response was incomplete
```

---

## Implementation Notes

- **Proposals 1-2**: Minimal risk, can be deployed immediately
- **Proposal 3**: Requires new FSM state for voice handling
- **Proposal 4**: Requires new DB queries, small trainer tool
- **Proposals 5-6**: Polish, can be deferred to future

All proposals maintain backward compatibility and don't require schema changes.
