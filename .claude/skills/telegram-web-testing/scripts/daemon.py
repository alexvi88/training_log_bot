"""Launch (or relaunch) a persistent, headless Chromium logged into Telegram Web,
exposing it over CDP on port 9222 so short-lived driver scripts (tg.py) can
attach to it instantly instead of paying login/proxy-negotiation cost every call.

Usage:
    python3 daemon.py <profile_dir> <telegram_url> [cdp_port]

Run this DETACHED (setsid ... &, disown) so it survives the launching shell
command exiting — see SKILL.md for the exact incantation and why a plain
background `&` is not enough in this harness.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PROFILE_DIR = sys.argv[1]
URL = sys.argv[2]
CDP_PORT = sys.argv[3] if len(sys.argv) > 3 else "9222"


async def main():
    # The proxy port is NOT stable across container/session restarts in this
    # harness -- always read it fresh from the live env var, never hardcode
    # or cache a port number from an earlier run.
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    proxy = {"server": "http://127.0.0.1:" + https_proxy.rsplit(":", 1)[-1]} if https_proxy else None

    lock = os.path.join(PROFILE_DIR, "SingletonLock")
    if os.path.exists(lock):
        os.remove(lock)

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=True,
            executable_path="/opt/pw-browsers/chromium",
            proxy=proxy,
            args=[
                "--disable-quic",
                "--ssl-version-max=tls1.2",
                f"--remote-debugging-port={CDP_PORT}",
            ],
        )
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto(URL, wait_until="load", timeout=30000)
        print("READY", flush=True)
        # Sleeps forever holding the browser process open; the caller kills
        # this process (or lets the container reclaim it) when done testing.
        await asyncio.sleep(3600 * 6)


asyncio.run(main())
