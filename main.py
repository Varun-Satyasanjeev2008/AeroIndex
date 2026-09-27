"""
main.py – AeroIndex pipeline orchestrator.

Run with:
    python main.py

Steps
-----
1. Load .env, validate critical env vars
2. Apply database schema (idempotent)
3. Upsert route weights from config
4. Check robots.txt for all configured scrape targets
5. Scrape fares for each allowed route
6. Clean fare data (dedup, outlier flag, fare split)
7. Store cleaned fares in Postgres
8. Compute weighted price index
9. Write index to daily_index table
10. Check for spike; if triggered, call Groq and store explanation
11. Print summary
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

# ── Load env FIRST (before any config import that reads os.getenv) ────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aeroindex.main")


def _check_env() -> None:
    """Validate required environment variables before running the pipeline."""
    missing = []
    if not os.getenv("DATABASE_URL"):
        missing.append("DATABASE_URL")
    # GROQ_API_KEY is only required when a spike is actually detected;
    # spike_explainer.py will raise at that point with a clear message.
    if missing:
        logger.error(
            "Missing required environment variables: %s\n"
            "Copy .env.example → .env and fill in the values.",
            ", ".join(missing),
        )
        sys.exit(1)


def main() -> None:
    _check_env()

    # ── Imports (after env is loaded) ─────────────────────────────────────────
    from config import ROUTE_BASKET, SCRAPE_TARGETS
    from db.connection import get_conn, run_schema, upsert_route_weights
    from scraper.robots_check import check_all_targets
    from scraper.fetch import fetch_fares_for_route
    from pipeline.clean import clean
    from pipeline.store import store_fares
    from pipeline.index import compute_index, write_index
    from pipeline.spike_explainer import maybe_explain_spike

    # ── Step 1: Schema bootstrap ──────────────────────────────────────────────
    logger.info("=== Step 1: Applying database schema …")
    run_schema()

    # ── Step 2: Upsert route weights ──────────────────────────────────────────
    logger.info("=== Step 2: Syncing route weights to DB …")
    upsert_route_weights(ROUTE_BASKET)

    # ── Step 3: robots.txt gate ───────────────────────────────────────────────
    logger.info("=== Step 3: Checking robots.txt for all scrape targets …")
    allowed_targets = check_all_targets(SCRAPE_TARGETS)

    if not allowed_targets:
        logger.error(
            "All scrape targets are disallowed by robots.txt. "
            "No data can be collected. Consider requesting a data partnership."
        )
        sys.exit(0)

    # ── Step 4–5: Scrape ──────────────────────────────────────────────────────
    logger.info("=== Step 4–5: Scraping fares …")
    all_raw_fares: list[dict] = []
    for route in allowed_targets:
        logger.info("  Fetching route %s …", route)
        fares = fetch_fares_for_route(route)
        if not fares:
            logger.warning("  No fares retrieved for route %s.", route)
        else:
            logger.info("  ✓ %d observation(s) for %s.", len(fares), route)
        all_raw_fares.extend(fares)

    if not all_raw_fares:
        logger.error(
            "No fare data retrieved from any route. "
            "Check network connectivity and selector freshness in scraper/fetch.py."
        )
        sys.exit(1)

    # ── Step 6: Clean ─────────────────────────────────────────────────────────
    logger.info("=== Step 6: Cleaning fare data …")
    cleaned_df = clean(all_raw_fares)

    if cleaned_df.empty:
        logger.error("Cleaning produced an empty DataFrame – aborting.")
        sys.exit(1)

    # ── Step 7: Store fares ───────────────────────────────────────────────────
    logger.info("=== Step 7: Storing fares in Postgres …")
    with get_conn() as conn:
        n_inserted = store_fares(cleaned_df, conn)
    logger.info("  %d fare rows inserted.", n_inserted)

    # ── Step 8–9: Index computation ───────────────────────────────────────────
    logger.info("=== Step 8–9: Computing index and writing to DB …")
    with get_conn() as conn:
        index_result = compute_index(cleaned_df, conn)

        if index_result is None:
            logger.error(
                "Index computation returned None – insufficient data. "
                "Ensure at least one route has a valid non-outlier fare."
            )
            sys.exit(1)

        write_index(index_result, conn)

    # ── Step 10: Spike explanation ────────────────────────────────────────────
    logger.info("=== Step 10: Checking for index spike …")
    explanation: str | None = None
    try:
        with get_conn() as conn:
            explanation = maybe_explain_spike(index_result, conn)
    except EnvironmentError as env_err:
        # GROQ_API_KEY missing – print the helpful message and stop
        print("\n" + str(env_err) + "\n", file=sys.stderr)
        logger.warning(
            "Spike detected but GROQ_API_KEY is missing – "
            "explanation not generated. Set the key and re-run."
        )

    # ── Step 11: Summary ──────────────────────────────────────────────────────
    print("\n" + "─" * 60)
    print("  AeroIndex Pipeline — Summary")
    print("─" * 60)
    print(f"  Date          : {index_result.date}")
    print(f"  Index value   : {index_result.index_value:.4f}")
    if index_result.pct_change is not None:
        print(f"  Day Δ         : {index_result.pct_change:+.2f}%")
    else:
        print("  Day Δ         : N/A (first run – base prices bootstrapped)")

    print("\n  Per-route breakdown:")
    for route, info in index_result.per_route.items():
        print(
            f"    {route}  ₹{info['price_today']:.0f}  "
            f"(relative={info['price_relative']:.3f}, weight={info['weight']:.2f})"
        )

    if explanation:
        print(f"\n  ⚠ Spike explanation:\n  {explanation}")
    else:
        print("\n  No spike detected (or first run).")

    print("─" * 60 + "\n")


if __name__ == "__main__":
    main()
