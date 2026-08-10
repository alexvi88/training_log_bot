---
name: telegram-web-testing
description: Live-test this bot's actual behavior in a real Telegram client (Web K, via web.telegram.org/k/) using a headless, CDP-driven Chromium session, instead of trusting unit tests alone. Use this whenever asked to "test live", "check on prod", "test in Telegram", "test the bot", verify a bug fix actually shows up in production, or reproduce something reported from a screenshot -- especially for anything touching message rendering, rich messages, inline keyboards, or multi-step flows, since those have repeatedly diverged from what unit tests predict (rich-message photos/tables render differently -- or not at all -- across Telegram Web K, Web A, Desktop and mobile clients; unit tests mock the Bot API and can't catch that). Always use this instead of ad hoc curl/requests against the Bot API when the goal is "does a real person actually see the right thing."
---

# Telegram Web testing

Unit tests mock the Telegram client. They tell you the bot *sent* the right
API call, never whether a real client actually *rendered* it correctly. This
session's most expensive bugs were exactly that gap: a rich-message photo that
Telegram Web K silently swallowed (no error, no fallback -- just gone), and an
AI-trainer answer that vanished the same way. Neither was visible from the
send-side code or from mocked tests. The only way to catch them is to drive a
real client and look.

That's what this skill does: keep a real, logged-in Telegram Web session
running in a headless browser, and drive it like a person would -- open the
chat, type a message, tap a button, screenshot, read the screenshot.

## When this is (and isn't) the right tool

Reach for this when you need to know what a real human actually sees:
verifying a fix after deploy, reproducing a bug from a user's screenshot,
checking how a new inline-keyboard layout looks, confirming a rich
message/table/photo renders on the client you're worried about.

Don't reach for it to check business logic that pure unit tests already cover
well (e.g. "does the achievement threshold math work") -- that's slower and
noisier than just running pytest. Use it when the *rendering* or the *live
end-to-end path* is the actual question.

## One-time setup: a logged-in profile

This skill drives an *already authenticated* Telegram session -- it cannot log
in on its own (that needs a human to scan a QR code or approve a login once).
Before first use, check whether a persistent Chromium profile already exists
for this project (look for a directory with `SingletonLock`/`Default/` inside
it, commonly left in a scratch or session directory from a prior run). If one
exists and is still logged in, reuse it. If not, tell the user you need them
to complete a one-time QR login and walk them through it (launch
`daemon.py` pointed at a fresh profile dir, screenshot the QR code, wait for
them to scan it with their phone).

Pick a profile directory once per project/test-account and keep reusing it --
that's what makes every subsequent test run in this skill fast (no re-login,
no re-navigating).

## Running it

**1. Check if the daemon is already alive:**

```bash
curl -s http://127.0.0.1:9222/json/version
```

If that returns JSON, skip to step 3. If it errors or hangs, the browser died
(this happens often -- proxy sidecars restart, containers idle out) and needs
relaunching.

**2. Launch (or relaunch) the daemon, detached:**

```bash
PROFILE=/path/to/persistent/profile   # reuse the same one every time
setsid python3 scripts/daemon.py "$PROFILE" "https://web.telegram.org/k/#@your_bot_username" \
  > /tmp/tg-daemon.log 2>&1 < /dev/null &
disown
```

`setsid ... &` + `disown` matters: a plain background `&` gets killed the
moment the launching shell command's tool-call returns in this harness. Give
it a few seconds, then confirm with the `curl` check above. If `READY` never
appears in the log, check `/tmp/tg-daemon.log` for a TLS/proxy error --
`daemon.py` reads `HTTPS_PROXY` fresh from the environment for exactly this
reason (the port is not stable across restarts, never hardcode it).

**3. Drive it:**

```bash
python3 scripts/tg.py 9222 open  "Your Bot Display Name"          state.png
python3 scripts/tg.py 9222 send  "какой у меня тоннаж за неделю?" 15000 answer.png
python3 scripts/tg.py 9222 click "Меню"                            5000 menu.png
python3 scripts/tg.py 9222 shot  8000                              wait.png
```

Then **Read the screenshot** -- don't infer success from exit code or stdout,
actually look at the image before claiming something works or is broken.

## Pitfalls worth knowing before you hit them

- **The daemon dies mid-session, silently.** Before any multi-step sequence,
  `curl` the CDP port first. If it's dead, relaunch (step 2) and re-navigate
  before continuing -- don't assume a session that worked five minutes ago is
  still up.
- **Slow AI/agentic replies need real patience, not more retries.** A
  tool-calling AI-trainer answer can legitimately take 20-90 seconds. Use
  `shot` with a generous `wait_ms` (or call it again after waiting) rather
  than concluding "it's broken" after 5 seconds of silence -- but also don't
  wait forever blindly; if nothing shows after ~90s and no busy/error message
  appeared either, that itself might be the bug (see the vanished-message
  story above).
- **`.last` on a text-matched button can grab a stale one.** Telegram Web
  keeps the *entire* scrollback in the DOM, not just what's visible. If the
  button label you're clicking is a text-substring of another button
  somewhere earlier in the chat (two exercise names where one is a prefix of
  the other, e.g. "Тяга штанги в наклоне" vs "...обратным хватом"), a
  `has_text` filter can match the wrong one even with `.last`. Force-scroll to
  the bottom first (both driver scripts already do this), and when in doubt,
  screenshot before clicking to confirm the exact button text you're about to
  hit.
- **A screenshot that looks unchanged might mean the scroll didn't happen,
  not that nothing new arrived.** If a screenshot after an action looks
  identical to the one before it, don't conclude the action was a no-op --
  first re-check with an explicit scroll (`shot` re-scrolls every time; if
  it's still stuck, try clicking the chat in the left sidebar to force a
  fresh render).
- **Client matters.** `web.telegram.org/k/` (Web K) and `web.telegram.org/a/`
  (Web A) are different rendering engines and have diverged on rich-message
  support in this project's testing. If a client-rendering bug is in
  question, note *which* client you tested on in your findings, and consider
  checking more than one before concluding something is universally broken --
  a fix that "doesn't work" on K might work fine on A or Desktop, which
  changes the right fix (see the rich-message-photo story: it turned out to
  be a client-specific gap, not a universal one, and that changed the whole
  plan).
- **This drives a real account.** Treat it like a human tester: don't spam
  real users, don't send test traffic to anyone but the designated test
  account, and remember that actions here (creating exercises, logging
  workouts, sending AI-trainer questions) have real side effects -- real
  quota consumption, real LLM spend, real database rows -- exactly as if a
  person did them by hand.

## Reporting what you found

State plainly what you saw, not what you expected: "screenshot shows no photo
and squished text with no line breaks" beats "the rich message didn't render
correctly" -- the former is falsifiable by anyone looking at the same
screenshot, the latter is your interpretation already baked in. Send the
screenshot itself when it's the actual evidence for a bug, not just a
description of it.
