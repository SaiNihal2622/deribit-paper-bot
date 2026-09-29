"""services/feature_engine/service.py — feature computation engine.

This is the second service in the rewrite. It subscribes to raw TickEvents from
the ingest service, computes features (DVOL, IV-rank, momentum, regime, microstructure),
and publishes FeaturesEvents that strategies consume.

Responsibilities:
  1. Subscribe to `market.<venue>.<instrument>.tick` (all instruments)
  2. Subscribe to `market.spot.<underlying>` (spot index)
  3. Maintain rolling buffers per instrument for momentum / regime calc
  4. Compute features on a cadence (default 5s for spot, per-tick for options)
  5. Publish FeaturesEvent to `signal.features.<underlying>`

Tech: Python + asyncio + numpy + river (online ML). This is NOT on the hot path —
features are 100Hz max, well within Python's capability.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from typing import Optional

import numpy as np

from services.contracts.bus import make_bus
from services.contracts.events import (
    TickEvent, SpotTickEvent, FeaturesEvent, BaseEvent,
    subject_features, subject_spot,
)

log = logging.getLogger(__name__)


class FeatureEngine:
    """Computes features and publishes them."""

    def __init__(self):
        self.bus = make_bus()
        # Per-underlying rolling buffers for momentum / regime
        self._prices: dict[str, collections.deque[float]] = {}
        self._ivs: dict[str, collections.deque[float]] = {}
        # Per-instrument latest tick cache
        self._latest_ticks: dict[str, TickEvent] = {}
        # Latest spot per underlying
        self._spot: dict[str, float] = {}
        self._running = False
        self._stats = {
            "ticks_processed": 0,
            "feature_publishes": 0,
        }
        # Cadence: how often to publish FeaturesEvent per underlying
        self._feature_interval_sec = float(os.environ.get("FEATURE_INTERVAL_SEC", "5.0"))

    def _ensure_buffer(self, key: str, maxlen: int = 200):
        if key not in self._prices:
            self._prices[key] = collections.deque(maxlen=maxlen)
        if key not in self._ivs:
            self._ivs[key] = collections.deque(maxlen=maxlen)

    async def start(self):
        await self.bus.start()
        self._running = True
        log.info("[features] starting")
        # Subscribe to ALL market ticks (Deribit for now)
        await self.bus.subscribe("market.deribit.*.*.tick", "feature-engine",
                                  self._on_tick)
        # Also subscribe to spot index
        await self.bus.subscribe("market.spot.*", "feature-engine",
                                  self._on_spot)
        # Run feature-publish loop
        asyncio.create_task(self._feature_publish_loop())

    async def stop(self):
        self._running = False
        await self.bus.stop()

    async def _on_tick(self, event):
        self._stats["ticks_processed"] += 1
        # Cache latest tick
        self._latest_ticks[event.instrument] = event
        # Update rolling buffers for spot-tracking instruments (perps + spot)
        if event.kind in ("perp", "future"):
            self._ensure_buffer(event.instrument)
            self._prices[event.instrument].append(event.ltp)
        # Update IV history for options
        if event.kind == "option" and event.iv > 0:
            self._ensure_buffer(event.instrument)
            self._ivs[event.instrument].append(event.iv)
        # Update underlying spot (best-effort: from perp ltp)
        if event.underlying:
            self._ensure_buffer(event.underlying)
            self._prices[event.underlying].append(event.ltp)

    async def _on_spot(self, event):
        self._spot[event.underlying] = event.ltp
        self._ensure_buffer(event.underlying)
        self._prices[event.underlying].append(event.ltp)

    async def _feature_publish_loop(self):
        """Publish FeaturesEvent per underlying every feature_interval_sec."""
        while self._running:
            try:
                for underlying in ("BTC", "ETH"):
                    features = self._compute_features(underlying)
                    if features is None:
                        continue
                    await self.bus.publish(
                        subject_features(underlying),
                        features,
                    )
                    self._stats["feature_publishes"] += 1
            except Exception:
                log.exception("[features] publish loop error")
            await asyncio.sleep(self._feature_interval_sec)

    def _compute_features(self, underlying: str) -> Optional[FeaturesEvent]:
        # Need at least 5 data points
        buf = self._prices.get(underlying)
        if buf is None or len(buf) < 5:
            return None
        prices = np.array(buf)
        spot = float(self._spot.get(underlying) or prices[-1])

        # Momentum: fractional change over recent window
        mom_20 = 0.0
        if len(prices) >= 20:
            mom_20 = float((prices[-1] - prices[-20]) / prices[-20])
        elif len(prices) >= 5:
            mom_20 = float((prices[-1] - prices[0]) / prices[0])

        # 100-tick momentum if available
        mom_100 = 0.0
        if len(prices) >= 100:
            mom_100 = float((prices[-1] - prices[-100]) / prices[-100])

        # Volatility proxy: stdev of recent returns, annualized
        rets = np.diff(prices) / prices[:-1]
        realized_vol = float(rets.std() * np.sqrt(252 * 24 * 60)) if len(rets) > 2 else 0.0

        # IV-rank: percentile of current realized_vol in 100-period history
        iv_history = np.array(self._ivs.get(f"{underlying}-PERP", self._ivs.get(underlying, [])))
        if len(iv_history) > 10:
            current_iv_proxy = float(iv_history[-1])
            iv_rank = float((current_iv_proxy - iv_history.min()) /
                          max(iv_history.max() - iv_history.min(), 1e-6) * 100)
        else:
            current_iv_proxy = realized_vol * 100  # use realized vol as IV proxy
            iv_rank = 50.0

        # Regime classification
        abs_mom = abs(mom_20)
        if abs_mom > 0.005:  # > 0.5% momentum
            regime = "trending"
        elif realized_vol > 0.80:  # > 80% annualised vol
            regime = "volatile"
        else:
            regime = "range"

        # ADX proxy: derived from momentum strength
        adx = float(min(100.0, abs_mom * 5000 + 15))

        # Trend strength: signed momentum magnitude
        trend_strength = float(np.clip(mom_20 * 50, -1.0, 1.0))

        # Microstructure (limited without full order book; defaults to 0)
        bid_ask_spread_pct = 0.0
        order_book_imbalance = 0.0
        flow_toxicity = 0.0

        return FeaturesEvent(
            ts_event_ms=int(time.time() * 1000),
            source="feature-engine",
            underlying=underlying,
            spot=spot,
            dvol=current_iv_proxy,
            iv_rank=iv_rank,
            momentum_20=mom_20,
            momentum_100=mom_100,
            regime=regime,
            adx=adx,
            trend_strength=trend_strength,
            bid_ask_spread_pct=bid_ask_spread_pct,
            order_book_imbalance=order_book_imbalance,
            flow_toxicity=flow_toxicity,
        )

    def stats(self) -> dict:
        return dict(self._stats)


async def _main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    eng = FeatureEngine()
    try:
        await eng.start()
        # Run forever
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        await eng.stop()


if __name__ == "__main__":
    asyncio.run(_main())
