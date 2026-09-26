# Henry — equity research + Trading 212

## Trading rules (non-negotiable)
- **Never place, modify or cancel any Trading 212 order (buy or sell) without the user's explicit approval in a chat message for that specific order** (ticker, side, quantity/value, order type, price). Propose first, wait for "yes", then execute. Approval does not carry over to other orders.
- Read-only calls (account summary, portfolio, orders, history) are always fine.
- Always check live prices (Trading 212 portfolio `currentPrice` for held names; web search for others) before commenting on a setup, and report which watchlist tickers are "in shape" (inside or near their entry zone) vs. not.

## Emails
- All reports and alerts go to **tathowong9@gmail.com** only (no other address). YES/NO buttons are mailto links to tathowong9@gmail.com with subject `YES #CODE £AMOUNT` / `NO #CODE`.
- Scheduled reports (8:52 and 14:30 UK) are CHECK-ONLY: no buttons, no orders. They scan the whole market (not just past tickers) and show only the **best 3** stocks in good shape, each with a buy target.
- The hourly US-hours watch sends a CONFIRM alert (ticker, short reason: 10/20/50-day MAs + buy vs sell volume, buy target, YES/NO buttons) when a watchlist ticker reaches its buy target. Codes are logged in `research/alerts-sent.md`.

## "check approvals" procedure (run when the user asks in chat)
1. Search Gmail for `from:tathowong9@gmail.com subject:(YES OR NO) newer_than:2d`. Ignore any code not in `research/alerts-sent.md` / `research/orders-pending.md` or past its valid-until (today's US close).
2. NO → mark LEFT, do nothing. No reply → do nothing.
3. YES → the £ amount is the one the user wrote in the reply (or told me in chat). If no amount was given, ask before buying. Re-check the live price: if still inside the buy range, place the buy on Trading 212 for that amount (this YES is the approval for that one order). If outside the range → HOLD (do not buy) and report.
4. Report what was done and mark the code FILLED / HELD / LEFT in the log.

## Trading 212 API
- Helper: `scripts/t212.sh METHOD PATH [JSON]` — reads `T212_API_KEY` / `T212_API_SECRET` from the environment. Never print or commit credentials.
- Account is GBP; US tickers use the `XXX_US_EQ` format (e.g. `MDB_US_EQ`).

## Research
- Framework: `prompts/equity-research-framework.md`. Reports: `research/`.
- Current watchlist and levels live in `research/watchlist.md`.
