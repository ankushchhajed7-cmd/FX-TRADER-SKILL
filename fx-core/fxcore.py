"""fxcore v1.00 — shared engine for the FX Trader Skill Pack.

Pure Python standard library (3.9+). No pip installs.

What lives here:
  * pair settings (pip size, contract size, default spread)
  * candle loading (monthly CSV files in data/5m/<PAIR>/YYYY-MM.csv),
    resampling and an optional direct Yahoo fetch
  * session times (London / New York, daylight saving handled by zoneinfo)
  * the three strategies: Asian Range Breakout, NY Opening Breakout, RSI Bounce
  * a conservative trade simulator (stop before target when a bar is unclear)
  * statistics for the Time Machine

All times inside are UTC epoch seconds.
"""

import bisect
import csv
import datetime as dt
import glob
import json
import os
import urllib.request
from zoneinfo import ZoneInfo

VERSION = "1.00"
UTC = dt.timezone.utc
LON = ZoneInfo("Europe/London")
NYC = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- pairs
# spread = typical cost in PRICE units, taken off every trade.
# units_001 = units in 0.01 lot (FX 1,000 units; gold 1 oz).
PAIRS = {
    "XAUUSD": {"yahoo": "GC=F", "pip": 0.1, "digits": 2, "units_001": 1, "quote": "USD", "spread": 0.30},
    "EURUSD": {"yahoo": "EURUSD=X", "pip": 0.0001, "digits": 5, "units_001": 1000, "quote": "USD", "spread": 0.00012},
    "GBPUSD": {"yahoo": "GBPUSD=X", "pip": 0.0001, "digits": 5, "units_001": 1000, "quote": "USD", "spread": 0.00015},
    "USDJPY": {"yahoo": "USDJPY=X", "pip": 0.01, "digits": 3, "units_001": 1000, "quote": "JPY", "spread": 0.015},
    "AUDUSD": {"yahoo": "AUDUSD=X", "pip": 0.0001, "digits": 5, "units_001": 1000, "quote": "USD", "spread": 0.00012},
}

STRATEGIES = {
    "arb": "Asian Range Breakout",
    "nyorb": "NY Opening Breakout",
    "rsi": "RSI Bounce",
}

DEFAULTS = {
    "rr": 1.5,              # target = rr x risk
    "arb_window_h": 4,      # hours after London open to accept a breakout
    "arb_min_adr": 0.15,    # Asian range must be >= this share of ADR ...
    "arb_max_adr": 0.90,    # ... and <= this share
    "buffer": 0.05,         # breakout buffer = share of the range
    "ny_open": "09:30",     # New York local time
    "ny_range_min": 15,     # opening range length (minutes)
    "ny_window_end": "12:00",
    "ny_min_adr": 0.05,
    "ny_max_adr": 0.50,
    "rsi_period": 14,
    "rsi_low": 30,
    "rsi_high": 70,
    "rsi_tf": 15,           # minutes
    "rsi_near_adr": 0.25,   # swing must be this close (share of ADR) to prev-day high/low
    "rsi_end": "12:00",     # New York local, last signal time
    "flat": "16:00",        # New York local, every trade closed by then
    "adr_days": 10,
}


def fmt(pair, x):
    return f"{x:.{PAIRS[pair]['digits']}f}"


def usd_per_001(pair, move, price):
    """Money result of a price move for 0.01 lot, in USD."""
    p = PAIRS[pair]
    v = move * p["units_001"]
    if p["quote"] == "JPY":
        v = v / price
    return v


# ---------------------------------------------------------------- time helpers
def ts_of(day, hhmm, tz):
    """UTC epoch for local clock time hhmm ('09:30') on calendar date `day` in tz."""
    h, m = (int(x) for x in hhmm.split(":"))
    return int(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).timestamp())


def utc_date(ts):
    return dt.datetime.fromtimestamp(ts, UTC).date()


def iso(ts):
    return dt.datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def ist(ts):
    return dt.datetime.fromtimestamp(ts, ZoneInfo("Asia/Kolkata")).strftime("%H:%M IST")


# ---------------------------------------------------------------- candles
# A bar is a tuple (ts, open, high, low, close).

def parse_time(s):
    s = s.strip()
    if s.isdigit():
        return int(s)
    s = s.replace("T", " ").replace("Z", "")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return int(dt.datetime.strptime(s, f).replace(tzinfo=UTC).timestamp())
        except ValueError:
            pass
    raise ValueError(f"bad time: {s}")


def read_csv(path):
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            if not row or not row[0] or row[0][0].isalpha():
                continue  # header
            try:
                out.append((parse_time(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
            except (ValueError, IndexError):
                continue
    return out


def read_bars_file(path):
    """CSV (time,open,high,low,close) or JSON (list of bars, or Twelve Data style
    {"values":[{"datetime":..,"open":..}]}). Times are UTC."""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            j = json.load(fh)
        rows = j.get("values", j.get("bars", [])) if isinstance(j, dict) else j
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append((parse_time(str(r.get("datetime") or r.get("time"))), float(r["open"]),
                            float(r["high"]), float(r["low"]), float(r["close"])))
            else:
                out.append((parse_time(str(r[0])), *map(float, r[1:5])))
        return clean(out)
    return clean(read_csv(path))


def clean(bars):
    seen = {}
    for b in bars:
        if b[2] >= b[3] and b[2] > 0:
            seen[b[0]] = b
    return [seen[k] for k in sorted(seen)]


def load_bars(data_dir, pair, tf="5m"):
    files = sorted(glob.glob(os.path.join(data_dir, tf, pair, "*.csv")))
    bars = []
    for f in files:
        bars.extend(read_csv(f))
    return clean(bars)


def write_month_files(data_dir, pair, bars, tf="5m"):
    """Merge bars into data/<tf>/<pair>/YYYY-MM.csv. Returns number of new bars."""
    by_month = {}
    for b in bars:
        by_month.setdefault(dt.datetime.fromtimestamp(b[0], UTC).strftime("%Y-%m"), []).append(b)
    added = 0
    for month, rows in by_month.items():
        folder = os.path.join(data_dir, tf, pair)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, month + ".csv")
        old = read_csv(path) if os.path.exists(path) else []
        merged = {b[0]: b for b in old}
        before = len(merged)
        for b in rows:
            merged[b[0]] = b
        added += len(merged) - before
        d = PAIRS[pair]["digits"] + 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("time_utc,open,high,low,close\n")
            for k in sorted(merged):
                t, o, h, l, c = merged[k]
                fh.write(f"{iso(t)},{o:.{d}f},{h:.{d}f},{l:.{d}f},{c:.{d}f}\n")
        os.replace(tmp, path)
    return added


def fetch_yahoo(pair, interval="5m", rng="60d", timeout=20):
    """Direct fetch from Yahoo's public chart API. Raises on any network problem."""
    sym = PAIRS[pair]["yahoo"]
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(sym)}"
           f"?interval={interval}&range={rng}&includePrePost=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fx-trader-skill)",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(interval, 300)
    now = int(dt.datetime.now(UTC).timestamp())
    out = []
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c) or t + step > now:   # drop gaps and the bar still forming
            continue
        out.append((int(t) - int(t) % step, float(o), float(h), float(l), float(c)))
    return clean(out)


def resample(bars, minutes):
    step = minutes * 60
    out, cur = [], None
    for t, o, h, l, c in bars:
        k = t - t % step
        if cur and cur[0] == k:
            cur = (k, cur[1], max(cur[2], h), min(cur[3], l), c)
        else:
            if cur:
                out.append(cur)
            cur = (k, o, h, l, c)
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- indicators
def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0)
        loss += max(-d, 0)
    ag, al = gain / n, loss / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def daily_hl(bars):
    """{utc_date: (high, low, n_bars)} for weekdays."""
    d = {}
    for t, o, h, l, c in bars:
        k = utc_date(t)
        if k.weekday() >= 5:
            continue
        if k in d:
            hh, ll, n = d[k]
            d[k] = (max(hh, h), min(ll, l), n + 1)
        else:
            d[k] = (h, l, 1)
    return d


def adr_before(days_hl, day, n=10):
    """Average daily range of the n full weekdays before `day` (None if < 3 days)."""
    full = max((v[2] for v in days_hl.values()), default=0) * 0.5
    prev = [k for k in sorted(days_hl) if k < day and days_hl[k][2] >= full][-n:]
    if len(prev) < 3:
        return None
    return sum(days_hl[k][0] - days_hl[k][1] for k in prev) / len(prev)


def prev_day(days_hl, day):
    full = max((v[2] for v in days_hl.values()), default=0) * 0.5
    prev = [k for k in sorted(days_hl) if k < day and days_hl[k][2] >= full]
    return days_hl[prev[-1]] if prev else None


# ---------------------------------------------------------------- simulator
def simulate(bars, i, side, entry, sl, tp, flat_ts, tp_on_first_bar=True):
    """Walk forward from bar i (entry bar). Stop is checked before target in every
    bar (conservative). Returns (exit_ts, exit_price, reason) or None if data ends."""
    for j in range(i, len(bars)):
        t, o, h, l, c = bars[j]
        if t >= flat_ts:
            return t, o, "time"
        if side == "BUY":
            if j > i and o <= sl:
                return t, o, "sl"
            if l <= sl:
                return t, sl, "sl"
            if j > i and o >= tp:
                return t, o, "tp"
            if h >= tp and (j > i or tp_on_first_bar):
                return t, tp, "tp"
        else:
            if j > i and o >= sl:
                return t, o, "sl"
            if h >= sl:
                return t, sl, "sl"
            if j > i and o <= tp:
                return t, o, "tp"
            if l <= tp and (j > i or tp_on_first_bar):
                return t, tp, "tp"
    return None


def first_trigger(bars, i0, end_ts, buy_lvl, sell_lvl):
    """First bar from i0 (before end_ts) that touches a level.
    Returns (index, side, fill) / ('both', index) / None."""
    for j in range(i0, len(bars)):
        t, o, h, l, c = bars[j]
        if t >= end_ts:
            return None
        up, dn = h >= buy_lvl, l <= sell_lvl
        if up and dn:
            return ("both", j)
        if up:
            return (j, "BUY", max(buy_lvl, o))
        if dn:
            return (j, "SELL", min(sell_lvl, o))
    return None


def make_trade(pair, strat, day, side, entry_ts, entry, sl, tp, exit_info, spread):
    exit_ts, exit_px, reason = exit_info
    risk = abs(entry - sl)
    move = (exit_px - entry) if side == "BUY" else (entry - exit_px)
    move -= spread
    return {
        "pair": pair, "strategy": strat, "day": day.isoformat(), "side": side,
        "entry_time": iso(entry_ts), "entry": round(entry, 6), "sl": round(sl, 6), "tp": round(tp, 6),
        "exit_time": iso(exit_ts), "exit": round(exit_px, 6), "reason": reason,
        "r": round(move / risk, 3) if risk > 0 else 0.0,
        "usd_001": round(usd_per_001(pair, move, exit_px), 2),
    }


# ---------------------------------------------------------------- strategy setups
def arb_setup(bars, ts_list, days_hl, pair, day, P):
    """Asian range = 00:00 UTC -> London 08:00. Returns dict (levels may be provisional)."""
    if day.weekday() >= 5:
        return None
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    lon = ts_of(day, "08:00", LON)
    i0, i1 = bisect.bisect_left(ts_list, start), bisect.bisect_left(ts_list, lon)
    rng = bars[i0:i1]
    if not rng:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    w = hi - lo
    adr = adr_before(days_hl, day, P["adr_days"])
    buf = w * P["buffer"]
    buy, sell, mid = hi + buf, lo - buf, (hi + lo) / 2
    rr = P["rr"]
    s = {
        "strategy": "arb", "day": day, "range_start": start, "range_end": lon,
        "window_end": lon + int(P["arb_window_h"] * 3600), "flat": ts_of(day, P["flat"], NYC),
        "high": hi, "low": lo, "adr": adr, "bars": len(rng), "i_entry": i1,
        "buy": buy, "buy_sl": mid, "buy_tp": buy + rr * (buy - mid),
        "sell": sell, "sell_sl": mid, "sell_tp": sell - rr * (mid - sell),
        "ok": True, "why": "",
    }
    expected = (lon - start) / 300 * 0.6
    if len(rng) < expected:
        s["ok"], s["why"] = False, "range me data kam hai"
    elif adr is None:
        s["ok"], s["why"] = False, "ADR ke liye pichhle din ka data nahi"
    elif w < P["arb_min_adr"] * adr:
        s["ok"], s["why"] = False, f"range bahut chhota ({w / adr:.0%} of ADR)"
    elif w > P["arb_max_adr"] * adr:
        s["ok"], s["why"] = False, f"range bahut bada ({w / adr:.0%} of ADR)"
    return s


def nyorb_setup(bars, ts_list, days_hl, pair, day, P):
    if day.weekday() >= 5:
        return None
    op = ts_of(day, P["ny_open"], NYC)
    end_rng = op + P["ny_range_min"] * 60
    i0, i1 = bisect.bisect_left(ts_list, op), bisect.bisect_left(ts_list, end_rng)
    rng = bars[i0:i1]
    if not rng:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    w = hi - lo
    adr = adr_before(days_hl, day, P["adr_days"])
    buf = w * P["buffer"]
    buy, sell = hi + buf, lo - buf
    rr = P["rr"]
    s = {
        "strategy": "nyorb", "day": day, "range_start": op, "range_end": end_rng,
        "window_end": ts_of(day, P["ny_window_end"], NYC), "flat": ts_of(day, P["flat"], NYC),
        "high": hi, "low": lo, "adr": adr, "bars": len(rng), "i_entry": i1,
        "buy": buy, "buy_sl": lo, "buy_tp": buy + rr * (buy - lo),
        "sell": sell, "sell_sl": hi, "sell_tp": sell - rr * (hi - sell),
        "ok": True, "why": "",
    }
    if adr is None:
        s["ok"], s["why"] = False, "ADR ke liye pichhle din ka data nahi"
    elif w < P["ny_min_adr"] * adr:
        s["ok"], s["why"] = False, f"range bahut chhota ({w / adr:.0%} of ADR)"
    elif w > P["ny_max_adr"] * adr:
        s["ok"], s["why"] = False, f"range bahut bada ({w / adr:.0%} of ADR)"
    return s


def breakout_trade(bars, pair, s, spread):
    """Run a breakout setup (arb / nyorb). Returns trade dict, or None."""
    if not s or not s["ok"]:
        return None
    trig = first_trigger(bars, s["i_entry"], s["window_end"], s["buy"], s["sell"])
    if trig is None or trig[0] == "both":
        return None
    j, side, fill = trig
    if side == "BUY":
        sl, tp = s["buy_sl"], fill + (s["buy_tp"] - s["buy"])
    else:
        sl, tp = s["sell_sl"], fill - (s["sell"] - s["sell_tp"])
    ex = simulate(bars, j, side, fill, sl, tp, s["flat"], tp_on_first_bar=False)
    if ex is None:
        return None
    return make_trade(pair, s["strategy"], s["day"], side, bars[j][0], fill, sl, tp, ex, spread)


class RsiState:
    """RSI on resampled bars, computed once per pair."""

    def __init__(self, bars, P):
        self.tf = P["rsi_tf"]
        self.rb = resample(bars, self.tf)
        self.rts = [b[0] for b in self.rb]
        self.rsi = rsi([b[4] for b in self.rb], P["rsi_period"])


def rsi_signals(st, days_hl, day, P, until_ts=None):
    """Signals on closed RSI bars for `day` between London open and rsi_end."""
    if day.weekday() >= 5:
        return []
    pd_ = prev_day(days_hl, day)
    adr = adr_before(days_hl, day, P["adr_days"])
    if not pd_ or not adr:
        return []
    pdh, pdl = pd_[0], pd_[1]
    start, end = ts_of(day, "08:00", LON), ts_of(day, P["rsi_end"], NYC)
    i0 = bisect.bisect_left(st.rts, start)
    out = []
    near = P["rsi_near_adr"] * adr
    for i in range(max(i0, 5), len(st.rb)):
        t = st.rb[i][0]
        close_ts = t + st.tf * 60
        if t >= end or (until_ts is not None and close_ts > until_ts):
            break
        a, b = st.rsi[i - 1], st.rsi[i]
        if a is None or b is None:
            continue
        swing_lo = min(x[3] for x in st.rb[i - 4:i + 1])
        swing_hi = max(x[2] for x in st.rb[i - 4:i + 1])
        if a < P["rsi_low"] <= b and abs(swing_lo - pdl) <= near:
            out.append({"side": "BUY", "bar": t, "ready": close_ts, "rsi": b,
                        "stop": swing_lo - 0.05 * adr, "level": pdl, "adr": adr, "close": st.rb[i][4]})
        elif a > P["rsi_high"] >= b and abs(swing_hi - pdh) <= near:
            out.append({"side": "SELL", "bar": t, "ready": close_ts, "rsi": b,
                        "stop": swing_hi + 0.05 * adr, "level": pdh, "adr": adr, "close": st.rb[i][4]})
    return out


def rsi_trade(bars, ts_list, pair, sig, day, P, spread):
    j = bisect.bisect_left(ts_list, sig["ready"])
    if j >= len(bars):
        return None
    entry = bars[j][1]
    sl = sig["stop"]
    risk = (entry - sl) if sig["side"] == "BUY" else (sl - entry)
    if risk <= 0 or risk > 0.5 * sig["adr"]:
        return None
    tp = entry + P["rr"] * risk if sig["side"] == "BUY" else entry - P["rr"] * risk
    ex = simulate(bars, j, sig["side"], entry, sl, tp, ts_of(day, P["flat"], NYC))
    if ex is None:
        return None
    return make_trade(pair, "rsi", day, sig["side"], bars[j][0], entry, sl, tp, ex, spread)


# ---------------------------------------------------------------- backtest
def run_pair(pair, bars, strategies, P, spread, date_from=None, date_to=None):
    ts_list = [b[0] for b in bars]
    days_hl = daily_hl(bars)
    days = sorted(k for k in days_hl if (date_from is None or k >= date_from) and (date_to is None or k <= date_to))
    trades = {s: [] for s in strategies}
    st = RsiState(bars, P) if "rsi" in strategies else None
    for day in days:
        if "arb" in strategies:
            t = breakout_trade(bars, pair, arb_setup(bars, ts_list, days_hl, pair, day, P), spread)
            if t:
                trades["arb"].append(t)
        if "nyorb" in strategies:
            t = breakout_trade(bars, pair, nyorb_setup(bars, ts_list, days_hl, pair, day, P), spread)
            if t:
                trades["nyorb"].append(t)
        if st is not None:
            for sig in rsi_signals(st, days_hl, day, P):
                t = rsi_trade(bars, ts_list, pair, sig, day, P, spread)
                if t:
                    trades["rsi"].append(t)
                    break  # one RSI trade per day
    return trades, (days[0] if days else None), (days[-1] if days else None)


def stats(trades):
    n = len(trades)
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    streak = worst = 0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    half = n // 2
    return {
        "trades": n,
        "win_pct": round(100 * wins / n, 1) if n else 0.0,
        "net_r": round(sum(rs), 2),
        "avg_r": round(sum(rs) / n, 3) if n else 0.0,
        "pf": round(gp / gl, 2) if gl > 0 else (None if gp == 0 else 99.0),
        "max_dd_r": round(dd, 2),
        "worst_streak": worst,
        "usd_001": round(sum(t["usd_001"] for t in trades), 2),
        "first_half_r": round(sum(rs[:half]), 2),
        "second_half_r": round(sum(rs[half:]), 2),
    }
