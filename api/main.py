"""
api/main.py – FastAPI REST layer for AeroIndex.

Endpoints
---------
GET /index/today
    Returns the latest computed index value and pct_change.

GET /index/history?days=N
    Returns the last N days of index values for charting.

GET /index/explain/{date}
    Returns the stored spike explanation for a specific date
    (YYYY-MM-DD format).

GET /health
    Simple liveness check.

All endpoints read from Postgres; no writes.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

from db.connection import get_conn, run_schema

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ── Lifespan: run schema on startup ──────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        run_schema()
        logger.info("Database schema verified on startup.")
    except Exception as exc:
        logger.error("Failed to connect to database on startup: %s", exc)
        # Don't crash – endpoints will return 503 if DB is unavailable
    yield


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="AeroIndex API",
    description=(
        "Domestic flight fare price index for India. "
        "Data is scraped from public sources and NOT suitable for "
        "official statistical publication without DGCA data partnership."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Response models ───────────────────────────────────────────────────────────
class IndexToday(BaseModel):
    date: date
    index_value: float
    pct_change: Optional[float]
    computed_at: datetime


class IndexPoint(BaseModel):
    date: date
    index_value: float
    pct_change: Optional[float]


class IndexHistory(BaseModel):
    days_requested: int
    points: list[IndexPoint]


class SpikeExplanation(BaseModel):
    date: date
    pct_change: float
    matched_calendar_event: Optional[str]
    explanation: str
    generated_at: datetime


class HealthResponse(BaseModel):
    status: str
    database: str


# ── Helper ────────────────────────────────────────────────────────────────────
def _db_error(exc: Exception) -> HTTPException:
    logger.error("Database error: %s", exc)
    return HTTPException(
        status_code=503,
        detail=(
            "Database unavailable. "
            "Ensure DATABASE_URL is set and Postgres is running."
        ),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health", response_model=HealthResponse, tags=["System"])
def health():
    """Liveness check – also tests DB connectivity."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
        db_status = "connected"
    except Exception:
        db_status = "unavailable"

    return HealthResponse(
        status="ok",
        database=db_status,
    )


@app.get("/index/today", response_model=IndexToday, tags=["Index"])
def get_index_today():
    """
    Return the most recently computed AeroIndex value and its
    day-over-day percentage change.
    """
    try:
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
    except Exception as exc:
        raise _db_error(exc)

    if not row:
        raise HTTPException(
            status_code=404,
            detail=(
                "No index data found. "
                "Run `python main.py` to execute the pipeline first."
            ),
        )

    return IndexToday(
        date=row[0],
        index_value=float(row[1]),
        pct_change=float(row[2]) if row[2] is not None else None,
        computed_at=row[3],
    )


@app.get("/index/history", response_model=IndexHistory, tags=["Index"])
def get_index_history(
    days: int = Query(default=30, ge=1, le=365, description="Number of days of history"),
):
    """
    Return a time series of AeroIndex values for the last *days* calendar days.
    Suitable for charting on a dashboard.
    """
    try:
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
    except Exception as exc:
        raise _db_error(exc)

    points = [
        IndexPoint(
            date=r[0],
            index_value=float(r[1]),
            pct_change=float(r[2]) if r[2] is not None else None,
        )
        for r in rows
    ]

    return IndexHistory(days_requested=days, points=points)


@app.get("/index/explain/{index_date}", response_model=SpikeExplanation, tags=["Index"])
def get_explanation(index_date: date):
    """
    Return the stored spike explanation for *index_date* (YYYY-MM-DD).
    Returns 404 if no spike was detected or explanation was not generated
    for that date.
    """
    try:
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
                    (index_date,),
                )
                row = cur.fetchone()
    except Exception as exc:
        raise _db_error(exc)

    if not row:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No spike explanation found for {index_date}. "
                "Either no spike occurred on that date, or the pipeline "
                "has not yet been run."
            ),
        )

    return SpikeExplanation(
        date=row[0],
        pct_change=float(row[1]),
        matched_calendar_event=row[2],
        explanation=row[3],
        generated_at=row[4],
    )
