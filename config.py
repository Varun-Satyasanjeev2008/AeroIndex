"""
config.py – Central configuration for AeroIndex.

IMPORTANT: The route weights below are ILLUSTRATIVE PLACEHOLDERS
derived from publicly reported traffic share estimates. They MUST be
replaced with real DGCA (Directorate General of Civil Aviation) O&D
traffic-share data before use in any official statistical publication.

Official DGCA data: https://www.dgca.gov.in/digigov-portal/?page=statistics
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ─── Route basket ─────────────────────────────────────────────────────────────
# Each entry: route_code → {"weight": float, "base_price": float | None}
# base_price is the reference price used for price-relative computation.
# Set to None initially; the pipeline will populate it from the first
# successful scrape batch and store it in the route_weights table.

ROUTE_BASKET: dict[str, dict] = {
    "DEL-BOM": {
        "weight": 0.45,   # illustrative – replace with DGCA traffic share
        "base_price": None,
    },
    "DEL-BLR": {
        "weight": 0.35,   # illustrative – replace with DGCA traffic share
        "base_price": None,
    },
    "BOM-BLR": {
        "weight": 0.20,   # illustrative – replace with DGCA traffic share
        "base_price": None,
    },
}

# Weights must sum to 1.0 (checked at import time)
_total_weight = sum(r["weight"] for r in ROUTE_BASKET.values())
assert abs(_total_weight - 1.0) < 1e-9, (
    f"Route weights must sum to 1.0, got {_total_weight}"
)

# ─── Scraping settings ────────────────────────────────────────────────────────
# Seconds to wait between consecutive requests to the *same* domain.
RATE_LIMIT_DELAY: float = float(os.getenv("RATE_LIMIT_DELAY", "2"))

# User-agent string sent with non-stealth requests (robots.txt check).
ROBOTS_USER_AGENT: str = "AeroIndexBot/1.0 (+https://github.com/your-org/aeroindex)"

# ─── Index / spike settings ───────────────────────────────────────────────────
# Day-over-day index change (%) that triggers an LLM-generated explanation.
SPIKE_THRESHOLD: float = float(os.getenv("SPIKE_THRESHOLD", "5.0"))

# How many days of look-ahead/look-back to check in the festival calendar.
CALENDAR_LOOKAHEAD_DAYS: int = 3

# ─── Groq settings ───────────────────────────────────────────────────────────
GROQ_MODEL: str = "llama3-8b-8192"   # free-tier model
GROQ_MAX_TOKENS: int = 256

# ─── Scrape target URLs ───────────────────────────────────────────────────────
# Maps route_code → list of (label, url) tuples to try (in order).
#
# NOTE: URLs are built dynamically per run so the search date stays
# N days ahead instead of going stale. Always re-verify via
# robots_check.py before adding new targets.

# City metadata needed to build search URLs: code → city name
_AIRPORT_LABELS: dict[str, str] = {
    "DEL": "Delhi",
    "BOM": "Mumbai",
    "BLR": "Bangalore",
}

# How many days ahead to search for (avoids booking near-term/sold-out dates)
SCRAPE_SEARCH_DAYS_AHEAD: int = 14
# ─── Proxy settings ────────────────────────────────────────────────────────
# Comma-separated proxy URLs (e.g. "http://user:pass@host:port,http://...").
# Empty/unset = no proxy, requests go out from this machine's IP directly.
_proxy_list_raw = os.getenv("PROXY_LIST", "").strip()
PROXY_LIST: list[str] = [p.strip() for p in _proxy_list_raw.split(",") if p.strip()]

def _build_easemytrip_url(origin: str, dest: str) -> str:
    """Build a live EaseMyTrip search-results URL for origin→dest, N days out."""
    search_date = (datetime.now() + timedelta(days=SCRAPE_SEARCH_DAYS_AHEAD)).strftime("%d/%m/%Y")
    origin_label = f"{origin}-{_AIRPORT_LABELS[origin]}-India"
    dest_label = f"{dest}-{_AIRPORT_LABELS[dest]}-India"
    srch = f"{origin_label}|{dest_label}|{search_date}"
    return (
        "https://www.easemytrip.com/flight-search/listing"
        f"?srch={srch}"
        "&px=1-0-0&cbn=0&ar=undefined&isow=true&isdm=true"
        "&lang=en-us&IsDoubleSeat=false&CCODE=IN&curr=INR&apptype=B2C"
    )


def _build_makemytrip_url(origin: str, dest: str) -> str:
    """Build a live MakeMyTrip search-results URL for origin→dest, N days out."""
    search_date = (datetime.now() + timedelta(days=SCRAPE_SEARCH_DAYS_AHEAD)).strftime("%d/%m/%Y")
    itinerary = f"{origin}-{dest}-{search_date}"
    return (
        "https://www.makemytrip.com/flight/search"
        f"?itinerary={itinerary}"
        "&tripType=O&paxType=A-1_C-0_I-0&intl=false&cabinClass=E&lang=eng"
    )


# Maps route_code → list of (label, url) tuples to try (in order).
SCRAPE_TARGETS: dict[str, list[tuple[str, str]]] = {
    "DEL-BOM": [
        ("easemytrip", _build_easemytrip_url("DEL", "BOM")),
        ("makemytrip", _build_makemytrip_url("DEL", "BOM")),
    ],
    "DEL-BLR": [
        ("easemytrip", _build_easemytrip_url("DEL", "BLR")),
        ("makemytrip", _build_makemytrip_url("DEL", "BLR")),
    ],
    "BOM-BLR": [
        ("easemytrip", _build_easemytrip_url("BOM", "BLR")),
        ("makemytrip", _build_makemytrip_url("BOM", "BLR")),
    ],
}