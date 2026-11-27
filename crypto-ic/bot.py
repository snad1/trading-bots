#!/usr/bin/env python3
"""BTCUSD_V6_M15 -- 24/7 DEMO forward test of the frozen v0.6 M15 rules on IC Markets (MT5, Wine prefix
/root/.mt5-icmarkets, DISPLAY :101, account 53158615 ICMarketsSC-Demo, magic 993515).

Isolation: this process, this magic and this events/state file only.  The gold/FX bot (/opt/mt5/bot,
magic 990515/991515/992515) shares neither prefix nor files.

Detection and trade selection are crypto_ic.context()/decide() -- the same functions replay.py walks
bar by bar, which is what makes the live path testable against the backtest (lesson 26).

Loop every LOOP_S: wait for terminal connectivity -> on each closed M15 bar evaluate the frozen rules
-> market order with SL and TP attached (ORDER_FILLING_FOK) -> reconcile fills/closes from deals.
SL is never widened, no averaging, one position at a time, 3 trades / 2 losses per UTC day.
"""
import argparse
import json
import math
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import crypto_ic as C          # noqa: E402

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
CREDS_FILE = os.environ.get("CRYPTO_IC_CREDS") or "/root/.icmarkets/creds.pw"
DEMO_LOGIN = 53158615
DEMO_SERVER = 'ICMarketsSC-Demo'
RISK_PCT = 0.005
LOOP_S = 10
HEARTBEAT_S = 1800
DISCONNECT_ALERT_S = 15 * 60
EVENTS = os.environ.get('CRYPTO_IC_EVENTS') or os.path.join(HERE, 'events.jsonl')
STATE = os.environ.get('CRYPTO_IC_STATE') or os.path.join(HERE, 'state.json')
NY = ZoneInfo('America/New_York')
GH = ZoneInfo('Africa/Accra')
UTC = timezone.utc

FIELDS = ['symbol', 'side', 'entry', 'sl', 'tp', 'rr', 'lots', 'risk_usd', 'result_R', 'pnl_usd',
          'balance', 'reason', 'entry_type', 'confirmation', 'bias_reason']

# symbol_info.filling_mode is a MASK: 1 = FOK allowed, 2 = IOC allowed (2 is NOT "FOK").
# Measured on BTCUSD / ICMarketsSC-Demo: mask 2, FOK -> retcode 10030, IOC -> Done.  So the
# order uses FOK only when the mask says so, else IOC; order_check decides if the mask lies.
# live funnel (diagnostics only: counters, never read by any decision); emitted every 4h UTC (00,04,..,20)
FUNNEL_FILE = os.path.join(HERE, 'funnel_live.json')
FUNNEL_EVERY_H = 4
FUNNEL_KEYS = ('m15_bars', 'no_bar', 'bias_bars', 'zone_bars', 'tap_bars', 'events', 'busy', 'day_stop', 'bias',
               'no_quote', 'gate', 'signal', 'gate_send', 'lot_min', 'rejected', 'placed', 'filled', 'closed')

FILLINGS = [(mt5.ORDER_FILLING_FOK, 'FOK'), (mt5.ORDER_FILLING_IOC, 'IOC'), (mt5.ORDER_FILLING_RETURN, 'RETURN')]


def fillings_for(si):
    """supported filling modes, mask order first"""
    order = [f for f in FILLINGS if si.filling_mode & (1 if f[0] == mt5.ORDER_FILLING_FOK else 2)]
    return order or [FILLINGS[1]]


class Bot:
    def __init__(self, dry=False):
        self.dry = dry
        self.fresh = not os.path.exists(STATE)      # first boot on a live account: adopt, don't re-count
        self.st = json.load(open(STATE)) if os.path.exists(STATE) else {}
        self.st.setdefault('used', [])
        self.st.setdefault('consumed', [])
        self.st.setdefault('daily', {})
        self.st.setdefault('orders', {})
        self.st.setdefault('deals', [])
        self.st.setdefault('last_m15_close', None)
        self.offset_h = self.st.get('offset_h', 2)
        self.last_hb = 0.0
        self.fun_st = {'counts': {}, 'since': None, 'last_emit': None}
        try:
            self.fun_st.update(json.load(open(FUNNEL_FILE)))
        except Exception:                                            # noqa: BLE001
            pass
        self.fun_st['since'] = self.fun_st.get('since') or datetime.now(UTC).isoformat(timespec='seconds')
        self.fun_st['last_emit'] = self.fun_st.get('last_emit') or \
            pd.Timestamp.now(tz='UTC').floor(f'{FUNNEL_EVERY_H}h').isoformat()

    # ---------------- live funnel (diagnostics only) ----------------
    def fun(self, key, n=1):
        try:
            c = self.fun_st['counts']
            c[key] = c.get(key, 0) + n
            if not self.dry:
                tmp = FUNNEL_FILE + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(self.fun_st, f)
                os.replace(tmp, FUNNEL_FILE)
        except Exception:                                            # noqa: BLE001
            pass

    def fun_ctx(self, P, S, BE, j):
        try:
            ts = P['bclose'][j]
            i = -1
            for k, e in enumerate(BE):
                if e['ts'] <= ts:
                    i = k
            if i >= 0 and ts < BE[i]['exp']:
                self.fun('bias_bars')
            live = [s for s in S if s['ta'] < j <= s['ta'] + C.V.LIFE and (s['inval'] is None or j < s['inval'])]
            if live:
                self.fun('zone_bars')
            if any((P['l'][j] <= s['hi']) if s['d'] == 1 else (P['h'][j] >= s['lo']) for s in live):
                self.fun('tap_bars')
        except Exception:                                            # noqa: BLE001
            pass

    @staticmethod
    def fun_line(c):
        if not c.get('m15_bars'):
            return 'no M15 bars evaluated'
        s = (f"{c['m15_bars']} M15 bars, bias {c.get('bias_bars', 0)}/{c['m15_bars']} bars, zone live "
             f"{c.get('zone_bars', 0)}, tapped {c.get('tap_bars', 0)}, {c.get('events', 0)} confirmation(s)")
        names = dict(busy='busy', day_stop='daily stop', bias='no H1 bias', no_quote='no quote', gate='cost gate',
                     gate_send='cost gate at send', lot_min='lot<min', rejected='rejected')
        rej = [f'{c[k]} {v}' for k, v in names.items() if c.get(k)]
        if rej:
            s += ', ' + ', '.join(rej)
        if c.get('signal') or c.get('placed') or c.get('filled') or c.get('closed'):
            s += f", signal={c.get('signal', 0)} placed={c.get('placed', 0)} filled={c.get('filled', 0)} closed={c.get('closed', 0)}"
        elif not c.get('events'):
            s += ' -> no setup confirmed'
        else:
            s += ' -> no trade'
        return s

    def fun_emit_due(self):
        b = pd.Timestamp.now(tz='UTC').floor(f'{FUNNEL_EVERY_H}h')
        if b <= pd.Timestamp(self.fun_st['last_emit']):
            return
        c = self.fun_st['counts']
        full = {k: int(c.get(k, 0)) for k in FUNNEL_KEYS}
        since = self.fun_st['since']
        self.ev('funnel', symbol=C.SYMBOL, live=True, funnel=full, period_start=since, period_end=b.isoformat(),
                reason=(f'live funnel {pd.Timestamp(since):%m-%d %H:%M}-{b:%H:%M}Z: {self.fun_line(full)} | '
                        + ' '.join(f'{k}={v}' for k, v in full.items())))
        self.fun_st = {'counts': {}, 'since': b.isoformat(), 'last_emit': b.isoformat()}
        self.fun('m15_bars', 0)                                       # persist the reset

    # ---------------- logging ----------------
    def ev(self, typ, instance=C.INSTANCE, **kw):
        now = datetime.now(UTC)
        rec = dict(ts_utc=now.isoformat(timespec='seconds'), ts_ny=now.astimezone(NY).isoformat(timespec='seconds'),
                   ts_ghana=now.astimezone(GH).isoformat(timespec='seconds'), instance=instance, type=typ)
        for f in FIELDS:
            rec[f] = kw.pop(f, None)
        rec.update(kw)
        for k, v in list(rec.items()):
            if isinstance(v, (np.floating, float)):
                rec[k] = None if not math.isfinite(float(v)) else round(float(v), 5)
            elif isinstance(v, np.integer):
                rec[k] = int(v)
            elif isinstance(v, np.datetime64):
                rec[k] = str(v)
        line = json.dumps(rec, default=str)
        with open(EVENTS, 'a') as f:
            f.write(line + '\n')
        print(line, flush=True)

    def save(self):
        if self.dry:
            return
        self.st['offset_h'] = self.offset_h
        self.st['deals'] = self.st['deals'][-2000:]
        self.st['used'] = self.st['used'][-400:]
        self.st['consumed'] = self.st['consumed'][-400:]
        tmp = STATE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.st, f, default=str, indent=1)
        os.replace(tmp, STATE)

    # ---------------- infra ----------------
    def connect(self):
        # Log in from a root-only credential file so a terminal restart can never leave the bot
        # stuck on a login dialog (Snad approved storing the DEMO password, 2026-11-20).
        creds = {}
        try:
            with open(CREDS_FILE, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, v = ln.split("=", 1)
                    creds[k.strip().lower()] = v.strip()
        except FileNotFoundError:
            creds = {}
        init_kw = dict(path=TERMINAL, timeout=120000)
        if creds.get("password") and creds.get("login", "").isdigit():
            init_kw.update(login=int(creds["login"]), password=creds["password"],
                           server=creds.get("server") or DEMO_SERVER)
        if not mt5.initialize(**init_kw):
            raise RuntimeError(f'mt5.initialize failed {mt5.last_error()}')
        ai = mt5.account_info()
        if ai is None:
            raise SystemExit(f'no account info {mt5.last_error()}')
        self.ev('heartbeat', reason=f'connect: login={ai.login} server={ai.server} balance={ai.balance} '
                                   f'mode={ai.trade_mode} leverage=1:{ai.leverage}')
        if ai.login != DEMO_LOGIN or ai.server != DEMO_SERVER:
            mt5.shutdown()
            raise SystemExit(f'REFUSING: expected DEMO {DEMO_LOGIN}/{DEMO_SERVER}, got {ai.login}/{ai.server}')
        if ai.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise SystemExit('REFUSING: not a DEMO account')          # never trade a real account
        if not mt5.symbol_select(C.SYMBOL, True):
            raise SystemExit(f'{C.SYMBOL} not available')
        si = mt5.symbol_info(C.SYMBOL)
        self.check_spec(si)
        self.offset_h = C.server_offset(mt5, C.SYMBOL, self.offset_h)
        if self.fresh and not self.dry:
            # deals already on the account (manual tests etc.) must not seed the daily stop
            now = datetime.now(UTC)
            deals = mt5.history_deals_get(now - timedelta(days=30), now + timedelta(days=1)) or []
            self.st['deals'] = [d.ticket for d in deals if d.magic == C.MAGIC]
            self.fresh = False
            self.save()
            self.ev('heartbeat', reason=f'fresh state: {len(self.st["deals"])} pre-existing deals with '
                                        f'magic {C.MAGIC} marked as seen (not counted in the daily stop)')
        return si

    def check_spec(self, si):
        """the venue facts the frozen rules depend on; anything off is logged loudly"""
        bad = []
        if not fillings_for(si):
            bad.append('no filling mode')
        if si.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
            bad.append(f'trade_mode={si.trade_mode}')
        if si.volume_min > 0.01:
            bad.append(f'volume_min={si.volume_min}')
        self.ev('spec', symbol=si.name, reason=(f'digits={si.digits} contract={si.trade_contract_size} '
                                                f'min_lot={si.volume_min} step={si.volume_step} '
                                                f'filling_mask={si.filling_mode} '
                                                f'(fok={bool(si.filling_mode & 1)} ioc={bool(si.filling_mode & 2)}) '
                                                f'-> using {fillings_for(si)[0][1]} '
                                                f'spread_pts={si.spread} tick_value={si.trade_tick_value} '
                                                f'tick_size={si.trade_tick_size} max_lot={si.volume_max}')
                + (' | PROBLEM: ' + '; '.join(bad) if bad else ''))
        return not bad

    def alive(self):
        """wait out server reconnects; never re-initialise while the link is merely reconnecting (lesson 15)"""
        t0 = None
        while True:
            ti = mt5.terminal_info()
            if ti is None:
                try:
                    mt5.shutdown()
                except Exception:                                    # noqa: BLE001
                    pass
                if mt5.initialize(path=TERMINAL, timeout=60000):
                    ti = mt5.terminal_info()
            if ti is not None and ti.connected:
                if t0 is not None:
                    self.ev('reconnected', reason=f'terminal back online after {int(time.time() - t0)}s')
                    self._disc_alerted = False
                return ti
            if t0 is None:
                t0 = time.time()
                self.ev('disconnected', reason='terminal not connected to server; waiting')
            if time.time() - t0 > DISCONNECT_ALERT_S and not getattr(self, '_disc_alerted', False):
                self._disc_alerted = True
                self.ev('error', reason=f'terminal disconnected for {int(time.time() - t0) // 60} min')
            time.sleep(15)

    def rates(self, timeframe, n):
        r = mt5.copy_rates_from_pos(C.SYMBOL, timeframe, 0, n)
        if r is None or len(r) == 0:
            raise RuntimeError(f'copy_rates_from_pos({timeframe}) failed {mt5.last_error()}')
        si = mt5.symbol_info(C.SYMBOL)
        self.offset_h = C.server_offset(mt5, C.SYMBOL, self.offset_h)
        return C.frame_from_mt5(r, self.offset_h, si.point)

    def mine(self):
        return [p for p in (mt5.positions_get(symbol=C.SYMBOL) or []) if p.magic == C.MAGIC]

    def day(self, dkey=None):
        k = dkey or datetime.now(UTC).strftime('%Y-%m-%d')
        return self.st['daily'].setdefault(k, dict(trades=0, losses=0))

    # ---------------- deals -> filled / closed ----------------
    def scan_deals(self):
        now = datetime.now(UTC)
        deals = mt5.history_deals_get(now - timedelta(days=4), now + timedelta(days=2)) or []
        seen = set(self.st['deals'])
        for d in sorted(deals, key=lambda x: x.time_msc):
            if d.magic != C.MAGIC or d.ticket in seen:
                continue
            meta = (self.st['orders'].get(str(d.position_id)) or self.st['orders'].get(str(d.order)) or {})
            self.st['deals'].append(d.ticket)
            bal = mt5.account_info().balance
            side = 'long' if d.type == mt5.DEAL_TYPE_BUY else 'short'
            done = datetime.fromtimestamp(d.time_msc / 1000, UTC)
            if d.entry == mt5.DEAL_ENTRY_IN:
                self.fun('filled')
                self.day(done.strftime('%Y-%m-%d'))['trades'] += 1
                self.ev('filled', symbol=d.symbol, side=side, entry=d.price, lots=d.volume,
                        sl=meta.get('sl'), tp=meta.get('tp'), rr=C.RR, risk_usd=meta.get('risk_usd'),
                        balance=bal, entry_type=meta.get('entry_type'), confirmation=meta.get('confirmation'),
                        bias_reason=meta.get('bias_reason'),
                        reason=f'deal={d.ticket} position={d.position_id} vol={d.volume}')
            elif d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                pnl = d.profit + d.commission + d.swap + getattr(d, 'fee', 0.0)
                rusd = meta.get('risk_usd')
                R = pnl / rusd if rusd else None
                self.fun('closed')
                if pnl < 0:
                    self.day(done.strftime('%Y-%m-%d'))['losses'] += 1
                why = {mt5.DEAL_REASON_SL: 'SL', mt5.DEAL_REASON_TP: 'TP'}.get(d.reason, d.comment or 'manual/flat')
                self.ev('closed', symbol=d.symbol, side='short' if side == 'long' else 'long',
                        entry=meta.get('entry'), sl=meta.get('sl'), tp=meta.get('tp'), lots=d.volume,
                        rr=C.RR, risk_usd=rusd, result_R=R, pnl_usd=pnl, balance=bal,
                        entry_type=meta.get('entry_type'), confirmation=meta.get('confirmation'),
                        bias_reason=meta.get('bias_reason'),
                        reason=f'{why} exit={d.price} position={d.position_id} R={R if R is None else round(R, 3)}')
        self.save()

    # ---------------- per-bar evaluation ----------------
    def on_m15_close(self, bc):
        """bc = close time (UTC) of the bar that just closed = open time of the next bar.
        last_m15_close is only advanced once the bar has actually been evaluated, so an exception
        (terminal hiccup) makes the loop retry the same bar instead of silently skipping it."""
        m15 = self.rates(mt5.TIMEFRAME_M15, C.N_M15 + 2)
        m15 = m15[m15.index + pd.Timedelta(minutes=C.TF) <= bc].iloc[-C.N_M15:]
        if len(m15) == 0 or m15.index[-1] != bc - pd.Timedelta(minutes=C.TF):
            self.fun('no_bar')
            self.ev('skipped', reason=f'no closed M15 bar for {bc} (last {m15.index[-1] if len(m15) else None})')
            self.st['last_m15_close'] = bc.isoformat()
            self.save()
            return
        h1 = self.rates(mt5.TIMEFRAME_H1, C.N_H1 + 2)
        h1 = h1[h1.index + pd.Timedelta(hours=1) <= bc].iloc[-C.N_H1:]
        P, S, BE = C.context(m15, h1)
        j = len(P['c']) - 1
        if pd.Timestamp(P['bclose'][j]).tz_localize('UTC') != bc:
            self.ev('error', reason=f'bar alignment: engine close {pd.Timestamp(P["bclose"][j])} != {bc}')
            self.st['last_m15_close'] = bc.isoformat()
            self.save()
            return
        self.fun('m15_bars')
        self.fun_ctx(P, S, BE, j)
        pos = self.mine()
        dd = self.day(bc.strftime('%Y-%m-%d'))
        st = dict(used=set(self.st['used']), consumed=set(self.st['consumed']), nt=dd['trades'], nl=dd['losses'])

        def quote(_st, d):
            tk = mt5.symbol_info_tick(C.SYMBOL)
            if tk is None:
                return None, 'no tick'
            if not getattr(self, 'as_of_mode', False) and \
                    datetime.now(UTC) - bc.to_pydatetime() > timedelta(minutes=C.TF):
                return None, 'signal stale (>1 bar old)'
            return (tk.ask if d == 1 else tk.bid), max(float(P['spr'][j]), tk.ask - tk.bid)

        ch, log = C.decide(P, S, BE, j, st, bool(pos), quote)
        for _, _, _, _, why in log:                                   # diagnostics only
            if why == 'setup already traded':
                continue
            self.fun('events')
            self.fun('signal' if why == 'ok' else 'busy' if why.startswith('busy') else 'day_stop' if why.startswith('daily stop')
                     else 'bias' if 'H1 bias' in why else 'no_quote' if why.startswith('no fill') else 'gate')
        for si, kind, ext, bi, why in log:
            if why == 'ok':
                continue
            C_ = C.describe(P, S, BE, si, kind, ext, bi)
            self.ev('skipped', symbol=C.SYMBOL,
                    entry_type=C_['entry_type'], confirmation=C_['confirmation'],
                    bias_reason=C_['bias_reason'], reason=f'bar {bc}: {why}')
        if ch is None:
            self.st['last_m15_close'] = bc.isoformat()
            self.save()
            return
        base = C.describe(P, S, BE, ch['si'], ch['kind'], ch['ext'], ch['bi'])
        d = S[ch['si']]['d']
        sig_close = float(P['c'][j])
        self.ev('signal', symbol=C.SYMBOL, side=base['side'], entry=round(ch['fill'], 2), sl=round(ch['sl'], 2),
                tp=round(ch['tp'], 2), rr=C.RR, entry_type=base['entry_type'], confirmation=base['confirmation'],
                bias_reason=base['bias_reason'],
                reason=f"M15 {base['entry_type']} {ch['kind']} bar close {bc} spread {ch['spr']:.2f} "
                       f"gate {C.GATE_X * ch['spr']:.2f} bias: {base['bias_reason']}")
        if self.dry:
            self.st['last_m15_close'] = bc.isoformat()
            self.save()
            return
        self.place(base, d, ch, sig_close)
        self.st['last_m15_close'] = bc.isoformat()
        self.save()

    def place(self, base, d, ch, sig_close):
        si = mt5.symbol_info(C.SYMBOL)
        tk = mt5.symbol_info_tick(C.SYMBOL)
        if tk is None:
            self.ev('order_rejected', symbol=C.SYMBOL, entry_type=base['entry_type'],
                    confirmation=base['confirmation'], reason='no tick at send')
            return
        ti = mt5.terminal_info()
        if ti is not None and not ti.trade_allowed:
            self.ev('order_rejected', symbol=C.SYMBOL, entry_type=base['entry_type'],
                    confirmation=base['confirmation'],
                    reason='AutoTrading disabled in the terminal (terminal_info.trade_allowed=False)')
            return
        dg = si.digits
        price = float(tk.ask if d == 1 else tk.bid)
        sl = round(ch['sl'], dg)
        spr = max(float(ch['spr']), tk.ask - tk.bid)
        risk = d * (price - sl)
        if risk <= 0 or risk < C.GATE_X * spr:
            self.fun('gate_send')
            self.ev('skipped', symbol=C.SYMBOL, side=base['side'], entry=round(price, 2), sl=sl,
                    entry_type=base['entry_type'], confirmation=base['confirmation'],
                    bias_reason=base['bias_reason'],
                    reason=f'cost_gate at send: SL {risk:.2f} < {C.GATE_X}x spread {C.GATE_X * spr:.2f}')
            return
        ai = mt5.account_info()
        bal = float(ai.balance)
        risk_usd = RISK_PCT * bal
        per_lot = risk * si.trade_tick_value / si.trade_tick_size      # $ per lot at the SL distance
        lots = math.floor(risk_usd / per_lot / si.volume_step + 1e-9) * si.volume_step
        lots = round(min(lots, si.volume_max), 2)
        if lots < si.volume_min:
            self.fun('lot_min')
            self.ev('skipped', symbol=C.SYMBOL, side=base['side'], sl=sl, risk_usd=round(risk_usd, 2),
                    entry_type=base['entry_type'], confirmation=base['confirmation'],
                    reason=f'lot {lots:.2f} < min {si.volume_min} (risk ${risk_usd:.2f} / SL {risk:.2f})')
            return
        tp = round(price + d * C.RR * risk, dg)
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=C.SYMBOL, volume=float(lots),
                   type=mt5.ORDER_TYPE_BUY if d == 1 else mt5.ORDER_TYPE_SELL,
                   price=price, sl=sl, tp=tp, deviation=20, magic=C.MAGIC,
                   comment='v6 btc m15', type_time=mt5.ORDER_TIME_GTC, type_filling=None)
        # filling mode: only what the symbol mask allows, and order_check is the arbiter
        tries, ok = [], False
        for f, name in fillings_for(si):
            req['type_filling'] = f
            chk = mt5.order_check(req)
            code = getattr(chk, 'retcode', None)
            tries.append(f"{name}={code}")
            if getattr(self, 'order_check_only', False):
                self.ev('order_check', symbol=C.SYMBOL, side=base['side'], entry=round(price, 2), sl=sl,
                        tp=tp, lots=lots, risk_usd=round(risk_usd, 2), balance=bal,
                        entry_type=base['entry_type'], confirmation=base['confirmation'],
                        reason=f"{name} order_check ret={code} {getattr(chk, 'comment', '')} "
                               f"margin={getattr(chk, 'margin', None)} free={getattr(chk, 'margin_free', None)} "
                               f"trade_allowed={mt5.terminal_info().trade_allowed}")
                return
            if chk is not None and code in (0, mt5.TRADE_RETCODE_DONE):
                ok = True
                break
        if not ok:
            self.fun('rejected')
            self.ev('order_rejected', symbol=C.SYMBOL, side=base['side'], entry=round(price, 2), sl=sl, tp=tp,
                    lots=lots, risk_usd=round(risk_usd, 2), balance=bal,
                    entry_type=base['entry_type'], confirmation=base['confirmation'],
                    bias_reason=base['bias_reason'],
                    reason=f"no filling mode accepted: {' '.join(tries)} {mt5.last_error()}")
            return
        r = mt5.order_send(req)
        if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            self.fun('rejected')
            self.ev('order_rejected', symbol=C.SYMBOL, side=base['side'], entry=round(price, 2), sl=sl, tp=tp,
                    lots=lots, risk_usd=round(risk_usd, 2), balance=bal,
                    entry_type=base['entry_type'], confirmation=base['confirmation'],
                    bias_reason=base['bias_reason'],
                    reason=f"order_send ret={getattr(r, 'retcode', None)} {getattr(r, 'comment', '')} "
                           f"fill={req['type_filling']} {mt5.last_error()}")
            return
        fill = float(r.price or price)
        fill_risk = d * (fill - sl)
        tp_f = round(fill + d * C.RR * fill_risk, dg) if fill_risk > 0 else tp
        if abs(tp_f - tp) >= si.point and fill_risk > 0:          # TP = 3R from the ACTUAL fill; SL untouched
            for p in self.mine():
                m = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, symbol=C.SYMBOL, position=p.ticket,
                                        sl=sl, tp=tp_f, magic=C.MAGIC))
                if m is not None and m.retcode == mt5.TRADE_RETCODE_DONE:
                    tp = tp_f
                else:
                    self.ev('error', symbol=C.SYMBOL, reason=f'TP re-fix failed ret={getattr(m, "retcode", None)}; '
                                                            f'TP stays {tp}')
        risk_usd_act = fill_risk * lots * si.trade_tick_value / si.trade_tick_size
        self.st['used'].append(base['setup_key'])
        if base.get('bias_key'):
            self.st['consumed'].append(base['bias_key'])
        self.st['orders'][str(r.order)] = dict(
            instance=C.INSTANCE, d=d, entry=fill, sl=sl, tp=tp, lots=lots, risk_usd=risk_usd_act,
            placed_utc=datetime.now(UTC).isoformat(), entry_type=base['entry_type'],
            confirmation=base['confirmation'], bias_reason=base['bias_reason'], position=r.order)
        self.save()
        slip_r = (d * (fill - sig_close)) / fill_risk if fill_risk > 0 else None
        self.fun('placed')
        self.ev('order_placed', symbol=C.SYMBOL, side=base['side'], entry=round(fill, 2), sl=round(sl, 2),
                tp=round(tp, 2), rr=C.RR, lots=lots, risk_usd=round(risk_usd_act, 2), balance=bal,
                entry_type=base['entry_type'], confirmation=base['confirmation'],
                bias_reason=base['bias_reason'], slippage_R=None if slip_r is None else round(slip_r, 3),
                order_kind=f"market {name}",
                reason=f"ticket={r.order} deal={r.deal} signal_close={sig_close:.2f} fill={fill:.2f} "
                       f"slippage={0 if slip_r is None else slip_r:+.3f}R risk=${risk_usd_act:.2f}")

    # ---------------- main ----------------
    def run(self):
        si = self.connect()
        self.ev('heartbeat', balance=mt5.account_info().balance,
                reason=f'start instance={C.INSTANCE} magic={C.MAGIC} symbol={C.SYMBOL} dry={self.dry} '
                       f'rules=v0.6 M15 K={C.K} confs={"/".join(C.CONFS)} TP={C.RR}R gate={C.GATE_X}x spread '
                       f'buf=max(2x spread,{C.BUF_PCT:.2%}) daily={C.DAILY_MAX_TRADES}t/{C.DAILY_MAX_LOSSES}l UTC '
                       f'risk={RISK_PCT:.1%} offset=UTC{self.offset_h:+d} trade_allowed={mt5.terminal_info().trade_allowed}')
        if not self.dry and self.mine():
            for p in self.mine():
                self.ev('recovered', symbol=p.symbol, side='long' if p.type == mt5.POSITION_TYPE_BUY else 'short',
                        entry=p.price_open, sl=p.sl, tp=p.tp, lots=p.volume, reason=f'adopted open position {p.ticket}')
        while True:
            try:
                ti = self.alive()
                now = pd.Timestamp.now(tz='UTC')
                bc = now.floor(f'{C.TF}min')
                last = self.st.get('last_m15_close')
                if last and (bc - pd.Timestamp(last)).total_seconds() > C.TF * 60:
                    gap = int((bc - pd.Timestamp(last)).total_seconds() // (C.TF * 60)) - 1
                    if getattr(self, '_gap_logged', None) != (last, str(bc)):
                        self._gap_logged = (last, str(bc))
                        self.ev('skipped', reason=f'was down/behind: {gap} M15 bar(s) never evaluated '
                                                  f'(last {last}); resuming at {bc}')
                if (now - bc).total_seconds() >= 5 and self.st.get('last_m15_close') != bc.isoformat():
                    if not self.dry:
                        self.scan_deals()
                    self.on_m15_close(bc)
                if not self.dry:
                    self.scan_deals()
                if not self.dry:
                    try:
                        self.fun_emit_due()                           # diagnostics only
                    except Exception as e:                            # noqa: BLE001
                        print(f'funnel emit failed: {e}', flush=True)
                if time.time() - self.last_hb >= HEARTBEAT_S:
                    self.last_hb = time.time()
                    ai = mt5.account_info()
                    pos = self.mine()
                    dd = self.day()
                    self.ev('heartbeat', balance=ai.balance, reason=(
                        f'equity={ai.equity:.2f} trade_allowed={ti.trade_allowed} offset=UTC{self.offset_h:+d} '
                        f'last_m15_close={self.st.get("last_m15_close")} pos={len(pos)} '
                        f'day={dd["trades"]}t/{dd["losses"]}l used={len(self.st["used"])} '
                        f'consumed={len(self.st["consumed"])} dry={self.dry}'))
            except SystemExit:
                raise
            except Exception as e:                                    # noqa: BLE001
                self.ev('error', reason=f'{type(e).__name__}: {e} | {traceback.format_exc()[-400:]}')
                time.sleep(30)
            time.sleep(LOOP_S)


def selftest(b, lots=0.01, sl_pct=0.004, send=False):
    """Execution-path check on the live venue: build a market order at the current tick with a
    realistic SL distance, run order_check (FOK) -- and with send=True actually open and close
    `lots` immediately, to prove fill + SL/TP attach + deal reconciliation end to end."""
    si = b.connect()
    tk = mt5.symbol_info_tick(C.SYMBOL)
    price = float(tk.ask)
    sl = round(price * (1 - sl_pct), si.digits)
    risk = price - sl
    tp = round(price + C.RR * risk, si.digits)
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=C.SYMBOL, volume=float(lots), type=mt5.ORDER_TYPE_BUY,
               price=price, sl=sl, tp=tp, deviation=20, magic=C.MAGIC, comment='v6 selftest',
               type_time=mt5.ORDER_TIME_GTC, type_filling=None)
    ok, tries = False, []
    for f, name in fillings_for(si):
        req['type_filling'] = f
        chk = mt5.order_check(req)
        code = getattr(chk, 'retcode', None)
        tries.append(f"{name}={code}")
        b.ev('order_check', symbol=C.SYMBOL, side='long', entry=round(price, 2), sl=sl, tp=tp, lots=lots,
             risk_usd=round(risk * lots * si.trade_tick_value / si.trade_tick_size, 2),
             balance=mt5.account_info().balance,
             reason=f"selftest {name} order_check ret={code} {getattr(chk, 'comment', '')} ok="
                    f"{code in (0, mt5.TRADE_RETCODE_DONE)} margin={getattr(chk, 'margin', None)} "
                    f"free={getattr(chk, 'margin_free', None)} trade_allowed={mt5.terminal_info().trade_allowed}")
        if code in (0, mt5.TRADE_RETCODE_DONE):
            ok = True
            break
    if not ok:
        b.ev('error', reason=f"selftest: no filling mode accepted ({' '.join(tries)})")
        return False
    if not send:
        return ok
    if not ok:
        b.ev('error', reason='selftest: order_check refused, not sending')
        return False
    r = mt5.order_send(req)
    b.ev('order_placed' if r and r.retcode == mt5.TRADE_RETCODE_DONE else 'order_rejected',
         symbol=C.SYMBOL, side='long', entry=getattr(r, 'price', None), sl=sl, tp=tp, lots=lots,
         balance=mt5.account_info().balance,
         reason=f"selftest SEND ret={getattr(r, 'retcode', None)} {getattr(r, 'comment', '')} "
                f"order={getattr(r, 'order', None)} deal={getattr(r, 'deal', None)}")
    if not (r and r.retcode == mt5.TRADE_RETCODE_DONE):
        return False
    time.sleep(2)
    pos = b.mine()
    for p in pos:
        b.ev('recovered', symbol=p.symbol, side='long' if p.type == mt5.POSITION_TYPE_BUY else 'short',
             entry=p.price_open, sl=p.sl, tp=p.tp, lots=p.volume,
             reason=f'position {p.ticket} SL={p.sl} TP={p.tp} (both must be non-zero)')
    b.scan_deals()
    return flatten(b)


def flatten(b):
    """close every position carrying our magic (the only manual intervention the bot needs)"""
    n = 0
    for p in b.mine():
        si = mt5.symbol_info(p.symbol)
        tk = mt5.symbol_info_tick(p.symbol)
        long = p.type == mt5.POSITION_TYPE_BUY
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=p.volume, position=p.ticket,
                   type=mt5.ORDER_TYPE_SELL if long else mt5.ORDER_TYPE_BUY,
                   price=tk.bid if long else tk.ask, deviation=50, magic=C.MAGIC, comment='flatten',
                   type_time=mt5.ORDER_TIME_GTC, type_filling=None)
        done = None
        for f, name in fillings_for(si):
            req['type_filling'] = f
            c = mt5.order_send(req)
            if c is not None and c.retcode == mt5.TRADE_RETCODE_DONE:
                done = (c, name)
                break
        b.ev('closed' if done else 'error', symbol=p.symbol, side='long' if long else 'short',
             entry=p.price_open, sl=p.sl, tp=p.tp, lots=p.volume,
             reason=(f"flatten ticket={p.ticket} {done[1]} ret={done[0].retcode} "
                     f"price={getattr(done[0], 'price', None)}"
                     if done else f"flatten FAILED for {p.ticket} {mt5.last_error()}"))
        n += 1
    time.sleep(2)
    b.scan_deals()
    b.save()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--selftest', action='store_true',
                    help='order_check the execution path at the current tick (no order sent)')
    ap.add_argument('--send-test-trade', action='store_true',
                    help='with --selftest: also open and immediately close 0.01 lots (DEMO only)')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--once', action='store_true', help='evaluate the last closed M15 bar once, then exit')
    ap.add_argument('--as-of', help='with --once: evaluate the bar that closed at this UTC time (debug/replay)')
    ap.add_argument('--order-check', action='store_true',
                    help='with --once: build the order and run order_check only (never sends)')
    ap.add_argument('--flatten', action='store_true', help='close every position with our magic, then exit')
    a = ap.parse_args()
    b = Bot(dry=a.dry_run)
    if a.flatten:
        b.connect()
        n = flatten(b)
        print(f'flattened {n} position(s)')
        return 0
    if a.selftest:
        b.ev('selftest', reason='starting execution-path selftest')
        ok = selftest(b, send=a.send_test_trade)
        print(f'selftest ok={ok}')
        return 0 if ok else 1
    if a.once:
        b.connect()
        if a.as_of:
            t = pd.Timestamp(a.as_of, tz='UTC') if pd.Timestamp(a.as_of).tzinfo is None else pd.Timestamp(a.as_of)
            bc = t.tz_convert('UTC').ceil(f'{C.TF}min')      # bar OPEN time -> its close
        else:
            bc = pd.Timestamp.now(tz='UTC').floor(f'{C.TF}min')
        if a.order_check:
            b.order_check_only = True
        b.as_of_mode = bool(a.as_of)
        b.on_m15_close(bc)
        b.save()
        return 0
    while True:
        try:
            b.run()
            break
        except SystemExit as e:
            print(f'REFUSING: {e}', flush=True)
            time.sleep(60)
        except Exception as e:                                        # noqa: BLE001
            print(f'FATAL, restarting in 15s: {type(e).__name__} {e}', flush=True)
            traceback.print_exc()
            time.sleep(15)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
