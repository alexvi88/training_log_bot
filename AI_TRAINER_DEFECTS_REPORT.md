# AI Trainer Communication Defects & Improvements Report

**Analysis Date**: 2026-08-10  
**Testing Method**: Code inspection + handler flow analysis  
**Scope**: `/home/user/training_log_bot/handlers/ai_trainer.py` (2759 lines) + `/home/user/training_log_bot/ai_trainer.py` (5081 lines)

---

## Executive Summary

The AI trainer implementation is well-architected with several safeguards for concurrent operations and state consistency (e.g., program draft ID validation, atomicity in save operations). However, the communication flow has areas where user expectations might not align with actual behavior, particularly around:

1. **Setup questions** - conditions when they trigger vs. when program builds directly
2. **Program draft lifecycle** - what happens when state is cleared or messages are lost
3. **Streaming behavior** - how partial responses render
4. **Error messaging** - when and how failures are communicated to the user

---

## Confirmed Behaviors (Not Defects)

### ✅ A3: Program Draft ID Collision - FIXED

**Code Location**: Lines 1612-1622 in `ai_trainer.py`

The implementation correctly handles concurrent saves by:
1. Generating random IDs: `program_draft["id"] = secrets.token_hex(4)` (Line 2086)
2. Validating ID on load: `str(draft.get("id")) != draft_id` (Line 1208)
3. Atomic removal before save: `await state.update_data(ai_program_draft=None)` (Line 1622)

This prevents stale buttons from saving the wrong program.

### ✅ A1: Silent Program Creation on Duplicate Names - FIXED

The `_run_program_save()` function (around line 1450) asks for confirmation on name conflicts, not silently creating duplicates.

---

## Identified Defects & Edge Cases

### 🔴 D1: Setup Questions Don't Auto-Trigger After Answers (HIGH)

**Location**: `/handlers/ai_trainer.py:2497-2514` (ai_question handler) + `/handlers/ai_trainer.py:2128-2136` (_handle_question finalization)

**Scenario**:
1. User asks: "Составь программу для домашнего тренинга"
2. Trainer returns setup questions (via `on_questions` callback)
3. User answers questions one-by-one
4. After last answer, _record_setup_answer() → _finish_setup() calls _handle_question()
5. Trainer receives answers as `text: "Опыт: 5 лет\nЦель: гипертрофия\n..."`

**Expected**: Program is built from setup answers and user gets "Добавить себе" button

**Actual - Potential Issue**: Looking at line 2373 in `_finish_setup`:
```python
await _handle_question(target, state, text, history_question=text, user_id=user_id)
```

This sends the setup answers back to the model as a plain question. But the model doesn't have context that this is a follow-up to setup questions - it just sees:
```
Исходный запрос: Составь программу для домашнего тренинга
Ответы: <setup_answers_text>
```

**Issue**: If trainer doesn't recognize this as a continuation of program building, it might generate a text response instead of calling `propose_program`. The setup answers are treated as a regular Q&A, not as inputs to program generation.

**How to Verify via TG API**:
```
User: "Составь программу для 3х дневной тренировки"
Trainer: [Shows 3 setup questions]
User: [Answers all 3]
Expected: Program proposal with "Добавить себе" button
Risk: Trainer responds with advice text instead
```

**Fix Suggestion**: The prompt in `_finish_setup` should explicitly signal to the model that it's building a program from setup answers, not answering a new question.

---

### 🟡 D2: Program Draft Survives Across Different Conversation Topics (MEDIUM)

**Location**: `/handlers/ai_trainer.py:2071-2090` (Draft persistence logic)

**Scenario**:
1. User gets program proposal with "Добавить себе" button
2. User doesn't save, navigates to main menu
3. State might not be fully cleared (depending on FSM routing)
4. User returns to AI trainer and asks different question: "Как увеличить жим лёжа?"
5. Trainer answers with new program proposal
6. Both buttons now have live program_draft, but UI shows both old and new

**Actual Behavior**: Per lines 2071-2076:
```python
# Черновик один на пользователя: новое предложение затирает старое.
if program_draft:
    program_draft["id"] = secrets.token_hex(4)
    await state.update_data(ai_program_draft=program_draft)
else:
    await state.update_data(ai_history=history)
```

The logic says "новое предложение затирает старое" (new proposal replaces old), which is correct. However, this only applies to the FSM state - the old button message in Telegram chat still exists. If user clicks it:
- Button callback reads from state
- New program_draft is there (from second question)
- Button ID mismatch → "Это предложение уже неактуально" alert

**This is working as designed**, but the UX implication is:
- User sees multiple program proposal buttons in chat history
- Only the newest one is clickable
- Clicking old ones shows cryptic error message

**Fix Suggestion**: When a new program is proposed, could edit the previous message to remove the button, or add a note "This proposal has been replaced".

---

### 🟡 D3: Rich Message Fallback Behavior on Interrupted Streams (MEDIUM)

**Location**: `/handlers/ai_trainer.py:2114-2127` (Answer rendering logic)

**Scenario**:
1. Trainer is generating response with table (exercise list, progress chart)
2. Network timeout or stream interruption occurs after 1500 chars (partial table)
3. Code path: `sent_rich = formatting.has_markdown_table(answer) and await _send_rich_answer(...)`

**Issue**: The conditional short-circuits:
```python
sent_rich = formatting.has_markdown_table(answer) and await _send_rich_answer(...)
```

If `has_markdown_table()` returns True for an incomplete table (e.g., "| Header 1 |" without closing), it attempts rich rendering. If `_send_rich_answer()` fails midway, the fallback is triggered but the table structure is already broken.

**How to Verify**: Interrupt a table-generating request (e.g., "Покажи мой прогресс по жиму") by network throttling.

**Current Safeguard**: Lines 2024-2029 handle exceptions and retry with HTML, so users do get a response, just not richly formatted.

**This is acceptable behavior** - degradation to plain text is better than no message - but worth noting that partial tables might render poorly.

---

### 🟡 D4: Voice Transcription Ambiguity in Setup Responses (MEDIUM)

**Location**: `/handlers/ai_trainer.py:2647-2700` (ai_voice_question handler)

**Scenario**:
1. Setup question is active: "Сколько лет ты тренируешься?"
2. User sends voice message with background noise that transcribes as: "мм.. не знаю... может..?"
3. Whisper API returns: `"не знаю"`
4. Handler treats it as setup answer (per line 2510: `await _record_setup_answer(message, state, user_id, setup, question)`)

**Issue**: Noisy or ambiguous voice transcription can register as a setup answer without user confirmation. Unlike text input where user can proofread, voice goes straight to setup answers.

**How to Verify**: Send voice message with:
- Background music
- Unclear pronunciation
- Multiple speakers

Check if transcription is reasonable or if garbage gets recorded as an answer.

**Current State**: No verification loop for voice transcription accuracy. System assumes Whisper is 100% correct.

**Fix Suggestion**: For setup answers specifically, could show transcribed text with "✏️ Изменить" button before recording.

---

### 🟡 D5: CSV Import Exercise Names Not Visible to AI Trainer (MEDIUM)

**Location**: This is a cross-cutting concern between `/handlers/csv_import.py` and `/ai_trainer.py`

**Scenario**:
1. User imports CSV with 5 exercises via "Settings → Import CSV"
2. System matches exercise names via LLM (lines 188-220 in csv_import.py)
3. New exercises are added to user's catalog
4. User immediately chats with AI trainer: "Какие упражнения я только что импортировал?"

**Issue**: The trainer's system prompt (line 37-64 in ai_trainer.py) gives it access to:
- get_exercise_progress (recent exercises)
- get_full_workout_history (all past workouts)

But it does NOT have direct access to a list of "recently added exercises". The trainer must infer from workout history what was imported.

**How to Verify**:
1. Import CSV with new, uncommon exercises (e.g., "Тяга Т-грифа в машине")
2. Immediately ask: "Назови все мои упражнения"
3. Check if imported exercises appear in response

**Current Behavior**: Trainer will only know about imported exercises if they appear in recent workout history. If exercises were imported but not used yet in a workout, trainer won't know about them.

**Fix Suggestion**: Add a `get_recently_added_exercises()` tool to the trainer's arsenal, or include recent exercises in context on AI intro screen.

---

### 🔵 D6: Program Generation Without Setup Questions (LOWER PRIORITY)

**Location**: `/handlers/ai_trainer.py:2077-2090` & `/ai_trainer.py` (propose_program tool)

**Scenario**:
1. User: "Дай быструю программу на неделю"
2. Trainer: Directly generates program (without asking setup questions)
3. Program proposal shows with "Добавить себе" button

**Expected**: User can immediately save

**Actual**: Same as expected - this works correctly

**Note**: This is the opposite of D1 - when trainer skips questions and goes straight to proposal, the flow is clean. The issue is when questions are asked but then responses aren't properly recognized as program inputs.

---

## Architecture Observations

### Strengths

1. **Atomicity in saves** (Line 1622): Program draft is cleared before save starts, preventing double-saves
2. **ID-based button validation** (Line 1208): Prevents stale buttons from acting on wrong programs
3. **Round-trip history preservation** (Line 1992): Uses wire_cell to store exact model conversation for cache coherence
4. **Setup question recursion limit** (SETUP_MAX_ROUNDS): Prevents infinite loops in iterative program building

### Areas for Improvement

1. **Setup answer ambiguity**: Answers are plain text - no distinction between "skip this question" and "answer with silence"
2. **Program draft lifecycle**: Could be more explicit about when drafts are invalidated
3. **Error messages**: `_PROGRAM_GONE` is informative but only triggers on ID mismatch, not on other failure modes
4. **Streaming interruption handling**: Partial responses are sent but UX for "response was cut short" isn't explicit

---

## Recommendations

### Immediate (HIGH Priority)

1. **Verify setup → program build flow**: Test that trainer recognizes setup answers as program inputs, not standalone questions
2. **Document program draft lifecycle**: In CLAUDE.md or inline, clarify when `ai_program_draft` is cleared

### Short-term (MEDIUM Priority)

1. **Add voice transcription preview**: Show transcribed text before recording as setup answer
2. **Add CSV-imported exercises context**: Make trainer aware of freshly imported exercises
3. **Handle partial responses explicitly**: Add "(⏸ ответ был обрезан, но я записал, что есть)" on stream interruption

### Long-term (NICE-TO-HAVE)

1. **Setup answer confirmation UI**: Could show transcribed/entered text with edit button for setup questions
2. **Program draft expiration**: Could add timestamp and expire drafts after N minutes
3. **Exercise catalog sync**: Trainer could occasionally suggest importing popular exercises user hasn't tried

---

## Testing Checklist for Via-Telegram-API

To verify these findings on a live bot instance:

```
Setup Flow:
☐ Ask for program
☐ Verify setup questions appear (if trainer asks)
☐ Answer each question via text
☐ Verify final program proposal appears, not more questions
☐ Click "Добавить себе" and verify save

Program Draft Persistence:
☐ Get program proposal
☐ Don't save, navigate to main menu
☐ Return to AI trainer, ask different question
☐ Verify old program button now shows "уже неактуально"

Voice Input:
☐ When setup question active, send voice message
☐ Verify transcription is reasonable
☐ Check if text matches voice content

CSV Import Awareness:
☐ Import CSV with new exercises
☐ Ask trainer to list imported exercises
☐ Verify trainer knows what was just imported

Stream Interruption:
☐ Ask for response with table (progress/form critique)
☐ Interrupt mid-stream (pull network plug)
☐ Verify message still arrives (text or partial table)
```

---

## Conclusion

The AI trainer is architecturally sound with good safeguards for concurrency and state consistency. The defects identified are primarily in **communication clarity** and **edge case handling**, not in core logic failures. Most issues relate to user expectations about when setup questions trigger vs. when programs are generated directly, and how voice/streaming interruptions are handled.

All issues identified can be addressed through:
1. UX improvements (clearer messaging, confirmation flows)
2. Context awareness (trainer knowing about recent CSV imports)
3. Documentation (clarifying program draft lifecycle)

No critical data-loss or silent-failure bugs were found during this analysis.
