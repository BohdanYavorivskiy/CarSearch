"""Roof type (soft / hard top) and trim (Sport / Club / Grand Touring) detection.

Sources, in order of trust:
1. Lot title / URL slug, e.g. "Mazda Mx-5 Miata RF Grand Touring 2019 Red".
2. VIN pattern (ND generation, 2016+, US market), e.g. JM1NDAD7?G... = soft top Grand Touring:
     JM1NDAB  soft top  Sport        JM1NDAL  RF (hard)  Club
     JM1NDAC  soft top  Club         JM1NDAM  RF (hard)  Grand Touring
     JM1NDAD  soft top  Grand Touring
   NC (2006-15): JM1NC25 = soft top, JM1NC26 / JM1NC2P* / JM1NC2M* etc. = power retractable hard top (PRHT).
   ND prefixes checked against dealer listings / VIN-lookup sites (17vin, truevin), not Mazda docs.
   When title and VIN disagree, the title wins and
   ``variant_conflict`` is set so you can check those lots by hand.
"""
from __future__ import annotations

import re

ROOFS = ("soft", "hard")
TRIMS = ("sport", "club", "grand_touring", "other")

ND_VIN = {
    "B": ("soft", "sport"),
    "C": ("soft", "club"),
    "D": ("soft", "grand_touring"),
    "L": ("hard", "club"),
    "M": ("hard", "grand_touring"),
}


def from_text(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    t = " " + re.sub(r"[-_/]+", " ", text.lower()) + " "
    roof = None
    if re.search(r"\b(rf|retractable fastback|prht|hard ?top|hardtop|power retractable)\b", t):
        roof = "hard"
    elif re.search(r"\b(soft ?top|softtop|roadster|convertible|кабріолет|кабриолет)\b", t):
        roof = "soft"
    trim = None
    if re.search(r"\b(grand touring|gt)\b", t):
        trim = "grand_touring"
    elif re.search(r"\bclub\b", t):
        trim = "club"
    elif re.search(r"\bsport\b", t):
        trim = "sport"
    elif re.search(r"\b(launch edition|anniversary|kazari|special edition|touring|gs|ls|base)\b", t):
        trim = "other"
    return roof, trim


def from_vin(vin: str | None) -> tuple[str | None, str | None]:
    if not vin or len(vin) < 7:
        return None, None
    v = vin.upper()
    if v.startswith("JM1NDA") and v[6] in ND_VIN:
        return ND_VIN[v[6]]
    if v.startswith("JM1NC25"):
        return "soft", None
    if v.startswith("JM1NC2") and v[6] != "5":
        return "hard", None
    return None, None


def detect(lot: dict) -> dict:
    t_roof, t_trim = from_text(" ".join(str(lot.get(k) or "") for k in ("title", "url", "body_style")))
    vin = lot.get("vin")
    if not vin:  # listing cards carry the VIN in the title ("... vin: JM1NDAM7...") and URL
        m = re.search(r"\b(jm1[a-hj-npr-z0-9]{14})\b", f"{lot.get('title', '')} {lot.get('url', '')}", re.I)
        vin = m.group(1) if m else None
    v_roof, v_trim = from_vin(vin)
    roof = t_roof or v_roof
    trim = t_trim or v_trim
    # A plain "MX-5 Miata" ND title with no RF marker is a soft top only if the VIN agrees.
    conflict = bool((t_roof and v_roof and t_roof != v_roof) or (t_trim and v_trim and t_trim != v_trim))
    return {
        "roof": roof,
        "trim": trim,
        "variant_source": "title" if (t_roof or t_trim) else ("vin" if (v_roof or v_trim) else None),
        "variant_conflict": conflict,
    }
