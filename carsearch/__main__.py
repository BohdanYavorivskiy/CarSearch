"""CLI: python -m carsearch {scrape,photos,report,all} [options]"""
from __future__ import annotations

import argparse
import logging
import sys

from .fetch import Fetcher
from .storage import Store


def _fetcher(a) -> Fetcher:
    return Fetcher(backend=a.backend, delay=a.delay, cookies=a.cookies, user_agent=a.user_agent)


def cmd_scrape(a):
    from .scrape import Filters, scrape

    filters = Filters(
        year_from=a.year_from or None, year_to=a.year_to,
        transmission=None if a.transmission == "any" else a.transmission,
        roof=None if a.roof == "any" else a.roof,
        trims=[t for t in (a.trim or []) if t != "any"],
        include_unknown=a.include_unknown,
    )
    print("filters:", filters)
    f = _fetcher(a)
    try:
        stats = scrape(f, Store(a.data), make=a.make, model_slugs=a.model or None,
                       filters=filters, max_pages=a.max_pages, refresh=a.refresh)
    finally:
        f.close()
    print("scrape:", stats)


def cmd_photos(a):
    from .damage import PhotoAnalyzer

    store = Store(a.data)
    f = _fetcher(a)
    analyzer = PhotoAnalyzer(f, model=a.vision_model, max_photos=a.max_photos)
    todo = [l for l in store.lots.values() if l.get("photos") and (a.redo or "photo_damage" not in l)]
    todo = todo[: a.limit] if a.limit else todo
    print(f"photo analysis: {len(todo)} lots")
    for i, lot in enumerate(todo, 1):
        res = analyzer.analyze(lot)
        if res:
            lot["photo_damage"] = res
            print(f"[{i}/{len(todo)}] {lot['lot_id']}: severity {res['severity']}/10, {res['repair_bucket']}")
        if i % 5 == 0:
            store.save()
    store.save()
    f.close()


def cmd_report(a):
    from .analyze import report

    print(report(a.data, a.out))


def main(argv=None):
    p = argparse.ArgumentParser(prog="carsearch", description="Mazda MX-5 sold lots from bidfax.info")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--data", default="data", help="dataset directory (default: data)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def net(sp):
        sp.add_argument("--backend", choices=["requests", "curl", "playwright"], default="requests")
        sp.add_argument("--delay", type=float, default=2.5, help="seconds between requests")
        sp.add_argument("--cookies", help="cookie string or file (e.g. cf_clearance=...); or env BIDFAX_COOKIE")
        sp.add_argument("--user-agent", help="must match the browser that produced cf_clearance; or env BIDFAX_USER_AGENT")

    s = sub.add_parser("scrape", help="collect sold lots")
    net(s)
    s.add_argument("--make", default="mazda")
    s.add_argument("--model", action="append", help="model slug(s) on bidfax, default: mx-5-miata and mx-5")
    s.add_argument("--year-from", type=int, default=2016, help="default 2016 (ND); 0 = no limit")
    s.add_argument("--year-to", type=int)
    s.add_argument("--transmission", choices=["manual", "automatic", "any"], default="any")
    s.add_argument("--roof", choices=["soft", "hard", "any"], default="any",
                   help="soft = м'який дах, hard = твердий (RF / PRHT)")
    s.add_argument("--trim", action="append", choices=["sport", "club", "grand_touring", "other", "any"],
                   help="версія; можна кілька: --trim club --trim grand_touring")
    s.add_argument("--include-unknown", action="store_true",
                   help="не відкидати лоти, де дах/версію/коробку не вдалося визначити")
    s.add_argument("--max-pages", type=int, default=20)
    s.add_argument("--refresh", action="store_true", help="re-download lots already in the dataset")
    s.set_defaults(func=cmd_scrape)

    ph = sub.add_parser("photos", help="damage analysis of lot photos with Claude vision")
    net(ph)
    ph.add_argument("--max-photos", type=int, default=6)
    ph.add_argument("--limit", type=int, help="analyse at most N lots this run")
    ph.add_argument("--redo", action="store_true")
    ph.add_argument("--vision-model", help="default: env CARSEARCH_VISION_MODEL or claude-opus-5-5")
    ph.set_defaults(func=cmd_photos)

    r = sub.add_parser("report", help="price vs condition analysis -> reports/report.md")
    r.add_argument("--out", default="reports")
    r.set_defaults(func=cmd_report)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    a.func(a)


if __name__ == "__main__":
    sys.exit(main())
