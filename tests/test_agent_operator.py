"""Tests for the Operator — top-level orchestrator + integration with all agents."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from crypto_options_bot.agent.llm import LLMClient, LLMResponse
from crypto_options_bot.agent.operator import Operator, OperatorConfig
from crypto_options_bot.agent.tools import ToolCategory


def _llm(reply: str = "ok") -> LLMClient:
    def _mock(*_args, **_kwargs):
        return LLMResponse(text=reply, model="mock", input_tokens=1, output_tokens=1)

    return LLMClient(api_key="sk-test", mock=_mock)


def _write_prompts(project_root: Path) -> None:
    pd = project_root / "crypto_options_bot" / "agent" / "prompts"
    pd.mkdir(parents=True, exist_ok=True)
    for f in ("trader_system", "trader_decision", "evolver_review", "reflector_daily"):
        (pd / f"{f}.md").write_text("CTX={{CONTEXT}}", encoding="utf-8")


class TestOperator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write_prompts(self.root)
        # Fake paper_state.json so Sentinel doesn't blow up reading it.
        (self.root / "data_cache").mkdir(parents=True, exist_ok=True)
        (self.root / "data_cache" / "paper_state.json").write_text("{}", encoding="utf-8")
        # Settings.yaml so evolver snapshot doesn't break.
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "settings.yaml").write_text(
            "strategy:\n  iron_condor:\n    profit_target_pct: 50\n", encoding="utf-8"
        )
        self.cfg = OperatorConfig(
            project_root=self.root,
            memory_dir=self.root / "memory",
            sentinel_interval_sec=0.2,
            evolver_interval_sec=10.0,
            reflector_at_hhmm="00:05",
            heartbeat_interval_sec=0.1,
            llm_model="mock/test",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build(self) -> Operator:
        op = Operator(config=self.cfg)
        op.llm = _llm()
        return op

    def test_constructs_with_all_agents(self) -> None:
        op = self._build()
        self.assertIsNotNone(op.sentinel)
        self.assertIsNotNone(op.healer)
        self.assertIsNotNone(op.trader)
        self.assertIsNotNone(op.evolver)
        self.assertIsNotNone(op.reflector)
        self.assertEqual(len(op.scheduler.list_jobs()), 5)

    def test_runs_for_a_few_seconds(self) -> None:
        op = self._build()
        stop = threading.Event()
        t = threading.Thread(target=op.run_until, args=(stop,), daemon=True)
        t.start()
        time.sleep(1.5)
        stop.set()
        t.join(timeout=3.0)

        self.assertGreater(op.cycles, 0)
        # Sentinel must have written at least one health snapshot.
        health = op.memory.read_state("health:latest")
        self.assertTrue(health, "expected sentinel to have written health:latest")
        # Operator's own heartbeat file must exist.
        hb_path = self.cfg.heartbeat_path
        self.assertTrue(hb_path.exists(), f"missing heartbeat at {hb_path}")

    def test_status_returns_dashboard_dict(self) -> None:
        op = self._build()
        s = op.status()
        self.assertIn("scheduler", s)
        self.assertEqual(len(s["scheduler"]), 5)
        self.assertIn("trader", s)
        self.assertIn("healer", s)
        self.assertIn("evolver", s)
        self.assertIn("reflector", s)
        self.assertIn("llm_budget", s)
        self.assertIn("tools_recent", s)

    def test_tools_registered(self) -> None:
        op = self._build()
        names = {t["name"] for t in op.tools.list_tools()}
        self.assertIn("read_paper_state", names)
        self.assertIn("read_settings", names)
        self.assertIn("list_lessons", names)
        self.assertIn("list_proposals", names)
        self.assertIn("approve_proposal", names)
        self.assertIn("reject_proposal", names)
        self.assertIn("write_journal", names)

    def test_read_settings_tool_works(self) -> None:
        op = self._build()
        out = op.tools.call("read_settings")
        self.assertTrue(out["ok"])
        self.assertIn("strategy", out["result"])

    def test_read_paper_state_tool_works(self) -> None:
        op = self._build()
        out = op.tools.call("read_paper_state")
        self.assertTrue(out["ok"])
        self.assertEqual(out["result"], {})

    def test_approve_proposal_tool(self) -> None:
        op = self._build()
        op.memory.write_proposal(
            proposal_id="px",
            summary="t",
            rationale="r",
            diff={"x": 1},
        )
        out = op.tools.call("approve_proposal", {"proposal_id": "px", "note": "ok"})
        self.assertTrue(out["ok"])
        proposals = op.memory.list_proposals("approved")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["note"], "ok")

    def test_write_journal_tool(self) -> None:
        op = self._build()
        out = op.tools.call("write_journal", {"agent": "test", "text": "hello"})
        self.assertTrue(out["ok"])

    def test_reflector_disabled_skipped(self) -> None:
        self.cfg.enable_reflector = False
        self.cfg.evolver_interval_sec = 60.0
        op = self._build()
        # Force one reflector tick manually — should be no-op.
        op._tick_reflector()
        self.assertIsNone(op.reflector.last_lesson_path)


if __name__ == "__main__":
    unittest.main()
