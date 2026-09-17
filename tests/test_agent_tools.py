"""Tests for the tool registry."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.memory import Memory
from crypto_options_bot.agent.tools import ToolCategory, ToolRegistry


class TestToolRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = Memory(root=Path(self.tmp.name))
        self.reg = ToolRegistry(memory=self.mem)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_and_call(self) -> None:
        @self.reg.register("add", "add two numbers", ToolCategory.READ)
        def add(a: int, b: int) -> int:
            return a + b

        out = self.reg.call("add", {"a": 2, "b": 3})
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], 5)

    def test_call_unknown_tool(self) -> None:
        out = self.reg.call("nope")
        self.assertFalse(out["ok"])
        self.assertIn("unknown", out["error"])

    def test_call_catches_exception(self) -> None:
        @self.reg.register("boom", "always fails", ToolCategory.READ)
        def boom() -> int:
            raise RuntimeError("kaboom")

        out = self.reg.call("boom")
        self.assertFalse(out["ok"])
        self.assertIn("kaboom", out["error"])

    def test_recent_calls_truncated(self) -> None:
        @self.reg.register("noop", "no-op", ToolCategory.READ)
        def noop() -> None:
            return None

        for _ in range(10):
            self.reg.call("noop")
        recent = self.reg.recent_calls()
        self.assertEqual(len(recent), 10)

    def test_journal_recorded(self) -> None:
        @self.reg.register("ping", "ping", ToolCategory.READ)
        def ping() -> str:
            return "pong"

        self.reg.call("ping")
        # Tools journal under "tools" agent name.
        journal = list((Path(self.tmp.name) / "journal").glob("*.md"))
        self.assertTrue(journal)
        text = journal[0].read_text(encoding="utf-8")
        self.assertIn("tool=ping", text)

    def test_list_tools_includes_category(self) -> None:
        @self.reg.register("r", "r", ToolCategory.READ)
        def r() -> None:
            return None

        @self.reg.register("w", "w", ToolCategory.WRITE_STATE)
        def w() -> None:
            return None

        listed = {t["name"]: t["category"] for t in self.reg.list_tools()}
        self.assertEqual(listed["r"], "read")
        self.assertEqual(listed["w"], "write_state")


if __name__ == "__main__":
    unittest.main()
