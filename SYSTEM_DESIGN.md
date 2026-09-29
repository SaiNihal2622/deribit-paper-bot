# Crypto Options Trading System — Strategic Design

> **Why this document exists.** The current bot evolved by gluing patches onto a Python
> monolith. That approach gave us features, but it didn't give us a *system* — the kind that
> scales, observes itself, recovers from failure, and self-evolves without exploding. This
> document is the strategic rewrite. It's the target architecture. Everything new lives here.

---

## 1. Guiding principles

These are non-negotiable. Every component below is justified by one of these.

1. **Event-driven, not polling.** No 5-second `while True` loops. Every state change is an event
   on a bus. Consumers react. Producers don't know who consumes them.
2. **Typed contracts at every boundary.** No `dict` of `dict` crossing service boundaries. Every
   event has a Pydantic / dataclass schema with a version field. Breaking the schema is a hard
   error, not a silent corruption.
3. **Observability is the default, not a feature.** Every service emits structured logs, OTel
   traces, and Prometheus metrics from day one. If you can't graph its latency, you can't
   trust its output.
4. **Recoverable, not restartable.** A crash is detected by the orchestrator (not the service
   itself). The service has a deterministic recovery protocol: replay from the last event offset,
   re-derive state, resume. We don't write "if state is corrupt, delete file" hacks.
5. **Self-evolution is a first-class service, not a cron.** Strategies don't "reload from yaml".
   They mutate under an explicit A/B-test framework that records every change, the hypothesis
   behind it, the result, and the rollback path.
6. **Risk precedes P&L.** The risk engine is on the hot path before the execution layer ever
   sees a plan. Kill switches are designed-in from day one, not added after the bleed.
7. **Local-first, cloud-ready.** The system runs on a single Windows box today. Every service
   is a process you can kill and restart without losing state. Deploying to k8s later is
   a packaging change, not an architecture change.

---

## 2. Architecture overview

```
                              ┌──────────────────────┐
                              │   Operator Dashboard  │  (humans)
                              │   + Telegram/Discord  │
                              └──────────┬───────────┘
                                         │ control plane (slow)
                                         ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │                          Event Bus (NATS / JetStream)                         │
   │  subjects: market.deribit.tick, signal.features, strategy.plan,             │
   │            risk.decision, exec.order, exec.fill, audit.*, control.*           │
   └──────────────────────────────────────────────────────────────────────────────┘
        │           │            │            │            │            │
        ▼           ▼            ▼            ▼            ▼            ▼
   ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
   │Ingest │  │ Feature│  │Strategy│  │  Risk  │  │  Exec  │  │Evolver │
   │  ⇆Rust │  │   ⇆Py │  │   ⇆Py │  │  ⇆Rust │  │  ⇆Rust │  │   ⇆Py  │
   └────┬───┘  └────┬───┘  └────┬───┘  └────┬───┘  └────┬───┘  └────┬───┘
        │          │           │           │           │           │
        ▼          ▼           ▼           ▼           ▼           ▼
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                  State Layer (QuestDB + Postgres + Object Store)          │
   │   time-series ticks/features     audit/events     parameter snapshots        │
   └──────────────────────────────────────────────────────────────────────────┘
```

**Process model.** Every box above is its own OS process. They communicate only via the event bus
and the state layer. There is no shared memory, no file-based IPC, no "just call this function
in the same module". When you add a new strategy, you spawn a new process — you don't edit the
strategy runner.

**Why event-driven.** Polling at 5s intervals means: (a) latency between signal and action,
(b) every cycle you waste CPU re-reading state, (c) coupling between consumers ("who else
is watching this?"). With NATS subjects, every event has at most one consumer per queue
group, and the bus delivers it once. Latency drops to ~1ms in-process, ~10ms cross-process.

---

## 3. Services (the components)

### 3.1 Ingestion service (`ingest/`, Rust)

**Responsibility.** Connect to Deribit (and later Binance, OKX, etc.) market data feeds,
normalize ticks into a unified schema, publish to `market.<venue>.<instrument>.tick` subjects.

**Why Rust.** Tick processing is 100% I/O bound and latency-sensitive. A Python loop on asyncio
achieves ~50µs per tick with one feed; Rust + tokio + websocket libraries hits ~5µs. That's the
difference between reacting to a 1ms price move and missing it. The Rust boundary is small:
just the WebSocket ingestion and a tick normalizer.

**Tech.** tokio, tokio-tungstenite, serde, tracing.

### 3.2 Feature engine (`features/`, Python)

**Responsibility.** Subscribe to raw ticks, compute features (DVOL, IV-rank, momentum,
micro-price, order-book imbalance, flow toxicity, regime classification), publish to
`signal.features.<symbol>`. Use a feature store so backtests don't recompute history.

**Why Python.** Most features are statistical; PyTorch / numpy / pandas handle them well.
LLM calls (for sentiment, news classification) are Python-native.

**Tech.** numpy, pandas, river (online ML), pytorch (for embeddings), prometheus_client,
opentelemetry.

### 3.3 Strategy service (`strategy/`, Python)

**Responsibility.** Subscribe to `signal.features`, produce `strategy.<id>.plan` events with a
defined R:R. Strategies are *separate processes* — each strategy gets its own queue group
on the bus, so the bus load-balances signals across strategy replicas for A/B testing.

**Each strategy is a small package.** `<strategy_id>/strategy.py` exports a class with:
- `eligible(features, state) -> bool`
- `plan(features, state) -> Optional[Plan]`
- `version: str`
- `hypothesis: str` (why this strategy should work)

That last field is critical: the evolver reads `hypothesis` to know what to test next.

**Strategies to start.** Carry over the existing 17 (with the fixes). Add new ones as we find edges:
- Volatility arbitrage: long vol when realized < implied, short when realized > implied
- Term-structure arb: calendar spreads when term-structure is steep
- Flow-following: detect large order flow via tick-size clustering
- LLM-sentiment: news + Twitter sentiment, mapped to directional bias

### 3.4 Risk engine (`risk/`, Rust)

**Responsibility.** Subscribe to `strategy.*.plan`, decide whether to allow/block/clip, publish
`risk.<strategy_id>.decision`. This is the hot path — every plan flows through here before
reaching execution.

**Why Rust.** A 100µs latency budget on risk decisions is realistic with a typed Rust
implementation. Python with asyncio is *usually* fine, but the risk engine is the wrong
place to find out about garbage collection.

**Built-in protections.**
- Drawdown circuit breaker (peak-equity tracker, pause + auto-resume)
- Per-trade risk cap (configurable per preset: aggressive / base / defensive)
- Per-strategy risk cap (don't let one strategy blow up the book)
- Correlation-based concentration limit (no two strategies with rho > 0.7 at full size)
- VaR estimate computed every N seconds; if VaR exceeds budget, scale all qty

### 3.5 Execution service (`exec/`, Rust)

**Responsibility.** Subscribe to `risk.*.decision` (filtered for `allowed=True`), place orders
on Deribit (and other venues), publish `exec.order.placed` / `exec.fill` events. Owns
position reconciliation (compare local view vs exchange view every 30s).

**Smart routing.** When the same instrument is available on multiple venues, pick the best
limit price. Track historical fill quality per venue and weight toward the best.

**Reconciliation is a continuous process, not a debug tool.** Every 30s: pull `/positions`,
diff against local positions, raise an alert + auto-correct on any discrepancy.

### 3.6 Self-evolution service (`evolver/`, Python)

**Responsibility.** Periodically propose parameter mutations, run shadow backtests against
historical data, deploy winners via the A/B framework. This is the brain of the system.

**Loop (every 30 minutes):**
1. Pull recent P&L per strategy.
2. Identify the worst-performing strategies / parameters.
3. Generate mutations: ±10% on each parameter, or LLM-driven "explain why this is underperforming
   and propose 3 alternative parameter sets".
4. Shadow-test each mutation on the last 30 days of features.
5. If shadow P&L is better by >5%, deploy to a canary (10% of capital) for 24 hours.
6. If canary is better by >5%, promote to 100%; otherwise revert.

**LLM-driven hypothesis generation.** Every 6 hours, ask the LLM:
> "Here are the last 100 trades and their outcomes. What's a new strategy hypothesis we
> should test? Output a Python class that conforms to the Strategy interface."

The output is sandboxed (no `os.system`, no `open`) before being added to the strategy registry.
The evolver manages the full lifecycle: shadow → canary → promote / kill.

### 3.7 Observability stack

- **Metrics.** Prometheus. Every service exposes `/metrics`. Key SLIs:
  - Strategy: plan generation rate, eligible rate, accepted/rejected ratio
  - Risk: decision latency p50/p95/p99, rejection reason distribution
  - Execution: order latency, fill rate, slippage per venue
  - System: event-bus lag, queue depth, replay lag
- **Logs.** Structured JSON via `tracing` → Vector → Grafana Loki.
- **Traces.** OpenTelemetry → Jaeger. Every `strategy.plan` event has a `trace_id` that propagates
  through risk → exec → fill.
- **Alerts.** Alertmanager rules:
  - Bot missing for >60s
  - Drawdown > 5%
  - Order latency > 500ms for >1 minute
  - Any service emitting errors at >0.1/s

### 3.8 State layer

- **QuestDB** — ticks, features, derived signals. Columnar, time-series, SQL. Embedded for
  single-node; clusterable for scale.
- **Postgres** — audit log, parameter snapshots, A/B test outcomes, kill-switch events.
- **S3-compatible object store** (or filesystem fallback) — trade journal snapshots, model
  weights, large exports.

---

## 4. Tech stack (concrete)

| Layer | Choice | Why |
|---|---|---|
| Languages | **Rust + Python** | Rust for hot path (ingest, risk, exec); Python for ML/strategy/evolver |
| Async runtime (Rust) | **tokio** | De facto standard; integrates with tonic (gRPC), reqwest |
| Async runtime (Py) | **asyncio + uvloop** | Standard, fast enough for strategy tier |
| Message bus | **NATS + JetStream** | Embedded mode for local dev; clusterable; ~1ms p99 |
| Time-series DB | **QuestDB** | SQL + columnar + tick-grade ingest. PostgreSQL-compatible wire protocol |
| Relational DB | **Postgres 16** | Audit log, parameter snapshots, kill-switch events |
| ML / DL | **PyTorch + scikit-learn + river** | Online + offline learning, NLP via HF transformers |
| LLM | **MiniMax / Anthropic / OpenAI** | Pluggable; multiple providers as fallback |
| Optimization | **Optuna + nevergrad** | Hyperparameter search + evolutionary strategies |
| Observability | **Prometheus + Grafana + Loki + Jaeger + OTel** | Industry standard, OSS, scales |
| Secrets | **HashiCorp Vault** | API keys, never in env vars |
| Container | **Docker / docker-compose** | Local-first; k8s-ready |
| CI/CD | **GitHub Actions + ArgoCD** | GitOps for deployments |
| IaC | **Terraform / Pulumi** | Reproducible infrastructure |

---

## 5. Migration plan (incremental, not big-bang)

The current bot is paper-trading. The migration is **side-by-side, not replace-in-place**.

### Phase 0: Stabilize current bot (1 day)
- Fix the contract_size bug (done)
- Disable bleeding strategies (done)
- Restart with clean state (done)

### Phase 1: Strangle the monolith (1 week)
- Identify the smallest service we can extract first → **ingestion**
- Replace the WebSocket loop in `data/deribit_ws.py` with a Rust process that publishes to NATS
- Existing bot subscribes to NATS instead of calling `feed.get_spot()` directly
- Validation: shadow run, compare tick streams for 24 hours

### Phase 2: Extract risk (1 week)
- Move `risk/engine.py` to a Rust service
- Bot becomes a strategy runner + thin execution adapter
- The risk service holds the kill switch state and the drawdown tracker
- Bot sends `strategy.plan` to risk → gets `risk.decision` back

### Phase 3: Time-series + replay (2 weeks)
- Add QuestDB. Every tick, every feature, every plan lands in QuestDB
- The replay engine: replay last 7 days of features through the strategy runner
- This becomes the backtester that the evolver uses

### Phase 4: Self-evolution (2 weeks)
- Build the A/B testing framework
- Build the LLM-driven strategy synthesis
- Wire evolver → strategy runner → canary deploys

### Phase 5: Multi-venue execution (2 weeks)
- Smart order routing across Deribit + OKX + Binance perp
- Cross-venue arbitrage strategies (basis trades, funding rate arb)
- This is where the system starts making real money

### Phase 6: Decommission old code (1 week)
- Old `__main__.py`, `paper_client.py`, `order_manager.py` go away
- Everything goes through the new event-driven services
- Single source of truth: NATS + QuestDB + Postgres

**Total timeline.** ~10 weeks for the rewrite. Current bot keeps paper-trading in parallel
throughout — no down-time, no risk.

---

## 6. What the rewrite UNLOCKS (and the patches couldn't)

| The patches couldn't... | The system does... |
|---|---|
| React to a 1ms price move | Rust ingest processes ticks in ~5µs; events flow at <1ms |
| Run 100 strategy variants in parallel | Each is its own process; bus load-balances signals |
| Backtest without re-running the world | Replay features from QuestDB; no re-ingestion needed |
| Self-evolve without manual restart | Strategy mutation → shadow test → canary → promote; all automated |
| Tell me WHY it made a trade | Full event trace from market data → strategy → risk → exec → fill |
| Survive a strategy bug mid-session | Risk engine blocks bad plans; kill switch + drawdown breaker auto-fires |
| Add a new exchange without surgery | New ingest service publishes to same bus; strategies subscribe unchanged |
| A/B test 50 parameter variants | Bus queue groups + canary deployment; evolver decides winner |
| Run shadow strategies on real data | Subscribe to features, produce plans but don't send to risk |
| Trust the live mode | Reconciliation runs continuously; discrepancies auto-correct + alert |

---

## 7. The honest timeline

Realistic to be honest:
- Phase 1 (Rust ingest) — 1 week of focused work, given current pace
- Phase 2 (Rust risk) — 1 week
- Phase 3 (QuestDB) — 2 weeks
- Phase 4 (self-evolution) — 2 weeks
- Total to a real, production-grade system: **6-10 weeks**

What you have right now: a paper-trading bot with 16 strategies that bleed money in
low-vol. The rewrite is the path to actually making money — but it's a real project, not a
patch.

---

## 8. Where we start

**Right now:** Phase 0 is done (bug fixed, clean state). The first concrete deliverable of
the rewrite is:

1. **`docs/ARCHITECTURE.md`** — this document, expanded with concrete service contracts
2. **`services/`** — new project structure with `ingest/`, `risk/`, `strategy/`, `exec/`,
   `evolver/` directories
3. **`services/ingest/`** — first Rust service. Subscribes to Deribit WS, publishes to NATS,
   handles the tick-to-event plumbing
4. **Bridge** — old Python bot subscribes to NATS instead of calling `feed.get_spot()` directly
5. **Validation** — shadow run for 24 hours, verify no tick loss

Let me know which part you want me to build first. My recommendation: **ingest first** —
it's the foundation, it's the smallest surface, and it's the piece that unlocks the latency
argument for the rest of the rewrite.
