"""Import lots gathered outside the scraper (e.g. a browser-automation run) as JSON.

Input: a JSON list (or {"lots": [...]}) of objects with keys like
url, title, final_price, auction, sale_status, lot_number, sale_date, year, vin, condition,
mileage, documents, location, primary_damage, secondary_damage, estimated_value,
repair_cost, transmission, color, keys, note. Values are normalised exactly like scraped ones.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .damage import text_damage_score
from .parse import EMPTY_VALUES, LOT_HREF_RE, YEAR_RE, mileage_km, model_generation, normalize_transmission, to_number
from .variants import detect

RENAME = {"final_price": "final_bid", "estimated_value": "acv", "note": "highlights"}
STATUS = {"на затвердженні": "on approval", "на утверждении": "on approval", "продано": "sold", "не продано": "not sold"}


def normalise(rec: dict) -> dict:
    lot = {}
    for k, v in rec.items():
        if isinstance(v, str):
            v = v.strip()
            if not v or v.lower() in EMPTY_VALUES:
                continue
        lot[RENAME.get(k, k)] = v
    m = LOT_HREF_RE.search(lot.get("url", ""))
    lot["lot_id"] = m.group(1) if m else lot.get("lot_number")
    y = YEAR_RE.search(str(lot.get("year") or lot.get("title") or ""))
    lot["year"] = int(y.group(1)) if y else None
    lot["mileage_mi"], lot["mileage_km"] = mileage_km(lot.get("mileage"))
    odo = re.search(r"\(([^)]+)\)", lot.get("mileage", ""))
    lot["odometer_status"] = odo.group(1) if odo else None
    lot["transmission_norm"] = normalize_transmission(lot.get("transmission"))
    lot["price_usd"] = to_number(lot.get("final_bid"))
    lot["repair_cost_usd"] = to_number(lot.get("repair_cost")) or None
    lot["acv_usd"] = to_number(lot.get("acv")) or None
    st = (lot.get("sale_status") or "").lower()
    lot["sale_status"] = STATUS.get(st, st or None)
    d = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", lot.get("sale_date", ""))
    lot["sale_date_iso"] = f"{d.group(3)}-{int(d.group(2)):02d}-{int(d.group(1)):02d}" if d else None
    lot["generation"] = model_generation(lot["year"])
    lot["text_damage_score"] = text_damage_score(lot)
    lot.update(detect(lot))
    lot["source"] = "import"
    lot["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return lot


def load(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    recs = data.get("lots", data) if isinstance(data, dict) else data
    return [normalise(r) for r in recs if r.get("url") or r.get("lot_number")]
