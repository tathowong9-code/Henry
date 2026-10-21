---
name: trade-checker
description: Henry's all-in-one trade checker for Trading 212 in GBP. Use for /trade, "check this trade", "should I buy X", "is X a good price", "scan the market", "find me buys", "check my holdings", the daily stock reports and the Monday holdings review.
---

# /trade: trade checker

One tool, `tradecheck.py`, built from nine trader-* skills plus /stockchecking:

| part | came from | what it does here |
| --- | --- | --- |
| scan | trader-scanner | daily setups across ~5,900 US stocks, then a full check of the best |
| shape + price | stockchecking | is the trend healthy (7 checks) and is today a good price (not stretched) |
| plan | trader-exit | stop and target BEFORE the entry, on daily bars (swing, not day trade) |
| replays + history | trader-timemachine | 10,000 bootstrapped replays of the next 20 sessions, and the same stop/target on every past day |
| size | trader-budget | shares in GBP from the max loss, fractional, Trading 212's 0.15% FX fee included |
| rules gate | trader-risk | PASS / BLOCK: stop, reward:risk, chart, price, earnings gap risk, size, positions |
| news / earnings | trader-news, trader-analyst | Yahoo headlines, SEC 8-Ks, reported quarters from SEC XBRL |
| entry | trader-entry | the exact entry trigger on 5-minute bars, and the Trading 212 order that executes it |
| clock | trader-alerts | NYSE open/closed and holidays in UK time |
| holdings | stockchecking | HOLD / TRIM 1/3 / WATCH STOP / SELL TO STAY SAFE per holding |

Not used: trader-broker (Alpaca/IBKR order placing; Henry uses Trading 212 and places his own orders), trader-pelosi (weeks-old Congress filings; not a trade signal). Those skills still work on their own if asked for by name.

It never places, edits or cancels an order. A PASS is a rules check, not a recommendation.

## 1. Get the code

The code is in the Stock project, not in this skill:

1. `Projects project_read tradecheck/tradecheck.py` and `tradecheck/tradecheck.example.json`.
2. Write them to the working directory as `tradecheck.py` and `tradecheck.json` (copy the example; if Henry has given limits, set them).
3. `python3 tradecheck.py --help`. Standard library only, Python 3.8+.

If Henry's Mac is linked and the tool is installed there (`~/tradecheck/`), run it there with `device_bash`: the Mac has open internet, so Yahoo, Nasdaq Trader, SEC and Trading 212 all work directly.

## 2. Get the data

- **Mac or any machine with internet:** nothing to do. The tool reads Yahoo daily bars (2 years), GBP/USD, Nasdaq Trader's symbol lists and SEC.
- **Cloud workspace (shell cannot reach market sites):** fetch with WebFetch and save files, then pass `--data DIR`:
  - `https://stockanalysis.com/api/symbol/s/<ticker>/history?range=1Y&period=daily` (ask for every row as `t,o,h,l,c,v`, oldest first), plus `https://stockanalysis.com/stocks/<ticker>/history/` for the latest weeks; the API snapshot often ends a few weeks early. Concatenate both into `DIR/<TICKER>.csv`; the tool dedupes by date.
  - GBP/USD from `https://tradingeconomics.com/united-kingdom/currency` into `DIR/fx.json` as `{"gbpusd": 1.338}`.
  - Next earnings date via WebSearch; pass `--earnings TICKER=YYYY-MM-DD`.
  - A full-market scan needs the Mac. In the cloud, scan a list (`--data DIR` holds the list).

## 3. Commands

```bash
python3 tradecheck.py check NVDA                         # one trade
python3 tradecheck.py check NVDA AMD KO --html check.html # several, with the report page
python3 tradecheck.py check NVDA --entry 215 --stop atr:2 --target 2.5R
python3 tradecheck.py scan --html scan.html              # whole market -> buy-zone picks only
python3 tradecheck.py holdings --t212 --html holdings.html   # needs T212_API_KEY (+ T212_API_SECRET) in the env
python3 tradecheck.py holdings --file holdings.json      # {"holdings":[{"ticker","qty","avg","stop"}],"cash_gbp"}
python3 tradecheck.py entry NVDA                         # trigger price + the Trading 212 order to place
python3 tradecheck.py entry NVDA --watch                 # Mac only: checks after every 5-minute bar, notifies on trigger
python3 tradecheck.py entry NVDA --setup level --level 225.50   # your own trigger price
python3 tradecheck.py news NVDA ; python3 tradecheck.py earnings NVDA ; python3 tradecheck.py clock
```

Common flags: `--data DIR`, `--gbpusd 1.338`, `--earnings T=DATE`, `--notes notes.json`, `--json`, `--html PATH`, `--days 20`, `--stop swing|atr:K|pct:X|price:X|ma20|ma50`, `--target 2R|price:X`, `--drift keep|zero`, `--now "YYYY-MM-DD HH:MM"` (New York time).

`--notes` adds researched context to the report: `{"NVDA": {"bull": [...], "bear": [...], "news": ["headline (source)"], "earnings": "2026-11-18", "call": "one-paragraph call"}}`.

## 4. How it decides

- **Shape** (7 checks): above the 200-day, 50 over 200, 50-day rising, above the 50-day, above the 20-day, up over 3 months, within 25% of the 52-week high. STRONG ≥85%, OK ≥57%, else WEAK.
- **Price:** OVEREXTENDED when >8% or >2.5 ATR over the 20-day, RSI >72, or up >25% in a month. GOOD when within 1 ATR of the 20-day (and above the 50-day), a pullback to a rising 50-day that is turning up, or a fresh breakout / bull flag / 20-day reclaim not more than 1.5 ATR stretched.
- **Verdict:** BUY ZONE (good shape + good price), WAIT (good shape, stretched; gives the buy zone), WATCH (good shape, fair price), SPECULATIVE (oversold in a weak trend), AVOID.
- Only BUY ZONE with rules PASS counts as a pick. Strong but stretched names are skipped, even if fewer than three qualify.

## 4b. Entry (exact price)

- Run `check` first; `entry` only plans entries for BUY ZONE, WAIT or WATCH charts.
- **BUY ZONE → level:** the trigger is yesterday's high + 1 cent. The entry is the first completed 5-minute bar that CLOSES above it, inside the entry window (09:45–15:30 New York = 14:45–20:30 UK most of the year). A wick above is not an entry.
- **WAIT / WATCH → zone:** the entry is the first completed bar that trades back into the buy zone and closes inside it.
- **orb:** the 15-minute opening-range breakout, as in trader-entry.
- The best way to get the exact price is the broker's own order, not polling: before the open, give Henry the **Stop-limit BUY** (stop = trigger, limit = trigger + 0.3%) or the **Limit BUY** at the top of the zone. Trading 212 watches every tick; free Yahoo 5-minute bars can lag.
- Re-run `entry` after the open: if it has triggered, the ticket switches to "buy now" with the size re-set at the real entry; if the price ran away past the limit, the trade is skipped, not chased.
- `--watch` runs only on the Mac, awake and online; it gives a macOS notification on trigger. Never run it from a scheduled cloud task.

## 5. Steps

1. One line saying what you're doing. Task list if more than two tickers.
2. Get the code and data (sections 1–2). Look up the next earnings date for every ticker and pass it.
3. Run the command. Read the numbers; never change a limit to make something pass.
4. Research what the numbers can't see: why it moved, the earnings date, analyst targets, one or two key risks. Cite every page. Put it in `notes.json` and re-run with `--notes` so the report carries it.
5. Publish the `--html` page as an Artifact (it already follows the page contract: GBP candlesticks, MA10/20/50/200, bull/base/bear projection, stop/target lines).
6. Reply terse and data-first: a compact table (ticker | close £ | call | key level), one line on anything urgent (earnings inside the plan, a holding within 5% of its stop), then Sources. No recap of the steps.

## 6. Henry's standing preferences

- Every stock gets a full bull and bear case with reasoning.
- Candlestick (K-bar) charts, green up / red down, with MA10/20/50/200 and a projection.
- All prices in GBP (Trading 212 account). Show USD beside it only where useful.
- Daily scans cover the whole US market; the old watchlist gets no priority.
- Emails go to tathowong9@gmail.com only: the 08:55 UK morning report, the US-open report, and a buy email only when a ticker is in the buy zone and passes the rules.
- Monday review of the Trading 212 holdings: `holdings`, plus earnings forecasts and the week's big events. Terse.

## 7. Honesty rules

- Replays are built from the stock's own past daily bars: a simulation, not a forecast. Say so once. Say the seed.
- Repeat the replay verdict line as printed. If history and replays disagree, say that.
- Free Yahoo data is delayed and unofficial. Say when the last bar is today's live bar.
- A stop is a plan, not a guarantee: an earnings gap can open past it.
- Analysis to support Henry's own decision. It is not regulated financial advice.
