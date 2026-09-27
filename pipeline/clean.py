"""
pipeline/clean.py – Pandas-based fare data cleaner for AeroIndex.

Cleaning steps (in order)
--------------------------
1. De-duplicate: keep the row with the lowest raw_price per
   (route, source, date-bucket).
2. Outlier detection: flag rows where raw_price > 3× the route's
   rolling median (computed over the entire batch + DB history).
3. Base fare / taxes split: if only total price is available, apply a
   configurable split ratio (derived from publicly available DGCA
   average breakdowns). Flag the split as estimated.

Input
-----
A list of fare dicts as returned by fetch.py:

    {
        "route":    str,
        "source":   str,
        "scraped_at": datetime,
        "raw_price": float,
        "flight_date": date | None,
        "advance_purchase_days": int | None,
    }

Output
------
A pandas DataFrame with the same columns plus:
    - is_outlier: bool
    - base_fare: float | NaN
    - taxes_fees: float | NaN
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pandas as pd

logger = logging.getLogger(__name__)

# ── Outlier config ─────────────────────────────────────────────────────────────
OUTLIER_MULTIPLIER: float = 3.0   # flag if price > N × route median

# ── Tax split config ───────────────────────────────────────────────────────────
# Industry rule-of-thumb: taxes + fees ≈ 15-25% of total fare for India
# domestic routes (DGCA/CAPA data, approximate).  Use 20% as default.
TAXES_PCT_OF_TOTAL: float = 0.20  # illustrative; replace with DGCA audit data


def fares_to_dataframe(fares: list[dict]) -> pd.DataFrame:
    """
    Convert a list of raw fare dicts to a typed DataFrame.
    Returns an empty DataFrame (with correct columns) if *fares* is empty.
    """
    columns = [
        "route", "source", "scraped_at", "raw_price",
        "flight_date", "advance_purchase_days",
        "base_fare", "taxes_fees", "is_outlier",
    ]

    if not fares:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(fares)

    # Ensure required columns exist with sensible defaults
    for col, default in [
        ("base_fare", float("nan")),
        ("taxes_fees", float("nan")),
        ("is_outlier", False),
        ("flight_date", None),
        ("advance_purchase_days", None),
    ]:
        if col not in df.columns:
            df[col] = default

    df["raw_price"] = pd.to_numeric(df["raw_price"], errors="coerce")
    df["scraped_at"] = pd.to_datetime(df["scraped_at"], utc=True)

    return df[columns]


def deduplicate(df: pd.DataFrame) -> pd.DataFrame:
    """
    Within each (route, source, date-bucket) keep the row with the
    lowest raw_price.  The date-bucket is truncated to the calendar day
    of scraped_at so multiple intra-day scrapes don't produce duplicates.
    """
    if df.empty:
        return df

    df = df.copy()
    df["_date_bucket"] = df["scraped_at"].dt.date

    df = (
        df.sort_values("raw_price")
          .drop_duplicates(subset=["route", "source", "_date_bucket"], keep="first")
          .drop(columns=["_date_bucket"])
          .reset_index(drop=True)
    )
    return df


def flag_outliers(df: pd.DataFrame, historical_df: pd.DataFrame | None = None) -> pd.DataFrame:
    """
    Flag rows where raw_price > OUTLIER_MULTIPLIER × route median.

    *historical_df* (optional): additional DB-loaded history to make the
    median more robust.  If provided, it is concatenated with *df* only
    for median computation; the returned DataFrame still contains only
    the rows from *df*.
    """
    if df.empty:
        return df

    df = df.copy()

    # Build a combined series for median computation
    if historical_df is not None and not historical_df.empty:
        combined = pd.concat([df[["route", "raw_price"]], historical_df[["route", "raw_price"]]])
    else:
        combined = df[["route", "raw_price"]]

    route_medians = (
        combined.groupby("route")["raw_price"]
                .median()
                .rename("_median")
    )

    df = df.join(route_medians, on="route")
    df["is_outlier"] = df["raw_price"] > (OUTLIER_MULTIPLIER * df["_median"])
    df.drop(columns=["_median"], inplace=True)

    n_outliers = df["is_outlier"].sum()
    if n_outliers:
        logger.warning(
            "Flagged %d outlier row(s) (price > %.0f× route median).",
            n_outliers, OUTLIER_MULTIPLIER,
        )

    return df


def split_fare(df: pd.DataFrame) -> pd.DataFrame:
    """
    Populate base_fare and taxes_fees where not already provided.

    If base_fare is already set (e.g. source provided a breakdown),
    derive taxes_fees = raw_price - base_fare.
    Otherwise, apply the TAXES_PCT_OF_TOTAL estimate.
    """
    if df.empty:
        return df

    df = df.copy()

    # Where base_fare is missing, estimate it
    missing_base = df["base_fare"].isna()
    df.loc[missing_base, "taxes_fees"] = (
        df.loc[missing_base, "raw_price"] * TAXES_PCT_OF_TOTAL
    )
    df.loc[missing_base, "base_fare"] = (
        df.loc[missing_base, "raw_price"] * (1 - TAXES_PCT_OF_TOTAL)
    )

    # Where base_fare is set but taxes_fees is missing
    known_base = ~df["base_fare"].isna() & df["taxes_fees"].isna()
    df.loc[known_base, "taxes_fees"] = (
        df.loc[known_base, "raw_price"] - df.loc[known_base, "base_fare"]
    )

    return df


def clean(
    fares: list[dict],
    historical_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Full cleaning pipeline:  coerce → deduplicate → flag outliers → split fare.

    Parameters
    ----------
    fares : list[dict]
        Raw fare dicts from fetch.py.
    historical_df : pd.DataFrame | None
        Optional DataFrame of previously stored fares (for robust medians).

    Returns
    -------
    pd.DataFrame
        Cleaned fare data.  Outlier rows are kept but flagged (is_outlier=True)
        so they can be excluded from index computation while still being stored.
    """
    df = fares_to_dataframe(fares)
    if df.empty:
        logger.warning("clean(): received empty fare list – nothing to clean.")
        return df

    n_raw = len(df)
    df = deduplicate(df)
    logger.info("Dedup: %d → %d rows.", n_raw, len(df))

    df = flag_outliers(df, historical_df=historical_df)
    df = split_fare(df)

    logger.info(
        "Cleaning done: %d rows total, %d non-outlier.",
        len(df), (~df["is_outlier"]).sum(),
    )
    return df
