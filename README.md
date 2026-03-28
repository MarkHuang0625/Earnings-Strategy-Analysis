# Earnings-Strategy-Analysis
1. Project Objective

This project builds an event-driven trading strategy around earnings announcements and systematically evaluates its performance using historical data.

The core idea is:
	•	Use pre-earnings price behavior to generate a signal
	•	Take long/short positions around earnings
	•	Apply intraday / multi-day holding strategies
	•	Evaluate performance relative to a market benchmark (SPY)

The goal is not just to test a single strategy, but to:
	•	Explore a large parameter space (threshold, stop-loss, profit-target, holding time)
	•	Identify robust patterns across multiple tickers
	•	Understand how execution design impacts returns

⸻

2. Script Structure Overview

The main script:
Earnings Analysis.py

(1) Data Layer
	•	MassiveClient
	•	Pulls:
	•	Earnings events
	•	Daily prices
	•	Intraday bars (5-min)
	•	Uses chunking to avoid API limits

(2) Signal Construction
python compute_pre_earnings_score()
	•	Computes a Sharpe-like signal:
  python score = cumulative_return / (volatility * sqrt(window))
  •	Based on last LOOKBACK_DAYS before earnings
  python classify_trade()
  •	Converts score → trading signal:
	  •	Long if > threshold
	  •	Short if < -threshold
