import MetaTrader5 as mt5, csv, sys
mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe")
sym = "GBPUSD"
mt5.symbol_select(sym, True)
rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 200)
with open(r"Z:\tmp\gbpusd_m15.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["time", "open", "high", "low", "close", "tick_volume"])
    for r in rates:
        w.writerow([int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), int(r["tick_volume"])])
print("rows", len(rates), "last", rates[-1]["close"])
mt5.shutdown()
