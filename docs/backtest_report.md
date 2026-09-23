# Crypto-Options-Bot Backtest — last 90 days

**Generated:** 2026-09-23 05:55 UTC

Compares each production strategy against buy-and-hold BTC and ETH
on the same historical window. Option P&L uses Black-Scholes synthetic
pricing driven by Deribit's DVOL index (the production bot's regime gate).

**Caveat:** this is a regime-test, not a faithful execution replay. The
production bot has features we don't simulate here: LLM gate, dedupe,
slippage model, market impact, regime adaptive sizing. The signal rules
(min_iv_rank, max_dvol, wing sizing, target/stop, expiry) ARE mirrored.

## Summary table

| Strategy / B&H | Trades | Win % | Total P&L | Sharpe | Max DD | vs B&H 
|---|---|---|---|---|---|---|
| **BTC short_strangle** | 24 | 79% | $-116 | -0.53 | $3,321 | ❌ loses B&H by $66,887 |
| **ETH short_strangle** | 22 | 82% | $-20 | -0.56 | $268 | ❌ loses B&H by $66,791 |
| B&H BTC | 1 | 100% | $+40,100 (+40.1%) | 3.81 | $7,923 | — |
| B&H ETH | 1 | 100% | $+66,771 (+66.8%) | 4.21 | $7,848 | — |

## Per-strategy detail

### BTC short_strangle

- Trades: **24** (19 W / 4 L / 1 scratch)
- Total credit collected: **$13,579.95**
- Total P&L (realized): **$-115.93**
- Sharpe (annualized): **-0.53**
- Max drawdown: **$3,320.84**
- Avg P&L per trade: **$-4.83**
- Win rate: **79.2%**

### ETH short_strangle

- Trades: **22** (18 W / 4 L / 0 scratch)
- Total credit collected: **$736.86**
- Total P&L (realized): **$-19.89**
- Sharpe (annualized): **-0.56**
- Max drawdown: **$267.51**
- Avg P&L per trade: **$-0.90**
- Win rate: **81.8%**

## Buy-and-hold benchmarks

### Buy & Hold BTC

- Entry: $61,790.00
- Exit:  $86,568.00
- Return: **$+40,100.34 (+40.1%)**
- Sharpe: 3.81
- Max drawdown: $7,922.80

### Buy & Hold ETH

- Entry: $1,655.20
- Exit:  $2,760.40
- Return: **$+66,771.39 (+66.8%)**
- Sharpe: 4.21
- Max drawdown: $7,847.99

## How to read this

- **Sharpe** = annualized risk-adjusted return. >1 is decent, >2 is great.
- **Max DD** = peak-to-trough equity drop on the bot's daily mark.
- **vs B&H** = how much the strategy beat (or lost to) buy-and-hold on
  the same window, in dollar terms.
- A strategy that **loses less than buy-and-hold during a bull run**
  is actually winning on risk-adjusted basis (look at Sharpe).
- For a SHORT-PREMIUM strategy, the goal is **consistent positive
  income in sideways markets**, not directional gains.

## Reproduce

```bash
python scripts/backtest.py --days 90
```
