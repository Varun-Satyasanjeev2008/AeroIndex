"""
scraper/robots_check.py – robots.txt compliance checker.

For every domain we intend to scrape, call `is_scraping_allowed()`
BEFORE issuing any HTTP request to that domain.  If the function
returns False, skip that domain and log the reason.
"""

from __future__ import annotations

import logging
import urllib.robotparser
from functools import lru_cache
from urllib.parse import urlparse

import requests

from config import ROBOTS_USER_AGENT

logger = logging.getLogger(__name__)

# Hard timeout (seconds) for fetching robots.txt. Prevents an unresponsive
# or slow server (e.g. behind anti-bot middleware) from hanging the pipeline.
ROBOTS_FETCH_TIMEOUT = 5


@lru_cache(maxsize=64)
def _get_parser(robots_url: str) -> urllib.robotparser.RobotFileParser:
    """
    Fetch and parse robots.txt for a given robots_url (cached per URL).
    Returns a parser even if robots.txt is unreachable or slow
    (permissive default) — never blocks longer than ROBOTS_FETCH_TIMEOUT.
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        resp = requests.get(
            robots_url,
            timeout=ROBOTS_FETCH_TIMEOUT,
            headers={"User-Agent": ROBOTS_USER_AGENT},
        )
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
            logger.debug("Fetched robots.txt from %s", robots_url)
        else:
            # Non-200 (404 etc.) — treat as permissive, matches robotparser's
            # own convention for a missing robots.txt.
            logger.debug(
                "robots.txt at %s returned status %s – treating as permissive.",
                robots_url,
                resp.status_code,
            )
            rp.parse([])
    except requests.exceptions.RequestException as exc:
        # Network error or timeout: treat as permissive (common convention)
        logger.warning(
            "Could not fetch robots.txt from %s within %ss (%s). "
            "Treating as permissive – proceed with caution.",
            robots_url,
            ROBOTS_FETCH_TIMEOUT,
            exc,
        )
        rp.parse([])
    return rp


def is_scraping_allowed(url: str) -> bool:
    """
    Return True if ROBOTS_USER_AGENT is allowed to fetch *url* per
    the site's robots.txt; False otherwise.

    Parameters
    ----------
    url : str
        The full URL we intend to scrape.

    Returns
    -------
    bool
        True  → scraping allowed (or robots.txt unreachable).
        False → robots.txt explicitly disallows our path.
    """
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    rp = _get_parser(robots_url)
    allowed = rp.can_fetch(ROBOTS_USER_AGENT, url)

    if not allowed:
        logger.warning(
            "⛔  robots.txt DISALLOWS scraping '%s' for agent '%s'. "
            "Skipping – consider requesting an official data partnership or API access.",
            url,
            ROBOTS_USER_AGENT,
        )
    else:
        logger.debug("✅  robots.txt allows scraping '%s'.", url)

    return allowed


def check_all_targets(targets: dict[str, list[tuple[str, str]]]) -> dict[str, list[tuple[str, str]]]:
    """
    Filter SCRAPE_TARGETS down to only URL/source pairs that pass the
    robots.txt check.

    Parameters
    ----------
    targets : dict[route, list[(source_label, url)]]

    Returns
    -------
    dict[route, list[(source_label, url)]]
        Same structure, with disallowed URLs removed.
        Routes with NO remaining URLs are also removed.
    """
    allowed: dict[str, list[tuple[str, str]]] = {}
    for route, sources in targets.items():
        permitted = []
        for label, url in sources:
            if is_scraping_allowed(url):
                permitted.append((label, url))
            else:
                logger.warning(
                    "Route %s / source '%s' excluded (robots.txt). "
                    "Requires official API / data partnership.",
                    route,
                    label,
                )
        if permitted:
            allowed[route] = permitted
        else:
            logger.warning(
                "Route %s: ALL sources excluded by robots.txt – "
                "no data will be collected for this route.",
                route,
            )
    return allowed