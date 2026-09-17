"""Tests for the agent Memory layer."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.memory import Memory


class TestMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(root=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # state/
    # ------------------------------------------------------------------
    def test_write_and_read_state(self) -> None:
        self.mem.write_state("health", {"bot_alive": True, "ws": 22})
        self.assertEqual(self.mem.read_state("health")["bot_alive"], True)
        self.assertEqual(self.mem.read_state("health")["ws"], 22)
        self.assertIn("_updated_at", self.mem.read_state("health"))

    def test_state_missing_returns_empty(self) -> None:
        self.assertEqual(self.mem.read_state("nope"), {})

    def test_list_state_keys(self) -> None:
        self.mem.write_state("a", {"x": 1})
        self.mem.write_state("b", {"x": 2})
        self.assertEqual(set(self.mem.list_state_keys()), {"a", "b"})

    def test_state_atomic(self) -> None:
        # Simulate crash mid-write: leave a .tmp file behind, ensure
        # load_state ignores it and returns empty.
        self.mem.write_state("ok", {"a": 1})
        (self.root / "state" / "broken.json.tmp").write_text("{partial", encoding="utf-8")
        self.assertEqual(self.mem.read_state("broken"), {})

    # ------------------------------------------------------------------
    # history/
    # ------------------------------------------------------------------
    def test_append_and_read_history(self) -> None:
        for i in range(5):
            self.mem.append_history("trades", {"i": i, "pnl": i * 10})
        out = self.mem.read_history("trades")
        self.assertEqual(len(out), 5)
        self.assertEqual(out[-1]["pnl"], 40)
        self.assertIn("_ts", out[0])

    def test_tail_history(self) -> None:
        for i in range(100):
            self.mem.append_history("x", {"i": i})
        self.assertEqual(len(self.mem.tail_history("x", n=10)), 10)
        self.assertEqual(self.mem.tail_history("x", n=10)[-1]["i"], 99)

    def test_history_corrupt_line_skipped(self) -> None:
        self.mem.append_history("x", {"i": 1})
        (self.root / "history" / "x.jsonl").write_text(
            (self.root / "history" / "x.jsonl").read_text(encoding="utf-8")
            + "garbage line\n",
            encoding="utf-8",
        )
        self.mem.append_history("x", {"i": 2})
        out = self.mem.read_history("x")
        self.assertEqual([r["i"] for r in out if "i" in r], [1, 2])

    # ------------------------------------------------------------------
    # lessons/
    # ------------------------------------------------------------------
    def test_write_lesson(self) -> None:
        path = self.mem.write_lesson("Test lesson", "Body text\nLine 2", tags=["test"])
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertIn("title: Test lesson", text)
        self.assertIn("tags: [test]", text)
        self.assertIn("Body text", text)

    def test_list_lessons(self) -> None:
        self.mem.write_lesson("Alpha", "a")
        self.mem.write_lesson("Beta", "b")
        out = self.mem.list_lessons()
        # Slug includes date prefix.
        self.assertTrue(any("alpha" in p.name.lower() for p in out))
        self.assertTrue(any("beta" in p.name.lower() for p in out))

    # ------------------------------------------------------------------
    # proposals/
    # ------------------------------------------------------------------
    def test_proposal_lifecycle(self) -> None:
        self.mem.write_proposal(
            proposal_id="p1",
            summary="tighten wings",
            rationale="DVOL up",
            diff={"strategy.iron_condor.wing_width": 250},
        )
        pending = self.mem.list_proposals(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["summary"], "tighten wings")
        self.mem.update_proposal_status("p1", "approved", note="manual")
        approved = self.mem.list_proposals(status="approved")
        self.assertEqual(approved[0]["note"], "manual")

    # ------------------------------------------------------------------
    # health/
    # ------------------------------------------------------------------
    def test_health_round_trip(self) -> None:
        for i in range(3):
            self.mem.write_health({"ok": i % 2 == 0})
        out = self.mem.read_health_today()
        self.assertEqual(len(out), 3)
        self.assertEqual([r["ok"] for r in out], [True, False, True])

    # ------------------------------------------------------------------
    # journal/
    # ------------------------------------------------------------------
    def test_journal_append(self) -> None:
        self.mem.append_journal("trader", "action=approve rationale=ok")
        self.mem.append_journal("healer", "playbook=disk_low")
        # We don't expose a reader; just verify the file exists with content.
        journal_files = list((self.root / "journal").glob("*.md"))
        self.assertEqual(len(journal_files), 1)
        text = journal_files[0].read_text(encoding="utf-8")
        self.assertIn("trader", text)
        self.assertIn("healer", text)
        self.assertIn("disk_low", text)

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    def test_slugify(self) -> None:
        self.assertEqual(Memory._slugify("Hello, World! Foo/Bar"), "hello-world-foo-bar")
        self.assertEqual(Memory._slugify("  spaces   "), "spaces")


if __name__ == "__main__":
    unittest.main()
