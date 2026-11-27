"""One-off DEMO test of the order path: far-away buy limit with SL/TP + expiration, then cancel."""
import numpy as np, pandas as pd, MetaTrader5 as mt5, time
import smc_bot as B
B.STATE = B.os.path.join(B.HERE, 'state_test.json')
b = B.Bot(events=B.os.path.join(B.HERE, 'events_test.jsonl')); b.connect(); b.server_offset()
tk = mt5.symbol_info_tick('XAUUSD'); e = round(tk.bid - 60, 2)
c = dict(symbol='XAUUSD', tf=10, magic=990399)
p = dict(entry=e, sl=e - 8, tp=e + 16, risk=8.0, rr=2, expiry=np.datetime64((pd.Timestamp.now('UTC') + pd.Timedelta(minutes=5)).tz_localize(None)))
b.place('TEST', c, 1, p, dict(symbol='XAUUSD', side='long'))
o = [x for x in mt5.orders_get(symbol='XAUUSD') or [] if x.magic == 990399]
print('pending:', [(x.ticket, x.type, x.volume_current, x.price_open, x.sl, x.tp, x.type_time, x.time_expiration, x.type_filling) for x in o])
print('tick time', tk.time, 'now', int(time.time()), 'offset', b.offset_h, 'expiration_mode', mt5.symbol_info('XAUUSD').expiration_mode, 'trade_allowed', mt5.terminal_info().trade_allowed)
for x in o: b.cancel('TEST', x, 'place_test cleanup')
print('left:', [x.ticket for x in mt5.orders_get(symbol='XAUUSD') or [] if x.magic == 990399])
