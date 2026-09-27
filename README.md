# AeroIndex 🛫

A Python pipeline that scrapes live domestic flight fares, computes a weighted
price index, generates LLM-powered spike explanations, stores everything in
PostgreSQL, and exposes it via a REST API (and optionally an MCP server).

> **Data disclaimer**: Route weights are illustrative placeholders. Replace
> them with real DGCA traffic-share data before using in any official
> statistical publication. Scraped data is indicative only.

---

## Quick start

### 1. Prerequisites

| Tool | Notes |
|---|---|
| Python 3.10+ | `python --version` |
| Docker + PostgreSQL | A running Postgres container (see below) |
| Groq API key | Free at [console.groq.com](https://console.groq.com) |

**Start a local Postgres container (if you don't have one):**
```bash
docker run -d \
  --name aeroindex-pg \
  -e POSTGRES_PASSWORD=password \
  -e POSTGRES_DB=aeroindex \
  -p 5432:5432 \
  postgres:15
```

### 2. Install dependencies

```bash
cd aeroindex
pip install -r requirements.txt

# Scrapling needs Playwright browsers for StealthyFetcher:
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env:
#   GROQ_API_KEY=gsk_...          (from console.groq.com)
#   DATABASE_URL=postgresql://postgres:password@localhost:5432/aeroindex
```

### 4. Run the pipeline

```bash
python main.py
```

Expected output (first run – base prices bootstrapped):
```
─────────────────────────────────────────────────────────
  AeroIndex Pipeline — Summary
─────────────────────────────────────────────────────────
  Date          : 2024-11-01
  Index value   : 100.0000
  Day Δ         : N/A (first run – base prices bootstrapped)

  Per-route breakdown:
    DEL-BOM  ₹4530  (relative=1.000, weight=0.45)
    DEL-BLR  ₹3890  (relative=1.000, weight=0.35)
    BOM-BLR  ₹3210  (relative=1.000, weight=0.20)

  No spike detected (or first run).
─────────────────────────────────────────────────────────
```

On subsequent runs, `Day Δ` will reflect the actual day-over-day movement.

### 5. Start the API server

```bash
uvicorn api.main:app --reload
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) for the
interactive Swagger UI.

### 6. (Optional) Start the MCP server

```bash
python -m mcp_server.server
```

---

## API reference

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness + DB connectivity check |
| `GET /index/today` | Latest index value + pct_change |
| `GET /index/history?days=N` | Last N days for charting (default 30) |
| `GET /index/explain/{date}` | Spike explanation for YYYY-MM-DD |

### Example responses

**GET /index/today**
```json
{
  "date": "2024-11-02",
  "index_value": 108.3,
  "pct_change": 8.3,
  "computed_at": "2024-11-02T12:34:56Z"
}
```

**GET /index/explain/2024-11-02**
```json
{
  "date": "2024-11-02",
  "pct_change": 8.3,
  "matched_calendar_event": "Diwali / Deepavali – peak domestic travel",
  "explanation": "AeroIndex surged 8.3% on 2024-11-02, driven by Diwali demand on DEL-BOM (₹6,200 vs base ₹4,530) and DEL-BLR (₹5,100 vs base ₹3,890).",
  "generated_at": "2024-11-02T12:35:02Z"
}
```

---

## Project structure

```
aeroindex/
├── .env.example              # Environment variable template
├── requirements.txt
├── config.py                 # Route basket, weights, settings
├── main.py                   # Pipeline orchestrator
│
├── db/
│   ├── schema.sql            # Idempotent DDL
│   └── connection.py         # psycopg2 helpers
│
├── scraper/
│   ├── robots_check.py       # robots.txt compliance (urllib.robotparser)
│   └── fetch.py              # Scrapling-based fare scraper
│
├── pipeline/
│   ├── clean.py              # Pandas: dedup, outlier flag, fare split
│   ├── index.py              # Weighted index computation
│   ├── spike_explainer.py    # Groq LLM spike narration
│   └── store.py              # Insert fares to Postgres
│
├── data/
│   └── calendar.json         # Festival/holiday calendar (manually seeded)
│
├── api/
│   └── main.py               # FastAPI app
│
└── mcp_server/
    └── server.py             # Optional MCP server (read-only tools)
```

---

## Index formula

```
AeroIndex_t = Σ_i  w_i × (P_it / P_i0) × 100
```

| Symbol | Meaning |
|---|---|
| `w_i` | Route weight (illustrative; replace with DGCA traffic share) |
| `P_it` | Median non-outlier fare for route *i* today |
| `P_i0` | Base-period fare (set on first pipeline run, stored in DB) |

On first run, `P_i0 = P_i1` so the index starts at 100. Subsequent days
reflect percentage changes relative to that base.

---

## Route basket

| Route | Weight (illustrative) |
|---|---|
| DEL-BOM | 0.45 |
| DEL-BLR | 0.35 |
| BOM-BLR | 0.20 |

> ⚠ **Replace with real DGCA O&D data** from
> [dgca.gov.in/statistics](https://www.dgca.gov.in/digigov-portal/?page=statistics)
> before any official publication.

---

## Spike explanation logic

1. Compute `pct_change` vs previous day's index.
2. If `|pct_change| >= SPIKE_THRESHOLD` (default 5%):
   - Check `data/calendar.json` for festivals within ±3 days.
   - Call Groq (`llama3-8b-8192`) with structured prompt:
     - Actual index values, per-route fares, calendar event (or explicit "none").
     - Strict instructions: one sentence, no hallucination of events.
   - Store explanation in `spike_explanations` table.

---

## Compliance notes

- **robots.txt**: Every scrape URL is checked via `urllib.robotparser`
  before fetching. Disallowed domains are logged and skipped.
- **Rate limiting**: 2-second delay between consecutive requests to the
  same domain (configurable via `RATE_LIMIT_DELAY` in `.env`).
- **Groq**: Used ONLY for narrating pre-computed structured data.
  It is never asked to look up, estimate, or find fare prices.
- **No paid services** assumed beyond Groq free tier.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `DATABASE_URL is not set` | Missing .env | Copy `.env.example` → `.env` and fill in |
| `No prices extracted` | Page structure changed | Update CSS/XPath selectors in `scraper/fetch.py` |
| `GROQ_API_KEY is not set` | Missing key | Get free key at console.groq.com, add to .env |
| `All sources excluded by robots.txt` | Site disallows crawling | Request official data partnership |
| `Index computation returned None` | All fares are outliers | Check `OUTLIER_MULTIPLIER` in `pipeline/clean.py` |
# AeroIndex
