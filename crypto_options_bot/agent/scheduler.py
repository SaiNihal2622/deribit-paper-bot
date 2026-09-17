"""Tiny cron-like scheduler for the agent layer.

Each ``Job`` runs at a fixed interval (or once-per-day at a specific
HH:MM). Jobs are cooperative: the scheduler hands control to one job
at a time inside the operator's event loop.

The scheduler exposes:

    * ``Scheduler.add(name, fn, every_sec=..., at_hhmm=...)``
    * ``Scheduler.run_until(idle_event)`` — main loop
    * ``Scheduler.pause(name)`` / ``Scheduler.resume(name)`` — soft pause
    * ``Scheduler.stop()`` — graceful shutdown for tests / signals

This deliberately avoids ``asyncio`` so the operator can run on the
stock stdlib interpreter that the bot already uses.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    fn: Callable[[], None]
    every_sec: Optional[float] = None       # interval-based
    at_hhmm: Optional[str] = None           # daily at HH:MM ("13:05")
    last_run_at: float = 0.0
    next_run_at: float = 0.0
    paused: bool = False
    runs: int = 0
    errors: int = 0
    last_error: str = ""
    last_duration_sec: float = 0.0

    def compute_next(self, now: float) -> None:
        if self.every_sec is not None:
            base = self.last_run_at or now
            self.next_run_at = base + self.every_sec
            return
        if self.at_hhmm is not None:
            hh, mm = (int(x) for x in self.at_hhmm.split(":"))
            import datetime

            base_ts = self.last_run_at or now
            t = datetime.datetime.fromtimestamp(base_ts)
            target = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
            target_ts = target.timestamp()
            if target_ts <= base_ts:
                target = target + datetime.timedelta(days=1)
                target_ts = target.timestamp()
            self.next_run_at = target_ts
            return
        # Neither configured; run once on next tick.
        self.next_run_at = now


@dataclass
class Scheduler:
    name: str = "scheduler"
    _jobs: list[Job] = field(default_factory=list)
    _stopped: bool = False
    _tick_sec: float = 0.5

    def add(
        self,
        name: str,
        fn: Callable[[], None],
        every_sec: Optional[float] = None,
        at_hhmm: Optional[str] = None,
    ) -> Job:
        if every_sec is None and at_hhmm is None:
            raise ValueError("add(): specify either every_sec or at_hhmm")
        if any(j.name == name for j in self._jobs):
            raise ValueError(f"job already exists: {name!r}")
        job = Job(name=name, fn=fn, every_sec=every_sec, at_hhmm=at_hhmm)
        job.compute_next(time.time())
        self._jobs.append(job)
        return job

    def get(self, name: str) -> Optional[Job]:
        for j in self._jobs:
            if j.name == name:
                return j
        return None

    def pause(self, name: str) -> None:
        j = self.get(name)
        if j:
            j.paused = True

    def resume(self, name: str) -> None:
        j = self.get(name)
        if j and j.paused:
            j.paused = False
            j.compute_next(time.time())

    def list_jobs(self) -> list[dict]:
        return [
            {
                "name": j.name,
                "every_sec": j.every_sec,
                "at_hhmm": j.at_hhmm,
                "paused": j.paused,
                "runs": j.runs,
                "errors": j.errors,
                "last_duration_sec": round(j.last_duration_sec, 4),
                "next_run_in_sec": round(max(0.0, j.next_run_at - time.time()), 2),
                "last_error": j.last_error,
            }
            for j in self._jobs
        ]

    def stop(self) -> None:
        self._stopped = True

    def run_until(self, stop_event: threading.Event) -> None:
        """Main loop. Returns when stop_event is set or self.stop() is called."""
        log.info("scheduler[%s] starting with %d jobs", self.name, len(self._jobs))
        while not (self._stopped or stop_event.is_set()):
            now = time.time()
            for job in self._jobs:
                if job.paused:
                    continue
                if now < job.next_run_at:
                    continue
                self._run_job(job, now)
            # Sleep until the soonest next_run_at, capped to _tick_sec.
            soonest = self._soonest_next_run(now)
            wait = min(self._tick_sec, max(0.05, soonest - now))
            stop_event.wait(timeout=wait)
        log.info("scheduler[%s] stopped", self.name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_job(self, job: Job, now: float) -> None:
        t0 = time.time()
        try:
            job.fn()
            job.runs += 1
        except Exception as exc:  # noqa: BLE001 — we want every error caught
            job.errors += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("scheduler[%s] job %s raised", self.name, job.name)
        finally:
            job.last_run_at = now
            job.last_duration_sec = time.time() - t0
            job.compute_next(time.time())

    def _soonest_next_run(self, now: float) -> float:
        candidates = [j.next_run_at for j in self._jobs if not j.paused]
        return min(candidates) if candidates else now + self._tick_sec
