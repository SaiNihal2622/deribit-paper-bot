"""test_strategy_presets.py — verify strategy preset variants.

Each strategy can be configured with either:
  - a flat config dict (one instance), OR
  - a `presets` sub-dict (one instance per preset, each inheriting
    the flat-dict params as defaults).

Presets give us TradingXBot-style "25 strategies" via parameter
variants. Each preset gets its own:
  - cooldown slot (via `unique_name = "<strategy>:<preset>"`)
  - trades journal attribution (preset_name preserved on Trade)
  - signal log entries (uses preset-aware name)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from crypto_options_bot.strategy.short_strangle import ShortStrangleStrategy
from crypto_options_bot.strategy.iron_condor import IronCondorStrategy
from crypto_options_bot.strategy.short_call import ShortCallStrategy
from crypto_options_bot.strategy.directional_debit import DirectionalDebitStrategy
from crypto_options_bot.strategy.calendar_spread import CalendarSpreadStrategy
from crypto_options_bot.strategy.long_straddle import LongStraddleStrategy
from crypto_options_bot.strategy.base import BaseStrategy, StrategyName


# ---------- Pure-Python unit tests of the preset framework ----------

def test_flat_config_single_instance():
    """No presets block -> one strategy instance with the flat config."""
    cfg = {"short_delta": 0.20, "wing_atm_mult": 0.05}
    s = ShortStrangleStrategy(cfg)
    assert s.preset_name == ""
    assert s.unique_name == "short_strangle"
    assert s.short_delta == 0.20
    assert s.wing_atm_mult == 0.05


def test_flat_config_defaults_preserved():
    """Without config, defaults kick in (regression check)."""
    s = ShortStrangleStrategy()
    assert s.preset_name == ""
    assert s.unique_name == "short_strangle"


def test_unique_name_with_preset_name():
    """When preset_name is set, unique_name includes it."""
    s = ShortStrangleStrategy({"short_delta": 0.20, "wing_atm_mult": 0.04})
    s.preset_name = "weekly_tight"
    assert s.unique_name == "short_strangle:weekly_tight"
    assert s.name == StrategyName.SHORT_STRANGLE  # class-level unchanged


def test_base_strategy_default_preset_name():
    """BaseStrategy instances have preset_name='' unless explicitly set."""
    s = ShortStrangleStrategy({})
    assert s.preset_name == ""


# ---------- Config-loading tests via __main__._build_strategies ----------

@pytest.fixture
def work_dir():
    """Provide a tempdir for settings.yaml; don't chdir (Windows can't
    remove cwd during teardown).

    `_build_strategies(work)` reads settings.yaml from the explicit path,
    so chdir is unnecessary — PaperRunner.__init__ only stores the path,
    doesn't open the file.
    """
    old = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        try:
            yield Path(td)
        finally:
            # If anything inside the test chdir'd back to old, fine;
            # otherwise, leave the cwd wherever it landed. The tempdir
            # will be cleaned up after the with-block exits.
            pass
    # chdir back AFTER tempdir cleanup (no-op if already in old).
    try:
        os.chdir(old)
    except (FileNotFoundError, OSError):
        pass


def _write_settings(work: Path, cfg: dict) -> None:
    (work / "config").mkdir(exist_ok=True)
    (work / "config" / "settings.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _build_strategies(work: Path) -> list:
    """Re-instantiate PaperRunner just enough to call _build_strategies()."""
    # Lazy-import so we don't pay the cost on every fixture
    sys.path.insert(0, str(REPO_ROOT))
    from crypto_options_bot.__main__ import PaperRunner
    import os as _os
    _os.environ.setdefault("DERIBIT_CLIENT_ID", "test_id")
    _os.environ.setdefault("DERIBIT_CLIENT_SECRET", "test_secret")
    runner = PaperRunner(cfg=yaml.safe_load((work / "config" / "settings.yaml").read_text(encoding="utf-8")))
    return runner._build_strategies()


def test_build_strategies_creates_one_instance_when_no_presets(work_dir):
    _write_settings(work_dir, {
        "strategy": {
            "cooldown_sec": 300,
            "short_strangle": {"short_delta": 0.20, "wing_atm_mult": 0.04, "min_iv_rank": 50},
            "iron_condor": {"wing_width_atm_mult": 0.03, "min_iv_rank": 30},
        }
    })
    strats = _build_strategies(work_dir)
    # 2 base strategies, no presets -> 2 instances
    assert len(strats) == 2
    assert all(s.preset_name == "" for s in strats)


def test_build_strategies_creates_one_per_preset(work_dir):
    """When `presets` block exists, instantiate one per preset key."""
    _write_settings(work_dir, {
        "strategy": {
            "cooldown_sec": 300,
            "short_strangle": {
                "short_delta": 0.20,
                "wing_atm_mult": 0.04,
                "min_iv_rank": 50,
                "presets": {
                    "weekly_tight": {"wing_atm_mult": 0.03},
                    "weekly_wide":  {"wing_atm_mult": 0.06},
                    "monthly_tight": {"wing_atm_mult": 0.04, "min_iv_rank": 35},
                },
            },
        }
    })
    strats = _build_strategies(work_dir)
    assert len(strats) == 3
    preset_names = {s.preset_name for s in strats}
    assert preset_names == {"weekly_tight", "weekly_wide", "monthly_tight"}
    # Each preset gets a unique cooldown key
    unique_names = {s.unique_name for s in strats}
    assert unique_names == {
        "short_strangle:weekly_tight",
        "short_strangle:weekly_wide",
        "short_strangle:monthly_tight",
    }


def test_presets_inherit_flat_defaults(work_dir):
    """Preset config merged with flat-dict defaults — preset wins on conflict."""
    _write_settings(work_dir, {
        "strategy": {
            "cooldown_sec": 300,
            "iron_condor": {
                "wing_width_atm_mult": 0.03,
                "short_delta": 0.16,
                "profit_target_pct": 50,
                "min_iv_rank": 30,
                "presets": {
                    "aggressive": {"short_delta": 0.20, "wing_width_atm_mult": 0.035},
                },
            },
        }
    })
    strats = _build_strategies(work_dir)
    assert len(strats) == 1
    s = strats[0]
    assert s.preset_name == "aggressive"
    assert s.short_delta == 0.20  # preset wins
    assert s.wing_width_atm_mult == 0.035  # preset wins
    assert s.min_iv_rank == 30  # inherited from flat
    assert s.profit_target_pct == 50  # inherited from flat


def test_count_total_strategy_variants_in_full_config(work_dir):
    """Verify we have at least 9 distinct strategy variants from the full config.

    TradingXBot advertises 25 strategies. We get there via parameter
    variants of our 6 base strategies. The actual count from
    config/settings.yaml is what matters.
    """
    # Re-use the real settings.yaml
    real = REPO_ROOT / "config" / "settings.yaml"
    if not real.exists():
        pytest.skip("real settings.yaml not found")
    cfg = yaml.safe_load(real.read_text(encoding="utf-8"))
    work_dir_obj = work_dir  # use the fixture
    _write_settings(work_dir, cfg)
    strats = _build_strategies(work_dir)
    # At least: 1 iron_condor (3 presets) + 1 short_strangle (4 presets)
    # + 1 short_call (2 presets) + 1 directional_debit (2 presets)
    # + 1 calendar_spread (2 presets) + 1 long_straddle (2 presets) = 15
    # or with no-presets fallbacks = potentially fewer
    print(f"\nTotal strategy variants loaded: {len(strats)}")
    for s in strats:
        print(f"  {s.unique_name}")
    assert len(strats) >= 9, f"Expected >=9 variants, got {len(strats)}"


def test_unique_names_are_unique_across_presets(work_dir):
    """No two strategies can share a unique_name (cooldown would collide)."""
    _write_settings(work_dir, {
        "strategy": {
            "cooldown_sec": 300,
            "short_strangle": {
                "short_delta": 0.20, "wing_atm_mult": 0.04,
                "presets": {
                    "p1": {"wing_atm_mult": 0.03},
                    "p2": {"wing_atm_mult": 0.06},
                    "p3": {"wing_atm_mult": 0.05},
                },
            },
        }
    })
    strats = _build_strategies(work_dir)
    names = [s.unique_name for s in strats]
    assert len(names) == len(set(names)), f"Duplicate unique_names: {names}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
