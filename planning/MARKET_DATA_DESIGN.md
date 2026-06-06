# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified data
source interface, the in-memory price cache, the GBM simulator, the Massive (Polygon.io)
REST client, the SSE streaming endpoint, and how it all wires into the FastAPI app
lifecycle.

This document describes the design **as shipped** in `backend/app/market/`. It is the
authoritative implementation reference; the higher-level rationale lives in
`MARKET_INTERFACE.md` (the contract), `MARKET_SIMULATOR.md` (the GBM math), and
`MASSIVE_API.md` (the provider). PLAN.md §6 is the product spec for the wire format.

> **Relationship to `archive/MARKET_DATA_DESIGN.md`.** That file was the *pre-build* draft.
> This document supersedes it and reflects the actual code, which diverged from the draft in
> a few places: `massive` is a top-level import (not lazy), `GBMSimulator.get_tickers()` is
> public, the unused `DEFAULT_CORR` constant was dropped, and the SSE generator is typed as
> `AsyncGenerator[str, None]`. Where the shipped code carries a known caveat (the Massive
> timestamp divisor), it is flagged inline rather than silently "fixed."

---

## Table of Contents

1. [Architecture at a Glance](#1-architecture-at-a-glance)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Unified Interface — `interface.py`](#5-unified-interface--interfacepy)
6. [Seed Prices & Parameters — `seed_prices.py`](#6-seed-prices--parameters--seed_pricespy)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming — `stream.py`](#10-sse-streaming--streampy)
11. [Package Surface — `__init__.py`](#11-package-surface--__init__py)
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Testing Strategy](#15-testing-strategy)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Architecture at a Glance

One contract, two interchangeable implementations, a single shared cache, and a thin SSE
reader. Nothing downstream of the cache knows or cares which source is running.

```
            create_market_data_source(cache)        ← reads MASSIVE_API_KEY
                        │
        ┌───────────────┴────────────────┐
        ▼                                 ▼
SimulatorDataSource                 MassiveDataSource
 (GBM, 500ms ticks)                 (REST poll, 15s default)
        │  writes                          │  writes
        └──────────────┬──────────────────┘
                       ▼
                  PriceCache  ──── monotonic version counter
                       │  reads
        ┌──────────────┼───────────────────────┐
        ▼              ▼                         ▼
  SSE /api/stream/  portfolio valuation     trade execution
     prices          (GET /api/portfolio)   (POST .../trade)
```

Four design rules govern the whole subsystem:

- **Source-agnostic consumers.** Only `factory.py` branches on simulator-vs-Massive. Every
  other module reads `PriceCache`.
- **One env var flips everything.** `MASSIVE_API_KEY` non-empty → real data; absent →
  simulation. No code change, no rebuild.
- **Push to write, pull to read.** A single background task *writes* the latest price per
  ticker; consumers *read* the latest snapshot. Producers and consumers are fully decoupled
  in time — the simulator ticks at 500ms, Massive polls every 15s, the SSE reader loops at
  500ms, and none of them block each other.
- **Single-process, single-writer.** One uvicorn worker, one data-source task — matches the
  concurrency model in PLAN.md §7.

---

## 2. File Structure

```
backend/app/market/
  __init__.py          # Public re-exports
  models.py            # PriceUpdate dataclass
  cache.py             # PriceCache (thread-safe store + version counter)
  interface.py         # MarketDataSource ABC
  seed_prices.py       # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
  simulator.py         # GBMSimulator (pure math) + SimulatorDataSource (async adapter)
  massive_client.py    # MassiveDataSource (REST poller)
  factory.py           # create_market_data_source()
  stream.py            # create_stream_router() — SSE endpoint
```

Each file has one responsibility. `__init__.py` re-exports the public API so the rest of the
backend imports from `app.market` without reaching into submodules.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the only type that leaves the market layer. Every consumer — SSE,
portfolio valuation, trade execution — works exclusively with it.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds (epoch float)

    @property
    def change(self) -> float:
        """Absolute price change from previous update (4 dp)."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update (4 dp); 0.0 if previous == 0."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission (PLAN.md §6 wire shape)."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

### Design decisions

- **`frozen=True`** — value objects, never mutated after creation. Safe to share across
  async tasks and hand to cache readers without defensive copies.
- **`slots=True`** — memory win; we create one of these per ticker per tick (≈20/sec).
- **Derived properties** — `change`, `change_percent`, `direction` are computed from `price`
  and `previous_price` so they can never drift out of sync. There is no stored `direction`
  field to go stale.
- **`change`/`change_percent` are tick-over-tick**, not day-over-day. They compare against
  the *previous cached price for the same ticker*. A day-change display, if ever wanted,
  comes from Massive's `prev_day.close` / `todays_change_percent` (see `MASSIVE_API.md` §3).
- **`timestamp` is epoch seconds (a float), deliberately.** This is the single project-wide
  exception to the ISO-8601-with-`Z` rule (PLAN.md §6, §13): the price stream fires several
  times a second and the frontend re-parses to a number for charting, so ISO formatting on
  every emit would be wasted work.

### Wire example

```json
{
  "ticker": "AAPL",
  "price": 190.45,
  "previous_price": 190.32,
  "timestamp": 1748269938.412,
  "change": 0.13,
  "change_percent": 0.0682,
  "direction": "up"
}
```

---

## 4. Price Cache — `cache.py`

The single point of truth. Producers call `update`; consumers call the `get*` family. A
`threading.Lock` (not `asyncio.Lock`) guards the dict because the Massive poller writes from
a real OS thread (via `asyncio.to_thread`) while SSE/portfolio read from the event-loop
thread — only a thread mutex protects both.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Derives direction/change from the previous cached price.
        First write for a ticker: previous_price == price, so direction == 'flat'.
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter; ++ on every update. Used for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Key behaviors

- **Price is rounded to 2 dp on store.** `change`/`change_percent` keep 4 dp (they are
  derived on read). Full-precision money math lives in the portfolio layer, not here.
- **Latest-only.** The cache holds exactly one `PriceUpdate` per ticker. History
  (sparklines, the P&L chart) is accumulated on the frontend from the SSE stream and from
  `portfolio_snapshots`, never held server-side.
- **The version counter is the SSE change signal.** The reader diffs `version` against its
  last-seen value to decide whether anything changed — no per-ticker dirty tracking:

  ```python
  last_version = -1
  while True:
      if cache.version != last_version:
          last_version = cache.version
          yield format_sse(cache.get_all())
      await asyncio.sleep(0.5)
  ```

### Why `threading.Lock` and not `asyncio.Lock`

`asyncio.Lock` only serializes coroutines on one event loop; it does **not** protect against
a write happening in a worker thread. The Massive client's synchronous `get_snapshot_all()`
runs under `asyncio.to_thread()` in a real OS thread, so the cache must be guarded by a true
thread mutex. `threading.Lock` is correct from both the thread and the event loop. The
critical section is a dict lookup plus an assignment — contention at 10 tickers × ~2 Hz is
negligible.

---

## 5. Unified Interface — `interface.py`

The contract both data sources satisfy. Implementations *push* into the injected
`PriceCache`; nothing calls them to *pull* a price.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing updates; spawn the background task. Call once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the task and release resources. Safe to call repeatedly."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove from the active set AND from the PriceCache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Current actively-tracked tickers."""
```

### Lifecycle guarantees both implementations honor

| Method | Guarantee |
|---|---|
| `start(tickers)` | **Immediate first fill** before the first interval elapses, so SSE clients never see an empty cache. Simulator seeds from `SEED_PRICES`; Massive does one synchronous poll. |
| `add_ticker(t)` | **Eager.** Mutates the in-memory set immediately; next produce cycle includes it. The simulator additionally seeds the new ticker's cache entry on the spot. |
| `remove_ticker(t)` | Removes from the active set **and** calls `cache.remove(t)`, so the ticker stops appearing in the SSE snapshot. |
| `stop()` | Cancels the asyncio task and swallows `CancelledError`. Idempotent. |

The push model is what decouples timing: the SSE layer never needs to know the source's
cadence, and swapping sources changes nothing downstream.

---

## 6. Seed Prices & Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond stdlib. Shared by the simulator for starting
prices, per-ticker GBM params, and the correlation structure.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# Per-ticker GBM parameters — sigma: annualized volatility, mu: annualized drift.
# NOTE: values are dicts {"sigma": ..., "mu": ...}, not tuples.
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers added at runtime without explicit params
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector membership for the Cholesky correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

Volatility (`sigma`) is the main "feel" lever: TSLA at `0.50` visibly jumps; JPM/V near
`0.17` barely drift. A ticker added at runtime that lacks explicit params falls back to
`DEFAULT_PARAMS` and a random seed price in `$50–$300` (see §7).

> In **simulator mode the universe is locked to these 10 tickers** — adding anything else to
> the watchlist returns `400 UNKNOWN_TICKER` (PLAN.md §7). The simulator *can* technically
> price an arbitrary symbol via the random-seed fallback, but the watchlist/trade validation
> layer rejects non-seeded symbols before they ever reach `add_ticker`. The fallback exists
> for Massive-mode parity and defensive robustness.

---

## 7. GBM Simulator — `simulator.py`

Two classes, math separated from plumbing:

- **`GBMSimulator`** — pure, synchronous, no I/O. Directly unit-testable.
- **`SimulatorDataSource`** — the async `MarketDataSource` adapter that loops `step()` and
  writes to the cache.

### 7.1 The math

Each tick advances every price by Geometric Brownian Motion:

```
S(t+dt) = S(t) * exp( (mu - sigma²/2) * dt  +  sigma * sqrt(dt) * Z )
```

The `- sigma²/2` term is the Itô correction that keeps the *expected* return equal to `mu`.
`dt` is 500ms expressed as a fraction of a **trading** year (not calendar), so the
annualized `sigma`/`mu` stay interpretable:

```
TRADING_SECONDS_PER_YEAR = 252 days * 6.5 hours * 3600 = 5,896,800
dt = 0.5 / 5,896,800 ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate into believable drift over
seconds and minutes rather than chaotic jumps.

### 7.2 Correlated moves — Cholesky

Real sectors move together. Build an `n×n` correlation matrix `C` from sector membership,
take its Cholesky factor `L` (`L @ L.T == C`), and on each tick turn `n` independent normals
into correlated ones:

```python
z_independent = np.random.standard_normal(n)   # n independent draws
z_correlated  = L @ z_independent              # now correlated per C
```

`C` (and `L`) is rebuilt whenever a ticker is added/removed — `O(n²)`, trivial for `n < 50`.
With `n ≤ 1` there is nothing to correlate, so the factor is `None` and the independent draw
is used directly.

### 7.3 Shock events

Pure GBM is too smooth. Each tick, **per ticker**, with probability `event_probability`
(default `0.001`), a sudden shock multiplies the price by `1 ± [2%, 5%]`. With 10 tickers at
~2 ticks/sec, expect a shock roughly every ~50 seconds somewhere on the board — enough to
make the terminal feel alive without being chaotic.

### 7.4 `GBMSimulator` — the engine

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers one tick. Returns {ticker: new_price}. Hot path."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s", ticker,
                    shock_magnitude * 100, "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Public accessor — avoids reaching into the private list."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky (batch init helper)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        if t1 == "TSLA" or t2 == "TSLA":      # in tech set, but behaves independently
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

> **Positive-definiteness.** `np.linalg.cholesky` raises if `C` is not positive-definite.
> The shipped scheme (0.5–0.6 intra-group, 0.3 cross) is comfortably valid. If you push
> intra-group correlation very high across many tickers, verify Cholesky still succeeds.

### 7.5 `SimulatorDataSource` — the async adapter

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Background asyncio task: GBMSimulator.step() every `update_interval` seconds,
    writing each price to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache BEFORE the loop so SSE has data immediately.
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)   # seed immediately
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")  # never kill the loop
            await asyncio.sleep(self._interval)
```

Key behaviors: **immediate seeding** (cache populated before the first tick → no blank
screen), **graceful cancellation** (`stop()` awaits the cancelled task), and **per-step
exception isolation** (one bad tick logs and continues; the feed survives).

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) **v2 full-market snapshot** endpoint for the union
of watched tickers in **one request per cycle** — the key to free-tier viability (5 req/min
→ 15s poll covers everything). The `massive` `RESTClient` is synchronous, so each poll runs
in `asyncio.to_thread()` to keep the event loop responsive.

```python
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Rate limits:
      - Free tier: 5 req/min  → poll every 15s (default)
      - Paid tiers: higher    → poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first fill
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers), self._interval,
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # see caveat below
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"), e,
                    )
            logger.debug("Massive poll: updated %d/%d", processed, len(self._tickers))
        except Exception as e:
            # 401 (bad key) / 429 (rate limit) / network — log and retry next cycle.
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Snapshot → cache mapping

Per `TickerSnapshot` the poller extracts exactly two fields (`MASSIVE_API.md` §3):

| Source | Used as |
|---|---|
| `snap.ticker` | cache key |
| `snap.last_trade.price` | the cached price |
| `snap.last_trade.timestamp` | the cached timestamp (after unit conversion) |

`snap.prev_day.close` and `snap.todays_change_percent` are available if a day-change display
is ever added; they are not consumed today.

> **Known timestamp-unit caveat (carried, not silently fixed).** The shipped code divides
> the snapshot trade timestamp by `1000.0`, treating it as **milliseconds**. Per
> `MASSIVE_API.md` §4 the v2 snapshot `lastTrade.t` is actually **nanoseconds**, so the
> correct divisor is `1_000_000_000`. The **cached price is unaffected** (only the timestamp
> field is wrong), and the price-SSE consumer cares about price + ordering rather than
> absolute trade time, so this has no functional impact today. New code that values
> freshness (e.g. a "stale price" indicator) should fix the divisor to `1e9` first. Flagged
> here so the discrepancy is explicit rather than hidden.

### Resilience matrix

| Failure | Behavior |
|---|---|
| 401 (bad key) | Logged; loop keeps running (fix `.env`, restart). Cache stays empty → `/api/health` returns `503`. |
| 403 (plan lacks endpoint) | Logged; treat as a fatal config error operationally. |
| 429 (rate limit) | Logged; next poll after `poll_interval`. Raise the interval if it recurs. |
| 5xx / network | Logged; retries next cycle. Cache serves last-good prices meanwhile. |
| Malformed single snapshot | That ticker skipped with a warning; others still processed. |

The whole `_poll_once` body is wrapped so **one bad poll never kills the task** — the cache
simply serves last-good prices until the next good cycle.

### A note on imports

`massive` is imported at **module top level** (not lazily) because it is a declared core
dependency in `pyproject.toml`. `factory.py` only constructs `MassiveDataSource` when
`MASSIVE_API_KEY` is set, so the simulator path never *uses* it — but the package is always
installed, so importing it unconditionally is fine and keeps the module straightforward.
(The archived draft proposed lazy imports; that was dropped during the build.)

---

## 9. Factory — `factory.py`

The single place the simulator-vs-Massive choice is made. Returns an **unstarted** source.

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set/non-empty → Massive; otherwise → Simulator.

    Returns an unstarted source; caller awaits source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

`.strip()` means a whitespace-only key counts as unset → simulator.

| Mode | Trigger | Universe | Cadence |
|---|---|---|---|
| Simulator | key empty/unset | 10 seeded tickers only (else `UNKNOWN_TICKER`) | ~500ms (≈2 events/s) |
| Massive | key set | whatever Massive recognizes (validated at add) | poll interval (15s free, 2–5s paid) |

---

## 10. SSE Streaming — `stream.py`

A FastAPI router factory that serves `GET /api/stream/prices` as a long-lived
`text/event-stream`. The generator emits a full cache snapshot **only when
`cache.version` changes** — no fixed heartbeat (PLAN.md §6).

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """SSE router with an injected PriceCache (no globals)."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yield SSE price events on cache-version change; stop on client disconnect."""
    yield "retry: 1000\n\n"  # EventSource auto-reconnects ~1s after a drop

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {t: u.to_dict() for t, u in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format

Each event is one `data:` line carrying the **full cache snapshot** keyed by ticker — no
envelope, no `version` field on the wire. The frontend derives deltas against its own
last-seen prices.

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1748269938.412,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Client:

```javascript
const es = new EventSource('/api/stream/prices');
es.onmessage = (event) => {
  const prices = JSON.parse(event.data);   // { AAPL: {price, direction, ...}, ... }
  // flash green/red vs. last-seen, push point into the sparkline buffer, etc.
};
```

### Initial-snapshot guarantee

Because `start()` seeds/polls the cache **before** the loop begins, the very first
changed-version read a freshly connected (or reconnected) client gets is already a populated
snapshot — never an empty `{}`. The `if prices:` guard means that, in the rare window before
the first tick (e.g. a slow Massive cold start), nothing is emitted and the connection is
held open until real data exists, satisfying PLAN.md §6's "hold open, emit the first real
tick" rule. All events use the default EventSource `message` event (no named `event:`
field), so the client listens only to `onmessage`.

### Disconnect handling

The loop checks `request.is_disconnected()` each iteration and breaks cleanly; a server-side
cancel is caught as `CancelledError`. EventSource's built-in retry (reinforced by the
`retry: 1000` directive) re-establishes the connection and gets a fresh snapshot — no
client-side resync logic needed.

---

## 11. Package Surface — `__init__.py`

The rest of the backend imports from `app.market`, never from submodules.

```python
"""Market data subsystem for FinAlly."""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

---

## 12. FastAPI Lifecycle Integration

The cache, the data source, and the SSE router are wired in the FastAPI `lifespan` context
manager. The cache and source are stashed on `app.state` so route handlers reach them via
dependency injection.

```python
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)   # env-driven
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # from SQLite (default 10 on fresh DB)
    await source.start(initial_tickers)               # immediate first fill

    yield  # app runs

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# SSE router is created with the cache injected. Including it at import time is fine
# because the cache object is created per-process before requests are served; if you
# prefer, include it inside lifespan after the cache exists.
app.include_router(create_stream_router(app.state.price_cache))


# --- DI accessors ---
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

> **Router-inclusion ordering note.** `create_stream_router` needs a live `PriceCache`.
> The cleanest pattern is to call `app.include_router(create_stream_router(price_cache))`
> *inside* `lifespan` right after the cache is built, so there is no import-time dependency
> on `app.state`. Either ordering works as long as the cache exists before the first request
> hits `/api/stream/prices`.

### Consuming the cache from other routes

```python
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    price = price_cache.get_price(trade.ticker)
    if price is None:
        raise HTTPException(400, {"error_code": "NO_PRICE_YET",
                                 "message": f"No price for {trade.ticker} yet — retry shortly"})
    # ... fill at `price`, mutate portfolio under the trade lock ...
```

---

## 13. Watchlist Coordination

When the watchlist changes (REST or LLM chat), the route mutates SQLite **and** notifies the
data source so the active ticker set stays in sync.

### Add

```
POST /api/watchlist {ticker: "PYPL"}
  → validate ticker against the universe (sim: 10 seeds; Massive: provider)
  → INSERT into watchlist (SQLite)
  → await source.add_ticker("PYPL")
        Simulator: GBMSimulator.add_ticker + rebuild Cholesky + seed cache entry
        Massive:   append to ticker list; appears on next poll
  → 201 { ticker, added_at }
```

### Remove (position-aware)

Removing a ticker the user still holds is **allowed** (PLAN.md §8.3): the watchlist row goes
away but the position must stay valuable, so the ticker must keep streaming.

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)        # 404 if it wasn't there

    # Only stop tracking if there's no open position to value.
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)         # also clears the cache entry

    return Response(status_code=204)
```

If a position exists, we deliberately **skip** `remove_ticker` so the ticker stays in the
data source and the cache, and the positions table can keep valuing and later selling it.

### LLM auto-execution ordering

Per PLAN.md §9, chat applies **watchlist changes first, then trades** — purely so the user
sees a new ticker appear before the buy confirmation. Trades validate against the universe
directly and do **not** require watchlist membership; the ordering is UX, not correctness.

---

## 14. Error Handling & Edge Cases

| Case | Behavior |
|---|---|
| **Empty watchlist at startup** | `start([])` is a no-op feed: simulator produces nothing, Massive skips the API call (`_poll_once` early-returns on empty list). First `add_ticker` starts tracking immediately. |
| **Trade before first price** | `cache.get_price` is `None` → route returns `400 NO_PRICE_YET`. Simulator avoids this by seeding in `add_ticker`; Massive may have a brief gap until the next poll. |
| **`UNKNOWN_TICKER` (sim mode)** | Validation rejects any symbol outside the 10 seeds before `add_ticker` is called. |
| **Invalid Massive key** | First poll 401s, logged; loop keeps retrying. Cache stays empty → SSE holds open / sends nothing; `/api/health` returns `503` until a real tick. |
| **Massive rate-limited (429)** | Logged; next attempt after `poll_interval`. Raise the interval if persistent. |
| **Transient poll/step error** | Caught, logged, loop continues; cache serves last-good prices. |
| **Removed ticker with open position** | `remove_ticker` is skipped (see §13); price keeps flowing for valuation. |
| **Cholesky non-PD** | Only if correlation constants are pushed invalid; the shipped 0.3–0.6 scheme is safe. |
| **Float precision** | Prices `round()`ed to 2 dp on store; the `exp()` form is numerically stable and always positive, so prices can never go ≤ 0. |
| **Thread-safety under load** | `threading.Lock` critical section is a dict op; negligible contention at this scale. A `RWLock` would be the (unnecessary) escalation for hundreds of tickers and many readers. |

---

## 15. Testing Strategy

The shipped suite is **73 tests across 6 modules** in `backend/tests/market/`
(overall ~84% coverage; `massive_client.py` is lower by design since the live API methods are
mocked). Run them with:

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app          # coverage
uv run --extra dev ruff check app/ tests/    # lint
```

### What each layer tests

| Module | Focus |
|---|---|
| `test_models.py` | `change`/`change_percent`/`direction` math, `to_dict` shape, zero-prev guard, frozen-ness |
| `test_cache.py` | update/get/remove, first-write-is-flat, up/down direction, version increments, `get_all` copy |
| `test_simulator.py` | GBM positivity over many steps, add/remove + Cholesky rebuild, seed prices, empty step, random-seed fallback |
| `test_simulator_source.py` | async: cache seeded on `start`, prices move over time, clean double-`stop`, add/remove round-trip |
| `test_factory.py` | env-var selection (set → Massive, empty/whitespace → simulator) |
| `test_massive.py` | mocked `_fetch_snapshots`: cache updated, malformed snapshot skipped, poll error doesn't crash |

### Representative tests

**Pure GBM (no timers, no mocks):**

```python
def test_prices_are_positive():
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0   # exp() can never produce a negative price

def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None        # 1 ticker → nothing to correlate
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None     # 2 tickers → factor exists
```

**Cache direction/version:**

```python
def test_direction_and_version():
    cache = PriceCache()
    v0 = cache.version
    assert cache.update("AAPL", 190.00).direction == "flat"   # first write
    assert cache.update("AAPL", 191.00).direction == "up"
    assert cache.update("AAPL", 190.00).direction == "down"
    assert cache.version == v0 + 3
```

**Async simulator source:**

```python
@pytest.mark.asyncio
async def test_start_seeds_then_streams():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL"])
    assert cache.get("AAPL") is not None      # seeded before first tick
    await source.add_ticker("TSLA")
    assert cache.get("TSLA") is not None       # seeded eagerly
    await source.remove_ticker("TSLA")
    assert cache.get("TSLA") is None
    await source.stop()
    await source.stop()                        # idempotent
```

**Massive poller (mock the sync fetch, drive one cycle):**

```python
def _snap(ticker, price, ts_ms):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap

@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache, poll_interval=60)
    source._client = MagicMock()               # avoid constructing a real RESTClient
    source._tickers = ["AAPL", "GOOGL"]
    snaps = [_snap("AAPL", 190.50, 1707580800000), _snap("GOOGL", 175.25, 1707580800000)]
    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()
    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("GOOGL") == 175.25

@pytest.mark.asyncio
async def test_poll_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache, poll_interval=60)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    with patch.object(source, "_fetch_snapshots", side_effect=Exception("boom")):
        await source._poll_once()              # must NOT raise
    assert cache.get_price("AAPL") is None
```

> The Massive tests set `source._client` to a `MagicMock` and patch `_fetch_snapshots`, so no
> real network call (and no real `RESTClient`) is ever made. This is why `massive_client.py`
> coverage is intentionally partial — the live REST paths are out of scope for unit tests.

### E2E

E2E (`test/`, Playwright, `LLM_MOCK=true`) exercises the *consumer* side: prices stream on
fresh load, add/remove a ticker, SSE reconnects after a drop. Those scenarios run against
the simulator so they need no API key.

---

## 16. Configuration Summary

| Parameter | Where | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | env var | `""` | non-empty → Massive; empty/whitespace → simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | simulator tick cadence |
| `event_probability` | `GBMSimulator` / `SimulatorDataSource` | `0.001` | per-ticker per-tick shock chance |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive poll cadence (lower on paid tiers) |
| SSE read `interval` | `_generate_events` | `0.5` s | how often the SSE loop checks `cache.version` |
| SSE `retry` | `_generate_events` | `1000` ms | EventSource reconnection delay directive |

### Tuning cheatsheet (simulator)

| Want | Change |
|---|---|
| A ticker to move more/less | its `sigma` in `TICKER_PARAMS` |
| A persistent up/down bias | its `mu` (drift) |
| Faster/slower visual updates | `update_interval` (and `dt` to keep annualization honest) |
| More/fewer dramatic jumps | `event_probability` |
| Stronger sector coupling | `INTRA_TECH_CORR` / `INTRA_FINANCE_CORR` (keep the matrix positive-definite) |
| A new default ticker | add to `SEED_PRICES` + `TICKER_PARAMS` (+ a correlation group) |

### Quick usage recap

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)        # env-driven
await source.start(["AAPL", "GOOGL", "MSFT"])    # immediate first fill

cache.get("AAPL")        # PriceUpdate | None
cache.get_price("AAPL")  # float | None
cache.get_all()          # dict[str, PriceUpdate]

await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
await source.stop()
```
</content>
</invoke>
