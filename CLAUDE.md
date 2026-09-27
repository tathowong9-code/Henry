# Henry — equity research + Trading 212

## Trading rules (non-negotiable)
- **Never place, modify or cancel any Trading 212 order (buy or sell) without the user's explicit approval in a chat message for that specific order** (ticker, side, quantity/value, order type, price). Propose first, wait for "yes", then execute. Approval does not carry over to other orders.
- Read-only calls (account summary, portfolio, orders, history) are always fine.
- Always check live prices (Trading 212 portfolio `currentPrice` for held names; web search for others) before commenting on a setup, and report which watchlist tickers are "in shape" (inside or near their entry zone) vs. not.

## Emails
- All reports and alerts go to **tathowong9@gmail.com** only.
- Scheduled reports (8:52 and 14:30 UK) are CHECK-ONLY. They scan the whole market (not just past tickers) and show only the **best 3** stocks in good shape, each with a buy target.
- The hourly US-hours alert emails when a best-3 ticker reaches its buy target: ticker, good price to get in, short reason (10/20/50-day MAs + buy vs sell volume) and a hold plan (holding period, trims, stops, target). **No buttons, no approval flow — the user buys by themselves.** Alerts are logged in `research/alerts-sent.md`.

## Trading 212 API
- Helper: `scripts/t212.sh METHOD PATH [JSON]` — reads `T212_API_KEY` / `T212_API_SECRET` from the environment. Never print or commit credentials.
- Account is GBP; US tickers use the `XXX_US_EQ` format (e.g. `MDB_US_EQ`).

## Research
- Framework: `prompts/equity-research-framework.md`. Reports: `research/`.
- Current watchlist and levels live in `research/watchlist.md`.
