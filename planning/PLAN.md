# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/                   # Schema definitions, seed data, migration logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: API key for real market data, used by the `massive` Python SDK
# which wraps Polygon.io. If not set, the built-in market simulator is used.
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: How many prior chat_messages rows (user + assistant combined) to include
# in the LLM prompt — counts rows, not turns. Default 20 = roughly 10 turns.
CHAT_HISTORY_LIMIT=20

# Optional: Timeout (seconds) for a single LLM call before the backend gives up (default 30)
LLM_TIMEOUT_SECONDS=30
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for the union of all currently watched tickers on a configurable interval
- The poll list is refreshed eagerly: `add_ticker`/`remove_ticker` updates the in-memory ticker set immediately, and the next poll cycle picks it up
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The server emits an event **only when the cache version changes** (i.e. at least one ticker has a new price). There is no fixed-cadence heartbeat. With the simulator this means roughly 2 events/sec; with Massive on the free tier this means roughly one event every 15 seconds.
- Each SSE event is a JSON `data:` line containing a **full snapshot of the price cache**, keyed by ticker. The frontend derives deltas by comparing against its own last-seen prices:

  ```json
  {
    "AAPL": {
      "ticker": "AAPL",
      "price": 190.45,
      "previous_price": 190.32,
      "timestamp": 1748269938.412,
      "change": 0.13,
      "change_percent": 0.068,
      "direction": "up"
    },
    "GOOGL": { "...": "..." }
  }
  ```

  `direction` is `"up"`, `"down"`, or `"flat"`. **Wire-format exception:** `timestamp` in this stream is a Unix epoch float (seconds with millisecond precision), *not* the ISO 8601 string used everywhere else in the system. This stream fires multiple times per second; ISO formatting on every emit is wasted work since the frontend re-parses to a number for charting. All persisted timestamps and every other API timestamp remain UTC ISO 8601 with `Z` suffix.
- **Initial snapshot on connect:** immediately after a client connects (or reconnects after EventSource's automatic retry), the server emits a single full price-cache snapshot in the same payload shape, before any change-driven events. This guarantees the frontend has a starting baseline without waiting for the next market tick (which could be up to ~15 seconds away on Massive's free tier). If the price cache is still empty at connect time (the data source hasn't produced its first tick), the server holds the connection open and emits the first real tick as the initial event rather than sending an empty `{}`. Clients can treat the very first event after connect as a snapshot regardless of source; subsequent events follow the cache-version-change rule above.
- Client handles reconnection automatically (EventSource has built-in retry). On every reconnect the server re-sends the initial snapshot as described above, so no client-side resync logic is needed.

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**Conventions used throughout the schema:**

- All timestamps are stored as **UTC ISO 8601 with a `Z` suffix** (e.g. `2026-05-26T14:32:18.412Z`). This is the wire format too — APIs return the same strings. The only exception is the price-SSE `timestamp` field (§6), which is a Unix epoch float because that stream fires multiple times per second.
- `quantity` is `REAL` but constrained to **4 decimal places** end-to-end: input validation rejects more than 4 dp, positions are stored at 4 dp, and any residual below `0.0001` after a sell auto-closes the position (the row is deleted).
- All monetary values (`cash_balance`, `price`, `avg_cost`, `total_value`) are `REAL`. The frontend rounds to 2 dp for display; the backend stores full precision so P&L math doesn't drift.
- **Concurrency:** the backend runs as a **single-worker uvicorn process**. Trade execution and portfolio mutations are serialized through an in-process lock — there is no multi-worker support. SQLite is opened with `check_same_thread=False` and accessed only from the FastAPI event loop.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task (`backend/app/portfolio/snapshotter.py`), and immediately after each trade execution. The snapshotter runs for the lifetime of the FastAPI app — it starts on app startup and stops on shutdown; it does not respect market hours (the simulator runs 24/7).
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (UTC ISO 8601, `Z` suffix)
- INDEX on `(user_id, recorded_at)` — history queries are time-range scans

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — see below; `null` for user messages)
- `created_at` TEXT (UTC ISO 8601, `Z` suffix)

**Row-per-turn contract:** a successful `POST /api/chat` call writes **exactly two rows** — first a `role = "user"` row with `content` set to the user's message and `actions = NULL`, then a `role = "assistant"` row with `content` set to the LLM's `message` text and `actions` set to the JSON array of executed actions (`[]` if there were none). Both rows share the same `user_id` and are written in a single transaction; the assistant row's `created_at` is strictly greater than the user row's. The `message_id` returned by `POST /api/chat` (§8.4) is the assistant row's `id`. On LLM timeout neither row is written. This row pattern is what `CHAT_HISTORY_LIMIT` counts: a default of 20 = roughly 10 user/assistant turns.

The `actions` column stores the executed result of any trades or watchlist changes the assistant attempted, as a JSON array. The frontend renders past assistant messages from this column with the same component used for fresh responses:

```json
[
  {
    "type": "trade",
    "ticker": "AAPL",
    "side": "buy",
    "quantity": 10,
    "price": 190.45,
    "status": "ok"
  },
  {
    "type": "watchlist",
    "ticker": "PYPL",
    "action": "add",
    "status": "error",
    "error_code": "UNKNOWN_TICKER"
  }
]
```

`status` is `"ok"` or `"error"`. `error_code` is present only when `status == "error"` and contains one of the documented error codes from §8. The field name `error_code` matches the REST error envelope used everywhere else in the API.

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX
- One initial `portfolio_snapshots` row with `total_value=10000.0` and `recorded_at` = init time, so the P&L chart has an origin point on first load

**Resetting the demo:** there is no admin/reset endpoint in v1. To start over, stop the container and delete the Docker volume (or remove `db/finally.db`). The next startup re-runs lazy init and reseeds the cash balance, watchlist, and origin snapshot.

**Supported ticker universe (simulator mode):** The simulator only knows the 10 seeded tickers above (defined in `backend/app/market/seed_prices.py`). Adding any other ticker to the watchlist in simulator mode is rejected with `400 UNKNOWN_TICKER`. In Massive mode, the supported universe is whatever Polygon.io recognises, validated at add time.

---

## 8. API Endpoints

All responses are JSON. All timestamps are UTC ISO 8601 with a `Z` suffix.

### 8.1 Market Data — SSE

**`GET /api/stream/prices`** — long-lived SSE stream of price updates. See §6 for the event payload shape. Events are emitted only when the cache version changes; there is no heartbeat.

### 8.2 Portfolio

**`GET /api/portfolio`** — current cash, positions, and total value.

Response `200`:
```json
{
  "cash": 8023.55,
  "total_value": 10142.18,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 10.0,
      "avg_cost": 190.13,
      "current_price": 191.86,
      "market_value": 1918.60,
      "unrealized_pnl": 17.30,
      "pnl_pct": 0.91
    }
  ]
}
```

`current_price`, `market_value`, `unrealized_pnl`, and `pnl_pct` may be `null` if the price cache hasn't ticked yet for that ticker. Frontend renders `—` for null values and skips them from `total_value` (which then reflects cash + valued positions only).

---

**`POST /api/portfolio/trade`** — execute a market order.

Request:
```json
{ "ticker": "AAPL", "side": "buy", "quantity": 10.0 }
```

`quantity` must be > 0 with at most 4 decimal places. `side` is `"buy"` or `"sell"`.

Response `200`:
```json
{
  "trade_id": "0d3c…",
  "ticker": "AAPL",
  "side": "buy",
  "quantity": 10.0,
  "executed_price": 190.45,
  "executed_at": "2026-05-26T14:32:18.412Z",
  "cash_after": 8095.50,
  "position_after": { "ticker": "AAPL", "quantity": 20.0, "avg_cost": 190.29 }
}
```

If the trade closes the position to zero, the row is deleted from `positions` and `position_after` is `null` in the response. A new `portfolio_snapshots` row is written before the response is returned.

Response `400`:
```json
{ "error_code": "INSUFFICIENT_CASH", "message": "Need $1904.50, have $1200.00" }
```

Documented error codes:

| Code | Meaning |
|---|---|
| `INSUFFICIENT_CASH` | Buy would push cash below zero |
| `INSUFFICIENT_SHARES` | Sell exceeds current quantity held |
| `UNKNOWN_TICKER` | Not in the simulator universe (simulator mode) or not recognised by Polygon (Massive mode) |
| `NO_PRICE_YET` | Cache has no price for this ticker yet — retry shortly |
| `INVALID_QUANTITY` | `quantity <= 0`, more than 4 dp, or non-numeric |

---

**`GET /api/portfolio/history?since=<iso>&until=<iso>`** — portfolio value snapshots for the P&L chart.

If `since` is omitted, the endpoint returns the last 24 hours of snapshots. `since` and `until` both accept any UTC ISO 8601 timestamp; values older than the earliest snapshot or newer than the latest are clamped silently. Max returned: 5000 rows; if the range contains more, the response returns the **most recent 5000** within `[since, until]` and sets `"truncated": true` in the response so the frontend knows to widen its bucketing rather than assume gaps. Server-side downsampling is out of scope for v1 — the client paginates by adjusting `until` if it needs the older portion.

Response `200`:
```json
{
  "snapshots": [
    { "total_value": 10000.00, "recorded_at": "2026-05-25T14:32:00.000Z" },
    { "total_value": 10042.18, "recorded_at": "2026-05-25T14:32:30.000Z" }
  ],
  "truncated": false
}
```

---

**`GET /api/stream/portfolio`** — long-lived SSE stream of portfolio changes, for multi-tab consistency.

Events are emitted in three cases: (1) **immediately on connect** (and on every reconnect), with `reason: "initial"`, so a freshly loaded or reloaded tab has authoritative cash/positions without polling `GET /api/portfolio` separately; (2) immediately after a trade executes (from any tab or from the AI), with `reason: "trade"`; and (3) every **5 minutes** as a reconciliation tick with `reason: "snapshot"`. The reconciliation cadence is deliberately slower than the 30s `portfolio_snapshots` write cadence — tabs already recompute `total_value` locally from price-SSE ticks, so the stream only needs to resync on trades plus an occasional drift correction. The event body is the same shape as `GET /api/portfolio`:

```json
{
  "cash": 8023.55,
  "total_value": 10142.18,
  "positions": [ ... ],
  "reason": "initial" | "trade" | "snapshot"
}
```

The `initial` event always fires (even if cash and positions are unchanged from seed) and is sent before any `trade` or `snapshot` event on the same connection. EventSource's automatic retry triggers a fresh `initial` event after each reconnect, so clients can treat the stream as self-healing and never need a separate REST fetch to recover state. Between events, tabs may recompute `total_value` locally from price-SSE ticks for smooth live updates; the next portfolio event resyncs to the authoritative value.

### 8.3 Watchlist

**`GET /api/watchlist`** — list watched tickers (no prices; the frontend gets prices from `/api/stream/prices`).

Response `200`:
```json
{
  "tickers": [
    { "ticker": "AAPL", "added_at": "2026-05-25T14:32:00.000Z" },
    { "ticker": "GOOGL", "added_at": "2026-05-25T14:32:00.000Z" }
  ]
}
```

---

**`POST /api/watchlist`** — add a ticker.

Request: `{ "ticker": "AAPL" }`

Response `201`: `{ "ticker": "AAPL", "added_at": "..." }`.
Response `400`: `{ "error_code": "UNKNOWN_TICKER" | "ALREADY_WATCHED", "message": "..." }`.

---

**`DELETE /api/watchlist/{ticker}`** — remove a ticker.

Removing a ticker for which the user still holds shares is **allowed**: the row is deleted from `watchlist`, the position remains in `positions`, and the backend keeps the ticker subscribed in the data source so the position can be valued and later sold. The watchlist panel no longer shows it; the positions table still does.

Response `204` on success. `404` if the ticker isn't in the watchlist.

### 8.4 Chat

**`POST /api/chat`** — send a user message; receive the assistant's complete response with any executed actions.

Request:
```json
{ "message": "Buy 5 shares of Apple" }
```

Response `200`:
```json
{
  "message": "Bought 5 shares of AAPL at $190.45.",
  "actions": [
    { "type": "trade", "ticker": "AAPL", "side": "buy", "quantity": 5, "price": 190.45, "status": "ok" }
  ],
  "message_id": "9f2a…",
  "created_at": "2026-05-26T14:32:19.001Z"
}
```

`actions` uses the same schema documented in §7's `chat_messages.actions`. `message_id` is the `id` of the assistant row written for this turn (the matching user row is persisted alongside it — see §7's row-per-turn contract). On LLM timeout (see `LLM_TIMEOUT_SECONDS`), the backend returns `504 { "error_code": "LLM_TIMEOUT", "message": "..." }` and persists neither row for that turn.

---

**`GET /api/chat/history?limit=<n>&before=<iso>`** — load past chat messages so the frontend can rehydrate the conversation panel after a reload.

Returns rows from `chat_messages` (both `user` and `assistant` per §7's row-per-turn contract) ordered by `created_at` **ascending** within the returned window — i.e. oldest first, newest last — so the frontend can append them directly to the bottom of the scroller and then append new live turns after them.

Query parameters:
- `limit` (optional, default `50`, max `200`) — maximum number of rows to return.
- `before` (optional) — UTC ISO 8601 timestamp; when set, returns the most recent `limit` rows whose `created_at < before`. When omitted, returns the most recent `limit` rows overall. Used for upward pagination as the user scrolls into older history.

Response `200`:
```json
{
  "messages": [
    {
      "id": "7a1c…",
      "role": "user",
      "content": "Buy 5 shares of Apple",
      "actions": null,
      "created_at": "2026-05-26T14:32:18.402Z"
    },
    {
      "id": "9f2a…",
      "role": "assistant",
      "content": "Bought 5 shares of AAPL at $190.45.",
      "actions": [
        { "type": "trade", "ticker": "AAPL", "side": "buy", "quantity": 5, "price": 190.45, "status": "ok" }
      ],
      "created_at": "2026-05-26T14:32:19.001Z"
    }
  ],
  "has_more": false
}
```

`actions` is `null` for `user` rows and a JSON array (possibly empty) for `assistant` rows, matching §7's schema. `has_more` is `true` when at least one older row exists before the earliest row returned — the frontend paginates by calling again with `before` set to the earliest returned `created_at`. The endpoint never returns partial turns: if a `limit` would split a user/assistant pair, the cut happens between turns (i.e. the response always begins with a `user` row, except when the entire history is shorter than `limit`).

This endpoint serves the UI only; the LLM prompt-context window is still built from `CHAT_HISTORY_LIMIT` rows internally (§9 step 2) and is independent of how many rows the UI has fetched.

### 8.5 System

**`GET /api/health`** — health check used by the Docker `HEALTHCHECK` and by deployment platforms.

Response `200`:
```json
{ "status": "ok", "market_data_source": "simulator" | "massive", "uptime_seconds": 4821 }
```

Returns `503` if the price cache has no data yet (i.e. the data source background task has not produced a first tick).

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads recent conversation history from the `chat_messages` table — the last `CHAT_HISTORY_LIMIT` rows (user and assistant combined, ordered by `created_at` ascending; default 20, env-configurable)
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill, with a hard timeout of `LLM_TIMEOUT_SECONDS` (default 30)
5. Parses the complete structured JSON response
6. Auto-executes any watchlist changes first, then any trades (see "Auto-Execution" below)
7. Persists the turn to `chat_messages` as two rows in a single transaction — a `role = "user"` row with `actions = NULL`, then a `role = "assistant"` row whose `actions` column holds the executed-action JSON array (`[]` if none) per §7's row-per-turn contract
8. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

On LLM timeout, the backend returns `504 LLM_TIMEOUT` and persists nothing for that turn.

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications

### Auto-Execution

Trades and watchlist changes specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

**Execution order:** watchlist changes run first, then trades. Trades do **not** require the ticker to be in the watchlist — `POST /api/portfolio/trade` validates against the supported ticker universe directly. The "watchlist first" ordering exists only so the user visually sees a new ticker before the buy confirmation appears for it; it is not a correctness requirement.

Each action runs through the same validation as its REST equivalent. Failures do not abort the rest of the response — each action records its own `status` and `error_code`, the full array is persisted to `chat_messages.actions`, and the frontend shows per-action success/failure inline. The LLM sees these statuses on the next turn via conversation history and can react.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

**Canonical mock behavior** — the mock matches the user message against a small set of regex patterns (case-insensitive) and returns a structured response:

| Pattern | Response |
|---|---|
| `^buy\s+(\d+(?:\.\d+)?)\s+(?:shares?\s+of\s+)?([A-Z]+)` | `message`: "Mock: buying {qty} {ticker}.", `trades`: `[{ticker, side: "buy", quantity}]` |
| `^sell\s+(\d+(?:\.\d+)?)\s+(?:shares?\s+of\s+)?([A-Z]+)` | `message`: "Mock: selling {qty} {ticker}.", `trades`: `[{ticker, side: "sell", quantity}]` |
| `^(?:add|watch)\s+([A-Z]+)` | `message`: "Mock: watching {ticker}.", `watchlist_changes`: `[{ticker, action: "add"}]` |
| `^(?:remove|unwatch)\s+([A-Z]+)` | `message`: "Mock: unwatching {ticker}.", `watchlist_changes`: `[{ticker, action: "remove"}]` |
| anything else | `message`: "Mock mode: no actions.", empty arrays |

Mock responses still pass through the same auto-execution and validation path as live LLM responses, so E2E tests exercise the full chat → action pipeline.

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change %, and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Canvas-based charting library preferred (Lightweight Charts) for performance
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves the static frontend files and all API routes on port 8000.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The `db/` directory in the project root maps to `/app/db` in the container. The backend writes `finally.db` to this path.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection

---

## 13. Doc Review Changelog

A second doc review on 2026-05-26 resolved four critical spec/code gaps that surfaced when the spec was compared against the shipped market data layer:

- **§6 price-SSE wire format** now matches the shipped `stream.py`: a flat dict-of-ticker (full cache snapshot per emit), no envelope, no `version` field. The frontend derives deltas locally.
- **§6 price-SSE `timestamp`** is explicitly a Unix epoch float on the wire (the only exception to the project-wide ISO-8601-with-`Z` rule), since this stream fires multiple times per second and the frontend re-parses to a number anyway.
- **§8.2 trade response** now includes top-level `ticker`, `side`, and `quantity`, making it symmetric with the `chat_messages.actions` schema.
- **§8.2 closed positions** return `position_after: null` (not a zero-quantity stub), so the frontend can't accidentally render a stale `avg_cost`.

The same review pass also tightened the following important-but-not-blocking contracts:

- **§7 concurrency** is documented: single-worker uvicorn, in-process lock around trade execution. Multi-worker is out of scope for v1.
- **§7 fractional-share residuals** under `0.0001` auto-close the position row.
- **§7 reset behavior** is documented: no admin endpoint; delete the volume to start over.
- **§7 chat actions** rename `error` → `error_code` to match the REST envelope.
- **§8.2 `/api/portfolio/history`** gains an `until` parameter and a `truncated` flag for ranges past 5000 rows. Server-side downsampling is out of scope for v1.
- **§8.2 `/api/stream/portfolio`** reconciliation cadence drops from 30s to 5min — tabs already recompute locally between trade events.
- **§9 `LLM_MOCK`** specifies a canonical regex-driven mock so E2E tests have a stable contract.
- **§9 `CHAT_HISTORY_LIMIT`** is clarified to count `chat_messages` rows (user + assistant combined), not turns.

An earlier doc review on 2026-05-26 resolved a batch of contract gaps and behavioural questions that surfaced once the Market Data subsystem shipped. The resolutions are now incorporated into §5–§9. For the record:

- **Stance on multi-user:** the schema is shaped for future multi-user (every table has `user_id`); the rest of the app is hardcoded to `user_id = "default"`. The `users_profile` table stays as-is.
- **Ticker universe in simulator mode:** locked to the 10 seeded tickers. Adds outside that set return `400 UNKNOWN_TICKER`. Massive mode validates against the provider at add time.
- **Watchlist removal with open position:** allowed. Position remains, price keeps streaming, the ticker just disappears from the watchlist panel.
- **Auto-execution order from chat:** watchlist changes first, then trades. Buys do not require watchlist membership; ordering is for UX, not correctness.
- **SSE for prices** emits on cache-version change, not on a fixed cadence — matches the shipped `stream.py`.
- **New endpoint `GET /api/stream/portfolio`** added so multiple tabs stay in sync after trades and snapshots.
- **Timestamps** standardised to UTC ISO 8601 with `Z` suffix at both the storage and wire layers.
- **Quantity precision** capped at 4 decimal places.
- **Chat history window** is the last 20 messages by default, configurable via `CHAT_HISTORY_LIMIT`. LLM call has a `LLM_TIMEOUT_SECONDS` hard timeout (default 30) returning `504 LLM_TIMEOUT`.
- **Initial portfolio snapshot** of $10,000 is seeded at DB init so the P&L chart has an origin.
- **portfolio_snapshots** gets an index on `(user_id, recorded_at)`; the writer lives in `backend/app/portfolio/snapshotter.py` and runs for the app's lifetime (no market-hours gating).
