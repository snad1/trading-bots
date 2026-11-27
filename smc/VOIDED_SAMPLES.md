# Voided samples — MT5 demo (account 5059023305)

Trades that must be **excluded from any statistical review**, because the position size
was wrong at the time. Keep this file next to the bot; the `events.jsonl` record stays
untouched as the raw log.

## 2026-11-20 — lot sizing bug (10× too large on XAUUSD)

Cause: `per_lot = risk * trade_tick_value / trade_tick_size` returned $10 per $1 move
per lot on gold, while the true figure is $100 (contract size 100 oz). Fixed in
`smc_bot.py` on 2026-11-20 (now `risk * trade_contract_size`, with a warning logged when
the two methods disagree). The bot was restarted with the fix at 15:29 UTC.

| Position | Instance | Side | Lots | Entry | Exit | P&L | Reported R | True R |
|---|---|---|---|---|---|---|---|---|
| 59445530762 | XAUUSD_M10 | sell | 0.23 | 3570.63 | 3528.43 (TP) | +$970.60 | +19.51 | ≈ +2.0 |
| 59445530879 | XAUUSD_M15 | sell | 0.39 | 3579.54 | 3554.45 (TP) | +$978.51 | +19.67 | ≈ +2.0 |

- Intended risk was $50; **actual risk was $485 and $489** (≈5% of the account each).
- The reported R is meaningless because it divides P&L by the *intended* $50.
- Direction was correct, so the setups themselves are still useful as qualitative
  examples. They are **not** valid samples for expectancy, win rate or drawdown.
- The GBPUSD trade of the same day (−$49.45, −1.0R) sized correctly on FX and **is**
  valid.

Rule going forward: any review that includes MT5 gold results must start from the
restart at 2026-11-20 15:29 UTC or later.
