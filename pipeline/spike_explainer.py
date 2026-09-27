"""
pipeline/spike_explainer.py – Groq LLM spike explanation generator.

Workflow
--------
1. Determine if today's pct_change exceeds SPIKE_THRESHOLD.
2. If yes, look up data/calendar.json for any festival/event within
   CALENDAR_LOOKAHEAD_DAYS of today's date.
3. Build a structured prompt with the index delta, affected routes,
   and calendar context.
4. Call Groq's chat completions endpoint via the official `groq` SDK.
5. Persist the explanation to the spike_explanations table.

IMPORTANT: Groq is used ONLY to narrate pre-computed structured data.
We NEVER ask Groq to find, look up, or estimate any price.
The prompt explicitly forbids Groq from inventing a calendar event
when none is provided.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import (
    CALENDAR_LOOKAHEAD_DAYS,
    GROQ_MAX_TOKENS,
    GROQ_MODEL,
    SPIKE_THRESHOLD,
)
from pipeline.index import IndexResult

logger = logging.getLogger(__name__)

_CALENDAR_PATH = Path(__file__).parent.parent / "data" / "calendar.json"


def _load_calendar() -> list[dict]:
    """Load data/calendar.json. Returns [] on missing/corrupt file."""
    try:
        return json.loads(_CALENDAR_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("calendar.json not found at %s.", _CALENDAR_PATH)
        return []
    except json.JSONDecodeError as exc:
        logger.error("calendar.json is malformed: %s", exc)
        return []


def _find_calendar_event(target_date: date) -> str | None:
    """
    Search calendar for any event within ±CALENDAR_LOOKAHEAD_DAYS of
    *target_date*.  Returns the event name/description, or None.
    """
    calendar = _load_calendar()
    window_start = target_date - timedelta(days=CALENDAR_LOOKAHEAD_DAYS)
    window_end = target_date + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)

    for entry in calendar:
        try:
            event_date = date.fromisoformat(entry["date"])
        except (KeyError, ValueError):
            continue
        if window_start <= event_date <= window_end:
            return entry.get("event", "Unknown event")

    return None


def _build_prompt(
    index_result: IndexResult,
    calendar_event: str | None,
) -> str:
    """
    Build a tightly-scoped prompt that gives Groq ONLY structured,
    pre-computed data and asks for a single-sentence explanation.
    """
    routes_info = "\n".join(
        f"  - {route}: ₹{info['price_today']:.0f} (base ₹{info['base_price']:.0f}, "
        f"relative {info['price_relative']:.3f})"
        for route, info in index_result.per_route.items()
    )

    if calendar_event:
        calendar_context = (
            f"A known calendar event was found near this date: **{calendar_event}**. "
            f"You MAY reference this event as a contributing factor."
        )
    else:
        calendar_context = (
            "No known festival, holiday, or long-weekend event was found in the "
            "calendar within a {days}-day window of this date. "
            "DO NOT invent or assume any event. "
            "Label this as an unexplained demand surge."
        ).format(days=CALENDAR_LOOKAHEAD_DAYS)

    prompt = f"""You are an aviation economics analyst writing a one-sentence explanation
for a flight-fare index spike in the AeroIndex system.

The following data has been computed from LIVE scraped fare data (not hypothetical):

Date: {index_result.date}
AeroIndex value: {index_result.index_value:.2f}
Day-over-day change: {index_result.pct_change:+.2f}%

Per-route fare details:
{routes_info}

Calendar context: {calendar_context}

STRICT RULES:
1. Write exactly ONE sentence (max 60 words).
2. Reference the index change percentage and at least one specific route.
3. Do NOT speculate about prices, data sources, or events not provided above.
4. Do NOT add any preamble, headings, or caveats.
5. If no calendar event was found, explicitly state "unexplained demand surge".

Write your one-sentence explanation now:"""

    return prompt


def _call_groq(prompt: str) -> str:
    """
    Call the Groq chat completions API and return the assistant's response text.
    Raises EnvironmentError if GROQ_API_KEY is missing.
    Raises RuntimeError on API error.
    """
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "\n"
            "─────────────────────────────────────────────────────────\n"
            "GROQ_API_KEY is not set in your .env file.\n"
            "Action required:\n"
            "  1. Sign up for a free key at https://console.groq.com\n"
            "  2. Add  GROQ_API_KEY=gsk_...  to your .env file.\n"
            "─────────────────────────────────────────────────────────"
        )

    try:
        from groq import Groq
    except ImportError:
        raise RuntimeError(
            "The 'groq' package is not installed. Run: pip install groq"
        )

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=GROQ_MAX_TOKENS,
        temperature=0.3,   # low temperature for factual narration
    )
    return response.choices[0].message.content.strip()


def maybe_explain_spike(
    index_result: IndexResult,
    conn,
) -> str | None:
    """
    If today's pct_change exceeds SPIKE_THRESHOLD, generate and persist
    a Groq-powered explanation.

    Returns the explanation string, or None if no spike / no explanation needed.
    """
    if index_result.pct_change is None:
        logger.info("pct_change is None (first run) – skipping spike check.")
        return None

    if abs(index_result.pct_change) < SPIKE_THRESHOLD:
        logger.info(
            "pct_change=%.2f%% is below SPIKE_THRESHOLD=%.1f%% – no explanation generated.",
            index_result.pct_change,
            SPIKE_THRESHOLD,
        )
        return None

    logger.info(
        "⚠  Spike detected: pct_change=%.2f%% on %s. Generating explanation …",
        index_result.pct_change,
        index_result.date,
    )

    calendar_event = _find_calendar_event(index_result.date)
    if calendar_event:
        logger.info("Calendar match found: %s", calendar_event)
    else:
        logger.info("No calendar event found near %s.", index_result.date)

    prompt = _build_prompt(index_result, calendar_event)

    try:
        explanation = _call_groq(prompt)
    except EnvironmentError:
        # Missing API key – re-raise so main.py can handle it explicitly
        raise
    except Exception as exc:
        logger.error("Groq API call failed: %s", exc)
        explanation = (
            f"[Groq API error – explanation unavailable: {exc}]"
        )

    logger.info("Explanation: %s", explanation)

    # Persist to DB
    _write_explanation(
        conn=conn,
        result=index_result,
        calendar_event=calendar_event,
        explanation=explanation,
    )

    return explanation


def _write_explanation(
    conn,
    result: IndexResult,
    calendar_event: str | None,
    explanation: str,
) -> None:
    """Insert spike explanation into spike_explanations table."""
    generated_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO spike_explanations
                (date, pct_change, matched_calendar_event, explanation, generated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
            """,
            (
                result.date,
                result.pct_change,
                calendar_event,
                explanation,
                generated_at,
            ),
        )
    logger.info("Spike explanation written for %s.", result.date)
