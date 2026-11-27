# crypto-ic — BTCUSD_V6_M15, 24/7 demo forward test on IC Markets

One instance, one magic, one log. **This is not the gold/FX bot.** `/opt/mt5/bot`
(`smc-bot.service`, MetaQuotes-Demo, :99, `/root/.mt5`, magics 990515/991515/992515) is a separate
frozen forward test and is not touched by anything here.

| | |
|---|---|
| venue | IC Markets demo, account 53158615, `ICMarketsSC-Demo`, Raw Trading Ltd |
| prefix / display | `/root/.mt5-icmarkets` / `:101` (`xvfb101.service`) |
| symbol | `BTCUSD` only (no suffix), digits 2, contract 1.0, min lot 0.01, step 0.01, **IOC** (mask 2) |
| instance / magic | `BTCUSD_V6_M15` / **993515** |
| executed by | Windows python in the prefix: `wine /root/.mt5-icmarkets/drive_c/Python310/python.exe` |
| balance | live from `account_info().balance` every decision (started $500, topped up to $1500 on 2026-11-20) |

## Rules

Frozen **v0.6 M15**: K=2.0 volume-candle **H1** bias (life 24 H1 bars), OBR/OBC/BB zones with SLQ
arming, tap + 6-bar confirmation window, confirmations **engulf | star | wick**, SL beyond the OB
body, **TP = 3R from the actual fill**, cost gate **SL ≥ 6 × spread**, **3 trades / 2 losses per UTC
day**, one position at a time, risk **0.5 %** of the live balance, `lots = risk / (SL distance ×
contract 1.0)` floored to 0.01, skip below min lot. Market order with SL and TP attached; SL is
never widened; no averaging.

Filling mode is derived from the symbol mask and settled by `order_check`: `filling_mode` is a
**bitmask** (1 = FOK, 2 = IOC) and BTCUSD here reports **2**, so FOK is refused (10030) and **IOC**
fills. Confirmed by a real 0.01-lot round trip on 2026-11-20 (order 2107518582 → fill 61930.06 with
SL 61682.94 and TP 62673.82 attached, closed −$0.06). Balance, and therefore lot size, is read from
`account_info().balance` at every decision.

24/7 only (venue, not strategy): no session window, no flat-at-12:00-NY, no midnight exit; the daily
stop is counted on the UTC day. SL buffer = `max(2 × bar spread, 0.05 % of price)` — the frozen
`BUF_MIN=0.30` is gold-scaled (0.008 % of $3700), 0.05 % of $62k is the equivalent floor.
Commission 0 (spread-only CFD), so the gate is `SL ≥ 6 × spread`.

`engine.py` / `engine_v05.py` / `engine_v06.py` are byte-identical to
`/Users/snad/Documents/SMC-Mentorship/backtest/` (md5 checked at install).

## Files

- `crypto_ic.py` — 24/7 switches, MT5→frame adapter, `context()` (detection), `decide()` (selection,
  same checks and order as `engine_v05.run_config`), exit simulator. Shared by bot and replay.
- `bot.py` — the service loop: closed-M15-bar evaluation, market orders, deal reconciliation,
  heartbeat every 30 min.
- `replay.py` — last-N-days replay **twice**: batch (backtest path) vs live (bar-by-bar truncated
  windows, the exact functions the service calls) and a field-level diff. This is lesson 26.
- `report.py` — summary from `events.jsonl` (trades, win rate, total R, P/L, balance, open position,
  gate funnel); `--live` adds the broker's own view.
- `bot.py --selftest [--send-test-trade]` — execution-path check (order_check, optionally a 0.01-lot
  open+close); `--flatten` closes anything carrying magic 993515; `--once --as-of "YYYY-MM-DD HH:MM"`
  re-evaluates one historical bar through the live code path; `--once --order-check` never sends.
- `DEPLOY.sh` — stops the ad-hoc terminal, patches `[Experts] Enabled=1` (AutoTrading) in
  `common.ini`, installs/enables/starts `xvfb101`, `mt5-ic-terminal`, `crypto-ic-bot`, waits 60 s and
  prints status + last 3 events.
- `events.jsonl` — one JSON line per event, same schema as the MT5 bot (`ts_utc, ts_ny, ts_ghana,
  instance, type, symbol, side, entry, sl, tp, rr, lots, risk_usd, result_R, pnl_usd, balance,
  reason, entry_type, confirmation, bias_reason`) plus `slippage_R` on `order_placed`.
- `state.json` — used/consumed keys, daily counters, order metadata, last processed M15 close.

Event types: `heartbeat`, `spec`, `signal`, `order_placed`, `filled`, `closed`, `skipped`
(`cost_gate|daily_stop|busy|no_bias`), `order_rejected`, `disconnected`, `reconnected`, `error`.

## Ops

    systemctl status crypto-ic-bot mt5-ic-terminal xvfb101 --no-pager
    journalctl -u crypto-ic-bot -n 50                 # or tail /opt/crypto-ic/service.log
    /usr/bin/python3 /opt/crypto-ic/report.py         # log only, no MT5 needed
    python3 /opt/crypto-ic/report.py --live           # under DISPLAY=:101 + the prefix env

Replay (read-only, no orders):

    cd /opt/crypto-ic && DISPLAY=:101 WINEPREFIX=/root/.mt5-icmarkets WINEDEBUG=-all \
      WINEDLLOVERRIDES=mscoree,mshtml= timeout 900 \
      wine /root/.mt5-icmarkets/drive_c/Python310/python.exe -u replay.py --days 10

## Measured on this venue (2026-11-20)

- spread $5.00 (500 pts) vs median M15 ATR ≈ $194–276 → ≈0.02R per trade; the 6× gate
  (SL ≥ $30) passes comfortably, unlike Binance taker costs (lesson 27).
- ETHUSD spread $2.90–2.96 against ATR ≈ $8 (≈0.37R) → **excluded**; ETH stays in the Binance paper sim.
- Replay 2026-11-05..11-20 (1479 M15 bars, `--days 15`): **2 trades** — OBC engulf short
  2026-11-14 07:30 (entry 67171.46, SL 67436.32) and OBC wick long 2026-11-15 15:15 (entry 64648.06,
  SL 64466.84), both −1R — and **batch vs live: 0 differences** (funnel: 13 candidates, 1 killed by
  bias, 0 by the gate).
- Signal arrival is thin: ~2 trades / 15 days ≈ 4 a month, so 60 trades needs ~15 months. Do not
  expect a verdict from this instance any time soon; it is a forward log, not a test of the edge.
