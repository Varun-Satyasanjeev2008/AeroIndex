"""
mcp_server/server.py – Minimal read-only MCP server for AeroIndex.

Exposes three tools that call the same Postgres queries as the FastAPI endpoints:
    - get_current_index()      → latest index value + pct_change
    - get_index_history(days)  → historical series (last N days)
    - explain_spike(date)      → stored spike explanation for a date

NO write or delete tools are exposed.

Running
-------
    python -m mcp_server.server

Or, if using the MCP CLI:
    mcp run mcp_server/server.py

Requirements
------------
    pip install mcp   (included in requirements.txt)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from mcp.server.fastmcp import FastMCP
from db.connection import get_conn, run_schema

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Initialise schema on server startup (idempotent)
try:
    run_schema()
    logger.info("MCP server: DB schema verified.")
except Exception as exc:
    logger.error("MCP server: DB schema check failed: %s", exc)

mcp = FastMCP(
    name="AeroIndex",
    instructions=(
        "AeroIndex is a domestic flight fare price index for India. "
        "All tools are READ-ONLY and return data from the AeroIndex Postgres database. "
        "Do NOT use these tools to infer or estimate fare prices – they only return "
        "pre-computed index values from scraped, structured data."
    ),
)


@mcp.tool()
def get_current_index() -> dict[str, Any]:
    """
    Return the most recently computed AeroIndex value.

    Returns
    -------
    dict with keys:
        date         (str, YYYY-MM-DD)
        index_value  (float)
        pct_change   (float | None)
        computed_at  (str, ISO-8601)
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, index_value, pct_change, computed_at
                FROM daily_index
                ORDER BY date DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()

    if not row:
        return {"error": "No index data available. Run the pipeline first (python main.py)."}

    return {
        "date": str(row[0]),
        "index_value": float(row[1]),
        "pct_change": float(row[2]) if row[2] is not None else None,
        "computed_at": row[3].isoformat(),
    }


@mcp.tool()
def get_index_history(days: int = 30) -> dict[str, Any]:
    """
    Return a time series of AeroIndex values for charting.

    Parameters
    ----------
    days : int
        Number of calendar days of history to return (1–365, default 30).

    Returns
    -------
    dict with keys:
        days_requested (int)
        points         (list of {date, index_value, pct_change})
    """
    days = max(1, min(365, int(days)))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, index_value, pct_change
                FROM daily_index
                WHERE date >= CURRENT_DATE - (%s || ' days')::INTERVAL
                ORDER BY date ASC;
                """,
                (str(days),),
            )
            rows = cur.fetchall()

    return {
        "days_requested": days,
        "points": [
            {
                "date": str(r[0]),
                "index_value": float(r[1]),
                "pct_change": float(r[2]) if r[2] is not None else None,
            }
            for r in rows
        ],
    }


@mcp.tool()
def explain_spike(index_date: str) -> dict[str, Any]:
    """
    Return the stored spike explanation for a given date.

    Parameters
    ----------
    index_date : str
        Date in YYYY-MM-DD format.

    Returns
    -------
    dict with keys:
        date                  (str)
        pct_change            (float)
        matched_calendar_event (str | None)
        explanation           (str)
        generated_at          (str, ISO-8601)

    Returns {"error": "..."} if no explanation exists for that date.
    """
    try:
        parsed_date = date.fromisoformat(index_date)
    except ValueError:
        return {"error": f"Invalid date format: '{index_date}'. Use YYYY-MM-DD."}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, pct_change, matched_calendar_event,
                       explanation, generated_at
                FROM spike_explanations
                WHERE date = %s
                ORDER BY generated_at DESC
                LIMIT 1;
                """,
                (parsed_date,),
            )
            row = cur.fetchone()

    if not row:
        return {
            "error": (
                f"No spike explanation for {index_date}. "
                "Either no spike occurred or the pipeline has not run yet."
            )
        }

    return {
        "date": str(row[0]),
        "pct_change": float(row[1]),
        "matched_calendar_event": row[2],
        "explanation": row[3],
        "generated_at": row[4].isoformat(),
    }


if __name__ == "__main__":
    mcp.run()
