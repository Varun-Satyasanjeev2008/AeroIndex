"""
db/connection.py – Postgres connection helper for AeroIndex.

Usage
-----
    from db.connection import get_conn, run_schema

    # One-time schema bootstrap (idempotent):
    run_schema()

    # Use a connection:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DATABASE_URL: str | None = os.getenv("DATABASE_URL")

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _get_database_url() -> str:
    """Return DATABASE_URL or raise a clear error if unset."""
    url = _DATABASE_URL or os.getenv("DATABASE_URL")
    if not url:
        raise EnvironmentError(
            "DATABASE_URL is not set. "
            "Copy .env.example → .env and fill in your Postgres connection string."
        )
    return url


@contextmanager
def get_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Context manager that yields an open psycopg2 connection.
    Commits on clean exit; rolls back on exception.
    """
    conn = psycopg2.connect(_get_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_schema() -> None:
    """
    Execute schema.sql against the configured database.
    All statements use CREATE … IF NOT EXISTS so this is safe to run
    on every application startup.
    """
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    logger.info("Schema applied (or already up-to-date).")


def upsert_route_weights(weights: dict[str, dict]) -> None:
    """
    Sync ROUTE_BASKET weights into the route_weights table.
    Skips routes that already have a base_price set in the DB.
    """
    sql = """
        INSERT INTO route_weights (route, weight, base_price, updated_at)
        VALUES (%(route)s, %(weight)s, %(base_price)s, NOW())
        ON CONFLICT (route) DO UPDATE
            SET weight     = EXCLUDED.weight,
                updated_at = EXCLUDED.updated_at
        -- Do NOT overwrite an existing base_price – preserve it
        WHERE route_weights.base_price IS NULL
              OR EXCLUDED.base_price IS NOT NULL;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            for route, cfg in weights.items():
                cur.execute(
                    sql,
                    {
                        "route": route,
                        "weight": cfg["weight"],
                        "base_price": cfg.get("base_price"),
                    },
                )
    logger.info("Route weights upserted: %s", list(weights.keys()))
