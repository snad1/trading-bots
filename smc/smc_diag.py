"""'Why no trade' diagnostics for the SMC demo bot. READ-ONLY: never places, cancels or decides anything.

How the funnel is built (and why it cannot change a live decision):
  * Detection stages are RECOMPUTED for a just-closed window from the same closed MT5 bars, using the
    unchanged engine functions (engine.prepare / engine_v05 via smc_live_v05.context). The v0.3 chain
    (sweep -> bias -> CHoCH -> FVG -> liquidity target) is a counting mirror of engine.setups(); its
    output is cross-checked against engine.setups() itself (field `consistent`).
  * Decision stages (cost gate, busy, daily stop, signal, placed, filled) are read from the bot's own
    events.jsonl for that window, i.e. what the live code actually did.
  * Nothing here writes to state.json, touches the bot's sets/dicts, or calls order functions; it only
    reads bars + the events log and returns dicts. The caller emits them as `session_summary` events.
"""
import json, os
from datetime import datetime, timezone
import bisect
import numpy as np, pandas as pd
import engine as E
import smc_live as L

NY = E.NY
WINDOWS = {'london': (2 * 60, 5 * 60), 'ny_am': (7 * 60, 11 * 60)}   # NY minutes-of-day (== engine.WINDOWS)
LABEL = {'london': 'London', 'ny_am': 'NY AM'}


def window_utc(day, window):
    s, e = WINDOWS[window]
    d = pd.Timestamp(day)
    ws = pd.Timestamp(datetime(d.year, d.month, d.day, s // 60, s % 60), tz=NY).tz_convert('UTC')
    we = pd.Timestamp(datetime(d.year, d.month, d.day, e // 60, e % 60), tz=NY).tz_convert('UTC')
    return ws, we


# ---------------- v0.3 (engine.setups mirror with counters) ----------------
def funnel_v03(m1, tf, cfg, day, window):
    """m1: closed M1 (same frame Bot.m1() builds) truncated at the window end. Counts per sweep ('arm')
    how far it got through engine.setups(); then L.order_params (cost gate) per setup."""
    N = cfg[0]
    P = E.prepare(m1, tf)
    o, h, l, c = P['o'], P['h'], P['l'], P['c']
    n = len(c); A = P['atr']
    ph, pl = E.pivots(h, l, N)
    _, bu, bd = E.structure(c, ph, pl, n)
    fu = np.zeros(n, bool); fd = np.zeros(n, bool)
    fu[2:] = (l[2:] > h[:-2]) & ((l[2:] - h[:-2]) >= 0.1 * A[2:])
    fd[2:] = (h[2:] < l[:-2]) & ((l[:-2] - h[2:]) >= 0.1 * A[2:])
    hi_lv, lo_lv = [], []
    h1h, h1l = P['h1_ph_t'], P['h1_pl_t']; j1h = j1l = 0
    iph = ipl = 0; last_pl = last_ph = None
    armL = armS = None
    ws, we = WINDOWS[window]; dday = np.datetime64(pd.Timestamp(day).date(), 'D')
    stage = {}            # (d, sweep_bar) -> max stage reached during the window
    bars = 0; mirror = []; bias_dirs = set()
    for t in range(n):
        while iph < len(ph) and ph[iph][0] <= t: last_ph = ph[iph][2]; bisect.insort(hi_lv, ph[iph][2]); iph += 1
        while ipl < len(pl) and pl[ipl][0] <= t: last_pl = pl[ipl][2]; bisect.insort(lo_lv, pl[ipl][2]); ipl += 1
        while j1h < len(h1h) and h1h[j1h][0] <= P['bclose'][t]: bisect.insort(hi_lv, h1h[j1h][1]); j1h += 1
        while j1l < len(h1l) and h1l[j1l][0] <= P['bclose'][t]: bisect.insort(lo_lv, h1l[j1l][1]); j1l += 1
        if np.isnan(A[t]): continue
        tod = P['tod'][t]
        if E.in_arm_zone(tod, tf):
            Ls = [x for x in (P['asl'][t], P['pdl'][t], last_pl) if x is not None and not np.isnan(x)]
            sw = [x for x in Ls if l[t] < x < c[t]]
            if sw: armL = (t, l[t], max(sw))
            Hs = [x for x in (P['ash'][t], P['pdh'][t], last_ph) if x is not None and not np.isnan(x)]
            sw = [x for x in Hs if h[t] > x > c[t]]
            if sw: armS = (t, h[t], min(sw))
        if not E.in_window(tod, tf): continue
        inw = P['day'][t] == dday and ws <= tod < we
        if inw:
            bars += 1
            if P['bias'][t]: bias_dirs.add(int(P['bias'][t]))
        b = P['bias'][t]
        for d, arm, brk, fv in ((1, armL, bu, fu), (-1, armS, bd, fd)):
            if arm is None: continue
            sb = arm[0]; fresh = 0 <= t - sb <= 12
            k_ = (d, sb)
            if inw and fresh: stage[k_] = max(stage.get(k_, 0), 1)                       # sweep live
            if b != d: continue
            if inw and fresh: stage[k_] = max(stage.get(k_, 0), 2)                       # H1 bias agrees
            if np.isnan(brk[t]): continue
            if not (0 < t - sb <= 12): continue
            if inw: stage[k_] = max(stage.get(k_, 0), 3)                                 # CHoCH close
            cs = [k for k in range(t, sb - 1, -1) if k >= 2 and fv[k]]
            if not cs: continue
            if inw: stage[k_] = max(stage.get(k_, 0), 4)                                 # FVG POI
            k = cs[0]
            if d == 1:
                ext = h[sb:t + 1].max(); tps = hi_lv[bisect.bisect_right(hi_lv, ext):]
                cand = ([tps[0]] if tps else []) + ([P['pdh'][t]] if P['pdh'][t] > ext else [])
            else:
                ext = l[sb:t + 1].min(); tps = lo_lv[:bisect.bisect_left(lo_lv, ext)]
                cand = ([tps[-1]] if tps else []) + ([P['pdl'][t]] if P['pdl'][t] < ext else [])
            if not cand: continue
            if inw: stage[k_] = max(stage.get(k_, 0), 5)                                 # liquidity target
            mirror.append((t, d))
            if d == 1: armL = None
            else: armS = None
    # cross-check: mirror == engine.setups() (unchanged engine), restricted to this window
    S = E.setups(P, N)
    wset = [s for s in S if P['day'][s['t']] == dday and ws <= P['tod'][s['t']] < we]
    mw = [(t, d) for t, d in mirror if P['day'][t] == dday and ws <= P['tod'][t] < we]
    cost_ok, cost_rej = 0, []
    for s in wset:
        p, why = L.order_params(s, P, cfg)
        if p is None: cost_rej.append(why)
        else: cost_ok += 1
    cnt = lambda k: sum(1 for v in stage.values() if v >= k)
    f = dict(sweeps=cnt(1), bias_aligned=cnt(2), choch=cnt(3), fvg=cnt(4), liq_target=cnt(5), setups=len(wset),
             discount_premium='n/a (v0.3 explore: no discount filter)', target_2R='fixed 2R TP',
             cost_gate_ok=cost_ok)
    return dict(bars_scanned=bars, funnel=f, h1_bias=sorted(bias_dirs), cost_rejects=cost_rej,
                consistent=[(int(t), int(d)) for t, d in mw] == [(int(s['t']), int(s['d'])) for s in wset])


# ---------------- v0.5 ----------------
def funnel_v05(L5, bars, h1, c, day, window):
    """bars/h1: CLOSED native bars truncated at the window end. Same smc_live_v05.context() the live bot uses."""
    tf = c['tf']; confs = c.get('confirmations', L5.CONFS)
    P, S, BE = L5.context(bars, h1, c)
    ws, we = WINDOWS[window]; dday = np.datetime64(pd.Timestamp(day).date(), 'D')
    J = [j for j in range(len(P['c'])) if P['day'][j] == dday and ws <= P['tod'][j] < we and E.in_window(P['tod'][j], tf)]
    bts = np.array([e['ts'] for e in BE], dtype='datetime64[ns]') if BE else np.array([], dtype='datetime64[ns]')
    bias_bars, bias_dirs = 0, set()
    for j in J:
        i = np.searchsorted(bts, P['bclose'][j], side='right') - 1
        if i >= 0 and P['bclose'][j] < BE[i]['exp']:
            bias_bars += 1; bias_dirs.add(int(BE[i]['d']))
    live, tapped = set(), set()
    h, l = P['h'], P['l']
    for si, s in enumerate(S):
        if s['type'] not in L5.TYPES: continue
        for j in J:
            if s['ta'] < j <= s['ta'] + L5.V.LIFE and (s['inval'] is None or j < s['inval']):
                live.add(si)
                if (l[j] <= s['hi'] if s['d'] == 1 else h[j] >= s['lo']): tapped.add(si)
    cands = [(j, si, kind) for j in J for _, _, _, si, kind, _ in L5.candidates(P, S, j, confs)]
    bias_ok = sum(1 for j, si, _ in cands if L5.bias_check(P, S, BE, si, j, set()) is not None)
    nev = 0
    if J:
        t0, t1 = P['bclose'][J[0]] - np.timedelta64(24, 'h'), P['bclose'][J[-1]]
        nev = sum(1 for e in BE if t0 <= e['ts'] <= t1)          # H1 volume-candle bias events in the last 24h
    f = dict(h1_bias_events_24h=nev, bias_active_bars=bias_bars, zones_live=len(live), zones_tapped=len(tapped),
             confirmations=len(cands), bias_ok_replay=bias_ok)
    return dict(bars_scanned=len(J), funnel=f, h1_bias=sorted(bias_dirs), confs=list(confs))


# ---------------- live decisions from the bot's own log ----------------
def live_decisions(events_path, inst, ws_utc, we_utc):
    out = dict(log_covered=False, signals=0, placed=0, filled=0, rejected=0, cancelled=0, skipped={})
    if not os.path.exists(events_path): return out
    lo, hi = ws_utc - pd.Timedelta(minutes=1), we_utc + pd.Timedelta(minutes=10)
    with open(events_path, errors='replace') as fh:
        for ln in fh:
            if '"ts_utc"' not in ln: continue
            try: e = json.loads(ln)
            except Exception: continue
            try: t = pd.Timestamp(e['ts_utc'])
            except Exception: continue
            if not (lo <= t <= hi): continue
            ty = e.get('type')
            if ty == 'heartbeat': out['log_covered'] = True
            if e.get('instance') != inst: continue
            out['log_covered'] = True
            if ty == 'signal': out['signals'] += 1
            elif ty == 'order_placed': out['placed'] += 1
            elif ty == 'filled': out['filled'] += 1
            elif ty == 'order_rejected': out['rejected'] += 1
            elif ty == 'cancelled': out['cancelled'] += 1
            elif ty == 'skipped':
                r = str(e.get('reason') or '')
                r = r.split(': ', 1)[1] if r.startswith(('bar ', 'v0.5')) and ': ' in r else r
                key = ('no H1 bias' if 'H1 bias' in r else 'cost gate' if 'cost' in r else 'busy' if 'busy' in r
                       else 'daily stop' if 'daily stop' in r else 'stale' if 'stale' in r else 'window' if 'window' in r
                       else r[:40])
                out['skipped'][key] = out['skipped'].get(key, 0) + 1
    return out


# ---------------- one-line reasons ----------------
def _plural(n, w): return f"{n} {w}{'' if n == 1 else 'es' if w.endswith('h') else 's'}"


def line_v03(window, r, dec):
    f = r['funnel']; lab = LABEL[window]
    if not r['bars_scanned']: return f'{lab}: no bars (market closed)'
    s = f"{lab}: {_plural(f['sweeps'], 'sweep')}"
    if f['sweeps']:
        if not f['bias_aligned']: s += ', none with H1 bias'
        elif not f['choch']: s += f", {f['bias_aligned']} bias-aligned, no CHoCH"
        elif not f['fvg']: s += f", {f['choch']} CHoCH, no FVG"
        elif not f['liq_target']: s += f", {f['fvg']} FVG, no liquidity target"
        elif not f['cost_gate_ok']: s += f", {f['setups']} setup(s), all failed cost gate"
        else: s += f", {f['cost_gate_ok']} signal(s)"
    return s + _dec_tail(dec)


def line_v05(window, r, dec):
    f = r['funnel']; lab = LABEL[window]
    if not r['bars_scanned']: return f'{lab}: no bars (market closed)'
    b = {(): 'no active H1 bias', (1,): 'H1 bias bull', (-1,): 'H1 bias bear'}.get(tuple(r['h1_bias']), 'H1 bias mixed')
    s = f"{lab}: {b}; {f['zones_live']} zones live, {f['zones_tapped']} tapped, {_plural(f['confirmations'], 'confirmation')}"
    if f['confirmations']: s += f", {f['bias_ok_replay']} with bias"
    return s + _dec_tail(dec)


def _dec_tail(dec):
    if not dec['log_covered']: return ' [bot not logging in this window]'
    t = ''
    if dec['skipped']: t += ' | skipped: ' + ', '.join(f'{k}={v}' for k, v in dec['skipped'].items())
    if dec['signals'] or dec['placed'] or dec['filled']:
        t += f" | signal={dec['signals']} placed={dec['placed']} filled={dec['filled']}"
    elif not dec['skipped']:
        t += ' -> no trade'
    return t
