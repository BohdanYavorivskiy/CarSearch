"""HTTP fetching with polite rate limiting, disk cache and pluggable backends.

bidfax.info sits behind Cloudflare. Three backends are available:

* ``requests``   – plain HTTP. Works when you pass a valid ``cf_clearance`` cookie
                   together with the *same* User-Agent of the browser that got it.
* ``curl``       – ``curl_cffi`` with a Chrome TLS fingerprint (often passes CF).
* ``playwright`` – a real Chromium; slowest but the most robust.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.6",
}


class NotFoundError(RuntimeError):
    """HTTP 404 – not retried."""


class BlockedError(RuntimeError):
    """Raised when the site answers with a Cloudflare challenge / 403."""


def _parse_cookie_string(raw: str) -> dict[str, str]:
    cookies = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _looks_blocked(status: int, text: str) -> bool:
    if status in (403, 429, 503):
        return True
    head = text[:5000].lower()
    return "just a moment" in head or "cf-chl" in head or "challenge-platform" in head


class Fetcher:
    def __init__(
        self,
        backend: str = "requests",
        delay: float = 2.5,
        cache_dir: str | Path | None = ".cache/html",
        cookies: str | None = None,
        user_agent: str | None = None,
        retries: int = 3,
    ):
        self.backend = backend
        self.delay = delay
        self.retries = retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent or os.environ.get("BIDFAX_USER_AGENT") or DEFAULT_UA
        cookie_str = cookies or os.environ.get("BIDFAX_COOKIE", "")
        if cookie_str and Path(cookie_str).is_file():
            cookie_str = Path(cookie_str).read_text().strip()
        self.cookies = _parse_cookie_string(cookie_str) if cookie_str else {}
        self._last = 0.0
        self._session = None
        self._pw = None
        self._page = None

    # ------------------------------------------------------------------ utils
    def _sleep(self):
        wait = self.delay * random.uniform(0.7, 1.3) - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".html")

    # --------------------------------------------------------------- backends
    def _get_session(self):
        if self._session is None:
            headers = {**BASE_HEADERS, "User-Agent": self.user_agent}
            if self.backend == "curl":
                from curl_cffi import requests as creq  # type: ignore

                self._session = creq.Session(impersonate="chrome", headers=headers)
            else:
                import requests

                self._session = requests.Session()
                self._session.headers.update(headers)
            for k, v in self.cookies.items():
                self._session.cookies.set(k, v, domain=".bidfax.info")
        return self._session

    def _fetch_http(self, url: str) -> str:
        r = self._get_session().get(url, timeout=40)
        if r.status_code == 404:
            raise NotFoundError(url)
        if _looks_blocked(r.status_code, r.text):
            raise BlockedError(f"{url}: HTTP {r.status_code} (Cloudflare?)")
        r.raise_for_status()
        return r.text

    def _fetch_playwright(self, url: str) -> str:
        if self._page is None:
            from playwright.sync_api import sync_playwright  # type: ignore

            self._pw = sync_playwright().start()
            try:
                headless = os.environ.get("BIDFAX_HEADLESS", "1") != "0"
                browser = self._pw.chromium.launch(headless=headless)
                ctx = browser.new_context(user_agent=self.user_agent, locale="uk-UA")
                if self.cookies:
                    ctx.add_cookies(
                        [{"name": k, "value": v, "domain": ".bidfax.info", "path": "/"} for k, v in self.cookies.items()]
                    )
                self._page = ctx.new_page()
            except Exception:
                # Stop the driver, otherwise the next retry fails with the misleading
                # "Sync API inside the asyncio loop" error instead of the real cause.
                self.close()
                raise
        resp = self._page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Give a Cloudflare JS challenge time to resolve itself.
        for _ in range(15):
            html = self._page.content()
            if not _looks_blocked(200, html):
                return html
            self._page.wait_for_timeout(2000)
        raise BlockedError(f"{url}: still on Cloudflare challenge (status {resp.status if resp else '?'})")

    # ------------------------------------------------------------------ public
    def get(self, url: str, use_cache: bool = True) -> str:
        cp = self._cache_path(url)
        if use_cache and cp and cp.exists():
            return cp.read_text(encoding="utf-8", errors="replace")
        last_err: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._sleep()
            try:
                html = self._fetch_playwright(url) if self.backend == "playwright" else self._fetch_http(url)
                if cp:
                    cp.write_text(html, encoding="utf-8")
                return html
            except NotFoundError:
                raise
            except BlockedError as e:
                last_err = e
                log.warning("blocked (attempt %d/%d): %s", attempt, self.retries, e)
                time.sleep(5 * attempt)
            except Exception as e:  # network errors
                last_err = e
                log.warning("error (attempt %d/%d) %s: %s", attempt, self.retries, url, e)
                time.sleep(3 * attempt)
        raise last_err  # type: ignore[misc]

    def get_bytes(self, url: str) -> bytes:
        self._sleep()
        if self.backend == "playwright" and self._page is not None:
            r = self._page.request.get(url)
            return r.body()
        r = self._get_session().get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def close(self):
        pw, self._pw, self._page = self._pw, None, None
        if pw:
            try:
                pw.stop()
            except Exception as e:  # already dead / half-started driver
                log.debug("playwright stop failed: %s", e)
