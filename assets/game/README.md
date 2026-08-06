# Арты персонажей «Кач-Раннера»

Страница игры (`game.html`) на экране выбора персонажа сначала пытается показать
арт из этой папки, и только если файла нет — рисует векторного атлета сама.
Чтобы включить арты, положи сюда четыре квадратных JPG (512×512 или больше):

- `power.jpg` — Пауэрлифтер
- `cross.jpg` — Кроссфитер
- `wrestle.jpg` — Борец
- `build.jpg` — Бодибилдер

## Промпты для генерации

Стиль — мультяшный, подчёркнуто нереалистичный: генераторы «semi-realistic»
уводят в гиперреализм с венами, а масса в мультстиле делается преувеличением
пропорций. Общий стилевой хвост, добавлять к каждому промпту:

> Flat 2D cartoon character art, cel-shaded with thick clean outlines,
> exaggerated stylized proportions, simple flat colors, like a character from a
> fun mobile arcade game, NOT photorealistic, no fine skin detail, no realistic
> anatomy, cheerful confident face, waist-up portrait, simple dark gym
> background with warm glowing bulbs drawn as simple shapes, one accent color
> per character, square format, no text, no watermark

- **power.jpg**: A colossal cartoon powerlifter, super heavyweight class, thick
  barrel torso, shoulders twice as wide as his head, grey bandana on head, dark
  red tank top, shiny gold lifting belt, chalk clouds around his hands, arms
  crossed, smug grin. *(акцент — красный)*
- **cross.jpg**: A lean bouncy cartoon crossfitter, springy athletic build,
  white headband, teal sleeveless shirt, jump rope over his shoulders, taped
  wrists, easy open grin. *(акцент — бирюзовый)*
- **wrestle.jpg**: A stocky cartoon wrestler, low and wide like a fridge,
  burgundy singlet, comically big cauliflower ears, short buzz cut, tiny calm
  smile, ready stance. *(акцент — бордовый)*
- **build.jpg**: An enormous cartoon bodybuilder with absurd heroic
  proportions, arms bigger than his head, huge round pecs, wide flaring lats,
  tiny waist, deep orange tan, dark posing trunks, short dark hair, proud
  chin-up pose. *(акцент — золотой)*

Практика: генерить все четыре подряд в одном чате генератора («same style,
now …») — карточки стоят рядом и должны выглядеть одной серией. Портрет по
пояс, персонаж по центру: карточка квадратная и кропает центр (object-fit:
cover), ноги в неё не влезают в любом случае.
