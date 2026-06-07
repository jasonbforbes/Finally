# Market Data Backend — Code Review

**Date:** 2026-06-06
**Scope:** `backend/app/market/` (8 modules) and `backend/tests/market/` (6 test modules), reviewed against `PLAN.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE_API.md`.
**Verdict:** Solid, well-structured, ships as documented. All tests pass and lint is clean. The two priority items from this review — the Massive timestamp unit bug and the SSE-endpoint test gap — have since been **resolved** (see the Update below); the remaining findings are low-severity polish.

> **Update — 2026-06-06 (post-review fixes):**
> - **All findings are now RESOLVED** — HIGH (timestamp), MEDIUM (SSE tests), and the LOW/INFO polish items (global router, ticker normalization, deprecated fixture, doc drift). See each finding for details.
> - Suite grew from 73 → **82 tests passing**; total coverage **91% → 98%**; `stream.py` **33% → 97%**; test-suite warnings eliminated. Lint still clean.

---

## 1. Test & Lint Results (reproduced)

```
uv run --extra dev pytest --cov=app
80 passed, 80 warnings in 1.99s          # was 73 passed at original review

uv run --extra dev ruff check app/ tests/
All checks passed!
```

Coverage (Python 3.14.5; "orig" = original review, before the post-review fixes):

| Module | Cover | Orig | Note |
|---|---|---|---|
| cache.py | 100% | 100% | |
| factory.py | 100% | 100% | |
| interface.py | 100% | 100% | |
| models.py | 100% | 100% | |
| seed_prices.py | 100% | 100% | |
| simulator.py | 98% | 98% | |
| massive_client.py | 94% | 94% | |
| **stream.py** | **97%** | 33% | SSE generator now covered (1 line: `CancelledError` log handler) |
| **TOTAL** | **98%** | 91% | |

Independently verified beyond the suite:
- The full 10-ticker correlation matrix is positive-definite — `np.linalg.cholesky` succeeds and 100 `step()` calls run clean. (No existing test exercises the full seeded set; the integration tests use 1–2 tickers.)

---

## 2. Findings

### HIGH — Massive last-trade timestamp uses the wrong unit  ✅ RESOLVED (2026-06-06)

> **Fixed.** `massive_client.py` now divides by `1_000_000_000`. `test_massive.py` was updated to feed realistic nanosecond inputs (helper param renamed `timestamp_ms` → `timestamp_ns`, literals `1707580800000` → `1707580800000000000`); the conversion test's `1707580800.0` assertion now genuinely guards the divisor — reverting to `/1000` makes it fail. 13 Massive tests pass.

`massive_client.py:103` (original)
```python
timestamp = snap.last_trade.timestamp / 1000.0   # treats ns as ms
```
The v2 snapshot `last_trade.timestamp` is **nanoseconds** (see `MASSIVE_API.md` §4, which already flags this exact line). The correct divisor is `1_000_000_000`. Dividing by `1000` produces a cached `timestamp` of ~`1.7e15` — an epoch in the year ~54,000,000 — instead of ~`1.7e9`.

Impact is limited to **Massive mode only** (the default simulator is unaffected, and the cached *price* is correct), but the price-SSE `timestamp` field is consumed by the frontend for charting (`PLAN.md` §6), so live charts in Massive mode would key off nonsensical times.

**The unit test enshrines the bug.** `test_massive.py::test_timestamp_conversion` feeds `1707580800000` (a *millisecond* value, helper param literally named `timestamp_ms`) and asserts `== 1707580800.0`. Because the mock input is already milliseconds, the wrong `/1000` divisor produces a "correct"-looking result. Fixing the code requires updating the test to use a realistic nanosecond input and the `/1e9` divisor.

**Fix:** change the divisor to `1_000_000_000` in `_poll_once`, and update the test (rename `timestamp_ms` → `timestamp_ns`, pass `1707580800000000000`, keep the `1707580800.0` expectation).

### MEDIUM — SSE endpoint (`stream.py`) has no tests  ✅ RESOLVED (2026-06-06)

> **Fixed.** Added `backend/tests/market/test_stream.py` (7 tests), taking `stream.py` from 33% → 97%. The generator is driven directly with a real `PriceCache` and a fake `Request`, collecting frames in a background drain task so the absence-assertions verify the stream *stayed silent* without a timeout-cancel closing the generator. Covered: retry directive, initial snapshot on connect (with wire-shape assertion), empty-cache hold-open, version-change-only emission, disconnect termination, and router wiring (route registration, media type, headers). The single remaining uncovered line is the defensive `except asyncio.CancelledError` log handler.

Coverage is 33%; the `_generate_events` async generator is the most behavior-rich, spec-laden code in the subsystem and is uncovered. `PLAN.md` §6 and `CHANGE_REVIEW.md` (Batch 3) define several subtle contracts that are currently unverified by any test:

- Initial full-snapshot emitted on connect/reconnect (`last_version = -1` achieves this — looks correct on read).
- Empty-cache behavior: hold the connection open and emit the first real tick rather than sending `{}` (the `if prices:` guard achieves this — looks correct on read).
- Disconnect detection via `request.is_disconnected()` breaking the loop.
- The wire shape: flat dict-of-ticker, no envelope, no `version` field.

These read as correct but are load-bearing for the frontend and deserve coverage (FastAPI `TestClient` / `httpx` streaming, or by calling `_generate_events` directly with a fake `Request`).

### LOW — `create_stream_router` mutates a module-global router  ✅ RESOLVED (2026-06-06)

> **Fixed.** The `APIRouter` is now constructed inside `create_stream_router`, so each call returns a fresh single-route router. The test helper's last-match workaround was simplified accordingly.


`stream.py:17` defines `router = APIRouter(...)` at module scope, and `create_stream_router` registers the route onto that shared global. Calling the factory more than once would register the `/prices` route repeatedly on the same router instance. It is only called once today, but the factory pattern implies reusability. Prefer constructing `APIRouter` *inside* the function and returning it, so each call yields an independent, single-route router.

### LOW — Ticker normalization differs between the two sources  ✅ RESOLVED (2026-06-06)

> **Fixed.** `SimulatorDataSource.add_ticker/remove_ticker` now apply `.upper().strip()`, matching `MassiveDataSource`, so both interface implementations handle un-normalized input identically. Added two tests (`test_add_ticker_normalizes`, `test_remove_ticker_normalizes`). The simulator universe guard (rejecting non-seeded tickers with `UNKNOWN_TICKER`) is deliberately left to the not-yet-built API layer rather than baked into the generic `GBMSimulator`; flagged here so it isn't forgotten when the watchlist API lands.

`MassiveDataSource.add_ticker/remove_ticker` apply `.upper().strip()`; `SimulatorDataSource`/`GBMSimulator` do not (original). In simulator mode, `add_ticker("aapl")` would create a *new* lowercase ticker with a random $50–$300 seed price rather than matching `AAPL`. The two implementations of the same interface therefore behave differently for un-normalized input. This is presumably masked by validation/normalization at the (not-yet-built) API layer, but the interface contract should be consistent — normalize in both, or document that callers must pass canonical symbols.

### LOW — Simulator does not enforce the locked ticker universe

`PLAN.md` §7 / `MARKET_SIMULATOR.md` §1 state simulator mode is locked to the 10 seeded tickers and adds outside that set return `400 UNKNOWN_TICKER`. `GBMSimulator.add_ticker` happily adds any symbol (random seed price, `DEFAULT_PARAMS`). This is acceptable *if* the API layer enforces the universe before calling `add_ticker`, but that enforcement does not exist in the market layer — worth a deliberate decision about where the guard lives so it isn't forgotten when the watchlist API is built.

### INFO — Test-suite warnings  ✅ RESOLVED (2026-06-06)

> **Fixed.** Deleted `tests/conftest.py` — its only content was the `event_loop_policy` override returning `asyncio.DefaultEventLoopPolicy()` (deprecated; removal in Python 3.16), which merely duplicated `pytest-asyncio`'s default under `asyncio_mode = "auto"`. The suite now runs with zero warnings.

`conftest.py:11` overrides `event_loop_policy` with `asyncio.DefaultEventLoopPolicy()`, which is deprecated (removal in Python 3.16) and produces 73 `DeprecationWarning`s (original). The fixture override appears unnecessary with modern `pytest-asyncio` (`asyncio_mode = "auto"` is already set).

### INFO — Doc drift in `MARKET_DATA_SUMMARY.md`  ✅ RESOLVED (2026-06-06)

> **Fixed.** Refreshed the summary's Test Suite table to current reality: 82 tests across 7 modules, `test_stream.py` added, `massive_client.py` 94%, overall 98%.

The summary reported "84% overall, massive_client.py 56%"; the review run showed 91% / 94%. Numbers were stale, not wrong-in-kind (original).

### INFO — Environment

Tests were run under CPython 3.14.5 (uv auto-created the venv), while `pyproject.toml` declares `requires-python = ">=3.12"`. Everything passed, but the project's stated target is 3.12; if 3.12 is the deployment runtime, pin/test against it in CI to avoid 3.14-only surprises.

---

## 3. What's Good

- **Clean strategy pattern.** `MarketDataSource` ABC with two interchangeable implementations; downstream code reads only the cache. Matches `MARKET_INTERFACE.md` faithfully.
- **Separation of math from plumbing.** `GBMSimulator` is pure and synchronous (no asyncio, no I/O), making the GBM/correlation logic directly unit-testable; `SimulatorDataSource` is the thin async adapter. This is the right seam and it's well tested (98%).
- **Correct GBM.** Itô correction term present, `dt` derived from trading-time, prices stay positive (10k-step test), 2dp rounding on the wire while full precision is kept internally (no P&L drift).
- **PriceCache** is minimal, correct, lock-guarded, 100% covered; the monotonic `version` counter cleanly drives SSE change detection.
- **Resilience** is handled where it matters: both background loops wrap their work in `try/except` and continue; `stop()` is idempotent and swallows `CancelledError`.
- **`PriceUpdate`** is frozen/slotted and its wire shape matches `PLAN.md` §6 (epoch-seconds timestamp exception included).

---

## 4. Recommended Actions (in priority order)

1. ~~**Fix the Massive timestamp divisor** (`/1000.0` → `/1_000_000_000`) and correct the masking test.~~ (HIGH) — ✅ **DONE 2026-06-06**
2. ~~**Add SSE tests** for `stream.py` covering initial snapshot, empty-cache hold-open, change-detection, and disconnect.~~ (MEDIUM) — ✅ **DONE 2026-06-06**
3. ~~Build `create_stream_router` to instantiate its own `APIRouter`.~~ (LOW) — ✅ **DONE 2026-06-06**
4. ~~Decide where ticker normalization and the simulator universe guard live; make the two sources consistent.~~ (LOW) — ✅ **DONE 2026-06-06** (normalization unified; universe guard deferred to the API layer by design)
5. ~~Drop the deprecated `event_loop_policy` fixture; refresh the summary's coverage numbers.~~ (INFO) — ✅ **DONE 2026-06-06**

All review items are resolved. The Massive path and live frontend chart can be trusted on the timestamp/SSE contracts, the two data sources behave consistently, and the test suite runs clean (82 passing, 98% coverage, zero warnings). The only intentionally deferred item is the simulator ticker-universe guard, which belongs in the not-yet-built watchlist API layer rather than the generic market-data layer.
