# AI Trainer Live Conversation Test Scenarios

## Test Session: AI Trainer Interaction Analysis

Based on code inspection of `/home/user/training_log_bot/handlers/ai_trainer.py` and `ai_trainer.py`, the following test scenarios would expose potential defects in AI trainer communication.

### Scenario 1: Program Draft Persistence After Setup Questions

**Setup**: User asks for a program. Trainer calls `ask_setup_questions` with 3 questions.

**Flow**:
1. User sees first setup question
2. User answers all 3 questions
3. System calls `_deliver_setup()` with setup_questions list
4. Trainer should build program based on answers

**Expected**: Final program proposal shows up with "Добавить себе" button

**Potential Defect**: Looking at line 2132-2136 in handlers/ai_trainer.py:
```python
await _deliver_setup(
    message, state, user_id,
    [] if program_draft else setup_questions,  # ← Only shows setup if NO program_draft
    goal=history_question,
)
```

If trainer generates BOTH a program AND questions in same response, the questions are suppressed. But if trainer generates questions without a program, then answers don't automatically trigger program generation - they just become history entries.

**To test via TG API**: Send "Составь программу для 3х дневной тренировки" and observe if setup questions appear or if program appears directly.

---

### Scenario 2: Rich Message Fallback on Incomplete Responses

**Setup**: User asks for a complex analysis (e.g., exercise form critique).

**Flow**:
1. Trainer starts generating response
2. Stream gets interrupted (network/timeout)
3. Partial answer is in `answer` variable
4. System checks `formatting.has_markdown_table(answer)`

**Expected**: Message sends as HTML with whatever text exists

**Potential Defect**: Looking at lines 2122-2126:
```python
sent_rich = formatting.has_markdown_table(answer) and await _send_rich_answer(
    message, placeholder, chunks, quota_md, quota_html, reply_markup
)
if not sent_rich:
    await _send_html_answer(message, placeholder, chunks, quota_html, reply_markup)
```

If stream breaks before table completes, `has_markdown_table()` might return True for a malformed table, causing `_send_rich_answer()` to fail and retry as HTML. But what if `_send_rich_answer()` partially succeeds?

**To test via TG API**: Send a query that normally returns a table, then kill the connection mid-stream to see how partial answers render.

---

### Scenario 3: Program Draft ID Collision in FSM State

**Setup**: User builds first program, state is cleared, user builds another.

**Flow**:
1. First "Add program" generates `program_draft["id"] = secrets.token_hex(4)` (line 2086)
2. Message edited but not deleted (fast user interaction)
3. state.clear() happens (e.g., after leaving menu)
4. Second program generated with new random ID

**Expected**: Each button is independent

**Potential Defect**: Looking at line 2086 - the ID is only generated if `program_draft` is truthy. But if a user:
1. Starts building program A
2. Navigates away (state.clear())
3. Comes back and gets the same program A still in draft

The old button will still have the old ID from before clear(). When clicked, it tries to load a program_draft that was cleared.

**To test via TG API**: 
1. Ask for program, get proposed draft with save button
2. Go to main menu and return to AI trainer
3. Click the old save button - does it error or does it save the program?

---

### Scenario 4: Voice Transcription with Trainer Recognition

**Setup**: User sends voice message

**Flow**:
1. Handler calls `_download_voice_as_file()`
2. Calls OpenAI Whisper API to transcribe
3. `ai_voice_question()` passes transcribed text as question

**Expected**: Trainer recognizes the transcribed text correctly

**Potential Defect**: Looking at lines 2647-2700 (voice handler), the voice is sent as OGG/Opus. If transcription returns empty string or garbage (common with background noise), trainer sees:
- Empty question → handler returns early (line 2489)
- Noise-as-text → trainer might interpret as setup question answer when actually it's garbage

Also, if Whisper times out, error text becomes the history.

**To test via TG API**: Send a voice message with background music/noise. Verify:
1. Transcription is accurate or degrades gracefully
2. Trainer doesn't confuse garbage transcription with real setup answers

---

### Scenario 5: Program Save Button Persistence vs State Clearing

**Setup**: Trainer proposes program with save button.

**Flow**:
1. User doesn't click save button immediately
2. User goes to Settings or Main Menu (triggers state.clear() for some FSMs)
3. User returns to AI trainer
4. Clicks the old save button

**Expected**: Old program draft is gone, button should error

**Potential Defect**: Looking at how `ai_program_draft` is stored in state:
- Line 2087: `await state.update_data(ai_program_draft=program_draft)`
- Line 1605: `ai:prog:save:` callback reads from state

If state.clear() is too aggressive, it might wipe `ai_program_draft`, but the button in chat still references that draft ID. The button will exist but have no matching draft to save.

**To test via TG API**:
1. Ask for program
2. See "Добавить себе" button
3. Go to main menu / settings / trigger state clear
4. Return to AI trainer
5. Click the save button - error message or successful save of stale program?

---

### Scenario 6: Consecutive Program Proposals (A8 behavior)

**Setup**: User asks for program, builds first one, then immediately asks for second.

**Flow**:
1. First program saved (or not)
2. User asks "А можно вариант посложнее?"
3. Trainer generates new program_draft
4. Line 2077: `if program_draft: program_draft["id"] = secrets.token_hex(4)`

**Expected**: New button replaces old one

**Potential Defect**: The code at line 2071-2076 says "черновик затирает старое" (draft replaces old), but what if:
- First program_draft is still being edited by user (button visible in chat)
- Second response comes in with new program_draft
- Old button now points to cleared state, new button is active

The UI shows both buttons but only one works.

**To test via TG API**: Ask for program, don't save it, then in next message ask "улучши эту программу". Check if both buttons appear or if first one is deactivated.

---

### Scenario 7: Exercise Name Auto-Detection in CSV Import + AI Trainer

**Setup**: User imports CSV with exercise names that need AI resolution.

**Flow**:
1. CSV import finds unknown exercise names
2. Calls AI trainer to suggest matching exercises
3. AI trainer returns matched names
4. User then chats with AI trainer about other topics

**Expected**: Trainer knows the exercises were already imported

**Potential Defect**: According to BUGS_LIVE_TESTING.md §23: "AI trainer promises save button but doesn't call propose_program". Additionally, the trainer might not have context that exercises were just imported. If they suggest creating a new exercise that's already in the catalog (from CSV), it's redundant.

**To test via TG API**:
1. Import CSV with exercises
2. Immediately ask trainer "Какие упражнения я импортировал?"
3. Check if trainer sees the newly imported exercises

---

### Scenario 8: Streaming Chunk Loss During Slow Delivery

**Setup**: Trainer's response contains multiple chunks.

**Flow**:
1. First chunk arrives and is shown in draft
2. Network slowness causes 10+ second gap
3. Remaining chunks arrive and are added

**Expected**: Final message is complete

**Potential Defect**: Looking at `_DraftStreamer` class (around line 1949), chunks are pushed via `on_chunk=streamer.push`. But if there's a long gap between chunks:
- Draft might time out and close
- New chunks coming after timeout might create a separate message
- Final answer gets split unexpectedly

**To test via TG API**: This would require intentionally throttling the network connection. Not practical via TG API alone.

---

## Defects Summary to Test

| ID | Title | Severity | How to Trigger | Expected vs Actual |
|----|-------|----------|-----------------|-------------------|
| D1 | Setup questions don't auto-trigger after answers | HIGH | Ask for program, answer setup Qs | Program should auto-build, but might stay waiting |
| D2 | Program draft ID collision after state.clear() | MEDIUM | Build program, go to menu, return, click save | Save works on old/cleared draft |
| D3 | Voice transcription garbage treated as setup answer | MEDIUM | Send noisy voice, trainer asks Q | Noise becomes setup answer |
| D4 | Multiple program buttons both clickable but one broken | MEDIUM | Ask for program, ask "улучши", click old button | Old button fails after new draft replaces it |
| D5 | Trainer doesn't know about CSV-imported exercises | HIGH | Import CSV then ask trainer | Trainer suggests duplicate exercises |
| D6 | Rich message partial render on stream interrupt | MEDIUM | Interrupt table-generating response | Table renders incomplete or reverts to HTML |

---

## Recommendations for Testing via Telegram Bot API

To properly test these scenarios, you would need:

1. **Bot Token & Test User ID**: Set up a dedicated test bot and account
2. **Environment Setup**: 
   ```bash
   export TG_TOKEN="your_test_bot_token"
   export TEST_USER_ID="your_telegram_id"
   python main.py
   ```
3. **Test Script**: Automate sending messages and capturing responses:
   ```python
   import aiohttp
   
   async def send_message(chat_id, text):
       async with aiohttp.ClientSession() as session:
           await session.post(
               f"https://api.telegram.org/bot{token}/sendMessage",
               json={"chat_id": chat_id, "text": text}
           )
   ```
4. **Screenshot Capture**: Use Telegram Web K automation (like the previous approach) to capture UI state between interactions

---

## Next Steps

Run these scenarios with actual Telegram API once bot token is available. The code inspection has identified these potential issues; live testing would confirm whether they actually occur in practice.
