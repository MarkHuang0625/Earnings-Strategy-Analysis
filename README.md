# Earnings Strategy Analysis

## 1. Project Objective

This project builds an event-driven trading framework around earnings announcements, with the goal of systematically analyzing how stocks react to earnings and identifying profitable trading patterns.

Instead of predicting earnings outcomes, the strategy focuses on **post-earnings price behavior**, using normalized signals to capture abnormal moves relative to historical volatility.

The framework is designed to:

- Generate trading signals around earnings events
- Evaluate performance across multiple parameter combinations
- Compare returns against a market benchmark (SPY)
- Identify patterns in what drives excess return

---

## 2. Strategy Logic

### (1) Execution Data

- Uses **1-minute intraday bars**
- Improves precision compared to previous 5-minute implementation
- Allows more accurate detection of early post-earnings moves

---

### (2) Pre-Earnings Volatility Baseline

Before each earnings event:

- Use the past `LOOKBACK_DAYS` daily returns
- Compute: pre_vol = std(daily returns)

This serves as a **baseline volatility level** for the stock.

---

### (3) Intraday Scanning

On the earnings reaction day:

- Start scanning from **9:45 AM ET**
- Avoid noise from opening auction (9:30–9:45)

---

### (4) Signal Construction

Define: signal_ratio = post_earnings_move / pre_earnings_volatility

Where:

- `post_earnings_move` = cumulative price change since 9:45
- `pre_earnings_volatility` = baseline volatility

---

### (5) Trading Rule

- `ratio > threshold` → **Long**
- `ratio < -threshold` → **Short**

This measures how large the move is relative to normal volatility.

---

### (6) Execution Timing

To avoid look-ahead bias:

- Signal is generated at bar close
- Entry happens at **next 1-minute bar** signal at t → trade at t+1

---

### (7) Exit Rules

The framework tests both:

#### Intraday holding:
- 5, 15, 30, 60, 120, 180, 390 minutes

#### Multi-day holding:
- 1, 3, 5, 10 days

With optional:
- Stop loss
- Profit target

---

## 3. Code Structure

The main script (`Earnings Analysis.py`) is organized into several components:

### Data Layer

- Pulls:
  - Earnings data
  - Daily OHLC
  - Intraday bars
- Uses caching to reduce API calls

---

### Signal Engine

- Computes pre-earnings volatility
- Tracks intraday move from 9:45
- Generates normalized signal (ratio)

---

### Backtesting Engine

- Iterates over:
  - Thresholds
  - Stop-loss / profit-target combinations
  - Holding periods
- Simulates:
  - Entry (next bar)
  - Exit (time / SL / PT)

---

### Performance Evaluation

For each strategy combination:

- Stock return
- Market return (SPY over same window)
- Excess return

Outputs:

- Trade-level data
- Strategy-level aggregation
- Top 5 configurations per ticker

---

## 4. Results Analysis

The following plots summarize **top 5 strategies per ticker**:

---

### (1) Holding Time vs Excess Return

![Holding](Output/top5_holding_time_vs_excess_return.png)

Key observations:

- Strong clustering in **multi-day strategies (3–10 days)**
- Intraday strategies rarely appear in top performers
- Suggests that earnings effects **persist beyond the same day**

---

### (2) Profit Target vs Excess Return

![PT](Output/top5_profit_target_vs_excess_return.png)

Observations:

- Higher profit targets (0.1–0.2) appear more frequently
- Indicates large post-earnings moves often **continue rather than revert**

---

### (3) Stop Loss vs Excess Return

![SL](Output/top5_stop_loss_vs_excess_return.png)

Observations:

- Moderate stop losses (0.05–0.2) dominate
- Very tight stops reduce performance (likely noise-driven exits)

---

### (4) Threshold vs Excess Return

![Threshold](Output/top5_threshold_vs_excess_return.png)

Observations:

- Higher thresholds (1.0–1.5) perform better
- Implies filtering for **strong signals** improves results
- Weak signals are mostly noise

---

## 5. Limitations

- Uses **bar data instead of trade data**
  - May miss intrabar price dynamics
- Signal only uses **price-based volatility**
  - No fundamentals or earnings surprise
- No **transaction cost / slippage modeling**
- Grid search is brute-force (not optimized)
- Limited ticker universe

---

## 6. Future Improvements

### (1) Upgrade to Trade-Level Data
- Capture more precise execution and microstructure effects
- Currently limited by memory and performance constraints

---

### (2) Improve Volatility Estimation

Current: std(daily returns)

Potential upgrades:
- Realized intraday volatility
- EWMA volatility
- Regime-adjusted volatility

---

### (3) Better Signal Design

- Incorporate:
  - Earnings surprise
  - Volume spikes
  - Gap size vs expected move
- Separate **continuation vs reversal regimes**

---

### (4) Execution Optimization

- Dynamic entry (not fixed 9:45 start)
- VWAP-based execution
- Adaptive holding period

---

### (5) Portfolio Construction

- Combine signals across tickers
- Risk-adjusted allocation
- Cross-sectional ranking

---

## 7. Summary

This project evolves from a simple pre-earnings signal into a **post-earnings volatility-normalized strategy**.

The key insight is:

> Earnings reactions should be evaluated relative to normal volatility, not in absolute terms.

The framework provides a flexible foundation for further research into:

- Event-driven trading
- Volatility-normalized signals
- Intraday vs multi-day dynamics
