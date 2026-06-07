# Backend — Developer Guide

FastAPI backend for FinAlly, managed with `uv`. The market data subsystem
(`app/market/`) is complete and the focus of this guide. For the full product
spec see `../planning/PLAN.md`; for deeper design detail see
`../planning/MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE_API.md`.

## Setup

```bash
cd backend
uv sync --extra dev                        # all deps incl. test/lint tools
uv run --extra dev pytest -q               # 82 tests
uv run --extra dev pytest --cov=app        # coverage (~98%)
uv run --extra dev ruff check app/ tests/  # lint
uv run market_data_demo.py                 # live terminal dashboard
```

Always use `uv run ...` / `uv add ...` — never bare `python` or `pip`.

## Market Data — Architecture

One contract, two implementations, a single in-memory cache as the hub. Nothing
downstream knows or cares which source filled the cache.

```
create_market_data_source(cache)        ← picks source from MASSIVE_API_KEY
            │
   ┌────────┴─────────┐
   ▼                  ▼
SimulatorDataSource   MassiveDataSource
(GBM, 500ms ticks)    (Polygon REST poll, 15s)
   │                  │
   └────────┬─────────┘  writes
        PriceCache  ──── monotonic version counter
            │  reads
   ┌────────┼──────────────────┐
   ▼        ▼                   ▼
 SSE      portfolio          trade
/prices   valuation          execution
```

Producers *write* the latest price per ticker; consumers *read* snapshots.
Single uvicorn worker, single data-source task (matches PLAN.md §7).

### Module map (`app/market/`)

| File | Role |
|---|---|
| `models.py` | `PriceUpdate` — immutable price snapshot |
| `cache.py` | `PriceCache` — thread-safe store + version counter |
| `interface.py` | `MarketDataSource` ABC — the five-method contract |
| `factory.py` | `create_market_data_source(cache)` — env-driven selection |
| `simulator.py` | `GBMSimulator` (pure math) + `SimulatorDataSource` (async adapter) |
| `massive_client.py` | `MassiveDataSource` — Polygon/Massive REST poller |
| `stream.py` | `create_stream_router(cache)` — SSE endpoint factory |
| `seed_prices.py` | seed prices, per-ticker GBM params, correlation groups |

### Public imports

```python
from app.market import (
    PriceUpdate, PriceCache, MarketDataSource,
    create_market_data_source, create_stream_router,
)
```

## Core Types

**`PriceUpdate`** — frozen, slotted dataclass; the single shape every consumer sees.
- Fields: `ticker`, `price`, `previous_price`, `timestamp` (epoch **seconds**, float).
- Properties: `change` (4dp), `change_percent` (4dp; 0.0 if prev == 0),
  `direction` (`"up"`/`"down"`/`"flat"`).
- `to_dict()` — the SSE/JSON wire payload.
- `change`/`direction` are derived against the *previous cached tick*, not the day open.

**`PriceCache`** — thread-safe (`threading.Lock`, since the Massive poller writes
from a worker thread via `asyncio.to_thread`).
- `update(ticker, price, timestamp=None) -> PriceUpdate` — derives prev/direction,
  rounds price to 2dp, bumps `version`.
- `get(ticker)`, `get_price(ticker)`, `get_all()` (shallow copy), `remove(ticker)`.
- `version` — monotonic counter, ++ on every update; the SSE endpoint diffs it to
  decide whether anything changed (no per-ticker dirty tracking).
- Stores only the latest price per ticker; history is accumulated client-side.

**`MarketDataSource`** (ABC) — implemented by both sources:
```python
async def start(self, tickers) -> None      # begin producing; immediate first fill
async def stop(self) -> None                # cancel task; idempotent
async def add_ticker(self, ticker) -> None  # eager; picked up next cycle
async def remove_ticker(self, ticker) -> None  # drop + cache.remove
def get_tickers(self) -> list[str]
```
Both normalize tickers with `.upper().strip()` in `add_ticker`/`remove_ticker`.

## Source Selection — `factory.py`

```python
def create_market_data_source(cache) -> MarketDataSource:
    if os.environ.get("MASSIVE_API_KEY", "").strip():
        return MassiveDataSource(api_key=..., price_cache=cache)
    return SimulatorDataSource(price_cache=cache)
```
Returns an **unstarted** source; the caller awaits `start(tickers)`. A
whitespace-only key counts as unset → simulator.

| Mode | Trigger | Universe | Cadence |
|---|---|---|---|
| Simulator | `MASSIVE_API_KEY` empty/unset | 10 seeded tickers | ~500ms (≈2/s) |
| Massive | key set | whatever Polygon recognizes | 15s free, 2–5s paid |

## Simulator (default) — `simulator.py`, `seed_prices.py`

`GBMSimulator` is pure and synchronous (no asyncio/IO) so the math is directly
unit-testable; `SimulatorDataSource` is the thin async adapter owning the 500ms loop.

- **GBM:** `S(t+dt) = S(t) * exp((mu - sigma^2/2)*dt + sigma*sqrt(dt)*Z)`. Itô term
  keeps expected return = `mu`. `dt = 0.5 / (252*6.5*3600) ≈ 8.48e-8` (trading-time),
  giving believable sub-cent per-tick moves.
- **Per-ticker params** in `seed_prices.py` (`SEED_PRICES`, `TICKER_PARAMS`); runtime
  tickers fall back to `DEFAULT_PARAMS` + a random $50–$300 seed.
- **Correlated moves:** sector correlation matrix → Cholesky factor `L`; each tick
  `z_correlated = L @ z_independent`. Tech 0.6, finance 0.5, TSLA/cross-sector 0.3.
  Rebuilt on add/remove; skipped for `n <= 1`.
- **Shock events:** ~0.1%/tick/ticker → a sudden ±2–5% move for drama.
- `start()` seeds the cache from `SEED_PRICES` before the first tick; the loop wraps
  each step in `try/except` so a transient error never kills it.

The simulator does **not** enforce the 10-ticker universe — that guard belongs to
the (future) watchlist API layer, not the generic simulator.

## Massive (real data) — `massive_client.py`

- One `get_snapshot_all("stocks", tickers)` per poll fetches **all** watched tickers
  in a single request (free-tier friendly). The `massive` `RESTClient` is synchronous,
  so each poll runs in `asyncio.to_thread(...)`.
- Caches `snap.last_trade.price`; the v2 snapshot trade timestamp is **nanoseconds** →
  divide by `1_000_000_000` for epoch seconds (see `MASSIVE_API.md` §4).
- `_poll_once` is wrapped in a broad `try/except` that logs and continues: a
  401/429/network blip never kills the loop; the cache serves last-good prices.

## SSE Streaming — `stream.py`

`create_stream_router(cache)` returns a fresh `APIRouter` serving
`GET /api/stream/prices` (`text/event-stream`). The generator:
1. Emits `retry: 1000` so EventSource auto-reconnects after ~1s.
2. Polls `cache.version`; on change, emits the **full cache snapshot** as one
   `data:` line — a flat dict `{ticker: PriceUpdate.to_dict()}`, no envelope, no
   `version` field. The frontend derives deltas against its own last-seen prices.
3. Sends an initial snapshot on connect/reconnect (`last_version = -1`); if the cache
   is still empty it holds the connection open and emits the first real tick rather
   than an empty `{}`.
4. Breaks when `request.is_disconnected()`.

**Wire-format exception:** the price-SSE `timestamp` is a Unix epoch float (this
stream fires multiple times/sec). Every other timestamp in the system is UTC ISO 8601
with a `Z` suffix.

## Lifecycle Example

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)        # env-driven
await source.start(["AAPL", "GOOGL", "MSFT"])     # immediate first fill

cache.get("AAPL")        # PriceUpdate | None
cache.get_price("AAPL")  # float | None
cache.get_all()          # dict[str, PriceUpdate]

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
await source.stop()                                # cancels the task cleanly
```

## Seed Data

Default watchlist: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed
prices and per-ticker volatility/drift live in `app/market/seed_prices.py`.

## Tests

82 tests across 7 modules in `tests/market/` (~98% coverage). Run with
`uv run --extra dev pytest`. `GBMSimulator` is tested directly (pure math);
`SimulatorDataSource`/`MassiveDataSource` via async integration tests with mocked
APIs; the SSE generator by driving `_generate_events` with a fake `Request` and a
background drain task.

## Demo

```bash
uv run market_data_demo.py
```

`market_data_demo.py` renders a live Rich dashboard driven by the simulator:
per-ticker **sparklines** (40-point rolling history), **color direction arrows**
(▲ green / ▼ red / ─ flat) with color-coded price/change/%, and a **Recent Events**
panel logging notable moves (>1%) with timestamp. Runs 60s (or Ctrl+C) and prints a
session summary vs. seed prices.
