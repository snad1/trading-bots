"""v0.5 'Full mentor model' (XAUUSD): H1 key-zone + volume-candle bias -> M3/M5/M15 structure
(OBR, OBC, BB) -> tap -> confirmation (engulfing / star; wick rejection secondary) -> market entry at
confirmation close, SL beyond OB body or confirmation, TP 3R. See PREREG_v05.md.
Reuses engine.py (resample, pivots, ATR, sessions, M1 fill/exit simulator, costs) and engine_v04.py ideas.
Every decision uses closed bars only."""
import glob, os
import numpy as np, pandas as pd
import engine as E

HERE = E.HERE
SLQ_WAIT = 20     # entry bars after the structure event in which SLQ must form (as v0.4)
LIFE = 30         # entry bars after arming that a zone stays live
CONF_WIN = 6      # confirmation bar must close within 6 entry bars of the latest tap (tap bar = 0)
BB_WAIT = 60      # entry bars after arming in which a close through the far edge creates a breaker
BIAS_LIFE = 24    # H1 bars
KZ_DAYS = 20      # trading days of H1 key zones
TOL_ATR = 0.25
MED_N = 50
OB_BACK = 10
BUF_MIN = 0.30
GATE_X = 6.0
RR = 3.0


# ---------------- data ----------------
def load_years(years, inst='xauusd', tag=None):
    """Yearly bid/ask CSVs -> cleaned M1 frame (same cleaning as engine_v04.load_all), own cache."""
    tag = tag or f'{years[0]}_{str(years[-1])[-2:]}'
    cache = f'{HERE}/data/{inst}_m1_{tag}.pkl'
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    out = {}
    for p in ('bid', 'ask'):
        fs = [f'{HERE}/data/{inst}_{p}_{y}.csv' for y in years]
        out[p] = pd.concat([pd.read_csv(f) for f in fs]).drop_duplicates('timestamp').set_index('timestamp').sort_index()
    df = out['bid'].join(out['ask'], rsuffix='_a', how='inner')
    df.columns = ['o', 'h', 'l', 'c', 'oa', 'ha', 'la', 'ca']
    stale = (df.o == df.h) & (df.h == df.l) & (df.l == df.c) & (df.c == df.c.shift()) & (df.ca == df.ca.shift())
    df = df[~stale]
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    df.to_pickle(cache)
    return df


def load_val():
    return load_years(list(range(2010, 2015)), tag='2010_14')


def load_main():
    import engine_v04 as V
    return V.load_all()            # 2015..2026 cache built for v0.4


# ---------------- H1 bias ----------------
def bias_events(m1, K):
    """Bias events decided at the close of H1 bar t. Key zones = fractal-2 H1 pivots confirmed by the close of
    t-1 whose pivot bar lies in the last KZ_DAYS NY trading days (dates with data). Volume candle = body covers
    previous body and body >= K x median(body of the previous 50 H1 bars). Touch: range within 0.25 x ATR14(H1)
    of a key zone. Bias = candle direction (a close-through is necessarily in the candle direction)."""
    b = E.resample(m1, 60)
    o, h, l, c = (b[k].values for k in 'ohlc'); n = len(c)
    cts = (b.index + pd.Timedelta(hours=1)).values
    nyd = b.index.tz_convert(E.NY).normalize()
    drank = pd.factorize(nyd)[0]            # data sorted -> ascending rank of NY dates with data
    ph, pl = E.pivots(h, l, 2)
    piv = sorted([(ci, p, x, 'H') for ci, p, x in ph] + [(ci, p, x, 'L') for ci, p, x in pl])
    body = np.abs(c - o); top = np.maximum(o, c); bot = np.minimum(o, c)
    med = pd.Series(body).rolling(MED_N).median().shift(1).values
    a = E.atr(h, l, c)
    act = []; ip = 0; ev = []
    for t in range(1, n):
        while ip < len(piv) and piv[ip][0] <= t - 1:
            _, p, x, kd = piv[ip]; act.append((p, x, kd)); ip += 1
        act = [z for z in act if drank[z[0]] >= drank[t] - (KZ_DAYS - 1)]
        if np.isnan(med[t]) or np.isnan(a[t]) or body[t] <= 0: continue
        if not (top[t] >= top[t - 1] and bot[t] <= bot[t - 1] and body[t] >= K * med[t]): continue
        tol = TOL_ATR * a[t]
        hit = [z for z in act if l[t] - tol <= z[1] <= h[t] + tol]
        if not hit: continue
        d = 1 if c[t] > o[t] else -1
        brk = [z for z in hit if (o[t] < z[1] < c[t] if d == 1 else c[t] < z[1] < o[t])]
        if brk:
            z = min(brk, key=lambda z: abs(c[t] - z[1])); kind = 'break'
        else:
            ref = l[t] if d == 1 else h[t]
            z = min(hit, key=lambda z: abs(ref - z[1])); kind = 'touch'
        exp = cts[t + BIAS_LIFE] if t + BIAS_LIFE < n else cts[t] + np.timedelta64(BIAS_LIFE, 'h')
        ev.append(dict(t=t, ts=cts[t], open_ts=b.index.values[t], d=d, kind=kind, zone=float(z[1]), zkind=z[2],
                       zone_ts=b.index.values[z[0]], exp=exp, body_x=float(body[t] / med[t])))
    return ev


# ---------------- entry-TF structure (same state machine as engine_v04.structure + BOS events) ----------------
def structure(o, h, l, c, N):
    n = len(c)
    ph, pl = E.pivots(h, l, N)
    trend = np.zeros(n, np.int8)
    up = dn = None; prot = None
    tr = 0; anchor = 0; nbos = 0; last_bos = np.nan
    ih = il = 0; mss = []; bos = []
    bu = lambda t, x: c[t] > x
    bd = lambda t, x: c[t] < x
    for t in range(n):
        while ih < len(ph) and ph[ih][0] <= t:
            _, p, x = ph[ih]; ih += 1
            if tr == 0: up = (x, p)
            elif tr == 1 and p >= anchor and (up is None or x > up[0]): up = (x, p)
        while il < len(pl) and pl[il][0] <= t:
            _, p, x = pl[il]; il += 1
            if tr == 0: dn = (x, p)
            elif tr == -1 and p >= anchor and (dn is None or x < dn[0]): dn = (x, p)
        if tr == 1:
            if prot is not None and bd(t, prot[0]):
                s = prot[1]; e = s + int(np.argmax(h[s:t + 1]))
                if nbos >= 1: mss.append(dict(t=t, d=-1, mss=prot[0], ext_bar=e, bos=last_bos))
                tr = -1; prot = (h[e], e); dn = None; up = None; anchor = t; nbos = 0
            elif up is not None and bu(t, up[0]):
                s = up[1] + 1; k = s + int(np.argmin(l[s:t + 1]))
                prot = (l[k], k); last_bos = up[0]; bos.append(dict(t=t, d=1, bos=up[0], ext_bar=k, hl=l[k]))
                up = None; anchor = t; nbos += 1
        elif tr == -1:
            if prot is not None and bu(t, prot[0]):
                s = prot[1]; e = s + int(np.argmin(l[s:t + 1]))
                if nbos >= 1: mss.append(dict(t=t, d=1, mss=prot[0], ext_bar=e, bos=last_bos))
                tr = 1; prot = (l[e], e); up = None; dn = None; anchor = t; nbos = 0
            elif dn is not None and bd(t, dn[0]):
                s = dn[1] + 1; k = s + int(np.argmax(h[s:t + 1]))
                prot = (h[k], k); last_bos = dn[0]; bos.append(dict(t=t, d=-1, bos=dn[0], ext_bar=k, hl=h[k]))
                dn = None; anchor = t; nbos += 1
        else:
            if up is not None and bu(t, up[0]):
                s = up[1] + 1; k = s + int(np.argmin(l[s:t + 1]))
                tr = 1; prot = (l[k], k); last_bos = up[0]; bos.append(dict(t=t, d=1, bos=up[0], ext_bar=k, hl=l[k]))
                up = None; dn = None; anchor = t; nbos = 1
            elif dn is not None and bd(t, dn[0]):
                s = dn[1] + 1; k = s + int(np.argmax(h[s:t + 1]))
                tr = -1; prot = (h[k], k); last_bos = dn[0]; bos.append(dict(t=t, d=-1, bos=dn[0], ext_bar=k, hl=h[k]))
                dn = None; up = None; anchor = t; nbos = 1
        trend[t] = tr
    return trend, mss, bos


def find_zone(o, c, e, d, cref):
    """Last opposite-colour candle searching back from bar e (incl., <= OB_BACK bars); in a run of consecutive
    opposite candles take the body closest to price. d = trade direction. Zone must lie on the far side of cref."""
    want = (lambda k: c[k] < o[k]) if d == 1 else (lambda k: c[k] > o[k])
    k0 = None
    for k in range(e, max(e - OB_BACK - 1, -1), -1):
        if want(k): k0 = k; break
    if k0 is None: return None
    run = [k0]; k = k0 - 1
    while k >= 0 and k > e - OB_BACK - 1 and want(k): run.append(k); k -= 1
    if d == 1:
        k = max(run, key=lambda j: max(o[j], c[j])); lo, hi = min(o[k], c[k]), max(o[k], c[k])
        if hi >= cref: return None
    else:
        k = min(run, key=lambda j: min(o[j], c[j])); lo, hi = min(o[k], c[k]), max(o[k], c[k])
        if lo <= cref: return None
    return k, lo, hi


def arm_slq(h, l, t, d, near, n):
    """SLQ = 3-bar failed pullback toward the zone that stays short of the near edge (as v0.4). Returns
    (arming bar, SLQ price) or None. Tapped before any SLQ -> None."""
    touch = (lambda j: l[j] <= near) if d == 1 else (lambda j: h[j] >= near)
    for j in range(t + 1, min(t + 1 + SLQ_WAIT, n)):
        if touch(j): return None
        k = j - 1
        if k >= t + 1:
            if d == -1 and h[k] > h[k - 1] and h[k] >= h[j]: return j, h[k]
            if d == 1 and l[k] < l[k - 1] and l[k] <= l[j]: return j, l[k]
    return None


def patterns(P, j, d, lo, hi):
    """Confirmation patterns closing at bar j for direction d. Returns list of (kind, extreme)."""
    o, h, l, c, med = P['o'], P['h'], P['l'], P['c'], P['med']
    out = []
    if j < 2: return out
    b = abs(c[j] - o[j])
    if d == 1:
        if c[j] > o[j] and c[j - 1] < o[j - 1] and o[j] <= c[j - 1] and c[j] >= o[j - 1]:
            out.append(('engulf', min(l[j - 1], l[j])))
        if (c[j - 2] < o[j - 2] and c[j] > o[j] and not np.isnan(med[j]) and b >= med[j]
                and abs(c[j - 1] - o[j - 1]) <= abs(c[j - 2] - o[j - 2]) and c[j] > (o[j - 2] + c[j - 2]) / 2):
            out.append(('star', min(l[j - 2:j + 1])))
        if (min(o[j], c[j]) - l[j]) >= 2 * b and l[j] <= hi and c[j] > hi:
            out.append(('wick', l[j]))
    else:
        if c[j] < o[j] and c[j - 1] > o[j - 1] and o[j] >= c[j - 1] and c[j] <= o[j - 1]:
            out.append(('engulf', max(h[j - 1], h[j])))
        if (c[j - 2] > o[j - 2] and c[j] < o[j] and not np.isnan(med[j]) and b >= med[j]
                and abs(c[j - 1] - o[j - 1]) <= abs(c[j - 2] - o[j - 2]) and c[j] < (o[j - 2] + c[j - 2]) / 2):
            out.append(('star', max(h[j - 2:j + 1])))
        if (h[j] - max(o[j], c[j])) >= 2 * b and h[j] >= lo and c[j] < lo:
            out.append(('wick', h[j]))
    return out


def scan(P, s):
    """Walk forward from arming: taps, confirmations (in session windows, within CONF_WIN of the latest tap),
    invalidation (close beyond far edge), life end. Adds s['confs'] = [(bar, kind, extreme)], s['inval']."""
    h, l, c, n, tf = P['h'], P['l'], P['c'], len(P['c']), P['tf']
    d, lo, hi, ta = s['d'], s['lo'], s['hi'], s['ta']
    touch = (lambda j: l[j] <= hi) if d == 1 else (lambda j: h[j] >= lo)
    beyond = (lambda j: c[j] < lo) if d == 1 else (lambda j: c[j] > hi)
    last_tap = None; confs = []; inval = None
    for j in range(ta + 1, min(ta + 1 + BB_WAIT, n)):
        if beyond(j): inval = j; break
        if j > ta + LIFE: continue
        if touch(j): last_tap = j
        if last_tap is not None and j - last_tap <= CONF_WIN and E.in_window(P['tod'][j], tf):
            for kind, ext in patterns(P, j, d, lo, hi):
                confs.append((j, kind, ext))
    s['confs'] = confs; s['inval'] = inval
    return s


def prepare(m1, tf, N=2):
    b = E.resample(m1, tf)
    o, h, l, c = (b[k].values for k in 'ohlc')
    ny = b.index.tz_convert(E.NY)
    body = np.abs(c - o)
    P = dict(o=o, h=h, l=l, c=c, tf=tf, N=N, idx=b.index, tod=(ny.hour * 60 + ny.minute).values,
             day=ny.normalize().tz_localize(None).values.astype('datetime64[D]'),
             bclose=(b.index + pd.Timedelta(minutes=tf)).values, atr=E.atr(h, l, c), spr=b.spr.values,
             med=pd.Series(body).rolling(MED_N).median().shift(1).values)
    P['trend'], P['mss'], P['bos'] = structure(o, h, l, c, N)
    return P


def setups(P):
    """All OBR / OBC / BB setups with arming + confirmations. Causal: each item is decided at the close of the
    bar it is attached to (zone at event bar, arming at ta, confirmations at their bar)."""
    o, h, l, c, n = P['o'], P['h'], P['l'], P['c'], len(P['c'])
    S = []
    raw = [('OBR', ev, ev['ext_bar']) for ev in P['mss']] + [('OBC', ev, ev['ext_bar']) for ev in P['bos']]
    for typ, ev, e in raw:
        t, d = ev['t'], ev['d']
        z = find_zone(o, c, e, d, c[t])
        if z is None: continue
        k, lo, hi = z
        near = hi if d == 1 else lo
        a = arm_slq(h, l, t, d, near, n)
        if a is None: continue
        s = dict(type=typ, d=d, lo=lo, hi=hi, ob_bar=k, t_ev=t, ta=a[0], slq=a[1],
                 bos=ev.get('bos', np.nan), mss=ev.get('mss', np.nan), hl=ev.get('hl', np.nan))
        S.append(scan(P, s))
    # breakers: an armed OBR/OBC whose far edge is closed through -> opposite-direction zone, new SLQ, retest
    for s0 in list(S):
        b = s0['inval']
        if b is None: continue
        d = -s0['d']; lo, hi = s0['lo'], s0['hi']
        near = hi if d == 1 else lo
        a = arm_slq(h, l, b, d, near, n)
        if a is None: continue
        s = dict(type='BB', d=d, lo=lo, hi=hi, ob_bar=s0['ob_bar'], t_ev=b, ta=a[0], slq=a[1], src=s0['type'],
                 bos=s0['bos'], mss=s0['mss'], hl=s0['hl'])
        S.append(scan(P, s))
    return S


# ---------------- execution ----------------
def _bias_ok(BE, bts, ts, d, t_struct, consumed):
    """Current bias at ts must be direction d, unexpired, unconsumed; the structure event must close at or after
    the start of the current unconsumed same-direction bias streak (volume candle comes first)."""
    i = np.searchsorted(bts, ts, side='right') - 1
    if i < 0: return None
    e = BE[i]
    if e['d'] != d or ts >= e['exp'] or i in consumed: return None
    j = i
    while j > 0 and BE[j - 1]['d'] == d and (j - 1) not in consumed and BE[j - 1]['exp'] >= BE[j]['ts']:
        j -= 1
    if t_struct < BE[j]['ts']: return None
    return i


def run_config(P, S, M, BE, slmode, mode, comm, start=None, end=None, types=('OBR', 'OBC', 'BB'),
               confs=('engulf', 'star'), use_bias=True, gate=True):
    bc, c, tf = P['bclose'], P['c'], P['tf']
    bts = np.array([e['ts'] for e in BE], dtype='datetime64[ns]') if BE else np.array([], dtype='datetime64[ns]')
    evs = []
    for si, s in enumerate(S):
        if s['type'] not in types: continue
        for j, kind, ext in s['confs']:
            if kind in confs:
                near = s['hi'] if s['d'] == 1 else s['lo']
                evs.append((j, abs(c[j] - near), -s['ta'], si, kind, ext))
    evs.sort()
    busy = np.datetime64('1970-01-01'); trades = []; daycnt = {}; used = set(); consumed = set(); skipped_gate = 0
    for j, _, _, si, kind, ext in evs:
        if si in used: continue
        s = S[si]; ts = bc[j]; d = s['d']
        if start is not None and ts < start: continue
        if end is not None and ts >= end: continue
        if ts < busy: continue
        dkey = P['day'][j]; nt, nl = daycnt.get(dkey, (0, 0))
        if nt >= 3 or nl >= 2: continue
        bi = None
        if use_bias:
            bi = _bias_ok(BE, bts, ts, d, bc[s['t_ev']], consumed)
            if bi is None: continue
        i = np.searchsorted(M.ts, ts)
        if i >= len(M.ts) or M.day[i] != dkey or M.ts[i] - ts > np.timedelta64(tf, 'm'): continue
        if M.tod[i] >= E.FLAT_MIN: continue
        buf = max(2 * P['spr'][j], BUF_MIN)
        if slmode == 'ob': sl = s['lo'] - buf if d == 1 else s['hi'] + buf
        else: sl = ext - buf if d == 1 else ext + buf
        fill_raw = M.ao[i] if d == 1 else M.bo[i]
        risk_raw = d * (fill_raw - sl)
        if risk_raw <= 0: continue
        if gate and risk_raw < GATE_X * (P['spr'][j] + comm): skipped_gate += 1; continue
        fill = (M.ao[i] if mode != 'gross' else M.bo[i]) if d == 1 else M.bo[i]
        risk = d * (fill - sl)
        if risk <= 0: continue
        tp = fill + d * RR * risk
        r = E.simulate(M, ts, d, fill, sl, tp, ts, ts, mode, comm, market=True)
        if r is None: continue
        busy = r['exit_ts']; nt += 1; nl += r['R'] < 0; daycnt[dkey] = (nt, nl); used.add(si)
        row = dict(signal_ts=ts, day=dkey, d=d, type=s['type'], conf=kind, conf_ext=ext, ev_ts=bc[s['t_ev']],
                   arm_ts=bc[s['ta']], ob_ts=P['idx'].values[s['ob_bar']], ob_lo=s['lo'], ob_hi=s['hi'], slq=s['slq'],
                   bos=s['bos'], mss=s['mss'], hl=s['hl'], entry=fill, sl=sl, tp=tp, risk=risk_raw,
                   spr=P['spr'][j], sl_atr=risk_raw / P['atr'][j] if P['atr'][j] > 0 else np.nan)
        if bi is not None:
            e = BE[bi]; consumed.add(bi)
            row.update(bias_ts=e['ts'], bias_open_ts=e['open_ts'], bias_kind=e['kind'], bias_zone=e['zone'],
                       bias_zkind=e['zkind'], bias_zone_ts=e['zone_ts'], bias_bodyx=e['body_x'])
        row.update(r); trades.append(row)
    df = pd.DataFrame(trades)
    df.attrs['skipped_gate'] = skipped_gate
    return df
