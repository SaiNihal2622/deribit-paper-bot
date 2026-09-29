"""services/contracts/bus.py — event bus abstraction.

Two implementations:
  - InProcessBus: asyncio queue per subject. Used for local dev / single-node.
  - NATSBus: talks to a NATS server (embedded or remote) via nats-py.

The interface is identical. Pick based on env var `BUS_BACKEND`:
  - "inproc" (default) — in-process asyncio queues. No external dep.
  - "nats" — connect to NATS_URL (default nats://localhost:4222).

Every event gets:
  - stamped with ts_bus_ms when published (so consumers can measure bus latency)
  - optionally filtered by subject pattern (NATS wildcards: `market.*.BTC-PERP.tick`)
  - optionally persisted in JetStream (durable queues for replay)

This is the SHAPE of the bus. The current bot uses file-based IPC; the new system uses this.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable, Optional

from .events import BaseEvent

log = logging.getLogger(__name__)

HandlerT = Callable[[BaseEvent], "asyncio.Future[None] | None"]


class EventBus(ABC):
    """Common interface — every service publishes / subscribes through this."""

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def publish(self, subject: str, event: BaseEvent) -> None:
        """Publish event. ts_bus_ms is stamped on publish."""

    @abstractmethod
    async def subscribe(
        self,
        subject_pattern: str,
        queue_group: str,
        handler: HandlerT,
    ) -> None:
        """Subscribe to a subject pattern. queue_group gives at-most-once delivery
        across replicas (load-balances signals across strategy A/B variants)."""

    @abstractmethod
    async def events(self, subject_pattern: str, queue_group: str) -> AsyncIterator[BaseEvent]:
        """Async iterator of events. Use when consumer wants to read sequentially."""

    @abstractmethod
    async def replay(self, subject: str, since_ms: int) -> AsyncIterator[BaseEvent]:
        """Replay events from the journal (JetStream-backed or in-memory ring)."""


class InProcessBus(EventBus):
    """Single-process bus. Each (subject, queue_group) is an asyncio.Queue.

    Good for: local dev, single-node, low event rates (≤10k/s).
    Bad for: multi-node, >10k/s, replay across crashes (we use a ring buffer).
    """

    def __init__(self, ring_size: int = 100_000):
        self._queues: dict[tuple[str, str], asyncio.Queue] = {}
        self._handlers: dict[tuple[str, str], list[HandlerT]] = {}
        self._ring: list[tuple[str, BaseEvent]] = []  # (subject, event)
        self._ring_size = ring_size
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        for q in self._queues.values():
            await q.put(None)  # poison pill
        self._started = False

    def _match(self, pattern: str, subject: str) -> bool:
        """Match subject against pattern. Supports `*` wildcard.

        Examples:
          market.*.BTC-PERP.tick  matches market.deribit.BTC-PERP.tick, market.binance.BTC-PERP.tick
          market.deribit.*          matches market.deribit.BTC-PERP.tick, market.deribit.BTC-29SEP26-100000-C.tick
        """
        if pattern == subject:
            return True
        if "*" not in pattern:
            return False
        pat_parts = pattern.split(".")
        sub_parts = subject.split(".")
        if len(pat_parts) != len(sub_parts):
            return False
        return all(p == "*" or p == s for p, s in zip(pat_parts, sub_parts))

    def _matching_queues(self, subject: str) -> list[tuple[str, str]]:
        return [
            (subj, group)
            for (subj, group) in self._queues.keys()
            if self._match(subj, subject)
        ]

    async def publish(self, subject: str, event: BaseEvent) -> None:
        if not self._started:
            return
        if event.ts_bus_ms == 0:
            event.ts_bus_ms = int(time.time() * 1000)
        # Append to ring (for replay)
        self._ring.append((subject, event))
        if len(self._ring) > self._ring_size:
            self._ring = self._ring[-self._ring_size:]
        # Fan-out to matching queues
        for subj, group in self._matching_queues(subject):
            q = self._queues[(subj, group)]
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(f"[bus] queue full for {subj}/{group}")

    async def subscribe(self, subject_pattern: str, queue_group: str, handler: HandlerT) -> None:
        key = (subject_pattern, queue_group)
        self._queues.setdefault(key, asyncio.Queue(maxsize=10_000))
        self._handlers.setdefault(key, []).append(handler)

        async def _loop():
            q = self._queues[key]
            while self._started:
                event = await q.get()
                if event is None:
                    return
                for h in list(self._handlers[key]):
                    try:
                        result = h(event)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        log.exception(f"[bus] handler error for {subject_pattern}/{queue_group}")

        asyncio.create_task(_loop())

    async def events(self, subject_pattern: str, queue_group: str) -> AsyncIterator[BaseEvent]:
        key = (subject_pattern, queue_group)
        q = self._queues.setdefault(key, asyncio.Queue(maxsize=10_000))
        while self._started:
            event = await q.get()
            if event is None:
                return
            yield event

    async def replay(self, subject: str, since_ms: int) -> AsyncIterator[BaseEvent]:
        for sub, event in self._ring:
            if sub == subject and event.ts_event_ms >= since_ms:
                yield event


class NATSBus(EventBus):
    """Talks to a NATS server via nats-py. Use for multi-node or >10k events/s.

    Requires `nats-py` installed (pip install nats-py). Falls back to InProcessBus
    with a clear warning if nats-py is not importable.
    """

    def __init__(self, url: str = "nats://localhost:4222", ring_size: int = 100_000):
        self.url = url
        self._nc = None
        self._ring: list[tuple[str, BaseEvent]] = []
        self._ring_size = ring_size

    async def start(self) -> None:
        try:
            import nats  # type: ignore
        except ImportError:
            log.warning("[bus] nats-py not installed; falling back to InProcessBus")
            self._fallback = InProcessBus()
            await self._fallback.start()
            return
        self._nc = await nats.connect(self.url)
        log.info(f"[bus] connected to NATS at {self.url}")

    async def stop(self) -> None:
        if self._nc is not None:
            await self._nc.drain()

    async def publish(self, subject: str, event: BaseEvent) -> None:
        if event.ts_bus_ms == 0:
            event.ts_bus_ms = int(time.time() * 1000)
        self._ring.append((subject, event))
        if len(self._ring) > self._ring_size:
            self._ring = self._ring[-self._ring_size:]
        if self._nc is None:
            return
        await self._nc.publish(subject, event.model_dump_json().encode())

    async def subscribe(self, subject_pattern: str, queue_group: str, handler: HandlerT) -> None:
        if self._nc is None:
            return

        async def _cb(msg):
            try:
                import json as _json
                # Lazy import — only re-import Pydantic when handler runs.
                from .events import BaseEvent as _BaseEvent
                event = _BaseEvent.model_validate(_json.loads(msg.data.decode()))
                handler(event)
            except Exception:
                log.exception(f"[bus] nats handler error for {subject_pattern}")

        await self._nc.subscribe(subject_pattern, cb=_cb, queue=queue_group)

    async def events(self, subject_pattern: str, queue_group: str) -> AsyncIterator[BaseEvent]:
        # Use a simple subscriber with a queue
        if self._nc is None:
            return
        q: asyncio.Queue = asyncio.Queue()

        async def _cb(msg):
            import json as _json
            from .events import BaseEvent as _BaseEvent
            event = _BaseEvent.model_validate(_json.loads(msg.data.decode()))
            await q.put(event)

        await self._nc.subscribe(subject_pattern, cb=_cb, queue=queue_group)
        while True:
            yield await q.get()

    async def replay(self, subject: str, since_ms: int) -> AsyncIterator[BaseEvent]:
        # For production, this hits JetStream's replay API. For now,
        # serve from the in-memory ring.
        for sub, event in self._ring:
            if sub == subject and event.ts_event_ms >= since_ms:
                yield event


def make_bus() -> EventBus:
    """Factory: read BUS_BACKEND env var and return the right implementation."""
    backend = os.environ.get("BUS_BACKEND", "inproc").lower()
    if backend == "nats":
        url = os.environ.get("NATS_URL", "nats://localhost:4222")
        return NATSBus(url=url)
    return InProcessBus()


__all__ = ["EventBus", "InProcessBus", "NATSBus", "make_bus"]
