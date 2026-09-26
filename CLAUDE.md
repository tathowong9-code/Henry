# Henry — equity research + Trading 212

## Trading rules (non-negotiable)
- **Never place, modify or cancel any Trading 212 order (buy or sell) without the user's explicit approval in a chat message for that specific order** (ticker, side, quantity/value, order type, price). Propose first, wait for "yes", then execute. Approval does not carry over to other orders.
- Read-only calls (account summary, portfolio, orders, history) are always fine.
- Always check live prices (Trading 212 portfolio `currentPrice` for held names; web search for others) before commenting on a setup, and report which watchlist tickers are "in shape" (inside or near their entry zone) vs. not.

## Emails
- Reports and alerts are for **tathowong99@gmail.com** (user's inbox). The Gmail connector mailbox is tathowong9@gmail.com; APPROVE/REJECT (YES/NO) buttons are mailto links that send from tathowong99 to tathowong9 with subject `YES #CODE` / `NO #CODE`.
- Scheduled reports (8:52 and 14:30 UK) are CHECK-ONLY: no buttons, no orders.
- The hourly US-hours watch sends a CONFIRM alert (ticker, short reason: 10/20/50-day MAs + buy vs sell volume, proposed order, YES/NO buttons) when a watchlist ticker reaches its buy target. Codes are logged in `research/alerts-sent.md`.

## "check approvals" procedure (run only when the user asks in chat)
1. Search Gmail for `from:tathowong99@gmail.com subject:(YES OR NO) newer_than:2d`. Ignore any other sender and any code not in `research/alerts-sent.md` / `research/orders-pending.md` or past its valid-until (today's US close).
2. NO → mark LEFT, do nothing. No reply → do nothing.
3. YES → re-check the live price. If still inside the entry zone (and at or below the proposed limit), place the proposed limit order on Trading 212 (the user's YES plus their "check approvals" request is the approval for that one order). If outside the zone → HOLD (do not buy), report it.
4. Report what was done and mark the code FILLED / HELD / LEFT in the log.

## Trading 212 API
- Helper: `scripts/t212.sh METHOD PATH [JSON]` — reads `T212_API_KEY` / `T212_API_SECRET` from the environment. Never print or commit credentials.
- Account is GBP; US tickers use the `XXX_US_EQ` format (e.g. `MDB_US_EQ`).

## Research
- Framework: `prompts/equity-research-framework.md`. Reports: `research/`.
- Current watchlist and levels live in `research/watchlist.md`.
