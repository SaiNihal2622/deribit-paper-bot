"""Tests for the Evolver agent."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from crypto_options_bot.agent.evolver import (
    MAX_RELATIVE_CHANGE,
    Evolver,
)
from crypto_options_bot.agent.llm import LLMClient, LLMResponse
from crypto_options_bot.agent.memory import Memory


def _llm(reply: str) -> LLMClient:
    def _mock(*_args, **_kwargs):
        return LLMResponse(text=reply, model="mock", input_tokens=1, output_tokens=1)

    return LLMClient(api_key="sk-test", mock=_mock)


def _write_review_prompt(project_root: Path) -> None:
    pd = project_root / "crypto_options_bot" / "agent" / "prompts"
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "evolver_review.md").write_text("CTX={{CONTEXT}}", encoding="utf-8")


def _write_settings(project_root: Path, body: str) -> Path:
    cfg = project_root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "settings.yaml"
    p.write_text(body, encoding="utf-8")
    return p


class TestEvolver(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write_settings(
            self.root,
            "strategy:\n"
            "  iron_condor:\n"
            "    profit_target_pct: 50\n"
            "    wing_width: 200\n"
            "  short_strangle:\n"
            "    profit_target_pct: 50\n"
            "    delta_threshold: 0.10\n"
            "risk:\n"
            "  max_open_positions: 4\n",
        )
        _write_review_prompt(self.root)
        self.mem = Memory(root=self.root / "memory")
        self.llm = _llm('{"proposals":[]}')
        self.ev = Evolver(project_root=self.root, memory=self.mem, llm=self.llm)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_proposals_when_llm_returns_empty(self) -> None:
        out = self.ev.run_once()
        self.assertEqual(out, [])

    def test_proposal_within_limits_autoapplies(self) -> None:
        # 50 -> 55 = +10 % (within ±20 %). profit_target_pct abs limit is 5.
        # 50 -> 55 is +5, exactly the absolute cap, so should auto-deploy.
        self.ev.llm = _llm(
            '{"proposals":[{"id":"e1","summary":"bump","rationale":"test","diff":{"strategy.iron_condor.profit_target_pct":55}}]}'
        )
        out = self.ev.run_once()
        self.assertEqual(len(out), 1)
        p = out[0]
        self.assertEqual(p.risk, "low")
        self.assertTrue(p.autodeploy)
        self.assertEqual(p.diff["strategy.iron_condor.profit_target_pct"], 55)
        # settings.yaml updated
        text = (self.root / "config" / "settings.yaml").read_text(encoding="utf-8")
        self.assertIn("profit_target_pct: 55", text)
        # proposal marked deployed
        proposals = self.mem.list_proposals("deployed")
        self.assertEqual(len(proposals), 1)

    def test_proposal_beyond_rel_limit_marked_high_risk(self) -> None:
        # 50 -> 75 = +50 % (over ±20 %). absolute cap is 5, so 50->75 violates both.
        self.ev.llm = _llm(
            '{"proposals":[{"id":"e2","summary":"big","rationale":"","diff":{"strategy.iron_condor.profit_target_pct":75}}]}'
        )
        out = self.ev.run_once()
        self.assertEqual(out[0].risk, "high")
        self.assertFalse(out[0].autodeploy)
        # settings.yaml NOT modified
        text = (self.root / "config" / "settings.yaml").read_text(encoding="utf-8")
        self.assertIn("profit_target_pct: 50", text)

    def test_max_open_positions_always_high_risk(self) -> None:
        self.ev.llm = _llm(
            '{"proposals":[{"id":"e3","summary":"cap","rationale":"","diff":{"risk.max_open_positions":1}}]}'
        )
        out = self.ev.run_once()
        self.assertEqual(out[0].risk, "high")
        self.assertFalse(out[0].autodeploy)

    def test_three_keys_escalates_to_high(self) -> None:
        self.ev.llm = _llm(
            '{"proposals":[{"id":"e4","summary":"multi","rationale":"","diff":'
            '{"strategy.iron_condor.profit_target_pct":52,'
            '"strategy.iron_condor.wing_width":210,'
            '"strategy.short_strangle.profit_target_pct":52}}]}'
        )
        out = self.ev.run_once()
        self.assertEqual(out[0].risk, "high")

    def test_unknown_key_skipped_in_apply(self) -> None:
        self.ev.llm = _llm(
            '{"proposals":[{"id":"e5","summary":"bad","rationale":"","diff":{"does.not.exist":1}}]}'
        )
        out = self.ev.run_once()
        # Low risk + auto-deploy attempts to apply but the key isn't in the file.
        # Settings yaml unchanged.
        text = (self.root / "config" / "settings.yaml").read_text(encoding="utf-8")
        self.assertIn("profit_target_pct: 50", text)

    def test_missing_review_prompt_returns_empty(self) -> None:
        (self.root / "crypto_options_bot" / "agent" / "prompts" / "evolver_review.md").unlink()
        out = self.ev.run_once()
        self.assertEqual(out, [])

    def test_invalid_json_returns_empty(self) -> None:
        self.ev.llm = _llm("not json at all")
        out = self.ev.run_once()
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
