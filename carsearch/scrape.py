"""Crawl bidfax.info listing pages for a make/model and collect sold lots."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .damage import text_damage_score
from .fetch import Fetcher
from .parse import BASE, next_page_url, parse_listing, parse_lot
from .storage import Store
from .variants import detect

log = logging.getLogger(__name__)

# bidfax uses several slugs for the same car over the years
DEFAULT_MODEL_SLUGS = ["mx-5-miata", "mx-5", "mx-5-miata-rf"]  # a missing slug is skipped (404)


def listing_url(make: str, model_slug: str, page: int) -> str:
    base = f"{BASE}/{make}/{model_slug}/"
    return base if page == 1 else f"{base}page/{page}/"


@dataclass
class Filters:
    year_from: int | None = 2016
    year_to: int | None = None
    transmission: str | None = None  # manual / automatic / None = any
    roof: str | None = None  # soft / hard / None = any
    trims: list[str] = field(default_factory=list)  # sport / club / grand_touring / other; empty = any
    include_unknown: bool = False  # keep lots whose roof / trim / transmission could not be determined


def matches(lot: dict, f: Filters, partial: bool = False) -> bool:
    """``partial=True`` is for listing cards and the skip registry: a missing value is
    not a reason to reject, because the lot page may still provide it."""
    y = lot.get("year")
    if y is None:
        if not partial and (f.year_from or f.year_to):
            return False
    else:
        if f.year_from and y < f.year_from:
            return False
        if f.year_to and y > f.year_to:
            return False
    for key, wanted in (("transmission_norm", {f.transmission} if f.transmission else None),
                        ("roof", {f.roof} if f.roof else None),
                        ("trim", set(f.trims) if f.trims else None)):
        if not wanted:
            continue
        val = lot.get(key)
        if val is None:
            if not (partial or f.include_unknown):
                return False
        elif val not in wanted:
            return False
    return True


def scrape(
    fetcher: Fetcher,
    store: Store,
    make: str = "mazda",
    model_slugs: list[str] | None = None,
    filters: Filters | None = None,
    max_pages: int = 20,
    stop_after_known: int = 2,
    refresh: bool = False,
) -> dict:
    """Walk listing pages newest→oldest. Stops when ``stop_after_known`` pages in a row
    contain only lots we already have (incremental updates), or after ``max_pages``."""
    f = filters or Filters()
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
                # cheap pre-filter on the card title (year, RF, trim, VIN), saves a request
                stub.update({k: v for k, v in detect(stub).items() if k in ("roof", "trim")})
                if not matches(stub, f, partial=True):
                    store.skipped.setdefault(stub["lot_id"], {k: stub.get(k) for k in ("year", "roof", "trim")})
                    stats["filtered_out"] += 1
                    continue
                if not refresh:
                    if stub["lot_id"] in store:
                        continue
                    prev = store.skipped.get(stub["lot_id"])
                    # already fetched once and rejected by the *current* filters -> don't refetch
                    if prev is not None and not matches(prev, f, partial=True):
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
                lot.update(detect(lot))
                stats["new"] += 1
                if matches(lot, f):
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
