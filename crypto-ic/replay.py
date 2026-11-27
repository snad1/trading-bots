#!/usr/bin/env python3
"""Dry-run replay of BTCUSD M15 for the last N days from MT5 data, twice, and diff the two paths.

  batch : one pass over the whole window, event-order exactly like engine_v05.run_config
          (the backtest path) -> "engine_v06 with the 24/7 switches"
  live  : bar by bar, truncated windows, the same crypto_ic.context()/decide() the service calls

The point is lesson 26: the live path must match the backtest on the same data.  Any mismatch is
printed with its bar so it can be explained.

Run (on the VPS, inside the IC prefix):
  DISPLAY=:101 WINEPREFIX=/root/.mt5-icmarkets wine .../python.exe replay.py --days 10
Writes replay_signals.csv / .json next to this file.
"""
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crypto_ic as C
import engine as E


def mt5_frames(days, extra_m15=900, extra_h1=600):
    import MetaTrader5 as mt5
    if not mt5.initialize():
        raise SystemExit(f'mt5.initialize failed {mt5.last_error()}')
    ai = mt5.account_info()
    print(f'account {ai.login} {ai.server} balance {ai.balance} {ai.currency}')
    si = mt5.symbol_info(C.SYMBOL)
    if si is None or not mt5.symbol_select(C.SYMBOL, True):
        raise SystemExit(f'{C.SYMBOL} not available')
    rng = max(days, 1) * 24 * 60
    m15 = C.frame_from_mt5(mt5.copy_rates_from_pos(C.SYMBOL, mt5.TIMEFRAME_M15, 0, rng // 15 + extra_m15),
                           C.server_offset(mt5), si.point)
    h1 = C.frame_from_mt5(mt5.copy_rates_from_pos(C.SYMBOL, mt5.TIMEFRAME_H1, 0, rng // 60 + extra_h1),
                          C.server_offset(mt5), si.point)
    return m15.dropna(), h1.dropna()


def mk_M(m15):
    """market object for crypto_ic.simulate: bid/ask OHLC at native M15 resolution."""
    class M:
        pass
    M.ts = m15.index.values.astype('datetime64[ns]')
    M.bo, M.bh, M.bl, M.bc = (m15[k].values for k in ('o', 'h', 'l', 'c'))
    M.ao, M.ah, M.al, M.ac = (m15[k].values for k in ('oa', 'ha', 'la', 'ca'))
    return M


def run_batch(m15, h1, t0):
    """engine_v05.run_config with the crypto 24/7 switches (the backtest path)."""
    P, S, BE = C.context(m15, h1)
    M = mk_M(m15)
    bts = np.array([e['ts'] for e in BE], dtype='datetime64[ns]') if BE else np.array([], dtype=np.datetime64)
    used, consumed, daycnt = set(), set(), {}
    busy = np.datetime64('1970-01-01')
    rows, fun = [], dict(events=0, before_t0=0, busy=0, daily=0, bias=0, gate=0, no_bar=0, trades=0)
    evs = []
    c = P['c']
    for si, s in enumerate(S):
        near = s['hi'] if s['d'] == 1 else s['lo']
        for j, kind, ext in s['confs']:
            if kind in C.CONFS:
                evs.append((j, abs(c[j] - near), -s['ta'], si, kind, ext))
    evs.sort()
    for j, _, _, si, kind, ext in evs:
        s, ts, d = S[si], P['bclose'][j], S[si]['d']
        if ts < t0:
            fun['before_t0'] += 1
            continue
        fun['events'] += 1
        if si in used:
            continue
        if ts < busy:
            fun['busy'] += 1
            continue
        dkey = C.day_key(ts)
        nt, nl = daycnt.get(dkey, (0, 0))
        if nt >= C.DAILY_MAX_TRADES or nl >= C.DAILY_MAX_LOSSES:
            fun['daily'] += 1
            continue
        bi = C.bias_check(P, S, BE, si, j, consumed)
        if bi is None:
            fun['bias'] += 1
            continue
        i = np.searchsorted(M.ts, ts)
        if i >= len(M.ts) or M.ts[i] != ts:
            fun['no_bar'] += 1
            continue
        spr = float(P['spr'][j])
        fill = float(M.ao[i] if d == 1 else M.bo[i])
        lv, why = C.levels(s, d, fill, spr, float(P['c'][j]))
        if lv is None:
            fun['gate'] += 1
            continue
        sl, tp, risk = lv
        r = C.simulate(M, ts, d, fill, sl, tp)
        if r is None:
            continue
        busy, nt, nl = r['exit_ts'], nt + 1, nl + (r['R'] < 0)
        daycnt[dkey] = (nt, nl)
        used.add(si)
        consumed.add(C.bias_key(BE[bi]))
        fun['trades'] += 1
        rows.append(dict(signal_ts=pd.Timestamp(ts), side='long' if d == 1 else 'short', entry_type=s['type'],
                         confirmation=kind, entry=fill, sl=sl, tp=tp, risk=risk, spread=spr,
                         exit=r['exit'], how=r['how'], exit_ts=pd.Timestamp(r['exit_ts']), R=r['R'],
                         bias_reason=C.bias_reason(BE[bi])))
    return pd.DataFrame(rows), fun


def run_live(m15, h1, t0):
    """Bar by bar over truncated windows, through the same context()/decide() the service uses."""
    M = mk_M(m15)
    h1t = h1.index.values.astype('datetime64[ns]')
    idx = m15.index
    j0 = int(np.searchsorted(idx.values, np.datetime64(t0)))
    used, consumed, daycnt = set(), set(), {}
    open_until = np.datetime64('1970-01-01')
    rows, fun = [], dict(bars=0, events=0, busy=0, daily=0, bias=0, gate=0, no_bar=0, trades=0)
    for j in range(j0, len(idx)):
        ts = np.datetime64(np.datetime64(idx.values[j], 'm') + np.timedelta64(C.TF, 'm'))
        fun['bars'] += 1
        m15w = m15.iloc[:j + 1]
        h1w = h1.iloc[:max(1, int(np.searchsorted(h1t, ts)))]
        try:
            P, S, BE = C.context(m15w, h1w)
        except Exception as e:                                   # noqa: BLE001
            print(f'  detection failed at {ts}: {type(e).__name__} {e}')
            continue
        if pd.Timestamp(P['bclose'][-1]).tz_localize('UTC') != pd.Timestamp(ts).tz_localize('UTC'):
            continue
        dkey = C.day_key(ts)
        st = dict(used=used, consumed=consumed, nt=daycnt.get(dkey, (0, 0))[0], nl=daycnt.get(dkey, (0, 0))[1])
        busy = ts < open_until

        def quote(_st, d, j=j):
            if j + 1 >= len(idx):
                return None, 'window end'
            return float(M.ao[j + 1] if d == 1 else M.bo[j + 1]), float(P['spr'][j])

        ch, log = C.decide(P, S, BE, j, st, busy, quote)
        fun['events'] += len(log)
        for si, kind, ext, bi, why in log:
            if why.startswith('cost_gate'):
                fun['gate'] += 1
            elif why.startswith('busy'):
                fun['busy'] += 1
            elif why.startswith('daily'):
                fun['daily'] += 1
            elif why.startswith('no matching'):
                fun['bias'] += 1
        if ch is None:
            continue
        s, d = S[ch['si']], S[ch['si']]['d']
        r = C.simulate(M, ts, d, ch['fill'], ch['sl'], ch['tp'])
        if r is None:
            fun['no_bar'] += 1
            continue
        open_until = r['exit_ts']
        nt, nl = daycnt.get(dkey, (0, 0))
        daycnt[dkey] = (nt + 1, nl + (r['R'] < 0))
        used.add(ch['key'])
        consumed.add(C.bias_key(BE[ch['bi']]))
        fun['trades'] += 1
        rows.append(dict(signal_ts=pd.Timestamp(ts), side='long' if d == 1 else 'short', entry_type=s['type'],
                         confirmation=ch['kind'], entry=ch['fill'], sl=ch['sl'], tp=ch['tp'], risk=ch['risk'],
                         spread=ch['spr'], exit=r['exit'], how=r['how'], exit_ts=pd.Timestamp(r['exit_ts']),
                         R=r['R'], bias_reason=C.bias_reason(BE[ch['bi']])))
    return pd.DataFrame(rows), fun


def diff(a, b):
    """compare two trade lists on the fields that define a trade"""
    cols = ['signal_ts', 'side', 'entry_type', 'confirmation', 'entry', 'sl', 'tp']
    if a.empty and b.empty:
        return []
    ka = [(r.signal_ts, r.side, r.entry_type, r.confirmation) for r in a.itertuples()]
    kb = [(r.signal_ts, r.side, r.entry_type, r.confirmation) for r in b.itertuples()]
    out = []
    for k in [x for x in ka if x not in kb]:
        out.append(f'batch only: {k}')
    for k in [x for x in kb if x not in ka]:
        out.append(f'live only : {k}')
    common = [(i, k) for i, k in enumerate(ka) if k in kb]
    for i, k in common:
        ra, rb = a.iloc[i], b.iloc[[x for x, y in enumerate(kb) if y == k][0]]
        for col in ('entry', 'sl', 'tp'):
            if abs(float(ra[col]) - float(rb[col])) > 1e-6:
                out.append(f'{k} {col}: batch {ra[col]:.2f} live {rb[col]:.2f}')
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=10)
    ap.add_argument('--out', default=os.path.join(HERE, 'replay_signals'))
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    m15, h1 = mt5_frames(a.days)
    t0 = np.datetime64((pd.Timestamp.now(tz='UTC').normalize() - pd.Timedelta(days=a.days)).tz_localize(None))
    print(f'M15 {len(m15)} bars [{m15.index[0]} .. {m15.index[-1]}]  H1 {len(h1)} bars  '
          f'spread median {m15.ca.sub(m15.c).median():.2f}  window from {t0}')
    bt, fb = run_batch(m15, h1, t0)
    lt, fl = run_live(m15, h1, t0)
    print('\nBATCH funnel', fb)
    print('LIVE  funnel', fl)
    d = diff(bt, lt)
    print(f'\nbatch trades {len(bt)}  live trades {len(lt)}  differences {len(d)}')
    for x in d:
        print('  ', x)
    if not bt.empty:
        show = bt.copy()
        show['signal_ts'] = show.signal_ts.dt.strftime('%Y-%m-%d %H:%M')
        show['exit_ts'] = show.exit_ts.dt.strftime('%Y-%m-%d %H:%M')
        for c in ('entry', 'sl', 'tp', 'risk', 'spread', 'exit'):
            show[c] = show[c].round(2)
        show['R'] = show['R'].round(3)
        cols = ['signal_ts', 'side', 'entry_type', 'confirmation', 'entry', 'sl', 'tp', 'spread', 'how', 'exit', 'R']
        print('\n' + show[cols].to_string(index=False))
        print(f"\ntotal {len(bt)} trades  win {int((bt.R > 0).sum())}  "
              f"win_rate {(bt.R > 0).mean():.0%}  sum R {bt.R.sum():+.2f}  "
              f"avg R {bt.R.mean():+.3f}")
        bt.to_csv(a.out + '.csv', index=False)
    if a.json:
        json.dump(dict(trades=len(bt), funnel_batch=fb, funnel_live=fl, differences=d),
                  open(a.out + '.json', 'w'), indent=1, default=str)
    return 0 if not d else 1


if __name__ == '__main__':
    sys.exit(main())
