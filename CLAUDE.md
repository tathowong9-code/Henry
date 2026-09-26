# Henry — equity research + Trading 212

## Trading rules (non-negotiable)
- **Never place, modify or cancel any Trading 212 order (buy or sell) without the user's explicit approval in a chat message for that specific order** (ticker, side, quantity/value, order type, price). Propose first, wait for "yes", then execute. Approval does not carry over to other orders.
- Read-only calls (account summary, portfolio, orders, history) are always fine.
- Always check live prices (Trading 212 portfolio `currentPrice` for held names; web search for others) before commenting on a setup, and report which watchlist tickers are "in shape" (inside or near their entry zone) vs. not.

## Trading 212 API
- Helper: `scripts/t212.sh METHOD PATH [JSON]` — reads `T212_API_KEY` / `T212_API_SECRET` from the environment. Never print or commit credentials.
- Account is GBP; US tickers use the `XXX_US_EQ` format (e.g. `MDB_US_EQ`).

## Research
- Framework: `prompts/equity-research-framework.md`. Reports: `research/`.
- Current watchlist and levels live in `research/watchlist.md`.
