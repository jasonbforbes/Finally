# Market Simulator

The default price source when `MASSIVE_API_KEY` is not set. It generates realistic,
continuously-moving prices for the watchlist using **Geometric Brownian Motion (GBM)** with
**sector-correlated** moves and occasional **shock events** for visual drama. No network, no
API key, no external dependencies — it runs as an in-process asyncio task 24/7.

Implementation: `backend/app/market/simulator.py` and `backend/app/market/seed_prices.py`.
For how it plugs into the rest of the system see `MARKET_INTERFACE.md`.

## 1. Why a Simulator

- **Zero-config demo.** Students run one command and see a live, moving terminal without
  signing up for a data vendor.
- **Deterministic universe.** Exactly the 10 seeded tickers (PLAN.md §7). Adding any other
  ticker in simulator mode returns `400 UNKNOWN_TICKER`.
- **Always on.** Real markets close; the simulator runs around the clock so the demo is never
  flat. The portfolio snapshotter (PLAN.md §7) likewise ignores market hours.

## 2. The Math — Geometric Brownian Motion

GBM is the standard model for stock prices: returns are normally distributed, prices stay
positive, and volatility scales with price. Each tick advances every price by:

```
S(t+dt) = S(t) * exp( (mu - sigma^2 / 2) * dt  +  sigma * sqrt(dt) * Z )
```

| Symbol | Meaning |
|---|---|
| `S(t)` | current price |
| `mu` | annualized drift (expected return) |
| `sigma` | annualized volatility |
| `dt` | time step as a fraction of a trading year |
| `Z` | a standard normal draw — **correlated** across tickers (see §4) |

The `- sigma^2 / 2` term is the Itô correction that keeps the *expected* return equal to
`mu` despite the exponential.

### Choosing `dt`

Ticks are 500ms. Expressed as a fraction of a trading year:

```
TRADING_SECONDS_PER_YEAR = 252 days * 6.5 hours * 3600 = 5,896,800
dt = 0.5 / 5,896,800 ≈ 8.48e-8
```

This tiny `dt` produces **sub-cent moves per tick** that accumulate naturally — prices drift
believably over seconds and minutes rather than jumping around. Using wall-clock trading time
(not calendar time) keeps the annualized `sigma`/`mu` interpretable.

## 3. Seed Prices and Per-Ticker Parameters

From `seed_prices.py`. Realistic starting prices and volatility/drift tuned per name:

```python
SEED_PRICES = {            # realistic starting prices
    "AAPL": 190, "GOOGL": 175, "MSFT": 420, "AMZN": 185, "TSLA": 250,
    "NVDA": 800, "META": 500, "JPM": 195, "V": 280, "NFLX": 600,
}

TICKER_PARAMS = {          # annualized sigma (volatility) / mu (drift)
    "AAPL": (0.22, 0.05), "GOOGL": (0.25, 0.05), "MSFT": (0.20, 0.05),
    "AMZN": (0.28, 0.05), "TSLA": (0.50, 0.03),   # TSLA: very volatile
    "NVDA": (0.40, 0.08),                          # NVDA: volatile, strong drift
    "META": (0.30, 0.05), "JPM": (0.18, 0.04),     # banks: calm
    "V":    (0.17, 0.04), "NFLX": (0.35, 0.05),
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}       # for any dynamically-added ticker
```

Volatility is the main lever for "feel": TSLA at `0.50` visibly jumps; JPM/V near `0.17`
barely drift. Tickers added at runtime that lack explicit params fall back to `DEFAULT_PARAMS`
(and a random seed price in `$50–$300`).

## 4. Correlated Moves — Cholesky Decomposition

Real sectors move together: when tech rallies, AAPL/MSFT/NVDA tend to rise as one. The
simulator reproduces this so the watchlist doesn't look like 10 independent random walks.

**How.** Build an `n x n` correlation matrix `C` from sector membership, then take its
**Cholesky factor** `L` (where `L @ L.T == C`). Each tick:

```python
z_independent = np.random.standard_normal(n)   # n independent draws
z_correlated  = L @ z_independent              # now correlated per C
```

Feeding `z_correlated[i]` as the `Z` for ticker `i` makes their moves share the desired
correlation structure.

**The correlation rules** (`_pairwise_correlation`):

| Pair | rho |
|---|---|
| Same tech sector (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.60 |
| Same finance sector (JPM, V) | 0.50 |
| TSLA with anything | 0.30 (it does its own thing) |
| Cross-sector / unknown tickers | 0.30 |

The matrix (and its Cholesky factor) is rebuilt whenever a ticker is added or removed —
`O(n^2)`, trivial for `n < 50`. With `n <= 1` there's nothing to correlate, so the factor is
skipped and the independent draw is used directly.

## 5. Shock Events

Pure GBM is smooth; markets are not. Each tick, **per ticker**, with probability
`event_probability = 0.001` (0.1%), a sudden shock multiplies the price by `1 ± [2%, 5%]`:

```python
if random.random() < self._event_prob:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

With 10 tickers at ~2 ticks/sec, expect a shock roughly **every ~50 seconds** somewhere on
the board — enough to make the terminal feel alive (price flashes, sparkline kinks) without
being chaotic.

## 6. Code Structure

Two classes, clean separation of *math* from *plumbing*:

### `GBMSimulator` — pure, synchronous, testable

No asyncio, no cache, no I/O. Holds per-ticker price/params and the Cholesky factor.

```python
class GBMSimulator:
    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001)
    def step(self) -> dict[str, float]      # advance ALL tickers one tick → {ticker: price}
    def add_ticker(self, ticker)            # add + rebuild Cholesky
    def remove_ticker(self, ticker)         # drop + rebuild Cholesky
    def get_price(self, ticker) -> float | None
    def get_tickers(self) -> list[str]
```

`step()` is the hot path (called every 500ms): one vectorized normal draw, one matrix-vector
product, then a per-ticker GBM update + shock roll, returning prices rounded to 2dp. Being
pure makes the GBM math and correlation directly unit-testable without timers or mocks.

### `SimulatorDataSource` — the async adapter

Implements `MarketDataSource` (see `MARKET_INTERFACE.md` §3). Owns the background loop and
the bridge to `PriceCache`:

```python
class SimulatorDataSource(MarketDataSource):
    async def start(self, tickers):     # build GBMSimulator, seed cache, spawn loop task
    async def stop(self):               # cancel loop, swallow CancelledError
    async def add_ticker(self, ticker): # sim.add_ticker + seed cache immediately
    async def remove_ticker(self, ticker): # sim.remove_ticker + cache.remove
    async def _run_loop(self):          # while True: step → write cache → sleep(interval)
```

`start()` seeds the cache from `SEED_PRICES` *before* the first tick so SSE clients get data
immediately. `_run_loop` wraps each step in `try/except` and logs — a transient error never
kills the loop.

## 7. Tuning Cheatsheet

| Want | Change |
|---|---|
| A ticker to move more/less | its `sigma` in `TICKER_PARAMS` |
| A persistent up/down bias | its `mu` (drift) |
| Faster/slower visual updates | `update_interval` on `SimulatorDataSource` (and `dt`) |
| More/fewer dramatic jumps | `event_probability` |
| Stronger sector coupling | the `INTRA_TECH_CORR` / `INTRA_FINANCE_CORR` constants |
| A new default ticker | add to `SEED_PRICES` + `TICKER_PARAMS` (+ a correlation group) |

> Correlation values must keep the matrix **positive-definite** or `np.linalg.cholesky`
> raises. The sector scheme here (0.5–0.6 intra, 0.3 cross) is comfortably valid; if you push
> intra-group correlation very high across many tickers, verify Cholesky still succeeds.

## 8. Demo

`backend/market_data_demo.py` renders a live Rich dashboard (sparklines, direction arrows,
event log) driven entirely by the simulator:

```bash
cd backend
uv run market_data_demo.py
```
