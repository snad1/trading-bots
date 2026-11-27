"""Shared v0.5 'full mentor model' decision logic for the live demo bot AND its replay test.
Detection = engine_v05.py UNCHANGED (prepare, setups, bias_events, _bias_ok) run on closed MT5 bars:
entry-TF bars and H1 bars are passed in as 'm1-like' frames; engine resample() of a native M15 / H1 bar series
at the same frequency returns the same bars, so engine_v05 sees exactly the bars it would build itself.
This file only adds: per-call bar truncation, the trade-selection step copied 1:1 from engine_v05.run_config,
and the live-only bias-expiry fix (see context()). Selected config (PREREG_v05 / RESULTS_v05): M15, K=2.0,
SL beyond OB body, TP 3R, cost gate SL >= 6x(spread+commission).
XAUUSD = the tested market. EURUSD / GBPUSD = UNTESTED EXTENSION (v0.5 was never backtested on FX).
Per-instance 'confirmations': XAUUSD_V5_M15 uses the v0.6 set engine_v06.CONFS = (engulf, star, wick) (PREREG_v06 M15
config: K=2.0, SL=OB, H1 bias == engine_v05 bias for bias_tf=60); the FX instances keep the v0.5 set (engulf, star).
Wick rejection (engine_v05.patterns): long = lower wick >= 2x body, low <= zone top, close > zone top; short mirror."""
import numpy as np, pandas as pd
import engine as E
import engine_v05 as V
import engine_v06 as V6

N_TF, N_H1 = 3000, 600           # history per evaluation (≈6 weeks M15, ≈5 weeks H1)
CONFS = ('engulf', 'star')        # v0.5 default set
TYPES = ('OBR', 'OBC', 'BB')
FAR = np.datetime64('2200-01-01T00:00:00', 'ns')

CONFIGS = {
    'XAUUSD_V5_M15': dict(symbol='XAUUSD', tf=15, magic=990515, K=2.0, sl='ob', comm=0.07, buf_min=0.30,
                          tested=True, confirmations=V6.CONFS, model='v0.5+wick',
                          label='v0.5+wick (v0.6 M15 config K2.0 SL=OB)'),
    'EURUSD_V5_M15': dict(symbol='EURUSD', tf=15, magic=991515, K=2.0, sl='ob', comm=0.00007, buf_min=0.00003,
                          tested=False, confirmations=CONFS, model='v0.5',
                          label='v0.5 M15 UNTESTED EXTENSION (FX)'),
    'GBPUSD_V5_M15': dict(symbol='GBPUSD', tf=15, magic=992515, K=2.0, sl='ob', comm=0.00007, buf_min=0.00003,
                          tested=False, confirmations=CONFS, model='v0.5',
                          label='v0.5 M15 UNTESTED EXTENSION (FX)'),
}


def as_m1(b):
    """native bars (UTC open-time index; o,h,l,c,spr) -> frame engine.resample() accepts (o,h,l,c,ca)."""
    return pd.DataFrame({'o': b.o.values, 'h': b.h.values, 'l': b.l.values, 'c': b.c.values,
                         'ca': b.c.values + b.spr.values}, index=b.index)


def context(bars, h1, cfg):
    """bars / h1: CLOSED bars only. Returns P, S, BE exactly as engine_v05 builds them, plus one live fix:
    engine bias_events() sets exp = close of H1 bar t+24, or t_close+24h when bar t+24 is not in the data yet.
    On truncated (live) data the fallback is wrong across weekends/gaps; the causal equivalent is 'not expired yet'
    (fewer than 24 H1 bars have closed since the event), so the fallback is replaced by +inf."""
    P = V.prepare(as_m1(bars), cfg['tf'])
    S = V.setups(P)
    BE = V.bias_events(as_m1(h1), cfg['K'])
    nh = len(E.resample(as_m1(h1), 60))
    for e in BE:
        if e['t'] + V.BIAS_LIFE >= nh: e['exp'] = FAR
    return P, S, BE


def setup_key(P, s):
    bc = P['bclose']
    return f"{s['type']}|{s['d']}|{pd.Timestamp(P['idx'].values[s['ob_bar']])}|{pd.Timestamp(bc[s['t_ev']])}|{pd.Timestamp(bc[s['ta']])}"


def bias_key(e):
    return str(pd.Timestamp(e['ts']))


def candidates(P, S, j, confs=CONFS):
    """Confirmations (kinds in confs) closing at bar j, sorted exactly like engine_v05.run_config (closest near edge, latest arming)."""
    c = P['c']; evs = []
    for si, s in enumerate(S):
        if s['type'] not in TYPES: continue
        for jj, kind, ext in s['confs']:
            if jj == j and kind in confs:
                near = s['hi'] if s['d'] == 1 else s['lo']
                evs.append((jj, abs(c[jj] - near), -s['ta'], si, kind, ext))
    evs.sort()
    return evs


def bias_check(P, S, BE, si, j, consumed_keys):
    """engine_v05._bias_ok with consumed bias events identified by their close timestamp (stable across windows)."""
    bts = np.array([e['ts'] for e in BE], dtype='datetime64[ns]') if BE else np.array([], dtype='datetime64[ns]')
    consumed = {i for i, e in enumerate(BE) if bias_key(e) in consumed_keys}
    s = S[si]
    return V._bias_ok(BE, bts, P['bclose'][j], s['d'], P['bclose'][s['t_ev']], consumed)


def bias_reason(e):
    if e is None: return None
    return (f"H1 {'bull' if e['d'] == 1 else 'bear'} volume candle {pd.Timestamp(e['open_ts']).strftime('%m-%d %H:%M')}Z "
            f"({e['body_x']:.1f}x median body) {e['kind']} of key zone {e['zkind']} {e['zone']:.5g}")


def order_levels(s, d, fill_raw, spr, cfg):
    """engine_v05.run_config SL/TP/cost gate for slmode 'ob'. Returns (sl, tp, risk) or (None, reason)."""
    buf = max(2 * spr, cfg['buf_min'])
    sl = s['lo'] - buf if d == 1 else s['hi'] + buf
    risk = d * (fill_raw - sl)
    if risk <= 0: return None, f'risk<=0 (price already beyond SL {sl:.5g})'
    if risk < V.GATE_X * (spr + cfg['comm']):
        return None, f'cost gate: SL dist {risk:.5g} < {V.GATE_X:g}x(spread {spr:.5g}+comm {cfg["comm"]:g})'
    return (sl, fill_raw + d * V.RR * risk, risk), None


def describe(P, S, BE, si, kind, ext, bi, model='v0.5'):
    s = S[si]; d = s['d']
    mss = s['mss'] if np.isfinite(s['mss']) else s['hl']
    return dict(side='long' if d == 1 else 'short', sweep_level=s['bos'], choch_level=mss,
                fvg_low=s['lo'], fvg_high=s['hi'], entry_type=s['type'], confirmation=kind, conf_extreme=ext,
                slq=s['slq'], bias_reason=bias_reason(BE[bi]) if bi is not None else None,
                bias_key=bias_key(BE[bi]) if bi is not None else None, setup_key=setup_key(P, s),
                event_close_utc=str(pd.Timestamp(P['bclose'][s['t_ev']])), armed_utc=str(pd.Timestamp(P['bclose'][s['ta']])),
                model=model)


def decide(P, S, BE, j, st, busy, quote, cfg):
    """Trade selection at the close of bar j, same checks and order as engine_v05.run_config.
    st: dict(used=set(setup keys), consumed=set(bias keys), nt=int, nl=int) for the NY day of bar j.
    quote(d) -> (fill_raw, spread) or (None, reason): backtest = next M1 open ask/bid + bar spread; live = tick.
    Returns (chosen or None, log); chosen = dict(si, kind, ext, bi, sl, tp, risk, fill_raw, spr);
    log = [(si, kind, ext, bi, reason)] for every candidate looked at."""
    log = []
    for jj, _, _, si, kind, ext in candidates(P, S, j, cfg.get('confirmations', CONFS)):
        if setup_key(P, S[si]) in st['used']: continue
        if busy: log.append((si, kind, ext, None, 'busy: position open')); continue
        if st['nt'] >= 3 or st['nl'] >= 2: log.append((si, kind, ext, None, f"daily stop trades={st['nt']} losses={st['nl']}")); continue
        bi = bias_check(P, S, BE, si, j, st['consumed'])
        if bi is None: log.append((si, kind, ext, None, 'no matching unconsumed H1 bias')); continue
        d = S[si]['d']
        fill_raw, spr = quote(d)
        if fill_raw is None: log.append((si, kind, ext, bi, f'no fill: {spr}')); continue
        lv, why = order_levels(S[si], d, fill_raw, spr, cfg)
        if lv is None: log.append((si, kind, ext, bi, why)); continue
        sl, tp, risk = lv
        log.append((si, kind, ext, bi, 'ok'))
        return dict(si=si, kind=kind, ext=ext, bi=bi, sl=sl, tp=tp, risk=risk, fill_raw=fill_raw, spr=spr), log
    return None, log
