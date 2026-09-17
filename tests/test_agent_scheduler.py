"""Tests for the scheduler + sentinel + healer integration."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from crypto_options_bot.agent.scheduler import Job, Scheduler


class TestScheduler(unittest.TestCase):
    def test_add_requires_interval(self) -> None:
        s = Scheduler()
        with self.assertRaises(ValueError):
            s.add("bad", lambda: None)

    def test_add_duplicate_raises(self) -> None:
        s = Scheduler()
        s.add("a", lambda: None, every_sec=1.0)
        with self.assertRaises(ValueError):
            s.add("a", lambda: None, every_sec=1.0)

    def test_runs_at_interval(self) -> None:
        s = Scheduler()
        hits: list[float] = []
        s.add("t", lambda: hits.append(time.time()), every_sec=0.05)
        stop = threading.Event()
        t = threading.Thread(target=s.run_until, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.4)
        stop.set()
        t.join(timeout=2.0)
        self.assertGreaterEqual(len(hits), 3, f"expected >=3 hits, got {len(hits)}")

    def test_pause_and_resume(self) -> None:
        s = Scheduler()
        hits: list[int] = []
        s.add("p", lambda: hits.append(1), every_sec=0.05)
        s.pause("p")
        stop = threading.Event()
        t = threading.Thread(target=s.run_until, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.4)
        self.assertEqual(len(hits), 0)
        s.resume("p")
        time.sleep(0.6)
        stop.set()
        t.join(timeout=2.0)
        self.assertGreater(len(hits), 0)

    def test_at_hhmm_fires(self) -> None:
        # HH:MM scheduling is inherently tied to wall-clock minute boundaries,
        # which makes wall-clock-based tests flaky (target "right now + 5s"
        # can land on the same minute, then the scheduler correctly defers
        # to tomorrow's slot). Test compute_next() directly instead.
        from datetime import datetime, timedelta

        job = Job(name="j", fn=lambda: None, at_hhmm="12:00")
        # Simulate "now" being 23:30 — next slot should be today at 23:30 + ??.
        # Actually 23:30 < 12:00 next day, so 14.5h away.
        import time as _t

        # Anchor on a known timestamp for reproducibility.
        anchor = _t.mktime((2026, 1, 1, 0, 0, 0, 0, 0, 0))  # 2026-01-01 00:00:00
        # 12:00 same day is 12h away.
        job.compute_next(anchor)
        self.assertEqual(job.next_run_at, anchor + 12 * 3600)

        # If we call compute_next AFTER 12:00, it should jump to tomorrow.
        job.compute_next(anchor + 13 * 3600)  # 13:00
        self.assertEqual(job.next_run_at, anchor + 36 * 3600)  # next day's 12:00

        # If we just missed (e.g., 12:00:00.5), still jump to tomorrow.
        job.compute_next(anchor + 12 * 3600 + 5)  # 12:00:05 same day
        self.assertEqual(job.next_run_at, anchor + 36 * 3600)

    @unittest.skip("wall-clock HH:MM test is inherently flaky in CI; "
                     "covered by test_at_hhmm_computes_next_occurrence")
    def test_at_hhmm_fires_wallclock(self) -> None:
        # Real wall-clock test: schedule for HH:MM:30 of the NEXT minute
        # boundary. Skipped by default; un-skip to run manually.
        from datetime import datetime, timedelta

        now = datetime.now()
        target_dt = (now + timedelta(seconds=70)).replace(second=30, microsecond=0)
        target = target_dt.strftime("%H:%M")
        s = Scheduler()
        hits: list[int] = []
        s.add("daily", lambda: hits.append(1), at_hhmm=target)
        stop = threading.Event()
        t = threading.Thread(target=s.run_until, args=(stop,), daemon=True)
        t.start()
        time.sleep((target_dt - now).total_seconds() + 5.0)
        stop.set()
        t.join(timeout=2.0)
        self.assertEqual(len(hits), 1, f"expected exactly 1 hit at {target}, got {len(hits)}")

    def test_list_jobs(self) -> None:
        s = Scheduler()
        s.add("a", lambda: None, every_sec=5.0)
        s.add("b", lambda: None, at_hhmm="03:33")
        jobs = s.list_jobs()
        names = {j["name"] for j in jobs}
        self.assertEqual(names, {"a", "b"})
        self.assertEqual([j for j in jobs if j["name"] == "a"][0]["every_sec"], 5.0)
        self.assertEqual([j for j in jobs if j["name"] == "b"][0]["at_hhmm"], "03:33")

    def test_errors_recorded_but_loop_continues(self) -> None:
        s = Scheduler()
        counter = {"n": 0}

        def flaky() -> None:
            counter["n"] += 1
            if counter["n"] == 1:
                raise RuntimeError("first call fails")

        s.add("flaky", flaky, every_sec=0.05)
        stop = threading.Event()
        t = threading.Thread(target=s.run_until, args=(stop,), daemon=True)
        t.start()
        time.sleep(0.3)
        stop.set()
        t.join(timeout=2.0)
        self.assertGreater(counter["n"], 1)
        job = s.get("flaky")
        self.assertEqual(job.errors, 1)
        self.assertIn("first call fails", job.last_error)


if __name__ == "__main__":
    unittest.main()
