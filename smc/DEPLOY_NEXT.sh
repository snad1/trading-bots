#!/bin/bash
# Deploy staged v0.5+wick (XAUUSD_V5_M15 only; magic 990515) to the DEMO bot. DEMO ONLY.
# Staged: smc_bot_next.py -> smc_bot.py, smc_live_next.py -> smc_live_v05.py (engine_v06.py already in place).
set -u
cd /opt/mt5/bot || exit 1
export DISPLAY=:99 WINEPREFIX=/root/.mt5 WINEDEBUG=-all WINEDLLOVERRIDES=mscoree,mshtml=
PY=/root/.mt5/drive_c/Python310/python.exe
TS=$(date -u +%Y-%m-%d-%H%M)
for f in smc_bot_next.py smc_live_next.py engine_v06.py; do
  [ -f "$f" ] || { echo "missing $f - abort (nothing changed)"; exit 1; }
done
cp -p smc_bot.py "smc_bot.py.bak-$TS" && cp -p smc_live_v05.py "smc_live_v05.py.bak-$TS" || { echo "backup failed - abort"; exit 1; }
echo "backups: smc_bot.py.bak-$TS smc_live_v05.py.bak-$TS"
mv smc_bot_next.py smc_bot.py && mv smc_live_next.py smc_live_v05.py
if ! wine "$PY" -m py_compile smc_bot.py smc_live_v05.py engine_v06.py engine_v05.py report.py; then
  echo "py_compile FAILED - restoring backups, service NOT restarted"
  cp -p "smc_bot.py.bak-$TS" smc_bot.py; cp -p "smc_live_v05.py.bak-$TS" smc_live_v05.py
  exit 1
fi
echo "compiled OK; restarting smc-bot"
systemctl restart smc-bot
sleep 90
echo "smc-bot: $(systemctl is-active smc-bot)"
tail -n 3 events.jsonl
