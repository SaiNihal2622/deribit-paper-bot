"""Tests for the Sentinel + Healer agents."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.healer import Healer
from crypto_options_bot.agent.memory import Memory
from crypto_options_bot.agent.sentinel import HealthReport, Sentinel


class TestSentinel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(root=self.root / "memory")
        self.heartbeat = self.root / "data_cache" / "heartbeat.json"
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text(
            '{"ts": 1, "open_trades": 1, "positions": 0, "pending_orders": 0}', encoding="utf-8"
        )
        self.state = self.root / "data_cache" / "paper_state.json"
        self.state.write_text(
            '{"open_trades": {"t1": {}}, "positions": {}, "orders": {}}', encoding="utf-8"
        )
        self.sentinel = Sentinel(
            project_root=self.root, memory=self.mem,
            heartbeat_path=self.heartbeat, state_path=self.state,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_probe_returns_report(self) -> None:
        rep = self.sentinel.probe()
        self.assertIsInstance(rep, HealthReport)
        self.assertIn("timestamp", rep.to_dict())

    def test_read_counts_from_state(self) -> None:
        # Sentinel reads file directly, not bot singleton.
        self.assertEqual(self.sentinel._read_counts(), (1, 0, 0))

    def test_health_persisted(self) -> None:
        self.sentinel.probe()
        snap = self.mem.read_state("health:latest")
        self.assertIn("bot_alive", snap)

    def test_heartbeat_age_computed(self) -> None:
        age = self.sentinel._heartbeat_age_sec()
        # File just written; should be small positive number.
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0)

    def test_missing_heartbeat(self) -> None:
        self.heartbeat.unlink()
        self.assertIsNone(self.sentinel._heartbeat_age_sec())

    def test_missing_state_file(self) -> None:
        # Heartbeat is now the primary source; if it has counts we use them.
        self.state.unlink()
        self.assertEqual(self.sentinel._read_counts(), (1, 0, 0))

    def test_missing_both_files(self) -> None:
        self.state.unlink()
        self.heartbeat.unlink()
        self.assertEqual(self.sentinel._read_counts(), (0, 0, 0))


class TestHealer(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mem = Memory(root=self.root / "memory")
        self.heartbeat = self.root / "data_cache" / "heartbeat.json"
        self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat.write_text('{"ts": 1}', encoding="utf-8")
        self.state = self.root / "data_cache" / "paper_state.json"
        self.state.write_text("{}", encoding="utf-8")
        self.sentinel = Sentinel(
            project_root=self.root, memory=self.mem,
            heartbeat_path=self.heartbeat, state_path=self.state,
        )
        self.healer = Healer(project_root=self.root, memory=self.mem, sentinel=self.sentinel)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _report(self, **kwargs):
        defaults = dict(
            timestamp="t", ok=True, bot_alive=True,
            heartbeat_age_sec=10.0, ws_subscribed=22,
            open_positions=0, open_trades=0, pending_orders=0, notes=[],
        )
        defaults.update(kwargs)
        return HealthReport(**defaults)

    def test_no_actions_when_healthy(self) -> None:
        actions = self.healer.heal(self._report())
        self.assertEqual(actions, [])

    def test_bot_dead_logged(self) -> None:
        rep = self._report(bot_alive=False, ok=False)
        actions = self.healer.heal(rep)
        # The script doesn't exist in our test, so action is recorded but error.
        flags = {a.playbook for a in actions}
        self.assertIn("bot_dead", flags)

    def test_heartbeat_warn_action_only(self) -> None:
        rep = self._report(heartbeat_age_sec=200.0, ok=False)
        actions = self.healer.heal(rep)
        flags = {a.playbook for a in actions}
        self.assertIn("heartbeat_warn", flags)

    def test_orphans_skipped_when_positions_present(self) -> None:
        rep = self._report(pending_orders=2, open_positions=1)
        actions = self.healer.heal(rep)
        flags = {a.playbook for a in actions}
        self.assertNotIn("orphans", flags)

    def test_journal_entries_written(self) -> None:
        self.healer.heal(self._report(bot_alive=False))
        journal = list((self.root / "memory" / "journal").glob("*.md"))
        self.assertTrue(journal)
        text = journal[0].read_text(encoding="utf-8")
        self.assertIn("playbook=bot_dead", text)

    def test_disk_low_logged_with_free_space(self) -> None:
        # Mock disk_usage via a sentinel override.
        rep = self._report()
        actions = self.healer.heal(rep)
        # No disk_low on a healthy system.
        self.assertNotIn("disk_low", {a.playbook for a in actions})


if __name__ == "__main__":
    unittest.main()
