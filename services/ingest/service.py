"""services/ingest/service.py — market data ingestion service.

This is the FIRST concrete service in the rewrite. It replaces the inline
WebSocket loop in `crypto_options_bot/data/deribit_ws.py`.

Responsibilities:
  1. Subscribe to Deribit WebSocket (ticker, instrument, chart channels)
  2. Subscribe to Deribit REST for snapshot data (option chains, positions, margins)
  3. Normalize ticks into the typed TickEvent schema
  4. Publish to `market.<venue>.<instrument>.tick` subjects on the bus

Why a separate process:
  - Latency: WebSocket handling should never block strategy logic
  - Resilience: if ingest dies, strategies keep running on the last known state
  - Multi-venue: same service template for Binance / OKX / Coinbase perp feeds

Run:
  python -m services.ingest.service

Config (env):
  BUS_BACKEND: inproc | nats (default inproc)
  INGEST_SYMBOLS: BTC-PERP,ETH-PERP,BTC-30SEP26-*,ETH-30SEP26-*
  DERIBIT_WS_URL: wss://www.deribit.com/ws/api/v2 (prod) or test.deribit.com (testnet)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import websockets
from pydantic import ValidationError

from services.contracts.bus import make_bus
from services.contracts.events import (
    TickEvent, SpotTickEvent,
    subject_market_tick, subject_spot,
)

log = logging.getLogger(__name__)


class DeribitIngestService:
    """One process = one venue. Spawn multiple for multi-venue."""

    VENUE = "deribit"

    def __init__(self):
        self.bus = make_bus()
        self.ws_url = os.environ.get(
            "DERIBIT_WS_URL", "wss://www.deribit.com/ws/api/v2"
        )
        # Whitelist of instruments to subscribe. Default: top BTC + ETH perps.
        self.instruments = self._parse_instruments(
            os.environ.get("INGEST_SYMBOLS", "BTC-PERP,ETH-PERP")
        )
        self._ws = None
        self._sub_id = 0
        self._running = False
        self._stats = {
            "ticks_published": 0,
            "ws_messages_received": 0,
            "ws_reconnects": 0,
            "errors": 0,
        }

    def _parse_instruments(self, raw):
        return [s.strip() for s in raw.split(",") if s.strip()]

    async def start(self):
        await self.bus.start()
        self._running = True
        log.info(f"[ingest:deribit] starting (instruments={self.instruments})")
        while self._running:
            try:
                await self._ws_loop()
            except Exception as e:
                self._stats["ws_reconnects"] += 1
                log.warning(f"[ingest:deribit] WS error: {e}; reconnecting in 5s")
                await asyncio.sleep(5.0)

    async def stop(self):
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        await self.bus.stop()

    async def _ws_loop(self):
        async with websockets.connect(self.ws_url, ping_interval=30) as ws:
            self._ws = ws
            for inst in self.instruments:
                channel = f"ticker.{inst}.100ms"
                await self._subscribe(channel)
            for ccy in ("BTC", "ETH"):
                await self._subscribe(f"deribit_price_index.{ccy.lower()}_usd")

            async for raw in ws:
                self._stats["ws_messages_received"] += 1
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._handle_message(msg)

    async def _subscribe(self, channel):
        self._sub_id += 1
        msg = {
            "jsonrpc": "2.0",
            "id": self._sub_id,
            "method": "public/subscribe",
            "params": {"channels": [channel]},
        }
        await self._ws.send(json.dumps(msg))

    async def _handle_message(self, msg):
        if "result" in msg and "id" in msg:
            return
        method = msg.get("method", "")
        if not method.startswith("subscription"):
            return
        params = msg.get("params", {})
        data = params.get("data", {})
        channel = params.get("channel", "")

        try:
            if channel.startswith("ticker."):
                await self._emit_ticker(channel, data)
            elif channel.startswith("deribit_price_index."):
                await self._emit_spot(channel, data)
        except ValidationError as e:
            log.warning(f"[ingest:deribit] schema validation failed: {e}")
            self._stats["errors"] += 1
        except Exception as e:
            log.exception(f"[ingest:deribit] emit error: {e}")
            self._stats["errors"] += 1

    async def _emit_ticker(self, channel, data):
        parts = channel.split(".")
        instrument = parts[1] if len(parts) >= 2 else ""

        best_bid = float(data.get("best_bid_price") or 0)
        best_ask = float(data.get("best_ask_price") or 0)
        mark_price = float(data.get("mark_price") or 0)
        mark_iv = float(data.get("mark_iv") or 0)
        last_price = float(data.get("last_price") or 0)

        kind, underlying, strike, opt_type, expiry = self._parse_instrument(instrument)

        ltp = last_price if last_price else mark_price
        if not ltp:
            return

        event = TickEvent(
            ts_event_ms=int(time.time() * 1000),
            source="ingest:deribit",
            venue=self.VENUE,
            instrument=instrument,
            underlying=underlying,
            kind=kind,
            bid=best_bid,
            ask=best_ask,
            ltp=ltp,
            mark=mark_price,
            iv=mark_iv,
            strike=strike,
            option_type=opt_type,
            expiry=expiry,
        )
        await self.bus.publish(subject_market_tick(self.VENUE, instrument), event)
        self._stats["ticks_published"] += 1

    async def _emit_spot(self, channel, data):
        ccy = channel.split(".")[1].split("_")[0].upper()
        ltp = float(data.get("price") or 0)
        if not ltp:
            return
        event = SpotTickEvent(
            ts_event_ms=int(time.time() * 1000),
            source="ingest:deribit",
            venue=self.VENUE,
            underlying=ccy,
            ltp=ltp,
        )
        await self.bus.publish(subject_spot(ccy), event)

    def _parse_instrument(self, instrument):
        underlying = instrument.split("-")[0] if instrument else ""
        strike = 0.0
        opt_type = None
        expiry = None

        if instrument.endswith("-PERP"):
            return ("perp", underlying, 0.0, None, None)

        parts = instrument.split("-")
        if len(parts) >= 3:
            try:
                expiry = parts[1]
                strike = float(parts[2])
                if len(parts) >= 4:
                    opt_type = parts[3]
            except (ValueError, IndexError):
                pass
            return ("option", underlying, strike, opt_type, expiry)

        return ("future", underlying, 0.0, None, None)

    def stats(self):
        return dict(self._stats)


async def _main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    svc = DeribitIngestService()
    try:
        await svc.start()
    except KeyboardInterrupt:
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(_main())
