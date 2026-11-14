"""Crawl bidfax.info listing pages for a make/model and collect sold lots."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .damage import text_damage_score
from .fetch import Fetcher
from .parse import BASE, next_page_url, parse_listing, parse_lot
from .storage import Store

log = logging.getLogger(__name__)

# bidfax uses several slugs for the same car over the years
DEFAULT_MODEL_SLUGS = ["mx-5-miata", "mx-5"]


def listing_url(make: str, model_slug: str, page: int) -> str:
    base = f"{BASE}/{make}/{model_slug}/"
    return base if page == 1 else f"{base}page/{page}/"


def matches(lot: dict, year_from: int | None, year_to: int | None, transmission: str | None) -> bool:
    y = lot.get("year")
    if year_from and (y is None or y < year_from):
        return False
    if year_to and (y is None or y > year_to):
        return False
    if transmission and transmission != "any":
        if lot.get("transmission_norm") != transmission:
            return False
    return True


def scrape(
    fetcher: Fetcher,
    store: Store,
    make: str = "mazda",
    model_slugs: list[str] | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    transmission: str | None = None,
    max_pages: int = 20,
    stop_after_known: int = 2,
    refresh: bool = False,
) -> dict:
    """Walk listing pages newest→oldest. Stops when ``stop_after_known`` pages in a row
    contain only lots we already have (incremental updates), or after ``max_pages``."""
    stats = {"pages": 0, "seen": 0, "new": 0, "kept": 0, "filtered_out": 0, "errors": 0}
    for slug in model_slugs or DEFAULT_MODEL_SLUGS:
        url: str | None = listing_url(make, slug, 1)
        page = 1
        known_streak = 0
        while url and page <= max_pages:
            try:
                html = fetcher.get(url, use_cache=False)
            except Exception as e:
                log.error("listing %s failed: %s", url, e)
                stats["errors"] += 1
                break
            stubs = parse_listing(html, url)
            stats["pages"] += 1
            log.info("%s page %d: %d lots", slug, page, len(stubs))
            if not stubs:
                break
            # a page is "known" when every lot on it was seen in an earlier run
            new_on_page = sum(1 for st in stubs if st["lot_id"] not in store and st["lot_id"] not in store.skipped)
            for stub in stubs:
                stats["seen"] += 1
                # cheap pre-filter by year from the card title, saves a request
                if not matches(stub, year_from, year_to, None) and stub.get("year"):
                    store.skipped.setdefault(stub["lot_id"], {"year": stub["year"], "transmission_norm": None})
                    stats["filtered_out"] += 1
                    continue
                if not refresh:
                    if stub["lot_id"] in store:
                        continue
                    prev = store.skipped.get(stub["lot_id"])
                    # already fetched once and rejected by the *current* filters -> don't refetch
                    if prev is not None and not matches(
                        prev, year_from, year_to, transmission if prev.get("transmission_norm") else None
                    ):
                        continue
                try:
                    lot = parse_lot(fetcher.get(stub["url"]), stub["url"])
                except Exception as e:
                    log.error("lot %s failed: %s", stub["url"], e)
                    stats["errors"] += 1
                    continue
                lot.setdefault("lot_id", stub["lot_id"])
                if lot.get("price_usd") is None and stub.get("listing_price"):
                    lot["price_usd"] = stub["listing_price"]
                lot["listing_thumb"] = stub.get("thumb")
                lot["scraped_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                lot["text_damage_score"] = text_damage_score(lot)
                stats["new"] += 1
                if matches(lot, year_from, year_to, transmission):
                    store.upsert(lot)
                    stats["kept"] += 1
                else:
                    store.skip(lot)
                    stats["filtered_out"] += 1
            store.save()  # checkpoint after every page
            known_streak = known_streak + 1 if new_on_page == 0 else 0
            if known_streak >= stop_after_known and not refresh:
                log.info("%s: %d pages without new lots, stopping", slug, known_streak)
                break
            url = next_page_url(html, url) or listing_url(make, slug, page + 1)
            page += 1
    return stats
