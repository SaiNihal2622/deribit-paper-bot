"""test_services_contracts.py — verify the event schemas + in-process bus."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.contracts.events import (
    TickEvent, SpotTickEvent, FeaturesEvent, StrategyPlan,
    RiskDecision, OrderPlaced, FillEvent, AuditEvent,
    subject_market_tick, subject_features, SCHEMA_VERSION,
)
from services.contracts.bus import InProcessBus

pytestmark = pytest.mark.asyncio


def test_tick_event_serializes_to_json():
    """TickEvent should round-trip through JSON without losing data."""
    e = TickEvent(
        ts_event_ms=1234567890000,
        source="ingest:deribit",
        venue="deribit",
        instrument="BTC-PERP",
        underlying="BTC",
        kind="perp",
        bid=83000.0, ask=83001.0, ltp=83000.5, mark=83000.0, iv=0.0,
    )
    raw = e.model_dump_json()
    e2 = TickEvent.model_validate_json(raw)
    assert e2.instrument == e.instrument
    assert e2.bid == e.bid
    assert e2.ts_event_ms == e.ts_event_ms


def test_schema_version_present():
    """Every event has a schema_version field so consumers can detect breaking changes."""
    e = TickEvent(
        ts_event_ms=0, source="test", venue="deribit",
        instrument="BTC-PERP", underlying="BTC", kind="perp",
    )
    assert e.schema_version == SCHEMA_VERSION


def test_strategy_plan_requires_legs_and_qty():
    """StrategyPlan.legs is a list of dicts — qty handling is the order_manager's job."""
    p = StrategyPlan(
        ts_event_ms=0, source="strategy",
        strategy_id="short_strangle:monthly_tight",
        strategy_version="1.0",
        underlying="BTC",
        legs=[{"side": "SELL", "qty": 1, "instrument": "BTC-29SEP26-90000-C", "price": 0.05}],
        target=0.028, stop=0.08, confidence=0.5,
    )
    assert len(p.legs) == 1


@pytest.mark.asyncio
async def test_inproc_bus_publish_subscribe():
    """InProcessBus: 1 publisher, 1 subscriber, 1 event round-trip."""
    bus = InProcessBus()
    await bus.start()
    try:
        received = []
        async def handler(event):
            received.append(event)
        await bus.subscribe("market.deribit.BTC-PERP.tick", "test-group", handler)
        tick = TickEvent(
            ts_event_ms=0, source="test", venue="deribit",
            instrument="BTC-PERP", underlying="BTC", kind="perp",
            ltp=83000.0,
        )
        await bus.publish("market.deribit.BTC-PERP.tick", tick)
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].ltp == 83000.0
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_inproc_bus_wildcard_subscription():
    """Wildcard `*` matches any single subject segment."""
    bus = InProcessBus()
    await bus.start()
    try:
        btc_received = []
        eth_received = []
        await bus.subscribe(
            "market.*.BTC-PERP.tick", "btc-group",
            lambda e: btc_received.append(e)
        )
        await bus.subscribe(
            "market.*.ETH-PERP.tick", "eth-group",
            lambda e: eth_received.append(e)
        )
        for inst in ("BTC-PERP", "ETH-PERP", "BTC-29SEP26-100000-C"):
            await bus.publish(
                f"market.deribit.{inst}.tick",
                TickEvent(
                    ts_event_ms=0, source="test", venue="deribit",
                    instrument=inst, underlying=inst.split("-")[0],
                    kind="perp", ltp=100.0,
                ),
            )
        await asyncio.sleep(0.05)
        assert len(btc_received) == 1
        assert len(eth_received) == 1
        assert btc_received[0].instrument == "BTC-PERP"
        assert eth_received[0].instrument == "ETH-PERP"
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_inproc_bus_stamps_bus_timestamp():
    """ts_bus_ms is set on publish, even if the producer left it at 0."""
    bus = InProcessBus()
    await bus.start()
    try:
        received = []
        async def handler(event):
            received.append(event)
        await bus.subscribe("test.subj", "g", handler)
        e = TickEvent(
            ts_event_ms=0, source="t", venue="deribit",
            instrument="BTC-PERP", underlying="BTC", kind="perp", ltp=1.0,
        )
        # Force the stamp by publishing
        assert e.ts_bus_ms == 0
        await bus.publish("test.subj", e)
        await asyncio.sleep(0.05)
        assert received[0].ts_bus_ms > 0
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_inproc_bus_replay():
    """Replay returns events from the ring buffer within time range."""
    bus = InProcessBus()
    await bus.start()
    try:
        # Publish a few events
        for i in range(5):
            await bus.publish(
                "test.replay",
                TickEvent(
                    ts_event_ms=1000 + i * 100, source="t", venue="deribit",
                    instrument="BTC-PERP", underlying="BTC", kind="perp",
                    ltp=float(i),
                ),
            )
        await asyncio.sleep(0.05)
        # Replay all events with ts >= 1200
        events = [e async for e in bus.replay("test.replay", since_ms=1200)]
        assert len(events) == 3
        assert events[0].ltp == 2.0
    finally:
        await bus.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
