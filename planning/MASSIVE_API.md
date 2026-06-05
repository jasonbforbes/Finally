# Massive API Reference (formerly Polygon.io)

Research reference for the Massive market-data REST/WebSocket API as used by FinAlly.
Covers the endpoints, the official `massive` Python client, response shapes, timestamp
units, rate limits, and error handling. FinAlly uses **REST polling of the multi-ticker
snapshot endpoint** (per PLAN.md §6); WebSocket is documented here as an alternative.

## 1. Background

- Polygon.io rebranded to **Massive** on 2025-10-30. Same data, same accounts, same keys.
- Official Python client: `massive-com/client-python`, published as the **`massive`** package
  (the successor to `polygon-api-client`). Min Python **3.9+**; FinAlly targets 3.12.
- Base URL: `https://api.massive.com`. The legacy `https://api.polygon.io` still resolves
  for an extended deprecation window, and existing API keys keep working unchanged.
- WebSocket cluster (stocks): `wss://socket.massive.com/stocks`.

### Install

```bash
uv add massive          # never pip install
```

### Auth

The key is sent as `Authorization: Bearer <API_KEY>`; the client handles this. FinAlly reads
it from `MASSIVE_API_KEY` (see PLAN.md §5). If the variable is empty the simulator is used
instead and the client is never constructed.

```python
from massive import RESTClient

client = RESTClient(api_key="<API_KEY>")   # explicit
client = RESTClient()                       # or read MASSIVE_API_KEY from env
```

The `RESTClient` is **synchronous** (blocking HTTP). In FinAlly's async backend it must be
called inside `asyncio.to_thread(...)` so it never blocks the event loop — see
`MARKET_INTERFACE.md` and `backend/app/market/massive_client.py`.

## 2. Rate Limits

| Tier | Limit | FinAlly poll interval |
|---|---|---|
| Free / Basic | 5 requests / minute | 15s (default) |
| Paid (Starter and up) | effectively unbounded; keep < ~100 req/s | 2–5s |

The whole point of the multi-ticker snapshot endpoint is that **all watched tickers cost one
request per poll**, so even the free tier comfortably refreshes the full watchlist every 15s.

## 3. Primary Endpoint — Full Market Snapshot (v2)

This is the endpoint FinAlly polls. One call returns the latest trade, latest quote, current
day bar, previous day bar, and most-recent minute bar for every requested ticker.

**REST**

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

Query params:
- `tickers` — comma-separated, **case-sensitive** list (omit to get the entire market).
- `include_otc` — boolean, default `false`.

**Python client**

```python
from massive import RESTClient

client = RESTClient(api_key="<API_KEY>")

# Pass market as the first positional arg ("stocks") and the ticker list second.
snapshots = client.get_snapshot_all("stocks", ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"])

for snap in snapshots:                      # each is a TickerSnapshot
    print(snap.ticker, snap.last_trade.price)
    print("  day change %:", snap.todays_change_percent)
    print("  prev close :", snap.prev_day.close)
```

> The `market_type` may also be passed via the `massive.rest.models.SnapshotMarketType`
> enum (`SnapshotMarketType.STOCKS`), which is what `massive_client.py` currently uses. The
> bare string `"stocks"` is equivalent.

### Response JSON

```json
{
  "status": "OK",
  "count": 5,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": -4.54,
      "todaysChangePerc": -3.50,
      "updated": 1675190399999999999,
      "day":     { "o": 129.61, "h": 130.15, "l": 125.07, "c": 125.07, "v": 111237700, "vw": 127.35 },
      "prevDay": { "o": 130.46, "h": 133.51, "l": 129.89, "c": 129.61, "v":  77633704, "vw": 131.61 },
      "min":     { "o": 125.12, "h": 125.22, "l": 125.02, "c": 125.07, "v": 12345, "vw": 125.1, "t": 1675190340000, "n": 102 },
      "lastTrade": { "p": 125.07, "s": 100, "t": 1675190399999999999, "x": 4, "i": "12345", "c": [14, 41] },
      "lastQuote": { "p": 125.06, "s": 5, "P": 125.08, "S": 10, "t": 1675190399999000000 }
    }
  ]
}
```

### Field map: JSON ↔ Python model (`TickerSnapshot`)

| JSON | Model attribute | Meaning |
|---|---|---|
| `ticker` | `snap.ticker` | Symbol |
| `lastTrade.p` | `snap.last_trade.price` | **Current price** (what we cache) |
| `lastTrade.s` | `snap.last_trade.size` | Trade size |
| `lastTrade.t` | `snap.last_trade.timestamp` | SIP timestamp, **nanoseconds** |
| `lastQuote.p` / `.P` | `snap.last_quote.bid_price` / `ask_price` | NBBO bid / ask |
| `day.o/h/l/c/v/vw` | `snap.day.open/high/low/close/volume/vwap` | Today's bar |
| `prevDay.c` | `snap.prev_day.close` | Previous close (day-change base) |
| `min.*` | `snap.min.*` | Latest minute bar (`min.t` is **milliseconds**) |
| `todaysChange` | `snap.todays_change` | Absolute day change |
| `todaysChangePerc` | `snap.todays_change_percent` | Percent day change |
| `updated` | `snap.updated` | Last update, **nanoseconds** |

The two fields FinAlly extracts each poll are `snap.last_trade.price` (the live price) and
`snap.last_trade.timestamp` (when it traded). `snap.prev_day.close` /
`snap.todays_change_percent` are available if a day-change display is wanted.

> **Correction vs. the archived draft:** percent day change is `snap.todays_change_percent`
> at the snapshot root — there is **no** `snap.day.change_percent`. The `day` object is a
> plain OHLCV bar.

## 4. Timestamp Units — read this carefully

Massive is **not** uniform about timestamp units, and this stream feeds a cache that stores
**epoch seconds** (`time.time()`-style float; see PLAN.md §6 and `PriceCache`).

| Source field | Unit | Convert to epoch seconds |
|---|---|---|
| `lastTrade.t`, `lastQuote.t`, `updated` (v2 snapshot) | nanoseconds | `t / 1_000_000_000` |
| `min.t` (minute bar) | milliseconds | `t / 1_000` |
| v3 unified `*.sip_timestamp`, `last_updated` | nanoseconds | `t / 1_000_000_000` |
| Aggregate bar `t` (`/v2/aggs/...`) | milliseconds | `t / 1_000` |

```python
# Correct conversion for the snapshot last-trade timestamp:
epoch_seconds = snap.last_trade.timestamp / 1_000_000_000
```

> **Caution:** `backend/app/market/massive_client.py` currently divides the snapshot trade
> timestamp by `1000.0`, treating it as milliseconds. The v2 snapshot trade timestamp is
> **nanoseconds**, so the correct divisor is `1e9`. The cached price is still correct (only
> the timestamp is affected), and the price-SSE consumer mostly cares about price + ordering,
> but new code valuing freshness should divide by `1e9`. Flagged here for the implementer.

## 5. Newer Endpoint — Unified Snapshot (v3)

A cross-asset replacement for the v2 snapshot, with snake_case JSON and explicit session
metrics. Useful if FinAlly later wants pre/post-market changes in one place.

**REST**

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=250
```

Query params (most relevant):
- `ticker.any_of` — comma-separated, **max 250 tickers** per call.
- `type` — `stocks | options | fx | crypto | indices`.
- `ticker.gt/gte/lt/lte`, `order`, `sort`, `limit` (default 10, max 250).

**Python client**

```python
for snap in client.list_universal_snapshots(type="stocks",
                                             ticker_any_of=["AAPL", "GOOGL", "MSFT"]):
    print(snap.ticker, snap.last_trade.price, snap.session.change_percent)
```

Per-result fields (`UniversalSnapshot`): `ticker`, `type`, `market_status`, `name`,
`session` (`price, change, change_percent, early/regular/late_trading_change*, open, high,
low, close, previous_close, volume, vwap, last_updated`), `last_trade` (`price, size,
exchange, conditions, sip_timestamp, participant_timestamp, id`), `last_quote` (`bid, ask,
bid_size, ask_size, bid_exchange, ask_exchange, midpoint, last_updated`), `last_minute`,
plus `error`/`message` per ticker when a symbol is bad. **All timestamps nanoseconds.**

For FinAlly's 10–30 ticker watchlist the v2 snapshot is simpler and sufficient; v3 matters
only if the watchlist ever exceeds what a single v2 call comfortably returns or if
session-level pre/post-market changes are needed.

## 6. Secondary Endpoints

### Single-ticker snapshot — detail view

```python
snap = client.get_snapshot_ticker("stocks", "AAPL")
print(snap.last_trade.price, snap.last_quote.bid_price, snap.last_quote.ask_price)
print(snap.day.low, snap.day.high)
```

### Previous close — realistic seed prices

```
GET /v2/aggs/ticker/{ticker}/prev
```

```python
for agg in client.get_previous_close_agg("AAPL"):
    print(agg.close, agg.open, agg.high, agg.low, agg.volume)  # Agg: o/h/l/c/v/vw/timestamp
```

Handy at startup to seed the cache (or the simulator) from real prior-session closes.

### Aggregates (historical bars) — main chart history

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
```

```python
bars = []
for a in client.list_aggs("AAPL", 1, "day", "2026-01-01", "2026-01-31", limit=50000):
    bars.append(a)   # a.open, a.high, a.low, a.close, a.volume, a.vwap, a.timestamp (ms)
```

`list_aggs` auto-paginates. `timespan` ∈ `minute, hour, day, week, month, quarter, year`.
Not needed for live polling; useful if the main chart wants real history rather than
SSE-accumulated points.

### Last trade / last quote

```python
trade = client.get_last_trade("AAPL")   # trade.price, trade.size, trade.sip_timestamp (ns)
quote = client.get_last_quote("AAPL")   # quote.bid_price, quote.ask_price, quote.sip_timestamp (ns)
```

## 7. Real-Time via WebSocket (optional alternative to polling)

PLAN.md §6 deliberately chose REST polling ("simpler, works on all tiers"). WebSocket gives
true push but **requires a paid real-time plan** and adds reconnect/auth complexity. Recorded
here for completeness.

```python
from massive import WebSocketClient
from massive.websocket.models import WebSocketMessage

ws = WebSocketClient(api_key="<API_KEY>", subscriptions=["T.AAPL", "T.GOOGL"])

def handle(msgs: list[WebSocketMessage]) -> None:
    for m in msgs:
        # EquityTrade: m.symbol, m.price, m.size, m.timestamp (ms), m.exchange, m.conditions
        print(m.symbol, m.price)

ws.run(handle_msg=handle)
```

Subscription prefixes (stocks cluster):

| Prefix | Channel | Example |
|---|---|---|
| `T` | Per-trade | `T.AAPL`, `T.*` (all) |
| `Q` | NBBO quotes | `Q.AAPL` |
| `A` | Per-second aggregate bar | `A.AAPL` |
| `AM` | Per-minute aggregate bar | `AM.AAPL` |

For FinAlly's "watch the whole watchlist" use case, `T.<TICKER>` for each ticker is the
analogue of one snapshot poll. To match Massive mode into the existing `PriceCache`, the
handler would call `cache.update(ticker=m.symbol, price=m.price, timestamp=m.timestamp/1000)`.

## 8. Error Handling

The synchronous client raises on HTTP errors:

| Status | Cause | FinAlly response |
|---|---|---|
| 401 | Bad / missing API key | Log; poller keeps retrying (no crash) |
| 403 | Plan lacks the endpoint/asset class | Log; treat as fatal config error |
| 429 | Rate limit (free tier: 5/min) | Back off to the configured poll interval |
| 5xx | Upstream error | Client retries up to 3× internally; loop retries next cycle |

FinAlly's poller wraps each cycle in a broad `try/except` that **logs and continues** — one
failed poll never kills the background task, and the cache simply serves the last good
prices until the next successful cycle (see `massive_client.py::_poll_once`).

## 9. Behavior Notes

- The snapshot returns **all requested tickers in one call** — the key to free-tier viability.
- Outside market hours, `lastTrade.price` is the last print (may include extended-hours).
- The `day` bar resets at the open; pre-market reads can reflect the prior session until the
  first regular-hours trade.
- Unknown/invalid tickers are simply absent from the v2 `tickers` array (v3 returns a
  per-ticker `error`/`message`). FinAlly validates ticker existence at watchlist-add time.

## Sources

- [Polygon.io is Now Massive](https://massive.com/blog/polygon-is-now-massive)
- [massive-com/client-python (GitHub)](https://github.com/massive-com/client-python)
- [Stocks REST API — Overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot (v2)](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Unified Snapshot (v3)](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Massive + Python blog](https://massive.com/blog/polygon-io-with-python-for-stock-market-data)
