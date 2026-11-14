import json
import random
import tempfile
import unittest
from pathlib import Path

from carsearch.analyze import report
from carsearch.damage import text_damage_score
from carsearch.parse import mileage_km, next_page_url, normalize_transmission, parse_listing, parse_lot
from carsearch import variants
from carsearch.scrape import Filters, scrape
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


ND_LOTS = {
    # lot_id: (title, vin, transmission)
    "50000001": ("Mazda Mx-5 Miata Rf Grand Touring 2019 Red", "JM1NDAM75K0300001", "Manual"),
    "50000002": ("Mazda Mx-5 Miata Club 2017 White", "JM1NDAC70H0100002", "Manual"),
    "50000003": ("Mazda Mx-5 Miata Sport 2016 Blue", "JM1NDAB78G0100003", "Automatic"),
    "50000004": ("Mazda Mx-5 Miata 2018 Grey", "JM1NDAD71J0100004", "Automatic"),  # trim only in VIN
    "50000005": ("Mazda Mx-5 Miata 2008 Black", "JM1NC26F080155907", "Automatic"),
}


def _slug(title):
    return title.lower().replace(" ", "-")


def nd_url(lot_id):
    t, vin, _ = ND_LOTS[lot_id]
    return f"https://bidfax.info/mazda/mx-5-miata/{lot_id}-{_slug(t)}-vin-{vin.lower()}.html"


class NDFetcher(FakeFetcher):
    """Listing with 5 lots of different variants; each lot page is lot.html with values swapped."""

    def get(self, url, use_cache=True):
        self.calls.append(url)
        card = (FX / "listing.html").read_text()
        if url == LIST_URL:
            links = "".join(
                f'<div class="thumbnail offer"><div class="caption"><a href="{nd_url(i)}">'
                f"<heading>{t} vin: {v}</heading></a></div><span class=\"prices\">{9000 + n * 1000}</span></div>"
                for n, (i, (t, v, _)) in enumerate(ND_LOTS.items())
            )
            return f"<html><body>{links}</body></html>"
        if "/page/" in url or url.rstrip("/").endswith(("mx-5", "mx-5-miata-rf")):
            return "<html><body></body></html>"
        lot_id = url.rsplit("/", 1)[1].split("-")[0]
        t, vin, tr = ND_LOTS[lot_id]
        year = next(w for w in t.split() if w.isdigit())
        html = (FX / "lot.html").read_text()
        return (html.replace("Mazda Mx-5 Miata 2008 Black", t).replace("JM1NC26F080155907", vin)
                .replace(">2008<", f">{year}<").replace(">Automatic<", f">{tr}<"))


def run(store, fetcher=None, **kw):
    return scrape(fetcher or NDFetcher(), store, filters=Filters(**kw))


class VariantTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(variants.from_text("Mazda Mx-5 Miata Rf Grand Touring 2019"), ("hard", "grand_touring"))
        self.assertEqual(variants.from_text("mazda-mx-5-miata-club-2017-white"), (None, "club"))
        self.assertEqual(variants.from_vin("JM1NDAB78G0100003"), ("soft", "sport"))
        self.assertEqual(variants.from_vin("JM1NDAM75K0300001"), ("hard", "grand_touring"))
        self.assertEqual(variants.from_vin("JM1NC25F490202195"), ("soft", None))
        d = variants.detect({"title": "Mazda Mx-5 Miata 2018 Grey", "vin": "JM1NDAD71J0100004"})
        self.assertEqual((d["roof"], d["trim"], d["variant_source"]), ("soft", "grand_touring", "vin"))
        d = variants.detect({"title": "Mazda Mx-5 Miata Club 2017", "vin": "JM1NDAD71J0100004"})
        self.assertTrue(d["variant_conflict"])


class PipelineTests(unittest.TestCase):
    def kept(self, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            run(store, **kw)
            return sorted(store.lots)

    def test_default_is_2016_plus(self):
        self.assertEqual(self.kept(), ["50000001", "50000002", "50000003", "50000004"])

    def test_filters(self):
        self.assertEqual(self.kept(roof="hard"), ["50000001"])
        self.assertEqual(self.kept(roof="soft"), ["50000002", "50000003", "50000004"])
        self.assertEqual(self.kept(trims=["club", "grand_touring"]), ["50000001", "50000002", "50000004"])
        self.assertEqual(self.kept(trims=["sport"]), ["50000003"])
        self.assertEqual(self.kept(transmission="manual", roof="soft"), ["50000002"])
        self.assertEqual(self.kept(year_from=None, roof="hard"), ["50000001", "50000005"])  # NC PRHT

    def test_prefilter_and_incremental(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            f = NDFetcher()
            run(store, f, roof="hard")
            lot_calls = [u for u in f.calls if u.endswith(".html")]
            self.assertEqual(lot_calls, [nd_url("50000001")])  # others rejected from the card alone
            self.assertTrue((Path(tmp) / "lots.csv").exists())
            f = NDFetcher()
            run(store, f, roof="hard")
            self.assertEqual([u for u in f.calls if u.endswith(".html")], [])  # nothing refetched
            f = NDFetcher()
            run(store, f, transmission="manual")  # different filter -> skipped lots become eligible
            self.assertIn(nd_url("50000002"), f.calls)
            self.assertEqual(sorted(store.lots), ["50000001", "50000002"])

    def test_report_on_synthetic_data(self):
        rnd = random.Random(1)
        lots = []
        for i in range(80):
            year = rnd.randint(1990, 2023)
            miles = rnd.randint(10_000, 200_000)
            dmg = rnd.choice(["Normal Wear", "Front End", "Rear End", "Side", "Burn"])
            manual = rnd.random() < 0.5
            lot = {"lot_id": str(i), "year": year, "roof": rnd.choice(["soft", "hard"]),
                   "trim": rnd.choice(["sport", "club", "grand_touring"]), "mileage_mi": miles, "primary_damage": dmg,
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
