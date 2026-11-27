"""v0.6 'Mentor M15 focus'. Thin layer over engine_v05 (unchanged rules) adding:
 - bias_events(m1, K, bias_tf, life): the v0.5 H1 bias generalised to any bias TF (H1 for M15, H4 for M30).
 - CONFS: engulf + star + wick rejection (pin bar) as PRIMARY confirmations.
 - data loaders for DEV 2010-24 and VAL 2005-09.
See PREREG_v06.md."""
import numpy as np, pandas as pd
import engine as E, engine_v05 as V5
from engine_v05 import prepare, setups, run_config, load_years, patterns   # noqa: F401  (re-exported)

CONFS = ('engulf', 'star', 'wick')
BIAS_TF = {15: 60, 30: 240}        # entry TF -> bias TF (minutes)
BIAS_LIFE = {60: 24, 240: 6}       # bias TF -> life in bias-TF bars (24 h either way)


def load_dev(inst='xauusd'):
    a = V5.load_years(list(range(2010, 2015)), inst=inst, tag='2010_14')
    import engine_v04 as V4
    b = V4.load_all(inst)
    b = b[b.index < pd.Timestamp('2025-01-01', tz='UTC')]
    return pd.concat([a, b[b.index >= pd.Timestamp('2015-01-01', tz='UTC')]]).sort_index()


def load_val(inst='xauusd'):
    return V5.load_years(list(range(2005, 2010)), inst=inst, tag='2005_09')


def load_app(inst='xauusd'):
    import engine_v04 as V4
    b = V4.load_all(inst)
    return b[b.index >= pd.Timestamp('2024-12-01', tz='UTC')]


def bias_events(m1, K, bias_tf=60):
    """Identical to engine_v05.bias_events except the bias bars are resampled to bias_tf minutes and the life is
    BIAS_LIFE[bias_tf] bias bars. For bias_tf=60 this reproduces v0.5 exactly (asserted in the lookahead test)."""
    life = BIAS_LIFE[bias_tf]
    b = E.resample(m1, bias_tf)
    o, h, l, c = (b[k].values for k in 'ohlc'); n = len(c)
    cts = (b.index + pd.Timedelta(minutes=bias_tf)).values
    nyd = b.index.tz_convert(E.NY).normalize()
    drank = pd.factorize(nyd)[0]
    ph, pl = E.pivots(h, l, 2)
    piv = sorted([(ci, p, x, 'H') for ci, p, x in ph] + [(ci, p, x, 'L') for ci, p, x in pl])
    body = np.abs(c - o); top = np.maximum(o, c); bot = np.minimum(o, c)
    med = pd.Series(body).rolling(V5.MED_N).median().shift(1).values
    a = E.atr(h, l, c)
    act = []; ip = 0; ev = []
    for t in range(1, n):
        while ip < len(piv) and piv[ip][0] <= t - 1:
            _, p, x, kd = piv[ip]; act.append((p, x, kd)); ip += 1
        act = [z for z in act if drank[z[0]] >= drank[t] - (V5.KZ_DAYS - 1)]
        if np.isnan(med[t]) or np.isnan(a[t]) or body[t] <= 0: continue
        if not (top[t] >= top[t - 1] and bot[t] <= bot[t - 1] and body[t] >= K * med[t]): continue
        tol = V5.TOL_ATR * a[t]
        hit = [z for z in act if l[t] - tol <= z[1] <= h[t] + tol]
        if not hit: continue
        d = 1 if c[t] > o[t] else -1
        brk = [z for z in hit if (o[t] < z[1] < c[t] if d == 1 else c[t] < z[1] < o[t])]
        if brk:
            z = min(brk, key=lambda z: abs(c[t] - z[1])); kind = 'break'
        else:
            ref = l[t] if d == 1 else h[t]
            z = min(hit, key=lambda z: abs(ref - z[1])); kind = 'touch'
        exp = cts[t + life] if t + life < n else cts[t] + np.timedelta64(life * bias_tf, 'm')
        ev.append(dict(t=t, ts=cts[t], open_ts=b.index.values[t], d=d, kind=kind, zone=float(z[1]), zkind=z[2],
                       zone_ts=b.index.values[z[0]], exp=exp, body_x=float(body[t] / med[t])))
    return ev
