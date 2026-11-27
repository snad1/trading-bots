"""Shared v0.3 decision logic used by BOTH the backtest comparison and the live bot.
Signal detection is engine.py unchanged (prepare + setups); this file only adds the order-parameter
step, copied 1:1 from engine.run_config(..., explore=True)."""
import numpy as np
import engine as E

CONFIGS = {  # frozen in PREREG_v03.md
    'XAUUSD_M10': dict(symbol='XAUUSD', tf=10, magic=990310, cfg=(5, 'edge', 'sweep', 2)),
    'XAUUSD_M15': dict(symbol='XAUUSD', tf=15, magic=990315, cfg=(2, 'ce', 'fvg', 2)),
}
COMM = 0.07  # $7/lot RT in price units (same as backtest raw cost; used only in the cost gate)


def order_params(s, P, cfg, comm=COMM):
    """Return (params dict, None) or (None, reject_reason). Mirrors engine.run_config explore=True."""
    _, etype, sltype, minrr = cfg
    d = s['d']; t = s['t']; tf = P['tf']
    entry = (s['fvg_top'] + s['fvg_bot']) / 2 if etype == 'ce' else (s['fvg_top'] if d == 1 else s['fvg_bot'])
    base = s['sweep_x'] if sltype == 'sweep' else s['a_ext']
    sl = base - d * (0.1 * s['atr'] + s['spr'])
    risk = d * (entry - sl)
    if risk <= 0: return None, 'risk<=0'
    tp = entry + d * minrr * risk
    if risk < 5 * (s['spr'] + comm): return None, f'cost_gate risk={risk:.2f} < 5x(spr {s["spr"]:.2f}+comm)'
    tclose = P['bclose'][t]
    expiry = tclose + np.timedelta64(10 * tf, 'm')
    we = E.window_end(P['tod'][t], tf)
    cancel = tclose + np.timedelta64(int(we - P['tod'][t] - tf), 'm')
    return dict(signal_ts=tclose, d=d, entry=entry, sl=sl, tp=tp, risk=risk, rr=minrr,
                expiry=min(expiry, cancel), sweep_level=s['sweep_level'], sweep_x=s['sweep_x'],
                choch_level=s['mss_level'], fvg_low=s['fvg_bot'], fvg_high=s['fvg_top'],
                atr=s['atr'], spr=s['spr']), None


def signals_at(m1, tf, cfg, close_ts=None):
    """Evaluate on M1 data (UTC index, cols o,h,l,c,oa,ha,la,ca). Returns list of (setup, params, reason)
    for setups decided at bar close == close_ts (np.datetime64, naive UTC); all if close_ts is None."""
    P = E.prepare(m1, tf); S = E.setups(P, cfg[0]); out = []
    for s in S:
        if close_ts is not None and P['bclose'][s['t']] != close_ts: continue
        p, why = order_params(s, P, cfg)
        out.append((s, p, why, P['bclose'][s['t']]))
    return out
