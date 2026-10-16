"""Публичный сайт, третья часть (seo_pages.py — каркас): разделы-библиотеки и
страницы для переезда.

- `/muscles`, `/muscles/<группа>` — группы мышц каталога бота: упражнения со
  своей страницей техники (карточки с кадрами), остальные упражнения
  каталога на эту группу и программы, которые её тренируют, — с числом
  подходов за цикл, посчитанным тем же правилом, что у бота
  (`seed_data.volume_split`: становая — пополам спине и задней цепи ног).
- `/compare`, `/compare/<a>-vs-<b>` — упражнение против упражнения: таблица
  (мышцы, снаряд, сложность, когда что брать) и свой разбор у каждой пары.
- `/glossary`, `/glossary/<термин>` — словарь зала со ссылками на
  калькуляторы (DefinedTerm в JSON-LD).
- `/guides`, `/guides/<тема>` — статьи голосом тренера, со ссылками на
  исследования, где названо число (`GUIDE_SOURCES`).
- `/alternatives/<hevy|strong>` — честный список «чем заменить»: мы первые,
  остальные — факты по данным на октябрь 2026 со ссылками.
- `/templates/workout-log` — шаблон дневника (файлы — seo_template.py).
- `/about` — о проекте.

Тексты — `seo.*` в каталоге; ссылки внутри текста — плейсхолдеры
(`{rm1}`, `{ex_back_squat}`, `{guide_plateau}`), их подставляет
`link_params`: адрес на языке страницы, сломанной ссылки быть не может —
неизвестный плейсхолдер валит рендер (и тест).
"""

from __future__ import annotations

import re
from typing import Optional

import config
import seed_data
import seo_design
import seo_pages as sp

Page = sp.Page

# ---------- данные разделов ----------

# Группы мышц: адрес → (каноническая группа каталога, мишень или None).
MUSCLES: tuple[tuple[str, str, Optional[str]], ...] = (
    ("chest", "Грудь", None),
    ("back", "Спина", None),
    ("shoulders", "Плечи", None),
    ("side-delts", "Плечи", "side_delts"),
    ("rear-delts", "Плечи", "rear_delts"),
    ("biceps", "Бицепс", None),
    ("triceps", "Трицепс", None),
    ("abs", "Пресс", None),
    ("quads", "Ноги", "quads"),
    ("hamstrings", "Ноги", "hamstrings"),
    ("glutes", "Ноги", "glutes"),
    ("calves", "Ноги", "calves"),
)
_MUSCLE_BY_URL = {url: (group, target) for url, group, target in MUSCLES}

# Пары «упражнение против упражнения» — только среди страниц техники.
PAIRS: tuple[tuple[str, str], ...] = (
    ("bench-press", "dumbbell-bench-press"),
    ("back-squat", "leg-press"),
    ("deadlift", "sumo-deadlift"),
    ("pull-up", "lat-pulldown"),
    ("back-squat", "front-squat"),
    ("deadlift", "romanian-deadlift"),
    ("barbell-row", "seated-cable-row"),
    ("overhead-press", "seated-dumbbell-press"),
    ("bench-press", "incline-bench-press"),
    ("pull-up", "chin-up"),
    ("hip-thrust", "glute-bridge"),
    ("lying-leg-curl", "seated-leg-curl"),
    ("romanian-deadlift", "stiff-leg-deadlift"),
    ("barbell-curl", "dumbbell-curl"),
    ("dumbbell-curl", "hammer-curl"),
    ("triceps-pushdown", "skull-crusher"),
    ("dips", "push-up"),
    ("cable-crossover", "dumbbell-fly"),
    ("deadlift", "trap-bar-deadlift"),
    ("bulgarian-split-squat", "dumbbell-lunge"),
)

# Снаряд и порог входа у упражнений из пар: (снаряд, сложность 1–3).
# 1 — можно с первой тренировки, 2 — нужна пара недель на технику,
# 3 — техника решает, начинать с лёгкого веса и с видео.
EX_FACTS: dict[str, tuple[str, int]] = {
    "bench-press": ("barbell", 2), "dumbbell-bench-press": ("dumbbells", 1),
    "back-squat": ("barbell", 3), "leg-press": ("machine", 1), "front-squat": ("barbell", 3),
    "deadlift": ("barbell", 3), "sumo-deadlift": ("barbell", 3), "romanian-deadlift": ("barbell", 2),
    "stiff-leg-deadlift": ("barbell", 2), "trap-bar-deadlift": ("trap_bar", 2),
    "pull-up": ("bar", 2), "chin-up": ("bar", 2), "lat-pulldown": ("cable", 1),
    "barbell-row": ("barbell", 2), "seated-cable-row": ("cable", 1),
    "overhead-press": ("barbell", 2), "seated-dumbbell-press": ("dumbbells", 1),
    "incline-bench-press": ("barbell", 2),
    "hip-thrust": ("barbell", 2), "glute-bridge": ("barbell", 1),
    "lying-leg-curl": ("machine", 1), "seated-leg-curl": ("machine", 1),
    "barbell-curl": ("barbell", 1), "dumbbell-curl": ("dumbbells", 1), "hammer-curl": ("dumbbells", 1),
    "triceps-pushdown": ("cable", 1), "skull-crusher": ("ez_bar", 2),
    "dips": ("bars", 2), "push-up": ("bodyweight", 1),
    "cable-crossover": ("cable", 1), "dumbbell-fly": ("dumbbells", 1),
    "bulgarian-split-squat": ("dumbbells", 2), "dumbbell-lunge": ("dumbbells", 1),
    "incline-dumbbell-press": ("dumbbells", 1), "lateral-raise": ("dumbbells", 1),
    "goblet-squat": ("kettlebell", 1), "back-extension": ("bench", 1), "standing-calf-raise": ("machine", 1),
    "one-arm-dumbbell-row": ("dumbbells", 1), "good-morning": ("barbell", 3), "plank": ("bodyweight", 1),
    "hanging-leg-raise": ("bar", 2), "decline-bench-press": ("barbell", 2), "close-grip-bench-press": ("barbell", 2),
    "machine-chest-press": ("machine", 1), "pec-deck": ("machine", 1), "rope-pushdown": ("cable", 1),
    "overhead-cable-extension": ("cable", 1), "triceps-kickback": ("dumbbells", 1), "bench-dips": ("bench", 1),
    "ez-bar-curl": ("ez_bar", 1), "incline-dumbbell-curl": ("dumbbells", 1), "concentration-curl": ("dumbbells", 1),
    "preacher-curl": ("ez_bar", 1), "arnold-press": ("dumbbells", 2), "face-pull": ("cable", 1),
    "rear-delt-fly": ("dumbbells", 1), "upright-row": ("barbell", 2), "front-raise": ("dumbbells", 1),
    "machine-shoulder-press": ("machine", 1), "t-bar-row": ("barbell", 2), "rack-pull": ("barbell", 2),
    "straight-arm-pulldown": ("cable", 1), "inverted-row": ("bodyweight", 1), "hack-squat": ("machine", 2),
    "leg-extension": ("machine", 1), "step-up": ("dumbbells", 1), "barbell-lunge": ("barbell", 2),
    "crunch": ("bodyweight", 1), "cable-crunch": ("cable", 1), "ab-wheel-rollout": ("wheel", 3),
    "side-plank": ("bodyweight", 1),
}

GLOSSARY: tuple[str, ...] = (
    "e1rm", "rpe", "rir", "tonnage", "double-progression", "superset", "drop-set",
    "warm-up-set", "working-set", "plateau", "deload", "hypertrophy",
)

GUIDES: tuple[str, ...] = (
    "how-to-keep-a-workout-log", "progressive-overload", "rest-between-sets", "break-a-plateau",
    "sets-per-muscle-per-week", "estimate-1rm-safely", "warm-up-before-lifting",
    "switch-from-hevy-or-strong", "choose-a-program", "film-your-set",
)
GUIDE_SECTIONS = {
    "how-to-keep-a-workout-log": 6, "progressive-overload": 6, "rest-between-sets": 6, "break-a-plateau": 7,
    "sets-per-muscle-per-week": 6, "estimate-1rm-safely": 6, "warm-up-before-lifting": 6,
    "switch-from-hevy-or-strong": 6, "choose-a-program": 6, "film-your-set": 6,
}

# Исследования, на которые опирается статья там, где названо число. Подпись —
# библиографическая (латиница), одинаковая на обоих языках.
_SCHOENFELD_2017 = ("Schoenfeld, Ogborn, Krieger (2017). Dose-response relationship between weekly resistance training volume "
                    "and increases in muscle mass. J Sports Sci", "https://pubmed.ncbi.nlm.nih.gov/27433992/")
_PELLAND = ("Pelland et al. The Resistance Training Dose Response: meta-regressions on weekly volume and frequency. "
            "Sports Med", "https://pubmed.ncbi.nlm.nih.gov/41343037/")
_FREQ_2016 = ("Schoenfeld, Ogborn, Krieger (2016). Effects of resistance training frequency on measures of muscle "
              "hypertrophy. Sports Med", "https://pubmed.ncbi.nlm.nih.gov/27102172/")
_REST_2016 = ("Schoenfeld et al. (2016). Longer interset rest periods enhance muscle strength and hypertrophy in "
              "resistance-trained men. J Strength Cond Res", "https://pubmed.ncbi.nlm.nih.gov/26605807/")
_REST_2024 = ("Singer et al. (2024). Give it a rest: a systematic review with Bayesian meta-analysis on inter-set rest "
              "interval duration and muscle hypertrophy. Front Sports Act Living", "https://pubmed.ncbi.nlm.nih.gov/39205815/")
_RIR = ("Zourdos et al. (2016). Novel resistance training-specific RPE scale measuring repetitions in reserve. "
        "J Strength Cond Res", "https://pubmed.ncbi.nlm.nih.gov/26049792/")
_PLOTKIN = ("Plotkin et al. (2022). Progressive overload without progressing load? The effects of load or repetition "
            "progression on muscular adaptations. PeerJ", "https://peerj.com/articles/14142")
_FAILURE = ("Refalo et al. (2023). Influence of resistance training proximity-to-failure on skeletal muscle "
            "hypertrophy: a systematic review with meta-analysis. Sports Med", "https://pmc.ncbi.nlm.nih.gov/articles/PMC9935748/")
_DELOAD_TRIAL = ("Coleman et al. (2024). Gaining more from doing less? The effects of a one-week deload period during "
                 "supervised resistance training on muscular adaptations. PeerJ", "https://peerj.com/articles/16777")
_DELOAD_SURVEY = ("Rogerson et al. (2024). Deloading practices in strength and physique sports: a cross-sectional survey. "
                  "Sports Med Open", "https://sportsmedicine-open.springeropen.com/articles/10.1186/s40798-024-00691-y")
_PROTEIN = ("Morton et al. (2018). A systematic review, meta-analysis and meta-regression of the effect of protein "
            "supplementation on resistance training-induced gains in muscle mass and strength. Br J Sports Med",
            "https://pubmed.ncbi.nlm.nih.gov/28698222/")
_WARMUP_META = ("Fradkin, Zazryn, Smoliga (2010). Effects of warming-up on physical performance: a systematic review "
                "with meta-analysis. J Strength Cond Res", "https://pubmed.ncbi.nlm.nih.gov/19996770/")
_WARMUP_RT = ("Ribeiro et al. (2021). The effect of warm-up in resistance training and strength performance: "
              "a systematic review. Motricidade", "https://revistas.rcaap.pt/motricidade/article/view/21143")

GUIDE_SOURCES: dict[str, tuple[tuple[str, str], ...]] = {
    "how-to-keep-a-workout-log": (_PLOTKIN, _RIR),
    "progressive-overload": (_PLOTKIN, _FAILURE, _RIR),
    "rest-between-sets": (_REST_2016, _REST_2024),
    "break-a-plateau": (_PELLAND, _DELOAD_TRIAL, _DELOAD_SURVEY, _FAILURE, _PROTEIN),
    "sets-per-muscle-per-week": (_SCHOENFELD_2017, _PELLAND, _FREQ_2016),
    "estimate-1rm-safely": (_RIR,),
    "warm-up-before-lifting": (_WARMUP_META, _WARMUP_RT),
    "switch-from-hevy-or-strong": (),
    "choose-a-program": (_FREQ_2016, _PELLAND, _SCHOENFELD_2017),
    "film-your-set": (),
}

# Ссылки «читать дальше» у термина: имена плейсхолдеров link_params.
TERM_LINKS: dict[str, tuple[str, ...]] = {
    "e1rm": ("rm1", "rpe", "goal", "guide_estimate_1rm_safely"),
    "rpe": ("rpe", "term_rir", "rm1", "guide_progressive_overload"),
    "rir": ("term_rpe", "rpe", "guide_progressive_overload"),
    "tonnage": ("tonnage", "term_hypertrophy", "guide_sets_per_muscle_per_week"),
    "double-progression": ("guide_progressive_overload", "term_plateau", "programs"),
    "superset": ("timer", "guide_rest_between_sets", "term_drop_set"),
    "drop-set": ("term_superset", "term_working_set", "tonnage"),
    "warm-up-set": ("warmup", "guide_warm_up_before_lifting", "term_working_set"),
    "working-set": ("term_warm_up_set", "rpe", "guide_sets_per_muscle_per_week"),
    "plateau": ("guide_break_a_plateau", "term_deload", "term_double_progression"),
    "deload": ("guide_break_a_plateau", "term_plateau", "programs"),
    "hypertrophy": ("guide_sets_per_muscle_per_week", "muscles", "guide_rest_between_sets"),
}

GUIDE_LINKS: dict[str, tuple[str, ...]] = {
    "how-to-keep-a-workout-log": ("template", "telegram", "term_e1rm", "guide_progressive_overload"),
    "progressive-overload": ("term_double_progression", "rm1", "guide_break_a_plateau", "programs"),
    "rest-between-sets": ("timer", "term_superset", "guide_sets_per_muscle_per_week"),
    "break-a-plateau": ("term_deload", "term_plateau", "guide_progressive_overload", "ai"),
    "sets-per-muscle-per-week": ("muscles", "programs", "term_working_set", "tonnage"),
    "estimate-1rm-safely": ("rm1", "rpe", "warmup", "goal"),
    "warm-up-before-lifting": ("warmup", "rpe", "term_warm_up_set"),
    "switch-from-hevy-or-strong": ("import_hevy", "import_strong", "template", "vs_hevy"),
    "choose-a-program": ("programs", "ai", "guide_sets_per_muscle_per_week", "muscles"),
    "film-your-set": ("video", "exercises", "ai"),
}

# Альтернативы: id приложений в списке; «us» — всегда первыми.
ALT_APPS = {
    "hevy": ("us", "strong", "fitbod", "jefit", "boostcamp", "sheet"),
    "strong": ("us", "hevy", "fitbod", "jefit", "boostcamp", "sheet"),
}
ALT_SOURCES = {
    "boostcamp": ("https://www.boostcamp.app/pro", "https://www.boostcamp.app/features"),
}

ABOUT_LINKS = ("ai", "video", "telegram", "import_hevy", "tools", "guides")


def pages() -> list[Page]:
    out = [Page("muscles")] + [Page("muscle", url) for url, _g, _t in MUSCLES]
    out += [Page("compare")] + [Page("pair", pair_slug(a, b)) for a, b in PAIRS]
    out += [Page("glossary")] + [Page("term", term) for term in GLOSSARY]
    out += [Page("guides")] + [Page("guide", g) for g in GUIDES]
    return out


def pair_slug(a: str, b: str) -> str:
    return f"{a}-vs-{b}"


_PAIR_BY_SLUG = {pair_slug(a, b): (a, b) for a, b in PAIRS}


def pair_exercises(slug: str) -> tuple[str, str]:
    return _PAIR_BY_SLUG[slug]


def updated_for(page: Page) -> Optional[str]:
    """Дата правки, если у страницы своя; иначе — общая дата сайта."""
    return None


# ---------- ссылки внутри текстов ----------


def _ph(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def link_params(lang: str) -> dict[str, str]:
    """Плейсхолдер → адрес на языке страницы. Плюс числа потолков из config."""
    u = lambda page: sp.url_for(page, lang)  # noqa: E731
    out: dict[str, object] = {
        "rm1": u(Page("tool", "1rm")), "warmup": u(Page("tool", "warmup")),
        "rpe": u(Page("tool", "rpe")), "wilks": u(Page("tool", "wilks")), "tonnage": u(Page("tool", "tonnage")),
        "timer": u(Page("tool", "rest-timer")), "goal": u(Page("tool", "goal")),
        "pct": u(Page("tool", "strength-percentile")), "tools": u(Page("tools")),
        "strength": u(Page("strength")), "programs": u(Page("programs")), "exercises": u(Page("exercises")),
        "muscles": u(Page("muscles")), "glossary": u(Page("glossary")), "guides": u(Page("guides")),
        "compare": u(Page("compare")), "template": u(Page("template", "workout-log")),
        "import_hevy": u(Page("import", "hevy")), "import_strong": u(Page("import", "strong")),
        "import_csv": u(Page("import", "csv")), "video": u(Page("video-review")), "ai": u(Page("ai-coach")),
        "telegram": u(Page("telegram")), "app": u(Page("workout-log-app")), "about": u(Page("about")),
        "vs_hevy": u(Page("vs", "hevy")), "vs_strong": u(Page("vs", "strong")),
        "seconds": config.MAX_VIDEO_SECONDS, "videos": config.AI_VIDEO_DAILY_LIMIT,
        "questions": config.AI_QUESTION_DAILY_LIMIT, "days": config.IMPORT_UNDO_DAYS,
        "max_reps": config.E1RM_MAX_REPS,
    }
    for url, _slug in sp.EXERCISES:
        out[f"ex_{_ph(url)}"] = u(Page("exercise", url))
    for p in seed_data.WORKOUT_PROGRAMS:
        out[f"prog_{_ph(p['key'])}"] = u(Page("program", p["key"]))
    for term in GLOSSARY:
        out[f"term_{_ph(term)}"] = u(Page("term", term))
    for g in GUIDES:
        out[f"guide_{_ph(g)}"] = u(Page("guide", g))
    for url, _g, _t in MUSCLES:
        out[f"mus_{_ph(url)}"] = u(Page("muscle", url))
    for a, b in PAIRS:
        out[f"cmp_{_ph(a)}__{_ph(b)}"] = u(Page("pair", pair_slug(a, b)))
    out.update(sp._article_params(lang))
    return out  # type: ignore[return-value]


def _link_label(lang: str, name: str) -> tuple[str, str]:
    """(адрес, подпись) для плейсхолдера из TERM_LINKS/GUIDE_LINKS."""
    t = sp._t(lang)
    href = str(link_params(lang)[name])
    if name.startswith("term_"):
        term = next(x for x in GLOSSARY if _ph(x) == name[5:])
        return href, t(f"seo.term.{term}.name")
    if name.startswith("guide_"):
        guide = next(x for x in GUIDES if _ph(x) == name[6:])
        return href, t(f"seo.guide.{guide}.h1")
    labels = {
        "rm1": "seo.tool.1rm.short", "warmup": "seo.tool.warmup.short",
        "rpe": "seo.tool.rpe.short", "tonnage": "seo.tool.tonnage.short", "timer": "seo.tool.rest-timer.short",
        "goal": "seo.tool.goal.short", "pct": "seo.tool.strength-percentile.short", "programs": "seo.programs.h1",
        "exercises": "seo.exercises.h1", "muscles": "seo.nav.muscles", "template": "seo.template.workout-log.short",
        "import_hevy": "seo.import.hevy.short", "import_strong": "seo.import.strong.short", "vs_hevy": "seo.vs.hevy.short",
        "video": "seo.video-review.h1", "ai": "seo.ai-coach.h1", "telegram": "seo.telegram.h1", "tools": "seo.nav.tools",
        "guides": "seo.nav.guides",
    }
    return href, sp._strip_emoji(t(labels[name]))


def _related(lang: str, names: tuple[str, ...]) -> str:
    t = sp._t(lang)
    items = "".join(f'<li><a href="{sp._e(href)}">{sp._e(label)}</a></li>'
                    for href, label in (_link_label(lang, n) for n in names))
    return f"<section class='reveal sub'><h2>{sp._e(t('seo.lib.related'))}</h2><ul class=links>{items}</ul></section>"


def _updated_line(lang: str, page: Page) -> str:
    iso = sp.page_updated(page)
    return (f'<p class="muted updated"><time datetime="{iso}">'
            f'{sp._e(sp._t(lang)("seo.updated", date=sp.human_date(iso, lang)))}</time></p>')


# ---------- заголовки ----------


def muscle_name(url: str, lang: str) -> str:
    group, target = _MUSCLE_BY_URL[url]
    if target:
        return seed_data.localized_muscle_target_name(target, lang)
    return seed_data.localized_muscle_group_name(group, lang)


def _second(name: str, lang: str) -> str:
    """Второе имя в «A или B» — со строчной: «Жим лёжа или жим гантелей».
    Аббревиатуры («EZ-гриф») и английский не трогаем."""
    if lang == "ru" and len(name) > 1 and name[0].isupper() and name[1].islower():
        return name[0].lower() + name[1:]
    return name


def _ex_name(url: str, lang: str) -> str:
    return seed_data.localized_exercise_name(sp._identity_for_media_slug(sp._EXERCISE_BY_URL[url]), lang)


def page_h1(page: Page, lang: str) -> str:
    t = sp._t(lang)
    k = page.kind
    if k in ("muscles", "compare", "glossary", "guides", "about"):
        return t(f"seo.{k}.h1")
    if k == "muscle":
        return t("seo.muscle.h1", name=muscle_name(page.arg, lang))
    if k == "pair":
        a, b = pair_exercises(page.arg)
        return t("seo.pair.h1", a=_ex_name(a, lang), b=_second(_ex_name(b, lang), lang))
    if k == "term":
        return t(f"seo.term.{page.arg}.h1")
    if k == "guide":
        return t(f"seo.guide.{page.arg}.h1")
    if k == "alternatives":
        return t(f"seo.alt.{page.arg}.h1")
    if k == "template":
        return t(f"seo.template.{page.arg}.h1")
    raise ValueError(k)


# ---------- мышцы ----------


def _sets_of(target: Optional[str]) -> Optional[int]:
    """«3×8–12» → 3, «5×5» → 5; схема без числа подходов — None."""
    match = re.match(r"^\s*(\d+)\s*[×x]", target or "")
    return int(match.group(1)) if match else None


def _share(identity: str, group: str, muscle_group: str, muscle_target: Optional[str]) -> float:
    """Доля подхода упражнения, которая идёт этой мышце, — правилом бота."""
    groups, targets = seed_data.volume_split(identity, None, group)
    if muscle_target:
        return targets.get(muscle_target, 0.0)
    return groups.get(muscle_group, 0.0)


def muscle_exercises(url: str) -> list[tuple[str, str]]:
    """Страницы техники, которые тренируют мышцу: (адрес, слаг) — главное
    движение первым (доля 1, потом половинные вроде становой)."""
    group, target = _MUSCLE_BY_URL[url]
    scored = []
    for i, (ex_url, slug) in enumerate(sp.EXERCISES):
        identity = sp._identity_for_media_slug(slug)
        share = _share(identity, sp._group_of(identity), group, target)
        if share > 0:
            scored.append((-share, i, ex_url, slug))
    return [(u, s) for _sh, _i, u, s in sorted(scored)]


def muscle_catalog(url: str, lang: str) -> list[str]:
    """Остальные упражнения каталога бота на эту мышцу (без своей страницы)."""
    group, target = _MUSCLE_BY_URL[url]
    with_pages = {sp._identity_for_media_slug(s) for _u, s in sp.EXERCISES}
    names = []
    for g, identity in seed_data.EXERCISE_TEMPLATES:
        if seed_data.is_catalog_hidden(identity) or identity in with_pages:
            continue
        if _share(identity, g, group, target) > 0:
            names.append(seed_data.localized_exercise_name(identity, lang))
    return names


def muscle_programs(url: str) -> list[tuple[str, float, int]]:
    """(программа, подходов на мышцу за цикл, дней в цикле) — где больше,
    те выше; программы без подходов на мышцу не попадают."""
    group, target = _MUSCLE_BY_URL[url]
    out = []
    for p in seed_data.WORKOUT_PROGRAMS:
        total = 0.0
        for _day, exercises in p["days"]:
            for identity, scheme in exercises:
                sets = _sets_of(scheme)
                if sets:
                    total += sets * _share(identity, sp._group_of(identity), group, target)
        if total > 0:
            out.append((p["key"], total, len(p["days"])))
    return sorted(out, key=lambda x: -x[1])


def _render_muscles(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    cards = []
    for url, _g, _tg in MUSCLES:
        n = len(muscle_exercises(url)) + len(muscle_catalog(url, lang))
        cards.append((sp.url_for(Page("muscle", url), lang), muscle_name(url, lang), t("seo.muscles.count", n=n)))
    title, description = t("seo.muscles.title"), t("seo.muscles.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{sp._e(t('seo.muscles.h1'))}</h1><p class=lead>{sp._e(t('seo.muscles.lead'))}</p>"
        + sp._cards(cards)
        + f"<section class='prose reveal'><h2>{sp._e(t('seo.muscles.how.h'))}</h2>{t('seo.muscles.how_html', **link_params(lang))}</section>"
        + sp._cta(lang, page.block, sp._e(t("seo.muscles.cta")), request)
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True,
                      ld=[sp._webpage_ld(lang, page, request, title, description, "CollectionPage")])


def _render_muscle(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    url = page.arg
    name = muscle_name(url, lang)
    params = link_params(lang)
    exercises = muscle_exercises(url)
    catalog = muscle_catalog(url, lang)
    tips = "".join(f"<li>{sp._e(x)}</li>" for x in t(f"seo.mus.{url}.tips").split("\n") if x.strip())
    grid = "".join(sp._exercise_card(u, s, lang) for u, s in exercises)
    progs = muscle_programs(url)
    prog_rows = "".join(
        f'<tr><th scope=row><a href="{sp.url_for(Page("program", k), lang)}">'
        f"{sp._e(sp._strip_emoji(seed_data.localized_program_name(k, lang)))}</a></th>"
        f"<td class=num>{sp._num(round(total, 1))}</td><td class=num>{days}</td></tr>"
        for k, total, days in progs
    )
    programs_html = ""
    if prog_rows:
        programs_html = (
            f"<section class='reveal sub'><h2>{sp._e(t('seo.muscle.programs.h', name=name))}</h2>"
            f"<div class=table-card><table><thead><tr><th>{sp._e(t('seo.muscle.col.program'))}</th>"
            f"<th class=num>{sp._e(t('seo.muscle.col.sets'))}</th><th class=num>{sp._e(t('seo.muscle.col.days'))}</th>"
            f"</tr></thead><tbody>{prog_rows}</tbody></table></div>"
            f"<p class=muted>{t('seo.muscle.programs.note_html', **params)}</p></section>"
        )
    pairs = [(a, b) for a, b in PAIRS if any(a == u or b == u for u, _s in exercises)]
    pairs_html = ""
    if pairs:
        items = "".join(
            f'<li><a href="{sp.url_for(Page("pair", pair_slug(a, b)), lang)}">'
            f"{sp._e(t('seo.pair.h1', a=_ex_name(a, lang), b=_second(_ex_name(b, lang), lang)))}</a></li>" for a, b in pairs)
        pairs_html = f"<section class='reveal sub'><h2>{sp._e(t('seo.muscle.pairs.h'))}</h2><ul class=links>{items}</ul></section>"
    catalog_html = ""
    if catalog:
        catalog_html = (f"<section class='reveal sub'><h2>{sp._e(t('seo.muscle.catalog.h', n=len(catalog)))}</h2>"
                        f"<ul class=chips>{''.join(f'<li>{sp._e(n)}</li>' for n in catalog)}</ul>"
                        f"<p class=muted>{sp._e(t('seo.muscle.catalog.note'))}</p></section>")
    title = t(f"seo.mus.{url}.title")
    description = t(f"seo.mus.{url}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.muscle'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.mus.{url}.lead'))}</p>"
        f"<section class='prose reveal'><h2>{sp._e(t('seo.muscle.about.h', name=name))}</h2>{t(f'seo.mus.{url}.about_html', **params)}</section>"
        + (f"<section class='reveal sub'><h2>{sp._e(t('seo.muscle.exercises.h', name=name))}</h2><div class=grid>{grid}</div></section>"
           if grid else "")
        + catalog_html
        + f"<section class='mistakes reveal'><h2>{sp._e(t('seo.muscle.tips.h'))}</h2><ul>{tips}</ul></section>"
        + programs_html
        + pairs_html
        + sp._cta(lang, page.block, sp._e(t("seo.muscle.cta", name=name)), request)
        + f'<p style="margin-top:32px"><a href="{sp.url_for(Page("muscles"), lang)}">{sp._e(t("seo.muscle.all"))}</a></p>'
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True,
                      ld=[sp._webpage_ld(lang, page, request, title, description,
                                         extra={"about": {"@type": "Thing", "name": name}})])


# ---------- упражнение против упражнения ----------


def _ex_muscles(url: str, lang: str) -> str:
    return sp._t(lang)(f"seo.exf.{url}.muscles")


def _pair_table(lang: str, a: str, b: str) -> str:
    t = sp._t(lang)
    params = link_params(lang)

    def cells(row: str) -> str:
        out = []
        for url in (a, b):
            equip, level = EX_FACTS[url]
            if row == "muscles":
                val = sp._e(_ex_muscles(url, lang))
            elif row == "equipment":
                val = sp._e(t(f"seo.equip.{equip}"))
            elif row == "level":
                val = f"<span class=lvl>{level}/3</span> {sp._e(t(f'seo.level.{level}'))}"
            else:
                val = t(f"seo.pair.{pair_slug(a, b)}.when_{'a' if url == a else 'b'}_html", **params)
            out.append(f"<td>{val}</td>")
        return "".join(out)

    rows = "".join(f"<tr><th scope=row>{sp._e(t(f'seo.pair.row.{r}'))}</th>{cells(r)}</tr>"
                   for r in ("muscles", "equipment", "level", "when"))
    head = "".join(f'<th><a href="{sp.url_for(Page("exercise", u), lang)}">{sp._e(_ex_name(u, lang))}</a></th>' for u in (a, b))
    return f"<div class='table-card vs'><table><thead><tr><th></th>{head}</tr></thead><tbody>{rows}</tbody></table></div>"


def _pair_frames(lang: str, a: str, b: str) -> str:
    t = sp._t(lang)
    figs = []
    for url in (a, b):
        slug = sp._EXERCISE_BY_URL[url]
        name = _ex_name(url, lang)
        figs.append(
            f'<figure><a href="{sp.url_for(Page("exercise", url), lang)}">'
            + sp.exercise_picture(slug, 1, t("seo.exercise.frame_start", name=name),
                                  sizes="(max-width: 600px) 50vw, 400px", widths=(360, 720), width=400, height=300)
            + "</a>"
            f"<figcaption>{sp._e(name)}</figcaption></figure>")
    return f"<div class=frames>{''.join(figs)}</div>"


def _render_pair(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    a, b = pair_exercises(page.arg)
    na, nb = _ex_name(a, lang), _ex_name(b, lang)
    params = link_params(lang)
    key = f"seo.pair.{page.arg}"
    in_programs = [p for p in seed_data.WORKOUT_PROGRAMS
                   if any(sp._exercise_url_for_identity(i) in (a, b) for _d, exs in p["days"] for i, _s in exs)]
    progs_html = ""
    if in_programs:
        progs_html = (f"<section class='reveal sub'><h2>{sp._e(t('seo.pair.programs.h'))}</h2>"
                      f"{sp.program_cards([p['key'] for p in in_programs], lang)}</section>")
    others = [(x, y) for x, y in PAIRS if (x, y) != (a, b) and {x, y} & {a, b}]
    others_html = ""
    if others:
        items = "".join(f'<li><a href="{sp.url_for(Page("pair", pair_slug(x, y)), lang)}">'
                        f"{sp._e(t('seo.pair.h1', a=_ex_name(x, lang), b=_second(_ex_name(y, lang), lang)))}</a></li>" for x, y in others)
        others_html = f"<section class='reveal sub'><h2>{sp._e(t('seo.pair.more.h'))}</h2><ul class=links>{items}</ul></section>"
    title = t("seo.pair.title", a=na, b=_second(nb, lang))
    description = t(f"{key}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.compare'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'{key}.lead'))}</p>"
        + _updated_line(lang, page)
        + _pair_frames(lang, a, b)
        + _pair_table(lang, a, b)
        + f"<div class=prose><section class='reveal sub'><h2>{sp._e(t('seo.pair.diff.h'))}</h2>{t(f'{key}.diff_html', **params)}</section>"
        + f"<section class='reveal sub'><h2>{sp._e(t('seo.pair.both.h'))}</h2>{t(f'{key}.both_html', **params)}</section></div>"
        + progs_html
        + sp._cta(lang, page.block, sp._e(t("seo.pair.cta")), request)
        + others_html
        + f'<p style="margin-top:32px"><a href="{sp.url_for(Page("compare"), lang)}">{sp._e(t("seo.pair.all"))}</a></p>'
    )
    base = sp.base_url(request)
    article = {
        "@context": "https://schema.org", "@type": "Article", "headline": title[:110], "description": description,
        "inLanguage": lang, "mainEntityOfPage": base + sp.url_for(page, lang),
        "image": [base + sp._media_url(f"{sp._EXERCISE_BY_URL[u]}_1.jpg") for u in (a, b)],
        "author": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "publisher": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "about": [{"@type": "Thing", "name": na}, {"@type": "Thing", "name": nb}],
    }
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True, ld=[article])


def _render_compare(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    pair_cards = [(sp.url_for(Page("pair", pair_slug(a, b)), lang), t("seo.pair.h1", a=_ex_name(a, lang), b=_second(_ex_name(b, lang), lang)),
                   t(f"seo.pair.{pair_slug(a, b)}.card")) for a, b in PAIRS]
    apps = [(sp.url_for(Page("vs", c), lang), t(f"seo.vs.{c}.h1"), t(f"seo.vs.{c}.card")) for c in sp.COMPETITORS]
    apps += [(sp.url_for(Page("alternatives", c), lang), t(f"seo.alt.{c}.h1"), t(f"seo.alt.{c}.card")) for c in sp.ALTERNATIVES]
    title, description = t("seo.compare.title"), t("seo.compare.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.compare'))}</span>"
        f"<h1>{sp._e(t('seo.compare.h1'))}</h1><p class=lead>{sp._e(t('seo.compare.lead'))}</p>"
        f"<h2 class=group-title>{sp._e(t('seo.compare.exercises.h'))}</h2>" + sp._cards(pair_cards)
        + f"<h2 class=group-title>{sp._e(t('seo.compare.apps.h'))}</h2>" + sp._cards(apps)
        + sp._cta(lang, page.block, sp._e(t("seo.compare.cta")), request)
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True,
                      ld=[sp._webpage_ld(lang, page, request, title, description, "CollectionPage")])


# ---------- словарь ----------


def _term_ld(lang: str, request, term: str) -> dict:
    t = sp._t(lang)
    base = sp.base_url(request)
    return {
        "@context": "https://schema.org", "@type": "DefinedTerm", "name": t(f"seo.term.{term}.name"),
        "description": t(f"seo.term.{term}.short"), "url": base + sp.url_for(Page("term", term), lang),
        "inDefinedTermSet": base + sp.url_for(Page("glossary"), lang), "inLanguage": lang,
    }


def _render_glossary(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    items = "".join(
        f'<div class="term reveal"><h2><a href="{sp.url_for(Page("term", term), lang)}">{sp._e(t(f"seo.term.{term}.name"))}</a></h2>'
        f"<p>{sp._e(t(f'seo.term.{term}.short'))}</p></div>" for term in GLOSSARY)
    title, description = t("seo.glossary.title"), t("seo.glossary.description")
    base = sp.base_url(request)
    ld = {"@context": "https://schema.org", "@type": "DefinedTermSet", "name": t("seo.glossary.h1"),
          "url": base + sp.url_for(page, lang), "inLanguage": lang,
          "hasDefinedTerm": [_term_ld(lang, request, term) for term in GLOSSARY]}
    for term in ld["hasDefinedTerm"]:
        term.pop("@context")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{sp._e(t('seo.glossary.h1'))}</h1><p class=lead>{sp._e(t('seo.glossary.lead'))}</p>"
        f"<div class=terms>{items}</div>"
        + sp._cta(lang, page.block, sp._e(t("seo.glossary.cta")), request)
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body,
                      ld=[sp._webpage_ld(lang, page, request, title, description, "CollectionPage"), ld])


def _render_term(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    term = page.arg
    params = link_params(lang)
    others = [x for x in GLOSSARY if x != term]
    chips = "".join(f'<li><a href="{sp.url_for(Page("term", x), lang)}">{sp._e(t(f"seo.term.{x}.name"))}</a></li>' for x in others)
    title, description = t(f"seo.term.{term}.title"), t(f"seo.term.{term}.short")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.glossary'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1>"
        f"<p class='lead definition'>{sp._e(t(f'seo.term.{term}.short'))}</p>"
        f"<section class=reveal>{t(f'seo.term.{term}.body_html', **params)}</section>"
        + _related(lang, TERM_LINKS[term])
        + sp._cta(lang, page.block, sp._e(t("seo.glossary.cta")), request)
        + f"<section><h2>{sp._e(t('seo.glossary.more.h'))}</h2><ul class=chips>{chips}</ul></section>"
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body,
                      ld=[sp._webpage_ld(lang, page, request, title, description), _term_ld(lang, request, term)])


# ---------- гайды ----------


def guide_text(slug: str, lang: str) -> str:
    """Весь текст статьи без разметки — для счёта слов (тест) и llms-full."""
    t = sp._t(lang)
    params = link_params(lang)
    parts = [t(f"seo.guide.{slug}.lead")]
    for i in range(1, GUIDE_SECTIONS[slug] + 1):
        parts += [t(f"seo.guide.{slug}.s{i}.h"), t(f"seo.guide.{slug}.s{i}_html", **params)]
    parts.append(t(f"seo.guide.{slug}.takeaways", **params))
    return sp._plain(" ".join(parts))


def _render_guides(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    cards = [(sp.url_for(Page("guide", g), lang), t(f"seo.guide.{g}.h1"), t(f"seo.guide.{g}.card")) for g in GUIDES]
    title, description = t("seo.guides.title"), t("seo.guides.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.library'))}</span>"
        f"<h1>{sp._e(t('seo.guides.h1'))}</h1><p class=lead>{sp._e(t('seo.guides.lead'))}</p>"
        + sp._cards(cards)
        + f'<p><a href="{sp.url_for(Page("glossary"), lang)}">{sp._e(t("seo.guides.glossary_link"))} →</a></p>'
        + sp._cta(lang, page.block, sp._e(t("seo.guides.cta")), request)
    )
    return sp._layout(lang, page, request, title=title, description=description, body=body, wide=True,
                      ld=[sp._webpage_ld(lang, page, request, title, description, "CollectionPage")])


def _render_guide(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    slug = page.arg
    params = link_params(lang)
    sections = "".join(
        f"<section class='reveal sub'><h2>{sp._e(t(f'seo.guide.{slug}.s{i}.h'))}</h2>{t(f'seo.guide.{slug}.s{i}_html', **params)}</section>"
        for i in range(1, GUIDE_SECTIONS[slug] + 1))
    takeaways = "".join(f"<li>{sp._e(x)}</li>" for x in t(f"seo.guide.{slug}.takeaways", **params).split("\n") if x.strip())
    sources = GUIDE_SOURCES[slug]
    sources_html = ""
    if sources:
        items = "".join(f'<li><a href="{sp._e(u)}" rel=nofollow>{sp._e(label)}</a></li>' for label, u in sources)
        sources_html = f"<section><h2>{sp._e(t('seo.guide.sources.h'))}</h2><ol class='muted sources'>{items}</ol></section>"
    title, description = t(f"seo.guide.{slug}.title"), t(f"seo.guide.{slug}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.guide'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.guide.{slug}.lead'))}</p>"
        + _updated_line(lang, page)
        + f"<aside class='takeaways reveal'><h2>{sp._e(t('seo.guide.takeaways.h'))}</h2><ul>{takeaways}</ul></aside>"
        + sections
        + sp._cta(lang, page.block, sp._e(t(f"seo.guide.{slug}.cta")), request)
        + _related(lang, GUIDE_LINKS[slug])
        + sources_html
    )
    base = sp.base_url(request)
    article = {
        "@context": "https://schema.org", "@type": "Article", "headline": t(f"seo.guide.{slug}.h1")[:110],
        "description": description, "inLanguage": lang, "mainEntityOfPage": base + sp.url_for(page, lang),
        "datePublished": sp.page_updated(page), "wordCount": len(guide_text(slug, lang).split()),
        "author": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "publisher": {"@id": base + sp.ORG_ID, "@type": "Organization", "name": t("seo.brand")},
        "image": base + f"/og/{lang}/{page.key}.png",
    }
    if sources:
        article["citation"] = [{"@type": "CreativeWork", "name": label, "url": u} for label, u in sources]
    return sp._layout(lang, page, request, title=title, description=description, body=body, ld=[article])


# ---------- альтернативы ----------


def _render_alternatives(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    rival = page.arg
    params = link_params(lang)
    items = []
    for i, app in enumerate(ALT_APPS[rival], start=1):
        cls = " us" if app == "us" else ""
        extra = ""
        if app in sp.COMPETITORS and app != rival:
            extra = f'<p><a href="{sp.url_for(Page("vs", app), lang)}">{sp._e(t("seo.alt.vs_link", name=t(f"seo.vs.{app}.name")))} →</a></p>'
        if app == "sheet":
            extra = f'<p><a href="{params["template"]}">{sp._e(t("seo.alt.sheet_link"))} →</a></p>'
        items.append(
            f'<li class="alt{cls} reveal"><div class=alt-head><span class=alt-n>{i}</span>'
            f"<h2>{sp._e(t(f'seo.alt.app.{app}.name'))}</h2></div>"
            f"<p class=alt-price><b>{sp._e(t('seo.alt.price'))}</b> {t(f'seo.alt.app.{app}.price_html', **params)}</p>"
            f"{t(f'seo.alt.app.{app}.what_html', **params)}"
            f"<p class=alt-fit><b>{sp._e(t('seo.alt.fit'))}</b> {t(f'seo.alt.app.{app}.fit_html', **params)}</p>{extra}</li>")
    sources = []
    for app in ALT_APPS[rival]:
        sources += list(sp._VS_SOURCES.get(app, ())) + list(ALT_SOURCES.get(app, ()))
    sources_html = "".join(f'<li><a href="{sp._e(u)}" rel=nofollow>{sp._e(u)}</a></li>' for u in dict.fromkeys(sources))
    title, description = t(f"seo.alt.{rival}.title"), t(f"seo.alt.{rival}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.compare'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'seo.alt.{rival}.lead'))}</p>"
        f"<p class=muted>{sp._e(t('seo.vs.asof'))}</p>"
        f"<section class='reveal sub'><h2>{sp._e(t('seo.alt.why.h', name=t(f'seo.vs.{rival}.name')))}</h2>{t(f'seo.alt.{rival}.why_html', **params)}</section>"
        f"<ol class=alts>{''.join(items)}</ol>"
        f"<section class='reveal sub'><h2>{sp._e(t('seo.vs.move.h'))}</h2>{t(f'seo.alt.{rival}.move_html', **params)}</section>"
        + sp._cta(lang, page.block, sp._e(t("seo.alt.cta")), request)
        + f"<section><h2>{sp._e(t('seo.vs.sources.h'))}</h2><ul class=muted>{sources_html}</ul>"
        f"<p class=muted>{sp._e(t('seo.alt.disclaimer'))}</p></section>"
    )
    ld = sp._webpage_ld(lang, page, request, title, description, extra={
        "mainEntity": {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": i, "name": t(f"seo.alt.app.{app}.name")}
            for i, app in enumerate(ALT_APPS[rival], start=1)]},
    })
    return sp._layout(lang, page, request, title=title, description=description, body=body, ld=[ld])


# ---------- шаблон дневника ----------


def _render_template(lang: str, page: Page, request) -> str:
    import seo_template

    t = sp._t(lang)
    key = f"seo.template.{page.arg}"
    params = link_params(lang)
    heads = seo_template.headers(lang)
    col_rows = "".join(
        f"<tr><th scope=row><code>{sp._e(h)}</code></th><td>{sp._e(t(f'seo.template.colhelp.{c}'))}</td></tr>"
        for c, h in zip(seo_template.COLUMNS, heads, strict=True))
    example = seo_template.example_rows(lang)[:5]
    ex_head = "".join(f"<th>{sp._e(h)}</th>" for h in heads)
    ex_rows = "".join("<tr>" + "".join(f"<td>{sp._e(v)}</td>" for v in row) + "</tr>" for row in example)
    labels = {"workout-log.xlsx": "xlsx", "workout-log.csv": "csv", "workout-log-example.csv": "example"}
    downloads = "".join(
        f'<a class="btn{" primary" if i == 0 else ""}" href="/{lang}/templates/{name}" download>'
        f'{seo_design.icon("import")}{sp._e(t("seo.template.dl." + labels[name]))}</a>'
        for i, name in enumerate(seo_template.FILES))
    upload = (f'<div class="cta reveal upload"><p class=cta-text>{sp._e(t(f"{key}.upload_cta"))}</p>'
              f"{t(f'{key}.upload_html', **params)}"
              f"{sp._cta_buttons(lang, page.block, sp.device_of(request))}</div>")
    faq = sp._faq_items(lang, sp.FAQ["template"])
    title, description = t(f"{key}.title"), t(f"{key}.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.template'))}</span>"
        f"<h1>{sp._e(page_h1(page, lang))}</h1><p class=lead>{sp._e(t(f'{key}.lead'))}</p>"
        f"<div class='btn-row downloads'>{downloads}</div>"
        f"<p class=muted>{sp._e(t(f'{key}.files_note'))}</p>"
        + upload
        + f"<section class='reveal sub'><h2>{sp._e(t(f'{key}.columns.h'))}</h2><div class=table-card><table><tbody>{col_rows}</tbody></table></div>"
        f"<p class=muted>{t(f'{key}.columns_note_html', **params)}</p></section>"
        + f"<section class='reveal sub'><h2>{sp._e(t(f'{key}.example.h'))}</h2><div class='table-card scroll-x nowrap'><table><thead><tr>{ex_head}</tr></thead>"
        f"<tbody>{ex_rows}</tbody></table></div></section>"
        + f"<section class='reveal sub'><h2>{sp._e(t(f'{key}.excel.h'))}</h2>{t(f'{key}.excel_html', **params)}</section>"
        + f"<section class='reveal sub'><h2>{sp._e(t(f'{key}.sheets.h'))}</h2>{t(f'{key}.sheets_html', **params)}</section>"
        + f"<section class='reveal sub'><h2>{sp._e(t(f'{key}.why.h'))}</h2>{t(f'{key}.why_html', **params)}</section>"
        + sp._faq_html(lang, faq)
        + sp._cta(lang, page.block, sp._e(t(f"{key}.cta")), request)
    )
    ld = sp._webpage_ld(lang, page, request, title, description, extra={
        "mainEntity": {"@type": "DigitalDocument", "name": t(f"{key}.h1"), "encodingFormat": [
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"],
            "url": sp.base_url(request) + f"/{lang}/templates/workout-log.xlsx"},
    })
    return sp._layout(lang, page, request, title=title, description=description, body=body, ld=[ld, sp._faq_ld(faq)])


# ---------- о проекте ----------


def _render_about(lang: str, page: Page, request) -> str:
    t = sp._t(lang)
    params = dict(link_params(lang), developer=sp.DEVELOPER)
    sections = "".join(f"<section class='reveal sub'><h2>{sp._e(t(f'seo.about.s{i}.h'))}</h2>{t(f'seo.about.s{i}_html', **params)}</section>"
                       for i in range(1, 6))
    title, description = t("seo.about.title"), t("seo.about.description")
    body = (
        f"<span class=eyebrow>{sp._e(t('seo.eyebrow.about'))}</span>"
        f"<h1>{sp._e(t('seo.about.h1'))}</h1><p class=lead>{sp._e(t('seo.about.lead'))}</p>"
        + sections + _related(lang, ABOUT_LINKS)
        + sp._cta(lang, page.block, sp._e(t("seo.about.cta")), request)
    )
    ld = sp._webpage_ld(lang, page, request, title, description, "AboutPage",
                        {"mainEntity": {"@id": sp.base_url(request) + sp.ORG_ID}})
    return sp._layout(lang, page, request, title=title, description=description, body=body,
                      ld=[ld, sp.organization_ld(lang, request)])


RENDERERS = {
    "muscles": _render_muscles, "muscle": _render_muscle,
    "compare": _render_compare, "pair": _render_pair,
    "glossary": _render_glossary, "term": _render_term,
    "guides": _render_guides, "guide": _render_guide,
    "alternatives": _render_alternatives, "template": _render_template, "about": _render_about,
}


def register_routes(server) -> None:
    import seo_calcs

    seo_calcs.register_routes(server)
