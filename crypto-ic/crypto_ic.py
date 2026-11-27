"""Frozen v0.6 M15 rules, 24/7, on IC Markets BTCUSD -- shared layer for the live bot and its replay.

FROZEN (engine.py / engine_v05.py / engine_v06.py, byte-identical to
/Users/snad/Documents/SMC-Mentorship/backtest/*.py):
  M15 entries, H1 volume-candle bias (K=2.0, life 24 H1 bars), OBR/OBC/BB zones with SLQ arming,
  tap + 6-bar confirmation window, confirmations engulf | star | wick, SL beyond the OB body,
  TP = 3R from the actual fill, cost gate SL >= 6 x spread, 3 trades / 2 losses per day,
  one position at a time.

ONLY adaptations (venue, not strategy):
  * 24/7 clock: no session window, no flat-at-12:00-NY, no midnight exit; daily stop on the UTC day.
  * SL buffer = max(2 x bar spread, 0.05% of price).  The frozen BUF_MIN=0.30 is gold-scaled
    (0.008% of $3700); 0.05% of BTC's $62k is the equivalent floor.
  * commission = 0 (IC Markets CFD, spread only), so the gate is SL >= 6 x spread.
  * one instance, fixed symbol BTCUSD, magic 993515.

Bars are native M15 / H1 frames from MT5 (o,h,l,c plus ask columns oa,ha,la,ca); engine.resample()
of a native bar series at its own frequency returns the same bars, so the frozen engine sees the
bars it would have built from M1.
"""
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import engine as E          # noqa: E402
import engine_v05 as V      # noqa: E402
import engine_v06 as V6     # noqa: E402

# ---------------- instance ----------------
INSTANCE = 'BTCUSD_V6_M15'
SYMBOL = 'BTCUSD'
MAGIC = 993515
SUFFIX = ''

# ---------------- frozen v0.6 config ----------------
TF = 15
K = 2.0
SLMODE = 'ob'
CONFS = ('engulf', 'star', 'wick')
RR = V.RR                    # 3.0
GATE_X = V.GATE_X            # 6.0
COMM = 0.0                   # spread-only CFD
BUF_PCT = 0.0005             # 0.05% of price (crypto scaling of BUF_MIN)
DAILY_MAX_TRADES = 3
DAILY_MAX_LOSSES = 2
N_M15, N_H1 = 2400, 1400     # history per evaluation (native bars)
FAR = np.datetime64('2200-01-01T00:00:00', 'ns')

# ---------------- 24/7 clock: disable the frozen session filters ----------
E.in_window = (lambda tod, tf: True)
E.in_arm_zone = (lambda tod, tf: True)


# ---------------- broker clock ----------------
def server_offset(mt5, symbol=None, prev=None):
    """Hours to subtract from MT5 bar/tick times to get UTC (the server clock, not a timezone).
    Measured from a fresh tick; falls back to `prev`, then to UTC+2 (ICMarketsSC-Demo, 2026-11-20)."""
    tk = mt5.symbol_info_tick(symbol or SYMBOL)
    if tk is not None and tk.time:
        off = round((tk.time - time.time()) / 3600)
        if abs(off) <= 4 and abs(tk.time - off * 3600 - time.time()) < 600:
            return off
    return 2 if prev is None else prev


# ---------------- frames ----------------
def as_frame(o, h, l, c, spr, idx):
    """native bars -> frame the frozen engine accepts (ask columns = bid + spread)."""
    idx = pd.DatetimeIndex(idx)
    o, h, l, c, spr = (np.asarray(x, dtype='float64') for x in (o, h, l, c, spr))
    return pd.DataFrame({'o': o, 'h': h, 'l': l, 'c': c,
                         'oa': o + spr, 'ha': h + spr, 'la': l + spr, 'ca': c + spr}, index=idx)


def frame_from_mt5(rates, offset_h, point):
    """MT5 copy_rates_* array -> native frame on a UTC open-time index, spread in price units."""
    df = pd.DataFrame(rates)
    if len(df) == 0:
        return None
    idx = pd.to_datetime(df['time'].values - int(offset_h) * 3600, unit='s', utc=True)
    spr = df['spread'].values.astype('float64') * float(point)
    m = as_frame(df['open'].values, df['high'].values, df['low'].values, df['close'].values, spr, idx)
    stale = (m.o == m.h) & (m.h == m.l) & (m.l == m.c) & (m.c == m.c.shift()) & (m.ca == m.ca.shift())
    return m[~stale]


# ---------------- detection ----------------
def context(m15, h1):
    """P, S, BE exactly as engine_v05 builds them, plus the live-only bias-expiry fix:
    bias_events() sets exp = close of H1 bar t+24, or t_close+24h when bar t+24 is not in the
    (truncated) data yet.  The causal equivalent is 'not expired yet' -> +inf (fewer than 24 H1
    bars have closed since the event)."""
    P = V.prepare(m15, TF, N=2)
    S = V.setups(P)
    BE = V6.bias_events(h1, K, bias_tf=60)
    nh = len(E.resample(h1, 60))
    for e in BE:
        if e['t'] + V.BIAS_LIFE >= nh:
            e['exp'] = FAR
    return P, S, BE


def candidates(P, S, j):
    """Confirmations closing at bar j, ordered exactly like engine_v05.run_config."""
    c = P['c']
    out = []
    for si, s in enumerate(S):
        near = s['hi'] if s['d'] == 1 else s['lo']
        for jj, kind, ext in s['confs']:
            if jj == j and kind in CONFS:
                out.append((abs(c[jj] - near), -s['ta'], si, kind, ext))
    out.sort()
    return [(si, kind, ext) for _, _, si, kind, ext in out]


def setup_key(P, s):
    return (f"{s['type']}|{s['d']}|{pd.Timestamp(P['idx'].values[s['ob_bar']])}|"
            f"{pd.Timestamp(P['bclose'][s['t_ev']])}|{pd.Timestamp(P['bclose'][s['ta']])}")


def bias_key(e):
    return str(pd.Timestamp(e['ts']))


def bias_reason(e):
    if e is None:
        return None
    return (f"H1 {'bull' if e['d'] == 1 else 'bear'} volume candle "
            f"{pd.Timestamp(e['open_ts']).strftime('%m-%d %H:%M')}Z ({e['body_x']:.1f}x median body) "
            f"{e['kind']} of key zone {e['zkind']} {e['zone']:.5g}")


def levels(s, d, fill, spr, px):
    """SL beyond the OB body + buffer max(2 x spread, 0.05% of price); TP 3R; gate SL >= 6 x spread."""
    buf = max(2.0 * spr, BUF_PCT * px)
    sl = (s['lo'] - buf) if d == 1 else (s['hi'] + buf)
    risk = d * (fill - sl)
    if risk <= 0:
        return None, f'risk<=0 (fill {fill:.2f} beyond SL {sl:.2f})'
    if risk < GATE_X * spr:
        return None, f'cost_gate: SL {risk:.2f} < 6x spread {GATE_X * spr:.2f} (spread {spr:.2f})'
    return (float(sl), float(fill + d * RR * risk), float(risk)), None


def describe(P, S, BE, si, kind, ext, bi):
    s = S[si]
    return dict(side='long' if s['d'] == 1 else 'short', entry_type=s['type'], confirmation=kind,
                bias_reason=bias_reason(BE[bi]) if bi is not None else None,
                bias_key=bias_key(BE[bi]) if bi is not None else None,
                setup_key=setup_key(P, s),
                event_close_utc=str(pd.Timestamp(P['bclose'][s['t_ev']])),
                armed_utc=str(pd.Timestamp(P['bclose'][s['ta']])),
                fvg_low=float(s['lo']), fvg_high=float(s['hi']), conf_extreme=float(ext))


def bias_check(P, S, BE, si, j, consumed_keys):
    """engine_v05._bias_ok with consumed bias events keyed by their close timestamp (stable across windows)."""
    bts = np.array([e['ts'] for e in BE], dtype='datetime64[ns]') if BE else np.array([], dtype='datetime64[ns]')
    consumed = {i for i, e in enumerate(BE) if bias_key(e) in consumed_keys}
    s = S[si]
    return V._bias_ok(BE, bts, P['bclose'][j], s['d'], P['bclose'][s['t_ev']], consumed)


def decide(P, S, BE, j, st, busy, quote):
    """Trade selection at the close of bar j -- same checks, same order as engine_v05.run_config.
    st    : dict(used=set(setup keys), consumed=set(bias keys), nt=int, nl=int) for the UTC day of bar j
    busy  : True if a position is already open
    quote : quote(state, d) -> (fill_price, spread) or (None, reason); fill = ask for long, bid for short
    Returns (chosen, log); chosen = dict(si, kind, ext, bi, sl, tp, risk, fill, spr)."""
    log, chosen = [], None
    for si, kind, ext in candidates(P, S, j):
        key = setup_key(P, S[si])
        if key in st['used']:
            log.append((si, kind, ext, None, 'setup already traded'))
            continue
        if busy:
            log.append((si, kind, ext, None, 'busy: position open'))
            continue
        if st['nt'] >= DAILY_MAX_TRADES or st['nl'] >= DAILY_MAX_LOSSES:
            log.append((si, kind, ext, None, f"daily stop trades={st['nt']} losses={st['nl']}"))
            continue
        bi = bias_check(P, S, BE, si, j, st['consumed'])
        if bi is None:
            log.append((si, kind, ext, None, 'no matching unconsumed H1 bias'))
            continue
        d = S[si]['d']
        fill, spr = quote(st, d)
        if fill is None:
            log.append((si, kind, ext, bi, f'no fill: {spr}'))
            continue
        lv, why = levels(S[si], d, fill, spr, P['c'][j])
        if lv is None:
            log.append((si, kind, ext, bi, why))
            continue
        sl, tp, risk = lv
        log.append((si, kind, ext, bi, 'ok'))
        chosen = dict(si=si, kind=kind, ext=ext, bi=bi, sl=sl, tp=tp, risk=risk, fill=float(fill),
                      spr=float(spr), key=key)
        break
    return chosen, log


# ---------------- exits ----------------
def simulate(M, ts, d, fill, sl, tp):
    """SL is checked before TP inside a bar; a bar opening beyond a level fills at the open."""
    i = np.searchsorted(M.ts, ts)
    n = len(M.ts)
    if i >= n:
        return None
    xo, xh, xl = (M.bo, M.bh, M.bl) if d == 1 else (M.ao, M.ah, M.al)
    j, lo, W = None, i, 512
    while lo < n:
        hi = min(n, lo + W)
        hs, ht = (xl[lo:hi] <= sl, xh[lo:hi] >= tp) if d == 1 else (xh[lo:hi] >= sl, xl[lo:hi] <= tp)
        hit = np.flatnonzero(hs | ht)
        if len(hit):
            j, isl = lo + hit[0], bool(hs[hit[0]])
            break
        lo, W = hi, W * 4
    if j is None:
        return None
    if isl:
        ex = (min(sl, xo[j]) if d == 1 else max(sl, xo[j])) if j > i else sl
        how = 'SL'
    else:
        ex = (max(tp, xo[j]) if d == 1 else min(tp, xo[j])) if j > i else tp
        how = 'TP'
    return dict(fill_ts=M.ts[i], exit_ts=M.ts[j], fill=fill, exit=ex, how=how,
                R=(ex - fill) * d / abs(fill - sl))


def day_key(ts):
    return str(pd.Timestamp(ts).tz_localize('UTC') if pd.Timestamp(ts).tzinfo is None else
               pd.Timestamp(ts).tz_convert('UTC'))[:10]
