# CarSearch: Mazda MX-5 на аукціонах (bidfax.info)

Скрипт збирає **продані** лоти Mazda MX-5 / Miata з [bidfax.info](https://bidfax.info/mazda/mx-5-miata/),
зберігає всі доступні дані, дає грубу оцінку пошкоджень за фото і будує модель:
**ціна ← рік, пробіг, коробка, пошкодження**.

## Швидкий старт

```bash
pip install -r requirements.txt
python -m carsearch scrape --transmission manual --year-from 2006   # збір
python -m carsearch photos --limit 20                               # аналіз фото (потрібен ANTHROPIC_API_KEY)
python -m carsearch report                                          # reports/report.md
```

### Фільтри `scrape`
| параметр | значення |
|---|---|
| `--transmission` | `manual`, `automatic`, `any` (за замовчуванням) |
| `--year-from`, `--year-to` | діапазон років випуску |
| `--max-pages` | скільки сторінок списку обходити (20 за замовчуванням) |
| `--model` | slug моделі на bidfax (за замовчуванням `mx-5-miata` і `mx-5`) |
| `--refresh` | перезавантажити вже збережені лоти |

Повторні запуски **інкрементальні**: вже відомі лоти не завантажуються повторно,
обхід зупиняється після 2 сторінок без нових лотів.
Лоти, що не пройшли фільтр, пишуться в `data/skipped.json` і теж не завантажуються вдруге.

## Що зберігається

* `data/lots.json` містить повні записи. Кожне поле сторінки лота лежить і в нормалізованому вигляді
  (`price_usd`, `year`, `mileage_mi/km`, `transmission_norm`, `primary_damage`, `repair_cost_usd`, `acv_usd`,
  `sale_date_iso`, `sale_status`, `generation` NA/NB/NC/ND, `photos`…), і як є, у `raw_fields`.
  Тож нічого не губиться, навіть якщо сайт додасть нові поля.
* `data/lots.csv` є плоскою таблицею для Excel / Google Sheets.
* `reports/report.md` містить таблиці за поколінням, коробкою, роком і пошкодженням, регресію та
  список лотів, що продались найдешевше відносно моделі. `reports/residuals.csv` містить усі лоти з відхиленням від моделі.

## Оцінка пошкоджень

1. **`text_damage_score` (0…1)** рахується завжди. Він складається з кодів аукціону (Front End, Rear End, Burn,
   Flood, Normal Wear…), стану (Run and Drive / Stationary), ключів, подушок безпеки та відношення
   вартості ремонту до ринкової вартості, якщо вони вказані.
2. **`photo_damage`** формує Claude, переглядаючи до 6 фото лота. Він повертає структурований JSON:
   серйозність 0–10, зони (front/rear/sides/roof/interior/…), підозру на геометрію, подушки, сліди
   води чи вогню, відсутні деталі, бакет ремонту та діапазон вартості ремонту в $.
   Потрібен `ANTHROPIC_API_KEY`. Модель задається через `--vision-model` або `CARSEARCH_VISION_MODEL`
   (за замовчуванням `claude-opus-5-5`; щоб було дешевше, можна взяти `claude-haiku-5-5`). Фото стискаються до 1024px.
   Коли фото-оцінка є хоча б у половини лотів, вона автоматично входить у регресію.

## Аналіз

OLS на `log(ціна)`: `вік + пробіг/10k миль + damage_score + механіка + покоління (+ photo severity)`.
Коефіцієнт читається як відсоток зміни ціни: наприклад, `mileage_10k = -0.03` означає приблизно −3% за кожні 10 тис. миль.

## Cloudflare

bidfax.info захищений Cloudflare. Варіанти, від найпростішого:

* `--backend playwright` запускає справжній Chromium (`pip install playwright && playwright install chromium`).
  `BIDFAX_HEADLESS=0` показує вікно, щоб пройти перевірку вручну.
* `--backend curl` використовує `curl_cffi` з TLS-відбитком Chrome.
* Cookie з браузера: відкрийте сайт, скопіюйте `cf_clearance` з DevTools → Application → Cookies і передайте
  `--cookies "cf_clearance=..." --user-agent "<UA того ж браузера>"` або змінні `BIDFAX_COOKIE` / `BIDFAX_USER_AGENT`.

Запити йдуть із затримкою ~2.5 с (`--delay`), сторінки кешуються в `.cache/`.

## Автоматичне оновлення в репозиторії

`.github/workflows/scrape.yml` запускається щодня (і вручну через *Run workflow* з фільтрами):
збирає нові лоти, аналізує фото (якщо є секрет `ANTHROPIC_API_KEY`), будує звіт і комітить
`data/` та `reports/`. Необов'язкові секрети: `BIDFAX_COOKIE`, `BIDFAX_USER_AGENT`.
Cloudflare може блокувати IP GitHub-раннерів. Тоді запускайте скрипт локально або на self-hosted runner.

## Тести

```bash
python -m unittest discover -s tests
```
Фікстури в `tests/fixtures/` зроблені з реальної розмітки сторінок bidfax.info (листопад 2026).
