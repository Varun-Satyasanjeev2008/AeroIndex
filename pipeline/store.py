"""
pipeline/store.py – Write cleaned fare rows to Postgres.

Uses ON CONFLICT DO NOTHING on the (route, source, scraped_at) UNIQUE
constraint, so re-running the pipeline on the same day is safe.
"""

from __future__ import annotations

import logging
from datetime import timezone

import pandas as pd

logger = logging.getLogger(__name__)

_INSERT_FARE_SQL = """
INSERT INTO fares
    (route, source, scraped_at, flight_date, advance_purchase_days,
     raw_price, base_fare, taxes_fees, is_outlier)
VALUES
    (%(route)s, %(source)s, %(scraped_at)s, %(flight_date)s,
     %(advance_purchase_days)s, %(raw_price)s, %(base_fare)s,
     %(taxes_fees)s, %(is_outlier)s)
ON CONFLICT (route, source, scraped_at) DO NOTHING;
"""


def _nan_to_none(value):
    """Convert pandas NaN / float nan to None for psycopg2."""
    import math
    if value is None:
        return None
    try:
        if math.isnan(float(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value


def store_fares(df: pd.DataFrame, conn) -> int:
    """
    Bulk-insert cleaned fare rows into the fares table.

    Parameters
    ----------
    df : pd.DataFrame
        Output of pipeline.clean.clean().
    conn : open psycopg2 connection (caller manages transaction).

    Returns
    -------
    int
        Number of rows inserted (excludes conflicts).
    """
    if df.empty:
        logger.info("store_fares: nothing to insert.")
        return 0

    inserted = 0
    with conn.cursor() as cur:
        for _, row in df.iterrows():
            # Ensure scraped_at is timezone-aware for Postgres TIMESTAMP
            scraped_at = row["scraped_at"]
            if hasattr(scraped_at, "tzinfo") and scraped_at.tzinfo is None:
                scraped_at = scraped_at.replace(tzinfo=timezone.utc)

            params = {
                "route":                 row["route"],
                "source":                row["source"],
                "scraped_at":            scraped_at,
                "flight_date":           _nan_to_none(row.get("flight_date")),
                "advance_purchase_days": _nan_to_none(row.get("advance_purchase_days")),
                "raw_price":             float(row["raw_price"]),
                "base_fare":             _nan_to_none(row.get("base_fare")),
                "taxes_fees":            _nan_to_none(row.get("taxes_fees")),
                "is_outlier":            bool(row.get("is_outlier", False)),
            }
            cur.execute(_INSERT_FARE_SQL, params)
            if cur.rowcount > 0:
                inserted += 1

    logger.info("Inserted %d/%d fare rows (rest were duplicates).", inserted, len(df))
    return inserted
