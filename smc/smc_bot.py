"""SMC DEMO forward-test bot (MT5). DEMO ONLY.
v0.3: XAUUSD M10 + M15 (engine.py via smc_live.py, limit orders).
v0.5 'full mentor model' M15: XAUUSD (tested) + EURUSD/GBPUSD (UNTESTED EXTENSION) (engine_v05.py via smc_live_v05.py,
market entry at the confirmation-bar close).
XAUUSD_V5_M15 adds the v0.6 wick-rejection confirmation (engulf/star/wick); FX instances keep engulf/star.
Run: wine python.exe smc_bot.py [--dry-run] [--replay-days N] [--replay-v5-days N [--replay-out FILE.csv]]
     wine python.exe smc_bot.py --explain-day YYYY-MM-DD   ('why no trade' funnel per window, read-only)
Diagnostics (2026-11-23): after each window (London 05:00 NY, NY AM 11:00 NY) one `session_summary` event per
instance (smc_diag.py; read-only recompute + this bot's own log; never alters a decision)."""
import sys, os, json, time, math, argparse, traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd
import MetaTrader5 as mt5
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import engine as E
import smc_live as L
import smc_diag as D
import importlib
L5 = importlib.import_module(os.environ.get('SMC_LIVE_MODULE', 'smc_live_v05'))   # staged test: SMC_LIVE_MODULE=smc_live_next

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
DEMO_LOGIN = 5059023305
NY, GH, UTC = ZoneInfo('America/New_York'), ZoneInfo('Africa/Accra'), timezone.utc
RISK_PCT = 0.005
HIST_M1 = 30000          # ~3 weeks of M1 for warm-up (H1 pivots, PDH/PDL, ATR)
LOOP_S = 20
HEARTBEAT_S = 1800
EVENTS = os.path.join(HERE, 'events.jsonl')
STATE = os.path.join(HERE, 'state.json')
DIAG_STATE = os.path.join(HERE, 'diag_state.json')   # diagnostics only (which summaries were sent); not bot state
FIELDS = ['symbol', 'side', 'sweep_level', 'choch_level', 'fvg_low', 'fvg_high', 'entry', 'sl', 'tp', 'rr',
          'lots', 'risk_usd', 'result_R', 'pnl_usd', 'balance', 'reason']


DISCONNECT_ALERT_S = 15 * 60


class Bot:
    def __init__(self, dry=False, events=EVENTS):
        self.dry = dry; self.events = events
        self.st = json.load(open(STATE)) if (os.path.exists(STATE) and not dry) else {}
        self.st.setdefault('orders', {}); self.st.setdefault('deals', []); self.st.setdefault('daily', {})
        self.st.setdefault('last_bar', {}); self.offset_h = self.st.get('offset_h'); self.last_hb = 0

    # ---------- infra ----------
    def ev(self, typ, inst=None, **kw):
        now = datetime.now(UTC)
        rec = dict(ts_utc=now.isoformat(timespec='seconds'), ts_ny=now.astimezone(NY).isoformat(timespec='seconds'),
                   ts_ghana=now.astimezone(GH).isoformat(timespec='seconds'), instance=inst, type=typ)
        for f in FIELDS: rec[f] = kw.pop(f, None)
        rec.update(kw)
        for k, v in rec.items():
            if isinstance(v, (np.floating, float)) and v is not None: rec[k] = None if not math.isfinite(v) else round(float(v), 5)
            elif isinstance(v, np.integer): rec[k] = int(v)
            elif isinstance(v, np.datetime64): rec[k] = str(v)
        line = json.dumps(rec, default=str)
        with open(self.events, 'a') as f: f.write(line + '\n')
        print(line, flush=True)

    def save(self):
        if self.dry: return
        self.st['offset_h'] = self.offset_h
        self.st['deals'] = self.st['deals'][-2000:]
        tmp = STATE + '.tmp'; json.dump(self.st, open(tmp, 'w'), default=str, indent=1); os.replace(tmp, STATE)

    def connect(self):
        if not mt5.initialize(path=TERMINAL, timeout=120000):
            raise RuntimeError(f'mt5.initialize failed {mt5.last_error()}')
        ai = mt5.account_info()
        if ai is None or ai.login != DEMO_LOGIN or ai.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            mt5.shutdown()
            raise SystemExit('REFUSING: not the expected DEMO account')  # never trade a real account
        mt5.symbol_select('XAUUSD', True)
        for inst in list(L5.CONFIGS):
            sym = L5.CONFIGS[inst]['symbol']
            if not mt5.symbol_select(sym, True) or mt5.symbol_info(sym) is None:
                L5.CONFIGS.pop(inst)
                self.ev('error', inst, symbol=sym, reason=f'symbol {sym} not available on this demo server; instance disabled')

    def alive(self):
        """Wait out broker/server disconnects instead of re-initialising (re-init during a reconnect
        returns -6 Authorization failed). Only escalate to an error after DISCONNECT_ALERT_S."""
        t0 = None
        while True:
            ti = mt5.terminal_info()
            if ti is None:                       # IPC to terminal lost: try to re-attach
                try: mt5.shutdown()
                except Exception: pass
                if mt5.initialize(path=TERMINAL, timeout=60000):
                    ti = mt5.terminal_info()
            if ti is not None and ti.connected:
                if t0 is not None:
                    self.ev('reconnected', reason=f'terminal back online after {int(time.time()-t0)}s')
                    self._disc_alerted = False
                return ti
            if t0 is None:
                t0 = time.time(); self.ev('disconnected', reason='terminal not connected to server; waiting')
            if time.time() - t0 > DISCONNECT_ALERT_S and not getattr(self, '_disc_alerted', False):
                self._disc_alerted = True
                self.ev('error', reason=f'terminal disconnected for {int(time.time()-t0)//60} min')
            time.sleep(15)

    def server_offset(self):
        """hours to subtract from MT5 bar/tick times to get UTC"""
        tk = mt5.symbol_info_tick('XAUUSD'); now = time.time()
        off = round((tk.time - now) / 3600) if tk else None
        if tk and abs(off) <= 4 and abs(tk.time - off * 3600 - now) < 600:  # fresh tick only
            if off != self.offset_h: self.ev('heartbeat', reason=f'server offset set to UTC{off:+d}h')
            self.offset_h = off
        if self.offset_h is None:  # market closed and never seen a fresh tick: MetaQuotes server = NY + 7h
            n = datetime.now(NY); self.offset_h = 7 + int(n.utcoffset().total_seconds() // 3600)
        return self.offset_h

    def m1(self, n=HIST_M1, symbol='XAUUSD'):
        r = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, n)
        if r is None or len(r) == 0: raise RuntimeError(f'copy_rates_from_pos failed {mt5.last_error()}')
        df = pd.DataFrame(r); off = self.server_offset(); pt = mt5.symbol_info(symbol).point
        idx = pd.to_datetime(df.time.values - off * 3600, unit='s', utc=True)
        spr = df.spread.values * pt
        m = pd.DataFrame({'o': df.open.values, 'h': df.high.values, 'l': df.low.values, 'c': df.close.values,
                          'oa': df.open.values + spr, 'ha': df.high.values + spr, 'la': df.low.values + spr,
                          'ca': df.close.values + spr}, index=idx)
        stale = (m.o == m.h) & (m.h == m.l) & (m.l == m.c) & (m.c == m.c.shift()) & (m.ca == m.ca.shift())
        return m[~stale]  # same stale filter as engine.load_m1

    # ---------- account helpers ----------
    def mine(self, magic, symbol='XAUUSD'):
        pos = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.magic == magic]
        ords = [o for o in (mt5.orders_get(symbol=symbol) or []) if o.magic == magic]
        return pos, ords

    def day(self, inst):
        k = f"{inst}|{datetime.now(NY).date()}"
        return self.st['daily'].setdefault(k, dict(trades=0, losses=0))

    def cancel(self, inst, o, reason):
        r = mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=o.ticket))
        ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        self.ev('cancelled' if ok else 'error', inst, symbol=o.symbol, reason=f'{reason} ticket={o.ticket}' + ('' if ok else f' ret={getattr(r, "retcode", None)} {mt5.last_error()}'))

    def close_pos(self, inst, p, reason):
        tk = mt5.symbol_info_tick(p.symbol)
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=p.symbol, volume=p.volume, position=p.ticket,
                   type=mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                   price=tk.bid if p.type == mt5.POSITION_TYPE_BUY else tk.ask, deviation=50, magic=p.magic,
                   comment=reason[:30], type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_IOC)
        r = mt5.order_send(req)
        if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
            self.ev('error', inst, symbol=p.symbol, reason=f'close failed {reason} ret={getattr(r, "retcode", None)} {getattr(r, "comment", "")}')

    # ---------- deals -> filled / closed ----------
    def scan_deals(self):
        now = datetime.now(UTC)
        deals = mt5.history_deals_get(now - timedelta(days=4), now + timedelta(days=2)) or []
        magics = {c['magic']: n for n, c in {**L.CONFIGS, **L5.CONFIGS}.items()}
        seen = set(self.st['deals'])
        for d in sorted(deals, key=lambda x: x.time_msc):
            if d.ticket in seen: continue
            meta = self.st['orders'].get(str(d.position_id)) or self.st['orders'].get(str(d.order))
            inst = magics.get(d.magic) or (meta or {}).get('instance')
            if inst is None: continue
            self.st['deals'].append(d.ticket)
            bal = mt5.account_info().balance
            side = 'long' if d.type == mt5.DEAL_TYPE_BUY else 'short'
            if d.entry == mt5.DEAL_ENTRY_IN:
                self.day(inst)['trades'] += 1
                self.ev('filled', inst, symbol=d.symbol, side=side, entry=d.price, lots=d.volume,
                        sl=(meta or {}).get('sl'), tp=(meta or {}).get('tp'), risk_usd=(meta or {}).get('risk_usd'),
                        balance=bal, reason=f'deal={d.ticket} position={d.position_id}')
            elif d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                pnl = d.profit + d.commission + d.swap + getattr(d, 'fee', 0.0)
                rusd = (meta or {}).get('risk_usd')
                R = pnl / rusd if rusd else None
                if pnl < 0: self.day(inst)['losses'] += 1
                why = {mt5.DEAL_REASON_SL: 'SL', mt5.DEAL_REASON_TP: 'TP'}.get(d.reason, d.comment or 'manual/flat')
                self.ev('closed', inst, symbol=d.symbol, side='long' if side == 'short' else 'short', entry=(meta or {}).get('entry'),
                        sl=(meta or {}).get('sl'), tp=(meta or {}).get('tp'), lots=d.volume, risk_usd=rusd,
                        result_R=R, pnl_usd=pnl, balance=bal, reason=f'{why} exit={d.price} position={d.position_id}')
        self.save()

    # ---------- housekeeping each loop ----------
    def manage(self, inst, c, m1recent):
        magic = c['magic']; pos, ords = self.mine(magic, c['symbol'])
        nyn = datetime.now(NY); tod = nyn.hour * 60 + nyn.minute
        flat = tod >= 12 * 60 or (nyn.weekday() == 4 and tod >= 11 * 60)
        for o in ords:
            meta = self.st['orders'].get(str(o.ticket), {})
            if flat: self.cancel(inst, o, 'flat_time'); continue
            exp = meta.get('expiry_utc')
            if exp and datetime.now(UTC) >= datetime.fromisoformat(exp): self.cancel(inst, o, 'expired(10 bars / window end)'); continue
            tp, placed = meta.get('tp'), meta.get('placed_utc')
            if tp and placed:  # engine rule: TP traded before fill -> cancel
                since = m1recent[m1recent.index >= pd.Timestamp(placed)]
                tk = mt5.symbol_info_tick(o.symbol)
                hi = max(since.h.max() if len(since) else -1e9, tk.bid); lo = min(since.l.min() if len(since) else 1e9, tk.bid)
                if (meta['d'] == 1 and hi >= tp) or (meta['d'] == -1 and lo <= tp): self.cancel(inst, o, 'TP traded before fill')
        for p in pos:
            if flat: self.close_pos(inst, p, 'flat 12:00 NY' if tod >= 720 else 'Friday 11:00 NY flat')

    # ---------- signal -> order ----------
    def act(self, inst, c, s, p, why, tclose):
        side = 'long' if s['d'] == 1 else 'short'
        base = dict(symbol=c['symbol'], side=side, sweep_level=s['sweep_level'], choch_level=s['mss_level'],
                    fvg_low=s['fvg_bot'], fvg_high=s['fvg_top'])
        if p is None:
            self.ev('skipped', inst, **base, reason=f'bar {tclose}: {why}'); return
        base.update(entry=p['entry'], sl=p['sl'], tp=p['tp'], rr=p['rr'])
        self.ev('signal', inst, **base, reason=f'bar_close_utc={tclose} expiry={p["expiry"]}')
        if self.dry: return
        if p['expiry'] <= tclose: self.ev('skipped', inst, **base, reason='zero order lifetime (signal at window end; backtest = no fill)'); return
        magic = c['magic']; pos, ords = self.mine(magic); dd = self.day(inst)
        if pos or ords: self.ev('skipped', inst, **base, reason='busy: max 1 position+pending'); return
        if dd['trades'] >= 3 or dd['losses'] >= 2: self.ev('skipped', inst, **base, reason=f'daily stop {dd}'); return
        nyn = datetime.now(NY); tod = nyn.hour * 60 + nyn.minute
        bny = (pd.Timestamp(tclose).tz_localize('UTC') - pd.Timedelta(minutes=c['tf'])).tz_convert(NY)
        if not E.in_window(bny.hour * 60 + bny.minute, c['tf']) or 12 * 60 <= tod < 13 * 60:
            self.ev('skipped', inst, **base, reason='outside window'); return
        if datetime.now(UTC) - pd.Timestamp(tclose).tz_localize('UTC').to_pydatetime() > timedelta(minutes=c['tf']):
            self.ev('skipped', inst, **base, reason='signal stale (>1 bar old)'); return
        self.place(inst, c, s['d'], p, base)

    def place(self, inst, c, d, p, base):
        magic = c['magic']
        si = mt5.symbol_info(c['symbol']); tk = mt5.symbol_info_tick(c['symbol']); dg = si.digits
        spread = tk.ask - tk.bid
        if p['risk'] < 5 * spread: self.ev('skipped', inst, **base, reason=f'cost gate: SL dist {p["risk"]:.2f} < 5x live spread {spread:.2f}'); return
        bal = mt5.account_info().balance; risk_usd = bal * RISK_PCT
        # SIZING FIX 2026-11-20: tick_value/tick_size is unreliable on this feed (XAUUSD reports
        # tick_value=0.1 / tick_size=0.01 => $10 per $1 per lot, but the real contract is 100oz =>
        # $100 per $1 per lot, i.e. lots came out 10x too big). Money per 1.0 price move per lot is
        # the contract size, which is correct for XAUUSD (100), EURUSD/GBPUSD (100000).
        per_lot = p['risk'] * si.trade_contract_size
        tv_per_lot = p['risk'] * si.trade_tick_value / si.trade_tick_size if si.trade_tick_size else 0
        if tv_per_lot > 0 and abs(tv_per_lot / per_lot - 1) > 0.25:
            self.ev('spec', inst, symbol=si.name,
                    reason=f'SIZING: contract-based ${per_lot:.2f}/lot vs tick-based ${tv_per_lot:.2f}/lot '
                           f'-> using contract-based (ratio {per_lot/tv_per_lot:.2f})')
        lots = math.floor(risk_usd / per_lot / si.volume_step + 1e-9) * si.volume_step
        lots = round(min(lots, si.volume_max), 2)
        if lots < si.volume_min: self.ev('skipped', inst, **base, reason=f'lot {lots} < min {si.volume_min}'); return
        entry, sl, tp = round(p['entry'], dg), round(p['sl'], dg), round(p['tp'], dg)
        d = d; market = (d == 1 and tk.ask <= entry) or (d == -1 and tk.bid >= entry)
        exp_utc = pd.Timestamp(p['expiry']).tz_localize('UTC')
        if market:  # engine fills at min(entry, open) when price already through the level
            req = dict(action=mt5.TRADE_ACTION_DEAL, type=mt5.ORDER_TYPE_BUY if d == 1 else mt5.ORDER_TYPE_SELL,
                       price=tk.ask if d == 1 else tk.bid, type_time=mt5.ORDER_TIME_GTC)
            fills = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]
        else:
            req = dict(action=mt5.TRADE_ACTION_PENDING, type=mt5.ORDER_TYPE_BUY_LIMIT if d == 1 else mt5.ORDER_TYPE_SELL_LIMIT,
                       price=entry)
            if si.expiration_mode & 4:
                req.update(type_time=mt5.ORDER_TIME_SPECIFIED, expiration=int(exp_utc.timestamp()) + self.offset_h * 3600)
            else: req.update(type_time=mt5.ORDER_TIME_GTC)
            fills = [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK]
        req.update(symbol=c['symbol'], volume=lots, sl=sl, tp=tp, deviation=20, magic=magic, comment=f'smc v03 {inst[-3:]}')
        chk = None
        for f in fills:
            req['type_filling'] = f; chk = mt5.order_check(req)
            if chk is not None and chk.retcode in (0, mt5.TRADE_RETCODE_DONE): break
        else:
            self.ev('order_rejected', inst, **base, lots=lots, risk_usd=risk_usd, balance=bal,
                    reason=f'order_check {getattr(chk, "retcode", None)} {getattr(chk, "comment", "")} {mt5.last_error()}'); return
        r = mt5.order_send(req)
        if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            self.ev('order_rejected', inst, **base, lots=lots, risk_usd=risk_usd, balance=bal,
                    reason=f'order_send {getattr(r, "retcode", None)} {getattr(r, "comment", "")} {mt5.last_error()}'); return
        tick = r.order
        self.st['orders'][str(tick)] = dict(instance=inst, d=d, entry=entry, sl=sl, tp=tp, lots=lots, risk_usd=risk_usd,
                                            expiry_utc=exp_utc.isoformat(), placed_utc=datetime.now(UTC).isoformat())
        self.save()
        self.ev('order_placed', inst, **{**base, 'entry': entry, 'sl': sl, 'tp': tp}, lots=lots, risk_usd=risk_usd, balance=bal,
                reason=f'{"market" if market else "limit"} ticket={tick} expiry_utc={exp_utc.isoformat()}')

    # ---------- v0.5 (engine_v05 via smc_live_v05) ----------
    def rates(self, symbol, timeframe, n):
        """Native MT5 bars -> UTC open-time index (DST-aware: MetaQuotes-Demo clock = New York + 7h, i.e. UTC+2
        winter / UTC+3 summer; falls back to the measured fixed offset if that rule disagrees with the live tick)."""
        r = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
        if r is None or len(r) == 0: raise RuntimeError(f'copy_rates_from_pos {symbol} tf={timeframe} failed {mt5.last_error()}')
        df = pd.DataFrame(r); pt = mt5.symbol_info(symbol).point; off = self.server_offset()
        srv = pd.to_datetime(df.time.values, unit='s')
        if off == 7 + int(datetime.now(NY).utcoffset().total_seconds() // 3600):
            idx = (srv - pd.Timedelta(hours=7)).tz_localize(NY, ambiguous='NaT', nonexistent='NaT').tz_convert('UTC')
        else:
            if not getattr(self, '_off_warned', False):
                self._off_warned = True; self.ev('heartbeat', reason=f'server offset {off} != NY+7 rule; v0.5 uses fixed offset')
            idx = pd.to_datetime(df.time.values - off * 3600, unit='s', utc=True)
        b = pd.DataFrame({'o': df.open.values, 'h': df.high.values, 'l': df.low.values, 'c': df.close.values,
                          'spr': df.spread.values * pt}, index=idx)
        return b[~b.index.isna()]

    def v5_state(self, inst):
        v = self.st.setdefault('v5', {}).setdefault(inst, {})
        v.setdefault('used', []); v.setdefault('consumed', [])
        return v

    def evaluate_v5(self):
        now = pd.Timestamp.now(tz='UTC')
        for inst, c in L5.CONFIGS.items():
            tf = c['tf']; bc = now.floor(f'{tf}min')
            if (now - bc).total_seconds() < 5: continue
            key = str(bc)
            if self.st['last_bar'].get(inst) == key: continue
            self.st['last_bar'][inst] = key
            ny = (bc - pd.Timedelta(minutes=tf)).tz_convert(NY)
            if not E.in_window(ny.hour * 60 + ny.minute, tf): continue   # confirmations only count inside windows
            bars = self.rates(c['symbol'], mt5.TIMEFRAME_M15, L5.N_TF + 2)
            bars = bars[bars.index + pd.Timedelta(minutes=tf) <= bc].iloc[-L5.N_TF:]      # closed M15 only
            if len(bars) == 0 or bars.index[-1] != bc - pd.Timedelta(minutes=tf): continue  # no ticks in that bar
            h1 = self.rates(c['symbol'], mt5.TIMEFRAME_H1, L5.N_H1 + 2)
            h1 = h1[h1.index + pd.Timedelta(hours=1) <= bc].iloc[-L5.N_H1:]                # closed H1 only
            self.v5_bar(inst, c, bars, h1, bc)
        self.save()

    def v5_bar(self, inst, c, bars, h1, bc):
        tf, sym = c['tf'], c['symbol']
        P, S, BE = L5.context(bars, h1, c); j = len(P['c']) - 1
        if pd.Timestamp(P['bclose'][j]).tz_localize('UTC') != bc: return
        vs = self.v5_state(inst); dd = self.day(inst); pos, ords = self.mine(c['magic'], sym)
        st = dict(used=set(vs['used']), consumed=set(vs['consumed']), nt=dd['trades'], nl=dd['losses'])

        def quote(d):
            tk = mt5.symbol_info_tick(sym)
            if tk is None: return None, 'no tick'
            nyn = datetime.now(NY); tod = nyn.hour * 60 + nyn.minute
            if tod >= 12 * 60: return None, 'after 12:00 NY'
            if nyn.weekday() == 4 and tod >= 11 * 60: return None, 'Friday >= 11:00 NY (flat rule)'
            if datetime.now(UTC) - bc.to_pydatetime() > timedelta(minutes=tf): return None, 'signal stale (>1 bar old)'
            return (tk.ask if d == 1 else tk.bid), max(float(P['spr'][j]), tk.ask - tk.bid)

        ch, log = L5.decide(P, S, BE, j, st, bool(pos or ords), quote, c)
        extra = dict(symbol=sym, untested_extension=not c['tested'], bar_close_utc=str(bc))
        for si, kind, ext, bi, why in log:
            if why == 'ok': continue
            self.ev('skipped', inst, **extra, **L5.describe(P, S, BE, si, kind, ext, bi, c.get('model', 'v0.5')), reason=f"{c.get('model', 'v0.5')} bar {bc}: {why}")
        if ch is None: return
        base = {**extra, **L5.describe(P, S, BE, ch['si'], ch['kind'], ch['ext'], ch['bi'], c.get('model', 'v0.5'))}
        self.ev('signal', inst, **base, entry=ch['fill_raw'], sl=ch['sl'], tp=ch['tp'], rr=L5.V.RR,
                reason=f"{c.get('model', 'v0.5')} {base['entry_type']} + {ch['kind']} at bar close {bc}; bias: {base['bias_reason']}")
        if self.dry: return
        self.place_v5(inst, c, ch, base)

    def place_v5(self, inst, c, ch, base):
        sym = c['symbol']; d = 1 if base['side'] == 'long' else -1
        si = mt5.symbol_info(sym); tk = mt5.symbol_info_tick(sym); dg = si.digits
        bal = mt5.account_info().balance; risk_usd = bal * RISK_PCT
        price = tk.ask if d == 1 else tk.bid
        sl = round(ch['sl'], dg); risk = d * (price - sl)
        spr = max(ch['spr'], tk.ask - tk.bid)
        if risk <= 0 or risk < L5.V.GATE_X * (spr + c['comm']):     # re-check gate at the fresh tick
            self.ev('skipped', inst, **base, reason=f'v0.5 cost gate at send: SL dist {risk:.5g}, spread {spr:.5g}'); return
        tp = round(price + d * L5.V.RR * risk, dg)
        per_lot = risk * si.trade_tick_value / si.trade_tick_size
        lots = math.floor(risk_usd / per_lot / si.volume_step + 1e-9) * si.volume_step
        lots = round(min(lots, si.volume_max), 2)
        if lots < si.volume_min: self.ev('skipped', inst, **base, reason=f'lot {lots} < min {si.volume_min}'); return
        req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=sym, volume=lots, type=mt5.ORDER_TYPE_BUY if d == 1 else mt5.ORDER_TYPE_SELL,
                   price=price, sl=sl, tp=tp, deviation=20, magic=c['magic'], comment=f'smc v05 {sym}', type_time=mt5.ORDER_TIME_GTC)
        chk = None
        for f in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK):   # IOC preferred; FX symbols here allow FOK only
            req['type_filling'] = f; chk = mt5.order_check(req)
            if chk is not None and chk.retcode in (0, mt5.TRADE_RETCODE_DONE): break
        else:
            self.ev('order_rejected', inst, **base, entry=price, sl=sl, tp=tp, lots=lots, risk_usd=risk_usd, balance=bal,
                    reason=f'order_check {getattr(chk, "retcode", None)} {getattr(chk, "comment", "")} {mt5.last_error()}'); return
        r = mt5.order_send(req)
        if r is None or r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            self.ev('order_rejected', inst, **base, entry=price, sl=sl, tp=tp, lots=lots, risk_usd=risk_usd, balance=bal,
                    reason=f'order_send {getattr(r, "retcode", None)} {getattr(r, "comment", "")} {mt5.last_error()}'); return
        fill = r.price or price
        vs = self.v5_state(inst)
        vs['used'] = (vs['used'] + [base['setup_key']])[-500:]
        if base.get('bias_key'): vs['consumed'] = (vs['consumed'] + [base['bias_key']])[-500:]
        tp_f = round(fill + d * L5.V.RR * d * (fill - sl), dg)           # TP = 3R from the ACTUAL fill; SL untouched
        if abs(tp_f - tp) >= si.point and d * (fill - sl) > 0:
            for p in self.mine(c['magic'], sym)[0]:
                m = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, symbol=sym, position=p.ticket, sl=sl, tp=tp_f, magic=c['magic']))
                if m is not None and m.retcode == mt5.TRADE_RETCODE_DONE: tp = tp_f
        risk_usd_act = d * (fill - sl) * lots * si.trade_tick_value / si.trade_tick_size
        self.st['orders'][str(r.order)] = dict(instance=inst, d=d, entry=fill, sl=sl, tp=tp, lots=lots, risk_usd=risk_usd_act,
                                               placed_utc=datetime.now(UTC).isoformat(), model='v0.5')
        self.save()
        self.ev('order_placed', inst, **{**base, 'entry': fill, 'sl': sl, 'tp': tp}, rr=L5.V.RR, lots=lots, risk_usd=risk_usd_act,
                balance=bal, order_kind='market', reason=f"market {'IOC' if req['type_filling'] == mt5.ORDER_FILLING_IOC else 'FOK'} "
                f"ticket={r.order} deal={r.deal}")

    # ---------- diagnostics: READ-ONLY (own state file, no order/state calls) ----------
    def _m1_until(self, we_utc, symbol='XAUUSD', n=HIST_M1):
        """same frame as m1(), but the n M1 bars up to we_utc (closed bars only)"""
        off = self.server_offset()
        r = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_M1, datetime.fromtimestamp(we_utc.timestamp() + off * 3600, UTC), n)
        if r is None or len(r) == 0: raise RuntimeError(f'copy_rates_from M1 failed {mt5.last_error()}')
        df = pd.DataFrame(r); pt = mt5.symbol_info(symbol).point
        idx = pd.to_datetime(df.time.values - off * 3600, unit='s', utc=True)
        spr = df.spread.values * pt
        m = pd.DataFrame({'o': df.open.values, 'h': df.high.values, 'l': df.low.values, 'c': df.close.values,
                          'oa': df.open.values + spr, 'ha': df.high.values + spr, 'la': df.low.values + spr,
                          'ca': df.close.values + spr}, index=idx)
        stale = (m.o == m.h) & (m.h == m.l) & (m.l == m.c) & (m.c == m.c.shift()) & (m.ca == m.ca.shift())
        m = m[~stale]
        return m[m.index + pd.Timedelta(minutes=1) <= we_utc]

    def _rates_until(self, symbol, timeframe, n, we_utc):
        """same conversion as rates(), but the n bars up to we_utc"""
        off = self.server_offset()
        r = mt5.copy_rates_from(symbol, timeframe, datetime.fromtimestamp(we_utc.timestamp() + off * 3600, UTC), n)
        if r is None or len(r) == 0: raise RuntimeError(f'copy_rates_from {symbol} tf={timeframe} failed {mt5.last_error()}')
        df = pd.DataFrame(r); pt = mt5.symbol_info(symbol).point
        srv = pd.to_datetime(df.time.values, unit='s')
        if off == 7 + int(datetime.now(NY).utcoffset().total_seconds() // 3600):
            idx = (srv - pd.Timedelta(hours=7)).tz_localize(NY, ambiguous='NaT', nonexistent='NaT').tz_convert('UTC')
        else:
            idx = pd.to_datetime(df.time.values - off * 3600, unit='s', utc=True)
        b = pd.DataFrame({'o': df.open.values, 'h': df.high.values, 'l': df.low.values, 'c': df.close.values,
                          'spr': df.spread.values * pt}, index=idx)
        return b[~b.index.isna()]

    def explain_window(self, day, window, events_path=EVENTS):
        """funnel per instance for one NY day + window ('london'|'ny_am'). Returns {inst: dict}."""
        ws, we = D.window_utc(day, window); out = {}
        if L.CONFIGS:
            m1 = self._m1_until(we)
            for inst, c in L.CONFIGS.items():
                r = D.funnel_v03(m1, c['tf'], c['cfg'], day, window)
                dec = D.live_decisions(events_path, inst, ws, we)
                out[inst] = dict(model='v0.3', symbol=c['symbol'], r=r, dec=dec, line=D.line_v03(window, r, dec))
        for inst, c in L5.CONFIGS.items():
            td = pd.Timedelta(minutes=c['tf'])
            bars = self._rates_until(c['symbol'], mt5.TIMEFRAME_M15, L5.N_TF + 40, we)
            bars = bars[bars.index + td <= we].iloc[-(L5.N_TF + 20):]
            h1 = self._rates_until(c['symbol'], mt5.TIMEFRAME_H1, L5.N_H1 + 8, we)
            h1 = h1[h1.index + pd.Timedelta(hours=1) <= we].iloc[-L5.N_H1:]
            r = D.funnel_v05(L5, bars, h1, c, day, window)
            dec = D.live_decisions(events_path, inst, ws, we)
            out[inst] = dict(model=c.get('model', 'v0.5'), symbol=c['symbol'], r=r, dec=dec, line=D.line_v05(window, r, dec))
        return out

    @staticmethod
    def flat_funnel(x):
        f = dict(x['r']['funnel']); dec = x['dec']
        for k, v in dec['skipped'].items(): f[f'skipped_{k.replace(" ", "_")}'] = v
        f.update(signals=dec['signals'], placed=dec['placed'], filled=dec['filled'], rejected=dec['rejected'])
        return f

    def session_diag(self):
        """one session_summary per instance 2..90 min after each window end (weekdays). Diagnostics only."""
        nyn = datetime.now(NY); tod = nyn.hour * 60 + nyn.minute
        if nyn.weekday() >= 5: return
        try: ds = json.load(open(DIAG_STATE))
        except Exception: ds = {}
        ds.setdefault('sent', []); ds.setdefault('lines', {})
        day = str(nyn.date())
        for w, (s, e) in D.WINDOWS.items():
            key = f'{day}|{w}'
            if not (e + 2 <= tod < e + 90) or key in ds['sent']: continue
            ds['sent'] = (ds['sent'] + [key])[-60:]          # mark first: an exception can never make it loop
            json.dump(ds, open(DIAG_STATE, 'w'), indent=1)
            res = self.explain_window(day, w)
            for inst, x in res.items():
                prev = ds['lines'].get(f'{day}|london|{inst}') if w == 'ny_am' else None
                ds['lines'][f'{day}|{w}|{inst}'] = x['line']
                self.ev('session_summary', inst, symbol=x['symbol'], window=w, day_ny=day, model=x['model'],
                        bars_scanned=x['r']['bars_scanned'], funnel=self.flat_funnel(x), h1_bias=x['r']['h1_bias'],
                        consistent=x['r'].get('consistent'), log_covered=x['dec']['log_covered'],
                        reason=(prev + '; ' if prev else '') + x['line'])
            ds['lines'] = {k: v for k, v in ds['lines'].items() if k[:10] >= str(nyn.date() - timedelta(days=7))}
            json.dump(ds, open(DIAG_STATE, 'w'), indent=1)

    # ---------- main loop ----------
    def evaluate_closed_bars(self, m1):
        now = pd.Timestamp.now(tz='UTC')
        for inst, c in L.CONFIGS.items():
            tf = c['tf']; bc = now.floor(f'{tf}min')
            if (now - bc).total_seconds() < 5: continue
            key = str(bc)
            if self.st['last_bar'].get(inst) == key: continue
            self.st['last_bar'][inst] = key
            m1t = m1[m1.index < bc]
            if len(m1t) == 0 or m1t.index[-1] < bc - pd.Timedelta(minutes=tf): continue  # no bars in closed TF bar (market closed)
            ny = (bc - pd.Timedelta(minutes=tf)).tz_convert(NY); tod = ny.hour * 60 + ny.minute
            if not E.in_window(tod, tf): continue  # setups() only fires inside the windows anyway
            close64 = np.datetime64(bc.tz_localize(None))
            for s, p, why, tclose in L.signals_at(m1t, tf, c['cfg'], close64):
                self.act(inst, c, s, p, why, tclose)
        self.save()

    def run(self):
        self.connect()
        self.ev('heartbeat', reason=f'start dry_run={self.dry} configs={ {k: v["cfg"] for k, v in L.CONFIGS.items()} } '
                f'v05={ {k: (v["magic"], v.get("label", ""), "K=%s sl=%s 3R" % (v["K"], v["sl"]), "confs=" + "/".join(v.get("confirmations", L5.CONFS)), "tested" if v["tested"] else "UNTESTED EXTENSION") for k, v in L5.CONFIGS.items()} }',
                balance=mt5.account_info().balance)
        while True:
            try:
                ti = self.alive()
                m1 = self.m1()
                now = pd.Timestamp.now(tz='UTC'); m1 = m1[m1.index + pd.Timedelta(minutes=1) <= now]  # closed M1 only
                if not self.dry:
                    self.scan_deals()
                    for inst, c in {**L.CONFIGS, **L5.CONFIGS}.items(): self.manage(inst, c, m1.iloc[-300:])
                self.evaluate_closed_bars(m1)
                self.evaluate_v5()
                if time.time() - self.last_hb >= HEARTBEAT_S:
                    ai = mt5.account_info(); self.last_hb = time.time()
                    opens = {inst: [len(x) for x in self.mine(c['magic'], c['symbol'])] for inst, c in {**L.CONFIGS, **L5.CONFIGS}.items()}
                    self.ev('heartbeat', balance=ai.balance, reason=f'equity={ai.equity:.2f} trade_allowed={ti.trade_allowed} '
                            f'offset=UTC{self.offset_h:+d} last_m1={m1.index[-1]} [pos,pending]={opens} dry_run={self.dry}')
                if not self.dry:
                    try: self.session_diag()           # diagnostics only; failures never reach the trading path
                    except Exception as e: self.ev('diag_error', reason=f'{type(e).__name__}: {e} | {traceback.format_exc()[-300:]}')
            except SystemExit: raise
            except Exception as e:
                self.ev('error', reason=f'{type(e).__name__}: {e} | {traceback.format_exc()[-400:]}')
                time.sleep(30)
            time.sleep(LOOP_S)


def explain_day(day):
    """READ-ONLY: print the per-window 'why no trade' funnel for a past NY date (same code as session_summary)."""
    b = Bot(dry=True, events=os.path.join(HERE, 'events_dryrun.jsonl')); b.connect()
    print(f'explain-day {day} (NY date; London 02:00-05:00, NY AM 07:00-11:00 NY; detection recomputed from MT5 bars, '
          f'decisions from events.jsonl)', flush=True)
    for w in D.WINDOWS:
        ws, we = D.window_utc(day, w)
        print(f'\n== {D.LABEL[w]} {ws:%H:%M}-{we:%H:%M} UTC', flush=True)
        for inst, x in b.explain_window(day, w, events_path=EVENTS).items():
            extra = '' if x['r'].get('consistent', True) else '  [WARN mirror != engine.setups]'
            print(f"{inst:14} {x['model']:10} bars={x['r']['bars_scanned']:2d}  {x['line']}{extra}")
            print(f"{'':14} funnel {json.dumps(Bot.flat_funnel(x), default=str)}", flush=True)
    mt5.shutdown()


def replay(days, out_csv):
    """DRY-RUN history replay: evaluate every closed in-window TF bar of the last N days on data truncated at
    that bar's close (exactly what the live loop sees), log signals, write CSV for comparison."""
    b = Bot(dry=True, events=os.path.join(HERE, 'events_dryrun.jsonl')); b.connect()
    m1 = b.m1(); end = m1.index[-1].floor('1min'); start = end.normalize() - pd.Timedelta(days=days)
    rows = []
    for inst, c in L.CONFIGS.items():
        tf = c['tf']
        for bc in pd.date_range(start, end, freq=f'{tf}min'):
            ny = (bc - pd.Timedelta(minutes=tf)).tz_convert(NY)
            if not E.in_window(ny.hour * 60 + ny.minute, tf): continue
            m1t = m1[m1.index < bc]
            if m1t.index[-1] < bc - pd.Timedelta(minutes=tf): continue
            for s, p, why, tclose in L.signals_at(m1t, tf, c['cfg'], np.datetime64(bc.tz_localize(None))):
                b.act(inst, c, s, p, why, tclose)
                rows.append(dict(instance=inst, bar_close_utc=str(tclose), d=s['d'], orderable=p is not None,
                                 entry=p['entry'] if p else None, sl=p['sl'] if p else None, tp=p['tp'] if p else None,
                                 sweep_level=s['sweep_level'], choch=s['mss_level'], reason=why))
        # one-pass over full history = backtest-style; must equal the per-bar truncated replay (causality on MT5 data)
        full = [(str(t), s['d']) for s, p, why, t in L.signals_at(m1, tf, c['cfg']) if pd.Timestamp(t, tz='UTC') >= start]
        trunc = [(r['bar_close_utc'], r['d']) for r in rows if r['instance'] == inst]
        print(inst, 'truncated-replay signals', len(trunc), 'one-pass signals', len(full), 'identical:', trunc == full, flush=True)
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print('server offset h', b.offset_h, 'M1 range', m1.index[0], m1.index[-1])
    mt5.shutdown()


def replay_v5(days, out_csv):
    """DRY-RUN v0.5 consistency check on MT5 bars. Live path = for every closed in-window M15 bar of the last N
    calendar days, the bot's own inputs (last N_TF closed M15 + last N_H1 closed H1 bars at that moment, sliding)
    -> smc_live_v05.context/decide, with positions emulated on MT5 M1 via engine.simulate. Batch = engine_v05
    run_config ONCE over the whole fetched history (backtest style). Trade lists must be identical."""
    b = Bot(dry=True, events=os.path.join(HERE, 'events_dryrun_v05.jsonl')); b.connect()
    rows, out = [], {}
    for inst, c in L5.CONFIGS.items():
        tf, sym, td = c['tf'], c['symbol'], pd.Timedelta(minutes=c['tf'])
        now = pd.Timestamp.now(tz='UTC')
        bars = b.rates(sym, mt5.TIMEFRAME_M15, L5.N_TF + days * 96 + 50); bars = bars[bars.index + td <= now]
        h1 = b.rates(sym, mt5.TIMEFRAME_H1, L5.N_H1 + days * 24 + 30); h1 = h1[h1.index + pd.Timedelta(hours=1) <= now]
        m1 = b.rates(sym, mt5.TIMEFRAME_M1, min(days * 1440 + 3000, 99000)); m1 = m1[m1.index + pd.Timedelta(minutes=1) <= now]
        m1 = pd.DataFrame({'o': m1.o, 'h': m1.h, 'l': m1.l, 'c': m1.c, 'oa': m1.o + m1.spr, 'ha': m1.h + m1.spr,
                           'la': m1.l + m1.spr, 'ca': m1.c + m1.spr}, index=m1.index)
        M = E.M1(m1)
        start = (bars.index[-1] + td).normalize() - pd.Timedelta(days=days)
        start = max(start, m1.index[0].ceil('1D'))
        s64 = np.datetime64(start.tz_convert(None))
        # ---- batch (pure engine_v05) ----
        V5 = L5.V; confs = tuple(c.get('confirmations', ('engulf', 'star')))
        if 'wick' in confs:   # v0.6 batch: engine_v06 bias (bias_tf=60) + engine_v06.CONFS
            V6 = importlib.import_module('engine_v06'); assert confs == tuple(V6.CONFS)
            Pb = V6.prepare(L5.as_m1(bars), tf); Sb = V6.setups(Pb); BEb = V6.bias_events(L5.as_m1(h1), c['K'], V6.BIAS_TF[tf])
            run = lambda *a, **k: V6.run_config(*a, confs=V6.CONFS, **k); eng = 'engine_v06'
        else:
            Pb = V5.prepare(L5.as_m1(bars), tf); Sb = V5.setups(Pb); BEb = V5.bias_events(L5.as_m1(h1), c['K'])
            run = V5.run_config; eng = 'engine_v05'
        bm, V5.BUF_MIN = V5.BUF_MIN, c['buf_min']      # engine constant is gold's $0.30; FX uses 0.3 pip like the live path
        try: tb = run(Pb, Sb, M, BEb, c['sl'], 'raw', c['comm'], start=s64)
        finally: V5.BUF_MIN = bm
        batch = [] if len(tb) == 0 else [(str(pd.Timestamp(r.signal_ts)), int(r.d), r.type, r.conf, round(float(r.ob_lo), 5), round(float(r.ob_hi), 5),
                                          round(float(r.sl), 5), round(float(r.entry), 5), round(float(r.R), 3)) for r in tb.itertuples()]
        # ---- live path, bar by bar ----
        used, consumed, daycnt, busy_until, live, nb = set(), set(), {}, None, [], 0
        for t0 in bars.index[bars.index + td >= start]:
            bc = t0 + td; ny = t0.tz_convert(NY)
            if not E.in_window(ny.hour * 60 + ny.minute, tf): continue
            bt = bars[bars.index + td <= bc].iloc[-L5.N_TF:]; ht = h1[h1.index + pd.Timedelta(hours=1) <= bc].iloc[-L5.N_H1:]
            P, S, BE = L5.context(bt, ht, c); j = len(P['c']) - 1; ts = P['bclose'][j]; nb += 1
            dkey = P['day'][j]; nt, nl = daycnt.get(dkey, (0, 0))

            def quote(d, ts=ts, dkey=dkey, j=j, P=P):            # engine_v05.run_config fill rule on MT5 M1
                i = np.searchsorted(M.ts, ts)
                if i >= len(M.ts) or M.day[i] != dkey or M.ts[i] - ts > np.timedelta64(tf, 'm'): return None, 'M1 gap'
                if M.tod[i] >= E.FLAT_MIN: return None, 'after 12:00 NY'
                return (M.ao[i] if d == 1 else M.bo[i]), P['spr'][j]
            busy = busy_until is not None and ts < busy_until
            ch, log = L5.decide(P, S, BE, j, dict(used=used, consumed=consumed, nt=nt, nl=nl), busy, quote, c)
            for si, kind, ext, bi, why in log:
                ds = L5.describe(P, S, BE, si, kind, ext, bi, c.get('model', 'v0.5'))
                rows.append(dict(instance=inst, bar_close_utc=str(bc), side=ds['side'], entry_type=ds['entry_type'], conf=kind,
                                 zone_lo=ds['fvg_low'], zone_hi=ds['fvg_high'], bos=ds['sweep_level'], mss=ds['choch_level'],
                                 bias=ds['bias_reason'], decision=why))
            if ch is None: continue
            s = S[ch['si']]; d = s['d']
            r = E.simulate(M, ts, d, ch['fill_raw'], ch['sl'], ch['tp'], ts, ts, 'raw', c['comm'], market=True)
            if r is None: rows[-1]['decision'] = 'no M1 exit data yet (open at data end)'; continue
            busy_until = r['exit_ts']; daycnt[dkey] = (nt + 1, nl + (r['R'] < 0))
            used.add(L5.setup_key(P, s)); consumed.add(L5.bias_key(BE[ch['bi']]))
            rows[-1].update(entry=ch['fill_raw'], sl=ch['sl'], tp=ch['tp'], R=r['R'], exit=r['how'])
            live.append((str(pd.Timestamp(ts)), int(d), s['type'], ch['kind'], round(float(s['lo']), 5), round(float(s['hi']), 5),
                         round(float(ch['sl']), 5), round(float(ch['fill_raw']), 5), round(float(r['R']), 3)))
        out[inst] = dict(bars_evaluated=nb, live=live, batch=batch, identical=live == batch, start=str(start),
                         m1=f'{m1.index[0]}..{m1.index[-1]}', setups=len(Sb), bias_events=len(BEb), batch_engine=eng, confs=list(confs),
                         wick_confs_in_window=sum(1 for s in Sb for jj, k, _ in s['confs'] if k == 'wick' and Pb['bclose'][jj] >= s64))
        print(inst, eng, 'confs', confs, 'from', start, '| in-window M15 bars evaluated', nb, '| live trades', len(live), '| batch trades', len(batch),
              '| identical:', live == batch, flush=True)
        for x in live: print('   live ', x)
        for x in batch:
            if x not in live: print('   batch-only', x)
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    json.dump(out, open(out_csv.replace('.csv', '.json'), 'w'), indent=1, default=str)
    print('candidates logged:', len(rows), '->', out_csv)
    mt5.shutdown()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--replay-days', type=int, default=0)
    ap.add_argument('--replay-v5-days', type=int, default=0)
    ap.add_argument('--replay-out', default='replay_v05_signals.csv')
    ap.add_argument('--explain-day', default=None, help='YYYY-MM-DD (NY date): print the per-window funnel, read-only')
    a = ap.parse_args()
    if a.explain_day: explain_day(a.explain_day)
    elif a.replay_v5_days: replay_v5(a.replay_v5_days, os.path.join(HERE, a.replay_out))
    elif a.replay_days: replay(a.replay_days, os.path.join(HERE, 'replay_signals.csv'))
    else: Bot(dry=a.dry_run).run()
