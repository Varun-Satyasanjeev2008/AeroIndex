"""
pipeline/index.py – Weighted price-index computation for AeroIndex.

Formula
-------
    AeroIndex_t = Σ_i  w_i × (P_it / P_i0) × 100

Where:
    w_i  = route weight (from route_weights table / config.py)
    P_it = today's representative fare for route i (median of non-outlier obs.)
    P_i0 = base-period fare for route i (stored in route_weights.base_price)

Base-period bootstrap
---------------------
If no base_price exists for a route (first-ever run), the current
observation is set as the base (index = 100).  The pipeline writes
this base_price to route_weights so subsequent runs compute relative
changes.

Output
------
Returns an IndexResult dataclass with:
    - date: today's date
    - index_value: float (e.g. 103.2)
    - pct_change: float | None (vs. previous day's stored index)
    - per_route: dict[route → {"price_today", "base_price", "price_relative"}]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class IndexResult:
    date: date
    index_value: float
    pct_change: float | None
    per_route: dict[str, dict[str, Any]] = field(default_factory=dict)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _get_route_weights(conn) -> dict[str, dict]:
    """
    Load current weights and base prices from route_weights table.
    Returns dict[route → {weight, base_price}].
    """
    with conn.cursor() as cur:
        cur.execute("SELECT route, weight, base_price FROM route_weights;")
        rows = cur.fetchall()
    return {
        row[0]: {"weight": float(row[1]), "base_price": float(row[2]) if row[2] else None}
        for row in rows
    }


def _get_previous_index(conn) -> float | None:
    """Return the most recent stored index value (excluding today), or None."""
    today = date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT index_value FROM daily_index
            WHERE date < %s
            ORDER BY date DESC LIMIT 1;
            """,
            (today,),
        )
        row = cur.fetchone()
    return float(row[0]) if row else None


def compute_index(cleaned_df: pd.DataFrame, conn) -> IndexResult | None:
    """
    Compute today's AeroIndex from *cleaned_df* (non-outlier rows only).

    Parameters
    ----------
    cleaned_df : pd.DataFrame
        Output of pipeline.clean.clean().
    conn : psycopg2 connection (open, caller manages lifetime).

    Returns
    -------
    IndexResult | None
        None if insufficient data to compute an index (e.g. no routes covered).
    """
    today = date.today()

    # Only use non-outlier rows
    df = cleaned_df[~cleaned_df["is_outlier"]].copy()
    if df.empty:
        logger.warning("compute_index: no non-outlier rows available.")
        return None

    route_weights = _get_route_weights(conn)
    if not route_weights:
        logger.error(
            "route_weights table is empty – run db.connection.upsert_route_weights() first."
        )
        return None

    # Compute per-route representative price (median of today's observations)
    today_prices: dict[str, float] = {}
    for route, grp in df.groupby("route"):
        today_prices[str(route)] = float(grp["raw_price"].median())

    covered_routes = set(today_prices.keys()) & set(route_weights.keys())
    if not covered_routes:
        logger.warning("No overlap between scraped routes and configured weights.")
        return None

    per_route: dict[str, dict] = {}
    weighted_sum = 0.0
    total_weight = 0.0
    new_base_prices: dict[str, float] = {}

    for route in covered_routes:
        p_today = today_prices[route]
        cfg = route_weights[route]
        w = cfg["weight"]
        p_base = cfg["base_price"]

        if p_base is None:
            # First run: bootstrap base price
            logger.info(
                "Route %s: no base price found – bootstrapping from today's price (%.2f).",
                route, p_today,
            )
            p_base = p_today
            new_base_prices[route] = p_today

        price_relative = p_today / p_base
        contribution = w * price_relative * 100

        per_route[route] = {
            "price_today": p_today,
            "base_price": p_base,
            "price_relative": price_relative,
            "weight": w,
            "weighted_contribution": contribution,
        }

        weighted_sum += contribution
        total_weight += w

    if total_weight == 0:
        logger.error("Total weight is 0 – cannot compute index.")
        return None

    # Normalise in case some routes had no data (partial coverage)
    index_value = weighted_sum / total_weight * 1.0   # already ×100 in loop

    # Actually: if total_weight < 1.0, we're missing routes → partial index
    # Re-scale so weights sum to 1 for partial coverage transparency
    if abs(total_weight - 1.0) > 1e-6:
        logger.warning(
            "Partial route coverage (total_weight=%.3f). "
            "Index is computed over covered routes only.",
            total_weight,
        )
        index_value = (weighted_sum / total_weight) if total_weight else 0.0

    # Persist new base prices if bootstrapped
    if new_base_prices:
        _write_base_prices(conn, new_base_prices)

    # Previous index for pct_change
    prev = _get_previous_index(conn)
    pct_change: float | None = None
    if prev is not None and prev != 0:
        pct_change = (index_value - prev) / prev * 100

    result = IndexResult(
        date=today,
        index_value=round(index_value, 4),
        pct_change=round(pct_change, 4) if pct_change is not None else None,
        per_route=per_route,
    )

    logger.info(
        "AeroIndex [%s] = %.4f  (pct_change=%s)",
        today,
        index_value,
        f"{pct_change:+.2f}%" if pct_change is not None else "N/A (first run)",
    )

    return result


def _write_base_prices(conn, new_base_prices: dict[str, float]) -> None:
    """Update route_weights.base_price for bootstrapped routes."""
    with conn.cursor() as cur:
        for route, bp in new_base_prices.items():
            cur.execute(
                """
                UPDATE route_weights
                SET base_price = %s, updated_at = NOW()
                WHERE route = %s;
                """,
                (bp, route),
            )
    logger.info("Base prices bootstrapped for: %s", list(new_base_prices.keys()))


def write_index(result: IndexResult, conn) -> None:
    """
    Persist an IndexResult to the daily_index table.
    Uses ON CONFLICT DO UPDATE to allow re-runs on the same day to overwrite.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO daily_index (date, index_value, pct_change, computed_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (date) DO UPDATE
                SET index_value = EXCLUDED.index_value,
                    pct_change  = EXCLUDED.pct_change,
                    computed_at = EXCLUDED.computed_at;
            """,
            (result.date, result.index_value, result.pct_change, result.computed_at),
        )
    logger.info("daily_index row written for %s.", result.date)
