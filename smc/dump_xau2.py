import MetaTrader5 as mt5, csv
mt5.initialize(path=r"C:\Program Files\MetaTrader 5\terminal64.exe")
mt5.symbol_select("XAUUSD", True)
for tf_name, tf in (("m10", mt5.TIMEFRAME_M10), ("m15", mt5.TIMEFRAME_M15)):
    rates = mt5.copy_rates_from_pos("XAUUSD", tf, 0, 160)
    with open(rf"Z:\tmp\xau_{tf_name}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["time", "open", "high", "low", "close"])
        for r in rates:
            w.writerow([int(r["time"]), r["open"], r["high"], r["low"], r["close"]])
    print(tf_name, len(rates), round(rates[-1]["close"], 2))
t = mt5.symbol_info_tick("XAUUSD"); print("tick", t.bid, t.ask, round(t.ask - t.bid, 2))
mt5.shutdown()
