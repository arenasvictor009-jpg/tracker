#!/usr/bin/env python3
"""
3-signal alert bot: VWAP reclaim + MACD histogram expansion + RSI.
Sends a phone push (via ntfy.sh) when all three agree. Runs during US market hours.

Setup:
  pip install requests
  export TWELVEDATA_KEY="your_key"      # free key from twelvedata.com
  export NTFY_TOPIC="pick-a-long-random-name"   # then subscribe to it in the ntfy app
  export TICKERS="SPY,QQQ,META"         # optional, comma separated
  python3 tracker.py --test             # sends a test push
  python3 tracker.py                    # runs forever
Options (env): REQUIRE_RECLAIM=1 only counts a FRESH cross above VWAP as bullish.
"""
import os, sys, time, json, datetime as dt
from zoneinfo import ZoneInfo
import requests

API_KEY = os.environ.get("TWELVEDATA_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
TICKERS = [t.strip().upper() for t in os.environ.get("TICKERS", "SPY,QQQ,META").split(",") if t.strip()]
REQUIRE_RECLAIM = os.environ.get("REQUIRE_RECLAIM", "0") == "1"
INTERVAL = "5min"
LOOP_SECONDS = 300
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
ET = ZoneInfo("America/New_York")


def ema(values, n):
    k, e, out = 2 / (n + 1), None, []
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) < n + 2:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def macd_hist(closes):
    if len(closes) < 40:
        return None, None
    line = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    sig = ema(line, 9)
    hist = [m - s for m, s in zip(line, sig)]
    return hist[-1], hist[-2]


def session_vwap(bars):
    """VWAP for the latest trading day. Returns (vwap_now, vwap_prior_bar)."""
    day = bars[-1]["datetime"][:10]
    pv = vol = 0.0
    series = []
    for b in bars:
        if b["datetime"][:10] != day:
            continue
        typical = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3
        v = float(b.get("volume") or 0)
        pv += typical * v
        vol += v
        series.append(pv / vol if vol else typical)
    if len(series) < 2:
        return None, None
    return series[-1], series[-2]


def evaluate(bars):
    closes = [float(b["close"]) for b in bars]
    p, q = closes[-1], closes[-2]
    v, qv = session_vwap(bars)
    r = rsi(closes)
    h, j = macd_hist(closes)
    if None in (v, qv, r, h, j):
        return None
    fresh_up = q < qv and p > v
    fresh_dn = q > qv and p < v
    if REQUIRE_RECLAIM:
        s_vwap = "up" if fresh_up else "dn" if fresh_dn else "neu"
    else:
        s_vwap = "up" if p > v else "dn" if p < v else "neu"
    s_hist = "up" if (h > 0 and h > j) else "dn" if (h < 0 and h < j) else "neu"
    s_rsi = "up" if 50 <= r <= 70 else "dn" if 30 <= r < 50 else "neu"
    sigs = [s_vwap, s_hist, s_rsi]
    overall = "up" if sigs == ["up"] * 3 else "dn" if sigs == ["dn"] * 3 else "none"
    return {"price": p, "vwap": v, "rsi": r, "hist": h, "prior_hist": j,
            "signals": sigs, "overall": overall, "fresh_reclaim": fresh_up}


def fetch(sym, size=300):
    resp = requests.get("https://api.twelvedata.com/time_series", timeout=20, params={
        "symbol": sym, "interval": INTERVAL, "outputsize": size,
        "timezone": "America/New_York", "apikey": API_KEY})
