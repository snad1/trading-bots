"""The bot trades only the configured demo account and never logs the shared terminal in."""
import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO, REAL = 0, 2


def _load(monkeypatch, **env):
    fake = types.ModuleType('MetaTrader5')
    fake.ACCOUNT_TRADE_MODE_DEMO = DEMO
    monkeypatch.setitem(sys.modules, 'MetaTrader5', fake)
    for key in ('SMC_DEMO_LOGIN', 'SMC_DEMO_SERVER'):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop('smc_bot', None)
    return importlib.import_module('smc_bot'), fake


def _account(login=53158615, server='ICMarketsSC-Demo', mode=DEMO):
    return types.SimpleNamespace(login=login, server=server, trade_mode=mode)


def test_default_account_is_the_metaquotes_demo(monkeypatch):
    bot, _ = _load(monkeypatch)
    assert bot.DEMO_LOGIN == 5059023305
    assert bot.DEMO_SERVER == ''


def test_account_and_server_come_from_the_environment(monkeypatch):
    bot, _ = _load(monkeypatch, SMC_DEMO_LOGIN='53158615', SMC_DEMO_SERVER='ICMarketsSC-Demo')
    assert bot.DEMO_LOGIN == 53158615
    assert bot.account_problem(_account(), bot.DEMO_LOGIN, bot.DEMO_SERVER, DEMO) is None


@pytest.mark.parametrize(
    ('account', 'reason'),
    [
        (None, 'no account info'),
        (_account(mode=REAL), 'not a DEMO'),
        (_account(login=111), 'expected 53158615'),
        (_account(server='ICMarketsSC-Live'), 'expected ICMarketsSC-Demo'),
    ],
)
def test_any_other_account_is_refused(monkeypatch, account, reason):
    bot, _ = _load(monkeypatch)
    assert reason in bot.account_problem(account, 53158615, 'ICMarketsSC-Demo', DEMO)


def test_connect_attaches_without_logging_in_and_refuses_a_wrong_account(monkeypatch):
    bot, fake = _load(monkeypatch, SMC_DEMO_LOGIN='53158615', SMC_DEMO_SERVER='ICMarketsSC-Demo')
    calls = {}
    fake.initialize = lambda **kw: calls.update(kw) or True
    fake.account_info = lambda: _account(login=999)
    fake.shutdown = lambda: calls.update(closed=True)
    with pytest.raises(SystemExit, match='REFUSING'):
        bot.Bot.connect(types.SimpleNamespace())
    assert 'login' not in calls
    assert 'password' not in calls
    assert calls.get('closed') is True
