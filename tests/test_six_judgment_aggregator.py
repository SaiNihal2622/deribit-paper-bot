"""Unit tests for the six-judgment aggregator and parser.

Run: python tests/test_six_judgment_aggregator.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make the agent package importable when running from project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crypto_options_bot.agent.trader import (  # noqa: E402
    SixJudgments,
    VALID_REGIMES,
    VALID_DIRECTIONS,
    VALID_TOXIC_FLOW,
    VALID_LIQUIDITY_STRESSED,
    VALID_QUOTE_ENV,
    VALID_INVENTORY_PRESSURE,
    _parse_six_judgments,
    aggregate_judgments,
)
from crypto_options_bot.agent.trader import TradeAction  # noqa: E402


def expect(label, got, want):
    ok = got == want
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}: got={got!r}, want={want!r}")
    return ok


def test_aggregator_tier1_vetoes():
    """Single-condition hard vetoes."""
    print("\n[test_aggregator_tier1_vetoes]")
    cases = [
        ("toxic_flow=high -> VETO",
         SixJudgments("range","neutral","high","no","favorable","no_pressure",""),
         TradeAction.VETO),
        ("liquidity_stressed=yes -> VETO",
         SixJudgments("range","neutral","low","yes","favorable","no_pressure",""),
         TradeAction.VETO),
        ("volatile_high + neutral dir -> VETO",
         SixJudgments("volatile_high","neutral","low","no","favorable","no_pressure",""),
         TradeAction.VETO),
    ]
    return all(
        expect(label, aggregate_judgments(j).action, want)
        for (label, j, want) in cases
    )


def test_aggregator_clean_approval():
    print("\n[test_aggregator_clean_approval]")
    # Range regime + neutral + favorable + no stress + low toxic = APPROVE
    j = SixJudgments(
        "range","neutral","low","no","favorable","no_pressure","clean short-vol setup",
    )
    d = aggregate_judgments(j, plan_qty=2)
    return all([
        expect("action", d.action, TradeAction.APPROVE),
        expect("target_qty respects plan", d.target_qty, 2),
    ])


def test_aggregator_quiet_low_approval():
    print("\n[test_aggregator_quiet_low_approval]")
    j = SixJudgments(
        "quiet_low","neutral","low","no","normal","no_pressure","cheap premium",
    )
    d = aggregate_judgments(j, plan_qty=1)
    return expect("action", d.action, TradeAction.APPROVE)


def test_aggregator_downsize_reasons():
    print("\n[test_aggregator_downsize_reasons]")
    cases = [
        ("trending_up triggers downsize",
         SixJudgments("trending_up","neutral","low","no","favorable","no_pressure",""),
         TradeAction.DOWNSIZE),
        ("trending_down triggers downsize",
         SixJudgments("trending_down","neutral","low","no","favorable","no_pressure",""),
         TradeAction.DOWNSIZE),
        ("unfavorable quotes trigger downsize",
         SixJudgments("range","neutral","low","no","unfavorable","no_pressure",""),
         TradeAction.DOWNSIZE),
        ("partial liquidity trigger downsize",
         SixJudgments("range","neutral","low","partial","favorable","no_pressure",""),
         TradeAction.DOWNSIZE),
        ("inventory pressure trigger downsize",
         SixJudgments("range","neutral","low","no","favorable","long_bias",""),
         TradeAction.DOWNSIZE),
        ("toxic_flow=normal triggers downsize",
         SixJudgments("range","neutral","normal","no","favorable","no_pressure",""),
         TradeAction.DOWNSIZE),
    ]
    return all(
        expect(label, aggregate_judgments(j).action, want)
        for (label, j, want) in cases
    )


def test_aggregator_qty_floor():
    """target_qty should never exceed plan_qty; tier4 floor at 1."""
    print("\n[test_aggregator_qty_floor]")
    j_down = SixJudgments("range","neutral","normal","no","unfavorable","long_bias","")
    d_down = aggregate_judgments(j_down, plan_qty=10)
    expect("DOWNSIZE clamps to 1", d_down.target_qty, 1)

    j_mixed = SixJudgments("range","bullish","low","no","normal","no_pressure","")
    d_mixed = aggregate_judgments(j_mixed, plan_qty=5)
    expect("tier-4 mixed -> downsize", d_mixed.action, TradeAction.DOWNSIZE)
    expect("tier-4 target_qty=1", d_mixed.target_qty, 1)
    return True


def test_parse_six_judgments():
    print("\n[test_parse_six_judgments]")
    cases = [
        ("clean JSON",
         '{"regime":"range","direction":"neutral","toxic_flow":"low",'
         '"liquidity_stressed":"no","quote_environment":"favorable",'
         '"inventory_pressure":"no_pressure","rationale":"clean"}'),
        ("fenced JSON",
         '```json\n{"regime":"range","direction":"neutral","toxic_flow":"low",'
         '"liquidity_stressed":"no","quote_environment":"favorable",'
         '"inventory_pressure":"no_pressure","rationale":"clean"}\n```'),
        ("inside prose",
         'Here is the analysis: '
         '{"regime":"range","direction":"neutral","toxic_flow":"low",'
         '"liquidity_stressed":"no","quote_environment":"favorable",'
         '"inventory_pressure":"no_pressure","rationale":"clean"} ok'),
    ]
    all_ok = True
    for label, raw in cases:
        parsed = _parse_six_judgments(raw)
        all_ok &= expect(label, parsed is not None and parsed.regime == "range", True)
    return all_ok


def test_parse_rejects_invalid_enums():
    print("\n[test_parse_rejects_invalid_enums]")
    bad = '{"regime":"WAT","direction":"neutral","toxic_flow":"low",'\
          '"liquidity_stressed":"no","quote_environment":"favorable",'\
          '"inventory_pressure":"no_pressure","rationale":"x"}'
    parsed = _parse_six_judgments(bad)
    return expect("invalid enum rejected", parsed is None, True)


def test_valid_sets_complete():
    print("\n[test_valid_sets_complete]")
    return all([
        expect("regimes", "range" in VALID_REGIMES, True),
        expect("directions", "bullish" in VALID_DIRECTIONS, True),
        expect("toxic_flow", "high" in VALID_TOXIC_FLOW, True),
        expect("liquidity", "partial" in VALID_LIQUIDITY_STRESSED, True),
        expect("quote_env", "favorable" in VALID_QUOTE_ENV, True),
        expect("inventory", "short_bias" in VALID_INVENTORY_PRESSURE, True),
    ])


if __name__ == "__main__":
    funcs = [
        test_aggregator_tier1_vetoes,
        test_aggregator_clean_approval,
        test_aggregator_quiet_low_approval,
        test_aggregator_downsize_reasons,
        test_aggregator_qty_floor,
        test_parse_six_judgments,
        test_parse_rejects_invalid_enums,
        test_valid_sets_complete,
    ]
    failed = 0
    total = len(funcs)
    for f in funcs:
        try:
            ok = f()
            if not ok:
                failed += 1
        except Exception as exc:
            print(f"  [ERROR] {f.__name__}: {exc}")
            failed += 1
    print("\n" + "=" * 60)
    print(f"  {total - failed}/{total} test groups passed")
    if failed == 0:
        print("  ALL TESTS PASSED")
        sys.exit(0)
    print(f"  {failed} group(s) FAILED")
    sys.exit(1)
