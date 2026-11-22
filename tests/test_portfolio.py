from datetime import datetime, timedelta, timezone

import pytest

import portfolio as pf

UTC = timezone.utc
MON_10 = datetime(2026, 11, 16, 4, 30, tzinfo=UTC)  # Mon 10:00 IST
MON_1516 = datetime(2026, 11, 16, 9, 46, tzinfo=UTC)  # Mon 15:16 IST
TUE_10 = datetime(2026, 11, 17, 4, 30, tzinfo=UTC)


def candle(start, o, h, l, c):
    return {"time": start, "open": o, "high": h, "low": l, "close": c}


def new_long(p, **kw):
    args = dict(symbol="A", name="A", side="LONG", price=100.0, risk=1.0, amount=10000, now=MON_10)
    args.update(kw)
    return pf.open_position(p, **args)


def test_sizing_and_session_rules():
    p = pf.create_portfolio()
    assert p["balance"] == 100000
    pos = new_long(p, symbol="TCS", price=1000.0, risk=10.0, amount=25000)
    assert (pos["qty"], pos["stop"], pos["target"]) == (25, 990.0, 1020.0)
    with pytest.raises(ValueError):
        new_long(p, symbol="TCS")
    with pytest.raises(ValueError):
        new_long(pf.create_portfolio(), now=MON_1516)
    with pytest.raises(ValueError):
        new_long(pf.create_portfolio(), now=datetime(2026, 11, 21, 5, 0, tzinfo=UTC))
    assert new_long(pf.create_portfolio(), now=MON_1516, off_hours=True)


def test_replay_target_and_stop():
    t0 = datetime(2026, 11, 16, 4, 35, tzinfo=UTC)
    p = pf.create_portfolio()
    new_long(p)
    [trade] = pf.settle(p, {"A": [candle(t0, 100, 102.5, 100, 102)]}, MON_10 + timedelta(hours=1))
    assert trade["reason"] == "Target hit"
    p = pf.create_portfolio()
    new_long(p)
    [trade] = pf.settle(p, {"A": [candle(t0, 100, 102.5, 98.5, 100)]}, MON_10 + timedelta(hours=1))
    assert trade["reason"] == "Stop loss hit"
