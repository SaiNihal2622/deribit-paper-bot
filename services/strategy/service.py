"""services/strategy/service.py — strategy runner service.

This is the third service. It subscribes to FeaturesEvents, runs each registered
strategy, and emits StrategyPlan events to the bus.

Key design:
  - Each strategy gets its OWN subscription queue group (load-balances signals
    across A/B variants when there are multiple replicas)
  - Strategies are imported as Python modules from a directory; adding a new
    strategy = dropping a file in `services/strategy/strategies/`
  - The runner is fault-tolerant: a buggy strategy doesn't kill the others
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

from services.contracts.bus import make_bus
from services.contracts.events import (
    FeaturesEvent, StrategyPlan, BaseEvent,
    subject_strategy_plan, subject_features,
)

log = logging.getLogger(__name__)


class StrategyService:
    """Loads strategy classes from a directory and runs them per FeaturesEvent."""

    def __init__(self, strategies_dir: str = None):
        self.bus = make_bus()
        self.strategies_dir = Path(
            strategies_dir or os.environ.get(
                "STRATEGIES_DIR",
                str(Path(__file__).parent / "strategies"),
            )
        )
        self.strategies: list = []  # list of strategy instances
        self._running = False
        self._stats = {
            "events_processed": 0,
            "plans_emitted": 0,
            "errors": 0,
        }

    async def start(self):
        await self.bus.start()
        # Load strategies from directory
        self._load_strategies()
        log.info(f"[strategy] loaded {len(self.strategies)} strategies from {self.strategies_dir}")

        # Subscribe to FeaturesEvent for BTC and ETH
        for underlying in ("BTC", "ETH"):
            await self.bus.subscribe(
                subject_features(underlying),
                f"strategy-{underlying}",
                lambda e, u=underlying: self._on_features(e, u),
            )

        self._running = True

    async def stop(self):
        self._running = False
        await self.bus.stop()

    def _load_strategies(self):
        """Import every .py file in strategies_dir; pick classes implementing
        a strategy interface (eligible + plan methods)."""
        if not self.strategies_dir.exists():
            log.warning(f"[strategy] dir not found: {self.strategies_dir}")
            return
        sys.path.insert(0, str(self.strategies_dir.parent))
        for f in sorted(self.strategies_dir.glob("*.py")):
            if f.name.startswith("_") or f.name == "__init__.py":
                continue
            mod_name = f"strategies_{f.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, f)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # Find classes with `plan` and `eligible` methods
                for name in dir(mod):
                    obj = getattr(mod, name)
                    if (inspect.isclass(obj)
                            and hasattr(obj, "plan")
                            and hasattr(obj, "eligible")
                            and obj.__module__ == mod_name):
                        self.strategies.append(obj())
                        log.info(f"[strategy] loaded {name} from {f.name}")
            except Exception:
                log.exception(f"[strategy] failed to load {f.name}")

    async def _on_features(self, event: FeaturesEvent, underlying: str):
        if event.underlying != underlying:
            return
        self._stats["events_processed"] += 1
        for strat in list(self.strategies):
            try:
                # Call the strategy synchronously
                eligible, _reason = strat.eligible(event)
                if not eligible:
                    continue
                plan = strat.plan(event)
                if plan is None:
                    continue
                # Convert to bus event
                strat_id = getattr(strat, "STRATEGY_ID", strat.__class__.__name__)
                strat_version = getattr(strat, "STRATEGY_VERSION", "1.0")
                hypothesis = getattr(strat, "HYPOTHESIS", "")
                bus_event = StrategyPlan(
                    ts_event_ms=event.ts_event_ms,
                    source="strategy",
                    strategy_id=strat_id,
                    strategy_version=strat_version,
                    underlying=event.underlying,
                    legs=plan.get("legs", []),
                    target=float(plan.get("target", 0)),
                    stop=float(plan.get("stop", 0)),
                    confidence=float(plan.get("confidence", 0.5)),
                    reason=plan.get("reason", ""),
                    hypothesis=hypothesis,
                    expected_hold_minutes=int(plan.get("expected_hold_minutes", 60)),
                )
                await self.bus.publish(
                    subject_strategy_plan(strat_id),
                    bus_event,
                )
                self._stats["plans_emitted"] += 1
            except Exception:
                self._stats["errors"] += 1
                log.exception(f"[strategy] {strat.__class__.__name__} crashed")

    def stats(self) -> dict:
        return dict(self._stats)


async def _main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    svc = StrategyService()
    try:
        await svc.start()
        while True:
            await asyncio.sleep(60)
    except KeyboardInterrupt:
        await svc.stop()


if __name__ == "__main__":
    asyncio.run(_main())
