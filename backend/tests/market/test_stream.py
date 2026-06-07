"""Tests for the SSE price-streaming endpoint (stream.py).

The logic lives in the `_generate_events` async generator. We drive it directly
with a real PriceCache and a fake Request, collecting frames in a background task
so we can assert both presence and *absence* of frames without cancelling the
generator mid-flight (it catches CancelledError and would exit). A short
`interval` keeps the tests fast.
"""

from __future__ import annotations

import asyncio
import json

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router

RETRY_FRAME = "retry: 1000\n\n"


class FakeRequest:
    """Minimal stand-in for a starlette Request."""

    def __init__(self) -> None:
        self.client = type("Client", (), {"host": "test"})()
        self._disconnected = False

    async def is_disconnected(self) -> bool:
        return self._disconnected


def parse_data(frame: str) -> dict:
    """Extract the JSON payload from a `data: {...}\\n\\n` SSE frame."""
    assert frame.startswith("data: ")
    return json.loads(frame[len("data: ") :].strip())


def data_frames(frames: list[str]) -> list[str]:
    return [f for f in frames if f.startswith("data:")]


async def _drain(gen, frames: list[str]) -> None:
    """Pull every frame from the generator until it ends (StopAsyncIteration)."""
    async for frame in gen:
        frames.append(frame)


# --- Generator logic ---


async def test_first_frame_is_retry():
    """The stream opens with a retry directive so EventSource auto-reconnects."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)

    assert await anext(gen) == RETRY_FRAME

    await gen.aclose()


async def test_initial_snapshot_on_connect():
    """First data frame is the full cache snapshot, in the documented wire shape."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.update("GOOGL", 175.0)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)

    await anext(gen)  # skip retry
    data = parse_data(await anext(gen))

    assert set(data) == {"AAPL", "GOOGL"}
    assert data["AAPL"]["price"] == 190.0
    assert data["AAPL"]["direction"] == "flat"
    # Flat dict keyed by ticker — no envelope, no version field on the wire.
    assert "version" not in data
    assert set(data["AAPL"]) == {
        "ticker",
        "price",
        "previous_price",
        "timestamp",
        "change",
        "change_percent",
        "direction",
    }

    await gen.aclose()


async def test_holds_open_until_first_tick():
    """An empty cache never emits `{}` — the stream waits for the first real tick."""
    cache = PriceCache()  # empty
    req = FakeRequest()
    gen = _generate_events(cache, req, interval=0.01)
    frames: list[str] = []
    task = asyncio.create_task(_drain(gen, frames))

    await asyncio.sleep(0.1)  # many intervals elapse while the cache is empty
    assert frames == [RETRY_FRAME]  # only the retry directive, no data frame

    cache.update("AAPL", 190.0)  # first tick
    await asyncio.sleep(0.1)

    sent = data_frames(frames)
    assert len(sent) == 1
    assert parse_data(sent[0])["AAPL"]["price"] == 190.0

    req._disconnected = True
    await asyncio.wait_for(task, timeout=1.0)


async def test_emits_only_on_version_change():
    """After the snapshot, a new frame appears only when the cache version changes."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    req = FakeRequest()
    gen = _generate_events(cache, req, interval=0.01)
    frames: list[str] = []
    task = asyncio.create_task(_drain(gen, frames))

    await asyncio.sleep(0.1)  # snapshot emitted; nothing changes after
    assert len(data_frames(frames)) == 1

    cache.update("AAPL", 191.0)  # version bumps
    await asyncio.sleep(0.1)

    sent = data_frames(frames)
    assert len(sent) == 2
    latest = parse_data(sent[-1])["AAPL"]
    assert latest["price"] == 191.0
    assert latest["direction"] == "up"

    req._disconnected = True
    await asyncio.wait_for(task, timeout=1.0)


async def test_disconnect_ends_stream():
    """Flipping is_disconnected stops the generator cleanly."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    req = FakeRequest()
    gen = _generate_events(cache, req, interval=0.01)
    frames: list[str] = []
    task = asyncio.create_task(_drain(gen, frames))

    await asyncio.sleep(0.05)
    req._disconnected = True

    # The drain task completing (no timeout) proves the generator ended.
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
    assert data_frames(frames)  # we did receive the initial snapshot first


# --- Router wiring ---


def _prices_route(router):
    """The most recently registered /api/stream/prices route.

    create_stream_router currently registers onto a module-global router, so
    repeated calls accumulate routes; take the last match to get this call's.
    """
    matches = [r for r in router.routes if getattr(r, "path", None) == "/api/stream/prices"]
    assert matches, "stream router did not register /api/stream/prices"
    return matches[-1]


def test_router_registers_prices_route():
    """create_stream_router exposes GET /api/stream/prices."""
    route = _prices_route(create_stream_router(PriceCache()))
    assert "GET" in route.methods


async def test_response_is_event_stream():
    """The endpoint returns an SSE response with no-cache / no-buffering headers."""
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    route = _prices_route(create_stream_router(cache))

    response = await route.endpoint(FakeRequest())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
