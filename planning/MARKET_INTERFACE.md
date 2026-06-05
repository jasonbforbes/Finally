# Market Data Interface

The unified Python interface FinAlly uses to retrieve stock prices. One contract, two
implementations: the **Massive REST poller** when `MASSIVE_API_KEY` is set, the **GBM
simulator** otherwise. Everything downstream (SSE, portfolio valuation, trade execution)
reads prices from a single in-memory cache and never knows or cares which source filled it.

This documents the shipped design in `backend/app/market/`. For the underlying provider see
`MASSIVE_API.md`; for the simulator internals see `MARKET_SIMULATOR.md`.

## 1. Design Goals

- **Source-agnostic consumers.** No code outside `app/market/` branches on simulator vs.
  Massive. They read the cache.
- **One env var flips everything.** `MASSIVE_API_KEY` present and non-empty → real data;
  absent → simulation. No code change, no rebuild.
- **Push model, pull reads.** A single background task *writes* the latest price per ticker;
  consumers *read* the latest snapshot. Producers and consumers are fully decoupled.
- **Single-process, single-writer.** One uvicorn worker, one data-source task. Matches the
  single-worker concurrency model in PLAN.md §7.

## 2. Component Map

```
            create_market_data_source(cache)        ← reads MASSIVE_API_KEY
                        │
        ┌───────────────┴────────────────┐
        ▼                                 ▼
SimulatorDataSource                 MassiveDataSource
 (GBM, 500ms ticks)                 (REST poll, 15s default)
        │                                 │
        └──────────────┬──────────────────┘
                       ▼  writes
                  PriceCache  ──── version counter
                       │  reads
        ┌──────────────┼───────────────────────┐
        ▼              ▼                         ▼
  SSE /api/stream/  portfolio valuation     trade execution
     prices          (GET /api/portfolio)   (POST .../trade)
```

Files:

| File | Role |
|---|---|
| `interface.py` | `MarketDataSource` ABC — the contract |
| `models.py` | `PriceUpdate` — immutable price snapshot |
| `cache.py` | `PriceCache` — thread-safe store + version counter |
| `factory.py` | `create_market_data_source(cache)` — env-driven selection |
| `simulator.py` | `SimulatorDataSource` (+ `GBMSimulator`) |
| `massive_client.py` | `MassiveDataSource` — REST poller |
| `stream.py` | `create_stream_router(cache)` — SSE endpoint |
| `seed_prices.py` | seed prices, GBM params, correlation groups |

## 3. The Contract — `MarketDataSource`

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None: ...   # begin producing; call once
    async def stop(self) -> None: ...                         # stop task; idempotent
    async def add_ticker(self, ticker: str) -> None: ...      # next cycle includes it
    async def remove_ticker(self, ticker: str) -> None: ...   # drop + remove from cache
    def get_tickers(self) -> list[str]: ...                   # current active set
```

Lifecycle:

```python
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])   # spawns the background task
...
await source.add_ticker("TSLA")        # watchlist add  → eager, picked up next cycle
await source.remove_ticker("GOOGL")    # watchlist del  → also cache.remove("GOOGL")
...
await source.stop()                     # app shutdown   → cancels the task cleanly
```

Both implementations satisfy this identically:
- `start` does an **immediate first fill** so the cache has data before the first interval
  elapses (critical on Massive's 15s free-tier cadence — see the initial-snapshot rule in
  PLAN.md §6). The simulator seeds from `SEED_PRICES`; Massive does one synchronous poll.
- `add_ticker` is **eager**: it mutates the in-memory ticker set immediately and the next
  produce cycle picks it up. The simulator additionally seeds the new ticker's cache entry on
  the spot so it has a price without waiting a tick.
- `remove_ticker` removes from both the active set **and** the cache, so a removed ticker
  stops appearing in the SSE snapshot.
- `stop` cancels the asyncio task and swallows `CancelledError`; safe to call more than once.

## 4. The Data Model — `PriceUpdate`

A frozen, slotted dataclass — the single shape every consumer sees, regardless of source.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)   # epoch SECONDS (float)

    @property
    def change(self) -> float: ...          # price - previous_price (4dp)
    @property
    def change_percent(self) -> float: ...  # percent move (4dp); 0.0 if prev == 0
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"
    def to_dict(self) -> dict: ...          # wire payload (see PLAN.md §6)
```

Notes:
- `timestamp` is **epoch seconds**, deliberately. This is the one wire field that is *not*
  ISO-8601 (PLAN.md §6): the price stream fires multiple times a second and the frontend
  re-parses to a number for charting, so ISO formatting on every emit is wasted work.
- `change`/`change_percent`/`direction` are derived against the *previous cached price for
  the same ticker* (i.e. the last tick), not against the day open. Day-change display, if
  wanted, comes from Massive's `prev_day.close` / `todays_change_percent`.
- Immutable by design: cache readers get a snapshot they cannot accidentally mutate.

## 5. The Hub — `PriceCache`

The single point of truth. Producers call `update`; consumers call `get*`. A `threading.Lock`
guards the dict because the Massive poller runs cache writes from a worker thread (via
`asyncio.to_thread`), while SSE/portfolio read from the event-loop thread.

```python
class PriceCache:
    def update(self, ticker, price, timestamp=None) -> PriceUpdate   # computes prev/direction; bumps version
    def get(self, ticker) -> PriceUpdate | None
    def get_price(self, ticker) -> float | None
    def get_all() -> dict[str, PriceUpdate]                          # shallow copy
    def remove(self, ticker) -> None
    @property
    def version(self) -> int                                         # monotonic; ++ every update
```

Key behaviors:
- `update` derives `previous_price` from whatever is currently cached for that ticker (first
  write: `previous_price == price`, so `direction == "flat"`). Price is rounded to 2dp on
  store.
- **`version`** is a monotonic counter incremented on every `update`. The SSE endpoint diffs
  this counter to decide whether anything changed — no per-ticker dirty tracking needed.
- The cache stores **only the latest** price per ticker. History (sparklines, P&L) is
  accumulated by the frontend from the SSE stream, not held server-side.

## 6. Source Selection — `factory.py`

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

The only place the choice is made. Returns an **unstarted** source; the caller awaits
`start(tickers)`. `.strip()` means a whitespace-only key counts as unset → simulator.

| Mode | Trigger | Ticker universe | Update cadence |
|---|---|---|---|
| Simulator | `MASSIVE_API_KEY` empty/unset | 10 seeded tickers only (else `UNKNOWN_TICKER`) | ~500ms (≈2 events/s) |
| Massive | `MASSIVE_API_KEY` set | whatever Massive recognizes (validated at add) | poll interval (15s free, 2–5s paid) |

## 7. Massive Implementation — `MassiveDataSource`

- Holds the watched-ticker set in memory; one `get_snapshot_all("stocks", tickers)` call per
  poll fetches **all** of them (one request → free-tier friendly; see `MASSIVE_API.md` §3).
- The `massive` `RESTClient` is **synchronous**, so each poll runs inside
  `asyncio.to_thread(...)` to keep the event loop responsive.
- Per snapshot it writes `cache.update(ticker, price=snap.last_trade.price,
  timestamp=<epoch seconds>)`. The trade timestamp is **nanoseconds** — divide by `1e9`
  (see the caution in `MASSIVE_API.md` §4).
- `_poll_once` is wrapped in a broad `try/except` that logs and continues: a 401/429/network
  blip never kills the loop; the cache serves last-good prices until the next good cycle.

## 8. Simulator Implementation — `SimulatorDataSource`

Thin async wrapper around the pure `GBMSimulator`: every `update_interval` (500ms) it calls
`sim.step()` and writes each returned price to the cache. Full internals — GBM math, seed
prices, sector correlation, shock events — are in `MARKET_SIMULATOR.md`.

## 9. SSE Delivery — `stream.py`

`create_stream_router(cache)` returns a FastAPI router serving `GET /api/stream/prices`.
The generator:
1. Emits `retry: 1000` so EventSource auto-reconnects after ~1s on drop.
2. Loops on a short interval, comparing `cache.version` to the last seen value.
3. On change, serializes the **full cache snapshot** (`{ticker: PriceUpdate.to_dict()}`) as one
   `data:` line. No envelope, no `version` field on the wire — the frontend derives deltas
   against its own last-seen prices (PLAN.md §6, §13).
4. Breaks when `request.is_disconnected()`.

Because the first poll/seed happens inside `start()`, a freshly connected client gets a
populated snapshot on its first changed-version read rather than an empty `{}`.

## 10. Extending the System

Adding a third source (e.g. a WebSocket push client, or a different vendor):
1. Implement `MarketDataSource` (the five methods), writing to the injected `PriceCache`.
2. Add a branch in `create_market_data_source`.
3. Done — SSE, portfolio, trades, and the entire frontend are untouched.

The cache-as-hub design also means multi-user is a future cache-keying change, not a
consumer rewrite (PLAN.md §6 "supports future multi-user without changes to the data layer").

## 11. Usage Recap

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)          # env-driven
await source.start(["AAPL", "GOOGL", "MSFT"])      # immediate first fill

cache.get("AAPL")        # PriceUpdate | None
cache.get_price("AAPL")  # float | None
cache.get_all()          # dict[str, PriceUpdate]

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
await source.stop()
```
