#!/usr/bin/env python3
"""Summary of the BTCUSD_V6_M15 24/7 demo forward test (frozen v0.6 M15 rules, IC Markets).

Usage:  python report.py [/opt/crypto-ic/events.jsonl] [--json] [--live]

Reads events.jsonl only (no MT5 needed, so the Mac copy works on a pulled log): trades, win rate,
total R, expectancy, profit factor, max drawdown, P/L and balance from the 'closed' events, the
gate funnel from signal/skipped events, and the current position (last order_placed without a
matching closed).  --live additionally prints the broker's own view from MT5.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

DEFAULT = '/opt/crypto-ic/events.jsonl'
START_BAL = 1500.0          # demo balance at launch (Snad topped up 500 -> 1500 on 2026-11-20)


def load(path):
    rows, bad = [], 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:                                    # noqa: BLE001
                bad += 1
    return rows, bad


def ts(s):
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:                                            # noqa: BLE001
        return datetime.min


def streaks(rs):
    best = cur = 0
    for r in rs:
        cur = cur + 1 if r < 0 else 0
        best = max(best, cur)
    return best


def stats(closed, span_days):
    rs = [float(r['result_R']) for r in closed]
    n = len(rs)
    if not n:
        return dict(n=0)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    cum = peak = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    bal = [float(r['balance']) for r in closed if r.get('balance') is not None]
    pnl = sum(float(r.get('pnl_usd') or 0) for r in closed)
    return dict(n=n, win=len(wins) / n, totR=sum(rs), expR=sum(rs) / n,
                pf=(sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float('inf'),
                maxddR=dd, streak=streaks(rs), tpm=n / (span_days / 30.44) if span_days else 0.0,
                bal=bal[-1] if bal else None, pnl=pnl, best=max(rs), worst=min(rs),
                wins=len(wins), losses=len(losses))


def open_position(rows):
    """last order_placed whose position has no closed event yet"""
    placed = {}
    for r in rows:
        if r.get('type') == 'order_placed':
            ticket = None
            for tok in (r.get('reason') or '').split():
                if tok.startswith('ticket='):
                    ticket = tok.split('=', 1)[1]
            placed[ticket or f'#{r.get("ts_utc")}'] = r
        elif r.get('type') == 'closed':
            for tok in (r.get('reason') or '').split():
                if tok.startswith('position='):
                    placed.pop(tok.split('=', 1)[1], None)
    return list(placed.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', nargs='?', default=DEFAULT)
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--live', action='store_true', help='also print the broker position from MT5')
    ap.add_argument('--start-bal', type=float, default=START_BAL, help='balance at launch, for P/L and %%')
    a = ap.parse_args()
    if not os.path.exists(a.path):
        sys.exit(f'no log at {a.path}')

    rows, bad = load(a.path)
    inst = next((r['instance'] for r in rows if r.get('instance')), 'BTCUSD_V6_M15')
    closed = sorted([r for r in rows if r.get('type') == 'closed' and r.get('result_R') is not None],
                    key=lambda r: ts(r['ts_utc']))
    first = min((ts(r['ts_utc']) for r in rows if r.get('ts_utc')), default=None)
    last = max((ts(r['ts_utc']) for r in rows if r.get('ts_utc')), default=None)
    span = max(((last - first).total_seconds() / 86400.0) if first and last else 1.0, 1.0)

    hb = [r for r in rows if r.get('type') == 'heartbeat']
    start_bal = a.start_bal
    bal = next((float(r['balance']) for r in reversed(rows) if r.get('type') in ('heartbeat', 'closed')
                and r.get('balance') is not None), start_bal)

    print(f'\n=== {inst} — frozen v0.6 M15, 24/7, IC Markets demo (BTCUSD, magic 993515) ===')
    print(f'log {a.path}   events {len(rows)}{"  bad lines " + str(bad) if bad else ""}')
    if first:
        print(f'period {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M}Z  ({span:.1f} days)   '
              f'heartbeats {len(hb)}   balance ${bal:,.2f}   P/L ${bal - start_bal:+,.2f} '
              f'({(bal / start_bal - 1) * 100:+.2f}%)')

    st = stats(closed, span)
    if not st['n']:
        print('\nno closed trades yet')
    else:
        print(f"\ntrades {st['n']}   wins {st['wins']}   win rate {100 * st['win']:.0f}%   "
              f"total R {st['totR']:+.2f}   expectancy {st['expR']:+.3f}R")
        print(f"profit factor {st['pf']:.2f}   max drawdown {st['maxddR']:.2f}R   "
              f"longest losing streak {st['streak']}   trades/month {st['tpm']:.1f}")
        print(f"P/L ${st['pnl']:+,.2f}   best {st['best']:+.2f}R   worst {st['worst']:+.2f}R")
        print(f"\n{'signal_ts':17} {'side':6} {'type':4} {'conf':7} {'entry':>10} {'sl':>10} {'tp':>10} "
              f"{'R':>6} {'pnl$':>8}  exit")
        for r in closed:
            print(f"{str(r.get('ts_utc'))[:16]:17} {(r.get('side') or ''):6} {(r.get('entry_type') or ''):4} "
                  f"{(r.get('confirmation') or ''):7} {float(r.get('entry') or 0):10.2f} "
                  f"{float(r.get('sl') or 0):10.2f} {float(r.get('tp') or 0):10.2f} "
                  f"{float(r['result_R']):+6.2f} {float(r.get('pnl_usd') or 0):8.2f}  "
                  f"{(r.get('reason') or '')[:40]}")

    pos = open_position(rows)
    print('\nopen position: ' + ('none' if not pos else ', '.join(
        f"{p.get('side')} {p.get('lots')} @ {p.get('entry')} SL {p.get('sl')} TP {p.get('tp')}" for p in pos)))

    print('\nfunnel')
    fun = Counter()
    for r in rows:
        t = r.get('type')
        if t == 'signal':
            fun['signals'] += 1
        elif t == 'filled':
            fun['fills'] += 1
        elif t == 'skipped':
            reason = (r.get('reason') or '')
            if 'cost_gate' in reason:
                fun['skipped: cost_gate'] += 1
            elif 'daily' in reason:
                fun['skipped: daily_stop'] += 1
            elif 'busy' in reason:
                fun['skipped: busy'] += 1
            elif 'bias' in reason:
                fun['skipped: no_bias'] += 1
            else:
                fun['skipped: other'] += 1
        elif t in ('order_rejected', 'error'):
            fun[t] += 1
    line = '   '.join(f'{k}={v}' for k, v in sorted(fun.items()))
    print('  ' + (line or '(no signal/skip events yet)'))
    mix = Counter((r.get('entry_type'), r.get('confirmation')) for r in closed)
    if mix:
        print('  mix  ' + '  '.join(f'{a_}/{b}={v}' for (a_, b), v in mix.most_common()))

    if a.live:
        try:
            import MetaTrader5 as mt5
            if mt5.initialize():
                ai = mt5.account_info()
                ps = [p for p in (mt5.positions_get(symbol='BTCUSD') or []) if p.magic == 993515]
                print(f"\nbroker: balance ${ai.balance:,.2f} equity ${ai.equity:,.2f} "
                      f"positions {len(ps)}")
                for p in ps:
                    print(f"  {p.ticket} {p.volume} lots @ {p.price_open} sl {p.sl} tp {p.tp} "
                          f"profit {p.profit:+.2f}")
        except Exception as e:                                   # noqa: BLE001
            print(f'\n(live view unavailable: {type(e).__name__} {e})')

    if a.json:
        print('\n' + json.dumps(dict(instance=inst, balance=bal, pnl=bal - start_bal, **st), indent=1,
                                default=float))


if __name__ == '__main__':
    main()
