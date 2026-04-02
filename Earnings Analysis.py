from __future__ import annotations

import os
import time
import requests
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Tuple

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pytz

warnings.filterwarnings("ignore")

ET = pytz.timezone("America/New_York")

# =========================
# Configuration
# =========================

# NOTE:
# This script performs an earnings-based event-driven backtest.
# Core idea:
# 1. Compute a pre-earnings volatility baseline from recent daily returns
# 2. On the earnings reaction day, start monitoring 1-minute bars from 09:45 ET
# 3. Build a signed reaction ratio = post-earnings move / pre-earnings volatility
# 4. Trigger long/short entries once the signed ratio crosses a threshold
# 5. Apply stop-loss / profit-target rules and compare against market benchmark (SPY)

API_KEY = "VxaAMkftmr39HPfhG2gQbpEACCd6b8Zo"
BASE_URL = "https://api.massive.com"

TICKERS = ["TMUS", "NVDA", "AAPL", "FDX", "PL", "FLY", "ORLA", "XPEV"]
MARKET_TICKER = "SPY"

START_DATE = "2022-01-01"
END_DATE = "2025-12-31"

THRESHOLDS = [0.5, 1.0, 1.5]
STOP_LOSS_PCTS = [None, 0.01, 0.03, 0.05, 0.1, 0.2]
PROFIT_TARGET_PCTS = [None, 0.01, 0.03, 0.05, 0.1, 0.2]
INTRADAY_HOLD_MINUTES = [5, 15, 30, 60, 120, 180, 390]
MULTIDAY_HOLD_DAYS = [1, 3, 5, 10]

LOOKBACK_DAYS = 10
MIN_DAILY_HISTORY = 40
MAX_EARNINGS = 1500
BAR_MINUTES = 1
INTRADAY_CHUNK_YEARS = 1

SIGNAL_START_TIME = "09:45:00"
MIN_INTRADAY_SIGNAL_BARS = 5


# =========================
# Helper dataclasses
# =========================

@dataclass
class TradeResult:
    ticker: str
    event_date: pd.Timestamp
    side: int
    threshold: float
    pre_vol: float
    signal_ratio: float
    hold_type: str
    hold_value: int
    stop_loss: Optional[float]
    profit_target: Optional[float]
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_px: float
    exit_px: float
    stock_ret: float
    market_ret: float
    excess_ret: float
    exit_reason: str


# =========================
# API client
# =========================

class MassiveClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("Missing MASSIVE_API_KEY environment variable.")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{BASE_URL}{path}"
        r = self.session.get(url, params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def get_earnings(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        limit: int = 50000,
    ) -> pd.DataFrame:
        params = {
            "ticker.any_of": ticker,
            "date.gte": start_date,
            "date.lte": end_date,
            "limit": min(limit, 50000),
            "sort": "date.asc",
        }
        data = self._get("/benzinga/v1/earnings", params=params)
        rows = data.get("results", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        keep_cols = [
            "ticker",
            "company_name",
            "date",
            "time",
            "importance",
            "eps_surprise_percent",
            "revenue_surprise_percent",
            "date_status",
        ]
        keep_cols = [c for c in keep_cols if c in df.columns]
        df = df[keep_cols].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.normalize()
        df = df.sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")
        return df.reset_index(drop=True)

    def get_aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
        limit: int = 50000,
    ) -> pd.DataFrame:
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": limit,
        }

        data = self._get(path, params=params)
        rows = data.get("results", [])
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        rename_map = {
            "o": "open",
            "h": "high",
            "l": "low",
            "c": "close",
            "v": "volume",
            "vw": "vwap",
            "t": "timestamp",
        }
        df = df.rename(columns=rename_map)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_convert(ET).dt.tz_localize(None)
        df["date"] = df["datetime"].dt.tz_localize(None).dt.normalize()
        df["ticker"] = ticker
        return df[["ticker", "datetime", "date", "open", "high", "low", "close", "volume"]].copy()

    def get_aggs_chunked(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
        limit: int = 50000,
    ) -> pd.DataFrame:
        start = pd.Timestamp(from_date)
        end = pd.Timestamp(to_date)
        pieces = []
        chunk_start = start

        while chunk_start <= end:
            chunk_end = min(chunk_start + pd.DateOffset(years=INTRADAY_CHUNK_YEARS) - pd.Timedelta(days=1), end)
            df = self.get_aggs(
                ticker=ticker,
                multiplier=multiplier,
                timespan=timespan,
                from_date=chunk_start.strftime("%Y-%m-%d"),
                to_date=chunk_end.strftime("%Y-%m-%d"),
                adjusted=adjusted,
                limit=limit,
            )
            if not df.empty:
                pieces.append(df)
            chunk_start = chunk_end + pd.Timedelta(days=1)

        if not pieces:
            return pd.DataFrame()

        out = pd.concat(pieces, ignore_index=True)
        out = out.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        return out


# =========================
# Signal construction
# =========================

def compute_pre_earnings_volatility(
    daily_df: pd.DataFrame,
    event_date: pd.Timestamp,
    lookback_days: int = 10,
) -> Optional[float]:
    hist = daily_df[daily_df["date"] < event_date].sort_values("date").tail(lookback_days + 1)
    if len(hist) < lookback_days + 1:
        return None

    closes = hist["close"].astype(float).values
    rets = pd.Series(closes).pct_change().dropna()
    if len(rets) < lookback_days:
        return None

    vol = rets.std(ddof=1)
    if vol is None or np.isnan(vol) or vol <= 0:
        return None
    return float(vol)


# =========================
# Event timing helpers
# =========================

def parse_earnings_time(event_date: pd.Timestamp, time_str: Optional[str]) -> Tuple[pd.Timestamp, str]:
    if not time_str or pd.isna(time_str):
        event_dt = datetime.combine(event_date.date(), datetime.strptime("08:00:00", "%H:%M:%S").time())
        return pd.Timestamp(event_dt), "pre"

    hhmmss = str(time_str)
    naive = datetime.combine(event_date.date(), datetime.strptime(hhmmss, "%H:%M:%S").time())
    event_dt = pd.Timestamp(naive)
    t = event_dt.time()

    if t < datetime.strptime("09:30:00", "%H:%M:%S").time():
        session = "pre"
    elif t > datetime.strptime("16:00:00", "%H:%M:%S").time():
        session = "post"
    else:
        session = "during"

    return event_dt, session


def get_entry_day(event_date: pd.Timestamp, session_type: str, daily_df: pd.DataFrame) -> Optional[pd.Timestamp]:
    trading_days = daily_df["date"].drop_duplicates().sort_values().tolist()
    if session_type in ("pre", "during"):
        return event_date if event_date in trading_days else None

    future_days = [d for d in trading_days if d > event_date]
    return future_days[0] if future_days else None


# New helper: get_signal_start_ts
def get_signal_start_ts(event_date: pd.Timestamp, session_type: str, event_ts: pd.Timestamp) -> pd.Timestamp:
    default_start = pd.Timestamp(
        datetime.combine(event_date.date(), datetime.strptime(SIGNAL_START_TIME, "%H:%M:%S").time())
    )
    if session_type == "during":
        return max(default_start, event_ts)
    return default_start


# =========================
# Bar helpers
# =========================

def get_regular_session_bars(df: pd.DataFrame, trade_date: pd.Timestamp) -> pd.DataFrame:
    day = df[df["date"] == trade_date].copy()
    if day.empty:
        return day

    start = pd.Timestamp(datetime.combine(trade_date.date(), datetime.strptime("09:30:00", "%H:%M:%S").time()))
    end = pd.Timestamp(datetime.combine(trade_date.date(), datetime.strptime("16:00:00", "%H:%M:%S").time()))
    day = day[(day["datetime"] >= start) & (day["datetime"] <= end)].copy()
    return day.sort_values("datetime").reset_index(drop=True)



def first_bar_at_or_after(df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    out = df[df["datetime"] >= ts].sort_values("datetime")
    return None if out.empty else out.iloc[0]


def nearest_bar_at_or_after(df: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    out = df[df["datetime"] >= ts].sort_values("datetime")
    return None if out.empty else out.iloc[0]


def first_bar_of_day(df: pd.DataFrame, trade_date: pd.Timestamp) -> Optional[pd.Series]:
    day = get_regular_session_bars(df, trade_date)
    return None if day.empty else day.iloc[0]


def last_bar_of_day(df: pd.DataFrame, trade_date: pd.Timestamp) -> Optional[pd.Series]:
    day = get_regular_session_bars(df, trade_date)
    return None if day.empty else day.iloc[-1]


# New function: find_signal_entry_from_ratio
def find_signal_entry_from_ratio(
    day_bars: pd.DataFrame,
    signal_start_ts: pd.Timestamp,
    pre_vol: float,
    threshold: float,
) -> Optional[Tuple[pd.Timestamp, int, float]]:
    if day_bars.empty or pre_vol is None or np.isnan(pre_vol) or pre_vol <= 0:
        return None

    ref_bar = first_bar_at_or_after(day_bars, signal_start_ts)
    if ref_bar is None:
        return None

    ref_dt = ref_bar["datetime"]
    ref_px = float(ref_bar["open"])
    if ref_px <= 0:
        return None

    path = day_bars[day_bars["datetime"] >= ref_dt].copy().sort_values("datetime")
    if len(path) < MIN_INTRADAY_SIGNAL_BARS:
        return None

    for i, (_, row) in enumerate(path.iterrows()):
        if i + 1 < MIN_INTRADAY_SIGNAL_BARS:
            continue

        move = float(row["close"]) / ref_px - 1
        ratio = move / pre_vol

        if ratio >= threshold:
            next_bar = day_bars[day_bars["datetime"] > row["datetime"]].sort_values("datetime").head(1)
            if next_bar.empty:
                return None
            return next_bar.iloc[0]["datetime"], 1, float(ratio)

        if ratio <= -threshold:
            next_bar = day_bars[day_bars["datetime"] > row["datetime"]].sort_values("datetime").head(1)
            if next_bar.empty:
                return None
            return next_bar.iloc[0]["datetime"], -1, float(ratio)

    return None



def compute_market_return(
    market_intraday: pd.DataFrame,
    market_daily: pd.DataFrame,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
) -> float:
    # Market return logic:
    # If entry and exit are on the same day, use SPY intraday bars over the same interval.
    # Example: if the stock trade runs from 09:30 to 12:30, use SPY from the first bar at/after 09:30
    # to the first bar at/after 12:30.
    # Otherwise, use daily open/close approximation.
    if market_daily.empty:
        return 0.0

    entry_date = pd.Timestamp(entry_ts).normalize()
    exit_date = pd.Timestamp(exit_ts).normalize()

    if entry_date == exit_date and not market_intraday.empty:
        day = get_regular_session_bars(market_intraday, entry_date)
        if not day.empty:
            e = first_bar_at_or_after(day, entry_ts)
            x = nearest_bar_at_or_after(day, exit_ts)

            if e is None:
                e = day.iloc[0]
            if x is None:
                x = day.iloc[-1]

            if x["datetime"] < e["datetime"]:
                x = e

            return float(x["close"] / e["open"] - 1)

    # Fallback for multi-day trades: use daily benchmark return over the same holding dates.
    e_row = market_daily[market_daily["date"] <= entry_date].tail(1)
    x_row = market_daily[market_daily["date"] <= exit_date].tail(1)
    if e_row.empty or x_row.empty:
        return 0.0

    return float(x_row.iloc[-1]["close"] / e_row.iloc[0]["open"] - 1)


# =========================
# Backtest logic using bars
# =========================

def apply_intraday_sl_pt_from_bars(
    day_bars: pd.DataFrame,
    entry_ts: pd.Timestamp,
    side: int,
    stop_loss: Optional[float],
    profit_target: Optional[float],
    max_hold_minutes: int,
) -> Tuple[pd.Timestamp, float, str]:
    if day_bars.empty:
        raise ValueError("day_bars is empty")

    entry_row = first_bar_at_or_after(day_bars, entry_ts)
    if entry_row is None:
        raise ValueError("No entry bar found")

    entry_px = float(entry_row["open"])
    max_exit_time = entry_ts + pd.Timedelta(minutes=max_hold_minutes)

    path = day_bars[day_bars["datetime"] >= entry_row["datetime"]].copy()
    path = path[path["datetime"] <= max_exit_time]
    if path.empty:
        path = day_bars.tail(1)

    # Iterate through bars and check if SL/PT is hit using high/low
    for _, row in path.iterrows():
        high_px = float(row["high"])
        low_px = float(row["low"])

        if side == 1:
            if profit_target is not None and (high_px / entry_px - 1) >= profit_target:
                return row["datetime"], entry_px * (1 + profit_target), "profit_target"
            if stop_loss is not None and (low_px / entry_px - 1) <= -stop_loss:
                return row["datetime"], entry_px * (1 - stop_loss), "stop_loss"
        else:
            if profit_target is not None and (entry_px / low_px - 1) >= profit_target:
                return row["datetime"], entry_px * (1 - profit_target), "profit_target"
            if stop_loss is not None and (entry_px / high_px - 1) <= -stop_loss:
                return row["datetime"], entry_px * (1 + stop_loss), "stop_loss"

    last_row = path.iloc[-1]
    return last_row["datetime"], float(last_row["close"]), "time_exit"


def apply_multiday_sl_pt_from_bars(
    intraday_bars: pd.DataFrame,
    daily_df: pd.DataFrame,
    entry_date: pd.Timestamp,
    side: int,
    stop_loss: Optional[float],
    profit_target: Optional[float],
    hold_days: int,
) -> Tuple[pd.Timestamp, float, str]:
    entry_bar = first_bar_of_day(intraday_bars, entry_date)
    if entry_bar is None:
        raise ValueError(f"No entry bar for {entry_date}")

    entry_px = float(entry_bar["open"])
    trading_days = daily_df["date"].drop_duplicates().sort_values().tolist()
    holding_window = [d for d in trading_days if d >= entry_date][:hold_days]
    if not holding_window:
        raise ValueError("No holding_window found")

    for d in holding_window:
        day = get_regular_session_bars(intraday_bars, d)
        if day.empty:
            continue

        for _, row in day.iterrows():
            high_px = float(row["high"])
            low_px = float(row["low"])

            if side == 1:
                if profit_target is not None and (high_px / entry_px - 1) >= profit_target:
                    return row["datetime"], entry_px * (1 + profit_target), "profit_target"
                if stop_loss is not None and (low_px / entry_px - 1) <= -stop_loss:
                    return row["datetime"], entry_px * (1 - stop_loss), "stop_loss"
            else:
                if profit_target is not None and (entry_px / low_px - 1) >= profit_target:
                    return row["datetime"], entry_px * (1 - profit_target), "profit_target"
                if stop_loss is not None and (entry_px / high_px - 1) <= -stop_loss:
                    return row["datetime"], entry_px * (1 + stop_loss), "stop_loss"

    last_day = holding_window[-1]
    exit_bar = last_bar_of_day(intraday_bars, last_day)
    if exit_bar is not None:
        return exit_bar["datetime"], float(exit_bar["close"]), "time_exit"

    row = daily_df[daily_df["date"] == last_day].sort_values("date")
    if row.empty:
        raise ValueError("No exit data found")

    exit_px = float(row.iloc[-1]["close"])
    exit_dt = pd.Timestamp(datetime.combine(last_day.date(), datetime.strptime("16:00:00", "%H:%M:%S").time()))
    return exit_dt, exit_px, "time_exit"


# =========================
# Analytics
# =========================

def summarize_results(trades: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()

    def max_drawdown(x: pd.Series) -> float:
        if len(x) == 0:
            return np.nan
        cum = (1 + x.fillna(0)).cumprod()
        peak = cum.cummax()
        dd = cum / peak - 1
        return float(dd.min())

    out = (
        trades.groupby(group_cols)
        .agg(
            n=("stock_ret", "size"),
            avg_stock_ret=("stock_ret", "mean"),
            med_stock_ret=("stock_ret", "median"),
            win_rate=("stock_ret", lambda s: (s > 0).mean()),
            avg_excess_ret=("excess_ret", "mean"),
            med_excess_ret=("excess_ret", "median"),
            excess_win_rate=("excess_ret", lambda s: (s > 0).mean()),
            std_stock_ret=("stock_ret", "std"),
            std_excess_ret=("excess_ret", "std"),
        )
        .reset_index()
    )

    out["stock_sharpe_like"] = out["avg_stock_ret"] / out["std_stock_ret"].replace(0, np.nan)
    out["excess_sharpe_like"] = out["avg_excess_ret"] / out["std_excess_ret"].replace(0, np.nan)

    dd_rows = []
    for keys, g in trades.groupby(group_cols):
        g = g.sort_values("exit_ts")
        if not isinstance(keys, tuple):
            keys = (keys,)
        dd_rows.append(tuple(list(keys) + [max_drawdown(g["stock_ret"]), max_drawdown(g["excess_ret"])]))

    dd_cols = group_cols + ["max_dd_stock", "max_dd_excess"]
    dd_df = pd.DataFrame(dd_rows, columns=dd_cols)
    out = out.merge(dd_df, on=group_cols, how="left")
    return out


# =========================
# Main analysis engine
# =========================

class EarningsStrategyAnalyzer:
    def __init__(self, client: MassiveClient):
        self.client = client
        self.daily_cache: Dict[str, pd.DataFrame] = {}
        self.intraday_cache: Dict[str, pd.DataFrame] = {}

    def load_prices_for_ticker(self, ticker: str):
        if ticker not in self.daily_cache:
            daily = self.client.get_aggs(ticker, 1, "day", START_DATE, END_DATE)
            self.daily_cache[ticker] = daily.sort_values("datetime").reset_index(drop=True) if not daily.empty else pd.DataFrame()

        if ticker not in self.intraday_cache:
            intraday = self.client.get_aggs_chunked(ticker, BAR_MINUTES, "minute", START_DATE, END_DATE)
            self.intraday_cache[ticker] = intraday.sort_values("datetime").reset_index(drop=True) if not intraday.empty else pd.DataFrame()

    def prepare_data(self, ticker: str) -> pd.DataFrame:
        earnings = self.client.get_earnings(ticker, START_DATE, END_DATE)
        if earnings.empty:
            raise ValueError(f"No earnings events found for {ticker}.")

        earnings = earnings.head(MAX_EARNINGS).copy()

        needed = [ticker, MARKET_TICKER]
        for i, t in enumerate(needed, 1):
            label = "stock prices" if t == ticker else "benchmark prices"
            print(f"[{i}/{len(needed)}] Loading {label} for {t} ...")
            self.load_prices_for_ticker(t)
            daily_rows = len(self.daily_cache.get(t, pd.DataFrame()))
            intraday_rows = len(self.intraday_cache.get(t, pd.DataFrame()))
            print(f"    loaded {daily_rows} daily rows and {intraday_rows} intraday rows for {t}")
            time.sleep(0.1)

        return earnings

    def backtest(self, ticker: str, earnings_df: pd.DataFrame) -> pd.DataFrame:
        all_trades: List[TradeResult] = []

        market_daily = self.daily_cache.get(MARKET_TICKER, pd.DataFrame())
        market_intraday = self.intraday_cache.get(MARKET_TICKER, pd.DataFrame())
        daily = self.daily_cache.get(ticker, pd.DataFrame())
        intraday = self.intraday_cache.get(ticker, pd.DataFrame())
        signal_count = 0
        trade_attempt_count = 0

        # If either stock or market data is missing, no trades will be generated
        if daily.empty or intraday.empty or len(daily) < MIN_DAILY_HISTORY:
            return pd.DataFrame()

        for idx, row in earnings_df.iterrows():
            event_date = pd.Timestamp(row["date"]).normalize()
            event_time = row["time"] if "time" in row else None

            pre_vol = compute_pre_earnings_volatility(daily, event_date, lookback_days=LOOKBACK_DAYS)
            if pre_vol is None or np.isnan(pre_vol) or pre_vol <= 0:
                continue

            event_ts, session_type = parse_earnings_time(event_date, event_time)
            entry_date = get_entry_day(event_date, session_type, daily)
            if entry_date is None:
                continue

            entry_day_bars = get_regular_session_bars(intraday, entry_date)
            if entry_day_bars.empty:
                continue

            signal_start_ts = get_signal_start_ts(entry_date, session_type, event_ts)

            for threshold in THRESHOLDS:
                signal_info = find_signal_entry_from_ratio(
                    entry_day_bars,
                    signal_start_ts,
                    pre_vol,
                    threshold,
                )
                if signal_info is None:
                    continue

                entry_ts, side, signal_ratio = signal_info
                signal_count += 1

                # Intraday strategy: enter and exit within same trading day
                for hold_min in INTRADAY_HOLD_MINUTES:
                    for sl in STOP_LOSS_PCTS:
                        for pt in PROFIT_TARGET_PCTS:
                            try:
                                trade_attempt_count += 1
                                entry_bar = first_bar_at_or_after(entry_day_bars, entry_ts)
                                if entry_bar is None:
                                    continue

                                exit_ts, exit_px, reason = apply_intraday_sl_pt_from_bars(
                                    entry_day_bars,
                                    entry_ts,
                                    side,
                                    sl,
                                    pt,
                                    hold_min,
                                )
                                entry_px = float(entry_bar["open"])
                                stock_ret = side * (exit_px / entry_px - 1)
                                market_ret = compute_market_return(market_intraday, market_daily, entry_bar["datetime"], exit_ts)
                                excess_ret = stock_ret - market_ret

                                all_trades.append(
                                    TradeResult(
                                        ticker=ticker,
                                        event_date=event_date,
                                        side=side,
                                        threshold=threshold,
                                        pre_vol=pre_vol,
                                        signal_ratio=signal_ratio,
                                        hold_type="intraday",
                                        hold_value=hold_min,
                                        stop_loss=sl,
                                        profit_target=pt,
                                        entry_ts=entry_bar["datetime"],
                                        exit_ts=exit_ts,
                                        entry_px=entry_px,
                                        exit_px=exit_px,
                                        stock_ret=stock_ret,
                                        market_ret=market_ret,
                                        excess_ret=excess_ret,
                                        exit_reason=reason,
                                    )
                                )
                            except Exception:
                                continue

                # Multi-day strategy: hold position across multiple trading days
                for hold_days in MULTIDAY_HOLD_DAYS:
                    for sl in STOP_LOSS_PCTS:
                        for pt in PROFIT_TARGET_PCTS:
                            try:
                                trade_attempt_count += 1
                                entry_bar = first_bar_of_day(entry_day_bars, entry_date)
                                if entry_bar is None:
                                    continue

                                exit_ts, exit_px, reason = apply_multiday_sl_pt_from_bars(
                                    intraday,
                                    daily,
                                    entry_date,
                                    side,
                                    sl,
                                    pt,
                                    hold_days,
                                )
                                entry_px = float(entry_bar["open"])
                                stock_ret = side * (exit_px / entry_px - 1)
                                market_ret = compute_market_return(market_intraday, market_daily, entry_bar["datetime"], exit_ts)
                                excess_ret = stock_ret - market_ret

                                all_trades.append(
                                    TradeResult(
                                        ticker=ticker,
                                        event_date=event_date,
                                        side=side,
                                        threshold=threshold,
                                        pre_vol=pre_vol,
                                        signal_ratio=signal_ratio,
                                        hold_type="multiday",
                                        hold_value=hold_days,
                                        stop_loss=sl,
                                        profit_target=pt,
                                        entry_ts=entry_bar["datetime"],
                                        exit_ts=exit_ts,
                                        entry_px=entry_px,
                                        exit_px=exit_px,
                                        stock_ret=stock_ret,
                                        market_ret=market_ret,
                                        excess_ret=excess_ret,
                                        exit_reason=reason,
                                    )
                                )
                            except Exception:
                                continue

            if (idx + 1) % 20 == 0:
                print(f"Processed {idx + 1} earnings events for {ticker}...")

        print(f"Signals found: {signal_count}")
        print(f"Trade configurations attempted: {trade_attempt_count}")

        if not all_trades:
            print("No trade records were created after entry/exit evaluation.")
            return pd.DataFrame()

        trades_df = pd.DataFrame([vars(t) for t in all_trades])
        trades_df["side_label"] = trades_df["side"].map({1: "long", -1: "short"})
        return trades_df


# =========================
# Run + report
# =========================

def run_analysis():
    client = MassiveClient(API_KEY)
    analyzer = EarningsStrategyAnalyzer(client)
    base_output_dir = "/Users/markhuang/Desktop/实习/Level2/Output"
    os.makedirs(base_output_dir, exist_ok=True)

    print(f"Running multi-ticker online backtest for {TICKERS} ...")
    print(f"Execution data: {BAR_MINUTES}-minute bars")
    print(f"Signal logic: signed post-earnings move / pre-earnings volatility, monitored from {SIGNAL_START_TIME} ET")
    print(f"Market benchmark: {MARKET_TICKER}")

    all_trades = []
    per_ticker_overall = []
    per_ticker_by_side = []
    per_ticker_combo_returns = []
    per_ticker_top5 = []

    for ticker in TICKERS:
        print(f"\n===== Processing {ticker} =====")
        ticker_output_dir = os.path.join(base_output_dir, ticker)
        os.makedirs(ticker_output_dir, exist_ok=True)
        earnings_df = analyzer.prepare_data(ticker)
        print(f"Loaded {len(earnings_df)} earnings events for {ticker}.")

        trades = analyzer.backtest(ticker, earnings_df)
        if trades.empty:
            print(f"No trades generated for {ticker}.")
            continue

        all_trades.append(trades)

        overall = summarize_results(
            trades,
            group_cols=["hold_type", "hold_value", "threshold", "stop_loss", "profit_target"],
        ).sort_values("avg_excess_ret", ascending=False)
        overall["ticker"] = ticker

        by_side = summarize_results(
            trades,
            group_cols=["hold_type", "hold_value", "side_label", "threshold", "stop_loss", "profit_target"],
        ).sort_values("avg_excess_ret", ascending=False)
        by_side["ticker"] = ticker

        strategy_combo_returns = (
            trades.groupby(["hold_type", "hold_value", "threshold", "stop_loss", "profit_target", "event_date"])
            .agg(
                stock_ret=("stock_ret", "mean"),
                market_ret=("market_ret", "mean"),
                excess_ret=("excess_ret", "mean"),
                n_trades=("ticker", "size"),
            )
            .reset_index()
            .sort_values([
                "hold_type", "hold_value", "threshold", "stop_loss", "profit_target", "event_date"
            ])
        )
        strategy_combo_returns["ticker"] = ticker

        top5_configs = overall.head(5).copy()
        per_ticker_overall.append(overall)
        per_ticker_by_side.append(by_side)
        per_ticker_combo_returns.append(strategy_combo_returns)
        per_ticker_top5.append(top5_configs)

        trades_path = os.path.join(ticker_output_dir, f"{ticker}_earnings_strategy_trades.csv")
        overall_path = os.path.join(ticker_output_dir, f"{ticker}_earnings_strategy_summary_overall.csv")
        by_side_path = os.path.join(ticker_output_dir, f"{ticker}_earnings_strategy_summary_by_side.csv")
        combo_returns_path = os.path.join(ticker_output_dir, f"{ticker}_strategy_combo_returns.csv")
        top5_path = os.path.join(ticker_output_dir, f"{ticker}_top5_configs.csv")

        trades.to_csv(trades_path, index=False)
        overall.to_csv(overall_path, index=False)
        by_side.to_csv(by_side_path, index=False)
        strategy_combo_returns.to_csv(combo_returns_path, index=False)
        top5_configs.to_csv(top5_path, index=False)

        print("\nTrades sample:")
        print(trades[[
            "ticker", "event_date", "entry_ts", "exit_ts", "entry_px", "exit_px",
            "pre_vol", "signal_ratio", "stock_ret", "market_ret", "excess_ret",
            "hold_type", "hold_value", "threshold", "stop_loss", "profit_target",
            "exit_reason", "side_label"
        ]].head(10))

        print(f"\n=== Top 5 Strategy Configurations for {ticker} by Average Excess Return ===")
        print(top5_configs[[
            "hold_type", "hold_value", "threshold", "stop_loss", "profit_target",
            "n", "avg_stock_ret", "avg_excess_ret", "win_rate", "excess_win_rate"
        ]].to_string(index=False))
        print(f"Saved outputs for {ticker} to: {ticker_output_dir}")

    if not all_trades:
        print("No trades generated for any ticker.")
        return

    all_trades_df = pd.concat(all_trades, ignore_index=True)
    all_overall_df = pd.concat(per_ticker_overall, ignore_index=True)
    all_by_side_df = pd.concat(per_ticker_by_side, ignore_index=True)
    all_combo_returns_df = pd.concat(per_ticker_combo_returns, ignore_index=True)
    all_top5_df = pd.concat(per_ticker_top5, ignore_index=True)

    print("\n=== Combined Top 5 Strategy Configurations Per Ticker ===")
    print(all_top5_df[[
        "ticker", "hold_type", "hold_value", "threshold", "stop_loss", "profit_target",
        "n", "avg_stock_ret", "avg_excess_ret", "win_rate", "excess_win_rate"
    ]].to_string(index=False))

    print(f"\nPer-ticker CSV outputs saved under: {base_output_dir}")

    plot_df = all_top5_df.copy()
    threshold_plot_path = os.path.join(base_output_dir, "top5_threshold_vs_excess_return.png")
    stop_loss_plot_path = os.path.join(base_output_dir, "top5_stop_loss_vs_excess_return.png")
    profit_target_plot_path = os.path.join(base_output_dir, "top5_profit_target_vs_excess_return.png")
    holding_time_plot_path = os.path.join(base_output_dir, "top5_holding_time_vs_excess_return.png")
    plot_df["holding_label"] = plot_df["hold_type"] + "_" + plot_df["hold_value"].astype(str)

    plt.figure(figsize=(10, 6))
    for ticker, g in plot_df.groupby("ticker"):
        plt.scatter(g["threshold"], g["avg_excess_ret"], label=ticker)
    plt.xlabel("Threshold")
    plt.ylabel("Average Excess Return")
    plt.title("Top-5 Strategy Points per Ticker: Threshold vs Excess Return")
    plt.legend()
    plt.tight_layout()
    plt.savefig(threshold_plot_path, dpi=300, bbox_inches="tight")
    plt.show()

    plt.figure(figsize=(10, 6))
    for ticker, g in plot_df.groupby("ticker"):
        plt.scatter(g["stop_loss"].fillna(-1), g["avg_excess_ret"], label=ticker)
    plt.xlabel("Stop Loss (None shown as -1)")
    plt.ylabel("Average Excess Return")
    plt.title("Top-5 Strategy Points per Ticker: Stop Loss vs Excess Return")
    plt.legend()
    plt.tight_layout()
    plt.savefig(stop_loss_plot_path, dpi=300, bbox_inches="tight")
    plt.show()

    plt.figure(figsize=(10, 6))
    for ticker, g in plot_df.groupby("ticker"):
        plt.scatter(g["profit_target"].fillna(-1), g["avg_excess_ret"], label=ticker)
    plt.xlabel("Profit Target (None shown as -1)")
    plt.ylabel("Average Excess Return")
    plt.title("Top-5 Strategy Points per Ticker: Profit Target vs Excess Return")
    plt.legend()
    plt.tight_layout()
    plt.savefig(profit_target_plot_path, dpi=300, bbox_inches="tight")
    plt.show()

    plt.figure(figsize=(12, 6))
    holding_order = [
        ("intraday", 5), ("intraday", 15), ("intraday", 30), ("intraday", 60),
        ("intraday", 120), ("intraday", 180), ("intraday", 390),
        ("multiday", 1), ("multiday", 3), ("multiday", 5), ("multiday", 10),
    ]
    holding_labels = [f"{hold_type} {hold_value}" for hold_type, hold_value in holding_order]
    holding_pos = {k: i for i, k in enumerate(holding_order)}

    plot_df["holding_x"] = plot_df.apply(
        lambda row: holding_pos.get((row["hold_type"], row["hold_value"]), np.nan),
        axis=1,
    )
    holding_plot_df = plot_df.dropna(subset=["holding_x"]).copy()

    for ticker, g in holding_plot_df.groupby("ticker"):
        plt.scatter(g["holding_x"], g["avg_excess_ret"], label=ticker)

    plt.xticks(range(len(holding_labels)), holding_labels, rotation=45, ha="right")
    plt.xlabel("Holding Time")
    plt.ylabel("Average Excess Return")
    plt.title("Top-5 Strategy Points per Ticker: Holding Time vs Excess Return")
    plt.legend()
    plt.tight_layout()
    plt.savefig(holding_time_plot_path, dpi=300, bbox_inches="tight")
    plt.show()

    print("Saved plots:")
    print(f"  - {threshold_plot_path}")
    print(f"  - {stop_loss_plot_path}")
    print(f"  - {profit_target_plot_path}")
    print(f"  - {holding_time_plot_path}")


if __name__ == "__main__":
    run_analysis()