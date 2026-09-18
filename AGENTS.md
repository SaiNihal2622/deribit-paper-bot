# AGENTS.md — Crypto Options Bot (deribit-paper-bot)

> **Audience:** future Mavis (mavis) sessions picking up this repo.
> **Goal:** stay on the same page about what the project is, what is finished, what is dangerous, and how to keep moving it forward.
> **Read first:** this file. Then `README.md`. Then `crypto_options_bot/agent/operator.py`.

---

## 1. What this project is

A **production-grade crypto options paper-trading bot** for Deribit testnet, wrapped by an **optional 6-agent self-evolving / self-healing operator** driven by the local `MiniMax M3` LLM.

- **Repo:** `SaiNihal2622/deribit-paper-bot` on GitHub
- **Local path:** `C:/Users/saini/.minimax-agent/projects/crypto-options-bot/`
- **Stack:** Python 3.12 stdlib-first. Dependencies are `loguru`, `pyyaml`, `python-dotenv`, `websocket-client`, `pytest`, `httpx` (LLM only). No Flask/FastAPI/numpy/pandas/scipy.
- **Exchange:** Deribit **testnet only by default**. Live trading requires explicit env-var consent (`DERIBIT_LIVE_CONFIRMED=YES`).
- **Mode:** paper-by-default. No real money unless the safety guard is explicitly flipped.

The bot **does not depend on the agent layer**. The agent layer is an enhancement. If `MINIMAX_API_KEY` is missing or the operator is down, the bot keeps trading via rule-based decisions.

---

## 2. Architecture (one paragraph)

The 5-strategy core (`iron_condor`, `short_strangle`, `directional_debit`, `calendar_spread`, `long_straddle`) is unchanged from the original repo. On top sits `crypto_options_bot/agent/` with 6 named agents wired by a single `Operator`:

| Agent | Cadence | What it does |
|---|---|---|
| Sentinel  | 60 s  | probe bot / heartbeat / WS / disk / positions |
| Healer    | 60 s  | recover via playbooks (`bot_dead`, `heartbeat_warn`, `ws_no_channels`, `orphans`, `disk_low`) |
| Trader    | every cycle (advisory) | LLM approves / vetoes / downsizes / holds the next TradePlan |
| Evolver   | 6 h    | reads journal + state + lesson → JSON config diff → auto-deploys low-risk (±20% rel, ±5% abs) or escalates |
| Reflector | daily @ 00:05 UTC | writes a Markdown lesson to `memory/lessons/` |
| Operator  | always | owns `Scheduler`, `LLMClient`, `Memory`; writes heartbeat to `data_cache/operator.heartbeat` |

LLM prompts are versioned in `crypto_options_bot/agent/prompts/` as Markdown. All prompt outputs are strict JSON.

---

## 3. Hard rules (do not violate)

1. **Paper-by-default.** No live trading changes without the user explicitly setting `DERIBIT_LIVE_CONFIRMED=YES`. The `DeribitClient` safety guard will refuse otherwise.
2. **Trader cannot widen risk rails.** `RiskEngine` caps are the only authority. The Trader's LLM can only `APPROVE / VETO / DOWNSIZE / HOLD` with a `target_qty` (downward only).
3. **Healer cannot edit code or positions.** Restart-only.
4. **Evolver diff rails are hard-coded in `evolver.py`:**
   - max ±20 % relative change per scalar
   - max ±5 percentage-point absolute change per scalar
   - ≥3 keys → escalate to human-approval (`pending` proposal)
   - `max_open_positions` is always escalated
   - **Only known scalar keys.** Unknown keys rejected outright.
5. **Real Deribit data only.** No synthetic ticks, no fake IV. Public endpoints need no auth; private endpoints need explicit `DERIBIT_CLIENT_ID/SECRET`.
6. **Stdlib HTTP.** `urllib.request` for REST. `http.server.ThreadingHTTPServer` for the dashboard. Don't add `requests`/`flask`/`httpx` to bot-core code (`httpx` is only OK inside the agent layer's LLM client).
7. **`memory/` is gitignored.** Do not commit it. It's per-instance state, rebuilt every restart.
8. **No new dependencies** without user confirmation. The dependency footprint is a feature; respect it.

---

## 3a. Profitability gates (NEW 2026-09-18)

Three settings in `config/settings.yaml` work together to make the bot trade
only when there's actual edge. They are *not* hardcoded — they live in config
so the user can tune them.

| Gate | Setting | Default | What it does |
|---|---|---|---|
| **IV regime** | `data.iv_regime_gate.{BTC,ETH}.{min_dvol,min_iv_rank}` | BTC: DVOL≥50, iv_rank≥40 / ETH: DVOL≥55, iv_rank≥45 | Skip all strategies on a currency when its DVOL is too low to make short premium profitable. Logs once every 5 min so it doesn't spam. |
| **Min DTE** | `data.min_dte_to_trade` | `2` | Skip strikes whose expiry is < 2 days out. No theta runway = no edge. Wired into both the WS feed (drops 0DTE/1DTE expiries from subscription) and the `short_call` strategy (rejects plans on near-dated legs). |
| **Wider universe** | `data.ws.expiry_count` + `data.ws.max_strikes_per_expiry` | `5` expiries × `5` strikes | Push the universe beyond the same-day weekly. Bot now subscribes to today + 4 future expiries (weeklies + monthlies) so it can find liquid quotes further out the curve. |

When DVOL is mid (BTC ~34, ETH ~50 today), the regime gate closes and
the bot logs `REGIME GATE closed for BTC: dvol=34<50. Waiting for vol to
recover (no trades)`. The position state, open trades, and operator
keep running — only new entries are paused.

---

## 3b. Going to mainnet (live trading on real Deribit)

Paper trading uses Deribit **testnet**, which has thin liquidity on
OTM strikes (puts especially). To flip to real-money mainnet:

1. Create a Deribit mainnet API key at https://www.deribit.com/ →
   Account → API → Create New Key (scope: `trade` + `read`).
2. Set three env vars in `.env`:
   ```
   DERIBIT_CLIENT_ID=<api_key>
   DERIBIT_CLIENT_SECRET=<api_secret>
   DERIBIT_LIVE_CONFIRMED=YES
   ```
3. Edit `config/settings.yaml`: change `data.deribit_env: testnet` → `prod`.
4. Stop the bot: `Stop-Process` on `bot.pid` (or `nssm stop KotakBotPaper`).
5. Restart: `powershell -File start_bot_detached.ps1` (or `nssm start`).
6. Verify: `python -m crypto_options_bot status` (look for `feed=DeribitWebSocketFeed` with `env=prod`).

`scripts/mainnet_readiness.py` automates step 0 — it prints what's
missing and gives you the exact `FIX` line for each blocker. Run it any
time before going live.

**Safety guarantee:** the bot refuses to start in `live` mode unless
`DERIBIT_LIVE_CONFIRMED=YES`. This is enforced by `DeribitSafetyError`
in `broker/deribit_client.py`. Setting `KOTAK_ENV=prod` without
`DERIBIT_LIVE_CONFIRMED=YES` will raise an error and the bot will not
trade.

---

## 4. State-of-the-world snapshot (last verified 2026-09-18)

- **Tests:** `pytest -q` → `156 passed, 1 skipped` (the skip is `test_at_hhmm_wall_clock` — covered deterministically by `test_at_hhmm_computes_next_occurrence`).
- **Branch:** `main`. Local HEAD ahead of `origin/main` by 9 commits pending this push.
- **NSSM services:** ✅ Both running. `CryptoOptionsBot` and `CryptoOptionsOperator` registered as Automatic-start Windows services. Bot parent PID = nssm.exe (PIDs 4952/5052/13036 etc.). Dashboard at http://127.0.0.1:8511/ responds.
- **Watchdog scheduled task:** Disabled. With NSSM running the bot, watchdog is redundant and would race for port 8511. Keep disabled.
- **Bot:** running on mainnet (wss://www.deribit.com), 50+ WS channels, $100k paper capital clean, 0 trades (regime gate engaged due to DVOL=35 BTC / 51 ETH, both below 50/55 thresholds).
- **Operator:** running. All 6 agent jobs active (sentinel, healer, evolver, reflector, heartbeat, llm_probe). 0 errors.
- **DVOL right now:** BTC=35, ETH=51 (real mainnet readings). Bot logs `REGIME GATE closed for {cur}` once per currency per ~5min cycle.
- **Mainnet:** ✅ Active. Credentials in `.env` (Deribit key name `cryptooptionsbot`, scopes trade:read_write + account:read_write). Bot reads real mainnet quotes via WS. Order placement is simulated by PaperClient — for real trading also flip `DERIBIT_LIVE_CONFIRMED=YES` in `.env`. See §3b.
- **Critical system-wide Python deps** (required for NSSM to launch bot successfully as LocalSystem): `pyyaml`, `loguru`, `colorama`, `pywin32`/`win32-setctime`, `websocket-client`, `python-dotenv`, `httpx`. If you reinstall Python or move to a new machine, run `pip install --upgrade pyyaml loguru colorama pywin32 win32-setctime websocket-client python-dotenv httpx pytest` from an **elevated** PowerShell.

---

## 5. Common operations

```powershell
# 30 s bot smoke
python -m crypto_options_bot paper --max-runtime 30

# 30 s operator smoke
python -m crypto_options_bot operator --max-runtime 30

# Status snapshot (human-readable)
python -m crypto_options_bot status
python scripts\operator_status.py

# Health probe (exit 0 = healthy)
powershell -File scripts\operator_health.ps1

# Install as NSSM service (admin required)
.\start_bot_service.ps1 install              # bot
.\start_bot_service.ps1 install-dashboard    # dashboard on :8511
.\start_bot_service.ps1 install-operator     # 6-agent operator
.\start_bot_service.ps1 status

# Watchdog + heartbeat + daily reset (Task Scheduler)
.\install_scheduled_tasks.ps1

# Mainnet readiness — env-var + config audit before flipping to live
python scripts/mainnet_readiness.py           # human-readable
python scripts/mainnet_readiness.py --json    # CI-friendly

# All tests
python -m pytest tests/ -v
```

Useful env vars (`.env`):

| Var | Required for | Default |
|---|---|---|
| `DERIBIT_CLIENT_ID` | mainnet private API | unset (testnet only) |
| `DERIBIT_CLIENT_SECRET` | mainnet private API | unset (testnet only) |
| `DERIBIT_LIVE_CONFIRMED` | live trading safety guard | `NO` |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Telegram alerts | unset |
| `MINIMAX_API_KEY` | LLM-driven agent layer | unset → rule-based fallback |
| `MINIMAX_BASE_URL` | override the API endpoint | `https://agent.minimax.io/mavis/api/v1/llm/v1` |
| `MINIMAX_CONFIG` | override the config file | `C:/Users/saini/.minimax/config.yaml` |

---

## 6. File layout (what lives where)

```
crypto-options-bot/
├── AGENTS.md                                ← this file
├── README.md
├── LICENSE                                  ← MIT
├── .env.example                             ← copy → .env
├── .gitignore                               ← excludes memory/, data_cache/, logs/, .env
├── requirements.txt
├── config/
│   └── settings.yaml                        ← all knobs (incl. agent: block)
├── crypto_options_bot/
│   ├── __main__.py                          ← CLI: paper / live / status / reset / operator
│   ├── supervisor.py                        ← exp-backoff child re-spawn
│   ├── data/                                ← REST polling + WS feed
│   ├── broker/                              ← PaperClient, DeribitClient (with safety guard)
│   ├── risk/                                ← Black-Scholes Greeks, RiskEngine + adaptive presets
│   ├── strategy/                            ← 5 strategies (eligibility + plan)
│   │   └── short_call.py    ← single-leg call-only (testnet-compatible, DTE-aware)
│   ├── execution/                           ← OrderManager
│   ├── alerts/                              ← TelegramAlerter
│   ├── dashboard/                           ← stdlib http.server :8511
│   ├── agent/                               ← 6-agent layer (optional)
│   │   ├── llm.py        ← MiniMax wrapper + budget + retry + mock
│   │   ├── memory.py     ← atomic JSON + JSONL append + proposals
│   │   ├── scheduler.py  ← cron-like (every_sec | at_hhmm) + pause/resume
│   │   ├── tools.py      ← READ / WRITE_STATE tool registry
│   │   ├── sentinel.py   ← bot / heartbeat / WS / disk / positions probe
│   │   ├── healer.py     ← playbook runner (restarts only)
│   │   ├── trader.py     ← APPROVE/VETO/DOWNSIZE/HOLD wrapper
│   │   ├── evolver.py    ← journal-driven config diff + auto-deploy rails
│   │   ├── reflector.py  ← daily lesson writer
│   │   ├── operator.py   ← top-level orchestrator + status
│   │   └── prompts/      ← versioned LLM prompts (Markdown)
│   └── utils/             ← loguru config
├── scripts/
│   ├── start_operator.ps1     ← detached operator launcher (PID file)
│   ├── stop_operator.ps1      ← graceful kill
│   ├── operator_health.ps1    ← exit-0 health probe (Task Scheduler)
│   ├── operator_status.py     ← pretty-print status
│   └── mainnet_readiness.py   ← env-var + config audit before going live
├── start_bot_service.ps1        ← NSSM installer (+ install-operator)
├── start_bot_detached.ps1
├── stop_bot_service.ps1
├── watchdog.ps1, heartbeat.ps1, daily_reset.ps1
├── supervisor_loop.ps1
├── install_scheduled_tasks.ps1
├── run_paper.bat
├── docs/screenshots/             ← README images
├── logs/                          ← gitignored
├── data_cache/                    ← gitignored (paper_state.json, operator.heartbeat)
└── tests/                         ← 119 unit tests + 1 skip
```

---

## 7. How to add a new agent

1. Create `crypto_options_bot/agent/<name>.py` exporting a class with a `def run_once(self) -> dict` and a meaningful state attribute.
2. Add its prompt (if it uses the LLM) to `crypto_options_bot/agent/prompts/<name>.md`.
3. Wire it in `Operator.__post_init__` — instantiate it, register a scheduler job via `self.scheduler.add(name, self._tick_<name>, every_sec=...)` or `at_hhmm=...`.
4. Surface it in `Operator.status()`.
5. Add tests in `tests/test_agent_<name>.py` using the mocked `LLMClient(api_key="sk-test", mock=fn)` pattern. Never call a real LLM in CI.

---

## 8. How to add a new strategy

(unchanged from before the agent layer was added)

1. `crypto_options_bot/strategy/<name>.py` — subclass `BaseStrategy`, implement `is_eligible(context) -> bool` and `build_plan(context) -> TradePlan | None`.
2. Add `StrategyName.<NAME>` in `crypto_options_bot/strategy/base.py`.
3. Add a `strategy:<name>:` block to `config/settings.yaml`.
4. Register the strategy in `__main__.PaperRunner._build_strategies`.
5. Add tests in `tests/test_strategies.py`.

---

## 9. How to test the LLM layer without burning budget

`LLMClient` accepts a `mock` callable in tests:

```python
from crypto_options_bot.agent.llm import LLMClient

def fake_complete(messages, **kw):
    return {"content": [{"type": "text", "text": '{"action":"APPROVE","target_qty":1,"reason":"test"}'}]}

client = LLMClient(api_key="sk-test", mock=fake_complete)
```

The mock bypasses the HTTP layer entirely. Budget is **not** decremented by mock calls. Use this for every CI test.

For a live-but-cheap end-to-end smoke:

```powershell
# 30-second operator run with real MiniMax calls
$env:MINIMAX_API_KEY="sk-your-key"
python -m crypto_options_bot operator --max-runtime 30 --verbose
```

---

## 10. Reference repos (read-only inspiration)

- `C:/Users/saini/.minimax-agent/projects/kotak-neo-bot/` — operational maturity patterns (live_go_tracker, _slippage_audit, _system_audit, quant_service). The 7-layer supervision stack in this project mirrors that one. Good source of next-step operational ideas.
- `C:/Users/saini/.minimax-agent/projects/CripeBot/` — hexagonal architecture, exchange adapter hardening, ML validation, EventBus (Redis Streams). Look here if/when scaling beyond a single bot.

---

## 11. Known limitations / open work

- **No `MINIMAX_API_KEY` set locally** → agent layer falls back to rule-based decisions. Bot still trades.
- **No live Deribit credentials** → bot is stuck on testnet. By design.
- **PAT exposed** in `git remote -v` output → user must revoke via GitHub. Also re-flip repo to private.
- **Active testing of agent LLM** requires the user to mint a real `MINIMAX_API_KEY` and add it to `.env`. Without it, the agent's Trader / Evolver / Reflector will never make an LLM call.
- **Evolver's auto-deploy is conservative.** It will never touch `max_open_positions` without human approval, and never deploy ≥3 key changes without human approval. The user can dial autonomy up by editing `agent.autonomy` in `config/settings.yaml` (`conservative | moderate | aggressive`).
- **`start_bot_service.ps1` requires NSSM.** It's expected to be in `tools/nssm.exe`. If missing, install NSSM or fall back to `start_bot_detached.ps1`.
- **No CI on GitHub yet.** Tests are run locally. Consider adding a GitHub Actions workflow if the user wants green-PR enforcement.

---

## 12. When the user says "make the bot smarter" / "let it learn" / "be more autonomous"

1. Check `memory/lessons/` for the Reflector's latest insight.
2. Check `memory/proposals/` for pending Evolver proposals. Surface them to the user via `python scripts\operator_status.py`.
3. Check `agent.daily_token_limit` and `agent.daily_call_limit` in `config/settings.yaml`. Raise them if the user is comfortable with cost.
4. Consider raising `agent.autonomy` from `moderate` to `aggressive` (only after a few weeks of stable `moderate`).
5. Consider widening the Evolver rails (±25% rel, ±10% abs). This is a **config change, not a code change** — but should be raised with the user first.

Do **not** silently widen risk rails, change risk presets, or remove the live-trading safety guard. The blast radius on this bot is the user's real money.

---

## 13. When the user asks "is it making money?" / "should I go live?"

1. Read `data_cache/paper_state.json` for cash + total + realized + unrealized.
2. Read `data_cache/trades_state.json` for the trade log.
3. Compare against the live-trading gates in `scripts/live_trading_gates.py` if it exists (mirror of kotak-neo-bot). If missing, do a manual check:
   - ≥ 5 consecutive paper-trading days with net-positive P&L
   - Sharpe > 1.0 over 30 days
   - Max drawdown < 10%
   - Win rate > 55%
4. **Never auto-go-live.** The user must set `DERIBIT_LIVE_CONFIRMED=YES` themselves. Tell them the gates they passed and the gates that still need work; let them decide.

---

## 14. Emergency: bot is doing something stupid

1. `python -m crypto_options_bot reset` — clears paper state. (Re-read the file first; this is destructive but reversible from `data_cache/archive/`.)
2. `.\stop_bot_service.ps1` if NSSM service.
3. `Get-Process python | Where-Object { $_.Path -like "*crypto-options-bot*" }` — find rogue processes.
4. Check `logs/bot.log` and `logs/bot_stderr.log` for the cause.
5. If the operator is involved, check `memory/journal/<today>.md` and `memory/state/sentinel_latest.json`.
6. If the operator's Evolver proposed the bad change, find it in `memory/proposals/*.json` and either let the operator roll back via the natural cycle, or manually edit `config/settings.yaml` + commit the rollback.

---

## 15. One-line TL;DR for the next session

> "It's a Deribit paper-trading bot wrapped by an optional 6-agent self-evolving / self-healing operator (LLM-driven, MiniMax). Paper-by-default. The bot does NOT depend on the operator — agent layer is enhancement. Stdlib-only HTTP. Tests: `pytest -q` → 119 passed, 1 skipped. Live trading needs `DERIBIT_LIVE_CONFIRMED=YES`."
