"""Оформление публичного сайта (seo_pages.py): стили и значки.

Одна таблица стилей прямо в `<style>` — без фреймворков и внешних запросов:
страница рисуется с первого байта, без прыжков вёрстки. Палитра — подвал
тренера: тёмно-коричневый фон, янтарный свет лампы, раскалённый оранжевый
акцент; карточки — полупрозрачное «стекло» с тонкой рамкой. Заголовки —
системный гротеск, жирный и плотный; пиксельный шрифт (Basement Pixel,
seo_static) — только для мелких меток и больших чисел калькуляторов.

Анимации — только CSS и только при `prefers-reduced-motion: no-preference`:
появление блоков при прокрутке (`animation-timeline: view()`, где браузер
его знает; где нет — блоки просто видны), набор «100 8» в демо подхода.

Значки — простые inline-SVG в одном стиле (контур 1.8, скругления), вместо
эмодзи в карточках.
"""

from __future__ import annotations

CSS = """
@font-face { font-family: "Basement Pixel"; src: url(__PIXEL2__) format("woff2"), url(__PIXEL__) format("woff"); font-display: swap; }
:root { color-scheme: dark;
  --bg:#0f0907; --bg2:#170e0b; --fg:#f7ede4; --muted:#c4a894; --dim:#9c8270;
  --accent:#ff8a3d; --accent2:#ffb35c; --ember:linear-gradient(135deg,#ffb35c 0%,#ff6a2b 100%);
  --glass:rgba(255,236,220,.045); --glass2:rgba(255,236,220,.075); --line:rgba(255,214,186,.12); --line2:rgba(255,214,186,.24);
  --ok:#7bd88f; --warn:#ffcf70; --r:22px; --r-s:14px;
  --display: ui-sans-serif, -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --pixel: "Basement Pixel", ui-monospace, Menlo, monospace;
  --s1:4px; --s2:8px; --s3:12px; --s4:16px; --s5:24px; --s6:32px; --s7:48px; --s8:72px; --s9:104px; }
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body { margin: 0; color: var(--fg); background: var(--bg); overflow-x: hidden;
  font: 17px/1.6 var(--display); -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }
body::before { content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
  background: radial-gradient(1100px 620px at 78% -8%, rgba(255,106,43,.22), transparent 60%),
              radial-gradient(900px 600px at -10% 10%, rgba(140,52,24,.25), transparent 60%),
              linear-gradient(180deg, #140c09 0%, var(--bg) 40%); }
img { max-width: 100%; height: auto; }
[hidden] { display: none !important; }
a { color: var(--accent2); text-underline-offset: 3px; }
a:hover { color: #ffd0a0; }
:focus-visible { outline: 2px solid var(--accent2); outline-offset: 3px; }
.wrap { width: 100%; max-width: 1140px; margin: 0 auto; padding: 0 var(--s4); }
@media (min-width: 720px) { .wrap { padding: 0 var(--s5); } }
.prose { max-width: 760px; }
.skip { position: absolute; left: -9999px; }
.skip:focus { left: var(--s4); top: var(--s2); z-index: 50; background: var(--bg2); padding: var(--s2) var(--s3); }

/* header */
.site-head { position: sticky; top: 0; z-index: 40; backdrop-filter: saturate(160%) blur(16px); -webkit-backdrop-filter: saturate(160%) blur(16px);
  background: rgba(15,9,7,.72); border-bottom: 1px solid var(--line); overflow-x: clip; }
.head-row { display: flex; align-items: center; gap: var(--s4); height: 60px; }
.brand { display: inline-flex; align-items: center; gap: 10px; color: var(--fg); text-decoration: none; flex: none; }
.brand img { width: 30px; height: 30px; border-radius: 8px; }
.brand span { font: 10px/1 var(--pixel); letter-spacing: .04em; }
.head-nav { display: none; gap: 2px; margin-left: var(--s3); }
.head-nav a { color: var(--muted); text-decoration: none; font-size: 15px; padding: 6px 10px; border-radius: 10px; }
.head-nav a:hover, .head-nav a[aria-current] { color: var(--fg); background: var(--glass2); }
.head-end { margin-left: auto; display: flex; align-items: center; gap: var(--s2); }
.lang { display: inline-grid; place-items: center; min-width: 44px; min-height: 44px; color: var(--muted); text-decoration: none; font: 11px/1 var(--pixel); padding: 0 10px; border: 1px solid var(--line2); border-radius: 10px; }
.lang:hover { color: var(--fg); border-color: var(--accent); }
.head-cta { display: none; white-space: nowrap; }
@media (min-width: 960px) { .head-nav { display: flex; } .head-cta { display: inline-flex; } }
.sub-nav { display: flex; gap: 6px; overflow-x: auto; max-width: 100vw; scrollbar-width: none; padding: 0 0 10px; margin: 0 calc(-1 * var(--s4)); padding-left: var(--s4); padding-right: var(--s4); }
.sub-nav::-webkit-scrollbar { display: none; }
.sub-nav a { flex: none; display: inline-flex; align-items: center; min-height: 44px; color: var(--muted); text-decoration: none; font-size: 14px; padding: 0 14px; border: 1px solid var(--line); border-radius: 999px; background: var(--glass); }
.sub-nav { -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 16px, #000 calc(100% - 28px), transparent 100%); mask-image: linear-gradient(90deg, transparent 0, #000 16px, #000 calc(100% - 28px), transparent 100%); }
.sub-nav a[aria-current] { color: var(--fg); border-color: var(--line2); }
@media (min-width: 960px) { .sub-nav { display: none; } }

/* type */
main { padding: var(--s6) 0 var(--s8); }
h1, h2, h3 { font-family: var(--display); font-weight: 800; letter-spacing: -.035em; line-height: 1.06; margin: 0; overflow-wrap: anywhere; }
h1 { font-size: clamp(36px, 6.4vw, 68px); margin: var(--s3) 0 var(--s4); }
.page h1 { font-size: clamp(32px, 5vw, 52px); }
h2 { font-size: clamp(27px, 3.6vw, 40px); margin: 0 0 var(--s4); }
h3 { font-size: 20px; letter-spacing: -.02em; line-height: 1.2; margin: 0 0 var(--s2); }
.prose h2 { font-size: clamp(24px, 3vw, 30px); margin-top: var(--s7); }
.sub > h2, .mistakes > h2 { font-size: clamp(24px, 3vw, 30px); }
.eyebrow + h2, .prose .eyebrow + h2 { margin-top: 10px; }
p, li { overflow-wrap: anywhere; }
p { margin: 0 0 var(--s4); }
.lead { font-size: clamp(18px, 2.1vw, 21px); line-height: 1.55; color: #f1dfd0; max-width: 680px; }
.muted { color: var(--muted); font-size: 15px; }
.eyebrow { display: inline-flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; font: 11px/1.4 var(--pixel); letter-spacing: .06em; color: var(--accent2); text-transform: uppercase; }
.eyebrow::before { content: ""; flex: none; align-self: center; width: 8px; height: 8px; border-radius: 2px; background: var(--ember); box-shadow: 0 0 12px rgba(255,138,61,.8); }
.grad { background: var(--ember); -webkit-background-clip: text; background-clip: text; color: transparent; }
ul, ol { padding-left: 22px; }
li { margin: 6px 0; }
code { font: 15px ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--glass2); border: 1px solid var(--line); border-radius: 7px; padding: 1px 7px; }
.crumbs { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; padding: 0; margin: 0 0 var(--s3); font-size: 14px; color: var(--dim); }
.crumbs li { margin: 0; }
.crumbs li + li::before { content: "/"; margin-right: 6px; color: var(--dim); }
.crumbs a { color: var(--muted); text-decoration: none; }
.crumbs a:hover { color: var(--fg); }
section { margin-top: var(--s9); }
.page section { margin-top: var(--s7); }
.sec-head { max-width: 720px; margin-bottom: var(--s6); }
.sec-head p { color: var(--muted); font-size: 18px; }

/* buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 10px; min-height: 52px; padding: 0 22px;
  font: 650 16px/1.25 var(--display); letter-spacing: -.01em; text-decoration: none; cursor: pointer; text-align: center; padding-top: 10px; padding-bottom: 10px;
  color: var(--fg); background: var(--glass2); border: 1px solid var(--line2); border-radius: 14px;
  transition: transform .15s ease, background .2s ease, border-color .2s ease, box-shadow .2s ease; }
.btn:hover { color: var(--fg); background: rgba(255,236,220,.11); border-color: rgba(255,214,186,.38); transform: translateY(-1px); }
.btn svg { width: 20px; height: 20px; flex: none; }
.btn.primary { color: #1b0d06; background: var(--ember); border-color: transparent; box-shadow: 0 10px 30px -10px rgba(255,106,43,.75), inset 0 1px 0 rgba(255,255,255,.35); }
.btn.primary:hover { color: #1b0d06; box-shadow: 0 14px 36px -10px rgba(255,106,43,.9), inset 0 1px 0 rgba(255,255,255,.4); }
.btn.small { min-height: 38px; padding: 0 14px; font-size: 14px; border-radius: 11px; }
.btn-row { display: flex; flex-wrap: wrap; gap: var(--s3); align-items: center; }
@media (max-width: 520px) { .btn-row .btn { width: 100%; } }
.alt-link { font-size: 15px; color: var(--muted); margin-top: 12px; }
.alt-link a { color: var(--fg); }

/* CTA */
.cta { position: relative; overflow: hidden; margin: var(--s7) 0; padding: var(--s6) var(--s5); border-radius: 28px;
  background: linear-gradient(140deg, rgba(255,138,61,.16), rgba(255,236,220,.04) 55%), var(--bg2); border: 1px solid var(--line2); }
.cta::after { content: ""; position: absolute; width: 380px; height: 380px; right: -120px; top: -160px; border-radius: 50%;
  background: radial-gradient(circle, rgba(255,138,61,.35), transparent 65%); pointer-events: none; }
.cta > * { position: relative; }
.cta-text { font-size: 20px; font-weight: 700; letter-spacing: -.02em; margin-bottom: var(--s4); max-width: 620px; }
.cta-body { display: grid; gap: var(--s5); align-items: center; }
@media (min-width: 760px) { .cta-body.has-qr { grid-template-columns: auto 1fr; } }
.qr { display: grid; grid-template-columns: 132px; gap: 8px; justify-items: center; text-align: center; }
.qr svg { width: 132px; height: 132px; border-radius: 14px; background: #fff; padding: 6px; box-shadow: 0 12px 40px -12px rgba(0,0,0,.6); }
.qr small { font-size: 13px; color: var(--muted); line-height: 1.3; }

/* cards */
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr)); gap: var(--s3); margin: var(--s4) 0; }
@media (min-width: 720px) { .grid.g2 { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (min-width: 900px) { .grid.g3 { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.card .badge { display: inline-block; margin-bottom: 10px; padding: 3px 9px; border-radius: 8px; font: 600 12px/1.6 var(--display); font-style: normal; color: var(--accent2); background: rgba(255,138,61,.12); border: 1px solid rgba(255,138,61,.3); }
.card b { padding-right: 28px; }
.card { position: relative; display: block; padding: var(--s5); background: var(--glass); border: 1px solid var(--line); border-radius: var(--r);
  color: var(--fg); text-decoration: none; transition: border-color .2s ease, background .2s ease, transform .2s ease; }
a.card:hover { color: var(--fg); border-color: var(--line2); background: var(--glass2); transform: translateY(-2px); }
.card b { display: block; font-size: 18px; letter-spacing: -.015em; line-height: 1.25; margin-bottom: 6px; }
.card span { display: block; color: var(--muted); font-size: 15px; line-height: 1.5; }
.card .ic { display: inline-grid; place-items: center; width: 42px; height: 42px; margin-bottom: var(--s4); border-radius: 12px;
  color: var(--accent2); background: rgba(255,138,61,.12); border: 1px solid rgba(255,138,61,.28); }
.card .ic svg { width: 22px; height: 22px; }
.card .arrow { position: absolute; right: 20px; top: 22px; color: var(--dim); transition: transform .2s ease, color .2s ease; }
a.card:hover .arrow { color: var(--accent2); transform: translateX(3px); }
.thumb-card { padding: var(--s3); }
.thumb-card picture { display: block; margin-bottom: var(--s3); }
.thumb-card img { display: block; width: 100%; height: auto; aspect-ratio: 4/3; object-fit: cover; object-position: center 60%; border-radius: 12px; background: #2a1810;
  box-shadow: inset 0 0 0 1px rgba(255,214,186,.14); }
.ex-grid { grid-template-columns: repeat(auto-fill, minmax(min(100%, 220px), 1fr)); }
@media (max-width: 600px) { .ex-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s2); }
  .ex-grid .thumb-card { padding: 8px; } .ex-grid .thumb-card b { font-size: 15px; padding: 0 4px 4px; } }
.thumb-card b, .thumb-card span { padding: 0 6px; }
.thumb-card span { margin-bottom: 4px; }
.group-title { font-size: 22px; margin: var(--s7) 0 var(--s2); }

/* hero */
.hero { display: grid; gap: var(--s6); align-items: center; padding-top: var(--s4); }
.hero-cta { margin-top: var(--s5); }
@media (min-width: 940px) { .hero { grid-template-columns: 1.08fr .92fr; gap: var(--s6); padding-top: var(--s6); } }
.hero h1 { font-size: clamp(42px, 7.2vw, 80px); line-height: .98; letter-spacing: -.045em; }
.trust { display: flex; flex-wrap: wrap; gap: 8px 18px; margin-top: var(--s5); padding: 0; list-style: none; color: var(--muted); font-size: 15px; }
.trust li { display: inline-flex; align-items: center; gap: 8px; margin: 0; }
.trust svg { width: 18px; height: 18px; color: var(--ok); }
.stage { position: relative; min-height: 600px; }
.stage .coach { position: absolute; width: min(72%, 380px); aspect-ratio: 1; right: 0; top: 0; border-radius: 28px; overflow: hidden;
  border: 1px solid var(--line2); box-shadow: 0 40px 90px -30px rgba(0,0,0,.8); transform: rotate(4deg); }
.stage .coach picture, .stage .coach img { width: 100%; height: 100%; object-fit: cover; display: block; }
.stage .glow { position: absolute; left: 10%; top: 10%; width: 80%; aspect-ratio: 1; border-radius: 50%; background: radial-gradient(circle, rgba(255,106,43,.45), transparent 62%); filter: blur(10px); }
.phone { position: relative; width: 252px; aspect-ratio: 9/19.5; border-radius: 46px; padding: 9px; background: linear-gradient(160deg,#3a2a24,#120c0a);
  box-shadow: 0 0 0 1px rgba(255,214,186,.18), 0 50px 100px -30px rgba(0,0,0,.85), 0 30px 60px -40px rgba(255,106,43,.5); }
.phone::before { content: ""; position: absolute; top: 18px; left: 50%; width: 74px; height: 22px; margin-left: -37px; border-radius: 14px; background: #050302; z-index: 2; }
.phone picture { display: block; width: 100%; height: 100%; }
.phone img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; border-radius: 38px; background: #2a1d17; }
/* the phone sits bottom-left, clear of the coach's face (upper middle of his square) */
.stage .phone { position: absolute; left: 0; bottom: 0; width: 236px; transform: rotate(-3deg); }
@media (max-width: 939px) { .stage { min-height: 560px; max-width: 460px; margin: 0 auto; width: 100%; } }
@media (max-width: 939px) { .stage { min-height: 470px; } .stage .phone { width: 220px; margin-right: 28%; } .stage .coach { width: 62%; } }
.chip { position: absolute; display: inline-flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: 14px; font-size: 14px; font-weight: 650;
  background: rgba(23,14,11,.82); border: 1px solid var(--line2); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); box-shadow: 0 20px 40px -20px rgba(0,0,0,.8); }
.chip b { font: 12px/1 var(--pixel); color: var(--accent2); }
/* c1 sits over the empty middle of the screenshot, not over the input row */
.chip.c1 { left: 0; top: 60%; }
.chip.c2 { right: 2%; bottom: 6%; }

/* bento */
.bento { display: grid; gap: var(--s3); grid-template-columns: 1fr; }
@media (min-width: 760px) { .bento { grid-template-columns: repeat(6, 1fr); }
  .bento .b-wide { grid-column: span 4; } .bento .b-tall { grid-column: span 2; grid-row: span 2; }
  .bento .b-third { grid-column: span 2; } .bento .b-half { grid-column: span 3; } }
.tile { position: relative; overflow: hidden; padding: var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
.tile h3 { font-size: 22px; }
.tile p { color: var(--muted); font-size: 16px; margin: 0; }
.tile .ic { display: inline-grid; place-items: center; width: 42px; height: 42px; margin-bottom: var(--s4); border-radius: 12px; color: var(--accent2); background: rgba(255,138,61,.12); border: 1px solid rgba(255,138,61,.28); }
.tile .ic svg { width: 22px; height: 22px; }
.tile.hot { background: linear-gradient(160deg, rgba(255,138,61,.14), rgba(255,236,220,.03) 60%); border-color: var(--line2); }
.tile a.more { display: inline-flex; align-items: center; min-height: 44px; margin-top: var(--s2); font-weight: 600; text-decoration: none; }

/* set-in-one-line demo */
.demo { margin-top: var(--s5); padding: var(--s4); border-radius: 18px; background: #f7f0ea; color: #2a1d17; box-shadow: 0 30px 60px -30px rgba(0,0,0,.8); }
/* set-in-one-line tile: text left, demo right on wide screens */
@media (min-width: 960px) { .tile.b-wide { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); grid-template-rows: auto auto auto 1fr; column-gap: var(--s6); align-items: start; }
  .tile.b-wide > .demo { grid-column: 2; grid-row: 1 / span 4; margin-top: 0; align-self: center; } }
.demo .ex { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; color: #6b3d2c; }
.demo .ex::before { content: ""; border-left: 7px solid #e08a2e; border-top: 5px solid transparent; border-bottom: 5px solid transparent; }
.demo .rows { margin: 10px 0; display: grid; gap: 4px; font: 600 16px/1.5 ui-monospace, Menlo, monospace; }
.demo .rows div { display: flex; justify-content: space-between; padding: 2px 4px; border-bottom: 1px dashed rgba(42,29,23,.12); }
.demo .new { color: #a14a16; }
.demo .input { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.demo .field { flex: 1; min-width: 0; height: 46px; display: flex; align-items: center; padding: 0 14px; border-radius: 12px; background: #ede3da; font: 600 18px ui-monospace, Menlo, monospace; overflow: hidden; }
.demo .typed { display: inline-block; overflow: hidden; white-space: nowrap; width: 5ch; border-right: 2px solid #e08a2e; }
.demo .send { flex: none; width: 46px; height: 46px; padding: 0; border-radius: 50%; background: #e08a2e; display: grid; place-items: center; color: #fff; }
.demo .send svg { display: block; width: 24px; height: 24px; stroke-width: 2.4; }
@media (prefers-reduced-motion: no-preference) {
  .demo .typed { animation: type 7s steps(5) infinite, caret .8s step-end infinite; }
  .demo .new { animation: logged 7s ease infinite; }
  @keyframes type { 0%, 8% { width: 0; } 38%, 62% { width: 5ch; } 66%, 100% { width: 0; } }
  @keyframes caret { 50% { border-color: transparent; } }
  @keyframes logged { 0%, 64% { opacity: 0; transform: translateY(6px); } 70%, 96% { opacity: 1; transform: none; } 100% { opacity: 0; } }
}

/* chat */
.chat { display: grid; gap: 10px; margin-top: var(--s5); }
.bubble { max-width: 92%; padding: 12px 15px; border-radius: 18px; font-size: 15px; line-height: 1.5; }
.bubble.me { justify-self: end; background: linear-gradient(135deg,#a8452b,#7c2e1e); color: #fff; border-bottom-right-radius: 6px; }
.bubble.coach { display: grid; grid-template-columns: 34px 1fr; gap: 10px; background: var(--glass2); border: 1px solid var(--line); border-bottom-left-radius: 6px; }
.bubble.coach img { width: 34px; height: 34px; border-radius: 50%; }
.chat .note { font-size: 13px; color: var(--dim); }
.examples { display: grid; gap: var(--s4); }
@media (min-width: 900px) { .examples { grid-template-columns: 1fr 1fr; } }
.example { padding: var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
.example h3 { margin-bottom: 0; }
.example .bubble.coach p { margin: 0 0 8px; } .example .bubble.coach p:last-child { margin: 0; }
.example .bubble.coach ul, .example .bubble.coach ol { margin: 6px 0; padding-left: 18px; }

/* screenshots */
.shots { display: grid; grid-auto-flow: column; grid-auto-columns: min(64%, 240px); gap: var(--s4); overflow-x: auto; scroll-snap-type: x mandatory;
  padding: var(--s2) var(--s4) var(--s5); margin: 0 calc(-1 * var(--s4)); scrollbar-width: thin; }
@media (min-width: 900px) { .shots { grid-auto-flow: row; grid-template-columns: repeat(4, 1fr); overflow: visible; margin: 0; padding: var(--s2) 0; } }
.shots figure { margin: 0; scroll-snap-align: start; }
.shots .phone { width: 100%; }
.shots figcaption { margin-top: var(--s3); color: var(--muted); font-size: 15px; text-align: center; }

/* FAQ */
.faq { display: grid; gap: var(--s2); max-width: 820px; }
.faq details { border: 1px solid var(--line); border-radius: 16px; background: var(--glass); }
.faq details[open] { border-color: var(--line2); background: var(--glass2); }
.faq summary { cursor: pointer; list-style: none; padding: 18px 52px 18px 20px; position: relative; font-weight: 700; font-size: 17px; letter-spacing: -.01em; }
.faq summary::-webkit-details-marker { display: none; }
.faq summary::after { content: ""; position: absolute; right: 22px; top: 50%; width: 9px; height: 9px; margin-top: -6px; border-right: 2px solid var(--muted); border-bottom: 2px solid var(--muted); transform: rotate(45deg); transition: transform .2s ease; }
.faq details[open] summary::after { transform: rotate(-135deg); margin-top: -2px; }
.faq .a { padding: 0 20px 18px; color: #ead6c6; }
.faq .a p:last-child { margin: 0; }

/* tables */
.table-card { overflow-x: auto; border: 1px solid var(--line); border-radius: var(--r); background: var(--glass); margin: var(--s4) 0; }
table { width: 100%; border-collapse: collapse; font-size: 15px; }
th, td { text-align: left; vertical-align: top; padding: 12px 14px; border-bottom: 1px solid var(--line); }
tr:last-child td, tr:last-child th { border-bottom: 0; }
th { color: var(--muted); font-weight: 650; font-size: 14px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
tbody tr:hover { background: rgba(255,236,220,.03); }
.vs td:nth-child(2) { color: var(--fg); }

/* forms and calculators */
.tool { display: grid; gap: var(--s4); align-items: start; margin-top: var(--s5); }
/* the form stays put while the result (often a long table) scrolls past it */
@media (min-width: 900px) { .tool { grid-template-columns: 1.1fr .9fr; } .tool form.calc { position: sticky; top: 84px; } }
.row { align-items: end; }
small.hint { font-weight: 500; font-size: 12.5px; color: var(--dim); }
form.calc { display: grid; gap: var(--s4); padding: var(--s5); background: var(--glass); border: 1px solid var(--line); border-radius: var(--r); margin: 0; }
label, .lbl { display: grid; gap: 6px; font-size: 14px; font-weight: 600; color: var(--muted); }
input, select { font: 500 17px var(--display); color: var(--fg); background-color: rgba(0,0,0,.28); border: 1px solid var(--line2);
  border-radius: 12px; height: 50px; padding: 0 14px; width: 100%; transition: border-color .2s ease, box-shadow .2s ease; }
input:hover, select:hover { border-color: rgba(255,214,186,.38); }
input:focus, select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(255,138,61,.25); }
select { -webkit-appearance: none; appearance: none; padding-right: 40px; cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 8'%3E%3Cpath d='M1 1.5l5 5 5-5' fill='none' stroke='%23f7ede4' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 14px center; background-size: 12px; }
select option { background: #1c120e; color: var(--fg); }
.row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s3); }
.tool > *, .row > *, fieldset, label { min-width: 0; }
@media (max-width: 380px) { .row { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 520px) { .seg label { font-size: 13.5px; } }
.seg { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 4px; padding: 4px; border-radius: 14px; background: rgba(0,0,0,.28); border: 1px solid var(--line2); }
.seg input { position: absolute; opacity: 0; width: 1px; height: 1px; pointer-events: none; }
.seg label { display: grid; place-items: center; min-height: 44px; padding: 0 8px; text-align: center; line-height: 1.1; border-radius: 10px; color: var(--muted); font-size: 15px; font-weight: 650; cursor: pointer; transition: background .2s ease, color .2s ease; }
.seg input:checked + label { color: #1b0d06; background: var(--ember); }
.seg input:focus-visible + label { outline: 2px solid var(--accent2); outline-offset: 2px; }
fieldset { border: 0; padding: 0; margin: 0; min-width: 0; display: grid; gap: 6px; }
legend { padding: 0; margin-bottom: 6px; font-size: 14px; font-weight: 600; color: var(--muted); }
.checks { display: flex; flex-wrap: wrap; gap: 8px; }
.checks label { display: inline-flex; align-items: center; gap: 6px; min-height: 44px; color: var(--fg); font-weight: 600; padding: 0 12px; border-radius: 10px; border: 1px solid var(--line2); background: rgba(0,0,0,.2); cursor: pointer; }
.checks input { width: 16px; height: 16px; accent-color: var(--accent); }
.result { padding: var(--s5); border-radius: var(--r); border: 1px solid rgba(255,138,61,.45);
  background: radial-gradient(500px 220px at 100% 0%, rgba(255,138,61,.2), transparent 70%), var(--bg2); box-shadow: 0 30px 60px -40px rgba(255,106,43,.7); }
.result .k { font-size: 14px; font-weight: 600; color: var(--muted); }
.big { margin: 10px 0 6px; font: clamp(30px, 6vw, 44px)/1.15 var(--pixel); color: var(--accent2); text-shadow: 0 0 30px rgba(255,138,61,.45); overflow-wrap: anywhere; }
.big .u { font-size: .55em; }
.note + .pair { margin-top: 16px; }
.big-words { font-size: clamp(22px, 4.2vw, 30px); line-height: 1.25; overflow-wrap: normal; }
.result .note { color: var(--muted); font-size: 15px; min-height: 1.5em; }
.pair { display: grid; grid-template-columns: repeat(auto-fit, minmax(92px, 1fr)); gap: var(--s4); }
.pair .big { font-size: clamp(17px, 3.9vw, 26px); white-space: nowrap; }
@media (max-width: 520px) {
  .pair { grid-template-columns: minmax(0, 1fr); gap: var(--s2); }
  .pair > div { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s3); }
  .pair .big { margin: 4px 0; font-size: 24px; }
  .row:has(> fieldset) { grid-template-columns: minmax(0, 1fr); }
}
.log-cta { display: none; margin-top: var(--s4); }
.result.ok .log-cta { display: flex; }
.steps-out { counter-reset: ws; display: grid; gap: 6px; margin: var(--s3) 0 0; padding: 0; list-style: none; }
.steps-out li { display: grid; grid-template-columns: 26px 1fr auto; gap: 10px; align-items: center; margin: 0; padding: 10px 12px; border-radius: 12px; background: var(--glass2); border: 1px solid var(--line); font-variant-numeric: tabular-nums; }
.steps-out li::before { counter-increment: ws; content: counter(ws); font: 10px var(--pixel); color: var(--accent2); }
.steps-out li.work { border-color: rgba(255,138,61,.5); }
.steps-out small { color: var(--muted); font-size: 13px; }
.lvl { display: inline-block; padding: 3px 9px; border-radius: 8px; font: 10px/1.6 var(--pixel); background: rgba(255,138,61,.14); color: var(--accent2); border: 1px solid rgba(255,138,61,.3); }
.embed-box { margin-top: var(--s5); padding: var(--s4) var(--s5); border-radius: var(--r); border: 1px dashed var(--line2); background: var(--glass); }
.embed-box summary { cursor: pointer; font-weight: 700; min-height: 44px; display: flex; align-items: center; }
.embed-box[open] summary { margin-bottom: var(--s3); }
.embed-box textarea { display: block; box-sizing: border-box; width: 100%; min-height: 120px; resize: vertical; font: 13px/1.5 ui-monospace, Menlo, monospace; color: var(--fg); background: rgba(0,0,0,.3); border: 1px solid var(--line2); border-radius: 12px; padding: 12px; }

/* exercises and programs */
.frames { display: grid; grid-template-columns: 1fr 1fr; gap: var(--s3); margin: var(--s5) 0; }
.frames figure { margin: 0; border-radius: 18px; overflow: hidden; border: 1px solid var(--line); background: var(--glass); }
.frames picture { display: block; }
.frames img { display: block; width: 100%; height: auto; aspect-ratio: 4/3; object-fit: cover; object-position: center 60%; background: #2a1810; }
.frames figcaption { padding: 10px 14px; font-size: 14px; color: var(--muted); }
.frames figcaption b { font: 9px var(--pixel); color: var(--accent2); margin-right: 8px; }
ol.steps { counter-reset: st; list-style: none; padding: 0; display: grid; gap: var(--s3); }
ol.steps li { counter-increment: st; position: relative; margin: 0; padding: 16px 18px 16px 62px; border-radius: 16px; background: var(--glass); border: 1px solid var(--line); }
ol.steps li::before { content: counter(st); position: absolute; left: 16px; top: 14px; width: 32px; height: 32px; display: grid; place-items: center; border-radius: 10px; font: 11px var(--pixel); color: #1b0d06; background: var(--ember); }
.mistakes { padding: var(--s5); border-radius: var(--r); background: linear-gradient(160deg, rgba(255,207,112,.09), rgba(255,236,220,.02)); border: 1px solid rgba(255,207,112,.3); }
.mistakes h2 { margin-top: 0 !important; }
.mistakes ul { list-style: none; padding: 0; margin: 0; display: grid; gap: var(--s3); }
.mistakes li { position: relative; margin: 0; padding-left: 30px; }
.mistakes li::before { content: "!"; position: absolute; left: 0; top: 2px; width: 20px; height: 20px; display: grid; place-items: center; border-radius: 6px; font: 9px var(--pixel); color: #1b0d06; background: var(--warn); }
.days { display: grid; gap: var(--s3); grid-template-columns: repeat(auto-fill, minmax(min(100%, 320px), 1fr)); margin-top: var(--s4); }
.day { padding: var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
.day h3 { display: flex; align-items: baseline; gap: 10px; }
.day h3 small { font: 10px var(--pixel); color: var(--accent2); }
.day ul { list-style: none; padding: 0; margin: var(--s3) 0 0; }
.day li { display: flex; justify-content: space-between; gap: var(--s3); margin: 0; padding: 10px 0; border-top: 1px solid var(--line); font-size: 15px; }
.day li span:last-child { color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.day a.ex-link { color: var(--fg); text-decoration: none; }
.day a.ex-link::after { content: "›"; margin-left: 6px; color: var(--accent2); }
.day a.ex-link:hover { text-decoration: underline; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin: var(--s4) 0; }
.tabs a { padding: 8px 14px; border-radius: 999px; border: 1px solid var(--line2); color: var(--muted); text-decoration: none; font-size: 15px; font-weight: 600; }
.tabs a[aria-current] { color: #1b0d06; background: var(--ember); border-color: transparent; }

/* library pages (guides, glossary, muscles, compare, alternatives, template) */
.updated { margin-top: calc(-1 * var(--s2)); font-size: 14px; }
ul.links { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
ul.links li { margin: 0; }
ul.links a { display: block; padding: 12px 16px; border-radius: 14px; background: var(--glass); border: 1px solid var(--line); text-decoration: none; color: var(--fg); font-weight: 600; }
ul.links a:hover { border-color: var(--line2); background: var(--glass2); }
ul.links a::after { content: " →"; color: var(--accent2); }
ul.chips { list-style: none; padding: 0; margin: var(--s3) 0; display: flex; flex-wrap: wrap; gap: 8px; }
ul.chips li { margin: 0; padding: 6px 12px; border-radius: 999px; background: var(--glass); border: 1px solid var(--line); font-size: 14px; color: var(--muted); }
ul.chips a { color: var(--fg); text-decoration: none; }
.takeaways { margin: var(--s5) 0; padding: var(--s5); border-radius: var(--r); background: linear-gradient(160deg, rgba(255,138,61,.12), rgba(255,236,220,.02)); border: 1px solid rgba(255,138,61,.35); }
.takeaways h2 { margin-top: 0 !important; font-size: 22px !important; }
.takeaways ul { margin: 0; padding-left: 20px; }
.takeaways li { margin: 6px 0; }
.prose ul, .prose ol, section > ul, section > ol { padding-left: 22px; }
.prose ol.crumbs, section > ol.crumbs, .prose ul.links, .prose ul.chips, section > ul.chips { padding-left: 0; }
.prose li, section li { margin: 6px 0; }
.prose .crumbs li, section .crumbs li, .prose ul.links li, section ul.links li { margin: 0; }
blockquote { margin: var(--s4) 0; padding: var(--s3) var(--s4); border-left: 3px solid var(--accent); background: var(--glass); border-radius: 0 14px 14px 0; color: #ead6c6; }
.sources { font-size: 14px; padding-left: 20px; }
.sources a { color: var(--muted); }
.definition { font-weight: 600; }
.terms { display: grid; gap: var(--s3); margin-top: var(--s5); }
.term { padding: var(--s4) var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
.term h2 { font-size: 22px; margin: 0 0 6px; }
.term h2 a { color: var(--fg); text-decoration: none; }
.term p { margin: 0; color: var(--muted); }
ol.alts { list-style: none; padding: 0; margin: var(--s5) 0; display: grid; gap: var(--s3); }
li.alt { margin: 0; padding: var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
li.alt.us { border-color: rgba(255,138,61,.55); background: linear-gradient(160deg, rgba(255,138,61,.14), rgba(255,236,220,.03) 60%); }
.alt-head { display: flex; align-items: center; gap: 12px; margin-bottom: var(--s3); }
.alt-head h2 { margin: 0 !important; font-size: 24px !important; }
.alt-n { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; font: 11px var(--pixel); color: #1b0d06; background: var(--ember); flex: none; }
.alt-price, .alt-fit { color: var(--muted); }
.alt-price b, .alt-fit b { color: var(--fg); }
.downloads { margin: var(--s4) 0 var(--s2); }
.cta.upload .btn-row { margin-top: var(--s4); }
.cta.upload ol { margin: 0; }
.vs th a { color: var(--fg); }
.frames figure a { display: block; }
/* calculators: tonnage rows, rest timer, percentile bars */
.set-row { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s3); margin-bottom: var(--s3); }
.clock { font-size: clamp(56px, 14vw, 96px); text-align: center; margin: var(--s3) 0; font-variant-numeric: tabular-nums; }
.bar-track { height: 10px; border-radius: 999px; background: rgba(255,236,220,.08); overflow: hidden; margin: var(--s3) 0; }
.bar-fill { height: 100%; width: 100%; background: var(--ember); border-radius: 999px; transition: width .2s linear; }
.timer .btn-row { justify-content: center; }
.timer.ring { border-color: var(--ok); box-shadow: 0 0 0 3px rgba(123,216,143,.35); }
@media (prefers-reduced-motion: no-preference) { .timer.ring .clock { animation: ring .5s ease 3; } @keyframes ring { 50% { transform: scale(1.06); } } }
.pct-list { display: grid; gap: var(--s3); margin-top: var(--s3); }
.pct-head { display: flex; justify-content: space-between; gap: var(--s3); font-size: 15px; }
.pct-head span { color: var(--accent2); font-weight: 700; text-align: right; }
.pct .bar-track { margin: 6px 0 0; }

/* footer */
.site-foot { margin-top: 0; padding: var(--s8) 0 var(--s7); border-top: 1px solid var(--line); background: linear-gradient(180deg, transparent, rgba(0,0,0,.25)); font-size: 15px; }
.foot-grid { display: grid; gap: var(--s6); grid-template-columns: repeat(2, minmax(0, 1fr)); }
@media (min-width: 860px) { .foot-grid { grid-template-columns: 1.3fr repeat(5, 1fr); gap: var(--s5); } }
.foot-grid .foot-h { margin: 0 0 var(--s3); font: 10px/1.4 var(--pixel); letter-spacing: .05em; color: var(--accent2); text-transform: uppercase; }
.foot-grid ul { list-style: none; padding: 0; margin: 0; }
.foot-grid li { margin: 0; }
.foot-grid li a { display: inline-block; padding: 8px 0; }
.foot-grid a { color: var(--muted); text-decoration: none; }
.foot-grid a:hover { color: var(--fg); }
.foot-about { grid-column: 1 / -1; }
@media (min-width: 860px) { .foot-about { grid-column: auto; } }
.foot-about p { color: var(--muted); max-width: 320px; }
.foot-about .btn-row { flex-direction: column; align-items: stretch; max-width: 300px; }
.foot-about .btn { min-height: 44px; font-size: 15px; width: 100%; }
.foot-bottom { display: flex; flex-wrap: wrap; justify-content: space-between; gap: var(--s3); margin-top: var(--s6); padding-top: var(--s5); border-top: 1px solid var(--line); color: var(--dim); font-size: 14px; }
.foot-bottom a { color: var(--muted); }

/* scroll reveal */
@media (prefers-reduced-motion: no-preference) {
  @supports (animation-timeline: view()) {
    .reveal { animation: rise both cubic-bezier(.2,.7,.2,1); animation-timeline: view(); animation-range: entry 0% entry 38%; }
    @keyframes rise { from { opacity: 0; transform: translateY(26px) scale(.985); } to { opacity: 1; transform: none; } }
  }
  .hero-in { animation: hero-in .9s cubic-bezier(.2,.7,.2,1) .1s both; }
  @keyframes hero-in { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }
}

/* pixel gym */
.sprite { display: inline-block; flex: none; image-rendering: pixelated; image-rendering: crisp-edges; background-repeat: no-repeat; background-position: 0 0; vertical-align: bottom; }
@media (prefers-reduced-motion: no-preference) {
  .sprite.anim { animation-name: sprite-run; animation-iteration-count: infinite; animation-timing-function: steps(var(--frames)); }
  @keyframes sprite-run { to { background-position: var(--sx) 0; } }
  .scene-dog { animation: dog-cross 9s linear infinite; }
  @keyframes dog-cross { 0% { transform: translateX(-90px); } 55%, 100% { transform: translateX(calc(100cqw + 20px)); } }
}
.sec-head .sprite { display: block; margin-bottom: 10px; }
.gym { display: grid; gap: var(--s5); align-items: center; }
@media (min-width: 940px) { .gym { grid-template-columns: .85fr 1.15fr; gap: var(--s7); } }
.gym-text p { color: var(--muted); }
.scene { position: relative; margin: 0; padding: 8px; background: #2a1810; container-type: inline-size;
  clip-path: polygon(14px 0, calc(100% - 14px) 0, 100% 14px, 100% calc(100% - 14px), calc(100% - 14px) 100%, 14px 100%, 0 calc(100% - 14px), 0 14px);
  box-shadow: 0 40px 80px -40px rgba(255,106,43,.6); }
.scene img { display: block; width: 100%; height: auto; image-rendering: pixelated;
  clip-path: polygon(10px 0, calc(100% - 10px) 0, 100% 10px, 100% calc(100% - 10px), calc(100% - 10px) 100%, 10px 100%, 0 calc(100% - 10px), 0 10px); }
.scene-dog { position: absolute; left: 8px; bottom: 14px; display: block; pointer-events: none; }
.you { display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; align-items: end; margin: var(--s5) 0; padding: var(--s4); border-radius: 18px; background: var(--glass); border: 1px solid var(--line); }
.you .sprite { grid-row: span 2; }
.you-tag { justify-self: start; font: 10px/1 var(--pixel); color: #1b0d06; background: #ffd23f; padding: 6px 8px; border-radius: 4px; box-shadow: 0 0 0 2px #1b0d06; }
.you p { margin: 0; font-size: 15px; }
.notfound { display: flex; gap: var(--s6); align-items: center; flex-wrap: wrap; margin: var(--s5) 0 var(--s6); }

/* embed */
body.embed { background: var(--bg2); }
body.embed::before { display: none; }
body.embed main { padding: var(--s3) 0 var(--s2); }
body.embed .tool { margin-top: 0; }
.powered { display: block; margin-top: var(--s2); text-align: right; font-size: 12px; color: var(--muted); }
.powered a { color: var(--accent2); }
/* ---- phase 3 system pass ---- */
.hero-text .lead { max-width: 560px; }
/* live set in the hero: type «100 8», get the row, tonnage, e1RM and a coach line */
.live { margin-top: var(--s5); padding: var(--s4); border-radius: 18px; background: #f7f0ea; color: #2a1d17; max-width: 520px; box-shadow: 0 30px 60px -30px rgba(0,0,0,.8); }
.live-ex { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 12px; font-weight: 700; font-size: 15px; color: #6b3d2c; }
.live-ex small { font-weight: 500; font-size: 13px; color: #8a6a58; }
.live ol { list-style: none; margin: 10px 0; padding: 0; display: grid; gap: 4px; font: 600 16px/1.5 ui-monospace, Menlo, monospace; }
.live li { display: flex; justify-content: space-between; align-items: center; padding: 2px 4px; border-bottom: 1px dashed rgba(42,29,23,.12); margin: 0; }
.live li svg { width: 16px; height: 16px; color: #3f8f55; }
.live li.new { color: #a14a16; }
.live li.rec::after { content: "PR"; font: 10px var(--pixel); color: #fff; background: #e08a2e; padding: 3px 6px; border-radius: 6px; }
.live .input { display: flex; align-items: center; gap: 10px; }
.live input { flex: 1; min-width: 0; height: 46px; border-radius: 12px; border: 1px solid rgba(42,29,23,.15); background: #ede3da; color: #2a1d17; font: 600 18px ui-monospace, Menlo, monospace; padding: 0 14px; }
.live input:focus { border-color: #e08a2e; box-shadow: 0 0 0 3px rgba(224,138,46,.25); }
.live .send { flex: none; width: 46px; height: 46px; padding: 0; border: 0; border-radius: 50%; background: #e08a2e; display: grid; place-items: center; color: #fff; cursor: pointer; }
.live .send svg { width: 24px; height: 24px; stroke-width: 2.4; }
.live-out { margin-top: 12px; font-size: 15px; }
.live-out p { margin: 0 0 8px; }
.live-out .coach-line { font-weight: 700; color: #2a1d17; }
.live-out .nums { display: flex; flex-wrap: wrap; gap: 6px 16px; color: #6b3d2c; font-variant-numeric: tabular-nums; }
.live-out .btn { color: #1b0d06; }
/* bento tail: a snap carousel on phones, part of the grid on wide screens */
.bento-rest { display: contents; }
@media (max-width: 759px) {
  .bento-rest { display: grid; grid-auto-flow: column; grid-auto-columns: 78%; gap: var(--s3); overflow-x: auto; scroll-snap-type: x mandatory; margin: 0 calc(-1 * var(--s4)); padding: 0 var(--s4) var(--s2); scrollbar-width: none; }
  .bento-rest::-webkit-scrollbar { display: none; }
  .bento-rest > .tile { scroll-snap-align: start; }
}
/* home on phones: compact two-column cards */
@media (max-width: 599px) {
  body:not(.page) .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s2); }
  body:not(.page) .grid .card { padding: var(--s4); }
  body:not(.page) .grid .card .ic { width: 34px; height: 34px; margin-bottom: var(--s3); }
  body:not(.page) .grid .card b { font-size: 16px; padding-right: 0; }
  body:not(.page) .grid .card span:not(.ic) { font-size: 13.5px; }
  body:not(.page) .grid .card .arrow { display: none; }
}
/* «one workout» scrollytelling */
.story { display: grid; gap: var(--s5); }
.steps-story { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s4); }
.step { position: relative; margin: 0; padding: var(--s5) var(--s5) var(--s5) 72px; border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); transition: border-color .3s ease, background .3s ease; }
.step.on { border-color: rgba(255,138,61,.55); background: var(--glass2); }
.step-n { position: absolute; left: 20px; top: 22px; width: 34px; height: 34px; display: grid; place-items: center; border-radius: 10px; font: 11px var(--pixel); color: #1b0d06; background: var(--ember); }
.step h3 { font-size: 22px; }
.step p { color: var(--muted); margin: 0; }
.step-shot { margin: var(--s4) auto 0; width: min(240px, 70%); }
.story-side { display: none; }
@media (min-width: 900px) {
  .story { grid-template-columns: 1fr 300px; gap: var(--s7); align-items: start; }
  .steps-story { gap: 26vh; padding: 8vh 0 18vh; }
  .step-shot { display: none; }
  .story-side { display: block; position: sticky; top: 96px; }
  .story-phone { width: 270px; margin-left: auto; }
  .story-phone .screen { position: absolute; inset: 9px; opacity: 0; transition: opacity .45s ease; }
  .story-phone .screen.on { opacity: 1; }
}
@media (prefers-reduced-motion: reduce) { .story-phone .screen { transition: none; } }
/* basement band: the pixel coach walks the floor, the board shows this week's real numbers */
.basement { position: relative; margin: var(--s8) 0; padding: var(--s5); border-radius: 28px; overflow: hidden; min-height: 150px;
  background: linear-gradient(180deg, #2a1810 0%, #1d110c 70%, #3a2116 70%, #2a1810 100%); border: 1px solid var(--line2); display: grid; gap: var(--s4); align-items: center; container-type: inline-size; }
@media (min-width: 760px) { .basement { grid-template-columns: 1fr auto; } }
.basement-floor { position: relative; height: 210px; }
.walker, .walker-dog { position: absolute; bottom: 0; left: 0; }
.walker-dog { bottom: 0; }
@media (prefers-reduced-motion: no-preference) {
  .walker { animation: walk 22s linear infinite alternate; }
  .walker-dog { animation: walk 9s linear infinite alternate-reverse; }
  @keyframes walk { from { transform: translateX(0); } to { transform: translateX(calc(100cqw - 180px)); } }
}
.board { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(3, minmax(0, auto)); gap: var(--s3) var(--s5); padding: var(--s4) var(--s5); border-radius: 6px; background: #12201a; border: 6px solid #5a3a22; box-shadow: 0 12px 30px -10px rgba(0,0,0,.7); color: #e8f1e6; }
.board-h { grid-column: 1 / -1; font: 10px/1.4 var(--pixel); letter-spacing: .05em; color: #ffd23f; }
.board div { display: grid; gap: 4px; }
.board-n { font: 22px/1 var(--pixel); color: #fff; }
.board small { font-size: 13px; color: #b9cdb5; }
.board-note { grid-column: 1 / -1; font-size: 12px !important; }
/* feature pages: text left, visual right */
.feature-head { display: grid; gap: var(--s5); align-items: center; }
@media (min-width: 940px) { .feature-head { grid-template-columns: minmax(0, 7fr) minmax(0, 5fr); gap: var(--s7); } }
.feature-text .btn-row { margin-top: var(--s5); }
.feature-text .alt-link { margin-top: var(--s3); }
.feature-visual { display: grid; justify-items: center; }
.feature-visual .chat { width: 100%; max-width: 440px; }
.feature-visual .phone { width: 236px; }
.ic-h { display: flex; align-items: center; gap: 12px; }
.ic-h .ic { flex: none; display: inline-grid; place-items: center; width: 40px; height: 40px; border-radius: 12px; color: var(--accent2); background: rgba(255,138,61,.12); border: 1px solid rgba(255,138,61,.28); }
.ic-h .ic svg { width: 21px; height: 21px; }
.review-card { width: 100%; max-width: 440px; padding: var(--s5); border-radius: var(--r); background: var(--glass2); border: 1px solid var(--line2); font-size: 15px; }
.review-card .badge { display: inline-block; margin-bottom: 8px; padding: 3px 9px; border-radius: 8px; font-size: 12px; color: var(--accent2); background: rgba(255,138,61,.12); border: 1px solid rgba(255,138,61,.3); }
.review-card .review-ex { font-weight: 700; margin-bottom: 8px; }
.review-card ul { margin: 0; padding-left: 18px; }
.csv-visual { width: 100%; max-width: 460px; display: grid; gap: var(--s3); justify-items: center; }
.csv-visual pre { width: 100%; margin: 0; padding: var(--s4); border-radius: 14px; background: rgba(0,0,0,.35); border: 1px solid var(--line2); overflow-x: auto; font: 12.5px/1.6 ui-monospace, Menlo, monospace; color: #ead6c6; }
.csv-arrow { transform: rotate(90deg); color: var(--accent2); }
.csv-arrow svg { width: 26px; height: 26px; }
.csv-visual ul { width: 100%; list-style: none; margin: 0; padding: var(--s3) var(--s4); border-radius: 14px; background: #f7f0ea; color: #2a1d17; font: 600 14px/1.5 ui-monospace, Menlo, monospace; }
.csv-visual li { display: flex; justify-content: space-between; gap: var(--s3); margin: 0; padding: 4px 0; border-bottom: 1px dashed rgba(42,29,23,.12); }
/* exercise page: sticky aside on wide screens */
.ex-layout { display: grid; gap: var(--s5); }
@media (min-width: 1000px) { .ex-layout { grid-template-columns: minmax(0, 1fr) 300px; gap: var(--s7); align-items: start; } .ex-aside { position: sticky; top: 84px; } }
.ex-main { max-width: 760px; }
.ex-aside { display: grid; gap: var(--s4); padding: var(--s5); border-radius: var(--r); background: var(--glass); border: 1px solid var(--line); }
.facts { margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 8px 14px; font-size: 15px; }
.facts dt { color: var(--muted); }
.facts dd { margin: 0; }
.aside-cta p { margin: 0 0 var(--s3); font-weight: 700; }
.aside-cta .btn-row { flex-direction: column; align-items: stretch; }
.aside-cta .btn { min-height: 46px; font-size: 15px; }
.aside-h { font-size: 18px !important; margin: 0 !important; }
.ex-aside ul.links a { padding: 10px 12px; font-size: 14px; }
.cap-short { display: none; }
@media (max-width: 520px) { .cap-long { display: none; } .cap-short { display: inline; } }
/* exercises hub: sticky group chips */
.group-chips { position: sticky; top: 60px; z-index: 5; display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; margin: var(--s4) calc(-1 * var(--s4)) 0; padding: 10px var(--s4); background: rgba(15,9,7,.86); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); }
.group-chips::-webkit-scrollbar { display: none; }
.group-chips a { flex: none; display: inline-flex; align-items: center; min-height: 40px; padding: 0 14px; border-radius: 999px; border: 1px solid var(--line2); color: var(--muted); text-decoration: none; font-size: 14px; font-weight: 600; }
.group-chips a:hover { color: var(--fg); }
@media (max-width: 959px) { .group-chips { top: 114px; } }
.group-title { scroll-margin-top: 190px; }
/* tables: horizontal scroll hint and sticky first column */
.table-card.scroll-x { position: relative; }
.sticky-first th:first-child { position: sticky; left: 0; z-index: 1; background: #1b100c; }
@media (max-width: 700px) { .table-card.scroll-x { -webkit-mask-image: linear-gradient(90deg, #000 calc(100% - 36px), transparent); mask-image: linear-gradient(90deg, #000 calc(100% - 36px), transparent); } }
/* vs table: our column highlighted, marks; row cards on phones */
.vs th.us, .vs td.us { background: rgba(255,138,61,.07); }
.vs thead th.us { color: var(--accent2); }
.mark { display: inline-grid; place-items: center; width: 22px; height: 22px; margin-right: 8px; border-radius: 7px; font: 700 13px/1 var(--display); vertical-align: -3px; }
.mark.yes { color: #0f2914; background: var(--ok); }
.mark.part { color: #2a1d06; background: var(--warn); }
.mark.no { color: var(--fg); background: rgba(255,236,220,.14); }
@media (max-width: 640px) {
  .cards-mobile table, .cards-mobile tbody, .cards-mobile tr, .cards-mobile th, .cards-mobile td { display: block; width: 100%; }
  .cards-mobile thead { display: none; }
  .cards-mobile tr { padding: var(--s3) var(--s4); border-bottom: 1px solid var(--line); }
  .cards-mobile tr:last-child { border-bottom: 0; }
  .cards-mobile th[scope=row] { padding: 0 0 6px; border: 0; color: var(--fg); font-size: 16px; }
  .cards-mobile td { padding: 6px 0; border: 0; }
  .cards-mobile td::before { content: attr(data-label); display: block; font-size: 12px; font-weight: 700; color: var(--muted); margin-bottom: 2px; }
}
.share svg { width: 16px; height: 16px; }
.log-cta { flex-wrap: wrap; }
@media print { .reveal, .hero-in { animation: none !important; } .site-head, .sub-nav, .cta, .site-foot { display: none; } body { background: #fff; color: #000; } }
"""

_ICON_PATHS = {
    "type": '<rect x="3" y="6" width="18" height="12" rx="3"/><path d="M7 10h.01M11 10h.01M15 10h.01M7 14h10"/>',
    "coach": '<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/><path d="M8.5 11h7M8.5 14h4"/>',
    "video": '<rect x="3" y="6" width="13" height="12" rx="3"/><path d="m16 10 5-3v10l-5-3"/>',
    "chart": '<path d="M4 19V5"/><path d="M4 19h16"/><path d="m7 15 4-4 3 3 5-6"/>',
    "program": '<rect x="4" y="4" width="16" height="17" rx="3"/><path d="M8 2v4M16 2v4M8 11h8M8 15h5"/>',
    "import": '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
    "phone": '<rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M11 18h2"/>',
    # Как кнопка «отправить» в приложении (arrow.up в круге): стрелка
    # симметрична, её центр — центр viewBox, оптический сдвиг не нужен.
    "send": '<path d="M12 19V5"/><path d="m6 11 6-6 6 6"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/>',
    "check": '<path d="m5 12 4.5 4.5L19 7"/>',
    "calc": '<rect x="5" y="3" width="14" height="18" rx="3"/><path d="M8 7h8M8.5 12h.01M12 12h.01M15.5 12h.01M8.5 16h.01M12 16h.01M15.5 16h.01"/>',
    "plates": '<path d="M2 12h20"/><rect x="5" y="6" width="3" height="12" rx="1"/><rect x="16" y="6" width="3" height="12" rx="1"/><rect x="8" y="8" width="2" height="8" rx="1"/><rect x="14" y="8" width="2" height="8" rx="1"/>',
    "trophy": '<path d="M8 4h8v5a4 4 0 0 1-8 0V4Z"/><path d="M8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M8 21h8M9 17h6"/>',
    "flame": '<path d="M12 22a7 7 0 0 0 7-7c0-4-3-6-4-9-1 2-2 3-4 3 0-2-1-4-2-6-2 4-4 7-4 12a7 7 0 0 0 7 7Z"/>',
    "dumbbell": '<path d="M6.5 6.5v11M17.5 6.5v11M3.5 9v6M20.5 9v6M6.5 12h11"/>',
    "warmup": '<path d="M4 20h4v-4h4v-4h4V8h4"/>',
    "gauge": '<path d="M4 15a8 8 0 1 1 16 0"/><path d="m12 15 4-5"/><path d="M12 15h.01"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "apple": '<path d="M16.4 12.6c0-2.3 1.9-3.4 2-3.5a4.3 4.3 0 0 0-3.4-1.8c-1.4-.2-2.8.8-3.5.8s-1.8-.8-3-.8a4.5 4.5 0 0 0-3.8 2.3c-1.6 2.8-.4 7 1.2 9.3.8 1.1 1.7 2.4 2.9 2.3 1.2 0 1.6-.7 3-.7s1.8.7 3 .7c1.3 0 2-1.1 2.8-2.3a9.6 9.6 0 0 0 1.3-2.6 3.9 3.9 0 0 1-2.5-3.7Z"/><path d="M14.2 5.4a4 4 0 0 0 .9-2.9 4.1 4.1 0 0 0-2.7 1.4 3.8 3.8 0 0 0-1 2.8 3.4 3.4 0 0 0 2.8-1.3Z"/>',
    "telegram": '<path d="M21.5 4.5 2.8 11.4c-.9.4-.9 1.6.1 1.9l4.6 1.4 1.8 5.6c.3.8 1.3 1 1.9.4l2.6-2.5 4.6 3.4c.7.5 1.7.1 1.9-.8L22.9 5.7c.2-1-.6-1.6-1.4-1.2Z"/><path d="m8 14.6 9.5-6.3"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "timer": '<circle cx="12" cy="13" r="8"/><path d="M12 9v4l2.5 2.5M9.5 2.5h5M12 2.5V5"/>',
    "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2"/>',
    "percent": '<path d="M19 5 5 19"/><circle cx="7" cy="7" r="2.5"/><circle cx="17" cy="17" r="2.5"/>',
    "body": '<circle cx="12" cy="4.5" r="2"/><path d="M5 8.5h14M12 8.5v6M9 21l3-6.5 3 6.5"/>',
    "book": '<path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5v-15Z"/><path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5M8 7h8M8 11h6"/>',
    "image": '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
    "scale": '<path d="M12 3v18M5 21h14M6 7h12M6 7l-3 7a3 3 0 0 0 6 0L6 7ZM18 7l-3 7a3 3 0 0 0 6 0l-3-7Z"/>',
}


def icon(name: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICON_PATHS[name]}</svg>')
