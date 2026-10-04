import os, io, re, sys, time, zipfile, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests
import tracker as t

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DAYS = int(os.environ.get("DAYS") or "7")
UTC, ET = dt.timezone.utc, t.ET
HDR = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"}
ALERT = re.compile(r"\[ALERT\] ([A-Z][A-Z0-9.\-]*): ALL 3 (BULLISH|BEARISH)[^\n]*?\$([0-9][0-9,]*\.?[0-9]*)")
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")


def list_runs():
    since = (dt.datetime.now(UTC) - dt.timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = []
    for page in range(1, 11):
        r = requests.get(f"https://api.github.com/repos/{REPO}/actions/workflows/tracker.yml/runs",
                         headers=HDR, timeout=30,
                         params={"per_page": 100, "page": page, "created": f">={since}", "status": "completed"})
        r.raise_for_status()
        batch = r.json().get("workflow_runs", [])
        runs += batch
        if len(batch) < 100:
            break
    return runs


def alerts_in_run(run):
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/actions/runs/{run['id']}/logs", headers=HDR, timeout=60)
        if r.status_code != 200:
            return []
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:
        return []
    out = []
    for name in z.namelist():
        if not name.endswith(".txt"):
            continue
        for line in z.read(name).decode("utf-8", "ignore").splitlines():
            if "[ALERT]" not in line:
                continue
            m, s = ALERT.search(line), STAMP.match(line)
            if m and s:
                ts = dt.datetime.strptime(s.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).astimezone(ET)
                out.append({"sym": m.group(1), "side": 1 if m.group(2) == "BULLISH" else -1,
                            "price": float(m.group(3).replace(",", "")), "time": ts})
    return out


def score(a, bars):
    times = [dt.datetime.strptime(b["datetime"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET) for b in bars]
    idx = None
    for i, tm in enumerate(times):
        if tm <= a["time"]:
            idx = i
        else:
            break
    if idx is None or idx < 20 or idx + 6 >= len(bars):
        return None
    d, p, day = a["side"], a["price"], bars[idx]["datetime"][:10]
    av = t.atr(bars[:idx + 1])

    def move(h):
        j = idx + h
        if j < len(bars) and bars[j]["datetime"][:10] == day:
            return d * (float(bars[j]["close"]) - p) / p * 100
        return None

    best = worst = 0.0
    outcome, hit1 = "no stop or target hit in 60 min", False
    stop, tp1, tp2 = p - d * t.STOP_ATR * av, p + d * t.TP1_ATR * av, p + d * t.TP2_ATR * av
    for j in range(idx + 1, min(idx + 13, len(bars))):
        if bars[j]["datetime"][:10] != day:
            break
        hi, lo = float(bars[j]["high"]), float(bars[j]["low"])
        best = max(best, ((hi - p) if d == 1 else (p - lo)) / p * 100)
        worst = max(worst, ((p - lo) if d == 1 else (hi - p)) / p * 100)
        lvl = p if hit1 else stop
        if (lo <= lvl) if d == 1 else (hi >= lvl):
            outcome = "TP1 then back to entry" if hit1 else "STOPPED"
            break
        if (hi >= tp1) if d == 1 else (lo <= tp1):
            hit1, outcome = True, "TP1 hit (still open)"
        if (hi >= tp2) if d == 1 else (lo <= tp2):
            outcome = "TP2 hit"
            break
    return {"m30": move(6), "m60": move(12), "best": best, "worst": worst, "outcome": outcome}


def stat(v):
    return "none" if not v else f"{sum(1 for x in v if x > 0)}/{len(v)} moved the right way ({sum(1 for x in v if x > 0) / len(v) * 100:.0f}%), average {sum(v) / len(v):+.3f}%"


def main():
    if not (t.API_KEY and REPO and TOKEN):
        sys.exit("Missing TWELVEDATA_KEY / GITHUB_REPOSITORY / GITHUB_TOKEN.")
    runs = list_runs()
    print(f"Reading {len(runs)} tracker runs from the last {DAYS} days...", flush=True)
    with ThreadPoolExecutor(8) as ex:
        found = [a for res in ex.map(alerts_in_run, runs) for a in res]
    seen, alerts = set(), []
    for a in sorted(found, key=lambda x: x["time"]):
        k = (a["sym"], a["side"], a["time"].strftime("%Y-%m-%d %H:%M:%S"), a["price"])
        if k not in seen:
            seen.add(k)
            alerts.append(a)
    if not alerts:
        print("No real alerts found in those runs. That can simply mean no setup passed all 3 checks.")
        return
    print(f"Found {len(alerts)} alerts. Fetching prices...\n", flush=True)
    bars = {}
    for sym in sorted({a["sym"] for a in alerts}):
        try:
            bars[sym] = t.fetch(sym, 1500)
        except Exception as e:
            print(sym, "price error:", e, flush=True)
        time.sleep(9)
    m30, m60, outs, skipped = [], [], {}, 0
    for a in alerts:
        s = score(a, bars[a["sym"]]) if a["sym"] in bars else None
        if not s:
            skipped += 1
            continue
        f = lambda x: "  n/a " if x is None else f"{x:+.2f}%"
        print(f"{a['time']:%a %m-%d %H:%M}  {a['sym']:5} {'CALL' if a['side'] == 1 else 'PUT '} ${a['price']:.2f}  "
              f"30m {f(s['m30'])}  60m {f(s['m60'])}  best {s['best']:+.2f}%  worst -{s['worst']:.2f}%  {s['outcome']}")
        if s["m30"] is not None:
            m30.append(s["m30"])
        if s["m60"] is not None:
            m60.append(s["m60"])
        outs[s["outcome"]] = outs.get(s["outcome"], 0) + 1
    print("\n=== SUMMARY OF YOUR REAL ALERTS (move in the alert's direction) ===")
    print("after 30 min:", stat(m30))
    print("after 60 min:", stat(m60))
    for k, v in sorted(outs.items(), key=lambda x: -x[1]):
        print(f"  {v:3}  {k}")
    if skipped:
        print(f"({skipped} alerts skipped: too recent to judge, or price data missing)")
    print("Small samples are noisy. Moves are on the stock price, not option prices, and exclude fees.")


if __name__ == "__main__":
    main()
