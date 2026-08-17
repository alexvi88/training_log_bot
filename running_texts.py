"""Placeholder-фразы для «тренер думает», подобранные под тему вопроса.

Пока ai_trainer.ask ждёт ответ модели (секунды, иногда десятки секунд — особенно
с tool-calls и веб-поиском под капотом), handlers/ai_trainer.py крутит в
placeholder-сообщении одну из фраз ниже. Раньше пул был один общий на все
случаи — вопрос про питание крутил "гружу знания, как штангу", вопрос про
программу — "остываю от подхода": фраза никак не намекала, о чём вообще спросили.

Здесь фразы разложены по темам, а тема угадывается по ключевым словам-стемам —
тем же приёмом, что и в exercise_mentions.py: текст фолдится в нижний регистр,
"ё" нормализуется в "е" (иначе "объём"/"объем" разъедутся), слово считается
попаданием в тему, если оно начинается с одного из стемов темы. Это не полный
стеммер (see exercise_mentions.find_mentions — там другая задача, сопоставление
двух конкретных названий упражнений друг с другом), а грубая, но дешёвая
классификация: нам не нужна точность, нужно, чтобы самая первая фраза, которую
человек видит ещё до единого tool-call, уже была в тему.

Темы проверяются по порядку в _TOPIC_STEMS — если текст можно отнести к
нескольким (например, «программа на неделю» — и PROGRAM, и WEEKLY_VOLUME), то
побеждает та, что стоит раньше в списке. Фарма и боль/травмы проверяются
первыми: жалоба на боль после жима — это про восстановление, а не про прогресс
в жиме, а «сколько скинул на оземпике» — про препарат, а не про дневник веса
(стем «масс» там тоже найдётся). Если ни один стем не подошёл — DEFAULT_TOPIC,
тот самый универсальный пул, почти не изменившийся с исходной версии.

Важное про сами фразы: пул выбирается ДО первого вызова инструмента, то есть по
догадке из слов вопроса. Поэтому фраза не должна утверждать, что бот уже куда-то
залез или что у человека что-то есть, — иначе на неверно угаданной теме она
врёт про данные (TONE_OF_VOICE.md: утверждение о данных отправляется только
когда данные его подтверждают). Так и вышло на пересланном посте про
семаглутид: «ищу, не слишком ли быстро уходит или приходит вес» — а бот не
смотрел ни в один дневник. Проверенные фразы про конкретный инструмент живут
отдельно, в ai_trainer._TOOL_RUNNING_TEXTS: они показываются, когда инструмент
реально вызван, и утверждать там можно.

Третий регистр персонажа (TONE_OF_VOICE.md: строчные с эмодзи) — единственный
регистр этого модуля, английская версия тоже строчная и с эмодзи, не капс и не
проза. `pool_for` берёт язык из `i18n.get_lang()` — того же контекста, который
handlers/ai_trainer.py уже выставляет к моменту вызова (middleware ставит язык
на весь апдейт), а не из текста вопроса: вопрос может быть на любом языке
(«how much protein do I need»), а показывать плейсхолдер всё равно нужно на
языке интерфейса пользователя. `classify` при этом смотрит и русские, и
английские стемы сразу — англоязычный вопрос обязан попадать в свою тему, а не
скатываться в DEFAULT_TOPIC только потому, что показывать фразу будем
по-английски.

FACT_CHECK_POOL — исключение: handlers/factcheck.py берёт его напрямую
(`running_texts.pick(running_texts.FACT_CHECK_POOL)`), без i18n вообще, так что
FACT_CHECK_POOL_EN пока не подключён — это точечный экран вне области этой
правки (handlers/*), останется мимо каталога до его собственной локализации.
"""

import random
import re
from typing import Optional

import i18n

_WORD_RE = re.compile(r"[а-яa-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("ё", "е"))


PROGRAM = "program"
EXERCISE_PROGRESS = "exercise_progress"
WEEKLY_VOLUME = "weekly_volume"
NUTRITION = "nutrition"
BODYWEIGHT = "bodyweight"
TECHNIQUE = "technique"
RECOVERY = "recovery"
HISTORY = "history"
TODAY = "today"
MOTIVATION = "motivation"
PHARMA = "pharma"
SUPPLEMENTS = "supplements"
EQUIPMENT = "equipment"
SLEEP = "sleep"
WARMUP = "warmup"
DEFAULT_TOPIC = "default"

# Порядок — приоритет при пересечении тем (см. модульный докстринг). Стемы
# смешивают русский и английский в одном кортеже — _tokens уже фолдит любой
# текст в нижний регистр, а вопрос на английском должен попадать в свою тему
# ровно так же, как вопрос на русском (см. докстринг про i18n.get_lang()).
_TOPIC_STEMS: list[tuple[str, tuple[str, ...]]] = [
    # Фарма — раньше веса и питания намеренно: «сколько скинул на оземпике» это
    # вопрос про препарат, а не про дневник взвешиваний, хотя стем «масс» там
    # тоже найдётся. Ровно на этом пул про вес и вылез на пересланном посте про
    # семаглутид.
    (PHARMA, (
        "оземпик", "семаглутид", "тирзепатид", "мунджаро", "лираглутид",
        "стероид", "тестостерон", "анабол", "аас", "сарм", "гормон",
        "тренболон", "станозолол", "нандролон", "фарм", "инсулин",
        "ozempic", "semaglutide", "tirzepatide", "mounjaro", "liraglutide",
        "steroid", "testosterone", "anabolic", "sarm", "hormone",
        "trenbolone", "stanozolol", "nandrolone", "insulin",
    )),
    (RECOVERY, (
        "боли", "больно", "болит", "болел", "болев", "травм", "растяжен",
        "потян", "восстановлен", "перетрен", "устал", "надорв",
        "pain", "hurt", "sore", "injur", "strain", "sprain", "recover",
        "overtrain", "exhaust",
    )),
    (SLEEP, (
        "сон", "сна", "спать", "спл", "выспа", "недосып", "бессонниц", "циркадн",
        "sleep", "insomnia", "circadian",
    )),
    (SUPPLEMENTS, (
        "креатин", "протеин", "гейнер", "бцаа", "bcaa", "добавк", "витамин",
        "омега", "предтрен", "изолят", "казеин", "цитруллин", "бета-алан",
        "creatine", "whey", "preworkout", "casein", "citrulline",
    )),
    (NUTRITION, (
        "питан", "калори", "белк", "углевод", "рацион", "диет", "бжу",
        "nutrition", "calorie", "protein", "carb", "macro",
    )),
    (WARMUP, (
        "разминк", "разминат", "разогрев", "растяжк", "растягив", "заминк",
        "мобильн", "миофасц",
        "warmup", "warm", "stretch", "mobility",
    )),
    (EQUIPMENT, (
        "заменит", "замена", "заменять", "вместо", "дома", "домашн",
        "инвентар", "оборудован", "гантел", "резинк", "турник", "тренажер",
        "replac", "substitut", "instead", "equipment", "dumbbell", "band",
    )),
    (BODYWEIGHT, (
        "сушк", "похуд", "взвеш", "набира", "масс", "вешу",
        "cutting", "bulking", "weigh",
    )),
    (PROGRAM, (
        "программ", "сплит", "мезоцикл", "макроцикл", "периодизац",
        "program", "split", "mesocycle", "macrocycle", "periodiz",
    )),
    (WEEKLY_VOLUME, (
        "объем", "перегруж", "недел", "баланс", "равномер",
        "volume", "overload", "weekly", "balanc",
    )),
    (EXERCISE_PROGRESS, (
        "прогресс", "рекорд", "плато", "максимум", "вырос", "увелич", "1пм", "e1rm",
        "progress", "record", "plateau", "increase",
    )),
    (TECHNIQUE, (
        "техник", "правильн", "ошибк", "выполня",
        "technique", "form", "mistake", "perform",
    )),
    (HISTORY, (
        "истори", "статистик", "раньше", "прошл",
        "histor", "stat", "before", "past",
    )),
    (TODAY, (
        "сегодня", "сейчас", "щас",
        "today", "now",
    )),
    (MOTIVATION, (
        "мотивац", "лень", "вдохнов", "смысл", "надоел",
        "motivat", "lazy", "inspir",
    )),
]

# ---------- пулы фраз ----------

PROGRAM_POOL = [
    "📋 верстаю программу, дай прикинуть периодизацию...",
    "🗂️ раскладываю тренировки по неделям, момент...",
    "🧮 считаю сплит так, чтобы группы не спорили за отдых...",
    "🏗️ строю программу с нуля, почти готово...",
    "📐 подгоняю план под твои вводные, секунду...",
    "🔄 балансирую фазы нагрузки и отдыха в программе...",
    "📅 расставляю тренировочные дни по календарю...",
    "🧩 собираю программу из твоих упражнений, как пазл...",
    "⚙️ настраиваю прогрессию по неделям, момент...",
    "🎯 целюсь в программу под твою цель, не сбивай...",
    "📖 сверяюсь с методикой периодизации, секунду...",
    "🧱 закладываю фундамент будущей программы...",
]

EXERCISE_PROGRESS_POOL = [
    "📈 поднимаю графики прогресса по упражнению...",
    "🏋️ сверяю твои веса и повторы за последние недели...",
    "🔍 ищу, где прибавка, а где плато...",
    "📊 считаю e1RM, чтобы не гадать на глаз...",
    "🥇 смотрю, не близко ли новый рекорд...",
    "🧮 сравниваю текущие рабочие веса с прошлыми...",
    "📏 меряю прогресс не по ощущениям, а по цифрам...",
    "🔁 листаю историю подходов по этому упражнению...",
    "🧭 ищу, куда именно двигать вес дальше...",
    "⚖️ сверяю силовые показатели, момент...",
    "🎯 целюсь точным ответом про твой прогресс...",
    "🚧 проверяю, не плато ли это, или просто отдохнуть надо...",
]

WEEKLY_VOLUME_POOL = [
    "📆 считаю, сколько подходов в неделю выходит на группу...",
    "⚖️ проверяю баланс нагрузки между мышечными группами...",
    "🧮 суммирую недельный объём, секунду...",
    "🔍 ищу, какая группа перегружена, а какая недогружена...",
    "📊 раскладываю тренировки по неделе, чтобы видеть картину...",
    "🗓️ смотрю, как распределена нагрузка по дням...",
    "🧭 ищу перекос в объёме между группами мышц...",
    "⚙️ сверяю частоту тренировок на каждую группу...",
    "📐 считаю, не перебор ли с объёмом на этой неделе...",
    "🔄 смотрю, равномерно ли грузим тело за неделю...",
    "🧱 складываю недельную нагрузку по кирпичикам...",
    "📋 свожу объём по группам в одну картину...",
]

NUTRITION_POOL = [
    "🍽 смотрю, что у тебя в дневнике питания...",
    "🥩 считаю белок, чтобы расти, а не только качаться...",
    "🍗 заряжаюсь белком мысли перед ответом про еду...",
    "🥗 раскладываю рацион по БЖУ, момент...",
    "🍚 прикидываю калорийность под твою цель...",
    "🥤 сверяюсь с диетологией, а не только с качалкой...",
    "🧮 считаю калории, секунду...",
    "🍳 разбираюсь, хватает ли тебе белка на массу...",
    "🥦 смотрю баланс еды и тренировок...",
    "🍖 прикидываю, чем кормить растущие мышцы...",
    "🧾 сверяюсь с твоим дневником еды, момент...",
    "🥣 считаю, сходится ли рацион с целью...",
]

BODYWEIGHT_POOL = [
    "⚖️ смотрю дневник веса, момент...",
    "📉 слежу за динамикой веса — сушка или набор...",
    "🧮 считаю, куда движется масса тела...",
    "📊 сверяю вес по неделям, без резких скачков...",
    "🥩 прикидываю, сушка это или честный набор...",
    "🧭 ищу тренд по взвешиваниям...",
    "📆 смотрю, как менялся вес за последнее время...",
    "⚖️ взвешиваю твои данные, буквально...",
    "🔍 ищу, не слишком ли быстро уходит или приходит вес...",
    "🧱 складываю историю взвешиваний в картину...",
    "📈 слежу, растёт масса или тает жир...",
    "🎯 целюсь в честный ответ про вес тела...",
]

TECHNIQUE_POOL = [
    "🎥 прокручиваю технику упражнения в голове...",
    "🩻 разбираю движение по фазам, момент...",
    "🧠 вспоминаю тренерские нюансы по технике...",
    "🛠️ ищу, где в технике закралась ошибка...",
    "📐 проверяю траекторию движения, секунду...",
    "🧭 ищу правильную амплитуду, не гони...",
    "🎯 целюсь в точный разбор техники...",
    "🩹 ищу, не там ли риск травмы в движении...",
    "📖 сверяюсь с методикой выполнения...",
    "🧩 собираю технику по деталям, как надо...",
    "🏋️ мысленно прогоняю подход, как эталонный...",
    "🔍 ищу слабое звено в технике...",
]

RECOVERY_POOL = [
    "🩹 разбираюсь, что там с болью, момент...",
    "🧊 остываю сам, пока думаю про восстановление...",
    "🛌 прикидываю, сколько тебе на самом деле нужно отдыха...",
    "🚿 после подхода думается чётче, секунду...",
    "🧘 не гони, разбираю восстановление по порядку...",
    "🔍 ищу, не перетрен ли это...",
    "🩺 аккуратно разбираю, что могло заболеть...",
    "🧯 тушу тревогу — сейчас разберёмся с болью...",
    "🧭 ищу баланс между нагрузкой и отдыхом...",
    "🛁 думаю про восстановление не спеша...",
    "🧠 взвешиваю, тренироваться дальше или дать телу отдохнуть...",
    "🩹 собираю ответ по косточкам, без резких движений...",
]

HISTORY_POOL = [
    "📚 листаю всю историю твоих тренировок...",
    "🗂️ поднимаю архив тренировок, момент...",
    "📖 перечитываю, что было на прошлых тренировках...",
    "🔍 ищу в истории то, что тебе нужно...",
    "🧾 сверяюсь со старыми записями...",
    "📅 листаю тренировки по датам...",
    "🗃️ копаюсь в архиве, секунду...",
    "📊 свожу старые тренировки в одну картину...",
    "🧭 ищу нужный момент в твоей истории...",
    "📚 пролистываю прошлые тренировки назад...",
    "🔎 копаюсь в записях, момент...",
    "🗒️ поднимаю старые заметки о тренировках...",
]

# Фразы тут намеренно не обещают ни активной тренировки, ни сохранённой
# программы: пул выбирается по слову «сегодня» в вопросе, а «что сегодня
# качать» чаще всего спрашивают как раз ДО тренировки и нередко без программы
# вообще. «Проверяю активную тренировку...» в этом случае — заявление о данных,
# которых нет (TONE_OF_VOICE.md: утверждение о данных отправляется только
# когда данные его подтверждают).
TODAY_POOL = [
    "📋 прикидываю, что тебе сегодня делать...",
    "🏋️ соображаю, чем тебя сегодня занять...",
    "🎯 целюсь в ответ прямо на сегодня...",
    "🔍 разбираюсь, что сегодня уместно...",
    "⏱️ прикидываю, что делать прямо сейчас...",
    "📆 думаю, что логично поставить на сегодня...",
    "🧭 ищу, куда двигаться именно сегодня...",
    "🗓️ раскидываю сегодняшний день, секунду...",
    "🏋️ подбираю, что зайдёт сегодня...",
    "⚡ собираю быстрый ответ на сейчас...",
    "🎯 фокусируюсь на сегодняшнем дне...",
    "📋 прикидываю сегодняшнюю нагрузку, момент...",
]

MOTIVATION_POOL = [
    "🔥 разжигаю мотивацию, момент...",
    "🧠 включаю тренерский мозг для доброго пинка...",
    "💬 подбираю слова поддержки, не гони...",
    "🥊 готовлю ответ, который встряхнёт...",
    "🧘 собираю мысли для честного разговора...",
    "🗿 стою как штанга — думаю тяжело, но верно...",
    "🎯 целюсь в слова, которые реально помогут...",
    "🔋 заряжаюсь энергией для ответа...",
    "🧢 не гони, тренер думает медленно, но метко...",
    "💪 держи паузу, сейчас будет по делу...",
    "🧠 ищу, чем тебя реально зацепить...",
    "🔥 собираю ответ, который вернёт огонь...",
]

PHARMA_POOL = [
    "🧪 разбираюсь с препаратом, тут спешить нельзя...",
    "📖 поднимаю, что известно про этот препарат...",
    "⚖️ взвешиваю пользу против побочек, момент...",
    "🩺 думаю как тренер, который читал не только форумы...",
    "🧠 собираю ответ без сказок и без страшилок...",
    "📉 прикидываю, что там на самом деле уходит — жир или мышцы...",
    "🚦 разбираю, где тут край, а где уже за краем...",
    "🧾 вспоминаю дозировки и что с ними обычно не так...",
    "🔬 отделяю доказанное от обещанного...",
    "🩹 думаю про последствия, а не только про результат...",
    "🧯 без паники, сейчас разложу по фактам...",
    "🎯 целюсь в честный ответ, а не в удобный...",
]

SUPPLEMENTS_POOL = [
    "🥤 разбираюсь, работает эта добавка или маркетинг...",
    "🧪 вспоминаю, что по ней реально показали исследования...",
    "💊 прикидываю, нужна она тебе вообще или нет...",
    "📖 сверяюсь с тем, что известно по дозировкам...",
    "⚖️ взвешиваю пользу против цены, момент...",
    "🥩 думаю, не решается ли это обычной едой...",
    "🔬 отделяю рабочее от модного...",
    "🧾 вспоминаю, с чем её обычно путают...",
    "🧠 собираю ответ без магии порошков...",
    "🚦 разбираю, стоит ли оно места в шкафу...",
    "🎯 целюсь в короткий ответ: надо или не надо...",
    "🍶 думаю, что из этого правда, а что этикетка...",
]

EQUIPMENT_POOL = [
    "🔧 подбираю, чем это заменить...",
    "🏠 прикидываю вариант без зала, момент...",
    "🧰 перебираю, что можно сделать с тем, что есть...",
    "🏋️ ищу движение с той же работой мышц...",
    "🪑 думаю, из чего собрать замену...",
    "🧩 подгоняю упражнение под твой инвентарь...",
    "📐 сверяю, тот ли угол и та ли амплитуда у замены...",
    "🔁 ищу аналог, а не просто похожее название...",
    "💡 придумываю, чем закрыть эту мышцу иначе...",
    "🎯 целюсь в замену, которая правда работает...",
    "🧠 вспоминаю, чем это обычно заменяют...",
    "⚙️ прикидываю, что даст ту же нагрузку...",
]

SLEEP_POOL = [
    "😴 думаю про сон — он в тренировках не мелочь...",
    "🌙 прикидываю, сколько тебе на самом деле надо спать...",
    "🛌 разбираюсь, как недосып бьёт по силе...",
    "⏰ думаю про режим, а не только про железо...",
    "🧠 вспоминаю, что со сном происходит с восстановлением...",
    "📉 прикидываю, во сколько тебе обходится недосып...",
    "🕯️ собираю ответ спокойно, как перед сном...",
    "☕ разбираюсь, при чём тут кофеин...",
    "🌒 думаю про циклы сна, момент...",
    "🎯 целюсь в совет, который реально выполним...",
    "🧘 не гони, тут ответ не в две строки...",
    "🛏️ думаю, где ты теряешь восстановление...",
]

WARMUP_POOL = [
    "🤸 прикидываю разминку под это движение...",
    "🔥 разогреваюсь вместе с тобой, момент...",
    "🧵 разбираюсь, что тут тянуть, а что не надо...",
    "📐 думаю, какие суставы готовить первыми...",
    "⏱️ прикидываю, сколько на это нужно минут...",
    "🦵 вспоминаю, что чаще всего забывают размять...",
    "🧠 собираю разминку, а не ритуал...",
    "🚦 думаю, где растяжка помогает, а где мешает...",
    "🩹 прикидываю, что уберёт риск на первом подходе...",
    "🎯 целюсь в короткую разминку по делу...",
    "🔄 подбираю подводящие подходы...",
    "🧩 складываю разминку под твоё упражнение...",
]

# Разбор пересланного поста (handlers/factcheck.py). Отдельный пул, а не тема
# по ключевым словам: тут заведомо известно, что происходит, — читаем чужой
# текст, а не свои данные. Ровно поэтому фразы говорят про пост, а не про
# дневники: угаданный по словам поста пул («ищу, не слишком ли быстро уходит
# вес» на посте про семаглутид) обещал заглянуть туда, куда бот не смотрел.
FACT_CHECK_POOL = [
    "🧐 гляну, что там понаписали...",
    "📖 читаю пост, момент...",
    "🔍 проверяю, где тут дело, а где красивые слова...",
    "🧪 отделяю факты от обещаний...",
    "⚖️ взвешиваю, что из этого правда...",
    "🤨 смотрю на это скептически, как положено...",
    "🧠 сверяю с тем, что знаю сам...",
    "🚦 разбираю по пунктам: верно, натянуто, мимо...",
    "📌 отмечаю, к чему тут стоит присмотреться...",
    "🎯 целюсь в честный вердикт...",
    "🧾 разбираю чужой совет по косточкам...",
    "🔬 проверяю, не выдумка ли это...",
]

DEFAULT_POOL = [
    "💪 держи паузу, сейчас будет по делу...",
    "🧠 включаю тренерский мозг, момент...",
    "🔥 разминаюсь перед ответом...",
    "🎯 целюсь в точный совет, не спугни...",
    "🧘 собираю мысли, не гони...",
    "🏋️ гружу знания, как штангу — по чуть-чуть...",
    "📖 сверяюсь с методикой, секунду...",
    "⏱️ отдыхаю между подходами мысли, погоди...",
    "🥩 перевариваю вопрос, дай времени...",
    "🧊 остываю от подхода, сейчас отвечу...",
    "🩹 разбираю по косточкам, момент...",
    "🚿 после подхода думается чётче, секунду...",
    "🧢 не гони, тренер думает медленно, но метко...",
    "🥊 бью по вопросу точно, момент...",
    "🧱 закладываю фундамент ответа...",
    "⚡ собираю энергию для ответа...",
    "🗿 стою как штанга — думаю тяжело, но верно...",
    "🧭 нахожу верное направление, секунду...",
    "🛠️ докручиваю ответ, почти готово...",
]

# ---------- английские пулы ----------
#
# Третий регистр персонажа (строчные + эмодзи) переносится как есть — не капс
# и не проза, см. модульный докстринг. FACT_CHECK_POOL_EN сюда не входит:
# handlers/factcheck.py берёт русский пул напрямую, без i18n (см. докстринг).

PROGRAM_POOL_EN = [
    "📋 building the program, working out the periodization...",
    "🗂️ laying the training days out by week...",
    "🧮 balancing the split so muscle groups don't fight over rest...",
    "🏗️ building the program from scratch, almost there...",
    "📐 fitting the plan to your specifics, one sec...",
    "🔄 balancing load and recovery phases...",
    "📅 slotting training days into the calendar...",
    "🧩 piecing the program together from your exercises...",
]

EXERCISE_PROGRESS_POOL_EN = [
    "📈 pulling up the progress charts for this lift...",
    "🏋️ checking your weights and reps over the last few weeks...",
    "🔍 looking for where you're gaining and where you're stuck...",
    "📊 running the e1RM so we're not guessing...",
    "🥇 checking if a new record's close...",
    "🧮 comparing current working weight to past sessions...",
    "📏 measuring progress by numbers, not by feel...",
    "🔁 scrolling through the set history on this one...",
]

WEEKLY_VOLUME_POOL_EN = [
    "📆 counting weekly sets for this muscle group...",
    "⚖️ checking the balance across muscle groups...",
    "🧮 adding up the weekly volume, one sec...",
    "🔍 looking for which group's overloaded and which is light...",
    "📊 laying the week out to see the whole picture...",
    "🗓️ checking how the load's spread across the week...",
    "🧭 hunting for a volume imbalance between groups...",
]

NUTRITION_POOL_EN = [
    "🍽 checking your food log...",
    "🥩 counting protein — growth needs more than lifting...",
    "🥗 breaking the diet down by macros, one sec...",
    "🍚 estimating calories for your goal...",
    "🧮 crunching the calorie numbers...",
    "🍳 checking if you're getting enough protein to grow...",
    "🥦 checking the balance between food and training...",
]

BODYWEIGHT_POOL_EN = [
    "⚖️ checking the weight log, one sec...",
    "📉 tracking the trend — cutting or bulking...",
    "🧮 running the numbers on where body weight's headed...",
    "📊 checking weigh-ins week over week, no wild swings...",
    "🧭 looking for the trend in your weigh-ins...",
    "📈 checking if it's mass going up or fat coming off...",
]

TECHNIQUE_POOL_EN = [
    "🎥 running the movement through my head...",
    "🩻 breaking the lift down phase by phase...",
    "🧠 pulling up coaching notes on this one...",
    "🛠️ looking for where the technique breaks down...",
    "📐 checking the bar path, one sec...",
    "🎯 zeroing in on a precise breakdown...",
]

RECOVERY_POOL_EN = [
    "🩹 figuring out what's going on with that pain...",
    "🛌 working out how much rest you actually need...",
    "🧘 taking this slow, sorting out recovery step by step...",
    "🔍 checking if this is just overtraining...",
    "🩺 carefully working through what might've hurt...",
    "🧭 finding the balance between load and rest...",
]

HISTORY_POOL_EN = [
    "📚 scrolling back through your whole training history...",
    "🗂️ pulling up the training archive, one sec...",
    "📖 rereading what happened in past sessions...",
    "🔍 digging through history for what you need...",
    "🧾 checking the old entries...",
    "📅 flipping through workouts by date...",
]

TODAY_POOL_EN = [
    "📋 figuring out what to put you through today...",
    "🏋️ thinking up what to hit today...",
    "🎯 aiming this straight at today...",
    "🔍 working out what fits today...",
    "⏱️ figuring out what to do right now...",
]

MOTIVATION_POOL_EN = [
    "🔥 stoking the motivation, one sec...",
    "🧠 switching on coach mode for a proper kick...",
    "💬 picking the right words, hang on...",
    "🥊 building an answer that'll shake something loose...",
    "💪 hold up, this one's coming with purpose...",
]

PHARMA_POOL_EN = [
    "🧪 taking this one carefully...",
    "📖 pulling up what's actually known about this...",
    "⚖️ weighing the upside against the side effects...",
    "🩺 thinking like a coach who's read more than forums...",
    "🔬 separating the proven from the promised...",
]

SUPPLEMENTS_POOL_EN = [
    "🥤 checking if this one actually works or if it's just marketing...",
    "🧪 recalling what the studies actually showed...",
    "💊 figuring out if you even need this...",
    "📖 checking the dosages...",
    "🔬 sorting the useful from the trendy...",
]

EQUIPMENT_POOL_EN = [
    "🔧 figuring out what to swap it for...",
    "🏠 working out a no-gym version, one sec...",
    "🧰 checking what you can do with what you've got...",
    "🏋️ looking for a move that hits the same muscles...",
    "🔁 finding a real substitute, not just a similar name...",
]

SLEEP_POOL_EN = [
    "😴 thinking about sleep — it's not a minor detail in training...",
    "🌙 figuring out how much sleep you actually need...",
    "🛌 checking how the lack of sleep is hitting your strength...",
    "🧠 recalling what sleep does for recovery...",
]

WARMUP_POOL_EN = [
    "🤸 putting together a warm-up for this movement...",
    "🔥 warming up right along with you, one sec...",
    "📐 figuring out which joints to prep first...",
    "🦵 recalling what usually gets skipped...",
]

DEFAULT_POOL_EN = [
    "💪 hold on, this one's coming...",
    "🧠 switching on coach mode, one sec...",
    "🔥 warming up before the answer...",
    "🎯 aiming for a precise answer, don't spook it...",
    "🧘 gathering my thoughts, hang on...",
    "🏋️ loading up the knowledge bit by bit...",
    "📖 checking the method, one sec...",
    "⏱️ resting between sets of thought, hang on...",
]

POOLS: dict[str, list[str]] = {
    PROGRAM: PROGRAM_POOL,
    EXERCISE_PROGRESS: EXERCISE_PROGRESS_POOL,
    WEEKLY_VOLUME: WEEKLY_VOLUME_POOL,
    NUTRITION: NUTRITION_POOL,
    BODYWEIGHT: BODYWEIGHT_POOL,
    TECHNIQUE: TECHNIQUE_POOL,
    RECOVERY: RECOVERY_POOL,
    HISTORY: HISTORY_POOL,
    TODAY: TODAY_POOL,
    MOTIVATION: MOTIVATION_POOL,
    PHARMA: PHARMA_POOL,
    SUPPLEMENTS: SUPPLEMENTS_POOL,
    EQUIPMENT: EQUIPMENT_POOL,
    SLEEP: SLEEP_POOL,
    WARMUP: WARMUP_POOL,
    DEFAULT_TOPIC: DEFAULT_POOL,
}

POOLS_EN: dict[str, list[str]] = {
    PROGRAM: PROGRAM_POOL_EN,
    EXERCISE_PROGRESS: EXERCISE_PROGRESS_POOL_EN,
    WEEKLY_VOLUME: WEEKLY_VOLUME_POOL_EN,
    NUTRITION: NUTRITION_POOL_EN,
    BODYWEIGHT: BODYWEIGHT_POOL_EN,
    TECHNIQUE: TECHNIQUE_POOL_EN,
    RECOVERY: RECOVERY_POOL_EN,
    HISTORY: HISTORY_POOL_EN,
    TODAY: TODAY_POOL_EN,
    MOTIVATION: MOTIVATION_POOL_EN,
    PHARMA: PHARMA_POOL_EN,
    SUPPLEMENTS: SUPPLEMENTS_POOL_EN,
    EQUIPMENT: EQUIPMENT_POOL_EN,
    SLEEP: SLEEP_POOL_EN,
    WARMUP: WARMUP_POOL_EN,
    DEFAULT_TOPIC: DEFAULT_POOL_EN,
}

# lang -> POOLS: держим отдельно от TEXTS_BY_LANG-приёма push_texts.py, потому
# что здесь нет каталога (locales/*.json) — эти фразы не проходят через ICU и
# не участвуют в тестах полноты каталога, так что задача проще: два обычных
# python-словаря без парсинга JSON.
_POOLS_BY_LANG: dict[str, dict[str, list[str]]] = {"ru": POOLS, "en": POOLS_EN}


def classify(text: str) -> str:
    """Тема вопроса по ключевым словам-стемам, либо DEFAULT_TOPIC, если ни
    один стем не подошёл (в том числе для пустого текста)."""
    tokens = _tokens(text or "")
    if not tokens:
        return DEFAULT_TOPIC
    for topic, stems in _TOPIC_STEMS:
        if any(token.startswith(stem) for token in tokens for stem in stems):
            return topic
    return DEFAULT_TOPIC


def pool_for(text: str) -> list[str]:
    """Пул фраз под тему вопроса, на языке текущего пользователя.

    Язык берём из ambient i18n.get_lang() (тот же приём, что analytics.Rank.name
    и push_texts.pick_text), а не угадываем по тексту вопроса: вопрос может быть
    на любом языке, а плейсхолдер должен звучать на языке интерфейса. По
    умолчанию (язык не поддерживается или контекст не выставлен) отдаём
    русский пул — так же, как i18n делает ru fallback для отсутствующего ключа.
    """
    topic = classify(text)
    return _POOLS_BY_LANG.get(i18n.get_lang(), POOLS)[topic]


def pick(pool: list[str]) -> str:
    return random.choice(pool)


def pick_different(pool: list[str], exclude: Optional[str]) -> str:
    """Случайная фраза из пула, отличная от предыдущей — иначе editText упадёт
    с "message is not modified", да и ротация без этого выглядит нечестно."""
    if len(pool) <= 1:
        return pick(pool)
    choice = exclude
    while choice == exclude:
        choice = pick(pool)
    return choice
