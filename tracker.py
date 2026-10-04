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
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "60"))   # no repeat alert on a ticker for this long
SKIP_OPEN_MIN = int(os.environ.get("SKIP_OPEN_MIN", "15"))  # ignore the first minutes after the open
STOP_ATR = float(os.environ.get("STOP_ATR", "1.0"))   # stop-loss distance, in ATRs (typical move size)
TP1_ATR = float(os.environ.get("TP1_ATR", "1.0"))     # first take-profit distance, in ATRs
TP2_ATR = float(os.environ.get("TP2_ATR", "2.0"))     # final target distance, in ATRs
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


def atr(bars, n=14):
    """Average true range: the stock's typical bar-to-bar move size."""
    trs = []
    for a, b in zip(bars[-n - 1:-1], bars[-n:]):
        hi, lo, pc = float(b["high"]), float(b["low"]), float(a["close"])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    return sum(trs) / len(trs) if trs else None


def trade_plan(price, a, direction):
    """direction: 1 for calls (stock going up), -1 for puts (stock going down).
    All levels are on the STOCK price, not the option price."""
    stop = price - direction * STOP_ATR * a
    tp1 = price + direction * TP1_ATR * a
    tp2 = price + direction * TP2_ATR * a
    pct = lambda x: (x - price) / price * 100
    return "\n".join([
        f"Entry ~{price:.2f}",
        f"Stop loss {stop:.2f} ({pct(stop):+.2f}%)",
        f"Take profit 1: {tp1:.2f} ({pct(tp1):+.2f}%) - sell half, move stop to entry",
        f"Take profit 2: {tp2:.2f} ({pct(tp2):+.2f}%) - sell the rest",
        f"Risk:reward 1:{TP2_ATR / STOP_ATR:.1f} | Exit if flat after 1 hour",
    ])


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
    j = resp.json()
    if "values" not in j:
        raise RuntimeError(j.get("message", "bad response"))
    return list(reversed(j["values"]))


def market_open(now):
    return now.weekday() < 5 and dt.time(9, 30) <= now.time() < dt.time(16, 0)


def bar_is_fresh(bars, now):
    t = dt.datetime.strptime(bars[-1]["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    return (now - t).total_seconds() < 15 * 60


def notify(title, msg):
    print(f"[ALERT] {title}: {msg}", flush=True)
    if not NTFY_TOPIC:
        return
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=msg.encode("utf-8"),
                      headers={"Title": title, "Priority": "high"}, timeout=15)
    except Exception as e:
        print("ntfy error:", e, flush=True)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {}


def save_state(s):
    try:
        json.dump(s, open(STATE_FILE, "w"))
    except Exception as e:
        print("state save error:", e)


def run_cycle(state):
    now = dt.datetime.now(ET)
    if now.time() < (dt.datetime.combine(now.date(), dt.time(9, 30)) + dt.timedelta(minutes=SKIP_OPEN_MIN)).time():
        print(f"First {SKIP_OPEN_MIN} min after the open: skipping.", flush=True)
        return
    for sym in TICKERS:
        try:
            bars = fetch(sym)
            if not bar_is_fresh(bars, now):
                print(f"{sym}: data stale, skipping", flush=True)
                continue
            res = evaluate(bars)
            if not res:
                print(f"{sym}: not enough data", flush=True)
                continue
            print(f"{now:%H:%M} {sym} ${res['price']:.2f} vwap {res['vwap']:.2f} rsi {res['rsi']:.1f} "
                  f"hist {res['hist']:.3f} -> {res['signals']} {res['overall']}", flush=True)
            last = state.get(sym + "__t", 0)
            if res["overall"] != "none" and state.get(sym) != res["overall"]:
                if time.time() - last >= COOLDOWN_MIN * 60:
                    side = "BULLISH" if res["overall"] == "up" else "BEARISH"
                    idea = "CALL idea" if res["overall"] == "up" else "PUT idea"
                    a = atr(bars)
                    plan = ""
                    if a:
                        plan = "\n" + trade_plan(res["price"], a, 1 if res["overall"] == "up" else -1)
                    notify(f"{sym}: ALL 3 {side} ({idea})",
                           f"{sym} ${res['price']:.2f} | VWAP {res['vwap']:.2f} | RSI {res['rsi']:.0f} | "
                           f"MACD hist {res['hist']:.3f} (prior {res['prior_hist']:.3f}){plan}\n"
                           f"Levels are on the stock price, not the option price.")
                    state[sym] = res["overall"]
                    state[sym + "__t"] = time.time()
                else:
                    print(f"{sym}: signal held back by {COOLDOWN_MIN}-min cooldown", flush=True)
            else:
                state[sym] = res["overall"]
        except Exception as e:
            print(f"{sym}: error {e}", flush=True)
        time.sleep(9)  # stay under the free-tier 8 requests/minute limit
    save_state(state)


def backtest():
    """Replay the last ~3 weeks of 5-min bars and show when alerts WOULD have fired."""
    HORIZONS = (6, 12)  # bars ahead: 30 min and 60 min
    total = {h: [] for h in HORIZONS}
    for sym in TICKERS:
        try:
            bars = fetch(sym, 1500)
        except Exception as e:
            print(f"{sym}: error {e}", flush=True)
            continue
        print(f"\n=== {sym} ({len(bars)} bars, {bars[0]['datetime'][:10]} to {bars[-1]['datetime'][:10]}) ===")
        prev, n = "none", 0
        for i in range(80, len(bars)):
            res = evaluate(bars[max(0, i - 299): i + 1])
            if not res:
                continue
            if res["overall"] != "none" and prev != res["overall"]:
                n += 1
                side = 1 if res["overall"] == "up" else -1
                p = res["price"]
                moves = []
                for h in HORIZONS:
                    if i + h < len(bars) and bars[i + h]["datetime"][:10] == bars[i]["datetime"][:10]:
                        m = side * (float(bars[i + h]["close"]) - p) / p * 100
                        moves.append(m)
                        total[h].append(m)
                    else:
                        moves.append(None)
                fmt = lambda m: "   n/a" if m is None else f"{m:+.2f}%"
                print(f"{bars[i]['datetime'][:16]}  {'CALL idea' if side == 1 else 'PUT idea '}  ${p:.2f}   after 30m {fmt(moves[0])}   after 60m {fmt(moves[1])}")
            prev = res["overall"]
        if n == 0:
            print("no all-3 signals in this period")
    print("\n=== SUMMARY (move in the signal's direction) ===")
    for h in HORIZONS:
        v = total[h]
        if v:
            wins = sum(1 for m in v if m > 0)
            print(f"after {h*5} min: {len(v)} signals, {wins/len(v)*100:.0f}% moved the right way, average {sum(v)/len(v):+.3f}%")
        else:
            print(f"after {h*5} min: no signals")
    print("Past results do not predict future results. Moves exclude spreads, fees and option pricing.")


def main():
    if not API_KEY:
        sys.exit("Set TWELVEDATA_KEY first.")
    if "--backtest" in sys.argv:
        backtest()
        return
    if "--test" in sys.argv:
        notify("Tracker test", "If you see this, alerts work.")
        return
    state = load_state()
    if "--once" in sys.argv:  # used by GitHub Actions: one check, then exit
        if market_open(dt.datetime.now(ET)):
            run_cycle(state)
        else:
            print("Market closed, nothing to do.")
        return
    print(f"Watching {TICKERS} every {LOOP_SECONDS}s during market hours (ET).", flush=True)
    while True:
        if market_open(dt.datetime.now(ET)):
            run_cycle(state)
        time.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    main()
