#!/bin/bash
# Deploy / re-deploy the BTCUSD_V6_M15 24/7 demo bot on IC Markets.
#   prefix  /root/.mt5-icmarkets   display :101   account 53158615 ICMarketsSC-Demo   magic 993515
# Idempotent; touches nothing of the gold/FX bot (/opt/mt5/bot, :99, /root/.mt5).
#
# Run as root:   bash /opt/crypto-ic/DEPLOY.sh
#
# What it does, in order (the order matters: the terminal rewrites common.ini on exit, so the
# AutoTrading flag is patched only after the old terminal is gone):
#   1. stop crypto-ic-bot (if it exists) and kill the wineserver of the IC prefix
#      (that kills only that prefix's terminal -- the :99 gold instance is a different prefix)
#   2. patch common.ini  [Experts] Enabled=1   (AutoTrading; without it order_send is refused)
#   3. install + enable xvfb101.service, mt5-ic-terminal.service, crypto-ic-bot.service
#   4. start xvfb :101 and the IC terminal, wait until it reports connected
#   5. start the bot, sleep 60, print is-active for all three units and the last 3 events
set -uo pipefail

HERE=/opt/crypto-ic
PREFIX=/root/.mt5-icmarkets
CONFIG="$PREFIX/drive_c/Program Files/MetaTrader 5/Config/common.ini"
PY="$PREFIX/drive_c/Python310/python.exe"
WINE=/usr/bin/wine
export DISPLAY=:101 WINEPREFIX="$PREFIX" WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
say() { printf '\n=== %s\n' "$*"; }
die() { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -x "$PY" ] || die "no python at $PY"
[ -f "$HERE/bot.py" ] || die "no $HERE/bot.py"
[ -f "$CONFIG" ] || die "no terminal config at $CONFIG"

say "1. stopping bot + IC terminal (prefix wineserver only)"
systemctl stop crypto-ic-bot 2>/dev/null || true
systemctl stop mt5-ic-terminal 2>/dev/null || true
# wineserver -k kills ONLY this prefix's wine processes (the :99 gold instance is another prefix);
# do NOT pkill terminal64.exe by name -- both brokers run the same exe path.
WINEPREFIX="$PREFIX" wineserver -k 2>/dev/null || true
sleep 4
left=$(ps -eo cmd | grep -c '[t]erminal64.exe /portable')
echo "  terminal64 processes still running (both brokers): $left"

say "2. patching AutoTrading flag in common.ini"
python3 - "$CONFIG" <<'EOF'
import sys
p = sys.argv[1]
raw = open(p, 'rb').read()
txt = raw.decode('utf-16')
if '[Experts]' in txt:
    print('  [Experts] already present - left untouched')
else:
    if not txt.endswith('\r\n'):
        txt += '\r\n'
    txt += '[Experts]\r\nAllowDllImport=0\r\nEnabled=1\r\nAccount=1\r\nProfile=1\r\nChart=0\r\nApi=0\r\n'
    open(p, 'wb').write(txt.encode('utf-16'))
    print('  added [Experts] Enabled=1 (AutoTrading on)')
EOF

say "3. installing units"
install -m 0644 "$HERE/mt5-ic-terminal.service" /etc/systemd/system/mt5-ic-terminal.service
install -m 0644 "$HERE/crypto-ic-bot.service" /etc/systemd/system/crypto-ic-bot.service
systemctl daemon-reload
systemctl enable xvfb101.service mt5-ic-terminal.service crypto-ic-bot.service >/dev/null 2>&1
for u in xvfb101 mt5-ic-terminal crypto-ic-bot; do printf '  %-18s %s\n' "$u" "$(systemctl is-enabled "$u" 2>&1)"; done

say "4. starting display + terminal"
systemctl restart xvfb101.service || die "xvfb101 failed"
sleep 2
systemctl restart mt5-ic-terminal.service || die "mt5-ic-terminal failed"
for i in $(seq 1 30); do
  sleep 6
  out=$(timeout 90 "$WINE" "$PY" -c "
import MetaTrader5 as mt5
if mt5.initialize():
    ti=mt5.terminal_info(); ai=mt5.account_info()
    print('OK' if (ti and ti.connected and ai) else 'WAIT', ai.balance if ai else None, ti.trade_allowed if ti else None)
else:
    print('WAIT', mt5.last_error())
" 2>/dev/null | tail -1)
  printf '  [%02d] %s\n' "$i" "$out"
  case "$out" in OK*) break;; esac
  [ "$i" = 30 ] && die "terminal never reported connected"
done
case "$out" in
  *True*) echo "  AutoTrading: on";;
  *) echo "  WARNING: AutoTrading is OFF (terminal_info.trade_allowed=False)."
     echo "           Check [Experts] Enabled=1 in common.ini, or press Ctrl+E in the terminal window.";;
esac

say "5. starting bot"
systemctl restart crypto-ic-bot.service || die "crypto-ic-bot failed"
sleep 60

say "status"
for u in xvfb101 mt5-ic-terminal crypto-ic-bot; do
  printf '  %-18s %-10s %s\n' "$u" "$(systemctl is-active "$u")" "$(systemctl show -p MainPID --value "$u")"
done
echo
tail -n 3 "$HERE/events.jsonl" 2>/dev/null || echo "  (no events yet)"
echo
python3 "$HERE/report.py" 2>/dev/null | head -12
echo
echo "next:  systemctl status crypto-ic-bot --no-pager"
echo "       python3 $HERE/report.py --live        (needs DISPLAY/:101 for --live, plain otherwise)"
echo "       systemctl restart crypto-ic-bot       (terminal and prefix are untouched by this)"
