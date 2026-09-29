---
name: stock-check
description: Run the user's stock check — live Trading 212 portfolio review (hold / trim / add) plus a fresh whole-market scan for the best 3 stocks in good shape, each with a buy range and hold plan. Use when the user types /stock-check or asks to "check my stock", "stock check", or "any potential stock".
---

# Stock check

Info only. NEVER place, modify or cancel a Trading 212 order — the user buys and sells by themselves.

## 1. Portfolio (live, read-only)
Run from the repo root:
```
scripts/t212.sh GET /equity/account/summary
scripts/t212.sh GET /equity/portfolio
```
Never print credentials. For each holding: average price, live price, £ P/L and % — then one action: HOLD / ADD (with add range) / TRIM (with level, 1/3) / WATCH STOP (with stop level). Note cash available and any concentration risk (e.g. share of money in AI/semis).

## 2. Market scan — best 3
Use WebSearch (WebFetch is mostly blocked). Scan widely, not only past tickers: recent beat-and-raise earnings, analyst-upgrade waves, sector leaders, pullbacks in strong uptrends, plus `research/watchlist.md`. Score each on:
- trend: price vs 10/20/50/200-day moving averages
- accumulation: buy volume vs sell volume
- a fresh fundamental catalyst (dates, $ amounts)
- valuation vs peers (growth-adjusted)
- upcoming event risk (earnings date — don't recommend buying right before a report)

Pick ONLY the best 3. For each: ticker, live price, 1-line why it's in good shape, **buy range**, status (IN RANGE / NEAR within 5% / NOT YET), short-term target (%, timeframe), holding period, trims (1/3 each), stops (initial and after each trim), 12-month target. Prefer names that reduce the user's concentration when quality is equal.

## 3. Save
Replace `research/watchlist.md` with the best 3 (keep the same table columns; list on-hold names below it), commit and push to the current branch. The hourly buy-target alert reads this file.

## 4. Reply format (chat, concise)
1. Portfolio table (holding | now | P/L | action).
2. Best 3 (numbered, with the fields above).
3. One line: what to do with available cash.
No long source lists or caveat paragraphs — a single short "Sources:" line at most.
