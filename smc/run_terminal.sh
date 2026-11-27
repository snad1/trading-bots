#!/bin/bash
# MT5 DEMO terminal under Wine (managed by mt5-terminal.service)
export DISPLAY=:99 WINEPREFIX=/root/.mt5 WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
cd /opt/mt5
exec /usr/bin/wine "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
