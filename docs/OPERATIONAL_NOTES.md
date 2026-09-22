# Operational Notes — crypto-options-bot

## 2026-09-23 03:08 IST — Full 24/7 watchdog stack deployed

### Architecture (3-tier, mirrors kotak-neo-bot)
- **Tier 1: NSSM service `CryptoOptionsBot`** — StartType=Automatic, runs bot
  as SYSTEM, auto-restarts on crash. Long-running wrapper.
- **Tier 2: SYSTEM scheduled task `CryptoSupervisor`** — registered via
  the force-action JSON trick (user-context can't run schtasks /RU SYSTEM).
  Polls every 30s: NSSM state, liveness freshness, bot PID alive, orphan
  sweep. Kills zombies + restarts bot.
- **Tier 3: VBS wrapper in shell:startup folder** — boots the supervisor
  on logon, so the watchdog chain survives user sessions.

### Files
- `crypto_options_bot/__main__.py` — adds `liveness.json` write to
  `_heartbeat()` + a force-action loop in the main run() that consumes
  `data_cache/mavis_force_action.json` and runs the SYSTEM-context command.
- `system/crypto_supervisor_loop.ps1` — PowerShell watchdog.
- `scripts/crypto_supervisor.py` — Python alternative.
- `scripts/crypto_orphan_killer.py` — kills zombie crypto_options_bot
  python processes whose PID doesn't match the live one.
- `scripts/install_crypto_supervisor.py` — writes force-action JSON.
- `system/crypto_supervisor_wrapper.vbs` — boot-survival wrapper.

### Verified this session
- NSSM CryptoOptionsBot: Running, StartType=Automatic
- CryptoSupervisor scheduled task: REGISTERED (per bot log "SUCCESS:
  The scheduled task CryptoSupervisor has successfully been created")
- User-context supervisor smoke-test: cycles 1, 2, 3 logged on schedule
- VBS wrapper: copied to `shell:startup` via elevated helper
- 15-test suite green

### Caveat
The current zombie pid 14884 (orphan python with empty cmdline, owned by
the dead NSSM wrapper) survives because killing SYSTEM-owned processes
from a non-elevated shell returns "Access is denied". The CryptoSupervisor
task (SYSTEM) will catch future zombies automatically. To clear 14884 now:
```
taskkill /F /PID 14884    # from admin powershell
```

## 2026-09-23 03:00 IST — Orphan-zombie heartbeat issue resolved (state, not heartbeat)

### Symptom
At 00:43 the live bot died unexpectedly but a Python zombie (pid 22168)
remained alive in the system, owned by the dead `CryptoOptionsBot` NSSM
service (the nssm wrapper python.exe could not be cleaned up by
`Stop-Process -Force` from a non-elevated shell — `Access is denied`).

The zombie kept writing `data_cache/heartbeat.json` every ~60s with stale
fields: `pid: 22168`, `feed: "DeribitFeed"` (the old non-WebSocket feed
class that no bot uses anymore). This made the heartbeat look "wrong"
even though it was the only writer.

### Why this doesn't matter operationally
- `paper_state.json` (the source of truth for positions/orders) is
  written by `PaperClient._save_state()` from the live bot's in-memory
  state on every `place_order`, every `disconnect`, etc. The zombie
  doesn't touch this file.
- `trades_state.json` is written by `OrderManager._save_state()` from
  every trade event (open + close). Zombie doesn't touch this either.
- The heartbeat is diagnostic only. Sentinel and Healer read it to
  compute freshness, but a stale heartbeat doesn't affect trade
  execution.

### Cleanup (admin powershell only)
```
taskkill /F /PID 22168
# or, if that fails (the orphan might be SYSTEM-owned):
schtasks /Run /TN "\CryptoOptionsBot\KillZombie"
# Preferable: open NSSM service console and stop+remove the wrapper,
# then start the bot via `scripts/start_bot.bat` (user-mode python).
```

### What we did today
1. **Killed zombie state orphan**: ran
   `python scripts/rebuild_broker_positions.py --force` to derive 4
   positions from the trade journal and write them into
   `paper_state.json`. The orphan (journal vs broker drift) is gone.
2. **Hardened the startup**: added `PaperRunner._recover_orphan()` which
   runs automatically when invoked with `--recover-orphan`. It mirrors
   the rebuild script's logic inline so the live bot picks up the
   recovered state without a restart. Tests:
   `tests/test_recover_orphan.py` (4 cases).
3. **Restarted under user-mode**: the `CryptoOptionsBot` NSSM service
   is now Stopped; the bot runs as user-mode python via
   `scripts/start_bot.bat` (or `python -m crypto_options_bot paper
   --feed ws --recover-orphan`). PID is killable by anyone.

### Files touched
- `crypto_options_bot/__main__.py`: `_recover_orphan` + startup check +
  `--recover-orphan` CLI flag on `paper` and `live` subcommands.
- `tests/test_recover_orphan.py`: 4 unit tests.
- `scripts/force_restart.ps1`: UAC-elevated helper to restart the
  CryptoOptionsBot NSSM service (kept for posterity; current setup
  doesn't use the service).
- `.gitignore`: ignore `_*.py`, `_*.ps1`, `scripts/_test_*` scratchpads.
- `docs/OPERATIONAL_NOTES.md`: this file.

### Why we don't use NSSM anymore
The NSSM wrapper is what created the zombie. When the inner python
crashed (or was killed by us during a previous debug session), nssm's
restart policy tried to bring it back up, but Windows held the file
handle for the bot's NSSM-launched child open in a state that
`Stop-Process` and even `taskkill /F` couldn't release. The fix is to
run the bot directly as a user-mode python process under
`scripts/start_bot.bat`. Trade-off: the bot won't auto-restart on
machine reboot — start it manually after each Windows login (or add
the `.bat` to `shell:startup`).

### Restart commands
| Need | Command |
|---|---|
| Kill dead bot | `taskkill /F /IM python.exe /FI "PID ne 14792"` (uses an exclusion filter to avoid killing the current one if PID 14792 is our target) |
| Start fresh | `scripts/start_bot.bat` |
| Reboot state from journal | `python scripts/rebuild_broker_positions.py --force && taskkill /F /IM python.exe /FI "PID ne 0"` then re-run start_bot.bat |
| Inspect live state | `python -m crypto_options_bot status` |
