"""Drive an already-running Telegram Web session over CDP: open a chat, send a
message, tap a button, or just screenshot. Talks to the daemon started by
daemon.py on the given CDP port -- start that first.

Usage:
    python3 tg.py <cdp_port> open   <chat_name_substring>            [out.png]
    python3 tg.py <cdp_port> send   <text>              [wait_ms]    [out.png]
    python3 tg.py <cdp_port> click  <button_text>        [wait_ms]   [out.png]
    python3 tg.py <cdp_port> shot   [wait_ms]                        [out.png]

Every command force-scrolls to the bottom of the chat before acting and again
before the final screenshot -- see SKILL.md's "stale buttons" pitfall for why
this matters more than it sounds like it should.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

SCRATCH = os.path.dirname(os.path.abspath(__file__))


async def _force_scroll(pg):
    await pg.evaluate(
        "(() => { const s = document.querySelector('.bubbles .scrollable-y') "
        "|| document.querySelector('.scrollable-y'); "
        "if (s) s.scrollTop = s.scrollHeight + 100000; })()"
    )
    await pg.mouse.wheel(0, 5000)
    await pg.wait_for_timeout(400)


async def main():
    port = sys.argv[1]
    cmd = sys.argv[2]
    args = sys.argv[3:]
    out = os.path.join(SCRATCH, args[-1]) if args and args[-1].endswith(".png") else os.path.join(SCRATCH, "live.png")
    if args and args[-1].endswith(".png"):
        args = args[:-1]

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pg = b.contexts[0].pages[0]
        await _force_scroll(pg)

        if cmd == "open":
            await pg.locator(f'a.chatlist-chat:has-text("{args[0]}")').first.click()
            await pg.wait_for_timeout(2000)
        elif cmd == "send":
            text = args[0]
            wait_ms = int(args[1]) if len(args) > 1 else 5000
            # .chat-input has two overlapping elements: the real editable one
            # (data-peer-id) and a decoy that swallows clicks. Target the real one.
            box = pg.locator("div.input-message-input[data-peer-id]").first
            await box.click()
            await box.fill(text)
            await pg.keyboard.press("Enter")
            await pg.wait_for_timeout(wait_ms)
        elif cmd == "click":
            label = args[0]
            wait_ms = int(args[1]) if len(args) > 1 else 5000
            # Scope to message-area buttons only (not the left chat-list preview
            # text, which also matches has_text). `.last` picks the newest
            # match, but Telegram Web keeps the WHOLE scrollback in the DOM --
            # if `label` is a text-substring of an older button too (e.g. one
            # exercise name that's a prefix of another), `.last` can still grab
            # the wrong one. Prefer distinctive, non-overlapping label text, or
            # screenshot first to confirm exact wording before clicking.
            await pg.locator(
                "#column-center button.reply-markup-button", has_text=label
            ).last.click()
            await pg.wait_for_timeout(wait_ms)
        elif cmd == "shot":
            wait_ms = int(args[0]) if args else 0
            if wait_ms:
                await pg.wait_for_timeout(wait_ms)
        else:
            raise SystemExit(f"unknown command: {cmd}")

        await _force_scroll(pg)
        await pg.screenshot(path=out)
        print("URL:", pg.url)
        print("OUT:", out)
        await b.close()


asyncio.run(main())
