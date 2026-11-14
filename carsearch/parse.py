"""HTML parsing for bidfax.info listing and lot pages.

The parser is deliberately *label driven*: instead of relying on exact CSS
classes (which change), it walks the page looking for "Label: value" pairs in
tables, definition lists, list items and spans, and maps the labels (EN/UA/RU)
to canonical field names. Every pair found is also kept verbatim in
``raw_fields`` so no data is lost even if a label is unknown.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

BASE = "https://bidfax.info"

# canonical field -> label variants (lower-case, without trailing colon)
LABELS: dict[str, list[str]] = {
    "auction": ["auction", "аукціон", "аукцион"],
    "lot_number": ["lot number", "lot", "lot #", "номер лоту", "номер лота", "лот"],
    "sale_date": ["date of sale", "sale date", "дата продажу", "дата продажи", "дата аукціону", "дата аукциона"],
    "year": ["year", "year of manufacture", "рік випуску", "год выпуска", "рік", "год"],
    "vin": ["vin", "vin code", "vin-код", "vin код"],
    "condition": ["condition", "стан", "состояние"],
    "engine": ["engine", "двигун", "двигатель", "об'єм двигуна", "объем двигателя"],
    "mileage": ["mileage", "odometer", "пробіг", "пробег", "одометр"],
    "seller": ["seller", "продавець", "продавец"],
    "documents": ["documents", "title", "документи", "документы", "sale document"],
    "location": ["location", "sale location", "місце продажу", "место продажи", "локація", "локация"],
    "primary_damage": ["primary damage", "основне ушкодження", "основне пошкодження", "основное повреждение", "пошкодження", "повреждение", "damage"],
    "secondary_damage": ["secondary damage", "другорядне пошкодження", "вторинне пошкодження", "вторичное повреждение", "второстепенное повреждение"],
    "repair_cost": ["estimated repair cost", "repair cost", "ціна ремонту", "цена ремонта", "вартість ремонту", "стоимость ремонта", "оцінка ремонту"],
    "acv": ["actual cash value", "acv", "est. retail value", "estimated value", "оціночна вартість", "оценочная стоимость", "ринкова вартість", "рыночная стоимость", "реальна вартість", "реальная стоимость"],
    "transmission": ["transmission", "gearbox", "коробка передач", "трансмісія", "трансмиссия", "кпп", "коробка"],
    "color": ["color", "colour", "body color", "колір", "цвет", "колір кузова", "цвет кузова"],
    "drive": ["drive", "drive type", "drive line", "привід", "привод"],
    "fuel": ["fuel", "fuel type", "паливо", "топливо", "тип палива", "тип топлива"],
    "keys": ["keys", "key", "ключі", "ключи", "наявність ключів", "наличие ключей"],
    "body_style": ["body style", "body type", "тип кузова", "кузов"],
    "cylinders": ["cylinders", "циліндри", "цилиндры"],
    "airbags": ["airbags", "подушки безпеки", "подушки безопасности"],
    "final_bid": ["final bid", "final price", "sold for", "фін. ставка", "фин. ставка", "фінальна ставка", "финальная ставка", "ціна продажу", "цена продажи", "ставка"],
    "highlights": ["highlights", "особливості", "особенности", "start code", "примітка", "примечание", "note"],
}
_LABEL_LOOKUP = {v: k for k, vs in LABELS.items() for v in vs}

EMPTY_VALUES = {"не вказано", "не указано", "not specified", "n/a", "-", "відсутня", "отсутствует", "absent", "невідомо", "unknown", "невідомо або електро", "неизвестно или электро", "unknown or electric"}


def norm_label(label: str) -> str:
    return re.sub(r"[\s:≈#]+$", "", label.lower().strip()).strip()


def canonical(label: str) -> str | None:
    return _LABEL_LOOKUP.get(norm_label(label))

LOT_HREF_RE = re.compile(r"/mazda/[^/]+/(\d+)-[^/]+\.html", re.I)
YEAR_RE = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
MONEY_RE = re.compile(r"\$?\s*([\d][\d\s,. ]*)\s*\$?")


# ---------------------------------------------------------------- normalisers
def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip(" : \t\n")


def to_number(s: str | None) -> float | None:
    if not s:
        return None
    m = MONEY_RE.search(s.replace(" ", " "))
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1).split(".")[0] if re.search(r"\.\d{2}\b", m.group(1)) else m.group(1))
    return float(digits) if digits else None


def mileage_km(s: str | None) -> tuple[float | None, float | None]:
    """Return (miles, km) from strings like '45,210 mi (72 760 km)' or '72760 км'."""
    if not s:
        return None, None
    s2 = s.lower().replace(" ", " ")
    miles = km = None
    m = re.search(r"([\d][\d ,.]*)\s*(mi|miles|миль|мил)", s2)
    if m:
        miles = float(re.sub(r"[^\d]", "", m.group(1)) or 0)
    k = re.search(r"([\d][\d ,.]*)\s*(km|км)", s2)
    if k:
        km = float(re.sub(r"[^\d]", "", k.group(1)) or 0)
    if miles is None and km is None:
        n = to_number(s2)
        miles = n  # US auctions report miles by default
    if miles is not None and km is None:
        km = round(miles * 1.609344)
    if km is not None and miles is None:
        miles = round(km / 1.609344)
    return miles, km


def normalize_transmission(s: str | None) -> str | None:
    if not s:
        return None
    t = s.lower()
    if re.search(r"manual|механ|ручн|\bmt\b|\bмкпп\b|\d-speed manual", t):
        return "manual"
    if re.search(r"auto|автомат|\bакпп\b|\bat\b|\bcvt\b|варіатор|вариатор", t):
        return "automatic"
    return None


def model_generation(year: int | None) -> str | None:
    if not year:
        return None
    if year <= 1997:
        return "NA"
    if year <= 2005:
        return "NB"
    if year <= 2015:
        return "NC"
    return "ND"


# -------------------------------------------------------------- listing page
def parse_listing(html: str, page_url: str = BASE) -> list[dict]:
    """Return lot stubs found on a listing page (url, lot_id, title, year, price, thumb)."""
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        m = LOT_HREF_RE.search(href)
        if not m:
            continue
        lot_id = m.group(1)
        stub = seen.setdefault(lot_id, {"lot_id": lot_id, "url": href.split("#")[0]})
        card = _card_for(a)
        text = clean(card.get_text(" ")) if card else clean(a.get_text(" "))
        title = clean(a.get("title") or a.get_text(" "))
        if title and len(title) > len(stub.get("title", "")):
            stub["title"] = title
        if "year" not in stub:
            y = YEAR_RE.search(title) or YEAR_RE.search(text) or YEAR_RE.search(href)
            if y:
                stub["year"] = int(y.group(1))
        if card is not None and "listing_price" not in stub:
            price_el = card.select_one(".prices, [class*=price]")
            if price_el:
                stub["listing_price"] = to_number(price_el.get_text(" "))
            img = card.find("img")
            if img:
                src = img.get("data-src") or img.get("src")
                if src and not src.startswith("data:"):
                    stub["thumb"] = urljoin(page_url, src)
            stub["card_text"] = text[:500]
    return list(seen.values())


def _card_for(a: Tag) -> Tag | None:
    node = a
    for _ in range(6):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            return None
        cls = " ".join(node.get("class", []))
        if re.search(r"listing-card|offer|thumbnail|short|card|item", cls) and node.name != "a":
            # stop at the card, not at a container that holds many cards
            if len(node.find_all("a", href=LOT_HREF_RE)) <= 3:
                return node
    return None


def next_page_url(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    link = soup.find("link", rel="next") or soup.find("a", rel="next")
    if link and link.get("href"):
        return urljoin(page_url, link["href"])
    for a in soup.select("a[href*='/page/']"):
        if clean(a.get_text()).lower() in ("next", "»", "›", "наступна", "следующая", "далі", "далее", ">"):
            return urljoin(page_url, a["href"])
    return None


# ------------------------------------------------------------------ lot page
STATUS_ICONS = {"onapproval": "on approval", "sold": "sold", "notsold": "not sold", "pure": "sold", "minimal": "on minimum bid"}


def sale_status(soup) -> str | None:
    """bidfax marks the sale result with an icon next to the auction name."""
    for img in soup.select("img[src*='dleimages']"):
        name = re.sub(r"\d*\.(png|gif|svg)$", "", img["src"].rsplit("/", 1)[-1].lower())
        text = clean(img.get_text(" ") or img.get("alt") or img.get("title") or "")
        return STATUS_ICONS.get(name, text or name)
    return None
def _pairs(soup: BeautifulSoup) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    # tables
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            pairs.append((clean(cells[0].get_text(" ")), clean(cells[1].get_text(" "))))
    # definition lists
    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            pairs.append((clean(dt.get_text(" ")), clean(dd.get_text(" "))))
    # "Label: <span>value</span>" or "<span>Label</span><span>value</span>" in li/p/div
    for el in soup.find_all(["li", "p", "div"]):
        if el.find(["li", "p", "div", "table"]):
            continue
        text = clean(el.get_text(" "))
        if not text or len(text) > 200:
            continue
        children = [c for c in el.find_all(["span", "b", "strong", "label", "em"], recursive=False)]
        if len(children) >= 2:
            pairs.append((clean(children[0].get_text(" ")), clean(" ".join(c.get_text(" ") for c in children[1:]))))
        elif ":" in text:
            k, v = text.split(":", 1)
            if 0 < len(k) <= 40:
                pairs.append((clean(k), clean(v)))
    return [(k, v) for k, v in pairs if k and v and len(k) <= 60]


def _photo_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    urls: list[str] = []
    for el in soup.find_all(["a", "img", "source"]):
        for attr in ("data-full", "data-original", "data-src", "data-lazy", "href", "src", "srcset"):
            v = el.get(attr)
            if not v:
                continue
            v = v.split(",")[0].split(" ")[0]
            if re.search(r"\.(jpe?g|webp|png)(\?|$)", v, re.I) and not re.search(
                r"/templates/|logo|icon|sprite|flag|avatar|banner|placeholder|no[-_]?photo|thumb_?s", v, re.I
            ):
                full = urljoin(page_url, v)
                if full not in urls:
                    urls.append(full)
    posts = [u for u in urls if "/uploads/posts/" in u]
    return posts or urls


def parse_lot(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    raw: dict[str, str] = {}
    fields: dict[str, str] = {}
    for k, v in _pairs(soup):
        raw.setdefault(k, v)
        key = canonical(k)
        if key and key not in fields and v.lower() not in EMPTY_VALUES:
            fields[key] = v

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ")) if h1 else clean(soup.title.get_text() if soup.title else "")
    page_text = clean(soup.get_text(" "))

    lot: dict = {"url": url, "title": title}
    m = LOT_HREF_RE.search(url)
    if m:
        lot["lot_id"] = m.group(1)
    lot.update(fields)

    # derived / normalised values
    if "year" in fields:
        y = YEAR_RE.search(fields["year"])
        lot["year"] = int(y.group(1)) if y else None
    else:
        y = YEAR_RE.search(title)
        lot["year"] = int(y.group(1)) if y else None
    if "vin" not in lot:
        v = VIN_RE.search(title) or VIN_RE.search(page_text)
        if v:
            lot["vin"] = v.group(0)
    lot["mileage_mi"], lot["mileage_km"] = mileage_km(fields.get("mileage"))
    lot["transmission_norm"] = normalize_transmission(fields.get("transmission") or title)
    lot["repair_cost_usd"] = to_number(fields.get("repair_cost")) or None  # bidfax shows 0 when unknown
    lot["acv_usd"] = to_number(fields.get("acv")) or None
    odo = re.search(r"\(([^)]+)\)", fields.get("mileage", ""))
    lot["odometer_status"] = odo.group(1) if odo else None
    lot["sale_status"] = sale_status(soup)
    d = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", fields.get("sale_date", ""))
    lot["sale_date_iso"] = f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}" if d else None

    price = to_number(fields.get("final_bid"))
    if price is None:
        el = soup.select_one(".bidfax-price .prices, .prices, [class*=price]")
        if el:
            price = to_number(el.get_text(" "))
    lot["price_usd"] = price
    lot["generation"] = model_generation(lot.get("year"))
    lot["photos"] = _photo_urls(soup, url)
    lot["raw_fields"] = raw
    return lot
