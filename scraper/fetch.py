"""
scraper/fetch.py – Scrapling-based fare fetcher for AeroIndex.

Architecture
------------
We use Scrapling's StealthyFetcher (Playwright-backed, stealth mode) for
sites that require JavaScript rendering, and fall back to the lighter
Fetcher (HTTPX-based) for purely static pages.

Scrape targets currently include EaseMyTrip and MakeMyTrip listing pages.
Selectors are source-specific (different sites, different markup) and
should be re-validated whenever a site updates its structure.

Rate limiting
-------------
A configurable delay (RATE_LIMIT_DELAY, default 2 s) is enforced between
consecutive requests to the *same domain*.  The delay is tracked per-domain
in a module-level dict so it persists across calls within a single pipeline
run.

Output
------
Each call to `fetch_fares_for_route()` returns a list of dicts:

    {
        "route":    "DEL-BOM",
        "source":   "easemytrip",
        "scraped_at": datetime (UTC),
        "raw_price": float,          # cheapest available fare found
        "flight_date": date | None,  # departure date if extractable
        "advance_purchase_days": int | None,
    }

Returns [] if robots.txt denied the URL or scraping fails entirely.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone, date
from typing import Any

from config import RATE_LIMIT_DELAY, SCRAPE_TARGETS, PROXY_LIST
from scraper.robots_check import is_scraping_allowed

logger = logging.getLogger(__name__)

# Track last-request timestamp per domain to enforce rate limits
_last_request_time: dict[str, float] = {}
# ── Proxy rotation ──────────────────────────────────────────────────────────
_proxy_index = 0

def _next_proxy() -> str | None:
    """Return the next proxy in round-robin order, or None if none configured."""
    global _proxy_index
    if not PROXY_LIST:
        return None
    proxy = PROXY_LIST[_proxy_index % len(PROXY_LIST)]
    _proxy_index += 1
    return proxy
# ── Per-source selector configuration ──────────────────────────────────────
# Each site has different markup, so selectors (and the DOM-ready wait
# condition used by StealthyFetcher) are keyed by source label.
#
# `wait_selector` should be the parent/child pattern confirmed to only
# appear once real fare data has rendered (not a loading skeleton).
SOURCE_SELECTORS: dict[str, dict] = {
    "easemytrip": {
        "wait_selector": ".flt_prc h4",
        "css_selectors": [
            ".flt_prc h4",
            "[id^='spnPrice']",
        ],
        "xpath_selectors": [
            "//div[contains(@class,'flt_prc')]//h4",
            "//*[starts-with(@id,'spnPrice')]",
        ],
    },
    "makemytrip": {
        # TODO: confirmed selectors pending — inspect debug_*_makemytrip.html
        # and update these once real markup is found (same process used
        # for easemytrip: dump HTML, grep for ₹, find the stable class).
        "wait_selector": None,   # no confirmed selector yet — falls back to network_idle only
        "css_selectors": [],
        "xpath_selectors": [],
    },
}

_DEFAULT_WAIT_SELECTOR = None  # used for any source not listed above


def _rate_limit(domain: str) -> None:
    """Sleep if needed to respect RATE_LIMIT_DELAY for *domain*."""
    last = _last_request_time.get(domain, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < RATE_LIMIT_DELAY:
        sleep_for = RATE_LIMIT_DELAY - elapsed
        logger.debug("Rate-limit: sleeping %.2f s for domain %s", sleep_for, domain)
        time.sleep(sleep_for)
    _last_request_time[domain] = time.monotonic()


def _parse_price(text: str) -> float | None:
    """
    Extract the first numeric value from a price string like '₹4,530' or '4530'.
    Returns None if no numeric value is found.
    """
    clean = re.sub(r"[^\d.]", "", text.replace(",", ""))
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def _try_stealthy_fetch(url: str, source_label: str) -> Any | None:
    """
    Attempt a Scrapling StealthyFetcher fetch (Playwright stealth mode).
    Waits for the source's confirmed price selector to render before
    returning, when one is configured. Routes through a rotating proxy
    from PROXY_LIST if one is configured (see config.py); otherwise
    fetches directly from this machine's IP. Returns a Scrapling Page
    object or None on failure.
    """
    source_cfg = SOURCE_SELECTORS.get(source_label, {})
    wait_selector = source_cfg.get("wait_selector", _DEFAULT_WAIT_SELECTOR)

    fetch_kwargs: dict[str, Any] = {
        "timeout": 30000,  # milliseconds — bump if pages still load slowly
        "disable_resources": True,  # skip fonts/images/media — we only need text/price data
        "block_ads": True,          # block ~3,500 known ad/tracking domains (speeds up network_idle)
    }
    if wait_selector:
        fetch_kwargs["wait_selector"] = wait_selector
        fetch_kwargs["wait_selector_state"] = "visible"
    else:
        fetch_kwargs["network_idle"] = True  # only fall back to this when no selector to wait for

    proxy = _next_proxy()
    if proxy:
        fetch_kwargs["proxy"] = proxy
        logger.debug("Using proxy for %s", url)

    try:
        from scrapling.fetchers import StealthyFetcher
        fetcher = StealthyFetcher()
        page = fetcher.fetch(url, **fetch_kwargs)
        return page
    except ImportError:
        logger.error(
            "StealthyFetcher not available – ensure scrapling[fetchers] is installed."
        )
        return None
    except Exception as exc:
        logger.warning("StealthyFetcher failed for %s: %s", url, exc)
        return None

def _try_plain_fetch(url: str) -> Any | None:
    """
    Attempt a Scrapling Fetcher (HTTPX) fetch for lighter pages.
    Returns a Scrapling Page object or None on failure.
    """
    try:
        from scrapling.fetchers import Fetcher
        fetcher = Fetcher()
        page = fetcher.fetch(url)
        return page
    except ImportError:
        logger.error("Fetcher not available – ensure scrapling is installed.")
        return None
    except Exception as exc:
        logger.warning("Fetcher failed for %s: %s", url, exc)
        return None


def _extract_fares_from_page(page: Any, route: str, source: str, url: str) -> list[dict]:
    """
    Extract raw fare data from a Scrapling Page using the source's
    configured CSS/XPath selectors (see SOURCE_SELECTORS above).

    NOTE: Web page structures change frequently. If this returns [] after a
    known-good fetch, re-inspect the page with browser DevTools and update
    SOURCE_SELECTORS for that source.
    """
    scraped_at = datetime.now(timezone.utc)
    fares = []

    source_cfg = SOURCE_SELECTORS.get(source)
    if not source_cfg:
        logger.warning(
            "No selectors configured for source=%s (route=%s) – skipping extraction.",
            source,
            route,
        )
        return []

    raw_prices: list[float] = []

    for selector in source_cfg.get("css_selectors", []):
        try:
            elements = page.css(selector)
            for el in elements:
                text = el.text if hasattr(el, "text") else str(el)
                price = _parse_price(text)
                if price and 500 < price < 100_000:  # sanity bounds (INR)
                    raw_prices.append(price)
        except Exception:
            pass

    for selector in source_cfg.get("xpath_selectors", []):
        try:
            elements = page.xpath(selector)
            for el in elements:
                text = el.text if hasattr(el, "text") else str(el)
                price = _parse_price(text)
                if price and 500 < price < 100_000:
                    raw_prices.append(price)
        except Exception:
            pass

    if not raw_prices:
        logger.warning(
            "No prices extracted from %s (route=%s, source=%s). "
            "Page structure may have changed, or selectors not yet configured.",
            url,
            route,
            source,
        )
        return []

    # Use the minimum fare as the representative price (cheapest available)
    cheapest = min(raw_prices)
    logger.info(
        "Route %s | source=%s | found %d prices | cheapest=₹%.0f",
        route,
        source,
        len(raw_prices),
        cheapest,
    )

    fares.append(
        {
            "route": route,
            "source": source,
            "scraped_at": scraped_at,
            "raw_price": cheapest,
            "flight_date": None,           # date extraction is source-specific
            "advance_purchase_days": None,
        }
    )
    return fares


def fetch_fares_for_route(route: str) -> list[dict]:
    """
    Fetch fare observations for *route* from all configured sources.

    Applies:
    1. robots.txt check (skips disallowed URLs)
    2. Rate-limit delay per domain
    3. StealthyFetcher → Fetcher fallback

    Returns a (possibly empty) list of fare dicts.
    """
    targets = SCRAPE_TARGETS.get(route, [])
    if not targets:
        logger.warning("No scrape targets configured for route %s.", route)
        return []

    all_fares: list[dict] = []

    for source_label, url in targets:
        # ── robots.txt gate ───────────────────────────────────────────────────
        if not is_scraping_allowed(url):
            # Message already logged by is_scraping_allowed
            continue

        # ── Rate limit ────────────────────────────────────────────────────────
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        _rate_limit(domain)

        logger.info("Fetching %s for route %s (source=%s) …", url, route, source_label)

        # ── Fetch: try StealthyFetcher first, fall back to Fetcher ────────────
        page = _try_stealthy_fetch(url, source_label)
        if page is None:
            logger.info("Falling back to plain Fetcher for %s", url)
            page = _try_plain_fetch(url)

        if page is None:
            logger.error("All fetch methods failed for %s – skipping.", url)
            continue

        # ── DEBUG: dump raw HTML to inspect structure (remove once stable) ────
        with open(f"debug_{route}_{source_label}.html", "w", encoding="utf-8") as f:
            f.write(page.html_content)
        print(
            f"[DEBUG] {route} ({source_label}): ₹ present = {'₹' in page.html_content}, "
            f"length = {len(page.html_content)}"
        )

        # ── Extract fares ─────────────────────────────────────────────────────
        fares = _extract_fares_from_page(page, route, source_label, url)
        all_fares.extend(fares)

    return all_fares


def fetch_all_routes() -> dict[str, list[dict]]:
    """
    Fetch fare data for every route in SCRAPE_TARGETS.
    Returns dict[route → list[fare_dict]].
    """
    results: dict[str, list[dict]] = {}
    for route in SCRAPE_TARGETS:
        fares = fetch_fares_for_route(route)
        results[route] = fares
        logger.info("Route %s: %d observations fetched.", route, len(fares))
    return results