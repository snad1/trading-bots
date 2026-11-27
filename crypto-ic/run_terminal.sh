#!/bin/bash
# Launch the IC Markets MT5 instance (separate from the :99 /root/.mt5 broker instance)
export DISPLAY=:101
export WINEPREFIX=/root/.mt5-icmarkets
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="
# Xvfb :101 is managed by systemd unit xvfb101.service
systemctl is-active --quiet xvfb101 || systemctl start xvfb101
exec wine "/root/.mt5-icmarkets/drive_c/Program Files/MetaTrader 5/terminal64.exe" /portable
