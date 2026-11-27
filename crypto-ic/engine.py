"""Causal SMC sweep->MSS->FVG v0.2 backtester. See PREREG.md."""
import glob, os, bisect
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

NY = ZoneInfo('America/New_York')
HERE = os.path.dirname(os.path.abspath(__file__))
COMM = {'xauusd': 0.07, 'eurusd': 0.00007}   # $7/lot RT in price units
DEV_END = pd.Timestamp('2025-01-01', tz='UTC')
WINDOWS = [(2 * 60, 5 * 60), (7 * 60, 11 * 60)]  # NY minutes-of-day
FLAT_MIN = 12 * 60


# ---------------- data ----------------
def load_m1(inst):
    cache = f'{HERE}/data/{inst}_m1.pkl'
    if os.path.exists(cache):
        return pd.read_pickle(cache)
    out = {}
    for p in ('bid', 'ask'):
        fs = sorted(glob.glob(f'{HERE}/data/{inst}_{p}_*.csv'))
        d = pd.concat([pd.read_csv(f) for f in fs]).drop_duplicates('timestamp').set_index('timestamp').sort_index()
        out[p] = d
    df = out['bid'].join(out['ask'], rsuffix='_a', how='inner')
    df.columns = ['o', 'h', 'l', 'c', 'oa', 'ha', 'la', 'ca']
    stale = (df.o == df.h) & (df.h == df.l) & (df.l == df.c) & (df.c == df.c.shift()) & (df.ca == df.ca.shift())
    df = df[~stale]
    df.index = pd.to_datetime(df.index, unit='ms', utc=True)
    df.to_pickle(cache)
    return df


def resample(m1, minutes):
    g = m1.resample(f'{minutes}min', label='left', closed='left')
    b = pd.DataFrame({'o': g.o.first(), 'h': g.h.max(), 'l': g.l.min(), 'c': g.c.last(),
                      'spr': (m1.ca - m1.c).resample(f'{minutes}min').median()}).dropna(subset=['o'])
    return b


def atr(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    a = np.empty_like(tr); a[:n] = np.nan; a[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        a[i] = (a[i - 1] * (n - 1) + tr[i]) / n
    return a


def pivots(h, l, N):
    """pivot at p confirmed (usable) at bar p+N. Returns lists of (confirm_idx, p, price)."""
    n = len(h); ph, pl = [], []
    for p in range(N, n - N):
        if h[p] > h[p - N:p].max() and h[p] >= h[p + 1:p + N + 1].max():
            ph.append((p + N, p, h[p]))
        if l[p] < l[p - N:p].min() and l[p] <= l[p + 1:p + N + 1].min():
            pl.append((p + N, p, l[p]))
    return ph, pl


def structure(c, ph, pl, n):
    """close-based BOS/CHoCH. returns trend after each bar, break_up[t] level or nan, break_dn."""
    trend = np.zeros(n, int); bu = np.full(n, np.nan); bd = np.full(n, np.nan)
    H = L = None; tr = 0; ih = il = 0
    for t in range(n):
        while ih < len(ph) and ph[ih][0] <= t: H = [ph[ih][2], False]; ih += 1
        while il < len(pl) and pl[il][0] <= t: L = [pl[il][2], False]; il += 1
        if H and not H[1] and c[t] > H[0]:
            H[1] = True; tr = 1; bu[t] = H[0]
        if L and not L[1] and c[t] < L[0]:
            L[1] = True; tr = -1; bd[t] = L[0]
        trend[t] = tr
    return trend, bu, bd


# ---------------- setup detection (config-independent except N) ----------------
def prepare(m1, tf):
    """everything derived only from bars; truncation = caller's job"""
    b = resample(m1, tf)
    h1 = resample(m1, 60)
    o, h, l, c = (b[k].values for k in 'ohlc')
    ny = b.index.tz_convert(NY)
    nyc = (b.index + pd.Timedelta(minutes=tf)).tz_convert(NY)
    tod = ny.hour * 60 + ny.minute
    day = ny.normalize().tz_localize(None).values.astype('datetime64[D]')
    # H1 bias as of each entry bar close
    h1ph, h1pl = pivots(h1.h.values, h1.l.values, 5)
    h1trend, _, _ = structure(h1.c.values, h1ph, h1pl, len(h1))
    h1close = (h1.index + pd.Timedelta(hours=1)).values
    bclose = (b.index + pd.Timedelta(minutes=tf)).values
    k = np.searchsorted(h1close, bclose, side='right') - 1
    bias = np.where(k >= 0, h1trend[np.clip(k, 0, None)], 0)
    # H1 pivot highs/lows with availability time
    h1_ph_t = [(h1close[ci], pr) for ci, p, pr in h1ph]
    h1_pl_t = [(h1close[ci], pr) for ci, p, pr in h1pl]
    # daily levels (previous NY date present) and Asian (20:00-24:00 prior evening)
    dfd = pd.DataFrame({'d': day, 'h': h, 'l': l, 'tod': tod})
    dd = dfd.groupby('d').agg(h=('h', 'max'), l=('l', 'min'))
    prev = dd.shift(1)
    pdh = prev.h.reindex(day).values; pdl = prev.l.reindex(day).values
    asi = dfd[dfd.tod >= 20 * 60].groupby('d').agg(h=('h', 'max'), l=('l', 'min'))
    asi.index = asi.index + np.timedelta64(1, 'D')   # belongs to next NY day
    ash = asi.h.reindex(day).values; asl = asi.l.reindex(day).values
    a14 = atr(h, l, c)
    return dict(b=b, o=o, h=h, l=l, c=c, tod=tod, day=day, bias=bias, pdh=pdh, pdl=pdl, ash=ash, asl=asl,
                atr=a14, spr=b.spr.values, h1_ph_t=h1_ph_t, h1_pl_t=h1_pl_t, bclose=bclose, tf=tf,
                nyclose=nyc)


def in_window(tod, tf):
    return any(s <= x and x + tf <= e for s, e in WINDOWS for x in [tod])


def in_arm_zone(tod, tf):  # window or 2h before
    return any(s - 120 <= tod and tod + tf <= e for s, e in WINDOWS)


def setups(P, N):
    """Return list of candidate setups (dicts), each decided at close of bar t. Fully causal."""
    o, h, l, c, tf = P['o'], P['h'], P['l'], P['c'], P['tf']
    n = len(c); A = P['atr']
    ph, pl = pivots(h, l, N)
    _, bu, bd = structure(c, ph, pl, n)
    # FVGs known at c-bar close
    fu = np.zeros(n, bool); fd = np.zeros(n, bool)
    fu[2:] = (l[2:] > h[:-2]) & ((l[2:] - h[:-2]) >= 0.1 * A[2:])
    fd[2:] = (h[2:] < l[:-2]) & ((l[:-2] - h[2:]) >= 0.1 * A[2:])
    # liquidity level lists (sorted) for TP: internal pivots + H1 pivots, added when confirmed
    hi_lv, lo_lv = [], []
    h1h, h1l = P['h1_ph_t'], P['h1_pl_t']; j1h = j1l = 0
    iph = ipl = 0; last_pl = last_ph = None
    armL = armS = None  # (bar, level-swept extreme)
    out = []
    for t in range(n):
        while iph < len(ph) and ph[iph][0] <= t: last_ph = ph[iph][2]; bisect.insort(hi_lv, ph[iph][2]); iph += 1
        while ipl < len(pl) and pl[ipl][0] <= t: last_pl = pl[ipl][2]; bisect.insort(lo_lv, pl[ipl][2]); ipl += 1
        while j1h < len(h1h) and h1h[j1h][0] <= P['bclose'][t]: bisect.insort(hi_lv, h1h[j1h][1]); j1h += 1
        while j1l < len(h1l) and h1l[j1l][0] <= P['bclose'][t]: bisect.insort(lo_lv, h1l[j1l][1]); j1l += 1
        if np.isnan(A[t]): continue
        tod = P['tod'][t]
        if in_arm_zone(tod, tf):
            Ls = [x for x in (P['asl'][t], P['pdl'][t], last_pl) if x is not None and not np.isnan(x)]
            sw = [x for x in Ls if l[t] < x < c[t]]
            if sw: armL = (t, l[t], max(sw))
            Hs = [x for x in (P['ash'][t], P['pdh'][t], last_ph) if x is not None and not np.isnan(x)]
            sw = [x for x in Hs if h[t] > x > c[t]]
            if sw: armS = (t, h[t], min(sw))
        if not in_window(tod, tf): continue
        b = P['bias'][t]
        for d, arm, brk, fv in ((1, armL, bu, fu), (-1, armS, bd, fd)):
            if b != d or arm is None or np.isnan(brk[t]): continue
            sb, sx, slv = arm
            if not (0 < t - sb <= 12): continue
            # most recent FVG with c in [sb, t]
            cs = [k for k in range(t, sb - 1, -1) if k >= 2 and fv[k]]
            if not cs: continue  # no POI (displacement via body alone cannot give a POI)
            k = cs[0]
            if d == 1:
                top, bot, aext = l[k], h[k - 2], l[k - 2]
                ext = h[sb:t + 1].max()
                tps = hi_lv[bisect.bisect_right(hi_lv, ext):]
                cand = ([tps[0]] if tps else []) + ([P['pdh'][t]] if P['pdh'][t] > ext else [])
            else:
                top, bot, aext = l[k - 2], h[k], h[k - 2]
                ext = l[sb:t + 1].min()
                tps = lo_lv[:bisect.bisect_left(lo_lv, ext)]
                cand = ([tps[-1]] if tps else []) + ([P['pdl'][t]] if P['pdl'][t] < ext else [])
            if not cand: continue
            tp = min(cand) if d == 1 else max(cand)
            out.append(dict(t=t, d=d, sweep_bar=sb, sweep_x=sx, sweep_level=slv, mss_level=brk[t], fvg_c=k,
                            fvg_top=top, fvg_bot=bot, a_ext=aext, ext=ext, tp=tp, atr=A[t], spr=P['spr'][t]))
            if d == 1: armL = None
            else: armS = None
    return out


# ---------------- execution on M1 ----------------
class M1:
    def __init__(self, m1):
        self.ts = m1.index.values
        self.bo, self.bh, self.bl, self.bc = (m1[k].values for k in ('o', 'h', 'l', 'c'))
        self.ao, self.ah, self.al, self.ac = (m1[k].values for k in ('oa', 'ha', 'la', 'ca'))
        ny = m1.index.tz_convert(NY)
        self.tod = (ny.hour * 60 + ny.minute).values
        self.day = ny.normalize().tz_localize(None).values.astype('datetime64[D]')


def window_end(tod, tf):
    for s, e in WINDOWS:
        if s <= tod < e: return e
    return tod + tf


def simulate(M, start_ts, d, entry, sl, tp, expiry_ts, cancel_ts, mode, comm, market=False):
    """mode: gross|std|raw. returns dict or None (not filled)."""
    i = np.searchsorted(M.ts, start_ts)
    if i >= len(M.ts): return None
    day0 = M.day[i]
    cost = mode != 'gross'
    # entry side prices
    if d == 1:
        eo, el = (M.ao, M.al) if cost else (M.bo, M.bl)
        xo, xh, xl = M.bo, M.bh, M.bl            # long exits at bid
    else:
        eo, el = (M.bo, M.bh) if True else None  # short entry at bid
        xo, xh, xl = (M.ao, M.ah, M.al) if cost else (M.bo, M.bh, M.bl)
    fill = None; j = i; n = len(M.ts)
    lim = min(expiry_ts, cancel_ts)
    if market:
        fill = eo[i]; j = i
    else:
        while j < n and M.ts[j] < lim and M.day[j] == day0 and M.tod[j] < FLAT_MIN:
            # TP traded before fill -> cancel
            if d == 1:
                if el[j] <= entry: fill = min(entry, eo[j]); break
                if M.bh[j] >= tp: return None
            else:
                if el[j] >= entry: fill = max(entry, eo[j]); break
                if M.bl[j] <= tp: return None
            j += 1
        if fill is None: return None
    fill_j = j; risk = abs(entry - sl) if not market else abs(fill - sl)
    while j < n:
        if M.day[j] != day0 or M.tod[j] >= FLAT_MIN:
            ex = xo[j]; how = 'flat'; break
        if d == 1:
            if xl[j] <= sl: ex = min(sl, xo[j]) if j > fill_j else sl; how = 'SL'; break
            if xh[j] >= tp: ex = max(tp, xo[j]) if j > fill_j else tp; how = 'TP'; break
        else:
            if xh[j] >= sl: ex = max(sl, xo[j]) if j > fill_j else sl; how = 'SL'; break
            if xl[j] <= tp: ex = min(tp, xo[j]) if j > fill_j else tp; how = 'TP'; break
        j += 1
    else:
        return None
    pnl = (ex - fill) * d - (comm if mode == 'raw' else 0.0)
    return dict(fill_ts=M.ts[fill_j], exit_ts=M.ts[j], fill=fill, exit=ex, how=how, R=pnl / risk)


def run_config(P, S, M, cfg, mode, comm, start=None, explore=False, funnel=None):
    """cfg: (N, entry 'ce'|'edge', sl 'sweep'|'fvg', minRR). S = setups(P, N). start: only signals >= start."""
    _, etype, sltype, minrr = cfg
    tf = P['tf']; busy_until = np.datetime64('1970-01-01'); trades = []
    daycnt = {}
    for s in S:
        t = s['t']; tclose = P['bclose'][t]
        if start is not None and tclose < start: continue
        if tclose < busy_until: continue
        dkey = P['day'][t]; nt, nl = daycnt.get(dkey, (0, 0))
        if nt >= 3 or nl >= 2: continue
        d = s['d']
        entry = (s['fvg_top'] + s['fvg_bot']) / 2 if etype == 'ce' else (s['fvg_top'] if d == 1 else s['fvg_bot'])
        f = funnel if funnel is not None else {}
        f['setup'] = f.get('setup', 0) + 1
        if not explore and d * (entry - (s['sweep_x'] + s['ext']) / 2) >= 0: f['premium'] = f.get('premium', 0) + 1; continue
        base = s['sweep_x'] if sltype == 'sweep' else s['a_ext']
        sl = base - d * (0.1 * s['atr'] + s['spr'])
        risk = d * (entry - sl)
        tp = s['tp'] if not explore else entry + d * minrr * risk
        rew = d * (tp - entry)
        if risk <= 0 or rew / risk < minrr - 1e-9: f['rr'] = f.get('rr', 0) + 1; continue
        if risk < 5 * (s['spr'] + comm): f['cost'] = f.get('cost', 0) + 1; continue
        expiry = P['bclose'][min(t + 10, len(P['bclose']) - 1)]
        we = window_end(P['tod'][t], tf)
        cancel = tclose + np.timedelta64(int(we - P['tod'][t] - tf), 'm')
        r = simulate(M, tclose, d, entry, sl, tp, expiry, cancel, mode, comm)
        if r is None:
            f['nofill'] = f.get('nofill', 0) + 1
            busy_until = min(expiry, cancel); continue
        busy_until = r['exit_ts']
        nt += 1; nl += r['R'] < 0; daycnt[dkey] = (nt, nl)
        f['filled'] = f.get('filled', 0) + 1
        trades.append(dict(signal_ts=tclose, day=dkey, d=d, entry=entry, sl=sl, tp=tp, rr=rew / risk,
                           sl_atr=risk / s['atr'], sweep_level=s['sweep_level'], sweep_x=s['sweep_x'],
                           mss_level=s['mss_level'], fvg_top=s['fvg_top'], fvg_bot=s['fvg_bot'], **r))
    return pd.DataFrame(trades)
