import json
import random
import tempfile
import unittest
from pathlib import Path

from carsearch.analyze import report
from carsearch.damage import text_damage_score
from carsearch.parse import mileage_km, next_page_url, normalize_transmission, parse_listing, parse_lot
from carsearch.scrape import scrape
from carsearch.storage import Store

FX = Path(__file__).parent / "fixtures"
LIST_URL = "https://bidfax.info/mazda/mx-5-miata/"
LOT_URL = "https://bidfax.info/mazda/mx-5-miata/40331410-mazda-mx-5-miata-2008-black-vin-jm1nc26f080155907.html"


class FakeFetcher:
    """Serves the fixtures: page 1 = listing, page 2 = empty, every lot = lot.html."""

    def __init__(self):
        self.calls = []

    def get(self, url, use_cache=True):
        self.calls.append(url)
        if url == LIST_URL:
            return (FX / "listing.html").read_text()
        if "/page/" in url:
            return "<html><body></body></html>"
        return (FX / "lot.html").read_text()

    def close(self):
        pass


class ParseTests(unittest.TestCase):
    def test_listing(self):
        html = (FX / "listing.html").read_text()
        stubs = parse_listing(html, LIST_URL)
        self.assertEqual([s["lot_id"] for s in stubs], ["40331410", "40329940"])
        self.assertEqual(stubs[0]["year"], 2008)
        self.assertEqual(stubs[0]["listing_price"], 6300)
        self.assertEqual(stubs[1]["year"], 2015)
        self.assertEqual(next_page_url(html, LIST_URL), LIST_URL + "page/2/")

    def test_lot(self):
        lot = parse_lot((FX / "lot.html").read_text(), LOT_URL)
        self.assertEqual(lot["lot_number"], "76264456")
        self.assertEqual(lot["auction"], "Copart")
        self.assertEqual(lot["year"], 2008)
        self.assertEqual(lot["vin"], "JM1NC26F080155907")
        self.assertEqual(lot["price_usd"], 6300)
        self.assertEqual(lot["mileage_mi"], 53875)
        self.assertEqual(lot["odometer_status"], "Actual")
        self.assertEqual(lot["transmission_norm"], "automatic")
        self.assertEqual(lot["primary_damage"], "Normal Wear")
        self.assertNotIn("secondary_damage", lot)  # "Не вказано" -> empty
        self.assertIsNone(lot["repair_cost_usd"])  # 0 -> unknown
        self.assertEqual(lot["sale_date_iso"], "2026-11-12")
        self.assertEqual(lot["sale_status"], "on approval")
        self.assertEqual(lot["generation"], "NC")
        self.assertEqual(len(lot["photos"]), 8)
        self.assertTrue(all("/uploads/posts/" in p for p in lot["photos"]))
        self.assertIn("Примітка", lot["raw_fields"])

    def test_normalisers(self):
        self.assertEqual(normalize_transmission("Manual"), "manual")
        self.assertEqual(normalize_transmission("6-speed MT"), "manual")
        self.assertEqual(normalize_transmission("Механіка"), "manual")
        self.assertEqual(normalize_transmission("Automatic"), "automatic")
        self.assertEqual(normalize_transmission("Автомат"), "automatic")
        self.assertIsNone(normalize_transmission("Не вказано"))
        self.assertEqual(mileage_km("72 760 км")[1], 72760)
        self.assertEqual(mileage_km("45,210 mi")[0], 45210)


class DamageTests(unittest.TestCase):
    def test_ordering(self):
        wear = text_damage_score({"primary_damage": "Normal Wear", "condition": "Run And Drive"})
        front = text_damage_score({"primary_damage": "Front End", "condition": "Starts"})
        burn = text_damage_score({"primary_damage": "Burn", "condition": "Stationary"})
        self.assertLess(wear, front)
        self.assertLess(front, burn)


class PipelineTests(unittest.TestCase):
    def test_scrape_filters_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            # year 2010+ drops the 2008 card before its lot page is fetched
            stats = scrape(FakeFetcher(), store, model_slugs=["mx-5-miata"], year_from=2010, transmission="any")
            self.assertEqual(stats["kept"], 0)  # the 2015 card's lot page says 2008 -> filtered
            f = FakeFetcher()
            stats = scrape(f, store, model_slugs=["mx-5-miata"], transmission="manual")
            self.assertEqual(stats["kept"], 0)  # fixture is automatic
            f = FakeFetcher()
            scrape(f, store, model_slugs=["mx-5-miata"], transmission="manual")
            self.assertEqual([u for u in f.calls if "/page/" not in u and u != LIST_URL], [])  # rejected lots not refetched
            stats = scrape(FakeFetcher(), store, model_slugs=["mx-5-miata"], transmission="automatic")
            self.assertEqual(stats["kept"], 2)  # both cards (fixture lot page reused)
            self.assertTrue((Path(tmp) / "lots.csv").exists())
            # incremental: a second run fetches no lot pages
            f = FakeFetcher()
            scrape(f, store, model_slugs=["mx-5-miata"], transmission="automatic")
            self.assertFalse(any(u.endswith(".html") and "/page/" not in u and u != LIST_URL for u in f.calls))

    def test_report_on_synthetic_data(self):
        rnd = random.Random(1)
        lots = []
        for i in range(80):
            year = rnd.randint(1990, 2023)
            miles = rnd.randint(10_000, 200_000)
            dmg = rnd.choice(["Normal Wear", "Front End", "Rear End", "Side", "Burn"])
            manual = rnd.random() < 0.5
            lot = {"lot_id": str(i), "year": year, "mileage_mi": miles, "primary_damage": dmg,
                   "transmission_norm": "manual" if manual else "automatic",
                   "generation": "NA" if year < 1998 else "NB" if year < 2006 else "NC" if year < 2016 else "ND"}
            lot["text_damage_score"] = text_damage_score(lot)
            price = 30000 * 0.93 ** (2026 - year) * 0.97 ** (miles / 10000) * (1 - 0.7 * lot["text_damage_score"]) * (1.08 if manual else 1)
            lot["price_usd"] = round(price * rnd.uniform(0.85, 1.15))
            lot["url"] = f"https://bidfax.info/x/{i}"
            lots.append(lot)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lots.json").write_text(json.dumps(lots))
            text = report(tmp, Path(tmp) / "rep")
            self.assertIn("R²", text)
            self.assertIn("mileage_10k", text)
            self.assertTrue((Path(tmp) / "rep" / "residuals.csv").exists())


if __name__ == "__main__":
    unittest.main()


class PhotoAnalyzerTests(unittest.TestCase):
    def test_request_shape_and_parse(self):
        import io
        from types import SimpleNamespace

        from PIL import Image

        from carsearch import damage

        buf = io.BytesIO()
        Image.new("RGB", (2000, 1500), "red").save(buf, format="JPEG")
        captured = {}
        verdict = {"severity": 4, "zones": ["front"], "structural_damage_suspected": False, "airbags_deployed": False,
                   "flood_signs": False, "fire_signs": False, "missing_parts": False, "repair_bucket": "moderate",
                   "est_repair_usd_low": 1500, "est_repair_usd_high": 3000, "confidence": "medium", "notes": "x"}

        class FakeMessages:
            def create(self, **kw):
                captured.update(kw)
                return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                       content=[SimpleNamespace(type="text", text=json.dumps(verdict))])

        an = damage.PhotoAnalyzer.__new__(damage.PhotoAnalyzer)
        an.client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
        an.fetcher = SimpleNamespace(get_bytes=lambda url: buf.getvalue())
        an.model, an.max_photos = "claude-opus-5-5", 3
        res = an.analyze({"lot_id": "1", "photos": ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]})
        self.assertEqual(res["severity"], 4)
        self.assertEqual(res["photos_used"], 3)
        imgs = [c for c in captured["messages"][0]["content"] if c["type"] == "image"]
        self.assertEqual(len(imgs), 3)
        self.assertEqual(captured["output_config"]["format"]["type"], "json_schema")
