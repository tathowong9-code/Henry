#!/usr/bin/env python3
"""tradecheck: Henry's trade checker (Trading 212, GBP, swing trades).

One tool built from nine of the trader-* skills plus /stockchecking:

  scanner     -> `scan`      daily setups across the US market, then a full check of the best
  news        -> `news`      Yahoo headlines + SEC 8-Ks per ticker
  analyst     -> `earnings`  reported quarters from SEC XBRL facts
  exit        -> plan        stop + target set before the entry (swing version: daily bars)
  timemachine -> replays     10,000 bootstrapped replays of the next N sessions + real history
  budget      -> size        position size in GBP from your max loss (fractional shares, FX fee)
  risk        -> rules gate  PASS / BLOCK against your limits, never edits the trade
  entry       -> `entry`     the exact entry trigger on 5-minute bars + the Trading 212 order to place
  alerts      -> `clock`     NYSE calendar shown in New York and UK time
  stockchecking -> `holdings` HOLD / TRIM / WATCH STOP / SELL TO STAY SAFE per holding

It never places, edits or cancels an order. A PASS is a rules check, not a recommendation.
Standard library only. Python 3.8+.

  python3 tradecheck.py check NVDA                     # one trade, full check
  python3 tradecheck.py check NVDA AMD --html out.html # report with candlestick charts
  python3 tradecheck.py scan --limit 800               # market scan -> buy-zone picks
  python3 tradecheck.py holdings --t212 --html h.html  # Trading 212 holdings review
  python3 tradecheck.py clock

Data: Yahoo (free, unofficial, delayed) when the machine has internet, or `--data DIR`
with DIR/<TICKER>.csv (date,open,high,low,close[,volume]) and DIR/fx.json {"gbpusd": 1.33}.
"""

import argparse
import base64
import csv
import datetime as dt
import gzip
import html as H
import io
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0"
UA = "Mozilla/5.0 (compatible; tradecheck/1.0)"
NOTE = "Analysis to support your own decision. It is not regulated financial advice."

DEFAULT_CONFIG = {
    "budget_gbp": 5000,             # money set aside for these trades
    "cash_gbp": None,               # cash available now (None = budget). --t212 fills it
    "cash_buffer_pct": 10,          # always left in cash
    "max_loss_per_trade_gbp": 50,   # loss if the stop fills at the stop, fees included
    "max_position_pct": 20,         # biggest single position, % of budget
    "max_order_gbp": 2000,
    "min_order_gbp": 10,
    "fractional": True,             # Trading 212 sells fractional shares
    "share_step": 0.01,
    "fx_fee_pct": 0.15,             # Trading 212 currency conversion fee, each way
    "target_r": 2.0,
    "min_reward_risk": 1.5,
    "stop_rule": "swing",
    "hold_days": 20,                # trading sessions a swing trade is planned for
    "block_earnings_within_days": 5,
    "max_positions": 10,
    "blocked": [],
    "allow_counter_trend": False,   # oversold bounces in a downtrend
    "min_price_usd": 5,
    "min_dollar_volume_usd": 20000000,
    "gbpusd": None,
    "sec_user_agent": None,         # "Your Name you@example.com", needed for SEC
    "replays": 10000,
    "seed": 19,
    "pool_days": 500,
    "drift": "keep",                # keep the stock's own drift in replays, or "zero"
    "entry_from": "09:45",          # New York time: no entry triggers before (14:45 UK most of the year)
    "entry_until": "15:30",         # New York time: no new entries after
    "entry_slippage_pct": 0.3,      # stop-limit: the most you pay above the trigger
}


class DataError(Exception):
    pass


# ================================================================ time and the NYSE calendar

NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}


def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_sunday(year, month):
    d = dt.date(year, month + 1, 1) - dt.timedelta(days=1) if month < 12 else dt.date(year, 12, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def ny_offset(utc):
    y = utc.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7, 0))
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6, 0))
    return -4 if start <= utc.replace(tzinfo=None) < end else -5


def uk_offset(utc):
    y = utc.year
    start = dt.datetime.combine(_last_sunday(y, 3), dt.time(1, 0))
    end = dt.datetime.combine(_last_sunday(y, 10), dt.time(1, 0))
    return 1 if start <= utc.replace(tzinfo=None) < end else 0


def to_ny(utc):
    return utc.replace(tzinfo=None) + dt.timedelta(hours=ny_offset(utc))


def to_uk(utc):
    return utc.replace(tzinfo=None) + dt.timedelta(hours=uk_offset(utc))


def ny_to_utc(local):
    for off in (-4, -5):
        u = local - dt.timedelta(hours=off)
        if ny_offset(u) == off:
            return u
    return local + dt.timedelta(hours=5)


def ts_to_ny(ts):
    return to_ny(dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(ts)))


def utc_now(text=None):
    """--now 'YYYY-MM-DD HH:MM' is New York time; default is the real clock."""
    if text:
        t = dt.datetime.fromisoformat(text.replace("T", " "))
        if t.tzinfo:
            return t.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return ny_to_utc(t)
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in NYSE_HOLIDAYS


def session(d):
    if not trading_day(d):
        return None
    return dt.time(9, 30), (dt.time(13, 0) if d.isoformat() in NYSE_EARLY_CLOSE else dt.time(16, 0))


def market_open(now_utc):
    n = to_ny(now_utc)
    s = session(n.date())
    return bool(s) and s[0] <= n.time() < s[1]


def next_trading_days(after, n):
    out, d = [], dt.date.fromisoformat(after)
    while len(out) < n:
        d += dt.timedelta(days=1)
        if trading_day(d):
            out.append(d.isoformat())
    return out


def trading_days_between(a, b):
    """Trading sessions after date a up to and including date b."""
    d, n = dt.date.fromisoformat(a), 0
    end = dt.date.fromisoformat(b)
    while d < end:
        d += dt.timedelta(days=1)
        n += trading_day(d)
    return n


# ================================================================ network

YAHOO = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")


def http_get(url, headers=None, timeout=20):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def yahoo(path):
    last = "no answer"
    for attempt in (0, 1):
        for host in YAHOO:
            try:
                return json.loads(http_get(host + path))
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                if e.code in (400, 404):
                    try:
                        return json.loads(e.read().decode("utf-8", "replace"))
                    except Exception:
                        raise DataError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = str(getattr(e, "reason", e))
        if attempt == 0:
            time.sleep(3)
    raise DataError("Yahoo unreachable (%s). Use --data DIR with CSV files instead." % last)


def ysym(sym):
    return sym.upper().replace(".", "-")


# ================================================================ bars

def _num(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace(",", "").replace("$", "").replace("£", "").strip()
    if s in ("", "-", "null", "None", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(x):
    s = str(x).strip().strip('"')
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", s):
        return s[:10]
    if re.fullmatch(r"\d{9,13}", s):
        v = int(s)
        return ts_to_ny(v / 1000 if v > 1e11 else v).date().isoformat()
    for fmt in ("%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def clean_bars(rows):
    """Dedupe by date (last wins), sort, repair highs/lows, drop obvious bad ticks."""
    by = {}
    for b in rows:
        if b and b.get("d") and None not in (b.get("o"), b.get("h"), b.get("l"), b.get("c")) and b["c"] > 0:
            by[b["d"]] = b
    bars = [by[k] for k in sorted(by)]
    for i, b in enumerate(bars):
        b["h"] = max(b["h"], b["o"], b["c"])
        b["l"] = min(b["l"], b["o"], b["c"])
        if i:
            pc = bars[i - 1]["c"]
            if b["h"] > 1.5 * max(pc, b["c"]):
                b["h"] = max(b["o"], b["c"])
            if b["l"] < 0.6 * min(pc, b["c"]):
                b["l"] = min(b["o"], b["c"])
        b.setdefault("v", 0)
        b["v"] = b["v"] or 0
    return bars


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    rows, hdr = [], None
    for cells in csv.reader(io.StringIO(text)):
        cells = [c.strip() for c in cells]
        if not cells or not any(cells):
            continue
        if hdr is None and not _date(cells[0]):
            hdr = [c.lower() for c in cells]
            continue
        rows.append(cells)
    names = {"d": ("date", "t", "time", "timestamp", "day"), "o": ("open", "o"), "h": ("high", "h"),
             "l": ("low", "l"), "c": ("close", "c", "close/last", "price"), "v": ("volume", "v", "vol")}
    if hdr:
        idx = {}
        for k, alts in names.items():
            for a in alts:
                if a in hdr:
                    idx[k] = hdr.index(a)
                    break
        if not {"d", "o", "h", "l", "c"} <= set(idx):
            raise DataError("%s: need date,open,high,low,close columns (got %s)" % (path, ",".join(hdr)))
    else:
        idx = {"d": 0, "o": 1, "h": 2, "l": 3, "c": 4, "v": 5}
    out = []
    for r in rows:
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        out.append({"d": _date(g("d")), "o": _num(g("o")), "h": _num(g("h")), "l": _num(g("l")),
                    "c": _num(g("c")), "v": _num(g("v")) or 0})
    return clean_bars(out)


def bars_from_chart(doc, sym):
    res = ((doc or {}).get("chart") or {}).get("result")
    if not res:
        err = ((doc or {}).get("chart") or {}).get("error") or {}
        raise DataError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    r = res[0]
    t = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    col = lambda k: (q.get(k) or [None] * len(t)) + [None] * len(t)
    o, h, l, c, v = col("open"), col("high"), col("low"), col("close"), col("volume")
    rows = [{"d": ts_to_ny(ts).date().isoformat(), "o": o[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i]}
            for i, ts in enumerate(t)]
    return clean_bars(rows), (r.get("meta") or {})


def get_bars(sym, a, now_utc):
    """-> (bars, info). info: source, currency, live flag, name."""
    if a.data:
        for ext in (".csv", ".json", ".CSV"):
            for name in (sym, sym.upper(), sym.lower(), ysym(sym)):
                p = os.path.join(a.data, name + ext)
                if os.path.exists(p):
                    if ext.lower() == ".json":
                        with open(p) as f:
                            bars, meta = bars_from_chart(json.load(f), sym)
                    else:
                        bars, meta = load_csv(p), {}
                    return bars, {"source": "file %s" % os.path.basename(p), "currency": meta.get("currency", "USD"),
                                  "live": False, "name": meta.get("longName") or meta.get("shortName") or ""}
        raise DataError("no file for %s in %s (want %s.csv)" % (sym, a.data, sym.upper()))
    doc = yahoo("/v8/finance/chart/%s?range=2y&interval=1d&includePrePost=false" % ysym(sym))
    bars, meta = bars_from_chart(doc, sym)
    live = bool(bars) and market_open(now_utc) and bars[-1]["d"] == to_ny(now_utc).date().isoformat()
    return bars, {"source": "Yahoo daily bars (free, delayed)", "currency": meta.get("currency", "USD"),
                  "live": live, "name": meta.get("longName") or meta.get("shortName") or ""}


def get_fx(a, cfg):
    """USD per GBP."""
    if getattr(a, "gbpusd", None):
        return float(a.gbpusd), "--gbpusd"
    if cfg.get("gbpusd"):
        return float(cfg["gbpusd"]), "config"
    if a.data:
        p = os.path.join(a.data, "fx.json")
        if os.path.exists(p):
            with open(p) as f:
                return float(json.load(f)["gbpusd"]), "fx.json"
        raise DataError("need GBP/USD: --gbpusd 1.33 or %s/fx.json" % a.data)
    doc = yahoo("/v8/finance/chart/GBPUSD=X?range=5d&interval=1d")
    bars, _ = bars_from_chart(doc, "GBPUSD")
    return bars[-1]["c"], "Yahoo GBPUSD=X"


# ================================================================ indicators

def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def wilder_atr(bars, n=14):
    out = [None] * len(bars)
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b["h"] - b["l"])
        else:
            pc = bars[i - 1]["c"]
            tr.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(tr) >= n:
        a = sum(tr[:n]) / n
        out[n - 1] = a
        for i in range(n, len(tr)):
            a = (a * (n - 1) + tr[i]) / n
            out[i] = a
    return out


def wilder_rsi(c, n=14):
    out = [None] * len(c)
    if len(c) < n + 1:
        return out
    g = [max(0.0, c[i] - c[i - 1]) for i in range(1, len(c))]
    lo = [max(0.0, c[i - 1] - c[i]) for i in range(1, len(c))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    out[n] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
        out[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    return out


class Series:
    def __init__(self, bars):
        self.bars = bars
        self.d = [b["d"] for b in bars]
        self.o = [b["o"] for b in bars]
        self.h = [b["h"] for b in bars]
        self.l = [b["l"] for b in bars]
        self.c = [b["c"] for b in bars]
        self.v = [b["v"] for b in bars]
        self.ma = {n: sma(self.c, n) for n in (10, 20, 50, 200)}
        self.vma50 = sma(self.v, 50)
        self.atr = wilder_atr(bars)
        self.rsi = wilder_rsi(self.c)
        n = len(bars)
        self.hi52, self.lo52 = [None] * n, [None] * n
        for i in range(n):
            j = max(0, i - 251)
            self.hi52[i] = max(self.h[j:i + 1])
            self.lo52[i] = min(self.l[j:i + 1])


SETUP_LABEL = {
    "breakout_20d": "20-day closing-high breakout",
    "bull_flag": "bull flag",
    "reclaim_ma20": "reclaimed the 20-day average",
    "oversold": "oversold (RSI under 30)",
    "oversold_bounce": "oversold bounce",
}


def setups_at(S, i):
    """The trader-scanner daily rules, on closes, at bar i."""
    if i < 30:
        return []
    c, tags = S.c, []
    if c[i] > max(c[i - 20:i]):
        tags.append("breakout_20d")
    run = c[i - 5] / c[i - 15] - 1
    rng5 = (max(c[i - 4:i + 1]) - min(c[i - 4:i + 1])) / c[i]
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if S.ma[20][i - 1] and c[i - 1] < S.ma[20][i - 1] and c[i] > S.ma[20][i]:
        tags.append("reclaim_ma20")
    r, rp = S.rsi[i], S.rsi[i - 1]
    if r is not None and r < 30:
        tags.append("oversold")
    if rp is not None and rp < 30 and c[i] > c[i - 1]:
        tags.append("oversold_bounce")
    return tags


def state_at(S, i):
    """Shape (is the trend healthy?), price (is now a good price?) and the verdict."""
    c = S.c[i]
    ma = {n: S.ma[n][i] for n in (10, 20, 50, 200)}
    atr = S.atr[i] or (c * 0.02)
    rsi = S.rsi[i]
    ma50p = S.ma[50][i - 20] if i >= 20 else None
    ret21 = c / S.c[i - 21] - 1 if i >= 21 else None
    ret63 = c / S.c[i - 63] - 1 if i >= 63 else None
    hi52, lo52 = S.hi52[i], S.lo52[i]
    tags = setups_at(S, i)

    checks = []  # (label when true, label when false, value or None)

    def chk(t, f, v):
        checks.append((t, f, v))
    chk("above the 200-day average", "below the 200-day average", None if ma[200] is None else c > ma[200])
    chk("50-day above 200-day (uptrend stack)", "50-day under 200-day (downtrend stack)",
        None if ma[200] is None or ma[50] is None else ma[50] > ma[200])
    chk("50-day average rising", "50-day average falling", None if ma50p is None or ma[50] is None else ma[50] > ma50p)
    chk("above the 50-day average", "below the 50-day average", None if ma[50] is None else c > ma[50])
    chk("above the 20-day average", "below the 20-day average", None if ma[20] is None else c > ma[20])
    chk("up over 3 months", "down over 3 months", None if ret63 is None else ret63 > 0)
    chk("within 25% of the 52-week high", "more than 25% under the 52-week high", c >= 0.75 * hi52)
    known = [x for x in checks if x[2] is not None]
    score = sum(1 for x in known if x[2])
    frac = score / len(known) if known else 0
    shape = "STRONG" if frac >= 0.85 else "OK" if frac >= 0.57 else "WEAK"

    ext20 = (c / ma[20] - 1) * 100 if ma[20] else 0.0
    ext_atr = (c - ma[20]) / atr if ma[20] else 0.0
    over = []
    if ext20 > 8:
        over.append("%.1f%% above the 20-day" % ext20)
    if ext_atr > 2.5:
        over.append("%.1f ATR above the 20-day" % ext_atr)
    if rsi is not None and rsi > 72:
        over.append("RSI %.0f" % rsi)
    if ret21 is not None and ret21 > 0.25:
        over.append("up %.0f%% in a month" % (ret21 * 100))
    good = []
    if ma[20] and abs(c - ma[20]) <= atr and (ma[50] is None or c >= ma[50]):
        good.append("near the 20-day (%+.1f ATR)" % ext_atr)
    if (ma[50] and ma[50] - 0.5 * atr <= c <= ma[50] + atr and c > S.c[i - 1]
            and ((ma[200] and ma[50] > ma[200]) or (ma50p and ma[50] > ma50p))):
        good.append("pullback to the 50-day, turning up")
    fresh = [t for t in tags if t in ("breakout_20d", "bull_flag", "reclaim_ma20")]
    if fresh and ext_atr <= 1.5:
        good.append(", ".join(SETUP_LABEL[t] for t in fresh))
    price = "OVEREXTENDED" if over else ("GOOD" if good else "FAIR")

    if shape in ("STRONG", "OK"):
        verdict = {"GOOD": "BUY ZONE", "OVEREXTENDED": "WAIT", "FAIR": "WATCH"}[price]
    else:
        verdict = "SPECULATIVE" if ("oversold" in tags or "oversold_bounce" in tags) else "AVOID"

    zone = None
    if ma[20]:
        lo = ma[20] - atr
        if ma[50] and ma[50] < ma[20]:
            lo = max(lo, ma[50])
        hi = min(ma[20] + atr, ma[20] * 1.08, ma[20] + 2.5 * atr)
        zone = (round(lo, 2), round(hi, 2))

    return {
        "i": i, "date": S.d[i], "close": c, "ma": ma, "atr": atr, "atr_pct": atr / c * 100, "rsi": rsi,
        "ret21": ret21, "ret63": ret63, "hi52": hi52, "lo52": lo52, "ext20": ext20, "ext_atr": ext_atr,
        "setups": tags, "checks": checks, "score": score, "of": len(known), "shape": shape,
        "price": price, "over": over, "good": good, "verdict": verdict, "zone": zone,
        "vol_x": (S.v[i] / S.vma50[i]) if S.vma50[i] else None,
        "dollar_vol": (S.vma50[i] or 0) * c,
    }


# ================================================================ plan (exit, swing version)

def floor_cent(x):
    return math.floor(x * 100 + 1e-9) / 100


def round_cent(x):
    return math.floor(x * 100 + 0.5) / 100


def make_plan(S, i, entry, stop_rule, target_rule):
    st = state_at(S, i)
    atr = st["atr"]
    rule = (stop_rule or "swing").lower()
    note = ""
    if rule == "swing":
        low10 = min(S.l[max(0, i - 9):i + 1])
        raw = low10 - 0.2 * atr
        why = "10-day swing low %.2f minus 0.2 ATR" % low10
        if entry - raw < atr:
            raw, note = entry - atr, "10-day swing low %.2f was closer than 1 ATR" % low10
            why = "1 x ATR(14) under the entry"
        elif entry - raw > 3 * atr:
            raw, note = entry - 3 * atr, "10-day swing low %.2f was further than 3 ATR" % low10
            why = "3 x ATR(14) under the entry (cap)"
    elif rule.startswith("atr:"):
        k = float(rule[4:])
        raw, why = entry - k * atr, "%.1f x ATR(14) %.2f under the entry" % (k, atr)
    elif rule.startswith("pct:"):
        p = float(rule[4:])
        raw, why = entry * (1 - p / 100), "%.1f%% under the entry" % p
    elif rule.startswith("price:"):
        raw, why = float(rule[6:]), "your price"
    elif rule in ("ma50", "ma20"):
        n = int(rule[2:])
        if not st["ma"][n]:
            raise DataError("not enough bars for the %d-day average" % n)
        raw, why = st["ma"][n] - 0.2 * atr, "%d-day average %.2f minus 0.2 ATR" % (n, st["ma"][n])
    else:
        raise DataError("unknown stop rule %r (swing, atr:K, pct:X, price:X, ma20, ma50)" % stop_rule)
    stop = floor_cent(raw)
    if stop >= entry:
        raise DataError("stop %.2f is not below the entry %.2f (%s)" % (stop, entry, why))
    risk = entry - stop
    if risk < 0.005 * entry:
        raise DataError("stop %.2f is too tight (%.2f%%): normal noise would hit it" % (stop, risk / entry * 100))
    tr = (target_rule or "2R").upper()
    if tr.endswith("R"):
        r = float(tr[:-1])
        target = round_cent(entry + r * risk)
        twhy = "%gR" % r
    elif tr.startswith("PRICE:"):
        target = float(tr[6:])
        twhy = "your price"
    else:
        raise DataError("unknown target %r (2R, 1.5R, price:X)" % target_rule)
    if target <= entry:
        raise DataError("target %.2f is not above the entry %.2f" % (target, entry))
    rr = (target - entry) / risk
    notes = [note] if note else []
    if entry < st["hi52"] < target:
        notes.append("52-week high %.2f sits between the entry and the target: likely resistance" % st["hi52"])
    return {"entry": round(entry, 2), "stop": stop, "stop_rule": rule, "stop_why": why, "target": target,
            "target_why": twhy, "risk": round(risk, 4), "risk_pct": risk / entry * 100, "rr": rr,
            "notes": notes}


# ================================================================ time machine (daily)

def tm_verdict(tf, sf, nn, r):
    hit = tf + sf
    if hit == 0:
        return "Neither side was hit in any replay.", []
    share = tf / hit * 100
    be = 100 / (1 + r)
    d = share - be
    side = "target" if d > 0 else "stop"
    if abs(d) < 5:
        label = "Close to a coin flip" if abs(r - 1) < 1e-9 else "Close to break-even"
    elif abs(d) < 15:
        label = "Leans toward the " + side
    else:
        label = "Strongly toward the " + side
    line = "%s: target first %.1f%% vs stop first %.1f%% (break-even needs the target in %.0f%% of the replays that hit a side; it got %.0f%%)." % (
        label, tf, sf, be, share)
    return line, []


def simulate(S, i, plan, days, replays, seed, pool_n, drift):
    start = max(1, i - pool_n + 1)
    pool = [(S.o[j] / S.c[j - 1], S.h[j] / S.c[j - 1], S.l[j] / S.c[j - 1], S.c[j] / S.c[j - 1])
            for j in range(start, i + 1)]
    if len(pool) < 120:
        raise DataError("only %d daily bars to replay from; need 120" % len(pool))
    g = math.exp(sum(math.log(p[3]) for p in pool) / len(pool))
    if drift == "zero":
        pool = [tuple(x / g for x in p) for p in pool]
        gday = 0.0
    else:
        gday = (g - 1) * 100
    rnd = random.Random(seed)
    E, ST, T, risk = plan["entry"], plan["stop"], plan["target"], plan["entry"] - plan["stop"]
    rr = (T - E) / risk
    by_day = [[] for _ in range(days)]
    tf = sf = nn = green = gaps = 0
    rsum = 0.0
    for _ in range(replays):
        p, hit = E, None
        for d in range(days):
            o, h, l, c = rnd.choice(pool)
            O, Hh, L, C = p * o, p * h, p * l, p * c
            if hit is None:
                if L <= ST:
                    hit = "stop"
                    gaps += O <= ST
                elif Hh >= T:
                    hit = "target"
            by_day[d].append(C)
            p = C
        if hit == "target":
            tf += 1
            green += 1
            rsum += rr
        elif hit == "stop":
            sf += 1
            rsum -= 1
        else:
            nn += 1
            green += p > E
            rsum += (p - E) / risk
    pct = lambda v, q: v[min(len(v) - 1, int(q * len(v)))]
    fan = []
    for d in range(days):
        v = sorted(by_day[d])
        fan.append([pct(v, q) for q in (0.10, 0.25, 0.50, 0.75, 0.90)])
    n = float(replays)
    out = {"days": days, "replays": replays, "seed": seed, "pool": len(pool), "drift": drift,
           "drift_pct_day": gday, "target_first": tf / n * 100, "stop_first": sf / n * 100,
           "neither": nn / n * 100, "green": green / n * 100, "avg_r": rsum / n, "gaps": gaps / n * 100,
           "fan": fan, "end": fan[-1], "rr": rr}
    v, extra = tm_verdict(out["target_first"], out["stop_first"], out["neither"], rr)
    out["verdict"] = v
    lines = []
    if out["neither"] > 40:
        lines.append("%.1f%% of the replays hit neither side in %d sessions; the price then decides those." % (out["neither"], days))
    lean_target = "toward the target" in v
    lean_stop = "toward the stop" in v
    if (lean_target and out["avg_r"] < 0) or (lean_stop and out["avg_r"] > 0):
        lines.append("Average R is %+.2f, against the lean, because of the replays that hit neither." % out["avg_r"])
    out["lines"] = lines
    return out


def history(S, i, plan, days, verdict_now):
    """The plan's stop/target geometry (in ATRs) replayed on every past day, and on the
    days this checker gave the same verdict. Real bars, overlapping windows."""
    atr_now = state_at(S, i)["atr"]
    k = (plan["entry"] - plan["stop"]) / atr_now
    r = (plan["target"] - plan["entry"]) / (plan["entry"] - plan["stop"])
    start = 200 if i - days > 260 else 60
    res = {"all": [0, 0, 0, 0.0], "same": [0, 0, 0, 0.0], "k_atr": k, "r": r, "days": days}
    for j in range(start, i - days + 1):
        a = S.atr[j]
        if not a:
            continue
        e = S.c[j]
        st, tg = e - k * a, e + r * k * a
        out, R = None, 0.0
        for m in range(j + 1, j + days + 1):
            if S.l[m] <= st:
                out, R = 2, -1.0
                break
            if S.h[m] >= tg:
                out, R = 1, r
                break
        if out is None:
            out, R = 3, (S.c[j + days] - e) / (k * a)
        for key in ("all",) + (("same",) if state_at(S, j)["verdict"] == verdict_now else ()):
            res[key][out - 1] += 1
            res[key][3] += R
    for key in ("all", "same"):
        t = sum(res[key][:3])
        res[key] = {"n": t, "target": res[key][0], "stop": res[key][1], "neither": res[key][2],
                    "avg_r": (res[key][3] / t) if t else None}
    return res


# ================================================================ size (budget, GBP)

def size(plan, fx, cfg, cash=None):
    fee = cfg["fx_fee_pct"] / 100.0
    e, s, t = plan["entry"] / fx, plan["stop"] / fx, plan["target"] / fx
    cost_ps = e * (1 + fee)
    loss_ps = (e - s) + fee * (e + s)
    budget = float(cfg["budget_gbp"])
    buffer = budget * cfg["cash_buffer_pct"] / 100.0
    cash_av = cash if cash is not None else (cfg["cash_gbp"] if cfg["cash_gbp"] is not None else budget)
    limits = {
        "max loss": cfg["max_loss_per_trade_gbp"] / loss_ps,
        "position cap": budget * cfg["max_position_pct"] / 100.0 / cost_ps,
        "max order": cfg["max_order_gbp"] / cost_ps,
        "cash after buffer": max(0.0, cash_av - buffer) / cost_ps,
    }
    step = cfg["share_step"] if cfg["fractional"] else 1.0
    decided = min(limits, key=limits.get)
    qty = math.floor(limits[decided] / step + 1e-9) * step
    qty = round(qty, 6)
    value = qty * cost_ps
    out = {"qty": qty, "decided_by": decided, "limits": {k: round(v, 4) for k, v in limits.items()},
           "entry_gbp": e, "stop_gbp": s, "target_gbp": t, "cost_gbp": value, "max_loss_gbp": qty * loss_ps,
           "reward_gbp": qty * ((t - e) - fee * (e + t)), "fee_pct": cfg["fx_fee_pct"], "cash_gbp": cash_av,
           "refused": None}
    if qty <= 0:
        out["refused"] = "zero shares fit: the %s limit allows %.4f" % (decided, limits[decided])
    elif value < cfg["min_order_gbp"]:
        out["refused"] = "order £%.2f is under your £%.2f minimum" % (value, cfg["min_order_gbp"])
    return out


# ================================================================ rules gate (risk)

def gate(sym, st, plan, sz, mc, cfg, info, earnings=None, held=None, n_positions=None, now_utc=None):
    out = []

    def add(level, name, text):
        out.append({"level": level, "check": name, "text": text})
    if sym.upper() in [b.upper() for b in cfg["blocked"]]:
        add("BLOCK", "blocked", "%s is on your blocked list" % sym)
    if plan["stop"] >= plan["entry"]:
        add("BLOCK", "stop", "the stop is not below the entry")
    else:
        add("PASS", "stop", "stop %.2f, %.1f%% under the entry (%s)" % (plan["stop"], plan["risk_pct"], plan["stop_why"]))
    if plan["rr"] + 1e-9 < cfg["min_reward_risk"]:
        add("BLOCK", "reward:risk", "%.2f is under your minimum %.2f" % (plan["rr"], cfg["min_reward_risk"]))
    else:
        add("PASS", "reward:risk", "%.2f (min %.2f)" % (plan["rr"], cfg["min_reward_risk"]))
    v = st["verdict"]
    if v == "AVOID":
        add("BLOCK", "chart", "weak chart (%d of %d trend checks): no new buys" % (st["score"], st["of"]))
    elif v == "WAIT":
        z = st["zone"]
        add("BLOCK", "price", "overextended (%s); wait for %.2f-%.2f" % (", ".join(st["over"]), z[0], z[1]) if z else
            "overextended (%s)" % ", ".join(st["over"]))
    elif v == "SPECULATIVE":
        lvl = "WARN" if cfg["allow_counter_trend"] else "BLOCK"
        add(lvl, "chart", "counter-trend bounce in a weak chart")
    elif v == "WATCH":
        z = st["zone"]
        add("WARN", "price", "fair price, not at a level; the better zone is %.2f-%.2f" % z if z else "fair price, not at a level")
    else:
        add("PASS", "chart", "%s chart, %s" % (st["shape"].lower(), "; ".join(st["good"])))
    if sz:
        if sz["refused"]:
            add("BLOCK", "size", sz["refused"])
        else:
            add("PASS", "loss", "£%.2f at the stop with fees (limit £%.0f)" % (sz["max_loss_gbp"], cfg["max_loss_per_trade_gbp"]))
    if earnings:
        today = to_ny(now_utc).date().isoformat()
        n = trading_days_between(today, earnings) if earnings >= today else -1
        if n < 0:
            add("PASS", "earnings", "last report %s" % earnings)
        elif n <= cfg["block_earnings_within_days"]:
            add("BLOCK", "earnings", "reports %s, %d sessions away: a gap can jump past any stop" % (earnings, n))
        elif n <= cfg["hold_days"]:
            add("WARN", "earnings", "reports %s, inside the %d-session plan (%d sessions away)" % (earnings, cfg["hold_days"], n))
        else:
            add("PASS", "earnings", "next report %s, after the plan window" % earnings)
    else:
        add("WARN", "earnings", "next report date unknown: check it before buying")
    if mc:
        if "toward the stop" in mc["verdict"]:
            add("WARN", "replays", mc["verdict"].split(":")[0])
        else:
            add("PASS", "replays", mc["verdict"].split(":")[0])
    if held:
        add("WARN", "position", "you already hold %s: this adds to it" % sym)
    if n_positions is not None and not held and n_positions >= cfg["max_positions"]:
        add("BLOCK", "positions", "%d positions open, your cap is %d" % (n_positions, cfg["max_positions"]))
    if st["dollar_vol"] and st["dollar_vol"] < cfg["min_dollar_volume_usd"] / 4:
        add("WARN", "liquidity", "about $%.1fM traded a day: wide spreads" % (st["dollar_vol"] / 1e6))
    if info.get("live"):
        add("WARN", "data", "last bar is today's live bar (delayed); the close can differ")
    age = (to_ny(now_utc).date() - dt.date.fromisoformat(st["date"])).days
    if age > 4:
        add("WARN", "data", "last bar is %d days old (%s)" % (age, st["date"]))
    overall = "BLOCK" if any(x["level"] == "BLOCK" for x in out) else "PASS"
    return overall, out


# ================================================================ bull / bear bullets

def bull_bear(st, plan, mc, fx):
    bull, bear = [], []
    for t, f, v in st["checks"]:
        if v is True:
            bull.append(t)
        elif v is False:
            bear.append(f)
    for s in st["setups"]:
        (bull if s in ("breakout_20d", "bull_flag", "reclaim_ma20", "oversold_bounce") else bear).append(SETUP_LABEL[s])
    if st["good"]:
        bull.append("good price: " + "; ".join(st["good"]))
    if st["over"]:
        bear.append("overextended: " + ", ".join(st["over"]))
    if st["rsi"] is not None and st["rsi"] < 35:
        bear.append("RSI %.0f: selling pressure" % st["rsi"])
    if st["atr_pct"] > 4:
        bear.append("volatile: daily range (ATR) is %.1f%% of the price" % st["atr_pct"])
    if st["vol_x"] and st["vol_x"] > 1.5:
        (bull if st["close"] >= st["ma"][20] else bear).append("volume %.1fx its 50-day average" % st["vol_x"])
    if mc:
        e = mc["end"]
        bull.append("replay p90 in %d sessions: £%.2f (%+.1f%%)" % (mc["days"], e[4] / fx, (e[4] / plan["entry"] - 1) * 100))
        bear.append("replay p10 in %d sessions: £%.2f (%+.1f%%)" % (mc["days"], e[0] / fx, (e[0] / plan["entry"] - 1) * 100))
    for n in plan["notes"]:
        if "resistance" in n:
            bear.append(n)
    return bull[:7], bear[:7]


# ================================================================ one full check

def full_check(sym, a, cfg, fx, now_utc, entry=None, holding=None, n_positions=None, earnings=None):
    bars, info = get_bars(sym, a, now_utc)
    if len(bars) < 60:
        raise DataError("%s: only %d daily bars" % (sym, len(bars)))
    S = Series(bars)
    i = len(bars) - 1
    st = state_at(S, i)
    e = entry if entry else st["close"]
    plan = make_plan(S, i, e, a.stop or cfg["stop_rule"], a.target or ("%gR" % cfg["target_r"]))
    days = a.days or cfg["hold_days"]
    mc = simulate(S, i, plan, days, a.replays or cfg["replays"], cfg["seed"] if a.seed is None else a.seed,
                  cfg["pool_days"], a.drift or cfg["drift"])
    hist = history(S, i, plan, days, st["verdict"])
    sz = size(plan, fx, cfg)
    overall, checks = gate(sym, st, plan, sz, mc, cfg, info, earnings, holding is not None, n_positions, now_utc)
    bull, bear = bull_bear(st, plan, mc, fx)
    return {"symbol": sym.upper(), "name": info.get("name", ""), "info": info, "state": st, "plan": plan,
            "mc": mc, "history": hist, "size": sz, "gate": overall, "checks": checks, "bull": bull,
            "bear": bear, "earnings": earnings, "bars": bars, "fx": fx}


# ================================================================ holdings (stockchecking)

def holding_call(sym, h, a, cfg, fx, now_utc, earnings=None):
    bars, info = get_bars(sym, a, now_utc)
    S = Series(bars)
    i = len(bars) - 1
    st = state_at(S, i)
    c, atr, ma = st["close"], st["atr"], st["ma"]
    avg = h.get("avg")
    pl = (c / avg - 1) if avg else None
    stop = h.get("stop")
    stop_src = "yours"
    if not stop:
        base = min(min(S.l[max(0, i - 9):i + 1]), ma[50] or c) - 0.2 * atr
        stop = floor_cent(min(max(base, c - 2.5 * atr), c - 1.5 * atr))
        stop_src = "auto: swing low / 50-day, 1.5-2.5 ATR under the price"
    lower_highs = i >= 20 and max(S.h[i - 9:i + 1]) < max(S.h[i - 19:i - 9])
    below_all = all(ma[n] is not None and c < ma[n] for n in (10, 20, 50, 200))
    earn_days = None
    if earnings:
        today = to_ny(now_utc).date().isoformat()
        earn_days = trading_days_between(today, earnings) if earnings >= today else None
    reasons, call, level = [], None, None
    if c < stop:
        call, reasons = "SELL TO STAY SAFE", ["closed under the stop %.2f" % stop]
    elif below_all and lower_highs:
        call, reasons = "SELL TO STAY SAFE", ["below all four averages with lower highs"]
    elif pl is not None and pl > 0 and (st["ext20"] > 10 or (st["rsi"] or 0) > 75
                                        or (c >= 0.97 * st["hi52"] and (st["rsi"] or 0) > 70)
                                        or (earn_days is not None and earn_days <= 10 and pl > 0.2)):
        call = "TRIM 1/3"
        if st["ext20"] > 10:
            reasons.append("%.1f%% above the 20-day" % st["ext20"])
        if (st["rsi"] or 0) > 70:
            reasons.append("RSI %.0f" % st["rsi"])
        if c >= 0.97 * st["hi52"] and (st["rsi"] or 0) > 70:
            reasons.append("at the 52-week high")
        if earn_days is not None and earn_days <= 10 and pl > 0.2:
            reasons.append("earnings in %d sessions on a %.0f%% gain" % (earn_days, pl * 100))
        level = floor_cent(max(stop, (ma[20] or c) - 0.25 * atr))
    elif ma[20] and ma[50] and c > ma[20] and c > ma[50]:
        call, reasons = "HOLD", ["above the 20- and 50-day averages and the stop"]
    else:
        call = "WATCH STOP"
        if ma[50] and c < ma[50]:
            reasons.append("under the 50-day")
        if ma[200] and c < ma[200]:
            reasons.append("under the 200-day")
        if not reasons:
            reasons.append("under the 20-day")
        reasons.append("a close under %.2f turns it into a sell" % stop)
    near = (c - stop) / c < 0.05
    plan = {"entry": c, "stop": stop, "target": None, "notes": []}
    pl_gbp = (c - avg) * h["qty"] / fx if avg and h.get("qty") else None
    bull, bear = bull_bear(st, {"entry": c, "notes": []}, None, fx)
    return {"symbol": sym, "info": info, "state": st, "holding": h, "call": call, "reasons": reasons,
            "stop": stop, "stop_src": stop_src, "near_stop": near, "pl_pct": pl, "pl_gbp": pl_gbp,
            "value_gbp": c * h.get("qty", 0) / fx, "new_stop": level, "bars": bars, "fx": fx,
            "earnings": earnings, "bull": bull, "bear": bear, "plan": plan}


def t212_portfolio():
    key = os.environ.get("T212_API_KEY")
    if not key:
        raise DataError("set T212_API_KEY (and T212_API_SECRET if your key has one) in the environment")
    sec = os.environ.get("T212_API_SECRET")
    base = os.environ.get("T212_BASE", "https://live.trading212.com")
    auth = "Basic " + base64.b64encode(("%s:%s" % (key, sec)).encode()).decode() if sec else key
    hdr = {"Authorization": auth}
    pos = json.loads(http_get(base + "/api/v0/equity/portfolio", hdr))
    try:
        cash = json.loads(http_get(base + "/api/v0/equity/account/cash", hdr))
    except Exception:
        cash = {}
    out, skipped = [], []
    for p in pos:
        t = p.get("ticker", "")
        m = re.match(r"^([A-Z]{1,5}(?:\.[A-Z])?)_US_EQ$", t)
        if not m:
            skipped.append(t)
            continue
        out.append({"ticker": m.group(1), "qty": p.get("quantity"), "avg": p.get("averagePrice"),
                    "t212_ppl_gbp": p.get("ppl")})
    return {"holdings": out, "cash_gbp": cash.get("free"), "total_gbp": cash.get("total"), "skipped": skipped,
            "source": "Trading 212 API (read only)"}


# ================================================================ scan (scanner, then checker)

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = re.compile(r"\bwarrants?\b|\b(units?|rights?),? (each|that|entitling)\b|\b(notes?|etns?|debentures?)\b|%"
                        r"|(?<!american )(?<!global )\bdepositary shares?\b")
COMMON = re.compile(r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
                    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b")
SOFT_OUT = re.compile(r"\bunits?\b|\brights?\b|\bbonds?\b|\bpreferred\b|\bpreference\b")


def universe(cache_dir):
    path = os.path.join(cache_dir, "universe.txt")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400:
        with open(path) as f:
            return [ln.split("|")[0] for ln in f.read().split("\n") if ln.strip()]
    keep = {}
    for fname in ("nasdaqlisted.txt", "otherlisted.txt"):
        text = http_get("https://www.nasdaqtrader.com/dynamic/SymDir/" + fname, timeout=60)
        lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
        hdr = lines[0].split("|")
        for ln in lines[1:]:
            if ln.startswith("File Creation"):
                continue
            r = dict(zip(hdr, ln.split("|")))
            sym = (r.get("Symbol") or r.get("ACT Symbol") or "").strip()
            name = " ".join((r.get("Security Name") or "").lower().split())
            if (r.get("Test Issue") or "").strip() != "N" or (r.get("ETF") or "").strip() != "N":
                continue
            if not TICKER.match(sym) or re.search(r"\.[UWR]$", sym) or ALWAYS_OUT.search(name):
                continue
            if not COMMON.search(name) and SOFT_OUT.search(name):
                continue
            keep[sym] = name
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join("%s|%s" % kv for kv in sorted(keep.items())))
    return sorted(keep)


def spark(symbols, pause=0.3):
    data, failed = {}, 0
    for k in range(0, len(symbols), 20):
        batch = symbols[k:k + 20]
        if k:
            time.sleep(pause)
        try:
            doc = yahoo("/v7/finance/spark?symbols=%s&range=1y&interval=1d" % ",".join(ysym(s) for s in batch))
        except DataError:
            failed += len(batch)
            continue
        want = {ysym(s): s for s in batch}
        for item in ((doc.get("spark") or {}).get("result") or []):
            resp = item.get("response") or []
            if not resp:
                continue
            ys = (item.get("symbol") or "").upper()
            r = resp[0]
            t = r.get("timestamp") or []
            c = (((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            rows = [{"d": ts_to_ny(ts).date().isoformat(), "o": c[j], "h": c[j], "l": c[j], "c": c[j], "v": 0}
                    for j, ts in enumerate(t) if j < len(c) and c[j] is not None]
            meta = r.get("meta") or {}
            data[want.get(ys, ys)] = (clean_bars(rows), meta.get("regularMarketVolume") or 0)
        failed += len([s for s in batch if s not in data])
        if len(symbols) > 400 and (k // 20) % 50 == 49:
            print("  ... %d of %d" % (k + 20, len(symbols)), file=sys.stderr)
    return data, failed


# ================================================================ news + earnings (network)

def news(sym, hours=72):
    text = http_get("https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US" % ysym(sym))
    items = []
    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=hours)
    for it in re.findall(r"<item>(.*?)</item>", text, re.S):
        g = lambda tag: (re.search(r"<%s>(.*?)</%s>" % (tag, tag), it, re.S) or [None, ""])[1]
        title = H.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", g("title"))).strip()
        when = g("pubDate").strip()
        try:
            t = dt.datetime.strptime(when[:25], "%a, %d %b %Y %H:%M:%S")
        except ValueError:
            t = None
        if t and t < cutoff:
            continue
        items.append({"time_ny": to_ny(t).strftime("%Y-%m-%d %H:%M") if t else when, "title": title,
                      "link": g("link").strip()})
    return items


def sec_ua(cfg):
    ua = cfg.get("sec_user_agent") or os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise DataError("SEC needs a contact: set sec_user_agent in the config, e.g. \"Your Name you@example.com\"")
    return ua


def cik_for(sym, ua):
    doc = json.loads(http_get("https://www.sec.gov/files/company_tickers.json", {"User-Agent": ua}))
    for v in doc.values():
        if v["ticker"].upper() == sym.upper().replace(".", "-"):
            return int(v["cik_str"])
    raise DataError("no SEC CIK for %s" % sym)


def sec_8k(sym, ua, n=5):
    cik = cik_for(sym, ua)
    text = http_get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=%d&type=8-K&dateb=&owner=include&count=%d&output=atom" % (cik, n),
                    {"User-Agent": ua})
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        g = lambda tag: (re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), e, re.S) or [None, ""])[1]
        link = (re.search(r'<link[^>]*href="([^"]+)"', e) or [None, ""])[1]
        out.append({"filed": g("filing-date") or g("updated")[:10], "items": H.unescape(g("items-desc") or g("summary"))[:160].strip(),
                    "link": link})
    return out


REV_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax"]


def sec_quarters(sym, ua, n=6):
    cik = cik_for(sym, ua)
    doc = json.loads(http_get("https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json" % cik, {"User-Agent": ua}))
    gaap = (doc.get("facts") or {}).get("us-gaap") or {}

    def q(tags, unit):
        """The tag whose quarterly frames run latest (companies switch revenue tags over the years)."""
        best, best_end = {}, ""
        for t in tags:
            vals = ((gaap.get(t) or {}).get("units") or {}).get(unit) or []
            fr = {v["frame"]: v for v in vals if re.fullmatch(r"CY\d{4}Q\d", v.get("frame", ""))}
            end = max((v["end"] for v in fr.values()), default="")
            if end > best_end:
                best, best_end = fr, end
        return best
    rev, ni = q(REV_TAGS, "USD"), q(["NetIncomeLoss"], "USD")
    eps, gp = q(["EarningsPerShareDiluted"], "USD/shares"), q(["GrossProfit"], "USD")
    frames = sorted(set(rev) | set(ni) | set(eps), key=lambda f: (int(f[2:6]), int(f[7])))[-n:]
    rows = []
    for f in frames:
        prev = "CY%d%s" % (int(f[2:6]) - 1, f[6:])
        val = lambda d, k: d[k]["val"] if k in d else None
        r_, rp = val(rev, f), val(rev, prev)
        e_, ep = val(eps, f), val(eps, prev)
        rows.append({"quarter": f[2:], "end": (rev.get(f) or ni.get(f) or eps.get(f))["end"], "revenue": r_,
                     "rev_yoy": (r_ / rp - 1) * 100 if r_ and rp else None, "net_income": val(ni, f),
                     "eps": e_, "eps_yoy": (e_ / ep - 1) * 100 if e_ is not None and ep else None,
                     "gross_margin": (val(gp, f) / r_ * 100) if val(gp, f) and r_ else None,
                     "filed": (rev.get(f) or ni.get(f) or eps.get(f))["filed"]})
    return rows


def yahoo_earnings_date(sym):
    """Best effort; Yahoo often wants a cookie for this. Returns 'YYYY-MM-DD' or None."""
    try:
        doc = json.loads(http_get(YAHOO[0] + "/v7/finance/quote?symbols=%s" % ysym(sym), timeout=8))  # one quick try
        r = ((doc.get("quoteResponse") or {}).get("result") or [{}])[0]
        ts = r.get("earningsTimestampStart") or r.get("earningsTimestamp")
        return ts_to_ny(ts).date().isoformat() if ts else None
    except Exception:
        return None



# ================================================================ entry timer (trader-entry, swing version)

STEP5 = 300


def _hm(text):
    h, mi = text.split(":")
    return dt.time(int(h), int(mi))


def intraday_from_chart(doc, sym):
    res = ((doc or {}).get("chart") or {}).get("result")
    if not res:
        raise DataError("no 5-minute data for %s" % sym)
    r = res[0]
    t = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    col = lambda k: (q.get(k) or []) + [None] * len(t)
    o, h, l, c, v = col("open"), col("high"), col("low"), col("close"), col("volume")
    out = []
    for i, ts in enumerate(t):
        if ts % STEP5 or None in (o[i], h[i], l[i], c[i]):
            continue  # Yahoo's live tick (10:49:45) is not a bar
        out.append({"t": ts, "o": o[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i] or 0})
    return out, (r.get("meta") or {}).get("regularMarketTime")


def intraday(sym, a):
    if a.data:
        p = os.path.join(a.data, sym.upper() + "_5m.json")
        if not os.path.exists(p):
            return [], None
        with open(p) as f:
            return intraday_from_chart(json.load(f), sym)
    return intraday_from_chart(yahoo("/v8/finance/chart/%s?range=1d&interval=5m&includePrePost=false" % ysym(sym)), sym)


def entry_eval(bars5, now_utc, mode, level, zone, cfg):
    """The first COMPLETED 5-minute bar that confirms the entry. A wick is not a trigger."""
    local = to_ny(now_utc)
    day = local.date()
    res = {"mode": mode, "status": "waiting", "reason": "", "level": level, "zone": zone, "trigger": None,
           "last": None, "forming": None, "orb": None}
    s = session(day)
    if not s:
        res.update(status="closed", reason="the NYSE is closed today")
        return res
    open_dt, close_dt = dt.datetime.combine(day, s[0]), dt.datetime.combine(day, s[1])
    before = dt.datetime.combine(day, _hm(cfg["entry_from"]))
    cutoff = min(dt.datetime.combine(day, _hm(cfg["entry_until"])), close_dt - dt.timedelta(minutes=30))
    res["window"] = (before, cutoff)
    if local < open_dt:
        res.update(status="pre-open", reason="the NYSE opens at 9:30 New York")
        return res
    closed = []
    for b in bars5:
        et = ts_to_ny(b["t"])
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        end = et + dt.timedelta(seconds=STEP5)
        if end <= local:
            closed.append(dict(b, et=et, end=end))
        elif et <= local:
            res["forming"] = {"start": et, "price": b["c"]}
    if mode == "orb":
        rng = [b for b in closed if b["et"] < open_dt + dt.timedelta(minutes=15)]
        if local < open_dt + dt.timedelta(minutes=15) or not rng:
            res.update(reason="the 15-minute opening range is still forming")
            return res
        res["orb"] = (max(b["h"] for b in rng), min(b["l"] for b in rng))
        level = res["level"] = res["orb"][0]
    if closed:
        res["last"] = closed[-1]
    for b in closed:
        if b["end"] < before or b["end"] > cutoff:
            continue
        if mode in ("orb", "level") and b["c"] > level:
            res.update(status="triggered", trigger=b, reason="the %s bar closed above $%.2f" % (b["et"].strftime("%H:%M"), level))
            return res
        if mode == "zone" and b["l"] <= zone[1] and b["c"] >= zone[0]:
            res.update(status="triggered", trigger=b,
                       reason="the %s bar traded into the zone and closed at $%.2f, inside it" % (b["et"].strftime("%H:%M"), b["c"]))
            return res
    if local >= cutoff:
        res.update(status="expired", reason="no trigger by %s New York, the last time for a new entry" % cutoff.strftime("%H:%M"))
        return res
    if not closed:
        res["reason"] = "no completed 5-minute bar yet"
    elif mode == "zone":
        c = closed[-1]["c"]
        res["reason"] = "last close $%.2f, %s the zone $%.2f-%.2f" % (c, "above" if c > zone[1] else "below", zone[0], zone[1])
    else:
        c = closed[-1]["c"]
        res["reason"] = "last close $%.2f, $%.2f %s the trigger $%.2f (%+.2f%%)" % (
            c, abs(level - c), "under" if c <= level else "over", level, (c / level - 1) * 100)
    return res


def ny_uk(local_ny):
    return "%s New York / %s UK" % (local_ny.strftime("%H:%M"), to_uk(ny_to_utc(local_ny)).strftime("%H:%M"))


def notify_mac(title, text):
    if sys.platform == "darwin":
        try:
            os.system("osascript -e %s >/dev/null 2>&1" % json.dumps(
                'display notification %s with title %s sound name "Glass"' % (json.dumps(text), json.dumps(title))))
        except Exception:
            pass


def entry_run(a, cfg, now, fx):
    sym = a.ticker.upper()
    bars, info = get_bars(sym, a, now)
    S = Series(bars)
    ny = to_ny(now)
    today = ny.date().isoformat()
    sess = session(ny.date())
    live_today = bool(sess) and ny.time() < sess[1]          # today's session not finished yet
    k = len(bars) - 2 if (bars[-1]["d"] == today and live_today) else len(bars) - 1   # judge on completed days
    st = state_at(S, k)
    prev = bars[k]
    mode = a.setup
    if not mode:
        if st["verdict"] == "BUY ZONE":
            mode = "level"
        elif st["verdict"] in ("WAIT", "WATCH"):
            mode = "zone"
        else:
            raise DataError("%s is %s (%d/%d trend checks): no entry plan for a weak chart. Use --setup to force one." % (
                sym, st["verdict"], st["score"], st["of"]))
    level = a.level if a.level else (floor_cent(prev["h"]) + 0.01 if mode == "level" else None)
    zone = st["zone"]
    if mode == "zone" and not zone:
        raise DataError("no buy zone yet (needs 20 days of bars)")
    if live_today:
        b5, _ = intraday(sym, a)
        ev = entry_eval(b5, now, mode, level, zone, cfg)
    else:   # after the close, a weekend or a holiday: plan the next session
        nxt = next_trading_days(today, 1)[0]
        d = dt.date.fromisoformat(nxt)
        ev = {"mode": mode, "status": "next", "level": level, "zone": zone, "trigger": None, "forming": None,
              "reason": "planned for %s (the next session)" % d.strftime("%a %d %b"),
              "window": (dt.datetime.combine(d, _hm(cfg["entry_from"])), dt.datetime.combine(d, _hm(cfg["entry_until"])))}
    level = ev["level"]
    if ev["trigger"]:
        entry = ev["trigger"]["c"]
    elif mode == "zone":
        entry = zone[1]
    else:
        entry = level
    plan = sz = None
    if entry:
        plan = make_plan(S, k, entry, a.stop or cfg["stop_rule"], a.target or ("%gR" % cfg["target_r"]))
        sz = size(plan, fx, cfg)
    return {"symbol": sym, "state": st, "prev": prev, "mode": mode, "ev": ev, "plan": plan, "size": sz, "fx": fx,
            "entry": entry, "info": info}


def entry_text(r, cfg):
    st, ev, p, sz, fx, sym = r["state"], r["ev"], r["plan"], r["size"], r["fx"], r["symbol"]
    L = []
    what = {"level": "close above yesterday's high" if not r.get("custom") else "close above your level",
            "orb": "close above the 15-minute opening range", "zone": "trade back into the buy zone"}[r["mode"]]
    L.append("%s  %s chart (%d/%d) · %s · entry rule: first completed 5-minute bar to %s" % (
        sym, st["verdict"], st["score"], st["of"], st["date"], what))
    if r["mode"] in ("level", "orb") and ev["level"]:
        L.append("  Trigger  $%.2f (£%.2f)%s" % (ev["level"], ev["level"] / fx,
                 "  = yesterday's high %.2f + 1 cent" % r["prev"]["h"] if r["mode"] == "level" else ""))
    if r["mode"] == "zone":
        z = ev["zone"]
        L.append("  Zone     $%.2f-%.2f (£%.2f-%.2f)" % (z[0], z[1], z[0] / fx, z[1] / fx))
    status = {"waiting": "WAITING", "triggered": "TRIGGERED", "expired": "EXPIRED", "pre-open": "NOT OPEN YET",
              "closed": "MARKET CLOSED", "next": "NEXT SESSION"}[ev["status"]]
    if ev["trigger"]:
        b = ev["trigger"]
        L.append("  %s at %s: $%.2f (£%.2f). %s." % (status, ny_uk(b["end"]), b["c"], b["c"] / fx, ev["reason"][0].upper() + ev["reason"][1:]))
    else:
        L.append("  %s: %s." % (status, ev["reason"]))
    if ev.get("forming"):
        f = ev["forming"]
        L.append("  The %s bar is still forming ($%.2f so far); it counts only once it closes." % (f["start"].strftime("%H:%M"), f["price"]))
    if ev.get("window"):
        w = ev["window"]
        L.append("  Entry window: %s to %s." % (ny_uk(w[0]), ny_uk(w[1])))
    if not p:
        return "\n".join(L)
    L.append("  Plan     entry $%.2f · stop $%.2f (%s, -%.1f%%) · target $%.2f (%s) · R:R %.2f" % (
        p["entry"], p["stop"], p["stop_why"], p["risk_pct"], p["target"], p["target_why"], p["rr"]))
    if sz["refused"]:
        L.append("  Size     refused: " + sz["refused"])
        return "\n".join(L)
    qty = sz["qty"]
    slip = cfg["entry_slippage_pct"] / 100.0
    L.append("")
    L.append("  Trading 212 order (you place it; this tool never does). Prices in USD as the order ticket shows them:")
    if ev["status"] == "triggered":
        L.append("    It has triggered. Market BUY %s shares now, or a Limit BUY at $%.2f to cap the price." % (m(qty), p["entry"] * (1 + slip)))
    elif r["mode"] in ("level", "orb"):
        L.append("    Stop-limit BUY %s shares · stop price $%.2f · limit price $%.2f · valid for the day" % (
            m(qty), ev["level"], round_cent(ev["level"] * (1 + slip))))
        L.append("    Trading 212 watches every tick and buys the moment the price reaches $%.2f, paying at most $%.2f." % (
            ev["level"], round_cent(ev["level"] * (1 + slip))))
    else:
        L.append("    Limit BUY %s shares · limit price $%.2f · good till cancelled" % (m(qty), ev["zone"][1]))
        L.append("    It fills only if the price comes back down to $%.2f or lower." % ev["zone"][1])
    L.append("    After it fills: Stop SELL at $%.2f (your stop) and, if you like, Limit SELL at $%.2f (target)." % (p["stop"], p["target"]))
    L.append("    About £%s with the 0.15%% FX fee · max loss £%s at the stop · size set by %s." % (
        m(sz["cost_gbp"]), m(sz["max_loss_gbp"]), sz["decided_by"]))
    L.append("    If Trading 212 won't take a fraction on this order type, round down to %d whole shares." % int(qty))
    return "\n".join(L)


def cmd_entry(a, cfg):
    fx, fxsrc = get_fx(a, cfg)
    if a.watch:
        return entry_watch(a, cfg, fx)
    r = entry_run(a, cfg, utc_now(a.now), fx)
    r["custom"] = bool(a.level)
    if a.json:
        out = dict(r)
        out.pop("info", None)
        print(json.dumps(out, indent=1, default=str))
    else:
        print(entry_text(r, cfg))
        print("\nFree Yahoo 5-minute bars can lag a minute or more; Trading 212's own stop-limit order watches every tick.")
        print(NOTE)
    return 0 if r["ev"]["status"] == "triggered" else 1


def entry_watch(a, cfg, fx):
    """Check after every 5-minute bar closes until it triggers or the window ends. Keep the Mac awake."""
    print("Watching %s on 5-minute bars. Keep this Mac awake and online. Ctrl-C to stop." % a.ticker.upper(), flush=True)
    errors = 0
    while True:
        now = utc_now(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        try:
            r = entry_run(a, cfg, now, fx)
            errors = 0
        except DataError as ex:
            errors += 1
            print("%s  data problem: %s" % (to_uk(now).strftime("%H:%M UK"), ex), flush=True)
            if errors >= 5:
                return 2
            time.sleep(60)
            continue
        ev = r["ev"]
        print("%s  %s" % (to_uk(now).strftime("%H:%M UK"), ev["status"].upper() + ": " + ev["reason"]), flush=True)
        if ev["status"] == "triggered":
            r["custom"] = bool(a.level)
            print(entry_text(r, cfg), flush=True)
            notify_mac("Entry: %s" % r["symbol"], "%s triggered at $%.2f" % (r["symbol"], ev["trigger"]["c"]))
            return 0
        if ev["status"] in ("expired", "closed") or a.now:
            notify_mac("Entry: %s" % r["symbol"], ev["reason"])
            return 1
        ny = to_ny(now)
        nxt = (ny.minute // 5 + 1) * 5
        wait = (nxt - ny.minute) * 60 - ny.second + 20   # just after the next bar closes
        if ev["status"] == "pre-open":
            s = session(ny.date())
            wait = max(wait, int((dt.datetime.combine(ny.date(), s[0]) - ny).total_seconds()) + 20)
        time.sleep(max(30, wait))


# ================================================================ text output

def m(x, nd=2):
    return "—" if x is None else ("{:,.%df}" % nd).format(x)


def text_check(r):
    st, p, mc, sz, hi, fx = r["state"], r["plan"], r["mc"], r["size"], r["history"], r["fx"]
    L = []
    ma = st["ma"]
    arrows = "  ".join("%d%s" % (n, "?" if ma[n] is None else ("▲" if st["close"] > ma[n] else "▼")) for n in (10, 20, 50, 200))
    L.append("%s  %s   close $%s (£%s)  %s%s" % (r["symbol"], st["verdict"], m(st["close"]), m(st["close"] / fx), st["date"],
                                                "  [live bar]" if r["info"].get("live") else ""))
    L.append("  Shape %s %d/%d · Price %s%s" % (st["shape"], st["score"], st["of"], st["price"],
                                               (" (" + "; ".join(st["good"] or st["over"]) + ")") if (st["good"] or st["over"]) else ""))
    L.append("  MAs %s   RSI %s  ATR $%s (%.1f%%)  1m %s  3m %s  52w %s-%s" % (
        arrows, m(st["rsi"], 0), m(st["atr"]), st["atr_pct"],
        "—" if st["ret21"] is None else "%+.1f%%" % (st["ret21"] * 100),
        "—" if st["ret63"] is None else "%+.1f%%" % (st["ret63"] * 100), m(st["lo52"]), m(st["hi52"])))
    if st["setups"]:
        L.append("  Setups: " + ", ".join(SETUP_LABEL[s] for s in st["setups"]))
    if st["verdict"] in ("WAIT", "WATCH") and st["zone"]:
        z = st["zone"]
        L.append("  Buy zone: $%s-%s (£%s-%s)" % (m(z[0]), m(z[1]), m(z[0] / fx), m(z[1] / fx)))
    L.append("  Plan  entry $%s £%s · stop $%s £%s (%s) -%.1f%% · target $%s £%s (%s) · R:R %.2f" % (
        m(p["entry"]), m(p["entry"] / fx), m(p["stop"]), m(p["stop"] / fx), p["stop_why"], p["risk_pct"],
        m(p["target"]), m(p["target"] / fx), p["target_why"], p["rr"]))
    for n in p["notes"]:
        L.append("    note: " + n)
    L.append("  Replays (%d sessions, %s, seed %d, %d-day pool, drift %s %+.2f%%/day):" % (
        mc["days"], "{:,}".format(mc["replays"]), mc["seed"], mc["pool"], mc["drift"], mc["drift_pct_day"]))
    L.append("    target first %.1f%% · stop first %.1f%% · neither %.1f%% · green %.1f%% · avg R %+.2f" % (
        mc["target_first"], mc["stop_first"], mc["neither"], mc["green"], mc["avg_r"]))
    e = mc["end"]
    L.append("    price in %d sessions: £%s / £%s / £%s (p10 / median / p90)" % (mc["days"], m(e[0] / fx), m(e[2] / fx), m(e[4] / fx)))
    L.append("    " + mc["verdict"])
    for ln in mc["lines"]:
        L.append("    " + ln)
    a, s = hi["all"], hi["same"]
    if a["n"]:
        L.append("  History (real bars, same stop/target in ATRs, %d sessions): every day %d → %d target, %d stop, %d neither, avg R %+.2f" % (
            hi["days"], a["n"], a["target"], a["stop"], a["neither"], a["avg_r"]))
    if s["n"]:
        L.append("    days this checker said %s: %d → %d target, %d stop, %d neither, avg R %+.2f (windows overlap)" % (
            st["verdict"], s["n"], s["target"], s["stop"], s["neither"], s["avg_r"]))
    if sz["refused"]:
        L.append("  Size  refused: " + sz["refused"])
    else:
        L.append("  Size  %s sh · £%s incl. %.2f%% FX fee · max loss £%s at the stop · decided by %s" % (
            m(sz["qty"], 2), m(sz["cost_gbp"]), sz["fee_pct"], m(sz["max_loss_gbp"]), sz["decided_by"]))
    L.append("  Rules %s" % r["gate"])
    for c in r["checks"]:
        if c["level"] != "PASS":
            L.append("    %s %s: %s" % (c["level"], c["check"], c["text"]))
    L.append("  Bull: " + "; ".join(r["bull"]))
    L.append("  Bear: " + "; ".join(r["bear"]))
    return "\n".join(L)


# ================================================================ HTML report

CSS = r"""
:root{
  /* Layout: a single reading column; each ticker is a sheet with the chart on top and the numbers in a grid below. */
  --bg:#f6f7f5; --panel:#ffffff; --ink:#18201c; --muted:#5d6862; --line:#dde2de; --soft:#eef1ee;
  --accent:#1f5f8b; --up:#14845a; --down:#c23a32; --amber:#a86a00;
  --up-bg:#e3f3ea; --down-bg:#fbe6e4; --amber-bg:#fbf0d9;
  --ma10:#d2902a; --ma20:#3c7fc0; --ma50:#8a4fc0; --ma200:#444c47;
  --display:"Schibsted Grotesk",ui-sans-serif,system-ui,sans-serif;
  --body:"Schibsted Grotesk",ui-sans-serif,system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#121614; --panel:#191f1c; --ink:#e6ebe7; --muted:#97a39c; --line:#2b3430; --soft:#202824;
  --accent:#6fb0de; --up:#3fbf86; --down:#ef6a5f; --amber:#e3a63a;
  --up-bg:#16362a; --down-bg:#3d1f1c; --amber-bg:#3a2e14;
  --ma10:#e6a94a; --ma20:#6ea8e0; --ma50:#b88ae6; --ma200:#c9d1cc; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#121614; --panel:#191f1c; --ink:#e6ebe7; --muted:#97a39c; --line:#2b3430; --soft:#202824;
  --accent:#6fb0de; --up:#3fbf86; --down:#ef6a5f; --amber:#e3a63a;
  --up-bg:#16362a; --down-bg:#3d1f1c; --amber-bg:#3a2e14;
  --ma10:#e6a94a; --ma20:#6ea8e0; --ma50:#b88ae6; --ma200:#c9d1cc; color-scheme:dark}
body{background:var(--bg);color:var(--ink);font-family:var(--body);font-size:14px;line-height:1.5;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:20px 40px}
.wrap{max-width:1040px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
h1{font-family:var(--display);font-size:26px;letter-spacing:-.01em;margin:0;text-wrap:balance}
h2{font-family:var(--display);font-size:20px;margin:0}
h3{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 6px;font-weight:600}
.meta{color:var(--muted);font-size:12.5px;font-family:var(--mono);display:flex;flex-wrap:wrap;gap:4px 16px}
.num,td,.mono{font-variant-numeric:tabular-nums}
.mono{font-family:var(--mono)}
.tbl{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
th{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
td.r,th.r{text-align:right}
td a{color:var(--ink);text-decoration:none;font-weight:600}
td a:hover,td a:focus-visible{text-decoration:underline}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700;letter-spacing:.03em}
.g{background:var(--up-bg);color:var(--up)} .a{background:var(--amber-bg);color:var(--amber)} .rd{background:var(--down-bg);color:var(--down)}
.chips{display:inline-flex;gap:3px}
.chip{font-family:var(--mono);font-size:10.5px;padding:1px 4px;border-radius:3px}
.sheet{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px;display:flex;flex-direction:column;gap:14px;min-width:0}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px}
.head .px{font-family:var(--mono);font-size:15px}
.head .sub{color:var(--muted);font-size:13px}
.chart{height:380px;width:100%;position:relative}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11.5px;color:var(--muted);font-family:var(--mono)}
.legend i{display:inline-block;width:14px;height:2px;vertical-align:middle;margin-right:5px}
.legend i.dash{background:none!important;border-top:2px dashed}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.box{background:var(--soft);border-radius:6px;padding:12px;min-width:0}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px}
.kv dt{color:var(--muted)} .kv dd{margin:0;text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:3px}
.bull li::marker{color:var(--up)} .bear li::marker{color:var(--down)}
.sub{color:var(--muted)}
.call{font-size:14px;max-width:72ch;margin:0}
.checks{font-size:12.5px;display:flex;flex-direction:column;gap:3px}
.checks span.lv{font-family:var(--mono);font-size:10.5px;font-weight:700;margin-right:6px}
.lv.PASS{color:var(--up)} .lv.WARN{color:var(--amber)} .lv.BLOCK{color:var(--down)}
footer{color:var(--muted);font-size:12px;display:flex;flex-direction:column;gap:6px;max-width:80ch}
footer a{color:var(--accent)}
.empty{background:var(--panel);border:1px dashed var(--line);border-radius:8px;padding:18px;color:var(--muted)}
@media (max-width:520px){.chart{height:300px}.sheet{padding:12px}}
"""

JS = r"""
(function(){
  const DATA = JSON.parse(document.getElementById('tc-data').textContent);
  const charts = [];
  function tok(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()}
  function build(){
    charts.forEach(c=>{try{c.remove()}catch(e){}}); charts.length=0;
    if(!window.LightweightCharts) return;
    const C={bg:tok('--panel'),ink:tok('--muted'),line:tok('--line'),up:tok('--up'),down:tok('--down'),
      m10:tok('--ma10'),m20:tok('--ma20'),m50:tok('--ma50'),m200:tok('--ma200'),amber:tok('--amber'),accent:tok('--accent')};
    DATA.forEach(d=>{
      const el=document.getElementById('chart-'+d.id); if(!el) return;
      const ch=LightweightCharts.createChart(el,{autoSize:true,
        layout:{background:{type:'solid',color:C.bg},textColor:C.ink,fontFamily:'IBM Plex Mono, ui-monospace, monospace',fontSize:11},
        grid:{vertLines:{color:C.line},horzLines:{color:C.line}},
        rightPriceScale:{borderColor:C.line},timeScale:{borderColor:C.line,rightOffset:2},
        crosshair:{mode:0},localization:{priceFormatter:p=>'£'+p.toFixed(2)}});
      charts.push(ch);
      const cs=ch.addCandlestickSeries({upColor:C.up,downColor:C.down,borderUpColor:C.up,borderDownColor:C.down,wickUpColor:C.up,wickDownColor:C.down});
      cs.setData(d.candles);
      [['ma10',C.m10],['ma20',C.m20],['ma50',C.m50],['ma200',C.m200]].forEach(([k,col])=>{
        if(d[k]&&d[k].length){const s=ch.addLineSeries({color:col,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});s.setData(d[k]);}});
      if(d.proj){[['bull',C.up],['base',C.ink],['bear',C.down]].forEach(([k,col])=>{
        const s=ch.addLineSeries({color:col,lineWidth:2,lineStyle:2,priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false,title:k});s.setData(d.proj[k]);});}
      (d.lines||[]).forEach(l=>cs.createPriceLine({price:l.p,color:C[l.c]||C.accent,lineWidth:1,lineStyle:l.s||0,axisLabelVisible:true,title:l.t}));
      ch.timeScale().fitContent();
    });
  }
  build();
  try{matchMedia('(prefers-color-scheme: dark)').addEventListener('change',build)}catch(e){}
  new MutationObserver(build).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
})();
"""


def pill(v):
    cls = {"BUY ZONE": "g", "HOLD": "g", "PASS": "g", "WATCH": "a", "WAIT": "a", "TRIM 1/3": "a", "WATCH STOP": "a",
           "SPECULATIVE": "rd", "AVOID": "rd", "SELL TO STAY SAFE": "rd", "BLOCK": "rd"}.get(v, "a")
    return '<span class="pill %s">%s</span>' % (cls, H.escape(v))


def chips(st):
    out = []
    for n in (10, 20, 50, 200):
        v = st["ma"][n]
        if v is None:
            out.append('<span class="chip a">%d ?</span>' % n)
        else:
            up = st["close"] > v
            out.append('<span class="chip %s">%d%s</span>' % ("g" if up else "rd", n, "▲" if up else "▼"))
    return '<span class="chips">%s</span>' % "".join(out)


def chart_blob(idx, r, fx, avg=None):
    bars = r["bars"]
    S = Series(bars)
    show = 90
    k0 = max(0, len(bars) - show)
    g = lambda x: round(x / fx, 4)
    blob = {"id": idx, "candles": [{"time": bars[j]["d"], "open": g(bars[j]["o"]), "high": g(bars[j]["h"]),
                                    "low": g(bars[j]["l"]), "close": g(bars[j]["c"])} for j in range(k0, len(bars))]}
    for n in (10, 20, 50, 200):
        blob["ma%d" % n] = [{"time": S.d[j], "value": g(S.ma[n][j])} for j in range(k0, len(bars)) if S.ma[n][j] is not None]
    mc = r.get("mc")
    if mc:
        days = next_trading_days(bars[-1]["d"], mc["days"])
        last = {"time": bars[-1]["d"], "value": g(bars[-1]["c"])}
        blob["proj"] = {k: [last] + [{"time": days[d], "value": g(mc["fan"][d][q])} for d in range(mc["days"])]
                        for k, q in (("bull", 4), ("base", 2), ("bear", 0))}
    lines = []
    p = r.get("plan") or {}
    if p.get("stop"):
        lines.append({"p": g(p["stop"]), "c": "down", "t": "stop", "s": 2})
    if p.get("target"):
        lines.append({"p": g(p["target"]), "c": "up", "t": "target", "s": 2})
    if r.get("stop") and not p.get("target"):
        pass
    if avg:
        lines.append({"p": g(avg), "c": "accent", "t": "your avg", "s": 0})
    blob["lines"] = lines
    return blob


LEGEND = ('<div class="legend"><span><i style="background:var(--ma10)"></i>MA10</span><span><i style="background:var(--ma20)"></i>MA20</span>'
          '<span><i style="background:var(--ma50)"></i>MA50</span><span><i style="background:var(--ma200)"></i>MA200</span>%s</div>')
PROJ_LEG = ('<span><i class="dash" style="border-color:var(--up)"></i>bull p90</span><span><i class="dash" style="border-color:var(--muted)"></i>base median</span>'
            '<span><i class="dash" style="border-color:var(--down)"></i>bear p10</span>')


def esc(x):
    return H.escape(str(x))


def kv(rows):
    return '<dl class="kv">%s</dl>' % "".join("<dt>%s</dt><dd>%s</dd>" % (esc(a), esc(b)) for a, b in rows)


def ul(items, cls):
    return '<ul class="%s">%s</ul>' % (cls, "".join("<li>%s</li>" % esc(x) for x in items)) if items else '<p class="sub">None.</p>'


def check_sheet(idx, r, notes):
    st, p, mc, sz, hi, fx = r["state"], r["plan"], r["mc"], r["size"], r["history"], r["fx"]
    n = notes.get(r["symbol"], {})
    bull = (n.get("bull") or []) + r["bull"]
    bear = (n.get("bear") or []) + r["bear"]
    z = st["zone"]
    zone = ("£%s–%s" % (m(z[0] / fx), m(z[1] / fx))) if z else "—"
    levels = [("Entry", "£%s  ($%s)" % (m(p["entry"] / fx), m(p["entry"]))),
              ("Stop", "£%s  −%.1f%%" % (m(p["stop"] / fx), p["risk_pct"])),
              ("Target", "£%s  %s" % (m(p["target"] / fx), p["target_why"])),
              ("Reward:risk", "%.2f" % p["rr"]),
              ("Buy zone", zone), ("ATR(14)", "£%s  %.1f%%" % (m(st["atr"] / fx), st["atr_pct"])),
              ("RSI(14)", m(st["rsi"], 0)), ("52-week", "£%s–%s" % (m(st["lo52"] / fx), m(st["hi52"] / fx)))]
    e = mc["end"]
    reps = [("Target first", "%.1f%%" % mc["target_first"]), ("Stop first", "%.1f%%" % mc["stop_first"]),
            ("Neither", "%.1f%%" % mc["neither"]), ("Average R", "%+.2f" % mc["avg_r"]),
            ("p10 / median / p90", "£%s / %s / %s" % (m(e[0] / fx), m(e[2] / fx), m(e[4] / fx)))]
    a, s = hi["all"], hi["same"]
    hist_txt = ""
    if a["n"]:
        hist_txt = "Same stop and target on every past day (%d): %d target, %d stop, %d neither, avg R %+.2f." % (
            a["n"], a["target"], a["stop"], a["neither"], a["avg_r"])
    if s["n"]:
        hist_txt += " On the %d days this checker said %s: %d target, %d stop, %d neither, avg R %+.2f." % (
            s["n"], st["verdict"], s["target"], s["stop"], s["neither"], s["avg_r"])
    sizebox = [("Refused", sz["refused"])] if sz["refused"] else [
        ("Shares", m(sz["qty"], 2)), ("Cost", "£%s" % m(sz["cost_gbp"])), ("Max loss at stop", "£%s" % m(sz["max_loss_gbp"])),
        ("Reward at target", "£%s" % m(sz["reward_gbp"])), ("Decided by", sz["decided_by"])]
    checks = "".join('<div><span class="lv %s">%s</span>%s: %s</div>' % (c["level"], c["level"], esc(c["check"]), esc(c["text"]))
                     for c in r["checks"])
    call = n.get("call") or auto_call(r)
    news_items = n.get("news") or []
    news_html = ('<div class="box"><h3>News</h3>%s</div>' % ul(news_items, "")) if news_items else ""
    return """
<section class="sheet" id="t-%s">
  <div class="head"><h2>%s</h2>%s<span class="px">£%s</span><span class="sub">$%s · %s%s</span>%s</div>
  <div class="head"><span class="sub">Shape %s %d/%d · Price %s</span>%s<span class="sub">Rules</span>%s</div>
  <div class="chart" id="chart-%d" role="img" aria-label="%s daily candlesticks in GBP with moving averages and a %d-session projection"></div>
  %s
  <p class="call">%s</p>
  <div class="grid">
    <div class="box"><h3>Levels</h3>%s</div>
    <div class="box"><h3>Next %d sessions · %s replays</h3>%s<p class="sub" style="margin:8px 0 0;font-size:12px">%s</p></div>
    <div class="box"><h3>Size in GBP</h3>%s</div>
  </div>
  <div class="grid">
    <div class="box"><h3>Bull case</h3>%s</div>
    <div class="box"><h3>Bear case</h3>%s</div>
    <div class="box"><h3>Rules check</h3><div class="checks">%s</div></div>
  </div>
  %s
  <p class="sub" style="margin:0;font-size:12px">%s Earnings: %s.</p>
</section>""" % (
        esc(r["symbol"]), esc(r["symbol"]), pill(st["verdict"]), m(st["close"] / fx), m(st["close"]), esc(st["date"]),
        (" · " + esc(r["name"])) if r.get("name") else "", " <span class=\"pill a\">live bar</span>" if r["info"].get("live") else "",
        st["shape"], st["score"], st["of"], st["price"], chips(st), pill(r["gate"]),
        idx, esc(r["symbol"]), mc["days"], LEGEND % PROJ_LEG, esc(call),
        kv(levels), mc["days"], "{:,}".format(mc["replays"]), kv(reps), esc(mc["verdict"]), kv(sizebox),
        ul(bull, "bull"), ul(bear, "bear"), checks, news_html, esc(hist_txt), esc(r["earnings"] or "date unknown"))


def auto_call(r):
    st, p, fx = r["state"], r["plan"], r["fx"]
    v = st["verdict"]
    if v == "BUY ZONE":
        s = "In the buy zone: %s trend (%d/%d), %s. Plan £%s, stop £%s, target £%s." % (
            st["shape"].lower(), st["score"], st["of"], "; ".join(st["good"]), m(p["entry"] / fx), m(p["stop"] / fx), m(p["target"] / fx))
    elif v == "WAIT":
        z = st["zone"]
        s = "Good trend but stretched (%s). Wait for £%s–%s." % (", ".join(st["over"]), m(z[0] / fx), m(z[1] / fx))
    elif v == "WATCH":
        z = st["zone"]
        s = "Trend fine, price between levels. Better entry £%s–%s." % (m(z[0] / fx), m(z[1] / fx))
    elif v == "SPECULATIVE":
        s = "Oversold in a weak trend: a bounce trade only, against the trend."
    else:
        s = "Weak chart (%d/%d trend checks). No new buy." % (st["score"], st["of"])
    s += " Rules: %s." % r["gate"]
    return s


def holding_sheet(idx, r, notes):
    st, fx, h = r["state"], r["fx"], r["holding"]
    n = notes.get(r["symbol"], {})
    rows = [("Shares", m(h.get("qty"), 4)), ("Avg price", "$%s" % m(h.get("avg"))),
            ("Value", "£%s" % m(r["value_gbp"])),
            ("P/L", "—" if r["pl_pct"] is None else "%+.1f%%  £%s" % (r["pl_pct"] * 100, m(r["pl_gbp"]))),
            ("Stop", "£%s  ($%s)" % (m(r["stop"] / fx), m(r["stop"]))), ("Stop from", r["stop_src"]),
            ("RSI(14)", m(st["rsi"], 0)), ("vs 20-day", "%+.1f%%" % st["ext20"]),
            ("52-week", "£%s–%s" % (m(st["lo52"] / fx), m(st["hi52"] / fx)))]
    if r["new_stop"]:
        rows.append(("After trim, stop to", "£%s ($%s)" % (m(r["new_stop"] / fx), m(r["new_stop"]))))
    call = n.get("call") or ("%s: %s." % (r["call"], "; ".join(r["reasons"])))
    news_items = n.get("news") or []
    return """
<section class="sheet" id="t-%s">
  <div class="head"><h2>%s</h2>%s<span class="px">£%s</span><span class="sub">$%s · %s</span>%s</div>
  <div class="head"><span class="sub">Shape %s %d/%d</span>%s</div>
  <div class="chart" id="chart-%d" role="img" aria-label="%s daily candlesticks in GBP with moving averages"></div>
  %s
  <p class="call">%s</p>
  <div class="grid">
    <div class="box"><h3>Position</h3>%s</div>
    <div class="box"><h3>Bull case</h3>%s</div>
    <div class="box"><h3>Bear case</h3>%s</div>
  </div>
  %s
</section>""" % (esc(r["symbol"]), esc(r["symbol"]), pill(r["call"]), m(st["close"] / fx), m(st["close"]), esc(st["date"]),
                 ' <span class="pill rd">within 5% of stop</span>' if r["near_stop"] else "",
                 st["shape"], st["score"], st["of"], chips(st), idx, esc(r["symbol"]), LEGEND % "",
                 esc(call), kv(rows), ul((n.get("bull") or []) + r["bull"], "bull"), ul((n.get("bear") or []) + r["bear"], "bear"),
                 ('<div class="box"><h3>News</h3>%s</div>' % ul(news_items, "")) if news_items else "")


def write_html(path, title, header_meta, summary_rows, summary_head, sheets, blobs, sources, extra=""):
    srcs = "".join('<a href="%s">%s</a> · ' % (esc(u), esc(t)) for t, u in sources).rstrip(" · ")
    page = """<title>%s</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Schibsted+Grotesk:wght@400;600;700&display=swap">
<style>%s</style>
<div class="wrap">
  <header style="display:flex;flex-direction:column;gap:6px"><h1>%s</h1><div class="meta">%s</div></header>
  %s
  <div class="tbl"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>
  %s
  <footer><div>Prices converted to GBP at today's rate; Trading 212 adds a %s FX fee each way. Replays and projections are built from the stock's own past daily bars: a simulation, not a forecast. Earnings, news and gaps can do things the past bars never did.</div>
  <div>%s</div><div>%s</div></footer>
</div>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<script type="application/json" id="tc-data">%s</script>
<script>%s</script>
""" % (esc(title), CSS, esc(title), header_meta, extra, summary_head, summary_rows, "\n".join(sheets) or
       '<div class="empty">Nothing passed today. That is a result too: no trade is a position.</div>',
       "0.15%", NOTE, srcs, json.dumps(blobs).replace("</", "<\\/"), JS)
    with open(path, "w") as f:
        f.write(page)


# ================================================================ commands

def load_cfg(path):
    cfg = dict(DEFAULT_CONFIG)
    for p in ([path] if path else ["tradecheck.json", os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradecheck.json")]):
        if p and os.path.exists(p):
            with open(p) as f:
                cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
            cfg["_path"] = p
            break
    return cfg


def parse_earn(items):
    out = {}
    for x in items or []:
        k, _, v = x.partition("=")
        if v:
            out[k.upper()] = v
    return out


def load_notes(path):
    if not path:
        return {}
    with open(path) as f:
        return {k.upper(): v for k, v in json.load(f).items()}


def head_meta(now_utc, fx, fxsrc, src, cfg):
    return "<span>%s UK · %s New York</span><span>GBP/USD %.4f (%s)</span><span>%s</span>" % (
        to_uk(now_utc).strftime("%a %d %b %Y %H:%M"), to_ny(now_utc).strftime("%H:%M"), fx, esc(fxsrc), esc(src))


def summary_check(rows, fx):
    head = "<th>Ticker</th><th>Call</th><th class=r>Close £</th><th class=r>1m</th><th>MAs</th><th class=r>RSI</th><th class=r>Stop £</th><th class=r>Target £</th><th class=r>Target 1st</th><th>Rules</th>"
    body = ""
    for r in rows:
        st, p, mc = r["state"], r["plan"], r["mc"]
        body += "<tr><td><a href=\"#t-%s\">%s</a></td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%s</td><td class=r>%.0f%%</td><td>%s</td></tr>" % (
            esc(r["symbol"]), esc(r["symbol"]), pill(st["verdict"]), m(st["close"] / fx),
            "—" if st["ret21"] is None else "%+.1f%%" % (st["ret21"] * 100), chips(st), m(st["rsi"], 0),
            m(p["stop"] / fx), m(p["target"] / fx), mc["target_first"], pill(r["gate"]))
    return head, body


ORDER = {"BUY ZONE": 0, "WATCH": 1, "WAIT": 2, "SPECULATIVE": 3, "AVOID": 4}


def cmd_check(a, cfg):
    now = utc_now(a.now)
    fx, fxsrc = get_fx(a, cfg)
    earn = parse_earn(a.earnings)
    notes = load_notes(a.notes)
    if a.entry and len(a.tickers) > 1:
        raise DataError("--entry works with one ticker")
    results, errors = [], []
    for t in a.tickers:
        t = t.upper()
        e = earn.get(t) or (notes.get(t) or {}).get("earnings")
        if not e and not a.data:
            e = yahoo_earnings_date(t)
        try:
            results.append(full_check(t, a, cfg, fx, now, a.entry, earnings=e))
        except DataError as ex:
            errors.append("%s: %s" % (t, ex))
    results.sort(key=lambda r: (ORDER.get(r["state"]["verdict"], 9), r["gate"] != "PASS", -r["state"]["score"]))
    emit(a, results, errors, fx, fxsrc, now, cfg, notes, "Trade Check")
    return 0 if any(r["gate"] == "PASS" for r in results) else 1


def emit(a, results, errors, fx, fxsrc, now, cfg, notes, title, extra=""):
    if a.json:
        print(json.dumps({"as_of_utc": now.isoformat() + "Z", "gbpusd": fx, "results": [strip(r) for r in results],
                          "errors": errors}, indent=1, default=str))
    else:
        print("GBP/USD %.4f (%s) · %s UK" % (fx, fxsrc, to_uk(now).strftime("%a %d %b %H:%M")))
        for r in results:
            print()
            print(text_check(r))
        for e in errors:
            print("ERROR " + e)
        print("\n" + NOTE)
    if a.html:
        src = results[0]["info"]["source"] if results else ""
        head, body = summary_check(results, fx)
        sheets = [check_sheet(i, r, notes) for i, r in enumerate(results)]
        blobs = [chart_blob(i, r, fx) for i, r in enumerate(results)]
        sources = [("Yahoo Finance", "https://finance.yahoo.com/quote/%s" % r["symbol"]) for r in results]
        write_html(a.html, "%s %s" % (title, to_uk(now).strftime("%d %b %Y")), head_meta(now, fx, fxsrc, src, cfg),
                   body, head, sheets, blobs, sources, extra)
        print("wrote " + a.html, file=sys.stderr)


def strip(r):
    r = dict(r)
    r.pop("bars", None)
    if "mc" in r and r["mc"]:
        r["mc"] = {k: v for k, v in r["mc"].items() if k != "fan"}
    return r


def cmd_scan(a, cfg):
    now = utc_now(a.now)
    fx, fxsrc = get_fx(a, cfg)
    cache = a.cache or "tradecheck-data"
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    elif a.data:
        syms = sorted({os.path.splitext(f)[0].upper() for f in os.listdir(a.data) if f.lower().endswith(".csv")} - {"FX"})
    else:
        syms = universe(cache)
    if a.limit:
        syms = syms[:a.limit]
    stage = []
    if a.data:
        for s in syms:
            try:
                bars, info = get_bars(s, a, now)
                stage.append((s, bars, bars[-1]["v"]))
            except DataError:
                pass
        failed = len(syms) - len(stage)
    else:
        print("Stage 1: daily closes for %d symbols (Yahoo spark, 20 a request)..." % len(syms), file=sys.stderr)
        data, failed = spark(syms)
        stage = [(s, b, v) for s, (b, v) in data.items()]
    cands, liquid = [], 0
    for s, bars, vol in stage:
        if len(bars) < 60:
            continue
        c = bars[-1]["c"]
        if c < cfg["min_price_usd"] or (vol and c * vol < cfg["min_dollar_volume_usd"] / 2 and not a.data):
            continue
        liquid += 1
        S = Series(bars)
        st = state_at(S, len(bars) - 1)
        if st["shape"] == "WEAK" or st["ext20"] > 10 or (st["rsi"] or 0) > 75:
            continue
        cands.append((st["score"] / max(1, st["of"]), c * (vol or 0), s, st))
    cands.sort(key=lambda x: (-x[0], -x[1]))
    deep = cands[:a.deep]
    print("Fetched %d of %d; %d liquid; %d in good shape and not stretched; full check on the top %d." % (
        len(stage), len(syms), liquid, len(cands), len(deep)), file=sys.stderr)
    results, errors = [], []
    for _, _, s, _ in deep:
        try:
            r = full_check(s, a, cfg, fx, now, earnings=None if a.data else yahoo_earnings_date(s))
            results.append(r)
        except DataError as ex:
            errors.append("%s: %s" % (s, ex))
    picks = [r for r in results if r["state"]["verdict"] == "BUY ZONE"]
    picks.sort(key=lambda r: (r["gate"] != "PASS", -r["mc"]["avg_r"]))
    rest = [r for r in results if r not in picks]
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "watchlist.json"), "w") as f:
        json.dump({"as_of_utc": now.isoformat() + "Z", "scanned": len(syms), "fetched": len(stage), "liquid": liquid,
                   "candidates": len(cands), "checked": len(results),
                   "picks": [{"symbol": r["symbol"], "gate": r["gate"], "close": r["state"]["close"], "stop": r["plan"]["stop"],
                              "target": r["plan"]["target"], "setups": r["state"]["setups"]} for r in picks],
                   "near_misses": [{"symbol": r["symbol"], "verdict": r["state"]["verdict"], "zone": r["state"]["zone"]} for r in rest]},
                  f, indent=1)
    extra = '<p class="meta">Scanned %d US stocks, %d returned data (%d failed), %d liquid, %d in good shape and not stretched; full check on %d; %d in the buy zone. A scan lists charts that match a rule, not stocks to buy.</p>' % (
        len(syms), len(stage), failed, liquid, len(cands), len(results), len(picks))
    if rest and not a.json:
        print("Near misses (good shape, wrong price): " + ", ".join("%s %s" % (r["symbol"], r["state"]["verdict"]) for r in rest))
    emit(a, picks, errors, fx, fxsrc, now, cfg, load_notes(a.notes), "Market Scan", extra)
    return 0 if picks else 1


def cmd_holdings(a, cfg):
    now = utc_now(a.now)
    fx, fxsrc = get_fx(a, cfg)
    if a.t212:
        pf = t212_portfolio()
    else:
        with open(a.file) as f:
            pf = json.load(f)
        pf.setdefault("source", "file " + os.path.basename(a.file))
    earn = parse_earn(a.earnings)
    notes = load_notes(a.notes)
    results, errors = [], []
    for h in pf["holdings"]:
        t = h["ticker"].upper()
        e = earn.get(t) or (notes.get(t) or {}).get("earnings")
        if not e and not a.data:
            e = yahoo_earnings_date(t)
        try:
            results.append(holding_call(t, h, a, cfg, fx, now, e))
        except DataError as ex:
            errors.append("%s: %s" % (t, ex))
    rank = {"SELL TO STAY SAFE": 0, "WATCH STOP": 1, "TRIM 1/3": 2, "HOLD": 3}
    results.sort(key=lambda r: (-r["state"]["score"] / max(1, r["state"]["of"]), -r["state"]["ext20"]))
    if a.json:
        print(json.dumps({"as_of_utc": now.isoformat() + "Z", "gbpusd": fx, "source": pf.get("source"),
                          "cash_gbp": pf.get("cash_gbp"), "results": [strip(r) for r in results], "errors": errors},
                         indent=1, default=str))
    else:
        print("Holdings · %s · GBP/USD %.4f · %s UK" % (pf.get("source"), fx, to_uk(now).strftime("%a %d %b %H:%M")))
        print("%-6s %-18s %9s %8s %8s  %s" % ("", "call", "close £", "P/L", "stop £", "why"))
        for r in results:
            print("%-6s %-18s %9s %8s %8s  %s%s" % (r["symbol"], r["call"], m(r["state"]["close"] / fx),
                                                 "—" if r["pl_pct"] is None else "%+.1f%%" % (r["pl_pct"] * 100),
                                                 m(r["stop"] / fx), "; ".join(r["reasons"]), "  ⚠ within 5% of stop" if r["near_stop"] else ""))
        for e in errors:
            print("ERROR " + e)
        if pf.get("skipped"):
            print("Not checked (not US-listed): " + ", ".join(pf["skipped"]))
        print("\n" + NOTE)
    if a.html:
        head = "<th>Holding</th><th>Call</th><th class=r>Close £</th><th class=r>P/L</th><th>MAs</th><th class=r>RSI</th><th class=r>Stop £</th>"
        body = "".join("<tr><td><a href=\"#t-%s\">%s</a></td><td>%s</td><td class=r>%s</td><td class=r>%s</td><td>%s</td><td class=r>%s</td><td class=r>%s</td></tr>" % (
            esc(r["symbol"]), esc(r["symbol"]), pill(r["call"]), m(r["state"]["close"] / fx),
            "—" if r["pl_pct"] is None else "%+.1f%%" % (r["pl_pct"] * 100), chips(r["state"]), m(r["state"]["rsi"], 0),
            m(r["stop"] / fx)) for r in results)
        sheets = [holding_sheet(i, r, notes) for i, r in enumerate(results)]
        blobs = [chart_blob(i, r, fx, r["holding"].get("avg")) for i, r in enumerate(results)]
        for b, r in zip(blobs, results):
            b["lines"] = [{"p": round(r["stop"] / fx, 4), "c": "down", "t": "stop", "s": 2}] + (
                [{"p": round(r["holding"]["avg"] / fx, 4), "c": "accent", "t": "your avg", "s": 0}] if r["holding"].get("avg") else [])
        extra = '<p class="meta">Cash £%s · Source: %s</p>' % (m(pf.get("cash_gbp")), esc(pf.get("source"))) if pf.get("cash_gbp") is not None else ""
        write_html(a.html, "Holdings Check %s" % to_uk(now).strftime("%d %b %Y"), head_meta(now, fx, fxsrc, pf.get("source", ""), cfg),
                   body, head, sheets, blobs, [("Yahoo Finance", "https://finance.yahoo.com/quote/%s" % r["symbol"]) for r in results], extra)
        print("wrote " + a.html, file=sys.stderr)
    return 0


def cmd_news(a, cfg):
    for t in a.tickers:
        print("== %s" % t.upper())
        try:
            for it in news(t, a.hours):
                print("  %s ET  %s\n      %s" % (it["time_ny"], it["title"], it["link"]))
        except Exception as e:
            print("  headlines unavailable: %s" % e)
        try:
            for f in sec_8k(t, sec_ua(cfg)):
                print("  8-K %s  %s  %s" % (f["filed"], f["items"], f["link"]))
        except Exception as e:
            print("  SEC filings skipped: %s" % e)
    print("\nHeadlines are leads, not facts: open the story or filing before acting.")
    return 0


def cmd_earnings(a, cfg):
    ua = sec_ua(cfg)
    for t in a.tickers:
        print("== %s (SEC XBRL facts, quarterly frames)" % t.upper())
        try:
            rows = sec_quarters(t, ua)
        except Exception as e:
            print("  %s" % e)
            continue
        print("  %-8s %10s %14s %8s %14s %8s %8s %7s" % ("quarter", "end", "revenue", "yoy", "net income", "EPS", "yoy", "GM"))
        for r in rows:
            print("  %-8s %10s %14s %8s %14s %8s %8s %7s" % (
                r["quarter"], r["end"], m(r["revenue"], 0), "—" if r["rev_yoy"] is None else "%+.1f%%" % r["rev_yoy"],
                m(r["net_income"], 0), m(r["eps"]), "—" if r["eps_yoy"] is None else "%+.1f%%" % r["eps_yoy"],
                "—" if r["gross_margin"] is None else "%.1f%%" % r["gross_margin"]))
        e = None if a.data else yahoo_earnings_date(t)
        print("  next report: %s" % (e or "unknown here; check the company's IR page or the news"))
    print("\nQ4 is usually missing from quarterly frames (it is filed inside the 10-K).")
    return 0


def cmd_clock(a, cfg):
    now = utc_now(a.now)
    n = to_ny(now)
    s = session(n.date())
    state = "OPEN" if market_open(now) else "CLOSED"
    print("NYSE %s · %s New York · %s UK" % (state, n.strftime("%a %d %b %H:%M"), to_uk(now).strftime("%H:%M")))
    d = n.date()
    if s and n.time() < s[0]:
        nxt = d
    else:
        nxt = d + dt.timedelta(days=1)
        while not trading_day(nxt):
            nxt += dt.timedelta(days=1)
    o = ny_to_utc(dt.datetime.combine(nxt, dt.time(9, 30)))
    cl = ny_to_utc(dt.datetime.combine(nxt, session(nxt)[1]))
    if state == "OPEN":
        cu = ny_to_utc(dt.datetime.combine(d, s[1]))
        print("Closes %s UK (%s New York)%s" % (to_uk(cu).strftime("%H:%M"), s[1].strftime("%H:%M"),
                                               " · early close" if d.isoformat() in NYSE_EARLY_CLOSE else ""))
    print("Next open: %s %s UK (09:30 New York), closes %s UK" % (nxt.strftime("%a %d %b"), to_uk(o).strftime("%H:%M"),
                                                               to_uk(cl).strftime("%H:%M")))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tradecheck", description="Henry's trade checker. Never places orders.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--data", help="folder of <TICKER>.csv daily bars (+ fx.json) instead of Yahoo")
        sp.add_argument("--gbpusd", type=float, help="USD per GBP, e.g. 1.33")
        sp.add_argument("--config", help="config JSON (default ./tradecheck.json)")
        sp.add_argument("--now", help="pin the clock, New York time 'YYYY-MM-DD HH:MM'")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--html", help="write a report page here")
        sp.add_argument("--notes", help="JSON {TICKER: {bull:[], bear:[], news:[], call:'', earnings:'YYYY-MM-DD'}}")
        sp.add_argument("--earnings", nargs="*", help="TICKER=YYYY-MM-DD next report dates")
        sp.add_argument("--stop", help="swing (default), atr:K, pct:X, price:X, ma20, ma50")
        sp.add_argument("--target", help="2R (default), 1.5R, price:X")
        sp.add_argument("--days", type=int, help="sessions to plan and replay (default 20)")
        sp.add_argument("--replays", type=int)
        sp.add_argument("--seed", type=int)
        sp.add_argument("--drift", choices=["keep", "zero"])

    c = sub.add_parser("check", help="full check of one or more trades")
    c.add_argument("tickers", nargs="+")
    c.add_argument("--entry", type=float, help="planned entry in USD (default: last close)")
    common(c)
    s = sub.add_parser("scan", help="scan the market, then fully check the best")
    s.add_argument("--symbols", help="comma list instead of the whole market")
    s.add_argument("--limit", type=int, help="first N symbols of the universe")
    s.add_argument("--deep", type=int, default=25, help="full checks on the top N candidates")
    s.add_argument("--cache", help="folder for universe.txt and watchlist.json")
    common(s)
    h = sub.add_parser("holdings", help="HOLD / TRIM / WATCH STOP / SELL per holding")
    g = h.add_mutually_exclusive_group(required=True)
    g.add_argument("--file", help='JSON {"holdings":[{"ticker","qty","avg","stop"}], "cash_gbp"}')
    g.add_argument("--t212", action="store_true", help="read Trading 212 (T212_API_KEY in the environment)")
    common(h)
    n = sub.add_parser("news", help="headlines + 8-Ks")
    n.add_argument("tickers", nargs="+")
    n.add_argument("--hours", type=int, default=72)
    common(n)
    e = sub.add_parser("earnings", help="reported quarters from SEC")
    e.add_argument("tickers", nargs="+")
    common(e)
    en = sub.add_parser("entry", help="exact entry trigger on 5-minute bars + the Trading 212 order")
    en.add_argument("ticker")
    en.add_argument("--setup", choices=["level", "zone", "orb"],
                    help="level: close above yesterday's high (default for BUY ZONE); zone: back into the buy zone "
                         "(default for WAIT/WATCH); orb: above the 15-minute opening range")
    en.add_argument("--level", type=float, help="your own trigger price in USD (with --setup level)")
    en.add_argument("--watch", action="store_true", help="keep checking after every 5-minute bar until it triggers")
    common(en)
    k = sub.add_parser("clock", help="NYSE open/closed in UK time")
    common(k)
    a = p.parse_args(argv)
    cfg = load_cfg(a.config)
    try:
        return {"check": cmd_check, "scan": cmd_scan, "holdings": cmd_holdings, "news": cmd_news,
                "earnings": cmd_earnings, "clock": cmd_clock, "entry": cmd_entry}[a.cmd](a, cfg)
    except DataError as ex:
        print("tradecheck: %s" % ex, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
