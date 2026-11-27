"""Summary of the SMC demo bot (v0.3 + v0.5): events.jsonl + MT5 deal history. Usage: wine python.exe report.py [days]"""
import sys, os, json
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import smc_live as L
import smc_live_v05 as L5
ALL = {**{k: dict(v, label=f"v0.3 cfg={v['cfg']}") for k, v in L.CONFIGS.items()}, **L5.CONFIGS}

days = int(sys.argv[1]) if len(sys.argv) > 1 else 3650
ev = [json.loads(l) for l in open(os.path.join(HERE, 'events.jsonl')) if l.strip()] if os.path.exists(os.path.join(HERE, 'events.jsonl')) else []
since = datetime.now(timezone.utc) - timedelta(days=days)
ev = [e for e in ev if datetime.fromisoformat(e['ts_utc']) >= since]
print(f'=== events.jsonl ({len(ev)} events, last {days}d) ===')
print('by type:', dict(Counter(e['type'] for e in ev)))
print('note: v0.5 was validated only on XAUUSD (VAL M15 +0.09R, 62 trades, INSUFFICIENT); EURUSD/GBPUSD v0.5 = untested extension')
for inst in ALL:
    cl = [e for e in ev if e['instance'] == inst and e['type'] == 'closed']
    R = [e['result_R'] for e in cl if e['result_R'] is not None]
    pnl = sum(e['pnl_usd'] or 0 for e in cl)
    wins = sum(1 for e in cl if (e['pnl_usd'] or 0) > 0)
    print(f"{inst} [{ALL[inst]['label']}]: signals={sum(e['instance']==inst and e['type']=='signal' for e in ev)} "
          f"orders={sum(e['instance']==inst and e['type']=='order_placed' for e in ev)} fills={sum(e['instance']==inst and e['type']=='filled' for e in ev)} "
          f"closed={len(cl)} win%={100*wins/len(cl) if cl else 0:.0f} totalR={sum(R):+.2f} avgR={(sum(R)/len(R)) if R else 0:+.3f} P&L=${pnl:+.2f}")
hb = [e for e in ev if e['type'] == 'heartbeat']
if hb: print('last heartbeat:', hb[-1]['ts_utc'], '|', hb[-1]['reason'])
errs = [e for e in ev if e['type'] in ('error', 'order_rejected')]
if errs: print(f'errors/rejections: {len(errs)}; last: {errs[-1]["ts_utc"]} {errs[-1]["reason"][:200]}')

if not mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe", timeout=60000):
    sys.exit(f'MT5 init failed {mt5.last_error()}')
ai = mt5.account_info()
print(f'\n=== MT5 account {ai.login} ({"DEMO" if ai.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "NOT DEMO!"}) ===')
print(f'balance ${ai.balance:.2f} equity ${ai.equity:.2f} margin ${ai.margin:.2f}')
deals = mt5.history_deals_get(since, datetime.now(timezone.utc) + timedelta(days=2)) or []
for inst, c in ALL.items():
    out = [d for d in deals if d.magic == c['magic'] and d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    pn = [d.profit + d.commission + d.swap for d in out]
    print(f"{inst} (magic {c['magic']}{'' if c.get('tested', True) else ', UNTESTED EXTENSION'}): closed trades={len(out)} wins={sum(p > 0 for p in pn)} P&L=${sum(pn):+.2f}")
for p in mt5.positions_get() or []:
    print(f'OPEN position {p.ticket} {p.symbol} magic={p.magic} {"BUY" if p.type == 0 else "SELL"} {p.volume} @ {p.price_open} sl={p.sl} tp={p.tp} pnl=${p.profit:+.2f}')
for o in mt5.orders_get() or []:
    print(f'PENDING order {o.ticket} {o.symbol} magic={o.magic} type={o.type} {o.volume_current} @ {o.price_open} sl={o.sl} tp={o.tp}')
mt5.shutdown()
