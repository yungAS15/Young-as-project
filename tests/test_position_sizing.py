import pytest

from risk.position_sizing import allowed_risk, compute_position_size, floor_to_step

BASE = dict(max_risk_ccy=100.0, max_risk_pct=1.0, value_per_point=1.0, min_size=0.01,
            max_size=100.0, size_step=0.01, slippage_buffer=0.0)


def size(**kw):
    args = dict(BASE, entry=2000.0, stop_loss=1995.0, capital_available=20000.0, quote_to_account_rate=1.0)
    args.update(kw)
    return compute_position_size(**args)


def test_allowed_risk_is_min_of_chf_and_pct():
    assert allowed_risk(20000, 100, 1.0) == 100          # 1 % = 200 → plafonné à 100
    assert allowed_risk(5000, 100, 1.0) == 50            # 1 % = 50 < 100
    assert allowed_risk(80, 100, 1.0) == pytest.approx(0.8)   # capital < 100 CHF
    assert allowed_risk(0, 100, 1.0) == 0
    assert allowed_risk(-10, 100, 1.0) == 0


def test_basic_size_floor():
    r = size()  # 100 CHF / 5 USD par unité (taux 1) = 20 unités
    assert r.ok and r.size == 20.0
    assert r.risk_effective == pytest.approx(100.0)


def test_rounds_down_never_up():
    r = size(stop_loss=1997.0)  # 100/3 = 33.333.. → 33.33
    assert r.ok and r.size == 33.33
    assert r.risk_effective <= r.risk_allowed


def test_floor_to_step_decimal_exact():
    assert floor_to_step(0.29999999, 0.01) == 0.29
    assert floor_to_step(1.0, 0.1) == 1.0
    assert floor_to_step(0.7, 0.1) == 0.7   # pas d'erreur binaire 0.7/0.1=6.999…


def test_currency_conversion_usd_to_chf():
    # 5 USD de distance, taux USD→CHF 0.9 → 4.5 CHF/unité ; 100/4.5 = 22.22
    r = size(quote_to_account_rate=0.9)
    assert r.ok and r.size == 22.22
    assert r.loss_per_unit == pytest.approx(4.5)
    assert r.risk_effective == pytest.approx(22.22 * 4.5)


def test_missing_fx_rate_is_no_trade():
    assert not size(quote_to_account_rate=None).ok
    assert not size(quote_to_account_rate=0).ok


def test_small_capital_adapts_risk():
    # capital 50 CHF → risque 0.50 CHF ; 0.5 / 5 = 0.1 unité
    r = size(capital_available=50.0)
    assert r.ok and r.size == pytest.approx(0.1)
    assert r.risk_effective <= 0.5 + 1e-9


def test_min_size_exceeds_risk_is_no_trade():
    # capital 50 → risque 0.5 ; taille min 1 unité = 5 CHF de risque → refus
    r = size(capital_available=50.0, min_size=1.0, size_step=1.0)
    assert not r.ok and r.size == 0
    assert "NO TRADE" in r.reason


def test_max_size_caps_and_lowers_risk():
    r = size(max_size=5.0)
    assert r.ok and r.size == 5.0 and r.risk_effective == pytest.approx(25.0)


def test_slippage_buffer_reduces_size():
    r = size(slippage_buffer=0.25)  # 100 / 5.25 = 19.04
    assert r.size == 19.04


def test_wrong_side_sl_refused():
    assert not size(stop_loss=2001.0).ok
    assert not size(stop_loss=2000.0).ok


def test_zero_capital_refused():
    assert not size(capital_available=0.0).ok


def test_sell_direction_ready():
    r = size(entry=2000.0, stop_loss=2005.0, direction="SELL")
    assert r.ok and r.size == 20.0
