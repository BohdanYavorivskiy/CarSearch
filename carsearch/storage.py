"""Dataset persistence: data/lots.json (full records) + data/lots.csv (flat table)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

CSV_COLUMNS = [
    "lot_id", "sale_date_iso", "sale_status", "year", "generation", "title", "price_usd", "mileage_mi", "mileage_km",
    "transmission_norm", "transmission", "roof", "trim", "variant_source", "variant_conflict", "engine", "primary_damage", "secondary_damage", "condition",
    "keys", "airbags", "odometer_status", "repair_cost_usd", "acv_usd", "text_damage_score", "photo_severity",
    "photo_repair_bucket", "auction", "lot_number", "location", "documents", "seller", "color", "drive",
    "fuel", "vin", "url",
]


class Store:
    def __init__(self, data_dir: str | Path = "data"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.json_path = self.dir / "lots.json"
        self.csv_path = self.dir / "lots.csv"
        self.lots: dict[str, dict] = {}
        # lots that were fetched but did not match the filters -> {lot_id: {year, transmission}}
        self.skipped_path = self.dir / "skipped.json"
        self.skipped: dict[str, dict] = (
            json.loads(self.skipped_path.read_text(encoding="utf-8")) if self.skipped_path.exists() else {}
        )
        if self.json_path.exists():
            for lot in json.loads(self.json_path.read_text(encoding="utf-8")):
                self.lots[str(lot["lot_id"])] = lot

    def __contains__(self, lot_id) -> bool:
        return str(lot_id) in self.lots

    def upsert(self, lot: dict):
        key = str(lot["lot_id"])
        old = self.lots.get(key, {})
        # keep previous photo analysis if a re-scrape doesn't have one
        if "photo_damage" in old and "photo_damage" not in lot:
            lot["photo_damage"] = old["photo_damage"]
        self.lots[key] = {**old, **lot}

    def skip(self, lot: dict):
        self.skipped[str(lot["lot_id"])] = {k: lot.get(k) for k in ("year", "transmission_norm", "roof", "trim")}

    def save(self):
        self.skipped_path.write_text(json.dumps(self.skipped, indent=0, sort_keys=True), encoding="utf-8")
        lots = sorted(self.lots.values(), key=lambda x: (str(x.get("sale_date_iso") or ""), str(x["lot_id"])), reverse=True)
        self.json_path.write_text(json.dumps(lots, ensure_ascii=False, indent=1), encoding="utf-8")
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for lot in lots:
                row = dict(lot)
                pd_ = lot.get("photo_damage") or {}
                row["photo_severity"] = pd_.get("severity")
                row["photo_repair_bucket"] = pd_.get("repair_bucket")
                w.writerow(row)
